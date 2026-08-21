#!/usr/bin/env bash
# Install (or uninstall) the Carrel ops self-heal launchd agent.
#
# It runs heal.sh --agent every 60s as the current user, re-applying the
# system-proxy bypass list if the GUI proxy client clobbers it. This is what
# makes the Tailscale/LAN access durable against FlashFox rewrites.
#
# Usage:
#   scripts/ops/install-agent.sh install   # load (default)
#   scripts/ops/install-agent.sh uninstall # unload and remove
#   scripts/ops/install-agent.sh status

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LABEL="com.carrel.ops-agent"
PLIST_SRC="$SCRIPT_DIR/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="/tmp/carrel-ops"

action="${1:-install}"

case "$action" in
  install)
    mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
    # Substitute the absolute project root into the plist.
    sed "s#__PROJECT_ROOT__#$PROJECT_ROOT#g" "$PLIST_SRC" > "$PLIST_DST"
    chmod 644 "$PLIST_DST"
    # Reload (unload first so re-runs are idempotent).
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    launchctl load "$PLIST_DST"
    echo "✔ Installed and loaded: $LABEL"
    echo "  plist: $PLIST_DST"
    echo "  runs:  every 60s, calls heal.sh --agent"
    echo "  logs:  $LOG_DIR/agent.{out,err}  and  $LOG_DIR/heal.log"
    echo "  tip:   scripts/ops/install-agent.sh status"
    ;;
  uninstall)
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    rm -f "$PLIST_DST"
    echo "✔ Uninstalled $LABEL"
    ;;
  status)
    if [ -f "$PLIST_DST" ]; then
      echo "plist:    installed ($PLIST_DST)"
    else
      echo "plist:    not installed"
    fi
    if launchctl list "$LABEL" >/dev/null 2>&1; then
      pid=$(launchctl list "$LABEL" 2>/dev/null | awk '/"PID"/{print $3}' | tr -d ';')
      last=$(launchctl list "$LABEL" 2>/dev/null | awk '/"LastExitStatus"/{print $3}' | tr -d ';')
      echo "agent:    loaded (pid=${pid:-—}, last exit=${last:-—})"
      echo "recent heal actions:"
      tail -n 5 "$LOG_DIR/heal.log" 2>/dev/null || echo "  (none yet)"
    else
      echo "agent:    not loaded"
    fi
    ;;
  *)
    echo "usage: $0 {install|uninstall|status}" >&2
    exit 2
    ;;
esac
