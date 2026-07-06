"""AI token-usage capture (PR9.5): the three real-call-site additions to
openai_client.py / playbook_classifier.py / last30days_polarity.py. The
_extract_usage helpers only ever see a real OpenAI SDK response object
(`_live_eval`/`_live_classify` are `# pragma: no cover` -- real network,
matching this codebase's established precedent of testing the pure/defensive
logic directly rather than mocking the SDK end-to-end; see
test_lineage.py::test_ai_call_lineage_is_deterministic_and_hashes_the_prompt
for the same precedent applied to PR4's lineage helper).
"""

from __future__ import annotations

from alphaos.ai.last30days_polarity import PolarityResult
from alphaos.ai.last30days_polarity import _extract_usage as polarity_extract_usage
from alphaos.ai.openai_client import OpenAIEvaluation
from alphaos.ai.openai_client import _extract_usage as openai_extract_usage
from alphaos.ai.playbook_classifier import PlaybookClassification
from alphaos.ai.playbook_classifier import _extract_usage as label_extract_usage

_EXTRACTORS = (openai_extract_usage, label_extract_usage, polarity_extract_usage)


class _FakeUsage:
    def __init__(self, prompt_tokens=100, completion_tokens=50, total_tokens=150):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


class _FakeResponse:
    def __init__(self, usage=None):
        self.usage = usage


# --------------------------------------------------------- _extract_usage
def test_extract_usage_reads_a_well_formed_response_all_three_modules():
    for extract in _EXTRACTORS:
        resp = _FakeResponse(usage=_FakeUsage(100, 50, 150))

        usage = extract(resp)

        assert usage == {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}


def test_extract_usage_returns_none_when_usage_attribute_is_none_all_three_modules():
    for extract in _EXTRACTORS:
        resp = _FakeResponse(usage=None)

        assert extract(resp) is None


def test_extract_usage_returns_none_when_usage_attribute_is_missing_all_three_modules():
    class _NoUsageResponse:
        pass

    for extract in _EXTRACTORS:
        assert extract(_NoUsageResponse()) is None


def test_extract_usage_tolerates_partial_usage_fields_all_three_modules():
    class _PartialUsage:
        prompt_tokens = 42
        # completion_tokens / total_tokens deliberately absent

    for extract in _EXTRACTORS:
        resp = _FakeResponse(usage=_PartialUsage())

        usage = extract(resp)

        assert usage["prompt_tokens"] == 42
        assert usage["completion_tokens"] is None
        assert usage["total_tokens"] is None


# ------------------------------------------------------- dataclass + to_row
def test_openai_evaluation_to_row_carries_token_fields():
    ev = OpenAIEvaluation(
        eval_id="ev1", candidate_id="c1", symbol="AAPL", model="gpt-5.4-mini",
        direction="long", entry=100.0, stop=97.0, target=106.0, max_holding_days=3,
        expected_r=2.0, confidence=0.8, decision="propose", reasoning_summary="test",
        prompt_tokens=120, completion_tokens=60, total_tokens=180,
    )

    row = ev.to_row()

    assert row["prompt_tokens"] == 120
    assert row["completion_tokens"] == 60
    assert row["total_tokens"] == 180


def test_openai_evaluation_defaults_token_fields_to_none():
    ev = OpenAIEvaluation(
        eval_id="ev1", candidate_id="c1", symbol="AAPL", model="mock",
        direction="long", entry=100.0, stop=97.0, target=106.0, max_holding_days=3,
        expected_r=2.0, confidence=0.8, decision="propose", reasoning_summary="test",
        is_mock=True,
    )

    row = ev.to_row()

    assert row["prompt_tokens"] is None
    assert row["completion_tokens"] is None
    assert row["total_tokens"] is None


def test_playbook_classification_to_row_carries_token_fields():
    from alphaos.util.ids import new_id

    clf = PlaybookClassification(
        label_id=new_id("lbl"), candidate_id="c1", symbol="AAPL", primary_label="Momentum",
        secondary_labels=[], candidate_tags=[], risk_tags=[], direction="long",
        label_decision="propose", confidence=0.8, reason_for_label="test", thesis_stub="",
        invalidation="", main_risk="", missing_context=[], suggested_new_tags=[],
        label_version="v1", label_source="openai", validation_status="passed",
        model="gpt-5.4-mini", is_mock=False,
        prompt_tokens=200, completion_tokens=80, total_tokens=280,
    )

    row = clf.to_row(packet_id="pkt1", scan_batch_id="sb1", frozen_at_utc="2026-07-06T00:00:00+00:00")

    assert row["prompt_tokens"] == 200
    assert row["completion_tokens"] == 80
    assert row["total_tokens"] == 280


def test_playbook_classification_to_row_does_not_include_lineage_fields():
    """Documents the deliberate asymmetry: model_provider/prompt_hash/
    system_prompt_hash flow into decision_adjustments.ai_lineage_json instead
    (Orchestrator._record_decision_adjustment), NOT this row -- but token
    usage gets its own columns here so cost_guard can count with a plain
    query, not a JSON parse. See playbook_classifier.py's to_row() comment."""
    from alphaos.util.ids import new_id

    clf = PlaybookClassification(
        label_id=new_id("lbl"), candidate_id="c1", symbol="AAPL", primary_label="Momentum",
        secondary_labels=[], candidate_tags=[], risk_tags=[], direction="long",
        label_decision="propose", confidence=0.8, reason_for_label="test", thesis_stub="",
        invalidation="", main_risk="", missing_context=[], suggested_new_tags=[],
        label_version="v1", label_source="openai", validation_status="passed",
        model="gpt-5.4-mini", is_mock=False, model_provider="openai", prompt_hash="abc123",
    )

    row = clf.to_row(packet_id=None, scan_batch_id=None, frozen_at_utc="2026-07-06T00:00:00+00:00")

    assert "model_provider" not in row
    assert "prompt_hash" not in row


def test_polarity_result_to_row_carries_token_fields():
    result = PolarityResult(
        candidate_id="c1", symbol="AAPL", provider="cli", model="gpt-5.4-mini",
        prompt_template_version="v1", sentiment_label="bullish", sentiment_score=0.5,
        confidence=0.7, direction_alignment="aligned", source_coverage_quality="medium",
        narrative_driver_type="catalyst", hype_or_manipulation_risk="none",
        requires_user_attention=False, official_catalyst_conflict=False,
        should_arm_override=False, arming_classification="non_arming", warning_message="",
        reasoning_summary="test", evidence_items=[], raw_json={}, parse_status="parsed",
        prompt_tokens=90, completion_tokens=30, total_tokens=120,
    )

    row = result.to_row()

    assert row["prompt_tokens"] == 90
    assert row["completion_tokens"] == 30
    assert row["total_tokens"] == 120


def test_polarity_result_defaults_token_fields_to_none():
    result = PolarityResult(
        candidate_id="c1", symbol="AAPL", provider="mock", model="mock",
        prompt_template_version="v1", sentiment_label="neutral", sentiment_score=0.0,
        confidence=0.5, direction_alignment="neutral", source_coverage_quality="low",
        narrative_driver_type="unclear", hype_or_manipulation_risk="none",
        requires_user_attention=False, official_catalyst_conflict=False,
        should_arm_override=False, arming_classification="non_arming", warning_message="",
        reasoning_summary="test", evidence_items=[], raw_json={}, parse_status="parsed",
    )

    row = result.to_row()

    assert row["prompt_tokens"] is None
    assert row["completion_tokens"] is None
    assert row["total_tokens"] is None
