#!/usr/bin/env bash
# PR9/PR9.5: install/reload the three AlphaOS LaunchAgents (scheduler cadence,
# dead-man heartbeat, ledger backup). Mirrors the sibling SG Card Tracker
# project's own launchctl (never cron) house pattern -- copy the plist into
# ~/Library/LaunchAgents, unload any prior copy, then load the fresh one.
#
# Usage:
#   deploy/install_launchagent.sh              # install/reload all three agents
#   deploy/install_launchagent.sh scheduler     # just com.ck.alphaos.scheduler
#   deploy/install_launchagent.sh heartbeat     # just com.ck.alphaos.heartbeat
#   deploy/install_launchagent.sh backup        # just com.ck.alphaos.backup
#
# Safe to re-run: unload is best-effort (ignores "not loaded" errors), then
# every install always (re)loads from the copy just placed in LaunchAgents.

set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs/alphaos"
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

  # PR9.5: validate ProgramArguments[0] (the venv python for the scheduler/
  # heartbeat agents, /bin/bash for the backup agent) BEFORE loading --
  # launchd loads a plist pointing at a missing/non-executable binary
  # silently (no error until the first failed tick, which then pages nobody
  # if alerting itself depends on that same broken interpreter). Fail loud
  # here instead.
  local program_path
  program_path="$(/usr/libexec/PlistBuddy -c "Print :ProgramArguments:0" "$src" 2>/dev/null || true)"
  if [ -z "$program_path" ] || [ ! -x "$program_path" ]; then
    echo "ERROR: $label's ProgramArguments[0] (${program_path:-<empty>}) is not an executable file." >&2
    echo "       If this is the venv python, check it exists (e.g. 'uv venv' / rebuild it) before installing." >&2
    exit 1
  fi

  mkdir -p "$LOG_DIR"
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
    install_agent "com.ck.alphaos.backup"
    ;;
  scheduler)
    install_agent "com.ck.alphaos.scheduler"
    ;;
  heartbeat)
    install_agent "com.ck.alphaos.heartbeat"
    ;;
  backup)
    install_agent "com.ck.alphaos.backup"
    ;;
  *)
    echo "Usage: $0 [all|scheduler|heartbeat|backup]" >&2
    exit 1
    ;;
esac

echo "Done. Check status with:"
echo "  launchctl list | grep com.ck.alphaos"
echo "  tail -f $LOG_DIR/*.log"
