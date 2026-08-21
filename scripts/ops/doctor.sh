#!/usr/bin/env bash
# Carrel ops health check. Read-only: reports status and exits non-zero if
# anything needs attention. Run `heal.sh` to auto-fix what can be fixed safely.
#
# Usage: scripts/ops/doctor.sh [--json]

set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

JSON=0
[ "${1:-}" = "--json" ] && JSON=1
if [ "$JSON" -eq 1 ]; then
  # Suppress human-readable lines; only emit JSON at the end.
  ok(){ :; }; bad(){ :; }; warn(){ :; }; info(){ :; }; section(){ :; }
fi
issues=0

# A check: name; "ok"|"bad"|"warn"; detail. For text mode we print as we go;
# for JSON mode we collect and emit at the end.
declare -a J_NAMES J_STATUS J_DETAIL
record() {
  local name="$1" status="$2" detail="${3:-}"
  J_NAMES+=("$name"); J_STATUS+=("$status"); J_DETAIL+=("$detail")
  case "$status" in
    ok)   ok "$name${detail:+ — $detail}" ;;
    bad)  bad "$name${detail:+ — $detail}"; issues=$((issues+1)) ;;
    warn) warn "$name${detail:+ — $detail}" ;;
  esac
}

emit_json() {
  python3 - "$issues" \
    <(printf '%s\n' "${J_NAMES[@]}") \
    <(printf '%s\n' "${J_STATUS[@]}") \
    <(printf '%s\n' "${J_DETAIL[@]}") <<'PY'
import json, sys
issues = int(sys.argv[1])
def load(p):
    with open(p) as f: return [l.rstrip("\n") for l in f]
names, statuses, details = load(sys.argv[2]), load(sys.argv[3]), load(sys.argv[4])
checks = [{"name": n, "status": s, "detail": d}
          for n, s, d in zip(names, statuses, details)]
print(json.dumps({"healthy": issues == 0, "issue_count": issues, "checks": checks},
                 ensure_ascii=False, indent=2))
PY
}

section "Processes & ports"
if port_listening "$VITE_PORT"; then
  pid=$(lsof -nP -iTCP:$VITE_PORT -sTCP:LISTEN -t 2>/dev/null | head -1)
  record "Vite dev server (:$VITE_PORT)" ok "pid $pid, bound 0.0.0.0 (LAN/tailnet reachable)"
else
  record "Vite dev server (:$VITE_PORT)" bad "not running — make frontend"
fi
if port_listening "$BACKEND_PORT"; then
  record "FastAPI backend (:$BACKEND_PORT)" ok "listening"
else
  record "FastAPI backend (:$BACKEND_PORT)" bad "not running — make backend"
fi
if port_listening "$POSTGRES_PORT"; then
  record "Postgres (:$POSTGRES_PORT)" ok
else
  record "Postgres (:$POSTGRES_PORT)" bad "not running — make up"
fi
if port_listening "$MINERU_PORT"; then
  record "MinerU parser (:$MINERU_PORT)" ok
else
  record "MinerU parser (:$MINERU_PORT)" warn "down (optional; PDF parsing unavailable) — make mineru-up"
fi

section "Application health"
if port_listening "$BACKEND_PORT"; then
  body=$(curl -s -m 5 "http://127.0.0.1:$BACKEND_PORT/health" 2>/dev/null)
  if echo "$body" | grep -q '"status":"ok"'; then
    db=$(echo "$body" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("db","?"))' 2>/dev/null)
    record "Backend /health" ok "db=$db"
  else
    record "Backend /health" bad "unexpected response: ${body:0:120}"
  fi
fi
if port_listening "$VITE_PORT"; then
  code=$(http_code "http://127.0.0.1:$VITE_PORT/")
  if [ "$code" = "200" ]; then
    record "Vite serves index" ok
  else
    record "Vite serves index" bad "HTTP $code"
  fi
  pcode=$(http_code "http://127.0.0.1:$VITE_PORT/api/health")
  if [ "$pcode" = "200" ]; then
    record "Vite /api proxy → backend" ok
  else
    record "Vite /api proxy → backend" bad "HTTP $pcode"
  fi
fi

section "Network ingress"
if ts_available; then
  if "$TS_CLI" status >/dev/null 2>&1; then
    dns=$(ts_dns_name)
    pub=$(public_url)
    record "Tailscale daemon" ok "$dns"
    if serve_active; then
      record "Tailscale Serve (HTTPS ingress)" ok "serving — $pub"
    else
      record "Tailscale Serve (HTTPS ingress)" bad \
        "not configured — run: tailscale serve --bg $VITE_PORT"
    fi
  else
    record "Tailscale daemon" bad "installed but not logged in / stopped"
  fi
else
  record "Tailscale CLI" bad "not found at $TS_CLI"
fi

# System proxy bypass — the recurring FlashFox footgun.
section "System proxy bypass (Tailscale/LAN must bypass GUI proxy)"
while IFS= read -r svc; do
  [ -z "$svc" ] && continue
  if bypass_is_complete "$svc"; then
    record "Proxy bypass on '$svc'" ok "100.*/ts.net present"
  else
    missing=$(comm -23 <(required_bypass | sort -u) <(current_bypass "$svc" | sort -u) \
              | paste -sd, -)
    record "Proxy bypass on '$svc'" bad "missing: $missing — run heal.sh"
  fi
done < <(proxy_services)

section "End-to-end reachability"
if ts_available && "$TS_CLI" status >/dev/null 2>&1; then
  ts_ip=$("$TS_CLI" ip -4 2>/dev/null | head -1)
  if [ -n "$ts_ip" ]; then
    code=$(http_code --noproxy '*' "http://$ts_ip:$VITE_PORT/")
    if [ "$code" = "200" ]; then
      record "Direct tailnet IP ($ts_ip:$VITE_PORT)" ok
    else
      record "Direct tailnet IP ($ts_ip:$VITE_PORT)" bad "HTTP $code (bypassing proxy)"
    fi
  fi
  if serve_active; then
    # Bypass the shell's ALL_PROXY: curl reads env vars, not the system bypass
    # list. Tailscale Serve terminates TLS locally on 443.
    dns=$(ts_dns_name)
    if [ -n "$dns" ]; then
      code=$(http_code --noproxy '*' "https://$dns/" 2>/dev/null)
      if [ "$code" = "200" ]; then
        record "Tailscale Serve URL ($(public_url))" ok "HTTPS 200"
      else
        record "Tailscale Serve URL ($(public_url))" warn "HTTP $code (cert may still be provisioning — retry in ~30s)"
      fi
    fi
  fi
fi

echo
if [ "$JSON" -eq 1 ]; then
  emit_json
  exit "$issues"
fi
if [ "$issues" -eq 0 ]; then
  echo "${C_GREEN}All critical checks passed.${C_RESET}"
else
  echo "${C_RED}$issues issue(s).${C_RESET} Run ${C_BOLD}scripts/ops/heal.sh${C_RESET} to auto-fix."
fi
exit "$issues"
