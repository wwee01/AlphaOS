"""User-override attribution report (Roadmap 2.8 follow-up). Pure-aggregation
unit tests over crafted override rows + one orchestrator integration. Hermetic;
the report is a READ-ONLY heuristic and must never claim significance on a small
sample."""

from __future__ import annotations

from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.reports.attribution import (
    ATTRIBUTION_CAVEAT,
    MIN_MEANINGFUL_OVERRIDE_SAMPLE,
    build_attribution_report,
    compute_attribution,
    render_markdown,
)
from conftest import inject_pending_proposal, make_settings


def _ovr(**over):
    base = dict(
        override_id="ovr_x", symbol="AAPL",
        user_override_action="watch_to_trade", user_reason_code="testing_hypothesis",
        arming_classification=None, armed_watch=0,
        execution_allowed=0, blocked_reason=None,
        nightdesk_research_candidate=0, nightdesk_research_reason=None,
        outcome_status="pending", attribution_result="pending",
        outcome_r=None, outcome_pnl=None,
    )
    base.update(over)
    return base


def test_report_counts_override_types():
    rows = [
        _ovr(user_override_action="watch_to_trade"),
        _ovr(user_override_action="watch_to_trade"),
        _ovr(user_override_action="propose_to_reject"),
        _ovr(user_override_action="manual_hold"),
    ]
    rep = compute_attribution(rows)
    assert rep["total_overrides"] == 4
    assert rep["by_action"] == {"watch_to_trade": 2, "propose_to_reject": 1, "manual_hold": 1}


def test_report_counts_reason_codes():
    rows = [
        _ovr(user_reason_code="testing_hypothesis"),
        _ovr(user_reason_code="risk_reduction"),
        _ovr(user_reason_code="risk_reduction"),
        _ovr(user_reason_code=None),                 # no reason -> skipped
    ]
    rep = compute_attribution(rows)
    assert rep["by_reason_code"] == {"testing_hypothesis": 1, "risk_reduction": 2}


def test_report_separates_high_risk_narrative():
    rows = [
        _ovr(arming_classification="high_risk_narrative"),
        _ovr(arming_classification="high_risk_narrative"),
        _ovr(arming_classification="normal_driver"),
        _ovr(armed_watch=1, arming_classification="normal_driver"),
    ]
    rep = compute_attribution(rows)
    assert rep["high_risk_narrative_overrides"] == 2
    assert rep["by_arming_classification"]["high_risk_narrative"] == 2
    assert rep["by_arming_classification"]["normal_driver"] == 2
    assert rep["armed_watch_overrides"] == 1


def test_report_executed_blocked_and_blocked_reasons():
    rows = [
        _ovr(execution_allowed=1),
        _ovr(execution_allowed=0, blocked_reason="stale_data"),
        _ovr(execution_allowed=0, blocked_reason="risk_gate_failed"),
        _ovr(execution_allowed=0, blocked_reason="stale_data"),
    ]
    rep = compute_attribution(rows)
    assert rep["executed"] == 1
    assert rep["blocked"] == 3
    assert rep["by_blocked_reason"] == {"stale_data": 2, "risk_gate_failed": 1}


def test_report_outcome_and_attribution_buckets():
    rows = [
        _ovr(outcome_status="pending", attribution_result="pending"),
        _ovr(outcome_status="won", attribution_result="user_outperformed"),
        _ovr(outcome_status="lost", attribution_result="alphaos_outperformed"),
        _ovr(outcome_status="won", attribution_result="inconclusive"),
        _ovr(outcome_status="breakeven", attribution_result="inconclusive"),
    ]
    rep = compute_attribution(rows)
    assert rep["outcomes"]["pending"] == 1
    assert rep["outcomes"]["completed"] == 4          # won+lost+breakeven
    assert rep["outcomes"]["won"] == 2 and rep["outcomes"]["lost"] == 1
    assert rep["outcomes"]["breakeven"] == 1
    assert rep["attribution"]["user_outperformed"] == 1
    assert rep["attribution"]["alphaos_outperformed"] == 1
    assert rep["attribution"]["inconclusive"] == 2
    assert rep["attribution"]["pending"] == 1


def test_report_win_rate_and_expectancy():
    rows = [
        _ovr(outcome_status="won", outcome_r=1.5, outcome_pnl=120.0),
        _ovr(outcome_status="won", outcome_r=1.0, outcome_pnl=80.0),
        _ovr(outcome_status="lost", outcome_r=-1.0, outcome_pnl=-50.0),
        _ovr(outcome_status="pending"),
    ]
    p = compute_attribution(rows)["performance"]
    assert p["completed_sample"] == 3
    assert p["user_win_rate"] == round(2 / 3, 3)
    assert p["user_expectancy_pnl"] == round((120 + 80 - 50) / 3, 2)
    assert p["user_expectancy_r"] == round((1.5 + 1.0 - 1.0) / 3, 3)


def test_report_alphaos_followed_expectancy_from_trade_outcomes():
    overrides = [_ovr(outcome_status="won", outcome_pnl=100.0)]
    alphaos_outcomes = [
        {"net_pnl": 50.0, "gross_pnl": 52.0, "costs": 2.0},
        {"net_pnl": -20.0, "gross_pnl": -19.0, "costs": 1.0},
    ]
    p = compute_attribution(overrides, alphaos_outcomes=alphaos_outcomes)["performance"]
    assert p["alphaos_followed_sample"] == 2
    assert p["alphaos_followed_expectancy_pnl"] == round((50 - 20) / 2, 2)   # 15.0


def test_report_small_sample_caveat_present():
    rows = [_ovr(outcome_status="won", outcome_pnl=10.0)]
    rep = compute_attribution(rows)
    assert rep["performance"]["small_sample"] is True
    assert "not statistically significant" in rep["performance"]["note"]
    assert rep["caveat"] == ATTRIBUTION_CAVEAT
    # threshold is honest about being low-frequency
    assert MIN_MEANINGFUL_OVERRIDE_SAMPLE >= 20


def test_report_nightdesk_candidates_counted():
    rows = [
        _ovr(nightdesk_research_candidate=1, nightdesk_research_reason="high_risk_narrative_override"),
        _ovr(nightdesk_research_candidate=1, nightdesk_research_reason="user_rejected_alphaos_proposal"),
        _ovr(nightdesk_research_candidate=0),
    ]
    rep = compute_attribution(rows)
    assert rep["nightdesk_research_candidates"] == 2
    assert rep["by_nightdesk_reason"]["high_risk_narrative_override"] == 1
    assert rep["by_nightdesk_reason"]["user_rejected_alphaos_proposal"] == 1


def test_report_override_rate_and_empty_is_safe():
    assert compute_attribution([])["total_overrides"] == 0
    empty = compute_attribution([])
    assert empty["performance"]["user_win_rate"] is None
    assert empty["outcomes"]["completed"] == 0
    assert "no resolved overrides" in empty["performance"]["note"]
    rep = compute_attribution([_ovr(), _ovr()], total_recommendations=10)
    assert rep["override_rate"] == 0.2


def test_render_markdown_is_stringy_and_mentions_caveat():
    rows = [_ovr(outcome_status="won", outcome_pnl=10.0, attribution_result="user_outperformed")]
    md = render_markdown(compute_attribution(rows))
    assert "Attribution Report" in md and "Who outperformed" in md
    assert "user_outperformed: 1" in md
    assert "⚠️" in md                                # caveat is surfaced


def test_attribution_report_via_orchestrator_is_readonly():
    o = Orchestrator(settings=make_settings(), journal=JournalStore(":memory:"))
    # proposal -> user rejects (risk_reduction)
    pid, _ = inject_pending_proposal(o, symbol="AAPL")
    cid = o.journal.proposal_by_id(pid)["candidate_id"]
    o.create_user_override(cid, "propose_to_reject", reason_code="risk_reduction")
    # a watch candidate -> watch_to_trade (testing_hypothesis) -> resolve as won
    o.run_scan_once()
    wc = (o.journal.one("SELECT candidate_id FROM candidates WHERE status='watch' LIMIT 1")
          or o.journal.one("SELECT candidate_id FROM candidates WHERE label_decision IS NOT NULL LIMIT 1"))
    ov = o.create_user_override(wc["candidate_id"], "watch_to_trade",
                                reason_code="testing_hypothesis")["override"]
    o.resolve_user_override(ov["override_id"], outcome_r=1.5, outcome_pnl=100.0,
                            outcome_status="won", did_trade=True)

    rep = build_attribution_report(o.journal, o.settings)
    assert rep["total_overrides"] >= 2
    assert rep["by_action"].get("propose_to_reject") == 1
    assert rep["by_action"].get("watch_to_trade") == 1
    assert rep["by_reason_code"].get("risk_reduction") == 1
    assert rep["outcomes"]["completed"] >= 1
    # PURE READ — the report never executed anything and never loosened safety
    assert o.journal.count_rows("paper_orders") == 0
    assert o.system_health()["real_money_trading"] == "unreachable"
    o.close()
