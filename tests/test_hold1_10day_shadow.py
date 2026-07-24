"""HOLD-1: 10-trading-day shadow outcome horizon
(docs/roadmap/alphaos-hold1-10day-shadow-horizon-spec.md, drafted 2026-07-24).

Six named tests below map 1:1 onto the spec's own "Tests (hermetic, S.H.1)"
list:

1. test_additive_migration_on_old_db_schema_version_stays_3_old_rows_null
2. test_10d_resolution_long_and_short_target_first_reached_day7
3. test_10d_window_not_yet_elapsed_stays_partial_without_touching_5d_status
4. test_report_arithmetic_on_seeded_ledger_exact_fractions
5. test_no_live_scan_eval_risk_execution_module_reads_the_new_columns_ast
6. test_digest_line_renders_the_accumulation_count

All offline, in-memory, mock mode. No real money, no network. Measurement
only -- see outcomes_tracker.py's own module docstring for why the 10d
continuation pass is structurally separate from the 1d/3d/5d loop.
"""

from __future__ import annotations

import datetime
import sqlite3

from alphaos.journal.journal_store import JournalStore
from alphaos.journal.schema import SCHEMA_VERSION
from alphaos.orchestrator import Orchestrator
from alphaos.learning.outcomes_tracker import seed_pending_outcomes, update_pending_outcomes
from alphaos.reports.hold1_report import (
    CAVEAT,
    REVISIT_FLOOR,
    TARGET_R,
    compute_hold1_report,
    hold1_digest_line,
    render_markdown as render_hold1_markdown,
)
from alphaos.util.ids import new_id
from conftest import make_settings


def _orch(**over):
    return Orchestrator(settings=make_settings(**over), journal=JournalStore(":memory:"))


class _FakeBars:
    def __init__(self, bars_by_symbol):
        self.bars_by_symbol = bars_by_symbol

    def get_daily_bars(self, symbol, start, end):
        return self.bars_by_symbol.get(symbol, [])


def _candidate(o, symbol="AAPL", **over):
    cand_id = new_id("cand")
    row = {
        "candidate_id": cand_id, "symbol": symbol, "direction": "long", "strategy": "swing",
        "momentum_score": 0.7, "status": "watch", "armed_watch": 0,
        "scan_id": "scan_x", "scan_batch_id": "scanb_x", "playbook_name": "momentum",
    }
    row.update(over)
    o.journal.insert("candidates", row)
    return cand_id


def _eval(o, cand_id, symbol="AAPL", entry=100.0, stop=95.0, target=112.0, direction="long"):
    o.journal.insert("openai_evaluations", {
        "eval_id": new_id("eval"), "candidate_id": cand_id, "symbol": symbol, "model": "mock",
        "direction": direction, "entry": entry, "stop": stop, "target": target,
        "max_holding_days": 5, "expected_r": 2.0, "confidence": 0.7, "decision": "propose",
        "reasoning_summary": "t", "is_mock": 1,
    })


def _proposal(o, cand_id, symbol="AAPL", entry=100.0, stop=95.0, target=112.0, direction="long"):
    o.journal.insert("trade_proposals", {
        "proposal_id": new_id("prop"), "candidate_id": cand_id, "symbol": symbol,
        "direction": direction, "strategy": "swing", "entry": entry, "stop": stop, "target": target,
        "max_holding_days": 5, "qty": 10, "risk_per_share": abs(entry - stop),
        "dollar_risk": abs(entry - stop) * 10, "expected_r": 2.0, "status": "pending_approval",
        "playbook_name": "momentum",
    })


def _seeded_row(o, entry=100.0, stop=95.0, target=112.0, symbol="AAPL", direction="long"):
    cid = _candidate(o, symbol=symbol, direction=direction)
    _eval(o, cid, symbol=symbol, entry=entry, stop=stop, target=target, direction=direction)
    _proposal(o, cid, symbol=symbol, entry=entry, stop=stop, target=target, direction=direction)
    seed_pending_outcomes(o.journal)
    return o.journal.one("SELECT * FROM candidate_outcomes WHERE candidate_id = ?", (cid,))


def _bars_from(d0: datetime.date, spec: list[tuple]) -> list[dict]:
    """spec: list of (day_offset, open, high, low, close)."""
    return [
        {"date": (d0 + datetime.timedelta(days=off)).isoformat(),
         "open": o, "high": hi, "low": lo, "close": c}
        for off, o, hi, lo, c in spec
    ]


# ---------------------------------------------------------------- Spec test 1
def test_additive_migration_on_old_db_schema_version_stays_3_old_rows_null(tmp_path):
    """An old ledger written before HOLD-1 (candidate_outcomes exists but
    lacks the 6 new value columns + outcome_status_10d) must gain all 7
    additively on open -- SCHEMA_VERSION stays 3, and a pre-existing row
    reads back with every new column NULL (never fabricated/backfilled),
    exactly like every other post-hoc column addition in this codebase's
    history (see tests/test_schema_migration.py's own sibling tests)."""
    db = str(tmp_path / "pre_hold1.db")
    raw = sqlite3.connect(db)
    raw.execute(
        "CREATE TABLE candidate_outcomes (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "outcome_id TEXT NOT NULL UNIQUE, candidate_id TEXT NOT NULL, symbol TEXT NOT NULL, "
        "candidate_type TEXT NOT NULL, decision_at_utc TEXT, "
        "outcome_status TEXT NOT NULL DEFAULT 'pending', "
        "forward_5d_r REAL, max_favorable_5d_r REAL, "
        "created_at_utc TEXT NOT NULL, created_at_sgt TEXT NOT NULL)"
    )
    raw.execute(
        "INSERT INTO candidate_outcomes "
        "(outcome_id, candidate_id, symbol, candidate_type, outcome_status, "
        "forward_5d_r, max_favorable_5d_r, created_at_utc, created_at_sgt) "
        "VALUES ('out1', 'cand1', 'AAPL', 'proposal', 'complete', 0.5, 1.1, "
        "'2026-07-01T00:00:00+00:00', '2026-07-01T08:00:00+08:00')"
    )
    raw.execute("PRAGMA user_version = 3")
    raw.commit()
    raw.close()

    j = JournalStore(db)
    try:
        assert j.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert SCHEMA_VERSION == 3

        cols = {r["name"] for r in j.conn.execute("PRAGMA table_info(candidate_outcomes)")}
        assert {
            "forward_10d_return_pct", "forward_10d_r", "max_favorable_10d_r",
            "max_adverse_10d_r", "bars_to_favorable_10d", "bars_to_adverse_10d",
            "outcome_status_10d",
        } <= cols

        row = j.one("SELECT * FROM candidate_outcomes WHERE outcome_id = 'out1'")
        assert row["forward_10d_return_pct"] is None
        assert row["forward_10d_r"] is None
        assert row["max_favorable_10d_r"] is None
        assert row["max_adverse_10d_r"] is None
        assert row["bars_to_favorable_10d"] is None
        assert row["bars_to_adverse_10d"] is None
        assert row["outcome_status_10d"] is None
        # Pre-existing 5d data is untouched by the migration.
        assert row["forward_5d_r"] == 0.5 and row["max_favorable_5d_r"] == 1.1
        assert row["outcome_status"] == "complete"
    finally:
        j.close()


# ---------------------------------------------------------------- Spec test 2
def test_10d_resolution_long_and_short_target_first_reached_day7():
    """Exact MFE/MAE/bars-to arithmetic for LONG and SHORT, both hitting the
    2.4xATR target distance for the FIRST time on day 7 -- the exact
    pre-registered scenario: the 5d family says <2.4, the 10d family says
    >=2.4 with bars_to_favorable_10d == 7."""
    # ---- LONG: entry=100, stop=95 (risk=5); 2.4R distance = 100+2.4*5=112.
    o = _orch()
    row = _seeded_row(o, entry=100.0, stop=95.0, target=999.0, symbol="LONGX", direction="long")
    d0 = datetime.date.fromisoformat(row["created_at_utc"][:10])
    bars = _bars_from(d0, [
        (1, 100, 105, 99, 101),
        (2, 101, 108, 100, 104),
        (3, 104, 110, 102, 105),   # days 1-5 max high = 110 -> (110-100)/5 = 2.0 < 2.4
        (4, 105, 107, 103, 104),
        (5, 104, 106, 102, 103),
        (6, 103, 109, 101, 105),
        (7, 105, 113, 104, 110),   # FIRST bar >= 112 -> (113-100)/5 = 2.6 >= 2.4
        (8, 110, 111, 105, 109),
        (9, 109, 110, 104, 108),
        (10, 108, 112, 105, 109),
    ])
    res = update_pending_outcomes(o.journal, bars_provider=_FakeBars({"LONGX": bars}))
    assert res["completed"] == 1
    assert res["hold1_10d"]["completed"] == 1
    updated = o.journal.one("SELECT * FROM candidate_outcomes WHERE outcome_id = ?", (row["outcome_id"],))
    assert updated["max_favorable_5d_r"] == 2.0
    assert updated["max_favorable_5d_r"] < TARGET_R
    assert updated["max_favorable_10d_r"] == 2.6
    assert updated["max_favorable_10d_r"] >= TARGET_R
    assert updated["bars_to_favorable_10d"] == 7
    assert updated["outcome_status_10d"] == "complete"
    o.close()

    # ---- SHORT: entry=100, stop=105 (risk=5); 2.4R distance = 100-2.4*5=88.
    o2 = _orch()
    row2 = _seeded_row(o2, entry=100.0, stop=105.0, target=1.0, symbol="SHORTX", direction="short")
    d0b = datetime.date.fromisoformat(row2["created_at_utc"][:10])
    bars2 = _bars_from(d0b, [
        (1, 100, 101, 95, 99),
        (2, 99, 100, 92, 96),
        (3, 96, 98, 90, 94),        # days 1-5 min low = 90 -> (100-90)/5 = 2.0 < 2.4
        (4, 94, 96, 91, 93),
        (5, 93, 95, 92, 94),
        (6, 94, 96, 91, 93),
        (7, 93, 95, 87, 90),        # FIRST bar <= 88 -> (100-87)/5 = 2.6 >= 2.4
        (8, 90, 92, 89, 91),
        (9, 91, 93, 90, 92),
        (10, 92, 94, 88, 91),
    ])
    res2 = update_pending_outcomes(o2.journal, bars_provider=_FakeBars({"SHORTX": bars2}))
    assert res2["completed"] == 1
    assert res2["hold1_10d"]["completed"] == 1
    updated2 = o2.journal.one("SELECT * FROM candidate_outcomes WHERE outcome_id = ?", (row2["outcome_id"],))
    assert updated2["max_favorable_5d_r"] == 2.0
    assert updated2["max_favorable_5d_r"] < TARGET_R
    assert updated2["max_favorable_10d_r"] == 2.6
    assert updated2["max_favorable_10d_r"] >= TARGET_R
    assert updated2["bars_to_favorable_10d"] == 7
    assert updated2["outcome_status_10d"] == "complete"
    o2.close()


# ---------------------------------------------------------------- Spec test 3
def test_10d_window_not_yet_elapsed_stays_partial_without_touching_5d_status():
    """A row whose 5-day family has resolved (outcome_status='complete') but
    whose 10-day window has NOT yet elapsed (only 7 of 10 forward bars exist
    so far) must show outcome_status_10d='partial' -- and, critically,
    outcome_status itself (the field every existing consumer reads) must be
    completely unaffected by the 10d family's own state, across MULTIPLE
    update passes."""
    o = _orch()
    row = _seeded_row(o, entry=100.0, stop=90.0, target=999.0)
    d0 = datetime.date.fromisoformat(row["created_at_utc"][:10])
    bars = _bars_from(d0, [
        (i, 100, 101, 99, 100.2) for i in range(1, 8)   # 7 bars: 5d resolves, 10d does not
    ])
    provider = _FakeBars({"AAPL": bars})

    first = update_pending_outcomes(o.journal, bars_provider=provider)
    assert first["completed"] == 1                       # 5d resolved
    assert first["hold1_10d"]["updated"] == 1
    assert first["hold1_10d"]["completed"] == 0           # 10d NOT resolved (7 < 10)

    row1 = o.journal.one("SELECT * FROM candidate_outcomes WHERE outcome_id = ?", (row["outcome_id"],))
    assert row1["outcome_status"] == "complete"            # 5d family: unaffected
    assert row1["outcome_status_10d"] == "partial"         # 10d family: genuinely pending
    assert row1["forward_10d_r"] is not None                # partial value written (mirrors 1d/3d/5d convention)

    # A second pass, with the SAME (still-only-7-bar) fixture, must be
    # idempotent for the 5d family and re-derive the SAME partial 10d
    # values -- never flips outcome_status, never fabricates a false
    # "complete" for the 10d family.
    second = update_pending_outcomes(o.journal, bars_provider=provider)
    assert second["updated"] == 0 and second["completed"] == 0   # 5d loop: nothing left to do
    assert second["hold1_10d"]["completed"] == 0
    row2 = o.journal.one("SELECT * FROM candidate_outcomes WHERE outcome_id = ?", (row["outcome_id"],))
    assert row2["outcome_status"] == "complete"
    assert row2["outcome_status_10d"] == "partial"
    assert row2["forward_10d_r"] == row1["forward_10d_r"]   # stable, not drifting
    o.close()


# ---------------------------------------------------------------- Spec test 4
def _insert_resolved_row(journal, candidate_type, symbol, decision_date, f5r, f10r, max_holding_days=5):
    cand_id = new_id("cand")
    journal.insert("candidates", {"candidate_id": cand_id, "symbol": symbol})
    journal.insert("trade_proposals", {
        "proposal_id": new_id("prop"), "candidate_id": cand_id, "symbol": symbol,
        "direction": "long", "strategy": "swing", "entry": 100.0, "stop": 95.0, "target": 120.0,
        "max_holding_days": max_holding_days, "qty": 10, "risk_per_share": 5.0,
        "dollar_risk": 50.0, "expected_r": 2.0, "status": "pending_approval",
        "playbook_name": "momentum",
    })
    journal.insert("candidate_outcomes", {
        "outcome_id": new_id("cout"), "candidate_id": cand_id, "symbol": symbol,
        "candidate_type": candidate_type, "decision_at_utc": f"{decision_date}T12:00:00+00:00",
        "outcome_status": "complete", "outcome_status_10d": "complete",
        "max_favorable_5d_r": f5r, "max_favorable_10d_r": f10r,
    })


def test_report_arithmetic_on_seeded_ledger_exact_fractions():
    """Known cohort counts in -> exact fractions out; caveat line present.
    Denominator: rows that FAILED to reach 2.4R by day 5 (max_favorable_5d_r
    < TARGET_R). Numerator: of those, rows that reached the threshold by
    day 10."""
    o = _orch()
    j = o.journal

    # 'proposed' cohort: 4 failed-by-day5 rows, 3 reach >=2.4R by day 10,
    # a 4th only reaches the 1.2R halfway mark.
    _insert_resolved_row(j, "proposal", "P1", "2026-01-05", 2.0, 2.5)
    _insert_resolved_row(j, "proposal", "P2", "2026-01-06", 1.9, 2.6)
    _insert_resolved_row(j, "blocked", "P3", "2026-01-07", 2.1, 2.4)   # 'blocked' folds into 'proposed'
    _insert_resolved_row(j, "proposal", "P4", "2026-01-08", 1.5, 1.3)   # halfway only, not 2.4R
    # A row that already cleared 2.4R by day 5 -- excluded from the
    # denominator entirely (not "failed by day 5").
    _insert_resolved_row(j, "proposal", "P5", "2026-01-09", 2.5, 2.9)

    # 'watch' cohort: 2 failed-by-day5, 1 reaches 2.4R by day 10.
    _insert_resolved_row(j, "armed_watch", "W1", "2026-01-05", 1.0, 2.5)
    _insert_resolved_row(j, "armed_watch", "W2", "2026-01-06", 0.5, 0.6)

    # 'rejected' cohort: 1 failed-by-day5, 0 reach 2.4R by day 10.
    _insert_resolved_row(j, "reject", "R1", "2026-01-05", -0.2, 0.1)

    # Audit fixup (2026-07-25, Auditor A MEDIUM): rows OUTSIDE the
    # pre-registered question's own population -- a bare 'candidate' row
    # (never acted on either way) and a 'user_override' row (a different,
    # human-decision population), both otherwise resolved and failed-by-
    # day5 -- must be counted in the UNSCOPED diagnostic but NOT in
    # n_pre_registered_population/effective_n/the revisit gate. (P5 above,
    # already cleared 2.4R by day 5, is the third excluded case -- outside
    # the population for a different reason: not "failed by day 5" at all.)
    _insert_resolved_row(j, "candidate", "C1", "2026-01-05", 1.0, 2.5)
    _insert_resolved_row(j, "user_override", "U1", "2026-01-05", 1.0, 2.5)

    rep = compute_hold1_report(j)
    prop = rep["by_cohort_target_2_4r"]["proposed"]
    assert prop["n_failed_by_day5"] == 4
    assert prop["n_reached_days6_10"] == 3
    assert prop["fraction"] == round(3 / 4, 4)

    watch = rep["by_cohort_target_2_4r"]["watch"]
    assert watch["n_failed_by_day5"] == 2
    assert watch["n_reached_days6_10"] == 1
    assert watch["fraction"] == round(1 / 2, 4)

    rejected = rep["by_cohort_target_2_4r"]["rejected"]
    assert rejected["n_failed_by_day5"] == 1
    assert rejected["n_reached_days6_10"] == 0
    assert rejected["fraction"] == 0.0

    # Halfway (1.2R) context fraction, same denominator, different
    # threshold: 'proposed' has all 4 failed-by-day5 rows reach >=1.2R
    # (2.5, 2.6, 2.4, 1.3 are all >= 1.2).
    prop_half = rep["by_cohort_halfway_1_2r"]["proposed"]
    assert prop_half["n_failed_by_day5"] == 4
    assert prop_half["n_reached_days6_10"] == 4
    assert prop_half["fraction"] == 1.0

    # Unscoped diagnostic: EVERY resolved row, all candidate_types --
    # P1-P5, W1-W2, R1, C1 (candidate), U1 (user_override) = 10.
    assert rep["n_resolved_both_families_unscoped"] == 10

    # Pre-registered population (Auditor A MEDIUM fix): only the 3 named
    # cohorts, only rows that failed 2.4R by day 5 -- P1,P2,P3,P4 (proposed;
    # P5 excluded, already cleared 2.4R by day 5) + W1,W2 (watch) + R1
    # (rejected) = 7. C1/U1 excluded entirely (wrong population), even
    # though both are otherwise resolved AND failed-by-day5.
    assert rep["n_pre_registered_population"] == 7
    # Every population row here is on a distinct symbol -> no clustering.
    assert rep["effective_n"] == 7
    assert rep["revisit_condition_met"] is False   # 7 < REVISIT_FLOOR (30)

    # The reported cohort fractions themselves are UNCHANGED by C1/U1 --
    # they were never in any cohort's denominator to begin with (proven
    # again here, post-fix, not just pre-fix above).
    assert prop["n_failed_by_day5"] == 4 and prop["n_reached_days6_10"] == 3

    assert rep["caveat"] == CAVEAT
    assert "no significance claimed" in rep["caveat"]

    md = render_hold1_markdown(rep)
    assert CAVEAT in md
    assert "HOLD-1" in md
    assert "UNSCOPED" in md
    assert f"{rep['n_resolved_both_families_unscoped']} raw resolved rows" in md
    o.close()


def test_report_empty_ledger_is_a_safe_no_op_not_an_error():
    o = _orch()
    rep = compute_hold1_report(o.journal)
    assert rep["n_resolved_both_families_unscoped"] == 0
    assert rep["n_pre_registered_population"] == 0
    assert rep["effective_n"] == 0
    assert rep["revisit_condition_met"] is False
    for cohort_stats in rep["by_cohort_target_2_4r"].values():
        assert cohort_stats["n_failed_by_day5"] == 0
        assert cohort_stats["fraction"] is None
    md = render_hold1_markdown(rep)
    assert "no resolved observations yet" in md
    o.close()


# ---------------------------------------------------------------- Spec test 5
# Audit fixup (2026-07-25, Auditor A NIT + Auditor B LOW, convergent): the
# original guard spot-checked 5 hand-picked modules. Both auditors'
# independent greps confirmed nothing violates the law TODAY -- this widens
# it to glob the whole decision-surface (regression-guard hardening, not a
# fix to a found violation). Globs directories rather than hand-listing
# every file, so a NEW file added later to any of these packages is
# automatically covered without anyone remembering to add it here.
_DECISION_SURFACE_PACKAGES = (
    "risk", "strategy", "execution", "tqs", "scanner", "ai",
)
_DECISION_SURFACE_ROOT_FILES = ("approval.py", "safety.py")
# Auditor B's own enumerated candidate_outcomes consumers that sit OUTSIDE
# the globbed packages above (ai/label_validation.py is already covered by
# the "ai" package glob).
_DECISION_SURFACE_EXTRA_FILES = (
    "hypotheses/proposer.py",
    "scheduler/shadow_label.py",
)

_HOLD1_NEW_COLUMNS = (
    "forward_10d_return_pct", "forward_10d_r", "max_favorable_10d_r",
    "max_adverse_10d_r", "bars_to_favorable_10d", "bars_to_adverse_10d",
    "outcome_status_10d",
)


def _decision_surface_files() -> list:
    import pathlib

    import alphaos

    root = pathlib.Path(alphaos.__file__).parent
    files = []
    for pkg in _DECISION_SURFACE_PACKAGES:
        files += sorted((root / pkg).rglob("*.py"))
    for fname in _DECISION_SURFACE_ROOT_FILES:
        files.append(root / fname)
    for rel in _DECISION_SURFACE_EXTRA_FILES:
        files.append(root / rel)
    files = [f for f in files if "__pycache__" not in f.parts]
    assert files, "decision-surface glob found nothing -- package layout changed, fix the glob"
    return files


def test_no_live_scan_eval_risk_execution_module_reads_the_new_columns_ast():
    """Structural proof, same zero-decision-surface pattern as
    tests/test_pr12_hypotheses.py::test_risk_engine_and_approval_never_
    reference_hypotheses_at_all and test_ab_eval.py's own AST/structural
    checks: no file anywhere under the decision-surface packages (risk/,
    strategy/, execution/, tqs/, scanner/, ai/), nor approval.py/safety.py,
    nor the specific candidate_outcomes consumers Auditor B enumerated that
    live outside those packages, may reference ANY of HOLD-1's 7 new column
    names, in any form (a docstring mention would still show up as source
    text -- this check is intentionally source-text-based, not AST-Call-
    based, so it also catches a stray comment/string reference, not just a
    live code read)."""
    for path in _decision_surface_files():
        text = path.read_text(encoding="utf-8")
        for col in _HOLD1_NEW_COLUMNS:
            assert col not in text, f"{path} references HOLD-1 column {col!r}"


def test_hold1_report_module_never_writes_and_never_touches_gates():
    """Defense in depth (complements the AST check above): compute_hold1_report
    is genuinely read-only against a real journal -- no order/approval/
    execution rows appear, real-money stays unreachable."""
    o = _orch()
    _insert_resolved_row(o.journal, "proposal", "AAPL", "2026-01-05", 2.0, 2.5)
    before = {t: o.journal.count_rows(t) for t in ("candidates", "trade_proposals", "candidate_outcomes")}
    compute_hold1_report(o.journal)
    after = {t: o.journal.count_rows(t) for t in ("candidates", "trade_proposals", "candidate_outcomes")}
    assert before == after
    assert o.journal.count_rows("paper_orders") == 0
    assert o.journal.count_rows("approvals") == 0
    assert o.system_health()["real_money_trading"] == "unreachable"
    o.close()


# ---------------------------------------------------------------- Spec test 6
def test_digest_line_renders_the_accumulation_count():
    """The daily brief's own accumulation line -- effective_n (independent
    clusters), NOT the raw resolved-row count, matching the revisit floor's
    own units (PORT-1's effective_n() law)."""
    o = _orch()
    j = o.journal
    _insert_resolved_row(j, "proposal", "AAPL", "2026-01-05", 2.0, 2.5)
    _insert_resolved_row(j, "proposal", "MSFT", "2026-01-06", 1.9, 2.6)

    rep = compute_hold1_report(j)
    line = hold1_digest_line(rep)
    assert line == f"HOLD-1: {rep['effective_n']}/{REVISIT_FLOOR} resolved 10d observations"
    assert rep["effective_n"] == 2   # two different symbols, different days -> 2 independent clusters
    assert f"{REVISIT_FLOOR}" in line
    o.close()


def test_daily_brief_markdown_includes_the_hold1_digest_line():
    """End-to-end: build_daily_brief's own render_markdown surfaces the
    HOLD-1 line even at 0/30 (Design Sec 4's own 'without anyone
    remembering to query' requirement) -- unlike every other *_health
    section in daily_brief.py, this one is never omitted at zero."""
    from alphaos.reports.daily_brief import build_daily_brief, render_markdown

    o = _orch()
    brief = build_daily_brief(o.journal, o.settings, o.kill_switch)
    assert brief["hold1_health"] is not None
    md = render_markdown(brief)
    assert f"HOLD-1: 0/{REVISIT_FLOOR} resolved 10d observations" in md
    o.close()
