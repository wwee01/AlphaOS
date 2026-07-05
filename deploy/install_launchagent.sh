#!/usr/bin/env bash
# PR9: install/reload the two AlphaOS LaunchAgents (scheduler cadence +
# dead-man heartbeat). Mirrors the sibling SG Card Tracker project's own
# launchctl (never cron) house pattern -- copy the plist into
# ~/Library/LaunchAgents, unload any prior copy, then load the fresh one.
#
# Usage:
#   deploy/install_launchagent.sh              # install/reload both agents
#   deploy/install_launchagent.sh scheduler     # just com.ck.alphaos.scheduler
#   deploy/install_launchagent.sh heartbeat     # just com.ck.alphaos.heartbeat
#
# Safe to re-run: unload is best-effort (ignores "not loaded" errors), then
# every install always (re)loads from the copy just placed in LaunchAgents.

set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST_DIR="$HOME/Library/LaunchAgents"
TARGET="${1:-all}"

install_agent() {
  local label="$1"
  local plist_name="${label}.plist"
  local src="$DEPLOY_DIR/$plist_name"
  local dest="$DEST_DIR/$plist_name"

  if [ ! -f "$src" ]; then
    echo "ERROR: $src not found" >&2
    exit 1
  fi

  echo "Installing $label ..."
  mkdir -p "$DEST_DIR"
  cp "$src" "$dest"

  # Best-effort unload of any prior copy (ignore "not loaded" failures).
  launchctl unload "$dest" 2>/dev/null || true
  launchctl load "$dest"
  echo "  loaded from $dest"
}

case "$TARGET" in
  all)
    install_agent "com.ck.alphaos.scheduler"
    install_agent "com.ck.alphaos.heartbeat"
    ;;
  scheduler)
    install_agent "com.ck.alphaos.scheduler"
    ;;
  heartbeat)
    install_agent "com.ck.alphaos.heartbeat"
    ;;
  *)
    echo "Usage: $0 [all|scheduler|heartbeat]" >&2
    exit 1
    ;;
esac

echo "Done. Check status with:"
echo "  launchctl list | grep com.ck.alphaos"
echo "  tail -f /tmp/alphaos-scheduler.log /tmp/alphaos-heartbeat.log"
