#!/usr/bin/env python3
"""EXP-1 mechanism 3: the MANDATED build-time saturation audit of EXP-0's
own accumulated instr1 shadow captures.

WHY THIS SCRIPT EXISTS INSTEAD OF ALREADY-COMPUTED LITERALS
-------------------------------------------------------------------------
The spec requires ``interest_score_shadow_v1``'s recalibrated constants
(change-scale, rel-vol-scale, day-range floor, momentum caps) to come from
a real audit of the operator's own accumulated shadow-tier data ("the data
answers 0.12-vs-0.15, this spec doesn't guess") -- never a guess, never
env-tunable, never retro-scored.

This build session could NOT run that audit: computing it requires reading
the live production ``data/alphaos.db`` (~1132 shadow-tier rows across ~4
trading days since 2026-07-10, per the operator's own decision-log entry),
and this session's sandbox permission classifier declined that read as a
"production database" access outside this build's remit (the build brief
separately says never touch/write that same file). So instead of guessing
a set of "final" literals and quietly hoping they're right, this build
ships the AUDIT ITSELF -- run it yourself, once real instr1 data has
accumulated (spec's own go-live order wants >=2-4 weeks; the operator's
override only waived the CANARY wait, not this one) -- and copy its
printed literals into ``alphaos/scanner/candidate_scanner.py``'s
``SHADOW_V1_*`` constants (bumping the version comment from
``interest_score_shadow_v1`` to ``_v2`` when you do, per the spec's own
"a literal change is a version bump, never an in-place edit" law).

USAGE
-----
    .venv/bin/python scripts/shadow_saturation_audit.py [--db data/alphaos.db]

Read-only: opens the DB with SQLite's ``mode=ro`` URI (the same idiom
``JournalStore``'s own read-only path uses) -- this script cannot write to
your production ledger even if it tried.
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys


def _percentile(values: list, pct: float) -> float:
    """Nearest-rank percentile -- no numpy dependency, matches this
    codebase's own deterministic nearest-rank convention (see
    Orchestrator._count_top_decile_interest)."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round(pct * (len(ordered) - 1)))))
    return ordered[idx]


def audit(db_path: str) -> int:
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError as exc:
        print(f"could not open {db_path!r} read-only: {exc}", file=sys.stderr)
        return 1
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT c.rel_strength AS change_pct, c.unusual_volume AS rel_volume, "
        "p.bar_high, p.bar_low, p.last_price, substr(c.created_at_sgt, 1, 10) AS day "
        "FROM candidates c LEFT JOIN price_snapshots p ON p.snapshot_id = c.price_snapshot_id "
        "WHERE c.shadow_tier = 1 AND c.instrument_version = 'instr1'"
    ).fetchall()

    n = len(rows)
    distinct_days = len({r["day"] for r in rows if r["day"]})
    print(f"instr1 shadow-tier candidate rows: {n}")
    print(f"distinct trading days represented: {distinct_days}")
    if distinct_days < 20:  # ~4 weeks of trading days
        print(
            "\nWARNING: fewer than ~20 trading days of instr1 shadow data (spec's own "
            "go-live order wants 2-4 weeks before this audit sets literals with real "
            "confidence). Numbers below are PROVISIONAL at this sample size -- do not "
            "treat them as final; do not flip SHADOW_LABELLING_ENABLED on them alone.",
            file=sys.stderr,
        )
    if n == 0:
        print("no instr1 shadow-tier rows found -- nothing to audit yet.")
        return 0

    changes = [abs(r["change_pct"]) for r in rows if r["change_pct"] is not None]
    rel_vols = [r["rel_volume"] for r in rows if r["rel_volume"] is not None]
    day_ranges = [
        (r["bar_high"] - r["bar_low"]) / r["last_price"]
        for r in rows
        if r["bar_high"] and r["bar_low"] and r["last_price"]
    ]

    print("\n--- |change_pct| distribution (megacap change_scale today: 0.06) ---")
    for pct in (0.5, 0.75, 0.90, 0.95):
        print(f"  p{int(pct*100)}: {_percentile(changes, pct):.4f}")
    print(f"  mean: {statistics.mean(changes):.4f}" if changes else "  (no data)")

    print("\n--- rel_volume distribution (megacap rel_vol_scale today: 2.0, "
          "normalized as rel_volume-1.0) ---")
    for pct in (0.5, 0.75, 0.90, 0.95):
        print(f"  p{int(pct*100)}: {_percentile(rel_vols, pct):.4f}")
    print(f"  mean: {statistics.mean(rel_vols):.4f}" if rel_vols else "  (no data)")

    print("\n--- day_range distribution (megacap day_range_min today: 0.02, "
          "described as 'always-true' at this band) ---")
    for pct in (0.10, 0.25, 0.5, 0.75):
        print(f"  p{int(pct*100)}: {_percentile(day_ranges, pct):.4f}")
    if day_ranges:
        always_true_rate = sum(1 for d in day_ranges if d >= 0.02) / len(day_ranges)
        print(f"  fraction >= 0.02 (today's floor): {always_true_rate:.3f}")

    print(
        "\nSUGGESTED (not authoritative -- read the distributions above and use "
        "judgment): "
        "change_scale ~= p75-p90 of |change_pct| (a move at this percentile should "
        "read as 'strongly interesting', not saturate at 1.0); "
        "rel_vol_scale ~= p75-p90 of rel_volume; "
        "day_range_min ~= a value where 'fraction >= floor' is well below 1.0 "
        "(today's 0.02 floor reads 'always-true' per the spec -- if the printed "
        "fraction above is also ~1.0, raise the floor until it meaningfully "
        "discriminates)."
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/alphaos.db", help="path to the production ledger (read-only)")
    args = parser.parse_args()
    sys.exit(audit(args.db))
