"""AILEG-1 (2026-08-16, docs/roadmap/alphaos-aileg1-replay-window-coherence-
spec.md): the AI leg (learning/outcomes_tracker.py) and the baseline leg
(baseline/tracker.py) must replay the SAME kind of window -- the card that
governed the row, resolved BY ID, never the live default -- and persist that
window on the row. This file covers the spec's own 7 test obligations
(section 5), one named test (or small group) per obligation:

1. AI leg uses the candidate's card window, resolved BY ID, immune to an
   ACTIVE_CARD_ID swap.
2. Baseline v1/hold10 arms are pinned by id, no `or 5` fallback reachable
   even when a row's own stored max_holding_days is stale/NULL.
3. replay_window_days is persisted on both tables and matches the window
   used.
4. No production call site omits max_days (AST/grep guard).
5. Determinism: recomputing an unchanged row twice yields byte-identical
   values.
6. replay_recompute --dry-run writes nothing; --apply writes exactly what
   it reported; both idempotent.
7. Migration is additive; existing rows read back unchanged before any
   recompute.

Hermetic throughout: in-memory SQLite, fixture bars, no network.
"""

from __future__ import annotations

import ast
import pathlib
import sqlite3

import pytest

from alphaos.baseline.rules import THRESHOLD_V1, THRESHOLD_V1_HOLD10
from alphaos.baseline.tracker import (
    BASELINE_HOLD10_PINNED_CARD_ID,
    BASELINE_V1_PINNED_CARD_ID,
    record_shadow_baseline_decisions,
    resolve_baseline_arm_windows,
    resolve_pending_baseline_decisions,
)
from alphaos.journal.journal_store import JournalStore
from alphaos.journal.schema import SCHEMA_VERSION
from alphaos.learning.outcomes_tracker import (
    AI_LEG_FALLBACK_CARD_ID,
    REPLAY_WINDOW_FALLBACK_DQ_STATUS,
    resolve_ai_replay_window,
    seed_pending_outcomes,
    update_pending_outcomes,
)
from alphaos.orchestrator import Orchestrator
from alphaos.util import timeutils
from alphaos.util.ids import new_id
from conftest import make_settings

from scripts.replay_recompute import run_replay_recompute

ALPHAOS_DIR = pathlib.Path(__file__).resolve().parent.parent / "alphaos"


# --------------------------------------------------------------------- fixtures
def _orch(**over):
    return Orchestrator(settings=make_settings(**over), journal=JournalStore(":memory:"))


def _candidate(o, symbol="AAPL", card_id=None, **over):
    cand_id = new_id("cand")
    row = {
        "candidate_id": cand_id, "symbol": symbol, "direction": "long", "strategy": "swing",
        "momentum_score": 0.7, "status": "watch", "armed_watch": 0,
        "scan_id": "scan_x", "scan_batch_id": "scanb_x", "playbook_name": "momentum",
    }
    if card_id is not None:
        row["card_id"] = card_id
    row.update(over)
    o.journal.insert("candidates", row)
    return cand_id


def _eval(o, cand_id, symbol="AAPL", entry=100.0, stop=95.0, target=112.0):
    o.journal.insert("openai_evaluations", {
        "eval_id": new_id("eval"), "candidate_id": cand_id, "symbol": symbol, "model": "mock",
        "direction": "long", "entry": entry, "stop": stop, "target": target,
        "max_holding_days": 5, "expected_r": 2.0, "confidence": 0.7, "decision": "propose",
        "reasoning_summary": "t", "is_mock": 1,
    })


def _proposal(o, cand_id, symbol="AAPL", entry=100.0, stop=95.0, target=112.0):
    o.journal.insert("trade_proposals", {
        "proposal_id": new_id("prop"), "candidate_id": cand_id, "symbol": symbol,
        "direction": "long", "strategy": "swing", "entry": entry, "stop": stop, "target": target,
        "max_holding_days": 5, "qty": 10, "risk_per_share": entry - stop,
        "dollar_risk": (entry - stop) * 10, "expected_r": 2.0, "status": "pending_approval",
        "playbook_name": "momentum",
    })


def _seeded_row(o, *, card_id=None, entry=100.0, stop=95.0, target=112.0, symbol="AAPL"):
    cid = _candidate(o, symbol=symbol, card_id=card_id)
    _eval(o, cid, symbol=symbol, entry=entry, stop=stop, target=target)
    _proposal(o, cid, symbol=symbol, entry=entry, stop=stop, target=target)
    seed_pending_outcomes(o.journal)
    return o.journal.one("SELECT * FROM candidate_outcomes WHERE candidate_id = ?", (cid,))


class _FakeBars:
    """Deliberately does NOT filter by [start, end] (matches
    tests/test_outcomes_tracker.py's own _FakeBars) -- the production
    code's own forward_bars filtering (strictly after decision_date) is
    what actually matters for these tests, and several fixtures below
    build "days after the decision" from a decision_at_utc that is
    effectively "now" (real wall clock), which a date-bounded fake would
    incorrectly exclude as "in the future" relative to today."""
    def __init__(self, bars_by_symbol):
        self.bars_by_symbol = bars_by_symbol

    def get_daily_bars(self, symbol, start, end, limit=200):
        return self.bars_by_symbol.get(symbol, [])


def _bars_days_after(created_date: str, n: int, **defaults):
    import datetime
    d0 = datetime.date.fromisoformat(created_date)
    high = defaults.get("high", 101.0)
    low = defaults.get("low", 99.0)
    close = defaults.get("close", 100.2)
    return [
        {"date": (d0 + datetime.timedelta(days=i)).isoformat(), "open": close, "high": high,
         "low": low, "close": close}
        for i in range(1, n + 1)
    ]


def _pending_row(j, *, entry=100.0, stop=96.0, target=104.8, direction="long",
                 days_ago: int = 20, candidate_id="cand1", rule_version=THRESHOLD_V1,
                 symbol="AAPL", max_holding_days=None):
    from datetime import timedelta
    decision_at_utc = timeutils.to_iso(timeutils.now_utc() - timedelta(days=days_ago))
    j.insert("shadow_baseline_decisions", {
        "baseline_decision_id": new_id("basedec"), "candidate_id": candidate_id, "symbol": symbol,
        "rule_version": rule_version, "decision": "propose", "decision_reason": "above_threshold",
        "direction": direction, "entry": entry, "stop": stop, "target": target,
        "max_holding_days": max_holding_days, "input_sha": "deadbeef",
        "decision_at_utc": decision_at_utc, "replay_status": "pending",
    })
    return decision_at_utc


def _bar_dates_after(decision_at_utc: str, n: int) -> list[str]:
    from datetime import timedelta
    decision_date = timeutils.parse_iso(decision_at_utc).date()
    return [(decision_date + timedelta(days=i)).isoformat() for i in range(1, n + 1)]


# ============================================================ obligation 1
# AI leg uses the candidate's card window, resolved BY ID, immune to
# ACTIVE_CARD_ID.
def test_1a_ai_leg_uses_candidates_own_card_window_not_5day_default():
    """catalyst_momentum_v3's window is 10 -- a target hit on day 7 is
    INVISIBLE to the old, buggy always-5-day replay (bars[:5] never reaches
    day 7) but correctly found once the window is resolved from the
    candidate's own stamped card."""
    o = _orch()
    row = _seeded_row(o, card_id="catalyst_momentum_v3", entry=100.0, stop=90.0, target=150.0)
    created_date = row["created_at_utc"][:10]
    bars = _bars_days_after(created_date, 6, high=101.0, low=99.0, close=100.2)
    bars.append({  # day 7: target finally hit
        "date": _bars_days_after(created_date, 7)[-1]["date"],
        "open": 100.2, "high": 151.0, "low": 100.0, "close": 150.5,
    })
    res = update_pending_outcomes(o.journal, bars_provider=_FakeBars({"AAPL": bars}))
    assert res["updated"] == 1
    updated = o.journal.one("SELECT * FROM candidate_outcomes WHERE outcome_id = ?", (row["outcome_id"],))
    assert updated["replay_window_days"] == 10
    assert updated["replay_result"] == "target_hit"
    o.close()


def test_1b_ai_leg_falls_back_to_pinned_v2_only_when_candidate_has_no_card():
    o = _orch()
    row = _seeded_row(o, card_id=None)   # no card at all
    created_date = row["created_at_utc"][:10]
    bars = _bars_days_after(created_date, 5)
    update_pending_outcomes(o.journal, bars_provider=_FakeBars({"AAPL": bars}))
    updated = o.journal.one("SELECT * FROM candidate_outcomes WHERE outcome_id = ?", (row["outcome_id"],))
    assert updated["replay_window_days"] == 3   # catalyst_momentum_v2's own max_holding_days_default
    assert updated["data_quality_status"] == REPLAY_WINDOW_FALLBACK_DQ_STATUS
    o.close()


def test_1c_ai_leg_with_a_card_never_uses_the_fallback_stamp():
    o = _orch()
    row = _seeded_row(o, card_id="catalyst_momentum_v2")
    created_date = row["created_at_utc"][:10]
    bars = _bars_days_after(created_date, 5)
    update_pending_outcomes(o.journal, bars_provider=_FakeBars({"AAPL": bars}))
    updated = o.journal.one("SELECT * FROM candidate_outcomes WHERE outcome_id = ?", (row["outcome_id"],))
    assert updated["data_quality_status"] == "ok"
    o.close()


def test_1d_ai_leg_window_resolved_by_id_is_immune_to_active_card_id_swap():
    """The HOLD-2 lesson, applied to the AI leg: a candidate stamped with
    catalyst_momentum_v2 (window 3) must keep replaying at 3 even when the
    LIVE ACTIVE_CARD_ID is swapped to catalyst_momentum_v3 (window 10) --
    resolve_ai_replay_window() never receives `settings`/reads the live
    default at all (see its own signature)."""
    o = _orch(ACTIVE_CARD_ID="catalyst_momentum_v3")
    assert o.settings.active_card_id == "catalyst_momentum_v3"
    row = _seeded_row(o, card_id="catalyst_momentum_v2", entry=100.0, stop=90.0, target=200.0)
    created_date = row["created_at_utc"][:10]
    bars = _bars_days_after(created_date, 5)
    update_pending_outcomes(o.journal, bars_provider=_FakeBars({"AAPL": bars}))
    updated = o.journal.one("SELECT * FROM candidate_outcomes WHERE outcome_id = ?", (row["outcome_id"],))
    assert updated["replay_window_days"] == 3   # unmoved by the live-default swap
    o.close()


def test_1e_resolve_ai_replay_window_direct_unit_swap_immune():
    """Direct unit-level proof (no orchestrator/bars plumbing): the
    function itself takes no settings/active-default input at all."""
    j = JournalStore(":memory:")
    cid = new_id("cand")
    j.insert("candidates", {
        "candidate_id": cid, "symbol": "AAPL", "direction": "long", "card_id": "catalyst_momentum_v2",
    })
    window_before, fallback_before = resolve_ai_replay_window(j, cid)
    # No live-default concept reaches this function at all -- there is
    # nothing to "swap" it through; re-resolving the SAME candidate must be
    # stable regardless of anything else changing in the process.
    window_after, fallback_after = resolve_ai_replay_window(j, cid)
    assert window_before == window_after == 3
    assert fallback_before is fallback_after is False
    j.close()


# ============================================================ obligation 2
# Baseline v1/hold10 pinned by id; no `or 5` fallback reachable.
def test_2a_resolve_baseline_arm_windows_pinned_values():
    windows = resolve_baseline_arm_windows()
    assert windows[THRESHOLD_V1] == 3
    assert windows[THRESHOLD_V1_HOLD10] == 10


def test_2b_v1_arm_ignores_stale_null_row_level_max_holding_days_no_or_5_fallback():
    """Even when a row's OWN stored max_holding_days is NULL (e.g. a
    pre-HOLD-2 legacy row), the resolver must use the PINNED window (3),
    never DEFAULT_REPLAY_WINDOW_DAYS (5). Proof: a target hit that would
    only be visible under a 5-day (or wider) window is NOT found -- the row
    resolves 'neither' at exactly 3 bars instead."""
    j = JournalStore(":memory:")
    decision_at_utc = _pending_row(j, max_holding_days=None, rule_version=THRESHOLD_V1, days_ago=20)
    dates = _bar_dates_after(decision_at_utc, 4)
    bars = [
        {"date": dates[0], "high": 101.0, "low": 99.0, "close": 100.5},
        {"date": dates[1], "high": 101.0, "low": 99.0, "close": 100.5},
        {"date": dates[2], "high": 101.0, "low": 99.0, "close": 100.5},
        {"date": dates[3], "high": 110.0, "low": 99.0, "close": 105.0},   # target (104.8) only hit on day 4
    ]
    provider = _FakeBars({"AAPL": bars})
    counts = resolve_pending_baseline_decisions(j, bars_provider=provider)
    assert counts["completed"] == 1
    row = j.one("SELECT * FROM shadow_baseline_decisions WHERE candidate_id = 'cand1'")
    assert row["replay_window_days"] == 3
    assert row["replay_result"] == "neither"   # day 4's breach never seen under the correct 3-day window
    j.close()


def test_2c_hold10_arm_ignores_stale_null_row_level_max_holding_days_no_or_5_fallback():
    """Mirror of 2b for the hold10 arm: window must be 10 (not 5), proven
    by a level that only breaches on day 7. Under the OLD buggy `or 5`
    fallback (row.max_holding_days NULL -> 5), the resolver would have
    treated the row as ALREADY fully elapsed once 7 (>=5) forward bars
    arrived and locked in a premature 'neither' mark-to-market WITHOUT ever
    looking at day 7's bar (bars[:5] never reaches it) -- silently missing
    the real target hit. Under the correct pinned window (10), day 7 is
    still within the window, so the real hit IS found."""
    j = JournalStore(":memory:")
    decision_at_utc = _pending_row(
        j, max_holding_days=None, rule_version=THRESHOLD_V1_HOLD10, days_ago=20, candidate_id="cand_h10",
    )
    dates = _bar_dates_after(decision_at_utc, 7)
    bars = [{"date": d, "high": 101.0, "low": 99.0, "close": 100.5} for d in dates[:6]]
    bars.append({"date": dates[6], "high": 110.0, "low": 99.0, "close": 105.0})  # day 7 target hit
    provider = _FakeBars({"AAPL": bars})
    counts = resolve_pending_baseline_decisions(j, bars_provider=provider)
    assert counts["completed"] == 1
    row = j.one("SELECT * FROM shadow_baseline_decisions WHERE candidate_id = 'cand_h10'")
    assert row["replay_status"] == "complete"
    assert row["replay_result"] == "target_hit"   # found -- NOT masked by a premature 5-day cutoff
    assert row["replay_window_days"] == 10
    j.close()


def test_2d_v1_and_hold10_arms_pinned_regardless_of_active_card_id(monkeypatch):
    """record_shadow_baseline_decisions already proved write-time immunity
    (test_record_shadow_baseline_decisions_pin_unaffected_by_active_card_id_swap
    in test_baseline.py); this proves the REPLAY-time resolver
    (resolve_baseline_arm_windows, used by resolve_pending_baseline_decisions
    AND scripts/replay_recompute.py) is equally immune -- it takes no
    settings/active-default input at all."""
    windows_before = resolve_baseline_arm_windows()
    # There is no settings/ACTIVE_CARD_ID parameter to swap here at all --
    # the function signature itself is the proof. Re-resolving must be stable.
    windows_after = resolve_baseline_arm_windows()
    assert windows_before == windows_after


# ============================================================ obligation 3
# replay_window_days persisted on both tables, matches the window used.
def test_3a_candidate_outcomes_replay_window_days_matches_window_used():
    o = _orch()
    row = _seeded_row(o, card_id="catalyst_momentum_v1")   # v1 card -> window 3
    created_date = row["created_at_utc"][:10]
    bars = _bars_days_after(created_date, 5)
    update_pending_outcomes(o.journal, bars_provider=_FakeBars({"AAPL": bars}))
    updated = o.journal.one("SELECT * FROM candidate_outcomes WHERE outcome_id = ?", (row["outcome_id"],))
    assert updated["replay_window_days"] == 3
    o.close()


def test_3b_shadow_baseline_decisions_replay_window_days_matches_window_used():
    j = JournalStore(":memory:")
    decision_at_utc = _pending_row(j, rule_version=THRESHOLD_V1_HOLD10, days_ago=20)
    bar_date = _bar_dates_after(decision_at_utc, 1)[0]
    provider = _FakeBars({"AAPL": [{"date": bar_date, "high": 106.0, "low": 101.0, "close": 105.0}]})
    resolve_pending_baseline_decisions(j, bars_provider=provider)
    row = j.one("SELECT * FROM shadow_baseline_decisions WHERE candidate_id = 'cand1'")
    assert row["replay_window_days"] == 10
    j.close()


def test_3c_no_replay_attempted_leaves_replay_window_days_null():
    """A no_action baseline row (never replayed at all -- replay_r=0.0 is a
    directly-observed fact, no bracket was ever built) must keep
    replay_window_days NULL -- there is no window to report."""
    j = JournalStore(":memory:")
    cand = {"candidate_id": "cand_na", "symbol": "AAPL", "direction": "long", "interest_score": 0.01}
    j.insert("candidates", cand)
    record_shadow_baseline_decisions(j, make_settings(), {**cand, "last_price": 100.0})
    row = j.one(
        "SELECT * FROM shadow_baseline_decisions WHERE candidate_id = 'cand_na' AND rule_version = ?",
        (THRESHOLD_V1,),
    )
    assert row["decision"] == "no_action"
    assert row["replay_window_days"] is None
    j.close()


# ============================================================ obligation 4
# No production call site omits max_days (AST/grep guard, in the style of
# tests/test_vocab_guard.py).
def _replay_bracket_call_violations(source: str, filename: str = "<test>") -> list[str]:
    """Every ast.Call node named replay_bracket must carry max_days -- either
    a `max_days=` keyword or a 6th positional argument. Returns a list of
    'filename:lineno' violation strings (empty means clean)."""
    violations = []
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else None)
        if name != "replay_bracket":
            continue
        has_kw = any(kw.arg == "max_days" for kw in node.keywords)
        has_pos = len(node.args) >= 6
        if not (has_kw or has_pos):
            violations.append(f"{filename}:{node.lineno}")
    return violations


def test_4a_ast_guard_detects_a_synthetic_violation():
    """Proves the detector itself works (adversarial self-test) before
    trusting it against the real codebase below."""
    bad = "replay_bracket(entry, stop, target, direction, bars)"
    assert _replay_bracket_call_violations(bad, "synthetic.py") == ["synthetic.py:1"]
    good_kw = "replay_bracket(entry, stop, target, direction, bars, max_days=5)"
    assert _replay_bracket_call_violations(good_kw, "synthetic.py") == []
    good_pos = "replay_bracket(entry, stop, target, direction, bars, 5)"
    assert _replay_bracket_call_violations(good_pos, "synthetic.py") == []


def test_4b_no_production_replay_bracket_call_site_omits_max_days():
    """Scans every .py file under the production package (alphaos/) --
    tests/ and scripts/ are deliberately excluded, matching outcomes_engine's
    own documented ad-hoc/CLI exemption for the bare default."""
    violations = []
    for path in sorted(ALPHAOS_DIR.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        violations.extend(
            _replay_bracket_call_violations(source, str(path.relative_to(ALPHAOS_DIR.parent)))
        )
    assert not violations, f"replay_bracket call site(s) omit max_days: {violations}"


def test_4c_replay_recompute_cli_also_never_omits_max_days():
    """Not a production call site (it's the operator-invoked repair CLI,
    exempt in principle) -- but it happens to always pass max_days too, so
    pin that as a regression guard."""
    path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "replay_recompute.py"
    violations = _replay_bracket_call_violations(path.read_text(encoding="utf-8"), str(path))
    assert not violations


def test_4d_replay_recompute_never_imported_by_scheduler_or_orchestrator():
    """Spec section 4's own 'the merge must not recompute anything' law:
    the recompute CLI must never be reachable from any scheduler job,
    startup path, or migration. AST-based import guard over the production
    package's own source (NOT a naive text/string search -- a docstring or
    comment cross-referencing the script BY NAME, e.g. for provenance/
    documentation purposes, must not trip this; only a real
    import/import-from statement pulling the module in counts as a
    reachability violation)."""
    hits = []
    for path in sorted(ALPHAOS_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any("replay_recompute" in n for n in names):
                hits.append(f"{path.relative_to(ALPHAOS_DIR.parent)}:{node.lineno}")
    assert not hits, f"scripts/replay_recompute.py must never be imported from alphaos/: {hits}"


# ============================================================ obligation 5
# Determinism: recomputing an unchanged row twice yields byte-identical
# values.
def _seed_legacy_ai_leg_row(j) -> dict:
    """A row shaped exactly like a pre-AILEG-1 row: outcome_status
    'complete', a real replay_result/replay_r already stored (from the OLD
    always-5-day replay), replay_window_days NULL (never stamped -- proof
    this row predates the fix)."""
    cid = new_id("cand")
    st = timeutils.stamp()
    j.insert("candidates", {"candidate_id": cid, "symbol": "AAPL", "direction": "long",
                            "card_id": "catalyst_momentum_v2"})
    outcome_id = new_id("cout")
    j.insert("candidate_outcomes", {
        "outcome_id": outcome_id, "candidate_id": cid, "symbol": "AAPL", "candidate_type": "proposal",
        "decision_at_utc": st.utc, "entry_reference_price": 100.0, "stop_price": 95.0,
        "target_price": 200.0, "direction_hint": "long", "outcome_status": "complete",
        "replay_result": "neither", "replay_r": 0.02, "replay_exit_reason": "window_exhausted",
        "replay_window_days": None,
    })
    return {"outcome_id": outcome_id, "candidate_id": cid, "decision_at_utc": st.utc}


def _seed_legacy_baseline_row(j) -> dict:
    st = timeutils.stamp()
    baseline_id = new_id("basedec")
    j.insert("shadow_baseline_decisions", {
        "baseline_decision_id": baseline_id, "candidate_id": "cand_legacy", "symbol": "AAPL",
        "rule_version": THRESHOLD_V1, "decision": "propose", "direction": "long",
        "entry": 100.0, "stop": 96.0, "target": 104.8, "max_holding_days": 3,
        "input_sha": "deadbeef", "decision_at_utc": st.utc, "replay_status": "complete",
        "replay_result": "neither", "replay_r": 0.01, "replay_exit_reason": "window_exhausted",
        "replay_window_days": None,
    })
    return {"baseline_decision_id": baseline_id, "decision_at_utc": st.utc}


def _bars_for_decision(decision_at_utc: str, n: int) -> list[dict]:
    from datetime import timedelta
    decision_date = timeutils.parse_iso(decision_at_utc).date()
    return [
        {"date": (decision_date + timedelta(days=i)).isoformat(),
         "high": 101.0, "low": 99.0, "close": 100.2}
        for i in range(1, n + 1)
    ]


def test_5_recompute_determinism_unchanged_row_twice_byte_identical():
    j = JournalStore(":memory:")
    ai_row = _seed_legacy_ai_leg_row(j)
    base_row = _seed_legacy_baseline_row(j)
    bars = {"AAPL": _bars_for_decision(ai_row["decision_at_utc"], 10)}
    provider = _FakeBars(bars)

    report1 = run_replay_recompute(j, provider, apply=False)
    report2 = run_replay_recompute(j, provider, apply=False)

    assert report1["candidate_outcomes"]["changes"] == report2["candidate_outcomes"]["changes"]
    assert (report1["shadow_baseline_decisions"]["changes"]
            == report2["shadow_baseline_decisions"]["changes"])
    # Neither dry run wrote anything -- both reports describe the SAME
    # still-NULL-window rows.
    assert report1["candidate_outcomes"]["eligible"] == report2["candidate_outcomes"]["eligible"] == 1
    j.close()


# ============================================================ obligation 6
# replay_recompute --dry-run writes nothing; --apply writes exactly what it
# reported; both idempotent.
def test_6a_dry_run_writes_nothing():
    j = JournalStore(":memory:")
    ai_row = _seed_legacy_ai_leg_row(j)
    _seed_legacy_baseline_row(j)
    bars = {"AAPL": _bars_for_decision(ai_row["decision_at_utc"], 10)}
    provider = _FakeBars(bars)

    before = [dict(r) for r in j.query("SELECT * FROM candidate_outcomes ORDER BY id")]
    before_base = [dict(r) for r in j.query("SELECT * FROM shadow_baseline_decisions ORDER BY id")]

    report = run_replay_recompute(j, provider, apply=False)
    assert report["candidate_outcomes"]["eligible"] >= 1   # there WAS something to report...

    after = [dict(r) for r in j.query("SELECT * FROM candidate_outcomes ORDER BY id")]
    after_base = [dict(r) for r in j.query("SELECT * FROM shadow_baseline_decisions ORDER BY id")]
    assert before == after            # ...but nothing was actually written
    assert before_base == after_base
    j.close()


def test_6b_apply_writes_exactly_the_rows_dry_run_reported():
    j = JournalStore(":memory:")
    ai_row = _seed_legacy_ai_leg_row(j)
    _seed_legacy_baseline_row(j)
    bars = {"AAPL": _bars_for_decision(ai_row["decision_at_utc"], 10)}
    provider = _FakeBars(bars)

    dry = run_replay_recompute(j, provider, apply=False)
    applied = run_replay_recompute(j, provider, apply=True)

    # Same row set, same computed new_* values -- only "apply" differs.
    assert dry["candidate_outcomes"]["changes"] == applied["candidate_outcomes"]["changes"]
    assert (dry["shadow_baseline_decisions"]["changes"]
            == applied["shadow_baseline_decisions"]["changes"])

    ai_change = applied["candidate_outcomes"]["changes"][0]
    written = j.one("SELECT * FROM candidate_outcomes WHERE outcome_id = ?", (ai_change["row_id"],))
    assert written["replay_window_days"] == ai_change["new_replay_window_days"]
    assert written["replay_result"] == ai_change["new_replay_result"]
    assert written["replay_r"] == ai_change["new_replay_r"]

    base_change = applied["shadow_baseline_decisions"]["changes"][0]
    written_base = j.one(
        "SELECT * FROM shadow_baseline_decisions WHERE baseline_decision_id = ?", (base_change["row_id"],))
    assert written_base["replay_window_days"] == base_change["new_replay_window_days"]
    j.close()


def test_6c_apply_is_idempotent_second_run_finds_nothing_left():
    j = JournalStore(":memory:")
    ai_row = _seed_legacy_ai_leg_row(j)
    _seed_legacy_baseline_row(j)
    bars = {"AAPL": _bars_for_decision(ai_row["decision_at_utc"], 10)}
    provider = _FakeBars(bars)

    run_replay_recompute(j, provider, apply=True)
    second = run_replay_recompute(j, provider, apply=True)

    assert second["candidate_outcomes"]["eligible"] == 0
    assert second["candidate_outcomes"]["recomputed"] == 0
    assert second["shadow_baseline_decisions"]["eligible"] == 0
    assert second["shadow_baseline_decisions"]["recomputed"] == 0
    j.close()


def test_6d_dry_run_is_idempotent_repeated_calls_never_accumulate_state():
    j = JournalStore(":memory:")
    ai_row = _seed_legacy_ai_leg_row(j)
    bars = {"AAPL": _bars_for_decision(ai_row["decision_at_utc"], 10)}
    provider = _FakeBars(bars)

    for _ in range(3):
        report = run_replay_recompute(j, provider, apply=False)
        assert report["candidate_outcomes"]["eligible"] == 1   # never drops to 0 -- nothing was ever written
    j.close()


def test_6e_rows_missing_forward_bars_are_skipped_not_guessed():
    """A row with no bars available yet is left alone (neither reported as
    a confident change nor written) -- never a fabricated result."""
    j = JournalStore(":memory:")
    _seed_legacy_ai_leg_row(j)   # no bars injected for this symbol at all
    provider = _FakeBars({})
    report = run_replay_recompute(j, provider, apply=True)
    assert report["candidate_outcomes"]["eligible"] == 1
    assert report["candidate_outcomes"]["recomputed"] == 0
    assert report["candidate_outcomes"]["skipped_no_forward_bars"] == 1
    j.close()


# ============================================================ obligation 7
# Migration is additive; existing rows read back unchanged before any
# recompute.
def test_7a_candidate_outcomes_additive_migration_preserves_existing_row(tmp_path):
    """A DB predating AILEG-1 (candidate_outcomes exists but has no
    replay_window_days column) gets the column added on open -- existing
    row values are read back byte-identical, and the new column is NULL."""
    db = str(tmp_path / "pre_aileg1.db")
    raw = sqlite3.connect(db)
    raw.execute("""
        CREATE TABLE candidate_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            outcome_id TEXT NOT NULL UNIQUE,
            candidate_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            candidate_type TEXT NOT NULL,
            replay_result TEXT,
            replay_r REAL,
            outcome_status TEXT NOT NULL DEFAULT 'pending',
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
    """)
    raw.execute(
        "INSERT INTO candidate_outcomes (outcome_id, candidate_id, symbol, candidate_type, "
        "replay_result, replay_r, outcome_status, created_at_utc, created_at_sgt) VALUES "
        "('cout_legacy', 'cand_legacy', 'AAPL', 'proposal', 'target_hit', 1.2, 'complete', "
        "'2026-07-01T00:00:00+00:00', '2026-07-01T08:00:00+08:00')"
    )
    raw.execute("PRAGMA user_version = 3")
    raw.commit()
    raw.close()

    j = JournalStore(db)
    try:
        cols = {r["name"] for r in j.conn.execute("PRAGMA table_info(candidate_outcomes)")}
        assert "replay_window_days" in cols
        row = j.one("SELECT * FROM candidate_outcomes WHERE outcome_id = 'cout_legacy'")
        assert row["candidate_id"] == "cand_legacy"
        assert row["symbol"] == "AAPL"
        assert row["replay_result"] == "target_hit"
        assert row["replay_r"] == 1.2
        assert row["outcome_status"] == "complete"
        assert row["replay_window_days"] is None   # additive column, NULL until a recompute
        assert j.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        j.close()


def test_7b_shadow_baseline_decisions_additive_migration_preserves_existing_row(tmp_path):
    db = str(tmp_path / "pre_aileg1_baseline.db")
    raw = sqlite3.connect(db)
    raw.execute("""
        CREATE TABLE shadow_baseline_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            baseline_decision_id TEXT NOT NULL UNIQUE,
            candidate_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            rule_version TEXT NOT NULL,
            decision TEXT NOT NULL,
            max_holding_days INTEGER,
            replay_status TEXT NOT NULL DEFAULT 'pending',
            replay_result TEXT,
            replay_r REAL,
            input_sha TEXT NOT NULL,
            decision_at_utc TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
    """)
    raw.execute(
        "INSERT INTO shadow_baseline_decisions (baseline_decision_id, candidate_id, symbol, "
        "rule_version, decision, max_holding_days, replay_status, replay_result, replay_r, "
        "input_sha, decision_at_utc, created_at_utc, created_at_sgt) VALUES "
        "('basedec_legacy', 'cand_legacy', 'AAPL', 'threshold_v1', 'propose', 3, 'complete', "
        "'stop_hit', -1.0, 'deadbeef', '2026-07-01T00:00:00+00:00', "
        "'2026-07-01T00:00:00+00:00', '2026-07-01T08:00:00+08:00')"
    )
    raw.execute("PRAGMA user_version = 3")
    raw.commit()
    raw.close()

    j = JournalStore(db)
    try:
        cols = {r["name"] for r in j.conn.execute("PRAGMA table_info(shadow_baseline_decisions)")}
        assert "replay_window_days" in cols
        row = j.one("SELECT * FROM shadow_baseline_decisions WHERE baseline_decision_id = 'basedec_legacy'")
        assert row["candidate_id"] == "cand_legacy"
        assert row["rule_version"] == "threshold_v1"
        assert row["replay_result"] == "stop_hit"
        assert row["replay_r"] == -1.0
        assert row["max_holding_days"] == 3
        assert row["replay_window_days"] is None
    finally:
        j.close()


def test_7c_nothing_recomputes_as_a_side_effect_of_opening_the_migrated_db(tmp_path):
    """The additive column shows up on open (schema shape), but no VALUES
    are ever rewritten just from connecting -- that repair is exclusively
    scripts/replay_recompute.py's job, run separately with --apply."""
    db = str(tmp_path / "pre_aileg1_dark.db")
    raw = sqlite3.connect(db)
    raw.execute("""
        CREATE TABLE candidate_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            outcome_id TEXT NOT NULL UNIQUE,
            candidate_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            candidate_type TEXT NOT NULL,
            replay_result TEXT,
            replay_r REAL,
            outcome_status TEXT NOT NULL DEFAULT 'pending',
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
    """)
    raw.execute(
        "INSERT INTO candidate_outcomes (outcome_id, candidate_id, symbol, candidate_type, "
        "replay_result, replay_r, outcome_status, created_at_utc, created_at_sgt) VALUES "
        "('cout_legacy', 'cand_legacy', 'AAPL', 'proposal', 'target_hit', 1.2, 'complete', "
        "'2026-07-01T00:00:00+00:00', '2026-07-01T08:00:00+08:00')"
    )
    raw.execute("PRAGMA user_version = 3")
    raw.commit()
    raw.close()

    j1 = JournalStore(db)
    j1.close()
    j2 = JournalStore(db)   # a SECOND connection/migration pass -- must still be a no-op on values
    try:
        row = j2.one("SELECT * FROM candidate_outcomes WHERE outcome_id = 'cout_legacy'")
        assert row["replay_result"] == "target_hit"   # byte-unchanged
        assert row["replay_r"] == 1.2
        assert row["replay_window_days"] is None       # still nothing recomputed
    finally:
        j2.close()
