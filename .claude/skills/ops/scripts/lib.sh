#!/usr/bin/env bash
# Shared helpers for Carrel ops scripts (doctor/heal/agent).
# Source this file; do not execute it directly.

set -u

# --- Paths -------------------------------------------------------------------

# Scripts live at .claude/skills/ops/scripts/, four levels below the repo root.
# Resolve from this file's location so callers work from any cwd.
OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
PROJECT_ROOT="$(cd "$OPS_DIR/../../../.." && pwd)"
SKILL_DIR="$(cd "$OPS_DIR/.." && pwd)"
BYPASS_FILE="$OPS_DIR/bypass-domains.txt"

TS_CLI="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
LOG_DIR="/tmp/carrel-ops"
mkdir -p "$LOG_DIR"
HEAL_LOG="$LOG_DIR/heal.log"

# Service topology. Vite is the single external entrypoint (proxies /api and
# /storage to the backend). Backend and postgres stay on loopback.
VITE_PORT=5173
BACKEND_PORT=8787
MINERU_PORT=8000
POSTGRES_PORT=5432

# The public Tailscale Serve URL is discovered at runtime from the node's
# MagicDNS name, so nothing machine-specific is hardcoded here.
public_url() {
  local dns
  dns=$(ts_dns_name)
  if [ -n "$dns" ]; then
    echo "https://$dns"
  else
    echo "https://<node>.<tailnet>.ts.net"
  fi
}

# --- Output ------------------------------------------------------------------

if [ -t 1 ]; then
  C_GREEN=$'\033[32m'; C_RED=$'\033[31m'; C_YELLOW=$'\033[33m'
  C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'; C_RESET=$'\033[0m'
else
  C_GREEN=""; C_RED=""; C_YELLOW=""; C_BOLD=""; C_DIM=""; C_RESET=""
fi

ok()   { printf "  ${C_GREEN}✔${C_RESET} %s\n" "$*"; }
bad()  { printf "  ${C_RED}✘${C_RESET} %s\n" "$*"; }
warn(){ printf "  ${C_YELLOW}!${C_RESET} %s\n" "$*"; }
info(){ printf "  ${C_DIM}·${C_RESET} %s\n" "$*"; }
section(){ printf "\n${C_BOLD}%s${C_RESET}\n" "$*"; }

log_heal() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$HEAL_LOG"; }

# --- Bypass list -------------------------------------------------------------

# Echo the authoritative bypass domains (comments/blanks stripped).
required_bypass() {
  grep -vE '^\s*(#|$)' "$BYPASS_FILE"
}

# Echo the bypass domains currently set on a service (e.g. "Wi-Fi").
current_bypass() {
  networksetup -getproxybypassdomains "$1" 2>/dev/null
}

# True if every required entry is present on the named service.
bypass_is_complete() {
  local svc="$1" missing=0 entry
  local current
  current="$(current_bypass "$svc")"
  while IFS= read -r entry; do
    [ -z "$entry" ] && continue
    if ! printf '%s\n' "$current" | grep -Fxq "$entry"; then
      missing=1
    fi
  done < <(required_bypass)
  [ "$missing" -eq 0 ]
}

# Physical network services that can carry the system proxy (Tailscale's own
# service is virtual and excluded).
proxy_services() {
  networksetup -listallnetworkservices 2>/dev/null \
    | tail -n +2 \
    | grep -vE '^\*|Tailscale|Thunderbolt Bridge'
}

# --- Port / HTTP checks ------------------------------------------------------

port_listening() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

# http_code URL [curl-extra-args...]
# curl itself prints "000" for failed connections via -w; avoid double-printing.
http_code() {
  local code
  code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$@" 2>/dev/null)
  echo "${code:-000}"
}

# --- Tailscale ---------------------------------------------------------------

ts_available() { [ -x "$TS_CLI" ]; }

ts_dns_name() {
  "$TS_CLI" status --json 2>/dev/null \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null
}

# Serve is configured only when `serve status` returns non-empty output that is
# not the "No serve config" sentinel.
serve_active() {
  [ -x "$TS_CLI" ] || return 1
  local out
  out=$("$TS_CLI" serve status 2>/dev/null) || return 1
  [ -n "$out" ] || return 1
  ! printf '%s' "$out" | grep -qi 'no serve config'
}
