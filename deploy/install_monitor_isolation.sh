#!/usr/bin/env bash
# Stage by default. Activation/resume require the operator's reviewed identities.
set -euo pipefail
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$DEPLOY_DIR/.." && pwd)"
DEST_DIR="$HOME/Library/LaunchAgents"
STAGE_DIR="$DEST_DIR/.alphaos-monitor-stage"
DOMAIN="gui/$(id -u)"
MODE=--stage
EXPECTED_HEAD=
EXPECTED_ENV_SHA256=
usage() {
  echo "Usage: $0 [--stage | --activate | --resume] [--expected-head SHA --expected-env-sha256 HASH]" >&2
}
while (( $# )); do
  case "$1" in
    --stage|--activate|--resume) MODE="$1"; shift ;;
    --expected-head|--expected-env-sha256)
      if (( $# < 2 )); then usage; exit 2; fi
      if [[ "$1" == --expected-head ]]; then EXPECTED_HEAD="$2"; else EXPECTED_ENV_SHA256="$2"; fi
      shift 2 ;;
    *) usage; exit 2 ;;
  esac
done
if [[ "$MODE" != --stage && ( ! "$EXPECTED_HEAD" =~ ^[0-9a-f]{40}$ || ! "$EXPECTED_ENV_SHA256" =~ ^[0-9a-f]{64}$ ) ]]; then
  echo "Activation/resume requires the reviewed full commit SHA and .env SHA-256." >&2
  usage; exit 2
fi
PROGRAM="$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0' "$DEPLOY_DIR/com.ck.alphaos.monitor.plist")"
CONFIGURED_PROJECT="$(/usr/libexec/PlistBuddy -c 'Print :WorkingDirectory' "$DEPLOY_DIR/com.ck.alphaos.monitor.plist")"
if [[ "$MODE" != --stage && ( ! -x "$PROGRAM" || "$PROJECT_DIR" != "$CONFIGURED_PROJECT" ) ]]; then
  echo "Refusing: activate/resume from the deployed project with its configured Python runtime." >&2; exit 2
fi
cd "$PROJECT_DIR"
verify_reviewed() {
  local actual_head actual_env dirty
  actual_head="$(git rev-parse HEAD)"
  dirty="$(git status --porcelain --untracked-files=no)"
  actual_env="$(shasum -a 256 .env | awk '{print $1}')"
  if [[ "$actual_head" != "$EXPECTED_HEAD" || -n "$dirty" || "$actual_env" != "$EXPECTED_ENV_SHA256" ]]; then
    echo "Refusing: reviewed HEAD, clean tracked files, or .env SHA-256 does not match. No configuration values printed." >&2
    return 1
  fi
}
if [[ "$MODE" != --stage ]]; then verify_reviewed; fi
mkdir -p "$STAGE_DIR" "$HOME/Library/Logs/alphaos"
for role in scheduler monitor heartbeat; do
  /usr/bin/plutil -lint "$DEPLOY_DIR/com.ck.alphaos.$role.plist"
  cp "$DEPLOY_DIR/com.ck.alphaos.$role.plist" "$STAGE_DIR/"
done
if [[ "$MODE" == --stage ]]; then
  echo "Staged three plists in $STAGE_DIR. No service changed. Activation requires reviewed HEAD and .env hash arguments."
  exit 0
fi
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
is_loaded() { launchctl print "$DOMAIN/com.ck.alphaos.$1" >/dev/null 2>&1; }
verified_loaded() {
  local role="$1" state expected_role arguments
  case "$role" in scheduler) expected_role=research ;; heartbeat) expected_role=health ;; *) expected_role=monitor ;; esac
  cmp -s "$STAGE_DIR/com.ck.alphaos.$role.plist" "$DEST_DIR/com.ck.alphaos.$role.plist" || return 1
  state="$(launchctl print "$DOMAIN/com.ck.alphaos.$role" 2>/dev/null)" || return 1
  # Verify actual registered program/arguments, not just the file on disk.
  [[ "$(sed -n 's/^[[:space:]]*program = //p' <<< "$state")" == "$PROGRAM" ]] || return 1
  [[ "$(sed -n 's/^[[:space:]]*working directory = //p' <<< "$state")" == "$PROJECT_DIR" ]] || return 1
  arguments="$(awk '/^[[:space:]]*arguments = \{/ {inside=1; next} inside && /^[[:space:]]*}/ {exit} inside {sub(/^[[:space:]]*/, ""); print}' <<< "$state")"
  [[ "$arguments" == "$(printf '%s\n-m\nalphaos.scheduler.worker\n--role\n%s' "$PROGRAM" "$expected_role")" ]]
}
ensure_independent() {
  local role="$1"
  if is_loaded "$role"; then
    if ! verified_loaded "$role"; then
      echo "$role: loaded definition is unverified; no process killed or duplicate started." >&2
      return 1
    fi
  else
    # Only the reviewed independent definition can be recovered.
    cp "$STAGE_DIR/com.ck.alphaos.$role.plist" "$DEST_DIR/" || return 1
    launchctl enable "$DOMAIN/com.ck.alphaos.$role" || return 1
    launchctl bootstrap "$DOMAIN" "$DEST_DIR/com.ck.alphaos.$role.plist" || return 1
  fi
  launchctl enable "$DOMAIN/com.ck.alphaos.$role" || return 1
  verified_loaded "$role"
}
BACKUP_DIR=
changed=false
bootout_started=false
finished=false
prior_scheduler=false
prior_monitor=false
on_exit() {
  local code=$? role monitor_state heartbeat_state
  trap - EXIT
  if [[ "$finished" != true && "$changed" == true ]]; then
    if [[ "$bootout_started" != true ]]; then
      for role in scheduler monitor; do
        if [[ ( "$role" == scheduler && "$prior_scheduler" == true ) || ( "$role" == monitor && "$prior_monitor" == true ) ]]; then
          launchctl disable "$DOMAIN/com.ck.alphaos.$role" || true
        else
          launchctl enable "$DOMAIN/com.ck.alphaos.$role" || true
        fi
      done
      echo "Migration refused before service replacement; previous enabled states restored." >&2
    else
      launchctl disable "$DOMAIN/com.ck.alphaos.scheduler" || true
      # Alert first; enabling alone does not establish a loaded service.
      heartbeat_state=UNAVAILABLE
      monitor_state=UNAVAILABLE
      if is_loaded heartbeat; then heartbeat_state=loaded_unverified; fi
      if is_loaded monitor; then monitor_state=loaded_unverified; fi
      if is_loaded scheduler && ! verified_loaded scheduler; then
        echo "Old or ambiguous research service remains registered; refusing independent bootstraps that could overlap its monitor." >&2
      elif verify_reviewed; then
        if ensure_independent heartbeat; then heartbeat_state=loaded_verified; fi
        if ensure_independent monitor; then monitor_state=loaded_verified; fi
      else
        echo "Reviewed identities changed: refusing new recovery bootstraps." >&2
      fi
      echo "Partial migration: research disabled; heartbeat=$heartbeat_state; monitor=$monitor_state. Loaded does not mean healthy. Originals: ${BACKUP_DIR:-prior activation backup}." >&2
      echo "Inspect logs and readiness; use --resume with the same reviewed identity arguments. No fuse, incident or cadence changed." >&2
    fi
  fi
  exit "$code"
}
trap on_exit EXIT
if [[ "$MODE" == --resume ]]; then
  # Do not interrupt a healthy monitor or manufacture a new pass.
  for role in scheduler monitor heartbeat; do
    cmp -s "$STAGE_DIR/com.ck.alphaos.$role.plist" "$DEST_DIR/com.ck.alphaos.$role.plist" || {
      echo "Resume refused: $role definition differs; use a reviewed idle activation." >&2; exit 1;
    }
  done
  if is_loaded scheduler; then
    echo "Resume refused: research is already loaded; inspect its state. No service changed." >&2; exit 1
  fi
  verify_reviewed
  changed=true
  bootout_started=true
  launchctl disable "$DOMAIN/com.ck.alphaos.scheduler"
  ensure_independent heartbeat
  ensure_independent monitor
  "$PROGRAM" -m alphaos.scheduler.worker --role readiness
else
  # Check before disabling, then repeat to close the launch scheduling race.
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
  changed=true
  for role in scheduler monitor; do launchctl disable "$DOMAIN/com.ck.alphaos.$role"; done
  check_quiet
  "$PROGRAM" -m alphaos.scheduler.worker --role preflight
  verify_reviewed
  bootout_started=true
  for role in scheduler monitor heartbeat; do
    if is_loaded "$role"; then launchctl bootout "$DOMAIN/com.ck.alphaos.$role"; fi
    cp "$STAGE_DIR/com.ck.alphaos.$role.plist" "$DEST_DIR/"
  done
  ensure_independent heartbeat
  ensure_independent monitor
  # A recent legacy pass may make the first dedicated pass not due.
  ready=false
  for attempt in {1..10}; do
    if "$PROGRAM" -m alphaos.scheduler.worker --role readiness; then ready=true; break; fi
    sleep 3
  done
  if [[ "$ready" != true ]]; then
    echo "Dedicated monitor is not ready yet; research remains stopped. Wait for a real scheduled pass, then use --resume." >&2
    exit 1
  fi
fi
# Recheck at admission. The application also gates final order submission.
verify_reviewed
verified_loaded heartbeat
verified_loaded monitor
"$PROGRAM" -m alphaos.scheduler.worker --role readiness
launchctl enable "$DOMAIN/com.ck.alphaos.scheduler"
launchctl bootstrap "$DOMAIN" "$DEST_DIR/com.ck.alphaos.scheduler.plist"
verified_loaded scheduler
finished=true
echo "Installed isolated monitor, read-only monitor heartbeat, and research worker. Originals: ${BACKUP_DIR:-prior activation backup}"
