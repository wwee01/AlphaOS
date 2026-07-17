"""OPS-B: the nightly/offsite backup status -- pure read of a small JSON
status file `deploy/backup_ledger.sh` writes at the end of every run
(`data/backup_status.json`, repo-relative, gitignored -- NOT the real
iCloud-mirrored backup destination, which this module never touches
directly). Filesystem-based rather than DB-based: the backup itself is
entirely outside the SQLite ledger by design (a torn/corrupt DB is exactly
the failure mode backups exist to survive). Zero decision surface.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from alphaos.util import timeutils

STATUS_FILE_DEFAULT = "data/backup_status.json"

# The status file is written ONLY on success, so its age is the one signal
# that survives every backup failure mode (crash, wedge, launch refusal,
# alert-path breakage alike). > this many days without a success is stale.
# 2 (not 1): the LaunchAgent fires 05:30 SGT daily, so a brief built minutes
# before that is legitimately ~1 day behind; 2 days means a whole scheduled
# run was missed.
STALE_AFTER_DAYS = 2


def _days_since(date_str: Optional[str], now=None) -> Optional[int]:
    """Whole days between a YYYY-MM-DD date and now (UTC). None if unparseable."""
    if not date_str:
        return None
    try:
        from datetime import date

        y, m, d = (int(p) for p in str(date_str)[:10].split("-"))
        then = date(y, m, d)
        today = (now or timeutils.now_utc()).date()
        return (today - then).days
    except (ValueError, AttributeError):
        return None


def build_backup_health(status_file: Optional[str] = None, now=None) -> Optional[dict]:
    """Returns the last-known backup status, or ``None`` if the backup job
    has never run yet (a fresh checkout, or the LaunchAgent hasn't fired
    yet) -- an expected, honest empty state, never an error.

    2026-07-17 audit (nightly backups failed silently Jul 12-16): the file is
    success-only, so "the file says OK" only ever means "the LAST SUCCESS said
    OK" -- it says nothing about every night since. This function now stamps
    ``stale`` / ``days_since_success`` so every renderer downstream shows a
    5-day-old "OK" as the outage it actually is."""
    path = status_file or STATUS_FILE_DEFAULT
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            parsed = json.load(f)
        # audit LOW (2026-07-10): syntactically valid JSON that isn't an
        # object (e.g. a bare list/number/string) would otherwise flow
        # through as "truthy, not a dict" and crash render_markdown's own
        # .get() calls -- only ever written by this script as an object,
        # but a read function must not trust that as a guarantee.
        if not isinstance(parsed, dict):
            return None
    except (OSError, json.JSONDecodeError):
        # A torn/corrupt status file must never crash the daily brief --
        # treat it the same as "never run" rather than raising, since this
        # file's own job is to inform, never to gate anything.
        return None
    days = _days_since(parsed.get("nightly_backup_date"), now=now)
    parsed["days_since_success"] = days
    # Unparseable date on an existing success file reads as stale, not fresh:
    # unknown-never-green, same direction as the rest of this codebase.
    parsed["stale"] = days is None or days > STALE_AFTER_DAYS
    return parsed


def render_markdown(health: Optional[dict]) -> str:
    if health is None:
        return "## Backups\n- No backup run recorded yet."
    lines = ["## Backups"]
    if health.get("stale"):
        days = health.get("days_since_success")
        ago = f"{days} day(s) ago" if days is not None else "at an unparseable date"
        lines.append(
            f"- 🚨 STALE: last successful nightly backup was {ago} "
            f"({health.get('nightly_backup_date', '?')}) -- every night since has "
            "FAILED or not run. Check ~/Library/Logs/alphaos/backup-error.log."
        )
    else:
        lines.append(
            f"- Nightly: OK {health.get('nightly_backup_ok_at_utc', '?')} "
            f"({health.get('nightly_backup_date', '?')})"
        )
    if health.get("env_enc_armed"):
        lines.append("- env.enc: armed (round-trip verified this run)")
    else:
        lines.append(
            "- ⚠️ env.enc: NOT ARMED -- `.env` backup is skipped; "
            "see deploy/backup_ledger.sh's header comment to arm it"
        )
    if health.get("offsite_configured"):
        last = health.get("offsite_last_ok_month") or "never"
        lines.append(f"- Offsite: configured, last OK {last}")
    else:
        lines.append("- ⚠️ Offsite: NOT CONFIGURED -- single-ecosystem (iCloud-only) failure domain remains")
    return "\n".join(lines)
