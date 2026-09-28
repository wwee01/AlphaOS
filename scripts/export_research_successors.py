#!/usr/bin/env python3
"""Export draft research from a saved consistent SQLite copy; SELECT only."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess

from alphaos.research.successor_cohorts import export_cohorts
from alphaos.research.successor_report import render_report
from alphaos.research.successor_analysis import analyze_export


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--captured-at-utc", required=True)
    parser.add_argument("--output-prefix", type=Path, required=True)
    parser.add_argument("--draft-horizons", type=Path,
                        help="Optional JSON {prereg_id: sessions} for explicitly UNREVIEWED draft sensitivity only")
    args = parser.parse_args()
    path = args.snapshot.resolve(strict=True)
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm")):
        raise SystemExit("Use a saved consistent copy without WAL/SHM, not a live database")
    outputs = [Path(str(args.output_prefix) + ext) for ext in (".json", ".md")]
    if any(p.exists() for p in outputs):
        raise SystemExit("Output already exists; use a new unique prefix")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    c = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    c.execute("PRAGMA query_only=ON")
    if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit("Snapshot integrity failed")
    permitted = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
    c.set_authorizer(lambda action, *_: sqlite3.SQLITE_OK if action in permitted else sqlite3.SQLITE_DENY)
    try:
        horizons = json.loads(args.draft_horizons.read_text()) if args.draft_horizons else None
        result = export_cohorts(c, as_of_utc=args.captured_at_utc, snapshot_sha256=digest, draft_horizons=horizons)
    finally:
        c.close()
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise SystemExit("Snapshot changed during read; no output saved")
    result["snapshot_unchanged"] = True
    tree = Path(__file__).resolve().parents[1]
    files = sorted((tree / "alphaos/research").glob("successor*.py")) + [Path(__file__).resolve(),
             tree / "alphaos/learning/outcomes_engine.py", tree / "alphaos/util/market_calendar.py",
             tree / "alphaos/stats/frozen_policy_inference.py"]
    result["source_files_sha256"] = {str(p.relative_to(tree)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    result["source_revision"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tree, text=True).strip()
    result["source_tracked_changes"] = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all"], cwd=tree, text=True).splitlines()
    analyses = {}
    for family in result["families"]:
        key = family["contract"]["predecessor_id"]
        if family["contract"].get("statistic") == "paired_mean":
            analyses[key] = {view: analyze_export(result, family, paired_population=view)
                             for view in ("primary_proposed", "final_proposed")}
        elif family["contract"].get("statistic"):
            analyses[key] = analyze_export(result, family)
    result["analyses"] = analyses
    bodies = (json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", render_report(result))
    for out, body in zip(outputs, bodies):
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(body)
    print(json.dumps({"families": len(result["families"]), "snapshot_unchanged": True,
                      "outputs": [str(p.resolve()) for p in outputs]}))


if __name__ == "__main__":
    main()
