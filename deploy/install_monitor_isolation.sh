#!/usr/bin/env bash
# Stage by default. --activate is an explicit service migration, never a fuse reset.
set -euo pipefail
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$DEPLOY_DIR/.." && pwd)"
DEST_DIR="$HOME/Library/LaunchAgents"
STAGE_DIR="$DEST_DIR/.alphaos-monitor-stage"
DOMAIN="gui/$(id -u)"
MODE="${1:---stage}"
if [[ "$MODE" != --stage && "$MODE" != --activate ]]; then
  echo "Usage: $0 [--stage|--activate]" >&2; exit 2
fi
PROGRAM="$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0' "$DEPLOY_DIR/com.ck.alphaos.monitor.plist")"
CONFIGURED_PROJECT="$(/usr/libexec/PlistBuddy -c 'Print :WorkingDirectory' "$DEPLOY_DIR/com.ck.alphaos.monitor.plist")"
if [[ ! -x "$PROGRAM" || "$PROJECT_DIR" != "$CONFIGURED_PROJECT" ]]; then
  echo "Refusing: run from the deployed project with its configured Python runtime." >&2; exit 2
fi
mkdir -p "$STAGE_DIR" "$HOME/Library/Logs/alphaos"
for role in scheduler monitor heartbeat; do
  /usr/bin/plutil -lint "$DEPLOY_DIR/com.ck.alphaos.$role.plist"
  cp "$DEPLOY_DIR/com.ck.alphaos.$role.plist" "$STAGE_DIR/"
done
if [[ "$MODE" == --stage ]]; then
  echo "Staged three plists in $STAGE_DIR. No service changed. Review then use --activate."
  exit 0
fi
cd "$PROJECT_DIR"
check_quiet() {
  local role state pid worker_state
  for role in scheduler monitor; do
    state="$(launchctl print "$DOMAIN/com.ck.alphaos.$role" 2>/dev/null || true)"
    pid="$(awk '$1 == "pid" && $2 == "=" {print $3}' <<< "$state")"
    worker_state="$(awk '$1 == "state" && $2 == "=" {print $3}' <<< "$state")"
    if [[ ( -n "$pid" && "$pid" != 0 ) || "$worker_state" == running ]]; then
      echo "Refusing migration while $role PID ${pid:-unknown} is active. Let it finish and rerun. No process killed." >&2
      return 1
    fi
  done
}
# Check before changing any enabled state, then repeat after disabling future
# launches to close the scheduling race. Pre-bootout refusal restores state.
check_quiet
"$PROGRAM" -m alphaos.scheduler.worker --role preflight
BACKUP_DIR="$DEST_DIR/.alphaos-monitor-backups/$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$BACKUP_DIR"
chmod 700 "$DEST_DIR/.alphaos-monitor-backups" "$BACKUP_DIR"
for role in scheduler monitor heartbeat; do
  if [[ -f "$DEST_DIR/com.ck.alphaos.$role.plist" ]]; then
    cp -p "$DEST_DIR/com.ck.alphaos.$role.plist" "$BACKUP_DIR/"
    shasum -a 256 "$BACKUP_DIR/com.ck.alphaos.$role.plist" >> "$BACKUP_DIR/SHA256SUMS"
  else
    echo "$role: previously absent" >> "$BACKUP_DIR/previous-state.txt"
  fi
done
# Store only enabled flags, never launchctl's environment dump.
disabled="$(launchctl print-disabled "$DOMAIN")"
prior_scheduler="$(awk '$1 == "\"com.ck.alphaos.scheduler\"" {print $3}' <<< "$disabled")"
prior_monitor="$(awk '$1 == "\"com.ck.alphaos.monitor\"" {print $3}' <<< "$disabled")"
printf 'scheduler_disabled=%s\nmonitor_disabled=%s\n' "${prior_scheduler:-false}" "${prior_monitor:-false}" >> "$BACKUP_DIR/previous-state.txt"
changed=false
bootout_started=false
finished=false
on_exit() {
  local code=$?
  if [[ "$finished" != true && "$changed" == true ]]; then
    if [[ "$bootout_started" != true ]]; then
      if [[ "$prior_scheduler" == true ]]; then
        launchctl disable "$DOMAIN/com.ck.alphaos.scheduler" || true
      else
        launchctl enable "$DOMAIN/com.ck.alphaos.scheduler" || true
      fi
      if [[ "$prior_monitor" == true ]]; then
        launchctl disable "$DOMAIN/com.ck.alphaos.monitor" || true
      else
        launchctl enable "$DOMAIN/com.ck.alphaos.monitor" || true
      fi
      echo "Migration refused before service replacement; previous enabled states restored." >&2
    else
      # Never restart the old combined scheduler alongside a new monitor.
      # Keep the installed monitor scheduled while new entries remain stopped.
      launchctl disable "$DOMAIN/com.ck.alphaos.scheduler" || true
      launchctl enable "$DOMAIN/com.ck.alphaos.monitor" || true
      echo "Partial migration: research remains stopped. Check monitor/heartbeat service state and logs; originals preserved in $BACKUP_DIR. No fallback starts the old combined worker." >&2
    fi
  fi
  exit "$code"
}
trap on_exit EXIT
changed=true
for role in scheduler monitor; do
  launchctl disable "$DOMAIN/com.ck.alphaos.$role"
done
check_quiet
"$PROGRAM" -m alphaos.scheduler.worker --role preflight
bootout_started=true
for role in scheduler monitor heartbeat; do
  if launchctl print "$DOMAIN/com.ck.alphaos.$role" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN/com.ck.alphaos.$role"
  fi
  cp "$STAGE_DIR/com.ck.alphaos.$role.plist" "$DEST_DIR/"
done
launchctl enable "$DOMAIN/com.ck.alphaos.monitor"
launchctl bootstrap "$DOMAIN" "$DEST_DIR/com.ck.alphaos.monitor.plist"
# No research/entry service is loaded until a real monitor pass is healthy.
ready=false
for attempt in {1..10}; do
  if "$PROGRAM" -m alphaos.scheduler.worker --role readiness; then
    ready=true; break
  fi
  sleep 3
done
launchctl enable "$DOMAIN/com.ck.alphaos.heartbeat"
launchctl bootstrap "$DOMAIN" "$DEST_DIR/com.ck.alphaos.heartbeat.plist"
if [[ "$ready" != true ]]; then
  echo "Monitor/heartbeat installed, research remains stopped: dedicated monitor is not ready. Inspect its logs/ledger; no fuse or incident cleared." >&2
  exit 1
fi
launchctl enable "$DOMAIN/com.ck.alphaos.scheduler"
launchctl bootstrap "$DOMAIN" "$DEST_DIR/com.ck.alphaos.scheduler.plist"
finished=true
echo "Installed isolated monitor, read-only monitor heartbeat, and research worker. Originals: $BACKUP_DIR"
