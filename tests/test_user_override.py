"""User Override Mode (Roadmap 2.8 Part C): a SEPARATE, safety-gated decision
layer. A user override NEVER rewrites AlphaOS's original recommendation, never
bypasses the gates / manual approval / real-money guard, and never auto-executes.
Hermetic."""

from __future__ import annotations

from types import SimpleNamespace

from alphaos.constants import (
    AttributionResult,
    Decision,
    OverrideAggressiveness,
    OverrideBlockedReason,
    OverrideOutcomeStatus,
    UserOverrideAction,
)
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from conftest import inject_pending_proposal, make_settings


def _orch(**over):
    return Orchestrator(settings=make_settings(**over), journal=JournalStore(":memory:"))


def _a_watch_candidate(o):
    """Run a scan and return a candidate_id whose AlphaOS decision is WATCH, with a
    decision_adjustments row + a usable eval. The mock scan's RNG is seeded by
    market_date(), so which candidates land where varies by day; normalize the
    chosen candidate to WATCH here so these tests are deterministic across dates."""
    o.run_scan_once()
    row = o.journal.one(
        "SELECT da.candidate_id AS candidate_id FROM decision_adjustments da "
        "JOIN openai_evaluations ev ON ev.candidate_id = da.candidate_id "
        "WHERE ev.entry IS NOT NULL ORDER BY da.id LIMIT 1")
    if not row:
        row = o.journal.one("SELECT candidate_id FROM candidates WHERE label_decision IS NOT NULL LIMIT 1")
    cid = row["candidate_id"]
    o.journal.conn.execute(
        "UPDATE decision_adjustments SET final_decision = 'watch' WHERE candidate_id = ?", (cid,))
    o.journal.conn.execute(
        "UPDATE candidates SET status = 'watch', label_decision = 'watch' WHERE candidate_id = ?", (cid,))
    o.journal.conn.commit()
    return cid


def test_watch_to_trade_creates_pending_proposal_and_preserves_alphaos(monkeypatch):
    o = _orch()
    cid = _a_watch_candidate(o)
    res = o.create_user_override(cid, UserOverrideAction.WATCH_TO_TRADE.value,
                                 reason_code="strong_conviction", note="testing")
    assert res["ok"]
    ov = res["override"]
    # AlphaOS original recommendation is PRESERVED, separate from the user decision
    assert ov["alphaos_final_decision"] != Decision.PROPOSE.value
    assert ov["user_final_decision"] == Decision.PROPOSE.value
    assert ov["override_aggressiveness"] == OverrideAggressiveness.MORE_AGGRESSIVE.value
    assert ov["user_reason_code"] == "strong_conviction" and ov["user_reason_text"] == "testing"
    # gates passed -> a PENDING_APPROVAL proposal exists, but NOTHING executed
    if ov["execution_allowed"]:
        prop = o.journal.proposal_by_id(ov["proposal_id"])
        assert prop and prop["status"] == "pending_approval"
    assert o.journal.count_rows("paper_orders") == 0
    assert o.journal.count_rows("approvals") == 0          # manual approval still required
    assert o.journal.count_open_positions() == 0
    # the candidate's own AlphaOS decision row is untouched (not rewritten to propose)
    adj = o.journal.one("SELECT final_decision FROM decision_adjustments WHERE candidate_id = ?", (cid,))
    assert adj["final_decision"] != Decision.PROPOSE.value
    o.close()


def test_override_blocked_by_freshness_gate(monkeypatch):
    o = _orch()
    cid = _a_watch_candidate(o)
    monkeypatch.setattr(o.freshness, "assess",
                        lambda snap: SimpleNamespace(is_usable=False, freshness_status="stale",
                                                     block_reason="stale_quote"))
    ov = o.create_user_override(cid, UserOverrideAction.WATCH_TO_TRADE.value)["override"]
    assert ov["execution_allowed"] == 0
    assert ov["blocked_reason"] == OverrideBlockedReason.STALE_DATA.value
    assert ov["proposal_id"] is None
    o.close()


def test_override_blocked_by_risk_gate(monkeypatch):
    o = _orch()
    cid = _a_watch_candidate(o)
    monkeypatch.setattr(o.risk, "assess",
                        lambda **kw: SimpleNamespace(approved=False, sizing=None,
                                                     primary_reason="risk_blocked", block_reasons=[]))
    ov = o.create_user_override(cid, UserOverrideAction.WATCH_TO_TRADE.value)["override"]
    assert ov["execution_allowed"] == 0
    assert ov["blocked_reason"] == OverrideBlockedReason.RISK_GATE_FAILED.value
    o.close()


def test_override_blocked_by_spread_maps_reason(monkeypatch):
    from alphaos.constants import ReasonCode
    o = _orch()
    cid = _a_watch_candidate(o)
    monkeypatch.setattr(o.risk, "assess",
                        lambda **kw: SimpleNamespace(approved=False, sizing=None,
                                                     primary_reason="wide_spread",
                                                     block_reasons=[{"code": ReasonCode.WIDE_SPREAD.value}]))
    ov = o.create_user_override(cid, UserOverrideAction.WATCH_TO_TRADE.value)["override"]
    assert ov["blocked_reason"] == OverrideBlockedReason.WIDE_SPREAD.value
    o.close()


def test_propose_to_reject_rejects_proposal_and_records_override():
    o = _orch()
    proposal_id, _ = inject_pending_proposal(o, symbol="AAPL")
    cid = o.journal.proposal_by_id(proposal_id)["candidate_id"]
    res = o.create_user_override(cid, UserOverrideAction.PROPOSE_TO_REJECT.value,
                                 reason_code="disagrees_with_ai")
    assert res["ok"]
    assert o.journal.proposal_by_id(proposal_id)["status"] == "rejected"
    ov = res["override"]
    assert ov["user_final_decision"] == Decision.REJECT.value
    assert ov["override_aggressiveness"] == OverrideAggressiveness.MORE_CONSERVATIVE.value
    o.close()


def test_manual_exit_without_position_is_blocked():
    o = _orch()
    cid = _a_watch_candidate(o)
    ov = o.create_user_override(cid, UserOverrideAction.MANUAL_EXIT.value)["override"]
    assert ov["blocked_reason"] == OverrideBlockedReason.NO_OPEN_POSITION.value
    o.close()


def test_override_record_persists_and_is_queryable():
    o = _orch()
    cid = _a_watch_candidate(o)
    o.create_user_override(cid, UserOverrideAction.MANUAL_HOLD.value, note="waiting for confirmation")
    rows = o.journal.recent_user_overrides(10)
    assert rows and rows[0]["user_override_action"] == UserOverrideAction.MANUAL_HOLD.value
    summ = o.journal.user_override_summary()
    assert summ["by_action"]
    o.close()


def test_high_risk_override_tags_proposal_warning_and_nightdesk(monkeypatch):
    o = _orch()
    cid = _a_watch_candidate(o)
    # mark the candidate's decision as high-risk-narrative armed
    o.journal.conn.execute(
        "UPDATE decision_adjustments SET arming_classification = 'high_risk_narrative' WHERE candidate_id = ?",
        (cid,))
    o.journal.conn.commit()
    ov = o.create_user_override(cid, UserOverrideAction.WATCH_TO_TRADE.value)["override"]
    assert ov["arming_classification"] == "high_risk_narrative"
    assert ov["nightdesk_research_candidate"] == 1     # flagged for research
    if ov["execution_allowed"]:
        prop = o.journal.proposal_by_id(ov["proposal_id"])
        assert prop["arming_classification"] == "high_risk_narrative"
        assert prop["narrative_warning"]               # warning surfaced on the proposal
    o.close()


def test_override_outcome_resolves_with_attribution():
    o = _orch()
    cid = _a_watch_candidate(o)
    ov = o.create_user_override(cid, UserOverrideAction.WATCH_TO_TRADE.value)["override"]
    assert ov["attribution_result"] == AttributionResult.PENDING.value
    out = o.resolve_user_override(ov["override_id"], outcome_r=1.5, outcome_pnl=120.0,
                                  outcome_status=OverrideOutcomeStatus.WON.value, did_trade=True)
    assert out["ok"]
    # AlphaOS would NOT have traded (it said watch) + the user won -> user_outperformed
    assert out["attribution_result"] == AttributionResult.USER_OUTPERFORMED.value
    row = o.journal.override_by_id(ov["override_id"])
    assert row["outcome_status"] == OverrideOutcomeStatus.WON.value and row["resolved_at_utc"]
    o.close()


def test_override_never_enables_real_money_or_auto_exec():
    o = _orch()
    cid = _a_watch_candidate(o)
    o.create_user_override(cid, UserOverrideAction.WATCH_TO_TRADE.value)
    h = o.system_health()
    assert h["real_money_trading"] == "unreachable"
    assert h["manual_approval"] == "required"
    assert h["execution_provider"] == "simulated_internal"
    assert o.journal.count_rows("paper_orders") == 0     # no auto-execution
    o.close()


def test_override_missing_candidate_returns_error():
    o = _orch()
    res = o.create_user_override("nope", UserOverrideAction.WATCH_TO_TRADE.value)
    assert res["ok"] is False
    o.close()
