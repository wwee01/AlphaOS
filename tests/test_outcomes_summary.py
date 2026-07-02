"""Counterfactual outcome report (Fable 5 review PR2, Part D). Pure-aggregation
unit tests over crafted candidate_outcomes rows + one journal integration.
Measurement visibility only — must never claim statistical significance."""

from __future__ import annotations

from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.reports.metrics import MIN_MEANINGFUL_SAMPLE
from alphaos.reports.outcomes_summary import (
    MEASUREMENT_CAVEAT,
    build_outcomes_report,
    compute_outcomes_summary,
    render_markdown,
)
from alphaos.learning.outcomes_tracker import seed_pending_outcomes, update_pending_outcomes
from alphaos.util.ids import new_id
from conftest import make_settings


def _row(**over):
    base = dict(
        candidate_type="proposal", outcome_status="pending",
        replay_result=None, forward_1d_r=None, forward_3d_r=None, forward_5d_r=None,
    )
    base.update(over)
    return base


def test_counts_by_status_and_type():
    rows = [
        _row(outcome_status="pending", candidate_type="proposal"),
        _row(outcome_status="complete", candidate_type="proposal"),
        _row(outcome_status="complete", candidate_type="reject"),
        _row(outcome_status="unavailable", candidate_type="candidate"),
    ]
    rep = compute_outcomes_summary(rows)
    assert rep["total_tracked"] == 4
    assert rep["pending"] == 1 and rep["complete"] == 2 and rep["unavailable"] == 1
    assert rep["by_candidate_type"] == {"proposal": 2, "reject": 1, "candidate": 1}


def test_forward_outcome_stats_per_type():
    rows = [
        _row(candidate_type="armed_watch", outcome_status="complete", forward_1d_r=0.2, forward_5d_r=1.0),
        _row(candidate_type="armed_watch", outcome_status="complete", forward_1d_r=-0.1, forward_5d_r=-0.5),
        _row(candidate_type="proposal", outcome_status="pending"),
    ]
    rep = compute_outcomes_summary(rows)
    aw = rep["by_type_forward_outcomes"]["armed_watch"]
    assert aw["tracked"] == 2 and aw["complete"] == 2
    assert aw["mean_forward_1d_r"] == round((0.2 - 0.1) / 2, 4)
    assert aw["mean_forward_5d_r"] == round((1.0 - 0.5) / 2, 4)
    prop = rep["by_type_forward_outcomes"]["proposal"]
    assert prop["tracked"] == 1 and prop["pending"] == 1
    assert prop["mean_forward_1d_r"] is None   # nothing resolved yet


def test_bracket_replay_result_counts():
    rows = [
        _row(replay_result="target_hit"), _row(replay_result="target_hit"),
        _row(replay_result="stop_hit"), _row(replay_result="ambiguous_same_bar"),
        _row(replay_result=None),   # not yet replayed -> excluded from the breakdown
    ]
    rep = compute_outcomes_summary(rows)
    assert rep["bracket_replay_results"] == {"target_hit": 2, "stop_hit": 1, "ambiguous_same_bar": 1}


def test_small_sample_caveat_always_present_below_threshold():
    rep = compute_outcomes_summary([_row(outcome_status="complete") for _ in range(3)])
    assert rep["small_sample"] is True
    assert "not statistically significant" in rep["note"]
    assert rep["caveat"] == MEASUREMENT_CAVEAT
    assert MIN_MEANINGFUL_SAMPLE >= 30


def test_no_statistical_claim_language_appears_even_with_many_rows():
    # Even at/above the meaningful-sample threshold, the caveat must still
    # appear — this report never claims edge, only visibility.
    rows = [_row(outcome_status="complete") for _ in range(MIN_MEANINGFUL_SAMPLE + 5)]
    rep = compute_outcomes_summary(rows)
    assert rep["small_sample"] is False
    assert rep["caveat"] == MEASUREMENT_CAVEAT   # caveat present regardless


def test_empty_is_safe():
    rep = compute_outcomes_summary([])
    assert rep["total_tracked"] == 0
    assert rep["small_sample"] is True
    assert rep["by_candidate_type"] == {}
    assert rep["bracket_replay_results"] == {}


def test_render_markdown_mentions_caveat_and_sections():
    rep = compute_outcomes_summary([
        _row(candidate_type="armed_watch", outcome_status="complete", forward_1d_r=0.3,
            replay_result="target_hit"),
    ])
    md = render_markdown(rep)
    assert "Counterfactual Outcome Report" in md
    assert "armed_watch" in md
    assert "target_hit" in md
    assert "⚠️" in md and "statistical" in md.lower()


# --------------------------------------------------------------- integration
def test_build_outcomes_report_via_journal_is_readonly():
    o = Orchestrator(settings=make_settings(), journal=JournalStore(":memory:"))
    o.journal.insert("candidates", {
        "candidate_id": new_id("cand"), "symbol": "AAPL", "direction": "long", "status": "watch",
    })
    seed_pending_outcomes(o.journal)
    update_pending_outcomes(o.journal, bars_provider=None)   # no-op safely

    rep = build_outcomes_report(o.journal, o.settings)
    assert rep["total_tracked"] >= 1
    assert rep["mode"] == "mock"
    # PURE READ — reporting never executes anything or loosens safety.
    assert o.journal.count_rows("paper_orders") == 0
    assert o.system_health()["real_money_trading"] == "unreachable"
    o.close()
