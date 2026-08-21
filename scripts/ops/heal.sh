#!/usr/bin/env bash
# Auto-remediate Carrel ops issues that are safe to fix without prompting.
#
# Safe auto-fixes:
#   - re-apply the authoritative system-proxy bypass list when FlashFox/闪狐云
#     has overwritten it (this is the recurring 502 cause).
#
# Reported but NOT auto-applied (need interactive auth / a human decision):
#   - starting Tailscale Serve (one-time GUI/sudo grant)
#   - (re)starting backend/frontend/postgres — user manages these for now
#
# Usage: scripts/ops/heal.sh [--dry-run] [--agent]
#   --agent   quiet mode for periodic launchd runs; only emits when it fixes
#             something (errors/warnings suppressed to keep the log clean).

set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

DRY_RUN=0
AGENT=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --agent)   AGENT=1 ;;
  esac
done

# In agent mode, silence helpers unless we actually fix something.
if [ "$AGENT" -eq 1 ]; then
  ok(){ :; }
  info(){ :; }
  section(){ :; }
  warn(){ :; }
  bad(){ log_heal "ERROR: $*"; }
fi

# Ensure every required entry is present on the service while MERGING (not
# replacing) whatever is already there — this preserves the GUI proxy client's
# own entries (e.g. CDN/direct-connect domains) instead of clobbering them.
apply_bypass() {
  local svc="$1"
  local current merged missing
  current="$(current_bypass "$svc")"
  # Union of current + required, de-duplicated, preserving no order requirement.
  merged=$( { printf '%s\n' "$current"; required_bypass; } \
            | grep -vE '^\s*$' | awk '!seen[$0]++' )
  missing=$(comm -23 <(required_bypass | sort -u) <(printf '%s\n' "$current" | sort -u) \
            | paste -sd, -)

  local merged_arr=()
  while IFS= read -r line; do merged_arr+=("$line"); done <<< "$merged"

  if [ "$DRY_RUN" -eq 1 ]; then
    warn "[dry-run] would add [$missing] to '$svc' (total ${#merged_arr[@]} entries)"
    return 0
  fi
  if networksetup -setproxybypassdomains "$svc" "${merged_arr[@]}" 2>/dev/null; then
    ok "Merged required bypass entries on '$svc' (added: $missing)"
    log_heal "proxy bypass on '$svc' added: $missing"
  else
    bad "Failed to set bypass on '$svc' (may need sudo / Full Disk Access)"
    return 1
  fi
}

[ "$AGENT" -eq 0 ] && echo "${C_BOLD}Carrel heal${C_RESET}"
fixed=0

section "System proxy bypass"
while IFS= read -r svc; do
  [ -z "$svc" ] && continue
  if bypass_is_complete "$svc"; then
    ok "'$svc' bypass list complete"
  else
    missing=$(comm -23 <(required_bypass | sort -u) <(current_bypass "$svc" | sort -u) \
            | paste -sd, -)
    warn "'$svc' missing: $missing"
    apply_bypass "$svc" && fixed=$((fixed+1))
  fi
done < <(proxy_services)

# The informational tail (Serve nudges, manual service reminders) is irrelevant
# to the periodic agent and would just spam the launchd log.
if [ "$AGENT" -eq 0 ]; then
section "Tailscale Serve"
if ts_available && "$TS_CLI" status >/dev/null 2>&1; then
  if serve_active; then
    ok "Tailscale Serve active — $(public_url)"
  else
    warn "Tailscale Serve not configured. To expose Carrel over HTTPS on your"
    warn "tailnet (the real fix for proxy-induced 502s), run once in a terminal:"
    echo
    echo "    \"$TS_CLI\" serve --bg $VITE_PORT"
    echo
    warn "It may pop a macOS grant dialog; approve it. Config persists across"
    warn "reboots. Then open https://<node>.<tailnet>.ts.net"
  fi
else
  warn "Tailscale not available/logged in; skipping Serve check."
fi

section "Services (manual)"
port_listening "$VITE_PORT"    || warn "Vite down:  cd frontend && npm run dev"
port_listening "$BACKEND_PORT" || warn "Backend down: uv run uvicorn carrel.main:app --host 127.0.0.1 --port 8787 --reload"
port_listening "$POSTGRES_PORT" || warn "Postgres down: make up"

echo
if [ "$fixed" -gt 0 ]; then
  ok "Applied $fixed auto-fix(es). Log: $HEAL_LOG"
else
  info "Nothing safe to auto-fix right now."
fi
fi
