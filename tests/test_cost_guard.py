"""AI cost guard (PR3; true-up in PR9.5): counts real (non-mock) AI calls
across all three call sites -- openai_evaluations, candidate_labels,
last30days_polarity -- against the trailing-30-day cap. Previously only the
first table was counted, undercounting real spend 2-3x. Hermetic; direct
row construction, never depends on a natural scan producing these rows.
"""

from __future__ import annotations

from datetime import timedelta

from alphaos.journal.journal_store import JournalStore
from alphaos.scheduler import cost_guard
from alphaos.util import timeutils
from alphaos.util.ids import new_id
from conftest import make_settings


def _eval_row(is_mock=0, age_days=0):
    st = timeutils.stamp(timeutils.now_utc() - timedelta(days=age_days))
    return {
        "eval_id": new_id("eval"), "candidate_id": new_id("cand"), "symbol": "AAPL",
        "model": "gpt-5.4-mini", "direction": "long", "entry": 100.0, "stop": 97.0,
        "target": 106.0, "max_holding_days": 3, "expected_r": 2.0, "confidence": 0.8,
        "decision": "propose", "reasoning_summary": "test", "is_mock": is_mock,
        "created_at_utc": st.utc, "created_at_sgt": st.local_sgt,
    }


def _label_row(is_mock=0, age_days=0):
    st = timeutils.stamp(timeutils.now_utc() - timedelta(days=age_days))
    return {
        "label_id": new_id("lbl"), "candidate_id": new_id("cand"), "symbol": "AAPL",
        "primary_label": "Momentum", "label_decision": "propose", "label_confidence": 0.8,
        "reason_for_label": "test", "label_version": "v1", "label_source": "openai",
        "validation_status": "passed", "model": "gpt-5.4-mini", "is_mock": is_mock,
        "created_at_utc": st.utc, "created_at_sgt": st.local_sgt,
    }


def _polarity_row(model_provider="openai", age_days=0):
    """model_provider=None simulates mock/skipped/error paths (see
    last30days_polarity.py's _build/_mock_classify/_skipped/_error)."""
    st = timeutils.stamp(timeutils.now_utc() - timedelta(days=age_days))
    return {
        "polarity_id": new_id("pol"), "candidate_id": new_id("cand"), "symbol": "AAPL",
        "sentiment_label": "bullish", "confidence": 0.7, "direction_alignment": "aligned",
        "source_coverage_quality": "medium", "narrative_driver_type": "catalyst",
        "hype_or_manipulation_risk": "none", "requires_user_attention": 0,
        "official_catalyst_conflict": 0, "should_arm_override": 0,
        "arming_classification": "non_arming", "warning_message": "", "reasoning_summary": "test",
        "parse_status": "parsed", "model_provider": model_provider,
        "created_at_utc": st.utc, "created_at_sgt": st.local_sgt,
    }


def test_counts_only_real_openai_evaluations(journal):
    journal.insert("openai_evaluations", _eval_row(is_mock=0))
    journal.insert("openai_evaluations", _eval_row(is_mock=1))

    assert cost_guard.calls_in_last_30_days(journal) == 1


def test_counts_only_real_candidate_labels(journal):
    journal.insert("candidate_labels", _label_row(is_mock=0))
    journal.insert("candidate_labels", _label_row(is_mock=1))

    assert cost_guard.calls_in_last_30_days(journal) == 1


def test_counts_only_real_last30days_polarity_via_model_provider(journal):
    journal.insert("last30days_polarity", _polarity_row(model_provider="openai"))
    journal.insert("last30days_polarity", _polarity_row(model_provider=None))  # mock/skipped/error

    assert cost_guard.calls_in_last_30_days(journal) == 1


def test_sums_across_all_three_tables():
    """The core PR9.5 fix: one real call in each of the three tables must
    count as 3, not 1 -- each is a genuinely separate real API call."""
    journal = JournalStore(":memory:")
    journal.insert("openai_evaluations", _eval_row(is_mock=0))
    journal.insert("candidate_labels", _label_row(is_mock=0))
    journal.insert("last30days_polarity", _polarity_row(model_provider="openai"))

    assert cost_guard.calls_in_last_30_days(journal) == 3
    journal.close()


def test_excludes_calls_older_than_30_days():
    journal = JournalStore(":memory:")
    journal.insert("openai_evaluations", _eval_row(is_mock=0, age_days=0))
    journal.insert("openai_evaluations", _eval_row(is_mock=0, age_days=31))
    journal.insert("candidate_labels", _label_row(is_mock=0, age_days=45))
    journal.insert("last30days_polarity", _polarity_row(model_provider="openai", age_days=60))

    assert cost_guard.calls_in_last_30_days(journal) == 1
    journal.close()


def test_check_scan_budget_detail_reflects_all_three_tables(journal):
    for _ in range(3):
        journal.insert("openai_evaluations", _eval_row(is_mock=0))
    journal.insert("candidate_labels", _label_row(is_mock=0))
    journal.insert("last30days_polarity", _polarity_row(model_provider="openai"))
    settings = make_settings(SCHEDULER_AI_COST_CAP_CALLS_PER_30D="100", SHADOW_AI_CAP_CALLS_PER_30D="25")

    within_budget, detail = cost_guard.check_scan_budget(settings, journal)

    assert within_budget is True
    assert "5/100 real AI calls used in trailing 30 days" == detail


def test_check_scan_budget_trips_when_the_combined_total_reaches_the_cap(journal):
    # SCHEDULER_AI_COST_CAP_CALLS_PER_30D's own validated floor is 50 -- split
    # 25/24/1 across the three tables so the trip is genuinely a COMBINED
    # total, not any single table alone reaching the cap.
    for _ in range(25):
        journal.insert("openai_evaluations", _eval_row(is_mock=0))
    for _ in range(24):
        journal.insert("candidate_labels", _label_row(is_mock=0))
    journal.insert("last30days_polarity", _polarity_row(model_provider="openai"))
    settings = make_settings(SCHEDULER_AI_COST_CAP_CALLS_PER_30D="50", SHADOW_AI_CAP_CALLS_PER_30D="12")

    within_budget, detail = cost_guard.check_scan_budget(settings, journal)

    assert within_budget is False
    assert "cap reached" in detail
    assert "50/50" in detail


def test_check_scan_budget_fails_safe_on_a_db_error(journal, monkeypatch):
    def boom(_journal):
        raise RuntimeError("simulated DB error")

    monkeypatch.setattr(cost_guard, "calls_in_last_30_days", boom)
    settings = make_settings()

    within_budget, detail = cost_guard.check_scan_budget(settings, journal)

    assert within_budget is False
    assert "error checking AI cost cap" in detail
