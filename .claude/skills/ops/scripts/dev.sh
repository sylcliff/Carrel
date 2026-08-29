#!/usr/bin/env bash
# Start / stop / restart Carrel's dev servers (backend + frontend).
#
# Postgres is managed by `make up` (docker compose) and MinerU by
# `make mineru-up`; this script only owns the two long-running dev servers.
# Both are launched detached with logs under /tmp/carrel-ops; it then waits for
# their health endpoints so "restart" is one command that returns only when the
# app is actually reachable.
#
# Usage: .claude/skills/ops/scripts/dev.sh <start|stop|restart|status>
#
# Always resolve paths from the script location, so it works no matter what the
# caller's cwd is (the cwd-relative `scripts/...` path is a recurring footgun).

set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"
cd "$PROJECT_ROOT"

BACKEND_LOG="$LOG_DIR/backend.log"
FRONTEND_LOG="$LOG_DIR/frontend.log"
# PID files let `stop` target the parent shell we spawned (the nohup wrapper
# of `uv run uvicorn` or `( cd ... && nohup npm run dev )`). Without them an
# overlapping `make restart` can leave an orphan that races the new listener
# for the port, which then makes `wait_http` spin to its 30s budget.
BACKEND_PID_FILE="$LOG_DIR/backend.pid"
FRONTEND_PID_FILE="$LOG_DIR/frontend.pid"

start_backend() {
  if port_listening "$BACKEND_PORT"; then
    info "backend already running on :$BACKEND_PORT"
    return 0
  fi
  info "starting backend (uvicorn --reload)..."
  # Match the Makefile: prefer `uv run uvicorn`, fall back to the venv python.
  if command -v uv >/dev/null 2>&1; then
    nohup uv run uvicorn carrel.main:app --host 127.0.0.1 --port "$BACKEND_PORT" --reload \
      > "$BACKEND_LOG" 2>&1 &
    echo $! > "$BACKEND_PID_FILE"
  else
    nohup python -m uvicorn carrel.main:app --host 127.0.0.1 --port "$BACKEND_PORT" --reload \
      > "$BACKEND_LOG" 2>&1 &
    echo $! > "$BACKEND_PID_FILE"
  fi
}

start_frontend() {
  if port_listening "$VITE_PORT"; then
    info "frontend already running on :$VITE_PORT"
    return 0
  fi
  info "starting frontend (vite)..."
  ( cd "$PROJECT_ROOT/frontend" && nohup npm run dev > "$FRONTEND_LOG" 2>&1 ) &
  echo $! > "$FRONTEND_PID_FILE"
}

stop_port() {
  local port="$1" name="$2" pidfile="${3:-}"
  # Target the tracked parent first — that's the nohup wrapper around `uv
  # run uvicorn`, which holds the port through the worker process tree.
  if [ -n "$pidfile" ] && [ -f "$pidfile" ]; then
    local pid
    pid=$(cat "$pidfile" 2>/dev/null || true)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      info "stopping $name (pid $pid)"
      kill "$pid" 2>/dev/null || true
    fi
    rm -f "$pidfile"
  fi
  if ! port_listening "$port"; then
    return 0
  fi
  # Anything still holding the port is an orphan from a previous run; reap it.
  info "stopping $name on :$port (orphan listeners)"
  lsof -ti tcp:"$port" | xargs -r kill 2>/dev/null || true
  for _ in 1 2 3 4 5; do
    port_listening "$port" || return 0
    sleep 1
  done
  lsof -ti tcp:"$port" | xargs -r kill -9 2>/dev/null || true
  for _ in 1 2 3; do
    port_listening "$port" || return 0
    sleep 1
  done
  return 1
}

# Probe a URL until 200 or the budget runs out. Sub-second polling catches
# warm starts in <200ms (the common case); the backoff stretches to 5s as
# the budget nears so a slow `carrel.main:app` import still gets multiple
# attempts instead of looking like a hang.
wait_http() {
  local url="$1" name="$2" budget="${3:-30}"
  local deadline=$(( $(date +%s) + budget ))
  local sleeps=(0.2 0.5 1 2 2 2 5)
  local attempt=0
  local started=$SECONDS
  while :; do
    if [ "$(http_code "$url")" = "200" ]; then
      ok "$name ready ($url) [${SECONDS}s, $((attempt + 1)) probes]"
      return 0
    fi
    [ "$(date +%s)" -ge "$deadline" ] && break
    local s=${sleeps[$((attempt % ${#sleeps[@]}))]}
    sleep "$s"
    attempt=$((attempt + 1))
    # After ~1s of waiting, surface a "still going" line every few attempts
    # so a slow import doesn't read as a frozen script.
    if [ "$((SECONDS - started))" -ge 1 ] && [ $((attempt % 4)) = 0 ]; then
      info "$name still waiting on $url ($((SECONDS - started))s elapsed, $attempt probes)"
    fi
  done
  bad "$name did not become ready at $url within ${budget}s (see $LOG_DIR)"
  return 1
}

cmd="${1:-status}"
case "$cmd" in
  start)
    section "Starting Carrel dev servers"
    start_backend
    start_frontend
    # Probe all three endpoints in parallel — the slowest one is the
    # bottleneck, not the sum of three serial waits. Warm restarts finish
    # in <1s; cold starts (uvicorn's first import) typically 2-4s.
    wait_http "http://127.0.0.1:$BACKEND_PORT/health" "backend" 30 &
    p1=$!
    wait_http "http://127.0.0.1:$VITE_PORT/" "frontend" 30 &
    p2=$!
    wait_http "http://127.0.0.1:$VITE_PORT/api/health" "frontend→backend proxy" 30 &
    p3=$!
    # `wait` returns the first non-zero exit from any child, so the script
    # fails loudly if any health check times out.
    wait "$p1" "$p2" "$p3"
    ;;
  stop)
    section "Stopping Carrel dev servers"
    stop_port "$VITE_PORT" "frontend" "$FRONTEND_PID_FILE"
    stop_port "$BACKEND_PORT" "backend" "$BACKEND_PID_FILE"
    # Belt-and-suspenders: reap any `uv run` parent that outlived the port.
    pkill -f "uvicorn carrel.main:app" 2>/dev/null || true
    ok "dev servers stopped"
    ;;
  restart)
    "$0" stop
    "$0" start
    ;;
  status)
    section "Carrel dev server status"
    if port_listening "$BACKEND_PORT"; then
      ok "backend listening on :$BACKEND_PORT"
    else
      bad "backend not running on :$BACKEND_PORT"
    fi
    if port_listening "$VITE_PORT"; then
      ok "frontend listening on :$VITE_PORT"
    else
      bad "frontend not running on :$VITE_PORT"
    fi
    ;;
  *)
    echo "usage: $0 <start|stop|restart|status>" >&2
    exit 2
    ;;
esac
