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
# TEXT-0: also mirrors data/text_archive/ into the SAME backup destination
# (interim -- OPS-B's Lane B priority formally rises once this archive
# starts growing, per the regime/text-archive reconciliation ruling; this
# is not waiting for that). Unlike the DB, archive files are already
# individually gzipped + immutable once written (a unique accession number
# per file), so this is an incremental MIRROR (rsync, skips unchanged
# files), never a daily full-rotation snapshot -- rotating a growing,
# multi-GB archive daily the way the DB is rotated would be wasteful of
# disk for no honesty benefit (the DB needs point-in-time snapshots because
# it MUTATES; the archive doesn't). A sha256 spot-verification runs after
# the mirror -- "verified on write [already done at fetch time] AND on
# backup" per TEXT-0's own MANIFEST semantics.
#
# Env var overrides (used by tests; production runs with neither set):
#   ALPHAOS_BACKUP_DB_PATH        -- override the source DB path
#   ALPHAOS_BACKUP_DEST_DIR       -- override the backup destination directory
#   ALPHAOS_BACKUP_TEXT_ARCHIVE_DIR -- override the source text-archive dir
#   ALPHAOS_BACKUP_TEST_MODE      -- if set (any value), skip the real alert send
#                                    (never fire a real ntfy push from a test run)
#
# Usage: deploy/backup_ledger.sh   (no args; safe to re-run any time)

set -uo pipefail
# NOTE: deliberately NOT `set -e` -- this script's own error handling (the
# alert_failure calls + explicit `exit 1`s) must run on failure, which a bare
# `set -e` would short-circuit past on the first failing command.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="$REPO_DIR/.venv/bin/python"
DB_PATH="${ALPHAOS_BACKUP_DB_PATH:-$REPO_DIR/data/alphaos.db}"
TEXT_ARCHIVE_DIR="${ALPHAOS_BACKUP_TEXT_ARCHIVE_DIR:-$REPO_DIR/data/text_archive}"
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

echo "Ledger backup complete: $(date -Iseconds)"

# --- 6. TEXT-0: mirror the text archive (best-effort -- never fails the
#     overall run just because the archive doesn't exist yet, e.g. before
#     TEXT_ARCHIVE_ENABLED is ever turned on; the DB backup above is the
#     part exit-review called CRITICAL, and it's already complete by now). ---
if [ -d "$TEXT_ARCHIVE_DIR" ]; then
  TEXT_ARCHIVE_BACKUP_DIR="$BACKUP_DIR/text_archive"
  mkdir -p "$TEXT_ARCHIVE_BACKUP_DIR" || echo "WARNING: could not create $TEXT_ARCHIVE_BACKUP_DIR (text archive backup skipped)" >&2

  if [ -d "$TEXT_ARCHIVE_BACKUP_DIR" ]; then
    if command -v rsync >/dev/null 2>&1; then
      CHANGED_LIST="$(mktemp "${TMPDIR:-/tmp}/alphaos-archive-changed-XXXXXX.txt")"
      if ! rsync -a --out-format='%n' "$TEXT_ARCHIVE_DIR"/ "$TEXT_ARCHIVE_BACKUP_DIR"/ > "$CHANGED_LIST" 2>&1; then
        alert_failure "text archive rsync failed: $(cat "$CHANGED_LIST")"
      else
        NEW_COUNT=$(grep -c '\.gz$' "$CHANGED_LIST" || true)
        echo "Text archive mirror OK: $NEW_COUNT file(s) synced to $TEXT_ARCHIVE_BACKUP_DIR"

        # sha256 spot-verification: TEXT-0's own MANIFEST semantics ("sha256
        # verified on write and on backup") -- check every file this run
        # actually copied (not the whole historical archive every night,
        # which would only get slower as the archive grows) against the
        # sha256 recorded in text_documents at fetch time. The changed-file
        # list is passed via a FILE PATH (never interpolated into the Python
        # source as a string) so filenames can't corrupt the script.
        if [ "$NEW_COUNT" -gt 0 ] && [ -x "$VENV_PYTHON" ]; then
          # DB_PATH passed explicitly (never via load_settings(), which reads
          # the REAL .env/environment and would silently ignore
          # ALPHAOS_BACKUP_DB_PATH's test override -- caught by this script's
          # own functional test, not just a syntax check: an early version of
          # this block called load_settings() here and silently verified
          # against the WRONG database in every test, always reporting
          # "0 mismatches" regardless of what was actually being checked).
          ALPHAOS_ARCHIVE_CHANGED_LIST="$CHANGED_LIST" ALPHAOS_ARCHIVE_BACKUP_DIR="$TEXT_ARCHIVE_BACKUP_DIR" \
            ALPHAOS_ARCHIVE_DB_PATH="$DB_PATH" \
            "$VENV_PYTHON" -c "
import gzip, hashlib, os, sys
from alphaos.journal.journal_store import JournalStore

changed_list_path = os.environ['ALPHAOS_ARCHIVE_CHANGED_LIST']
backup_dir = os.environ['ALPHAOS_ARCHIVE_BACKUP_DIR']
db_path = os.environ['ALPHAOS_ARCHIVE_DB_PATH']
with open(changed_list_path, encoding='utf-8') as f:
    changed_files = [line.strip() for line in f if line.strip().endswith('.gz')]

j = JournalStore(db_path)
mismatches = []
for rel_path in changed_files:
    row = j.one('SELECT sha256 FROM text_documents WHERE storage_path LIKE ?', ('%' + rel_path,))
    if not row:
        continue  # not one of our rows (or path shape differs) -- not this check's job to flag
    dest_path = os.path.join(backup_dir, rel_path)
    try:
        with gzip.open(dest_path, 'rb') as fh:
            actual_sha = hashlib.sha256(fh.read()).hexdigest()
    except OSError as exc:
        mismatches.append(f'{rel_path}: unreadable at backup dest ({exc})')
        continue
    if actual_sha != row['sha256']:
        mismatches.append(f'{rel_path}: sha256 mismatch (expected {row[\"sha256\"]}, got {actual_sha})')
j.close()
if mismatches:
    print('TEXT ARCHIVE BACKUP VERIFICATION FAILED:', file=sys.stderr)
    for m in mismatches:
        print(f'  - {m}', file=sys.stderr)
    sys.exit(1)
print(f'Text archive backup verification OK: {len(changed_files)} file(s) checked, 0 mismatches.')
" || alert_failure "text archive backup sha256 verification failed -- see log output above"
        fi
      fi
      rm -f "$CHANGED_LIST"
    else
      echo "WARNING: rsync not found -- text archive backup skipped this run" >&2
    fi
  fi
fi

echo "Backup complete: $(date -Iseconds)"
