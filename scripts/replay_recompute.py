#!/usr/bin/env python3
"""AILEG-1 (2026-08-16, docs/roadmap/alphaos-aileg1-replay-window-coherence-
spec.md, spec section 4) -- THE OPERATOR DECISION.

Re-runs ``alphaos.learning.outcomes_engine.replay_bracket`` for every EXISTING
``candidate_outcomes``/``shadow_baseline_decisions`` row that was resolved
BEFORE this ticket's fix landed (identified structurally by
``replay_window_days IS NULL`` -- every row this build's fixed code writes
going forward always stamps that column, so NULL is exactly "resolved under
the old, incoherent-window logic"), using each row's now-correct, card-derived
window (the SAME section-2 rule the live pipeline now applies to every NEW
row -- ``alphaos.learning.outcomes_tracker.resolve_ai_replay_window`` for the
AI leg, ``alphaos.baseline.tracker.resolve_baseline_arm_windows`` for the
baseline leg -- reused here verbatim, never a second resolution
implementation).

WHY THIS IS A LEGITIMATE REPAIR, NOT A RE-ROLL (spec section 4): ``replay_r``
is a DETERMINISTIC function of (entry, stop, target, direction, bars,
window). Every row already holds entry/stop/target/direction; ``bars`` are
historical daily OHLCV, immutable once a trading day closes; only ``window``
was ever wrong. Recomputing with the correct window is a repair, not a
re-roll: it cannot be influenced by knowing the result, and running it twice
against an unchanged ledger/bars source gives the same answer (see
``run_replay_recompute``'s own idempotency).

DARK BY DEFAULT (spec section 4's own "the merge must not recompute
anything" law): dry-run unless ``--apply`` is passed. This script is NEVER
invoked by any scheduler job, startup path, or migration -- grep this
codebase's own scheduler/orchestrator wiring and you will not find it
imported anywhere outside this file and its own tests
(tests/test_aileg1_replay_window_coherence.py pins this with an AST/import
guard). The operator runs this as its own, separate, explicit decision, with
the printed report going into a §9 log entry BEFORE ``--apply`` ever runs.

USAGE
-----
    .venv/bin/python scripts/replay_recompute.py [--db data/alphaos.db] [--apply] [--limit N]

``--apply`` writes ONLY the exact rows the immediately-preceding dry-run
would have reported (both are computed by the SAME function, over the SAME
NULL-replay_window_days row set) -- never a superset, never a sample. Every
write is an UPDATE of existing replay_result/replay_r/replay_exit_reason/
replay_window_days columns; this script never INSERTs or DELETEs a row.

Bars come from the real historical bars provider
(``alphaos.data.providers.alpaca_bars.make_bars_provider`` -- the same one
``update_pending_outcomes``/``resolve_pending_baseline_decisions`` already
use in production) unless a caller injects a different one (tests / an
operator measurement pass against a read-only DB copy with a cached bars
adapter -- see this script's own ``run_replay_recompute`` for the injection
point)."""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from alphaos.baseline.tracker import resolve_baseline_arm_windows
from alphaos.config.settings import load_settings
from alphaos.journal.journal_store import JournalStore
from alphaos.learning.outcomes_engine import replay_bracket
from alphaos.learning.outcomes_tracker import resolve_ai_replay_window
from alphaos.util import timeutils

# Only rows a real bracket replay was ever attempted for -- 'no_action'
# baseline rows and any row missing entry/stop/target never had a window to
# get wrong (see the module docstring's own framing: "only window was ever
# wrong" presupposes a replay actually ran).
_AI_LEG_ELIGIBLE_SQL = (
    "SELECT * FROM candidate_outcomes WHERE outcome_status = 'complete' "
    "AND replay_window_days IS NULL AND replay_result IS NOT NULL "
    "ORDER BY id ASC LIMIT ?"
)
_BASELINE_ELIGIBLE_SQL = (
    "SELECT * FROM shadow_baseline_decisions WHERE replay_status = 'complete' "
    "AND replay_window_days IS NULL AND decision = 'propose' "
    "ORDER BY id ASC LIMIT ?"
)


def _recompute_ai_leg_row(journal, bars_provider, row: dict) -> Optional[dict]:
    """Recompute ONE candidate_outcomes row. Returns a change record (always
    -- replay_window_days moves from NULL to a real value even when
    replay_r itself doesn't move) or None if this row can't be recomputed
    right now (e.g. no forward bars available from bars_provider -- left
    for a later pass, never guessed)."""
    window_days, used_fallback = resolve_ai_replay_window(journal, row.get("candidate_id"))
    decision_at = timeutils.parse_iso(row.get("decision_at_utc"))
    if decision_at is None:
        return None
    decision_date = decision_at.date().isoformat()
    today = timeutils.now_utc().date().isoformat()
    bars = bars_provider.get_daily_bars(row["symbol"], decision_date, today) or []
    forward_bars = [b for b in bars if b.get("date") and b["date"] > decision_date]
    if not forward_bars:
        return None

    replay = replay_bracket(
        row.get("entry_reference_price"), row.get("stop_price"), row.get("target_price"),
        row.get("direction_hint"), forward_bars, max_days=window_days,
    )
    return {
        "table": "candidate_outcomes",
        "row_id": row["outcome_id"],
        "candidate_id": row.get("candidate_id"),
        "symbol": row.get("symbol"),
        "old_replay_window_days": row.get("replay_window_days"),
        "new_replay_window_days": window_days,
        "used_fallback_card": used_fallback,
        "old_replay_result": row.get("replay_result"),
        "new_replay_result": replay["result"],
        "old_replay_r": row.get("replay_r"),
        "new_replay_r": replay["replay_r"],
        "old_replay_exit_reason": row.get("replay_exit_reason"),
        "new_replay_exit_reason": replay["replay_exit_reason"],
        "value_changed": (
            row.get("replay_result") != replay["result"]
            or row.get("replay_r") != replay["replay_r"]
            or row.get("replay_exit_reason") != replay["replay_exit_reason"]
        ),
    }


def _recompute_baseline_row(journal, bars_provider, row: dict, arm_windows: dict) -> Optional[dict]:
    """Recompute ONE shadow_baseline_decisions row. Same shape/contract as
    ``_recompute_ai_leg_row``."""
    window_days = arm_windows.get(row.get("rule_version"))
    if window_days is None:   # an orphaned/unknown rule_version -- never guess a window for it
        return None
    decision_at = timeutils.parse_iso(row.get("decision_at_utc"))
    if decision_at is None:
        return None
    decision_date = decision_at.date().isoformat()
    today = timeutils.now_utc().date().isoformat()
    bars = bars_provider.get_daily_bars(row["symbol"], decision_date, today) or []
    forward_bars = [b for b in bars if b.get("date") and b["date"] > decision_date]
    if not forward_bars:
        return None

    replay = replay_bracket(
        row.get("entry"), row.get("stop"), row.get("target"), row.get("direction"),
        forward_bars, max_days=window_days,
    )
    return {
        "table": "shadow_baseline_decisions",
        "row_id": row["baseline_decision_id"],
        "candidate_id": row.get("candidate_id"),
        "rule_version": row.get("rule_version"),
        "symbol": row.get("symbol"),
        "old_replay_window_days": row.get("replay_window_days"),
        "new_replay_window_days": window_days,
        "old_replay_result": row.get("replay_result"),
        "new_replay_result": replay["result"],
        "old_replay_r": row.get("replay_r"),
        "new_replay_r": replay["replay_r"],
        "old_replay_exit_reason": row.get("replay_exit_reason"),
        "new_replay_exit_reason": replay["replay_exit_reason"],
        "value_changed": (
            row.get("replay_result") != replay["result"]
            or row.get("replay_r") != replay["replay_r"]
            or row.get("replay_exit_reason") != replay["replay_exit_reason"]
        ),
    }


def _apply_ai_leg_change(journal, change: dict) -> None:
    journal.conn.execute(
        "UPDATE candidate_outcomes SET replay_result = ?, replay_r = ?, replay_exit_reason = ?, "
        "replay_window_days = ? WHERE outcome_id = ?",
        (change["new_replay_result"], change["new_replay_r"], change["new_replay_exit_reason"],
         change["new_replay_window_days"], change["row_id"]),
    )


def _apply_baseline_change(journal, change: dict) -> None:
    journal.conn.execute(
        "UPDATE shadow_baseline_decisions SET replay_result = ?, replay_r = ?, replay_exit_reason = ?, "
        "replay_window_days = ? WHERE baseline_decision_id = ?",
        (change["new_replay_result"], change["new_replay_r"], change["new_replay_exit_reason"],
         change["new_replay_window_days"], change["row_id"]),
    )


def run_replay_recompute(journal, bars_provider, apply: bool = False, limit: int = 5000) -> dict:
    """The recompute pass, over BOTH tables. Pure with respect to the DB
    when ``apply=False`` (issues zero UPDATE statements -- SELECT-only);
    when ``apply=True``, writes EXACTLY the rows this same call computed
    (never a superset -- there is only one code path from "computed" to
    "written", no separate re-query in between).

    Idempotent by construction: every written row's replay_window_days
    becomes non-NULL, so it drops out of ``_AI_LEG_ELIGIBLE_SQL``/
    ``_BASELINE_ELIGIBLE_SQL``'s own ``WHERE ... IS NULL`` on the next call
    -- a second ``--apply`` run against the same ledger finds zero eligible
    rows and writes nothing. Deterministic given an unchanged bars source:
    calling this twice with ``apply=False`` (or any number of times before
    the first ``apply=True``) must return byte-identical change records,
    since replay_r is a pure function of (entry, stop, target, direction,
    bars, window) and none of those five inputs are mutated by a dry run."""
    arm_windows = resolve_baseline_arm_windows()

    ai_leg_rows = journal.query(_AI_LEG_ELIGIBLE_SQL, (limit,))
    ai_leg_changes = []
    ai_leg_skipped_no_bars = 0
    for row in ai_leg_rows:
        change = _recompute_ai_leg_row(journal, bars_provider, row)
        if change is None:
            ai_leg_skipped_no_bars += 1
            continue
        ai_leg_changes.append(change)
        if apply:
            _apply_ai_leg_change(journal, change)

    baseline_rows = journal.query(_BASELINE_ELIGIBLE_SQL, (limit,))
    baseline_changes = []
    baseline_skipped_no_bars = 0
    for row in baseline_rows:
        change = _recompute_baseline_row(journal, bars_provider, row, arm_windows)
        if change is None:
            baseline_skipped_no_bars += 1
            continue
        baseline_changes.append(change)
        if apply:
            _apply_baseline_change(journal, change)

    if apply and (ai_leg_changes or baseline_changes):
        journal.conn.commit()

    return {
        "apply": apply,
        "candidate_outcomes": {
            "eligible": len(ai_leg_rows),
            "recomputed": len(ai_leg_changes),
            "value_changed": sum(1 for c in ai_leg_changes if c["value_changed"]),
            "skipped_no_forward_bars": ai_leg_skipped_no_bars,
            "changes": ai_leg_changes,
        },
        "shadow_baseline_decisions": {
            "eligible": len(baseline_rows),
            "recomputed": len(baseline_changes),
            "value_changed": sum(1 for c in baseline_changes if c["value_changed"]),
            "skipped_no_forward_bars": baseline_skipped_no_bars,
            "changes": baseline_changes,
        },
    }


def _mean(values: list) -> Optional[float]:
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 4) if values else None


def print_report(report: dict) -> None:
    mode = "APPLY (writes committed)" if report["apply"] else "DRY-RUN (nothing written)"
    print(f"replay_recompute -- {mode}\n")
    for table_key, label in (
        ("candidate_outcomes", "AI leg (candidate_outcomes)"),
        ("shadow_baseline_decisions", "baseline leg (shadow_baseline_decisions)"),
    ):
        section = report[table_key]
        print(f"-- {label} --")
        print(f"  eligible (replay_window_days IS NULL, previously replayed): {section['eligible']}")
        print(f"  recomputed (forward bars available):                        {section['recomputed']}")
        print(f"  value_changed (replay_result/replay_r/exit_reason moved):   {section['value_changed']}")
        print(f"  skipped (no forward bars available yet):                    {section['skipped_no_forward_bars']}")
        before_r = _mean([c["old_replay_r"] for c in section["changes"]])
        after_r = _mean([c["new_replay_r"] for c in section["changes"]])
        print(f"  mean replay_r before: {before_r}   after: {after_r}")
        print()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/alphaos.db", help="journal DB path")
    parser.add_argument("--apply", action="store_true", help="write the recomputed rows (default: dry-run only)")
    parser.add_argument("--limit", type=int, default=5000, help="max rows to recompute per table")
    args = parser.parse_args(argv)

    settings = load_settings()
    journal = JournalStore(args.db)
    try:
        from alphaos.data.providers.alpaca_bars import make_bars_provider
        bars_provider = make_bars_provider(settings, journal)
        if bars_provider is None:
            print(
                "No live bars provider available (mock/offline settings) -- "
                "replay_recompute needs a real historical-bars source. Aborting.",
                file=sys.stderr,
            )
            return 1
        report = run_replay_recompute(journal, bars_provider, apply=args.apply, limit=args.limit)
        print_report(report)
        return 0
    finally:
        journal.close()


if __name__ == "__main__":
    sys.exit(main())
