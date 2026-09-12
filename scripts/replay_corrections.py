#!/usr/bin/env python3
"""Preview versioned replay corrections from an explicitly named copied DB.

Offline cached bars only. Dry run opens SQLite mode=ro and never migrates.
--apply appends correction evidence to an already migrated copy, leaving
the original outcomes and all frozen hypothesis verdicts unchanged.
"""

import argparse
from pathlib import Path
import sqlite3

from alphaos.learning.replay_corrections import (
    append_replay_corrections, build_replay_corrections, canonical_json,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="explicit copied ledger path; no production default")
    parser.add_argument("--as-of-date", required=True, help="last eligible outcome bar date, YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--apply", action="store_true", help="append correction evidence only")
    args = parser.parse_args(argv)
    db_uri = Path(args.db).resolve().as_uri() + ("?mode=rw" if args.apply else "?mode=ro")
    conn = sqlite3.connect(db_uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        report = build_replay_corrections(conn, as_of_date=args.as_of_date, limit=args.limit)
        report["inserted"] = append_replay_corrections(conn, report) if args.apply else 0
        report["apply"] = args.apply
        print(canonical_json(report))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
