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
  else
    nohup python -m uvicorn carrel.main:app --host 127.0.0.1 --port "$BACKEND_PORT" --reload \
      > "$BACKEND_LOG" 2>&1 &
  fi
}

start_frontend() {
  if port_listening "$VITE_PORT"; then
    info "frontend already running on :$VITE_PORT"
    return 0
  fi
  info "starting frontend (vite)..."
  ( cd "$PROJECT_ROOT/frontend" && nohup npm run dev > "$FRONTEND_LOG" 2>&1 ) &
}

stop_port() {
  local port="$1" name="$2"
  if port_listening "$port"; then
    info "stopping $name on :$port"
    lsof -ti tcp:"$port" | xargs kill 2>/dev/null || true
    # Wait briefly, then force-kill anything still holding the port.
    for _ in 1 2 3 4 5; do
      port_listening "$port" || return 0
      sleep 1
    done
    lsof -ti tcp:"$port" | xargs kill -9 2>/dev/null || true
  fi
}

wait_http() {
  local url="$1" name="$2" tries="${3:-30}"
  for ((i=1; i<=tries; i++)); do
    if [ "$(http_code "$url")" = "200" ]; then
      ok "$name ready ($url)"
      return 0
    fi
    sleep 1
  done
  bad "$name did not become ready at $url (see $LOG_DIR)"
  return 1
}

cmd="${1:-status}"
case "$cmd" in
  start)
    section "Starting Carrel dev servers"
    start_backend
    start_frontend
    wait_http "http://127.0.0.1:$BACKEND_PORT/health" "backend"
    wait_http "http://127.0.0.1:$VITE_PORT/" "frontend"
    wait_http "http://127.0.0.1:$VITE_PORT/api/health" "frontend→backend proxy"
    ;;
  stop)
    section "Stopping Carrel dev servers"
    stop_port "$VITE_PORT" "frontend"
    stop_port "$BACKEND_PORT" "backend"
    # Also reap a detached `uv run` parent that may outlive its listener child.
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
