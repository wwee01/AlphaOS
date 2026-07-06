#!/usr/bin/env bash
# PR9: stop + remove the AlphaOS LaunchAgents. Per the master build plan's
# own doctrine ("stopping is always the easiest action"), this is the
# one-command way to fully turn off unattended cadence -- it does NOT touch
# the kill switch, any open positions, or any data; it only stops the
# scheduler/heartbeat processes from being invoked by launchd going forward.
#
# Usage:
#   deploy/uninstall_launchagent.sh              # remove both agents
#   deploy/uninstall_launchagent.sh scheduler     # just com.ck.alphaos.scheduler
#   deploy/uninstall_launchagent.sh heartbeat     # just com.ck.alphaos.heartbeat

set -euo pipefail

DEST_DIR="$HOME/Library/LaunchAgents"
TARGET="${1:-all}"

remove_agent() {
  local label="$1"
  local dest="$DEST_DIR/${label}.plist"

  if [ -f "$dest" ]; then
    launchctl unload "$dest" 2>/dev/null || true
    rm -f "$dest"
    echo "Removed $label ($dest)"
  else
    echo "$label was not installed ($dest not found)"
  fi
}

case "$TARGET" in
  all)
    remove_agent "com.ck.alphaos.scheduler"
    remove_agent "com.ck.alphaos.heartbeat"
    ;;
  scheduler)
    remove_agent "com.ck.alphaos.scheduler"
    ;;
  heartbeat)
    remove_agent "com.ck.alphaos.heartbeat"
    ;;
  *)
    echo "Usage: $0 [all|scheduler|heartbeat]" >&2
    exit 1
    ;;
esac
