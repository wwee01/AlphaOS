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
#   ALPHAOS_BACKUP_ENV_PATH       -- override the source .env path (OPS-B)
#   ALPHAOS_BACKUP_ENC_PASSPHRASE_OVERRIDE -- test-only: use this passphrase
#                                    directly instead of querying Keychain
#                                    (production NEVER sets this)
#
# OPS-B (extends PR9.5): nightly, ALSO encrypts .env -> env.enc next to the DB
# backup (same dated folder/rotation -- the DB backup and the config that ran
# it can never drift apart), then immediately decrypts it back and
# sha256-compares against the real .env (a round-trip self-check: "backup
# exists" vs "backup RESTORES" are different claims, and the cheap way to
# find that out is tonight, not at disaster time). Monthly, ALSO copies
# {db.gz, env.enc, MANIFEST.json} to a second target OUTSIDE Apple's account
# domain (operator-configured via BACKUP2_METHOD=rclone|disk + BACKUP2_DEST
# in .env -- read via a small Python shellout, same pattern as
# alert_failure's own load_settings() call, never by sourcing .env directly
# in bash). The encryption passphrase lives in the macOS login Keychain
# (local-only, never iCloud-synced, retrievable unattended while the
# session is unlocked -- the same availability envelope every other
# LaunchAgent here already assumes) -- NEVER in .env, NEVER in the repo, per
# spec. This is the RUNTIME copy only: the operator's password manager holds
# the RECOVERY copy (a Keychain-only passphrase dies with the Mac, exactly
# when a disaster restore would need it). Arming is a one-time operator
# action (see the ARM line printed below when unarmed) -- shipped unarmed by
# default; env.enc is simply skipped (loud warning, never a hard failure)
# until armed, same "ship mechanism, arm later" pattern as every other
# opt-in mechanism in this codebase.
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

# OPS-B: env.enc encryption
ENV_SRC="${ALPHAOS_BACKUP_ENV_PATH:-$REPO_DIR/.env}"
BACKUP_KEYCHAIN_SERVICE="AlphaOS-backup-passphrase"
BACKUP_KEYCHAIN_ACCOUNT="alphaos-backup"
OFFSITE_DIR="$BACKUP_DIR/offsite"
STATUS_FILE="$REPO_DIR/data/backup_status.json"

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

# mktemp template MUST end in the XXXXXX: BSD/macOS mktemp only substitutes
# TRAILING Xs -- with a suffix after them it returns the LITERAL template
# path every run. One abnormally-killed run (EXIT trap doesn't fire on
# SIGKILL) then strands that literal file, and every later run dies at
# "mkstemp failed: File exists" -- which is exactly how nightly backups
# silently failed Jul 12-16 2026. sqlite3 .backup does not care about a .db
# extension. [ -n ] guard: if mktemp still fails, die HERE, loudly -- an
# empty $TMP_BACKUP would otherwise sail through sqlite3 .backup '' AND
# "PRAGMA integrity_check" (both succeed on an empty path) and only trip at
# gzip with a confusing "can't stat" error.
TMP_BACKUP="$(mktemp "${TMPDIR:-/tmp}/alphaos-backup-XXXXXX")"
[ -n "$TMP_BACKUP" ] || fail "mktemp could not create the staging file for the DB backup"
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
      # Trailing-Xs rule (see TMP_BACKUP's comment): no suffix after XXXXXX.
      CHANGED_LIST="$(mktemp "${TMPDIR:-/tmp}/alphaos-archive-changed-XXXXXX")"
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

# --- 7. OPS-B: encrypt .env -> env.enc alongside today's DB backup, then
#     immediately round-trip-decrypt it and sha256-compare against the real
#     .env -- "backup exists" and "backup RESTORES" are different claims;
#     this finds the difference tonight, not at disaster time. Best-effort:
#     an unarmed Keychain (no passphrase stored yet) is a loud WARNING, never
#     a hard failure -- the DB backup above is already the CRITICAL part and
#     it's done. ---
ENV_ENC_DEST="$DAILY_DIR/env-$TODAY.enc"
ENV_ENC_OK=0
if [ -f "$ENV_SRC" ]; then
  if [ -n "${ALPHAOS_BACKUP_ENC_PASSPHRASE_OVERRIDE:-}" ]; then
    BACKUP_PASSPHRASE="$ALPHAOS_BACKUP_ENC_PASSPHRASE_OVERRIDE"
  else
    BACKUP_PASSPHRASE="$(security find-generic-password -s "$BACKUP_KEYCHAIN_SERVICE" -a "$BACKUP_KEYCHAIN_ACCOUNT" -w 2>/dev/null || true)"
  fi

  if [ -z "$BACKUP_PASSPHRASE" ]; then
    echo "WARNING: env.enc NOT ARMED -- no passphrase in Keychain. To arm: " >&2
    echo "  security add-generic-password -s $BACKUP_KEYCHAIN_SERVICE -a $BACKUP_KEYCHAIN_ACCOUNT -w '<passphrase>' -T /usr/bin/security -T /usr/bin/openssl" >&2
    echo "  (then ALSO store the same passphrase in your password manager -- the Keychain copy dies with this Mac, exactly when a disaster restore would need it)" >&2
  else
    ENV_ENC_ERR="$(mktemp "${TMPDIR:-/tmp}/alphaos-env-enc-err-XXXXXX")"
    if openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -pass fd:3 -in "$ENV_SRC" -out "$ENV_ENC_DEST.tmp" 3<<< "$BACKUP_PASSPHRASE" 2>"$ENV_ENC_ERR"; then
      DECRYPTED_CHECK="$(mktemp "${TMPDIR:-/tmp}/alphaos-env-check-XXXXXX")"
      if openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -pass fd:3 -in "$ENV_ENC_DEST.tmp" -out "$DECRYPTED_CHECK" 3<<< "$BACKUP_PASSPHRASE" 2>/dev/null \
         && [ "$(shasum -a 256 "$DECRYPTED_CHECK" | cut -d' ' -f1)" = "$(shasum -a 256 "$ENV_SRC" | cut -d' ' -f1)" ]; then
        mv "$ENV_ENC_DEST.tmp" "$ENV_ENC_DEST"
        ENV_ENC_OK=1
        echo "env.enc OK: $ENV_ENC_DEST (round-trip verified)"
      else
        rm -f "$ENV_ENC_DEST.tmp"
        alert_failure "env.enc round-trip verification failed -- the encrypted backup would NOT restore correctly; NOT written"
      fi
      rm -f "$DECRYPTED_CHECK"
    else
      # audit LOW (2026-07-10): surface WHY openssl failed -- an operator
      # previously got no diagnostic beyond "it failed" (the captured
      # stderr was written to a temp file and then discarded unread).
      alert_failure "openssl encryption of .env failed: $(cat "$ENV_ENC_ERR" 2>/dev/null)"
      rm -f "$ENV_ENC_DEST.tmp"
    fi
    rm -f "$ENV_ENC_ERR"
  fi
else
  echo "WARNING: no .env found at $ENV_SRC -- env.enc skipped" >&2
fi
# NOTE: BACKUP_PASSPHRASE (if it was set at all) deliberately stays in scope
# past this point -- the offsite leg (step 9 below) reuses the SAME
# passphrase to encrypt the DB copy before it ever leaves this Mac. It is
# unset exactly once, at the very end of the script (step 10), after both
# uses are done.

# --- 8. MANIFEST.json: sha256 of every artifact this run produced + schema
#     version + git rev + the EXACT decrypt command (flags included,
#     passphrase excluded) -- a disaster-time operator should never need to
#     remember `-iter 600000`, only their own passphrase. ---
GIT_REV="$(cd "$REPO_DIR" && git rev-parse HEAD 2>/dev/null || echo unknown)"
SCHEMA_VERSION="$("$VENV_PYTHON" -c "from alphaos.journal.schema import SCHEMA_VERSION; print(SCHEMA_VERSION)" 2>/dev/null || echo unknown)"
DB_SHA="$(shasum -a 256 "$DAILY_DEST" | cut -d' ' -f1)"
MANIFEST_DEST="$DAILY_DIR/MANIFEST-$TODAY.json"
{
  echo "{"
  echo "  \"date\": \"$TODAY\","
  echo "  \"git_rev\": \"$GIT_REV\","
  echo "  \"schema_version\": \"$SCHEMA_VERSION\","
  echo "  \"db_gz\": {\"file\": \"$(basename "$DAILY_DEST")\", \"sha256\": \"$DB_SHA\"},"
  if [ "$ENV_ENC_OK" -eq 1 ]; then
    ENV_ENC_SHA="$(shasum -a 256 "$ENV_ENC_DEST" | cut -d' ' -f1)"
    echo "  \"env_enc\": {\"file\": \"$(basename "$ENV_ENC_DEST")\", \"sha256\": \"$ENV_ENC_SHA\"},"
  else
    echo "  \"env_enc\": null,"
  fi
  echo "  \"decrypt_command\": \"openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -pass fd:3 -in env-$TODAY.enc -out .env.restored 3<<< YOUR_PASSPHRASE_HERE\""
  echo "}"
} > "$MANIFEST_DEST.tmp" && mv "$MANIFEST_DEST.tmp" "$MANIFEST_DEST"
echo "MANIFEST OK: $MANIFEST_DEST"

# --- 9. OPS-B: monthly, copy {db.gz(encrypted), env.enc, offsite MANIFEST}
#     to a second target OUTSIDE Apple's account domain -- breaks the
#     single-ecosystem failure domain (iCloud only) PR9.5 shipped with.
#     Same monthly-slot-backfill pattern as step 4 (fill in if this month
#     doesn't have one yet, robust to a missed exact "1st" tick -- the next
#     successful nightly backfills it). Operator-configured
#     (BACKUP2_METHOD=rclone|disk, BACKUP2_DEST=... in .env) -- read via a
#     small Python shellout (the one place that already knows how to parse
#     .env correctly), never by sourcing .env directly in bash. Mechanism
#     only: an unconfigured/absent second target is a WARNING (visible, not
#     silent), never a hard failure -- arming is an explicit, deliberate
#     operator action (their own cloud credentials or external disk), same
#     as env.enc above.
#
#     Per spec: "Encrypt the DB at the second target too (cheap); local
#     plain .backup stays for fast restore" -- the LOCAL daily/monthly
#     copies stay plaintext gzip (fast restore, already covered by iCloud's
#     own access controls), but nothing may leave this Mac for a
#     third-party cloud or a losable external disk unencrypted. This
#     REUSES the exact same Keychain passphrase as env.enc (encrypted
#     into a throwaway temp file, never persisted locally, cleaned up
#     after the copy) -- so the offsite leg is gated on the SAME arming
#     step as env.enc: no passphrase, no offsite DB copy, full stop. ---
BACKUP2_CONFIG_ERR="$(mktemp "${TMPDIR:-/tmp}/alphaos-backup2-config-err-XXXXXX")"
BACKUP2_CONFIG="$("$VENV_PYTHON" -c "
from alphaos.config.settings import load_settings
s = load_settings()
print(s.backup2_method)
print(s.backup2_dest)
" 2>"$BACKUP2_CONFIG_ERR")"
BACKUP2_SHELLOUT_STATUS=$?
BACKUP2_CONFIG_ERR_TEXT="$(cat "$BACKUP2_CONFIG_ERR" 2>/dev/null)"
rm -f "$BACKUP2_CONFIG_ERR"
BACKUP2_METHOD="$(echo "$BACKUP2_CONFIG" | sed -n '1p')"
BACKUP2_DEST="$(echo "$BACKUP2_CONFIG" | sed -n '2p')"
OFFSITE_MONTHLY_MARKER="$OFFSITE_DIR/.last-offsite-$THIS_MONTH"

if [ "$BACKUP2_SHELLOUT_STATUS" -ne 0 ]; then
  # audit MEDIUM (2026-07-10): distinct from "genuinely not configured" --
  # load_settings() itself raised (an invalid BACKUP2_METHOD, or ANY other
  # malformed .env value elsewhere), so BACKUP2_METHOD/DEST read as empty
  # for the wrong reason. Silently treating this the same as "unconfigured"
  # would let a working offsite setup go dark for a month after an
  # unrelated .env typo, with only a stderr warning to notice by.
  alert_failure "offsite backup config could not be read (.env / settings error): $BACKUP2_CONFIG_ERR_TEXT"
elif [ -z "$BACKUP2_METHOD" ]; then
  echo "WARNING: offsite backup NOT ARMED -- BACKUP2_METHOD unset in .env (rclone|disk). Single-ecosystem failure domain remains." >&2
elif [ -f "$OFFSITE_MONTHLY_MARKER" ]; then
  echo "Offsite backup already done this month ($THIS_MONTH) -- skipping."
elif [ -z "${BACKUP_PASSPHRASE:-}" ]; then
  echo "WARNING: offsite backup configured but SKIPPED -- no Keychain passphrase armed, and an unencrypted DB must never leave this Mac. Arm the same passphrase env.enc uses (see the ARM instructions above) to enable both." >&2
  alert_failure "offsite backup skipped: BACKUP2_METHOD is configured but no encryption passphrase is armed -- refusing to ship an unencrypted DB off-ecosystem"
else
  mkdir -p "$OFFSITE_DIR"
  # Trailing-Xs rule (see TMP_BACKUP's comment): no suffix after XXXXXX.
  DB_ENC_TMP="$(mktemp "${TMPDIR:-/tmp}/alphaos-db-offsite-XXXXXX")"
  OFFSITE_MANIFEST_TMP="$(mktemp "${TMPDIR:-/tmp}/alphaos-offsite-manifest-XXXXXX")"

  if ! openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -pass fd:3 -in "$DAILY_DEST" -out "$DB_ENC_TMP" 3<<< "$BACKUP_PASSPHRASE" 2>/dev/null; then
    alert_failure "offsite backup skipped: openssl encryption of the DB copy failed"
    rm -f "$DB_ENC_TMP" "$OFFSITE_MANIFEST_TMP"
  else
    DB_ENC_SHA="$(shasum -a 256 "$DB_ENC_TMP" | cut -d' ' -f1)"
    OFFSITE_DB_NAME="alphaos-$TODAY.db.gz.enc"
    {
      echo "{"
      echo "  \"date\": \"$TODAY\","
      echo "  \"git_rev\": \"$GIT_REV\","
      echo "  \"schema_version\": \"$SCHEMA_VERSION\","
      echo "  \"db_gz_enc\": {\"file\": \"$OFFSITE_DB_NAME\", \"sha256\": \"$DB_ENC_SHA\"},"
      if [ "$ENV_ENC_OK" -eq 1 ]; then
        echo "  \"env_enc\": {\"file\": \"$(basename "$ENV_ENC_DEST")\", \"sha256\": \"$(shasum -a 256 "$ENV_ENC_DEST" | cut -d' ' -f1)\"},"
      else
        echo "  \"env_enc\": null,"
      fi
      echo "  \"note\": \"both artifacts here are encrypted with the SAME passphrase (this Mac's Keychain + your password manager's recovery copy)\","
      echo "  \"decrypt_db_command\": \"openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -pass fd:3 -in $OFFSITE_DB_NAME -out alphaos-restored.db.gz 3<<< YOUR_PASSPHRASE_HERE\","
      echo "  \"decrypt_env_command\": \"openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -pass fd:3 -in env-$TODAY.enc -out .env.restored 3<<< YOUR_PASSPHRASE_HERE\""
      echo "}"
    } > "$OFFSITE_MANIFEST_TMP"

    OFFSITE_ARTIFACTS=("$DB_ENC_TMP")
    if [ "$ENV_ENC_OK" -eq 1 ]; then
      OFFSITE_ARTIFACTS+=("$ENV_ENC_DEST")
    fi

    OFFSITE_OK=1
    if [ "$BACKUP2_METHOD" = "disk" ]; then
      if [ -z "$BACKUP2_DEST" ] || [ ! -d "$BACKUP2_DEST" ]; then
        alert_failure "offsite backup configured as 'disk' but BACKUP2_DEST ($BACKUP2_DEST) is not a mounted/existing directory"
        OFFSITE_OK=0
      else
        cp "$DB_ENC_TMP" "$BACKUP2_DEST/$OFFSITE_DB_NAME" || { alert_failure "offsite disk copy failed for the encrypted DB"; OFFSITE_OK=0; }
        cp "$OFFSITE_MANIFEST_TMP" "$BACKUP2_DEST/MANIFEST-$TODAY.json" || { alert_failure "offsite disk copy failed for the offsite manifest"; OFFSITE_OK=0; }
        for artifact in "${OFFSITE_ARTIFACTS[@]:1}"; do  # remaining artifacts (env.enc, if armed) keep their own names
          cp "$artifact" "$BACKUP2_DEST/" || { alert_failure "offsite disk copy failed for $artifact"; OFFSITE_OK=0; }
        done
      fi
    elif [ "$BACKUP2_METHOD" = "rclone" ]; then
      if ! command -v rclone >/dev/null 2>&1; then
        alert_failure "offsite backup configured as 'rclone' but the rclone binary is not installed"
        OFFSITE_OK=0
      elif [ -z "$BACKUP2_DEST" ]; then
        alert_failure "offsite backup configured as 'rclone' but BACKUP2_DEST is empty"
        OFFSITE_OK=0
      else
        rclone copyto "$DB_ENC_TMP" "$BACKUP2_DEST/$OFFSITE_DB_NAME" || { alert_failure "rclone copy failed for the encrypted DB"; OFFSITE_OK=0; }
        rclone copyto "$OFFSITE_MANIFEST_TMP" "$BACKUP2_DEST/MANIFEST-$TODAY.json" || { alert_failure "rclone copy failed for the offsite manifest"; OFFSITE_OK=0; }
        for artifact in "${OFFSITE_ARTIFACTS[@]:1}"; do
          rclone copyto "$artifact" "$BACKUP2_DEST/$(basename "$artifact")" || { alert_failure "rclone copy failed for $artifact"; OFFSITE_OK=0; }
        done
      fi
    else
      echo "WARNING: unrecognized BACKUP2_METHOD=$BACKUP2_METHOD (expected rclone|disk) -- offsite backup skipped" >&2
      OFFSITE_OK=0
    fi

    rm -f "$DB_ENC_TMP" "$OFFSITE_MANIFEST_TMP"
    if [ "$OFFSITE_OK" -eq 1 ]; then
      touch "$OFFSITE_MONTHLY_MARKER"
      echo "Offsite backup OK (DB encrypted): $THIS_MONTH -> $BACKUP2_METHOD:$BACKUP2_DEST"
    fi
  fi
fi

unset BACKUP_PASSPHRASE

# --- 10. Status file (repo-relative, NOT the iCloud-mirrored BACKUP_DIR):
#     a small, non-sensitive summary the Python report layer reads for the
#     daily brief/digest -- never queries Keychain or the real backup
#     destination directly; the bash script that already knows both is the
#     one place that writes this. ---
mkdir -p "$(dirname "$STATUS_FILE")"
LAST_OFFSITE_MONTH="none"
if [ -n "$(find "$OFFSITE_DIR" -maxdepth 1 -name '.last-offsite-*' 2>/dev/null | sort | tail -n1)" ]; then
  LAST_OFFSITE_MONTH="$(basename "$(find "$OFFSITE_DIR" -maxdepth 1 -name '.last-offsite-*' | sort | tail -n1)" | sed 's/.last-offsite-//')"
fi
{
  echo "{"
  echo "  \"nightly_backup_ok_at_utc\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\","
  echo "  \"nightly_backup_date\": \"$TODAY\","
  echo "  \"env_enc_armed\": $( [ "$ENV_ENC_OK" -eq 1 ] && echo true || echo false ),"
  echo "  \"offsite_configured\": $( [ -n "$BACKUP2_METHOD" ] && echo true || echo false ),"
  echo "  \"offsite_last_ok_month\": \"$LAST_OFFSITE_MONTH\""
  echo "}"
} > "$STATUS_FILE.tmp" && mv "$STATUS_FILE.tmp" "$STATUS_FILE"

echo "Backup complete: $(date -Iseconds)"
