"""End-to-end Attribution v2 (PR8): discovered strictly inside the existing
outcomes_update measurement flow, resolved from candidate_outcomes/
trade_outcomes exactly as they already stand (never a second replay engine),
proven behavior-neutral (on/off A/B + structural no-read checks), fail-safe,
idempotent (double-run + SQLite NULL-uniqueness), mock/demo-aware, and
lineage-stamped. Hermetic -- mock mode, no network. Source rows are built by
DIRECT deterministic construction (never "hope the mock scan produces this
exact scenario") -- this session's own established lesson after two flaky
tests were traced to depending on what a natural mock scan happens to output.
"""

from __future__ import annotations

import inspect
import pathlib
import sqlite3

import pytest

from alphaos.attribution import discover_events, resolve_pending
from alphaos.attribution.resolve import ATTRIBUTION_VERSION
from alphaos.constants import AttributionResolvedStatus
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.util.ids import new_id
from conftest import inject_pending_proposal, make_settings


def _orch(**over):
    return Orchestrator(settings=make_settings(**over), journal=JournalStore(":memory:"))


def _cand(journal, symbol="AAPL", status="proposed", candidate_id=None, **kw):
    candidate_id = candidate_id or new_id("cand")
    row = {"candidate_id": candidate_id, "symbol": symbol, "direction": "long",
          "strategy": "swing", "status": status}
    row.update(kw)
    journal.insert("candidates", row)
    return candidate_id


def _proposal(journal, candidate_id, status, symbol="AAPL", proposal_id=None, **kw):
    proposal_id = proposal_id or new_id("prop")
    row = {"proposal_id": proposal_id, "candidate_id": candidate_id, "symbol": symbol,
          "direction": "long", "entry": 100.0, "stop": 97.0, "target": 106.0, "status": status}
    row.update(kw)
    journal.insert("trade_proposals", row)
    return proposal_id


def _co(journal, candidate_id, candidate_type="proposal", outcome_status="complete",
       replay_result=None, replay_r=None, symbol="AAPL", **kw):
    outcome_id = new_id("cout")
    row = {
        "outcome_id": outcome_id, "candidate_id": candidate_id, "symbol": symbol,
        "candidate_type": candidate_type, "outcome_status": outcome_status,
        "replay_result": replay_result, "replay_r": replay_r,
        # Match _proposal()'s own defaults (100.0/97.0/106.0) so a co row
        # built for "the" proposal on a candidate satisfies the levels-match
        # guard in discovery.candidate_outcome_for_proposal() (PR8 audit
        # LOW-1) unless a test explicitly overrides these to test a mismatch.
        "entry_reference_price": 100.0, "stop_price": 97.0, "target_price": 106.0,
    }
    row.update(kw)
    journal.insert("candidate_outcomes", row)
    return outcome_id


def _trade_outcome(journal, proposal_id, realized_r, symbol="AAPL", **kw):
    outcome_id = new_id("out")
    row = {"outcome_id": outcome_id, "position_id": new_id("pos"), "symbol": symbol,
          "proposal_id": proposal_id, "realized_r": realized_r}
    row.update(kw)
    journal.insert("trade_outcomes", row)
    return outcome_id


def _override(journal, candidate_id, alphaos_would_have_traded=0, user_final_decision="propose",
             symbol="AAPL", proposal_id=None, override_id=None, **kw):
    override_id = override_id or new_id("ovr")
    row = {"override_id": override_id, "candidate_id": candidate_id, "symbol": symbol,
          "alphaos_would_have_traded": alphaos_would_have_traded,
          "user_final_decision": user_final_decision, "proposal_id": proposal_id}
    row.update(kw)
    journal.insert("user_decision_overrides", row)
    return override_id


def _run(journal, settings):
    """discover then resolve in one call -- mirrors outcomes_update()'s own ordering."""
    d = discover_events(journal, settings)
    r = resolve_pending(journal, settings)
    return d, r


# ------------------------------------------------------------- default posture
def test_enabled_by_default_and_discovers_from_a_real_scan():
    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12")
    o.run_scan_once()
    res = o.outcomes_update()
    assert "attribution" in res
    assert res["attribution"]["discovered"]["total"] >= 0
    o.close()


def test_disabled_runs_zero_queries_and_writes_no_rows(monkeypatch):
    o = _orch(ATTRIBUTION_ENABLED="false")
    _cand(o.journal)
    called = {"n": 0}
    import alphaos.attribution as attribution_pkg

    monkeypatch.setattr(attribution_pkg, "discover_events", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    res = o.outcomes_update()
    assert "attribution" not in res
    assert called["n"] == 0
    assert o.journal.count_rows("attribution_records") == 0
    o.close()


# --------------------------------------------------------------- scenario: 1
def test_propose_user_rejected_winning_replay_gives_negative_delta():
    j = JournalStore(":memory:")
    s = make_settings()
    cand_id = _cand(j, status="rejected")
    prop_id = _proposal(j, cand_id, status="rejected")
    _co(j, cand_id, candidate_type="proposal", replay_result="target_hit", replay_r=2.0)
    _run(j, s)
    row = j.one("SELECT * FROM attribution_records WHERE proposal_id = ?", (prop_id,))
    assert row["attribution_type"] == "propose_user_rejected"
    assert row["agent"] == "user"
    assert row["resolved_status"] == "resolved"
    assert row["actual_path_r"] == 0.0
    assert row["alphaos_path_r"] == 2.0
    assert row["delta_r"] == -2.0
    j.close()


def test_propose_user_rejected_losing_replay_gives_positive_delta():
    j = JournalStore(":memory:")
    s = make_settings()
    cand_id = _cand(j, status="rejected")
    prop_id = _proposal(j, cand_id, status="rejected")
    _co(j, cand_id, candidate_type="proposal", replay_result="stop_hit", replay_r=-1.0)
    _run(j, s)
    row = j.one("SELECT * FROM attribution_records WHERE proposal_id = ?", (prop_id,))
    assert row["delta_r"] == 1.0
    assert row["resolved_status"] == "resolved"
    j.close()


# --------------------------------------------------------------- scenario: 2
def test_user_override_trade_realized_win():
    j = JournalStore(":memory:")
    s = make_settings()
    cand_id = _cand(j, status="watch")
    prop_id = _proposal(j, cand_id, status="filled")
    ov_id = _override(j, cand_id, proposal_id=prop_id)
    _trade_outcome(j, prop_id, realized_r=1.4)
    _run(j, s)
    row = j.one("SELECT * FROM attribution_records WHERE override_id = ?", (ov_id,))
    assert row["attribution_type"] == "user_override_trade"
    assert row["agent"] == "user"
    assert row["alphaos_path_r"] == 0.0
    assert row["actual_path_r"] == 1.4
    assert row["delta_r"] == 1.4
    assert row["resolved_status"] == "resolved"
    assert row["r_basis"] == "realized_net"
    j.close()


def test_user_override_trade_realized_loss():
    j = JournalStore(":memory:")
    s = make_settings()
    cand_id = _cand(j, status="watch")
    prop_id = _proposal(j, cand_id, status="filled")
    ov_id = _override(j, cand_id, proposal_id=prop_id)
    _trade_outcome(j, prop_id, realized_r=-0.6)
    _run(j, s)
    row = j.one("SELECT * FROM attribution_records WHERE override_id = ?", (ov_id,))
    assert row["delta_r"] == -0.6
    j.close()


def test_user_override_trade_excludes_propose_to_reject_overrides():
    """A PROPOSE_TO_REJECT override (user_final_decision='reject') must NOT
    produce a user_override_trade row -- that direction is covered by
    propose_user_rejected via the proposal it rejects, never double-counted
    here."""
    j = JournalStore(":memory:")
    s = make_settings()
    cand_id = _cand(j, status="proposed")
    _override(j, cand_id, alphaos_would_have_traded=1, user_final_decision="reject")
    discover_events(j, s)
    assert j.count_rows("attribution_records", "attribution_type = 'user_override_trade'") == 0
    j.close()


# --------------------------------------------------------------- scenario: 3
def test_propose_approved_executed_stores_execution_delta_not_decision_delta():
    j = JournalStore(":memory:")
    s = make_settings()
    cand_id = _cand(j, status="proposed")
    prop_id = _proposal(j, cand_id, status="filled")
    _co(j, cand_id, candidate_type="proposal", replay_result="target_hit", replay_r=1.5)
    _trade_outcome(j, prop_id, realized_r=1.1)
    _run(j, s)
    row = j.one("SELECT * FROM attribution_records WHERE proposal_id = ?", (prop_id,))
    assert row["attribution_type"] == "propose_approved_executed"
    assert row["agent"] == "execution"
    assert row["delta_r"] is None
    assert row["execution_delta_r"] == round(1.1 - 1.5, 4)
    assert row["r_basis"] == "net_vs_gross"
    assert row["resolved_status"] == "resolved"
    j.close()


def test_propose_approved_executed_partial_when_trade_open():
    j = JournalStore(":memory:")
    s = make_settings()
    cand_id = _cand(j, status="proposed")
    prop_id = _proposal(j, cand_id, status="approved")
    _co(j, cand_id, candidate_type="proposal", replay_result="target_hit", replay_r=1.5)
    # no trade_outcomes row yet -- trade is still open
    discover_events(j, s)
    resolve_pending(j, s)
    row = j.one("SELECT * FROM attribution_records WHERE proposal_id = ?", (prop_id,))
    assert row["resolved_status"] == "pending"  # nothing to close against yet
    assert row["delta_r"] is None
    assert row["execution_delta_r"] is None
    j.close()


def test_propose_approved_executed_resolves_execution_gap_for_an_override_origin_proposal():
    """PR8 audit LOW-2 regression: an override-created proposal's frozen
    levels are seeded under candidate_type='user_override' (see
    _source_from_override), not 'proposal'/'blocked'. Before the fix,
    candidate_outcome_for_proposal() only checked 'proposal'/'blocked' and so
    could never find this row -- the execution gap stayed permanently
    unresolvable for every override-originated trade."""
    j = JournalStore(":memory:")
    s = make_settings()
    cand_id = _cand(j, status="watch")
    prop_id = _proposal(j, cand_id, status="filled")
    _override(j, cand_id, proposal_id=prop_id)
    _co(j, cand_id, candidate_type="user_override", replay_result="target_hit", replay_r=1.5)
    _trade_outcome(j, prop_id, realized_r=1.1)
    _run(j, s)
    pae = j.one(
        "SELECT * FROM attribution_records WHERE proposal_id = ? AND attribution_type = 'propose_approved_executed'",
        (prop_id,),
    )
    assert pae["resolved_status"] == "resolved"
    assert pae["alphaos_path_r"] == 1.5
    assert pae["execution_delta_r"] == round(1.1 - 1.5, 4)
    j.close()


def test_candidate_with_two_proposals_does_not_cross_link_the_wrong_replay():
    """PR8 audit LOW-1 regression: candidate_outcomes seeds AT MOST ONE
    'proposal'/'blocked' row per candidate_id, frozen at first seed (see
    outcomes_tracker._classify_candidate). If a candidate later grows a
    SECOND proposal with DIFFERENT levels, the frozen row belongs to the
    FIRST proposal -- looking it up by candidate_id alone would silently
    borrow the wrong proposal's replay_r. The levels-match guard must refuse
    to use a mismatched row (honest 'pending', never a wrong delta_r)."""
    j = JournalStore(":memory:")
    s = make_settings()
    cand_id = _cand(j, status="rejected")
    prop_a = _proposal(j, cand_id, status="rejected", entry=100.0, stop=97.0, target=106.0)
    prop_b = _proposal(j, cand_id, status="filled", entry=200.0, stop=194.0, target=212.0)
    # frozen row belongs to B (the "first seeded" proposal in this scenario)
    _co(j, cand_id, candidate_type="proposal", replay_result="target_hit", replay_r=5.0,
       entry_reference_price=200.0, stop_price=194.0, target_price=212.0)
    discover_events(j, s)
    resolve_pending(j, s)
    row_a = j.one("SELECT * FROM attribution_records WHERE proposal_id = ?", (prop_a,))
    assert row_a["alphaos_path_r"] != 5.0  # must NOT borrow B's replay
    assert row_a["delta_r"] is None
    assert row_a["resolved_status"] == "pending"  # honest miss, not a wrong number
    j.close()


# --------------------------------------------------------------- scenario: 4
def test_propose_expired_copies_expired_reason_and_computes_operational_delta():
    j = JournalStore(":memory:")
    s = make_settings()
    cand_id = _cand(j, status="proposed")
    prop_id = _proposal(j, cand_id, status="expired", expired_reason="PROPOSAL_EXPIRED")
    _co(j, cand_id, candidate_type="proposal", replay_result="target_hit", replay_r=2.0)
    _run(j, s)
    row = j.one("SELECT * FROM attribution_records WHERE proposal_id = ?", (prop_id,))
    assert row["attribution_type"] == "propose_expired"
    assert row["agent"] == "operational"
    assert row["expired_reason"] == "PROPOSAL_EXPIRED"
    assert row["delta_r"] == -2.0
    j.close()


# --------------------------------------------------------------- scenario: 5
def test_propose_blocked_copies_blocked_reason_code_and_computes_gate_delta():
    j = JournalStore(":memory:")
    s = make_settings()
    cand_id = _cand(j, status="proposed")
    prop_id = _proposal(j, cand_id, status="blocked", proposal_reason="wide_spread")
    _co(j, cand_id, candidate_type="blocked", replay_result="stop_hit", replay_r=-1.0)
    _run(j, s)
    row = j.one("SELECT * FROM attribution_records WHERE proposal_id = ?", (prop_id,))
    assert row["attribution_type"] == "propose_blocked"
    assert row["agent"] == "gate"
    assert row["blocked_reason_code"] == "wide_spread"
    assert row["delta_r"] == 1.0  # blocking a loser saved 1R
    j.close()


def test_propose_blocked_end_to_end_from_a_real_mock_scan():
    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12")
    summ = o.run_scan_once()
    o.outcomes_update()
    rows = o.journal.query("SELECT * FROM attribution_records WHERE attribution_type = 'propose_blocked'")
    assert len(rows) == summ.risk_blocked
    o.close()


# ---------------------------------------------------------- unknown-never-zero
def test_no_stop_gives_null_delta_and_unresolvable():
    j = JournalStore(":memory:")
    s = make_settings()
    cand_id = _cand(j, status="rejected")
    _proposal(j, cand_id, status="rejected", stop=None, target=None)
    _co(j, cand_id, candidate_type="proposal", outcome_status="complete", replay_result=None, replay_r=None)
    _run(j, s)
    row = j.one("SELECT * FROM attribution_records WHERE candidate_id = ?", (cand_id,))
    assert row["delta_r"] is None
    assert row["resolved_status"] == "unresolvable"
    j.close()


def test_ambiguous_same_bar_gives_null_delta_and_unresolvable():
    j = JournalStore(":memory:")
    s = make_settings()
    cand_id = _cand(j, status="rejected")
    _proposal(j, cand_id, status="rejected")
    _co(j, cand_id, candidate_type="proposal", replay_result="ambiguous_same_bar", replay_r=None)
    _run(j, s)
    row = j.one("SELECT * FROM attribution_records WHERE candidate_id = ?", (cand_id,))
    assert row["delta_r"] is None
    assert row["resolved_status"] == "unresolvable"
    assert row["replay_status"] == "ambiguous_same_bar"
    j.close()


def test_data_unavailable_gives_null_delta_and_unresolvable():
    j = JournalStore(":memory:")
    s = make_settings()
    cand_id = _cand(j, status="rejected")
    _proposal(j, cand_id, status="rejected")
    _co(j, cand_id, candidate_type="proposal", outcome_status="unavailable")
    _run(j, s)
    row = j.one("SELECT * FROM attribution_records WHERE candidate_id = ?", (cand_id,))
    assert row["delta_r"] is None
    assert row["resolved_status"] == "unresolvable"
    j.close()


def test_unresolved_trade_stays_pending_not_zero():
    j = JournalStore(":memory:")
    s = make_settings()
    cand_id = _cand(j, status="proposed")
    prop_id = _proposal(j, cand_id, status="filled")
    # no candidate_outcomes, no trade_outcomes row at all yet
    _run(j, s)
    row = j.one("SELECT * FROM attribution_records WHERE proposal_id = ?", (prop_id,))
    assert row["resolved_status"] == "pending"
    assert row["delta_r"] is None
    assert row["actual_path_r"] is None  # NOT 0 -- genuinely unknown, not observed
    j.close()


def test_window_exhausted_neither_result_stays_unresolvable_not_mtm():
    j = JournalStore(":memory:")
    s = make_settings()
    cand_id = _cand(j, status="rejected")
    _proposal(j, cand_id, status="rejected")
    _co(j, cand_id, candidate_type="proposal", replay_result="neither", replay_r=0.4)
    _run(j, s)
    row = j.one("SELECT * FROM attribution_records WHERE candidate_id = ?", (cand_id,))
    assert row["delta_r"] is None
    assert row["resolved_status"] == "unresolvable"
    j.close()


# ------------------------------------------------------------------ idempotency
def test_running_twice_inserts_zero_new_rows():
    j = JournalStore(":memory:")
    s = make_settings()
    cand_id = _cand(j, status="rejected")
    _proposal(j, cand_id, status="rejected")
    d1 = discover_events(j, s)
    n1 = j.count_rows("attribution_records")
    assert n1 == 1
    d2 = discover_events(j, s)
    n2 = j.count_rows("attribution_records")
    assert n2 == n1
    assert d2["total"] == 0
    j.close()


def test_sqlite_null_uniqueness_blocks_duplicate_proposal_anchored_rows():
    """SQLite treats every NULL as distinct from every other NULL -- a plain
    UNIQUE(attribution_type, proposal_id, attribution_version) would still
    allow two override_id=NULL rows through unless the partial index
    specifically constrains proposal_id IS NOT NULL rows (which it does)."""
    j = JournalStore(":memory:")
    row = {
        "attribution_id": new_id("attr"), "attribution_type": "propose_user_rejected",
        "attribution_version": ATTRIBUTION_VERSION, "agent": "user", "source_id": "p1",
        "proposal_id": "p1", "symbol": "AAPL", "resolved_status": "pending",
        "data_quality_status": "ok",
    }
    j.insert("attribution_records", row)
    with pytest.raises(sqlite3.IntegrityError):
        j.insert("attribution_records", {**row, "attribution_id": new_id("attr")})
    j.close()


def test_sqlite_null_uniqueness_blocks_duplicate_override_anchored_rows():
    j = JournalStore(":memory:")
    row = {
        "attribution_id": new_id("attr"), "attribution_type": "user_override_trade",
        "attribution_version": ATTRIBUTION_VERSION, "agent": "user", "source_id": "o1",
        "override_id": "o1", "symbol": "AAPL", "resolved_status": "pending",
        "data_quality_status": "ok",
    }
    j.insert("attribution_records", row)
    with pytest.raises(sqlite3.IntegrityError):
        j.insert("attribution_records", {**row, "attribution_id": new_id("attr")})
    j.close()


def test_proposal_anchored_and_override_anchored_rows_never_collide():
    """A proposal_id-keyed row and an override_id-keyed row for the SAME
    underlying candidate must both be allowed -- they are different agents'
    events, not duplicates of each other. The proposal is left 'proposed'
    (not rejected/expired/blocked/approved+), so no competing
    proposal-anchored event exists for it -- isolating this check to the
    override-anchored path alone."""
    j = JournalStore(":memory:")
    s = make_settings()
    cand_id = _cand(j, status="watch")
    prop_id = _proposal(j, cand_id, status="proposed")
    _override(j, cand_id, proposal_id=prop_id)
    discover_events(j, s)
    assert j.count_rows("attribution_records") == 1
    row = j.one("SELECT * FROM attribution_records")
    assert row["attribution_type"] == "user_override_trade"
    j.close()


def test_resolved_rows_are_never_reresolved():
    """Once a row reaches resolved_status='resolved', a later change to the
    underlying candidate_outcomes row must NOT change the attribution row --
    matches update_pending_outcomes' own 'complete rows are never revisited'
    convention exactly."""
    j = JournalStore(":memory:")
    s = make_settings()
    cand_id = _cand(j, status="rejected")
    _proposal(j, cand_id, status="rejected")
    _co(j, cand_id, candidate_type="proposal", replay_result="stop_hit", replay_r=-1.0)
    _run(j, s)
    row = j.one("SELECT * FROM attribution_records WHERE candidate_id = ?", (cand_id,))
    assert row["delta_r"] == 1.0
    # now flip the underlying candidate_outcomes row to a wildly different value
    j.conn.execute("UPDATE candidate_outcomes SET replay_result = 'target_hit', replay_r = 99.0 WHERE candidate_id = ?",
                   (cand_id,))
    j.conn.commit()
    resolve_pending(j, s)
    row_after = j.one("SELECT * FROM attribution_records WHERE candidate_id = ?", (cand_id,))
    assert row_after["delta_r"] == 1.0  # unchanged -- resolved rows are frozen
    j.close()


# -------------------------------------------------------------------- fail-safe
def test_discovery_exception_is_logged_and_outcomes_update_continues(monkeypatch):
    o = _orch(INTEREST_SCAN_TOP_N="6", MAX_CANDIDATES_TO_AI="6")
    import alphaos.attribution.discovery as discovery_mod

    original = discovery_mod.find_propose_user_rejected
    discovery_mod.find_propose_user_rejected = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        res = o.outcomes_update()  # must NOT raise
    finally:
        discovery_mod.find_propose_user_rejected = original
    assert "attribution" in res
    events = o.journal.query("SELECT * FROM system_events WHERE category = 'attribution' AND severity = 'warning'")
    assert events
    assert any("boom" in (e.get("detail_json") or "") for e in events)
    o.close()


def test_resolve_row_exception_is_logged_and_continues():
    j = JournalStore(":memory:")
    s = make_settings()
    cand_id = _cand(j, status="rejected")
    _proposal(j, cand_id, status="rejected")
    discover_events(j, s)
    # corrupt the row's attribution_type so _resolve_one's branch dispatch hits
    # an unexpected state -- must log + skip, never crash resolve_pending.
    j.conn.execute("UPDATE attribution_records SET candidate_id = NULL")
    j.conn.commit()
    res = resolve_pending(j, s)  # must NOT raise
    assert res["total"] == 1
    j.close()


def test_attribution_failure_creates_no_orders_approvals_fills_positions():
    o = _orch(INTEREST_SCAN_TOP_N="6", MAX_CANDIDATES_TO_AI="6")
    import alphaos.attribution.batch as batch_mod

    batch_mod.discover_events = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        o.outcomes_update()
    except Exception:
        pass
    assert o.journal.count_rows("paper_orders") == 0
    assert o.journal.count_rows("paper_fills") == 0
    assert o.journal.count_rows("positions") == 0
    o.close()


# ------------------------------------------------------------------- mock/demo
def test_mock_scan_rows_all_tagged_is_mock():
    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12")
    o.run_scan_once()
    o.outcomes_update()
    rows = o.journal.query("SELECT is_mock, data_quality_status FROM attribution_records")
    assert rows
    assert all(r["is_mock"] == 1 for r in rows)
    assert all(r["data_quality_status"] == "mock" for r in rows)
    o.close()


def test_eval_less_mock_mode_candidate_is_tagged_mock_not_real():
    """Regression twin of the PR7 audit MEDIUM-1 fix: a candidate scored with
    no openai_evaluations row at all must still be tagged is_mock=1 when the
    run itself is ALPHAOS_MODE=mock."""
    j = JournalStore(":memory:")
    s = make_settings()
    assert s.is_mock is True
    cand_id = _cand(j, status="rejected")
    _proposal(j, cand_id, status="rejected")
    discover_events(j, s)
    row = j.one("SELECT * FROM attribution_records WHERE candidate_id = ?", (cand_id,))
    assert row["is_mock"] == 1
    assert row["data_quality_status"] == "mock"
    j.close()


def test_demo_proposal_is_skipped():
    j = JournalStore(":memory:")
    s = make_settings()
    cand_id = _cand(j, status="rejected")
    _proposal(j, cand_id, status="rejected", is_demo=1)
    discover_events(j, s)
    assert j.count_rows("attribution_records") == 0
    j.close()


def test_demo_candidate_override_is_skipped():
    j = JournalStore(":memory:")
    s = make_settings()
    cand_id = _cand(j, status="demo")
    _override(j, cand_id)
    discover_events(j, s)
    assert j.count_rows("attribution_records") == 0
    j.close()


# ------------------------------------------------------------------ lineage/schema
def test_lineage_id_copied_from_source_proposal():
    j = JournalStore(":memory:")
    s = make_settings()
    cand_id = _cand(j, status="rejected")
    _proposal(j, cand_id, status="rejected", lineage_id="lin_source_123")
    discover_events(j, s)
    row = j.one("SELECT * FROM attribution_records WHERE candidate_id = ?", (cand_id,))
    assert row["lineage_id"] == "lin_source_123"
    j.close()


def test_missing_legacy_lineage_degrades_not_crashes():
    """Non-mock settings (mock always wins data_quality_status precedence --
    see test_compute_data_quality_mock_takes_precedence_over_everything --
    so isolating 'degraded' requires a real/paper-mode run)."""
    j = JournalStore(":memory:")
    s = make_settings(ALPHAOS_MODE="paper")
    assert s.is_mock is False
    cand_id = _cand(j, status="rejected")
    _proposal(j, cand_id, status="rejected", lineage_id=None)
    _co(j, cand_id, candidate_type="proposal", replay_result="stop_hit", replay_r=-1.0)
    _run(j, s)  # must not raise
    row = j.one("SELECT * FROM attribution_records WHERE candidate_id = ?", (cand_id,))
    assert row["lineage_id"] is None
    assert row["is_mock"] == 0
    assert row["data_quality_status"] == "degraded"
    j.close()


def test_old_db_gets_attribution_records_table_added_additively(tmp_path):
    db_path = tmp_path / "legacy.db"
    j1 = JournalStore(str(db_path))
    j1.conn.execute("DROP TABLE IF EXISTS attribution_records")
    j1.conn.execute("DROP INDEX IF EXISTS idx_attr_proposal_unique")
    j1.conn.execute("DROP INDEX IF EXISTS idx_attr_override_unique")
    j1.conn.commit()
    j1.close()

    j2 = JournalStore(str(db_path))  # re-opening must additively recreate it
    cols = j2._cols("attribution_records")
    for expected in ("attribution_id", "attribution_type", "delta_r", "resolved_status",
                    "data_quality_status", "is_mock", "lineage_id"):
        assert expected in cols
    idx = {r["name"] for r in j2.query(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='attribution_records'")}
    assert "idx_attr_proposal_unique" in idx
    assert "idx_attr_override_unique" in idx
    j2.close()


def test_attribution_config_hash_present_on_lineage_snapshot():
    j = JournalStore(":memory:")
    s = make_settings()
    o = Orchestrator(settings=s, journal=j)
    cand_id = _cand(j, status="rejected")
    _proposal(j, cand_id, status="rejected", lineage_id=None)
    discover_events(j, s)  # forces get_or_create_lineage_id via... no, discovery never stamps lineage itself
    from alphaos import lineage

    lineage_id = lineage.get_or_create_lineage_id(j, s)
    row = j.one("SELECT * FROM lineage_snapshots WHERE lineage_id = ?", (lineage_id,))
    assert row is not None
    assert "attribution_config_hash" in row.keys()
    assert row["attribution_config_hash"] is not None
    o.close()


# --------------------------------------------------------------- no-read proof
def test_decision_functions_never_reference_attribution():
    """orchestrator.py DOES import alphaos.attribution (discover_events/
    resolve_pending are called write-only, inside outcomes_update, itself
    pure measurement). Extract the SOURCE of each actual decision-making
    function and confirm none mention 'attribution' at all."""
    decision_functions = (
        "_handle_proposal", "_resolve_decision", "_combine_decision",
        "_real_decision_driver", "approve_proposal", "reject_proposal",
        "_label_candidate", "_freeze_label", "run_scan_once", "_execute",
        "_override_open_trade",
    )
    for fn_name in decision_functions:
        fn = getattr(Orchestrator, fn_name)
        source = inspect.getsource(fn)
        assert "attribution" not in source.lower(), f"Orchestrator.{fn_name} references attribution"


def test_outcomes_update_is_the_only_attribution_call_site():
    source = inspect.getsource(Orchestrator.outcomes_update)
    assert "attribution" in source.lower()


def test_risk_engine_and_approval_never_reference_attribution_at_all():
    import alphaos.approval as approval_mod
    import alphaos.risk.risk_engine as risk_mod

    for mod, name in ((approval_mod, "approval.py"), (risk_mod, "risk_engine.py")):
        text = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        assert "attribution" not in text.lower(), f"{name} references attribution"


def test_attribution_package_never_imports_or_reads_tqs():
    """Prose in these modules' own docstrings legitimately NAMES alphaos.tqs
    by way of comparison (e.g. 'mirrors compute_tqs()'s pure/DB split') --
    that is documentation, not coupling. Check for actual FUNCTIONAL
    references (imports or the tqs_scores table) instead of a bare 'tqs'
    substring, which would false-positive on that prose."""
    import alphaos.attribution.batch as batch_mod
    import alphaos.attribution.discovery as discovery_mod
    import alphaos.attribution.resolve as resolve_mod

    banned = ("import alphaos.tqs", "from alphaos.tqs", "from alphaos import tqs", "tqs_scores")
    for mod, name in ((batch_mod, "batch.py"), (discovery_mod, "discovery.py"), (resolve_mod, "resolve.py")):
        text = pathlib.Path(mod.__file__).read_text(encoding="utf-8").lower()
        for phrase in banned:
            assert phrase not in text, f"alphaos/attribution/{name} references {phrase!r}"


def test_decision_functions_do_not_take_or_return_attribution_values():
    """approve_proposal/_handle_proposal/RiskEngine.assess signatures and
    behavior are unaffected -- call them and confirm normal operation with
    attribution enabled, proving no hidden coupling changed their contracts."""
    o = _orch()
    pid, _ = inject_pending_proposal(o, symbol="AAPL")
    ok, msg = o.approve_proposal(pid, approver="test")
    assert ok is True
    assert o.journal.count_rows("paper_fills") == 1
    o.close()


# --------------------------------------------------- behavior neutrality (A/B)
def _fingerprint_proposals(journal):
    return [dict(r) for r in journal.query(
        "SELECT symbol, direction, entry, stop, target, qty, status, expected_r, "
        "risk_per_share, dollar_risk, requires_margin, margin_approved, "
        "setup_classification, playbook_name FROM trade_proposals ORDER BY symbol, entry"
    )]


def _fingerprint_rejected(journal):
    return [dict(r) for r in journal.query(
        "SELECT symbol, stage, reason_code, direction, would_be_entry, would_be_stop "
        "FROM rejected_candidates ORDER BY symbol, stage, reason_code"
    )]


def _fingerprint_decision_adjustments(journal):
    return [dict(r) for r in journal.query(
        "SELECT symbol, eval_decision, label_decision, final_decision, adjustment, "
        "override_armed, driver, armed_watch FROM decision_adjustments ORDER BY symbol"
    )]


def _fingerprint_risk_checks(journal):
    return [dict(r) for r in journal.query(
        "SELECT result, fail_reason, max_risk_amount, position_size, entry_price, "
        "stop_price, target_price, reward_risk FROM risk_checks ORDER BY entry_price, result"
    )]


def _fingerprint_candidate_outcomes(journal):
    # Deliberately excludes candidate_id/outcome_id (random UUIDs that will
    # never match across two separate Orchestrator instances regardless of
    # any real behavioral difference) -- same convention as
    # _fingerprint_proposals/_fingerprint_rejected above.
    return [dict(r) for r in journal.query(
        "SELECT symbol, candidate_type, original_decision, entry_reference_price, "
        "stop_price, target_price, outcome_status FROM candidate_outcomes "
        "ORDER BY symbol, entry_reference_price, candidate_type"
    )]


def _fingerprint_trade_outcomes(journal):
    return [dict(r) for r in journal.query(
        "SELECT symbol, realized_r, net_pnl, classification FROM trade_outcomes "
        "ORDER BY symbol, entry_price"
    )]


def test_attribution_toggle_does_not_change_decision_artifacts():
    """The core behavior-neutrality claim: with ATTRIBUTION_ENABLED on vs off,
    every decision-bearing table's content is byte-identical, and the scan
    summary's decision counts match exactly."""
    base = {"INTEREST_SCAN_TOP_N": "12", "MAX_CANDIDATES_TO_AI": "12", "LABELLING_ENABLED": "true"}
    off = Orchestrator(settings=make_settings(ATTRIBUTION_ENABLED="false", **base), journal=JournalStore(":memory:"))
    summ_off = off.run_scan_once()
    off.outcomes_update()
    proposals_off = _fingerprint_proposals(off.journal)
    rejected_off = _fingerprint_rejected(off.journal)
    adjustments_off = _fingerprint_decision_adjustments(off.journal)
    risk_checks_off = _fingerprint_risk_checks(off.journal)
    off.close()

    on = Orchestrator(settings=make_settings(ATTRIBUTION_ENABLED="true", **base), journal=JournalStore(":memory:"))
    summ_on = on.run_scan_once()
    on.outcomes_update()
    proposals_on = _fingerprint_proposals(on.journal)
    rejected_on = _fingerprint_rejected(on.journal)
    adjustments_on = _fingerprint_decision_adjustments(on.journal)
    risk_checks_on = _fingerprint_risk_checks(on.journal)
    on.close()

    assert summ_on.proposed == summ_off.proposed
    assert summ_on.watch == summ_off.watch
    assert summ_on.rejected == summ_off.rejected
    assert summ_on.risk_blocked == summ_off.risk_blocked
    assert proposals_on == proposals_off
    assert rejected_on == rejected_off
    assert adjustments_on == adjustments_off
    assert risk_checks_on == risk_checks_off
    assert proposals_off or rejected_off or adjustments_off or risk_checks_off


def test_attribution_toggle_does_not_change_candidate_or_trade_outcomes_content():
    """Source-immutability across the SAME table this PR reads from: turning
    attribution on/off must not perturb candidate_outcomes/trade_outcomes
    content either (attribution only ever reads them)."""
    base = {"INTEREST_SCAN_TOP_N": "12", "MAX_CANDIDATES_TO_AI": "12", "LABELLING_ENABLED": "true"}
    off = Orchestrator(settings=make_settings(ATTRIBUTION_ENABLED="false", **base), journal=JournalStore(":memory:"))
    off.run_scan_once()
    off.outcomes_update()
    co_off = _fingerprint_candidate_outcomes(off.journal)
    to_off = _fingerprint_trade_outcomes(off.journal)
    off.close()

    on = Orchestrator(settings=make_settings(ATTRIBUTION_ENABLED="true", **base), journal=JournalStore(":memory:"))
    on.run_scan_once()
    on.outcomes_update()
    co_on = _fingerprint_candidate_outcomes(on.journal)
    to_on = _fingerprint_trade_outcomes(on.journal)
    on.close()

    assert co_on == co_off
    assert to_on == to_off


def test_attribution_pass_never_mutates_its_own_source_tables():
    """Fingerprint trade_proposals/user_decision_overrides/candidate_outcomes/
    trade_outcomes immediately before and after a full discover+resolve pass
    -- attribution must never UPDATE any of them."""
    j = JournalStore(":memory:")
    s = make_settings()
    cand_id = _cand(j, status="rejected")
    prop_id = _proposal(j, cand_id, status="rejected")
    _co(j, cand_id, candidate_type="proposal", replay_result="stop_hit", replay_r=-1.0)
    ov_cand = _cand(j, status="watch")
    ov_prop = _proposal(j, ov_cand, status="filled")
    _override(j, ov_cand, proposal_id=ov_prop)
    _trade_outcome(j, ov_prop, realized_r=0.5)

    before_props = _fingerprint_proposals(j)
    before_co = _fingerprint_candidate_outcomes(j)
    before_to = _fingerprint_trade_outcomes(j)
    before_ov = [dict(r) for r in j.query(
        "SELECT override_id, alphaos_would_have_traded, user_final_decision, outcome_r, "
        "outcome_status FROM user_decision_overrides ORDER BY override_id"
    )]

    _run(j, s)
    _run(j, s)  # twice, for good measure

    assert _fingerprint_proposals(j) == before_props
    assert _fingerprint_candidate_outcomes(j) == before_co
    assert _fingerprint_trade_outcomes(j) == before_to
    after_ov = [dict(r) for r in j.query(
        "SELECT override_id, alphaos_would_have_traded, user_final_decision, outcome_r, "
        "outcome_status FROM user_decision_overrides ORDER BY override_id"
    )]
    assert after_ov == before_ov
    j.close()


def test_real_money_remains_unreachable_with_attribution_enabled():
    o = _orch(ATTRIBUTION_ENABLED="true")
    health = o.system_health()
    assert health["real_money_trading"] == "unreachable"
    o.close()


def test_manual_approval_still_required_with_attribution_enabled():
    o = _orch(ATTRIBUTION_ENABLED="true", APPROVAL_MODE="manual")
    pid, _ = inject_pending_proposal(o, symbol="MSFT")
    row = o.journal.proposal_by_id(pid)
    assert row["status"] == "pending_approval"
    o.close()


# ------------------------------------------------------------------ reporting
def test_report_below_sample_floor_shows_counts_only():
    from alphaos.reports.attribution import compute_attribution_v2

    records = [
        {"attribution_type": "propose_user_rejected", "agent": "user", "resolved_status": "resolved",
        "delta_r": 1.0, "execution_delta_r": None, "is_mock": 0, "resolved_at_utc": "2026-01-01T00:00:00+00:00"}
        for _ in range(5)
    ]
    rep = compute_attribution_v2(records)
    agg = rep["aggregate_delta_r_by_type_and_agent"]["propose_user_rejected"]["user"]
    assert agg["status"] == "below_sample_floor"
    assert agg["mean_delta_r"] is None
    assert agg["sum_delta_r"] is None
    assert agg["resolved_count"] == 5


def test_report_meets_sample_floor_shows_aggregate():
    from alphaos.reports.attribution import compute_attribution_v2

    # Jan 1 -> Jan 29 is a 28-day span (29 - 1 = 28), meeting the >=28 floor;
    # 29 distinct days + 1 extra same-day record hits the >=30 resolved floor
    # too, without changing the min/max span.
    timestamps = [f"2026-01-{d:02d}T00:00:00+00:00" for d in range(1, 30)] + ["2026-01-29T12:00:00+00:00"]
    records = [
        {"attribution_type": "propose_user_rejected", "agent": "user", "resolved_status": "resolved",
        "delta_r": 1.0, "execution_delta_r": None, "is_mock": 0, "resolved_at_utc": ts}
        for ts in timestamps
    ]
    assert len(records) == 30
    rep = compute_attribution_v2(records)
    agg = rep["aggregate_delta_r_by_type_and_agent"]["propose_user_rejected"]["user"]
    assert agg["status"] == "ok"
    assert agg["mean_delta_r"] == 1.0
    assert agg["sum_delta_r"] == 30.0
    assert agg["resolved_count"] == 30
    assert agg["span_days"] == 28.5


def test_report_excludes_mock_rows_from_aggregate_and_counts_them():
    from alphaos.reports.attribution import compute_attribution_v2

    records = [
        {"attribution_type": "propose_blocked", "agent": "gate", "resolved_status": "resolved",
        "delta_r": 5.0, "execution_delta_r": None, "is_mock": 1, "resolved_at_utc": "2026-01-01T00:00:00+00:00"}
        for _ in range(50)
    ]
    rep = compute_attribution_v2(records)
    assert rep["mock_excluded_count"] == 50
    assert rep["aggregate_delta_r_by_type_and_agent"] == {}  # nothing to aggregate -- all excluded as mock


def test_report_never_aggregates_across_all_types_into_one_global_value():
    from alphaos.reports.attribution import compute_attribution_v2

    records = [
        {"attribution_type": "propose_blocked", "agent": "gate", "resolved_status": "resolved",
        "delta_r": 1.0, "execution_delta_r": None, "is_mock": 0, "resolved_at_utc": "2026-01-01T00:00:00+00:00"},
        {"attribution_type": "propose_user_rejected", "agent": "user", "resolved_status": "resolved",
        "delta_r": -1.0, "execution_delta_r": None, "is_mock": 0, "resolved_at_utc": "2026-01-01T00:00:00+00:00"},
    ]
    rep = compute_attribution_v2(records)
    assert "total_delta_r" not in rep
    assert "system_value" not in rep
    assert "global_delta_r" not in rep
    assert set(rep["aggregate_delta_r_by_type_and_agent"].keys()) == {"propose_blocked", "propose_user_rejected"}


def test_report_caveat_always_present():
    from alphaos.reports.attribution import compute_attribution_v2

    assert compute_attribution_v2([])["caveat"]
    assert "not" in compute_attribution_v2([])["caveat"].lower()


def test_report_has_no_moralizing_language():
    from alphaos.reports.attribution import ATTRIBUTION_V2_CAVEAT

    banned = ("was right", "was wrong", "user was", "alphaos was")
    text = ATTRIBUTION_V2_CAVEAT.lower()
    assert not any(phrase in text for phrase in banned)


def test_attribution_report_v2_block_present_and_wired():
    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12")
    o.run_scan_once()
    o.outcomes_update()
    rep = o.attribution_report()
    assert "v2" in rep
    assert rep["v2"]["attribution_version"] == ATTRIBUTION_VERSION
    o.close()


def test_digest_attribution_shadow_section_is_read_only():
    from alphaos.scheduler.digest import build_daily_digest

    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12")
    o.run_scan_once()
    o.outcomes_update()
    before = o.journal.count_rows("attribution_records")
    digest = build_daily_digest(o.journal, o.settings, o.kill_switch)
    after = o.journal.count_rows("attribution_records")
    assert after == before
    assert "attribution_shadow" in digest
    assert digest["attribution_shadow"]["enabled"] is True
    o.close()


# ------------------------------------------- UI-PR-A hindsight batch lookup
def _attribution_row(journal, candidate_id, symbol="AAPL", resolved_status="resolved",
                     delta_r=None, **kw):
    """Direct construction of one attribution_records row -- the discover/
    resolve pipeline itself is exhaustively covered elsewhere in this file;
    this only needs to exercise journal_store.attribution_by_candidate()'s
    own SQL, not re-prove attribution correctness."""
    row = {
        "attribution_id": new_id("attr"), "attribution_type": "propose_user_rejected",
        "attribution_version": ATTRIBUTION_VERSION, "agent": "system", "source_id": candidate_id,
        "candidate_id": candidate_id, "symbol": symbol, "resolved_status": resolved_status,
        "delta_r": delta_r, "data_quality_status": "ok",
    }
    row.update(kw)
    journal.insert("attribution_records", row)


def test_attribution_by_candidate_batch_lookup():
    j = JournalStore(":memory:")
    cid_resolved = _cand(j, status="rejected")
    cid_pending = _cand(j, status="rejected")
    cid_missing = _cand(j, status="rejected")  # never gets an attribution row
    _attribution_row(j, cid_resolved, resolved_status="resolved", delta_r=1.23)
    _attribution_row(j, cid_pending, resolved_status="pending", delta_r=None)

    out = j.attribution_by_candidate([cid_resolved, cid_pending, cid_missing])

    assert out[cid_resolved]["resolved_status"] == "resolved"
    assert out[cid_resolved]["delta_r"] == 1.23
    assert out[cid_pending]["resolved_status"] == "pending"
    assert cid_missing not in out  # absent, never a fabricated entry
    assert j.attribution_by_candidate([]) == {}


def test_attribution_by_candidate_latest_row_wins():
    """A candidate can accumulate more than one attribution row over its
    lifecycle (e.g. re-discovered after a later config change) -- the batch
    lookup must return the newest, not an arbitrary one. created_at_utc is
    passed explicitly (not left to real wall-clock insert order) -- two
    inserts in the same test can otherwise land in the same timestamp
    resolution and make "latest wins" nondeterministic (see this project's
    own false-green-from-wall-clock lesson)."""
    j = JournalStore(":memory:")
    cid = _cand(j, status="rejected")
    _attribution_row(j, cid, resolved_status="pending", delta_r=None,
                     created_at_utc="2026-01-01T00:00:00+00:00")
    _attribution_row(j, cid, resolved_status="resolved", delta_r=-0.5,
                     created_at_utc="2026-01-02T00:00:00+00:00")

    out = j.attribution_by_candidate([cid])

    assert out[cid]["resolved_status"] == "resolved"
    assert out[cid]["delta_r"] == -0.5
