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

STATUS_FILE_DEFAULT = "data/backup_status.json"


def build_backup_health(status_file: Optional[str] = None) -> Optional[dict]:
    """Returns the last-known backup status, or ``None`` if the backup job
    has never run yet (a fresh checkout, or the LaunchAgent hasn't fired
    yet) -- an expected, honest empty state, never an error."""
    path = status_file or STATUS_FILE_DEFAULT
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        # A torn/corrupt status file must never crash the daily brief --
        # treat it the same as "never run" rather than raising, since this
        # file's own job is to inform, never to gate anything.
        return None


def render_markdown(health: Optional[dict]) -> str:
    if health is None:
        return "## Backups\n- No backup run recorded yet."
    lines = ["## Backups"]
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
