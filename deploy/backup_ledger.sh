#!/usr/bin/env bash
# PR9.5: online, WAL-safe backup of the AlphaOS ledger (the fund's source of
# truth AND its future ML training data, currently on ONE disk with ZERO
# redundancy -- exit-review CRITICAL). Runs daily via its own LaunchAgent
# (com.ck.alphaos.backup.plist, 05:30 SGT -- after market close, before the
# first scan window).
#
# Steps: sqlite3's online `.backup` API (NEVER `cp` -- a plain file copy can
# grab a WAL-mode DB mid-write and produce a torn/inconsistent copy) -> a
# PRAGMA integrity_check gate (never keep/rotate-in a backup that doesn't
# pass) -> gzip -> copy into a daily-rotation directory (keep the newest 30)
# and, once per calendar month, into a monthly-rotation directory (keep the
# newest 12) -> any failure sends a high-priority alert.
#
# Env var overrides (used by tests; production runs with neither set):
#   ALPHAOS_BACKUP_DB_PATH    -- override the source DB path
#   ALPHAOS_BACKUP_DEST_DIR   -- override the backup destination directory
#   ALPHAOS_BACKUP_TEST_MODE  -- if set (any value), skip the real alert send
#                                (never fire a real ntfy push from a test run)
#
# Usage: deploy/backup_ledger.sh   (no args; safe to re-run any time)

set -uo pipefail
# NOTE: deliberately NOT `set -e` -- this script's own error handling (the
# alert_failure calls + explicit `exit 1`s) must run on failure, which a bare
# `set -e` would short-circuit past on the first failing command.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="$REPO_DIR/.venv/bin/python"
DB_PATH="${ALPHAOS_BACKUP_DB_PATH:-$REPO_DIR/data/alphaos.db}"
BACKUP_DIR="${ALPHAOS_BACKUP_DEST_DIR:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/AlphaOS-backups}"
DAILY_DIR="$BACKUP_DIR/daily"
MONTHLY_DIR="$BACKUP_DIR/monthly"
KEEP_DAILY=30
KEEP_MONTHLY=12
TODAY="$(date +%F)"          # YYYY-MM-DD
THIS_MONTH="$(date +%Y-%m)"  # YYYY-MM

alert_failure() {
  local message="$1"
  echo "BACKUP FAILURE: $message" >&2
  if [ -n "${ALPHAOS_BACKUP_TEST_MODE:-}" ]; then
    echo "[test mode -- real alert send skipped]"
    return 0
  fi
  if [ -x "$VENV_PYTHON" ]; then
    (cd "$REPO_DIR" && "$VENV_PYTHON" -c "
from alphaos.config.settings import load_settings
from alphaos.util import alerts
s = load_settings()
alerts.send_alert(s, 'AlphaOS backup FAILED', '''$message''', priority='high')
" 2>&1) || true
  fi
}

fail() {
  alert_failure "$1"
  exit 1
}

if [ ! -f "$DB_PATH" ]; then
  fail "source DB not found at $DB_PATH"
fi

mkdir -p "$DAILY_DIR" "$MONTHLY_DIR" || fail "could not create backup directories under $BACKUP_DIR"

TMP_BACKUP="$(mktemp "${TMPDIR:-/tmp}/alphaos-backup-XXXXXX.db")"
trap 'rm -f "$TMP_BACKUP"' EXIT

# --- 1. Online backup (WAL-safe -- consistent even if the scheduler is mid-write) ---
if ! sqlite3 "$DB_PATH" ".backup '$TMP_BACKUP'"; then
  fail "sqlite3 .backup command failed for $DB_PATH"
fi

# --- 2. Integrity gate: never rotate in a backup that doesn't pass ---
INTEGRITY_RESULT="$(sqlite3 "$TMP_BACKUP" "PRAGMA integrity_check;" 2>&1)"
if [ "$INTEGRITY_RESULT" != "ok" ]; then
  fail "PRAGMA integrity_check failed on the fresh backup: $INTEGRITY_RESULT"
fi

# --- 3. Compress + place into today's daily slot ---
DAILY_DEST="$DAILY_DIR/alphaos-$TODAY.db.gz"
if ! gzip -c "$TMP_BACKUP" > "$DAILY_DEST.tmp"; then
  rm -f "$DAILY_DEST.tmp"
  fail "gzip failed while writing $DAILY_DEST"
fi
mv "$DAILY_DEST.tmp" "$DAILY_DEST" || fail "could not finalize $DAILY_DEST"
echo "Daily backup OK: $DAILY_DEST ($(du -h "$DAILY_DEST" | cut -f1))"

# --- 4. Monthly slot: fill it in if this calendar month doesn't have one yet
#     (robust to a missed 05:30 fire on the 1st -- the next successful daily
#     run backfills it, rather than requiring the exact first-of-month tick). ---
MONTHLY_DEST="$MONTHLY_DIR/alphaos-$THIS_MONTH.db.gz"
if [ ! -f "$MONTHLY_DEST" ]; then
  cp "$DAILY_DEST" "$MONTHLY_DEST" || fail "could not create monthly snapshot $MONTHLY_DEST"
  echo "Monthly backup OK: $MONTHLY_DEST"
fi

# --- 5. Rotation: keep the newest N by filename (dates sort lexically = chronologically) ---
rotate() {
  local dir="$1" keep="$2"
  local total
  total=$(find "$dir" -maxdepth 1 -name "alphaos-*.db.gz" -type f | wc -l | tr -d ' ')
  if [ "$total" -gt "$keep" ]; then
    find "$dir" -maxdepth 1 -name "alphaos-*.db.gz" -type f | sort | head -n "$((total - keep))" | while IFS= read -r old; do
      rm -f "$old" && echo "Rotated out: $old"
    done
  fi
}
rotate "$DAILY_DIR" "$KEEP_DAILY"
rotate "$MONTHLY_DIR" "$KEEP_MONTHLY"

echo "Backup complete: $(date -Iseconds)"
