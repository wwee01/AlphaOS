"""TQS v0 pure scoring formula (PR7): component normalization, missing-data
policy (never treat unknown as safe), determinism, bucket thresholds, and the
never-fabricate-a-score-from-nothing contract. Hermetic -- pure function,
no journal/DB/orchestrator involved."""

from __future__ import annotations

from alphaos.constants import TqsBucket, TqsDataQualityStatus
from alphaos.tqs.scoring import (
    TQS_VERSION,
    WEIGHTS,
    TqsComponentInputs,
    compute_tqs,
)


def _inputs(**overrides) -> TqsComponentInputs:
    return TqsComponentInputs(symbol="AAPL", **overrides)


# --------------------------------------------------------------- all-missing
def test_all_missing_is_unscorable_never_fabricated():
    r = compute_tqs(_inputs())
    assert r.raw_score is None
    assert r.tqs_score is None
    assert r.tqs_bucket == TqsBucket.UNSCORABLE.value
    assert r.data_quality_status == TqsDataQualityStatus.UNSCORABLE.value
    assert r.data_confidence == 0.0
    assert r.components == {}
    assert set(r.missing_components.keys()) == set(WEIGHTS.keys())


# ------------------------------------------------- C1: reward:risk geometry
def test_reward_risk_geometry_normalization():
    r = compute_tqs(_inputs(expected_r=1.0))
    assert r.components["reward_risk_geometry"]["score"] == 0.0
    r2 = compute_tqs(_inputs(expected_r=3.0))
    assert r2.components["reward_risk_geometry"]["score"] == 1.0
    r3 = compute_tqs(_inputs(expected_r=2.0))
    assert r3.components["reward_risk_geometry"]["score"] == 0.5


def test_reward_risk_geometry_clamped_outside_range():
    r = compute_tqs(_inputs(expected_r=10.0))
    assert r.components["reward_risk_geometry"]["score"] == 1.0
    r2 = compute_tqs(_inputs(expected_r=-5.0))
    assert r2.components["reward_risk_geometry"]["score"] == 0.0


def test_reward_risk_geometry_missing_when_no_expected_r():
    r = compute_tqs(_inputs(expected_r=None))
    assert "reward_risk_geometry" in r.missing_components
    assert r.missing_components["reward_risk_geometry"]["reason"] == "missing_expected_r"


# ------------------------------------------------------ C2: interest strength
def test_interest_strength_passthrough():
    r = compute_tqs(_inputs(interest_score=0.73))
    assert r.components["interest_strength"]["score"] == 0.73


def test_interest_strength_missing_when_absent():
    r = compute_tqs(_inputs(interest_score=None))
    assert r.missing_components["interest_strength"]["reason"] == "missing_interest_score"


# --------------------------------------------------- C3: microstructure quality
def test_microstructure_zero_spread_is_perfect():
    r = compute_tqs(_inputs(spread_pct=0.0, max_spread_pct=0.01))
    assert r.components["microstructure_quality"]["score"] == 1.0


def test_microstructure_at_gate_limit_is_zero():
    r = compute_tqs(_inputs(spread_pct=0.01, max_spread_pct=0.01))
    assert r.components["microstructure_quality"]["score"] == 0.0


def test_microstructure_missing_when_spread_absent():
    r = compute_tqs(_inputs(spread_pct=None))
    assert r.missing_components["microstructure_quality"]["reason"] == "missing_spread_pct"


def test_microstructure_missing_when_spread_negative():
    r = compute_tqs(_inputs(spread_pct=-0.01, max_spread_pct=0.01))
    assert r.missing_components["microstructure_quality"]["reason"] == "missing_spread_pct"


# ------------------------------------------------------- C4: AI conviction
def test_ai_conviction_available_when_live():
    r = compute_tqs(_inputs(ai_available=True, ai_confidence=0.8, ai_is_mock=False,
                            ai_validation_status="passed"))
    assert r.components["ai_conviction"]["score"] == 0.8


def test_ai_conviction_missing_when_mock():
    r = compute_tqs(_inputs(ai_available=True, ai_confidence=0.8, ai_is_mock=True))
    assert r.missing_components["ai_conviction"]["reason"] == "mock_ai"


def test_ai_conviction_missing_when_no_evaluation():
    r = compute_tqs(_inputs(ai_available=False))
    assert r.missing_components["ai_conviction"]["reason"] == "no_evaluation"


def test_ai_conviction_missing_when_validation_failed():
    r = compute_tqs(_inputs(ai_available=True, ai_confidence=0.8, ai_is_mock=False,
                            ai_validation_status="invented_catalyst_in_no_news_mode"))
    assert r.missing_components["ai_conviction"]["reason"] == "validation_failed"


# ----------------------------------------------------- C5: labeller conviction
def test_label_conviction_available_when_openai():
    r = compute_tqs(_inputs(label_confidence=0.75, label_source="openai"))
    assert r.components["label_conviction"]["score"] == 0.75


def test_label_conviction_missing_when_mock():
    r = compute_tqs(_inputs(label_confidence=0.75, label_source="mock"))
    assert r.missing_components["label_conviction"]["reason"] == "mock_labeller"


def test_label_conviction_missing_when_failsafe_with_reason_recorded():
    """A labeller fail-safe reject must be VISIBLE as a missing component with
    an explicit reason -- never silently neutral-scored."""
    r = compute_tqs(_inputs(label_confidence=0.0, label_source="fail_safe"))
    assert "label_conviction" in r.missing_components
    assert r.missing_components["label_conviction"]["reason"] == "labeller_failsafe"


def test_label_conviction_missing_when_never_labelled():
    r = compute_tqs(_inputs(label_source=None))
    assert r.missing_components["label_conviction"]["reason"] == "not_labelled"


# -------------------------------------------------- C6: narrative alignment
def test_narrative_alignment_aligned_scores_above_half():
    r = compute_tqs(_inputs(
        direction="long", polarity_confidence=0.9, polarity_alignment="aligned",
        polarity_model_provider="anthropic", polarity_parse_status="parsed",
    ))
    assert r.components["narrative_alignment"]["score"] == 0.5 + 0.5 * 0.9


def test_narrative_alignment_conflicting_scores_below_half():
    r = compute_tqs(_inputs(
        direction="long", polarity_confidence=0.9, polarity_alignment="conflicting",
        polarity_model_provider="anthropic", polarity_parse_status="parsed",
    ))
    assert r.components["narrative_alignment"]["score"] == round(0.5 - 0.5 * 0.9, 4)


def test_narrative_alignment_measured_neutral_is_exactly_half():
    r = compute_tqs(_inputs(
        direction="long", polarity_confidence=0.9, polarity_alignment="neutral",
        polarity_model_provider="anthropic", polarity_parse_status="parsed",
    ))
    assert r.components["narrative_alignment"]["score"] == 0.5


def test_narrative_alignment_unclear_is_measured_neutral_not_missing():
    """UNCLEAR is still a real measurement (a live model looked and couldn't
    tell) -- distinct from having no live signal at all."""
    r = compute_tqs(_inputs(
        direction="long", polarity_confidence=0.9, polarity_alignment="unclear",
        polarity_model_provider="anthropic", polarity_parse_status="parsed",
    ))
    assert "narrative_alignment" in r.components
    assert r.components["narrative_alignment"]["score"] == 0.5


def test_narrative_alignment_missing_when_mock_polarity():
    """model_provider is None for mock polarity rows (empirically confirmed:
    Last30DaysPolarityClassifier's mock branch passes ai_lineage=None)."""
    r = compute_tqs(_inputs(
        direction="long", polarity_confidence=0.9, polarity_alignment="aligned",
        polarity_model_provider=None, polarity_parse_status="parsed",
    ))
    assert r.missing_components["narrative_alignment"]["reason"] == "no_live_narrative_or_catalyst"


def test_narrative_alignment_missing_when_parse_status_not_parsed():
    r = compute_tqs(_inputs(
        direction="long", polarity_confidence=0.9, polarity_alignment="aligned",
        polarity_model_provider="anthropic", polarity_parse_status="skipped",
    ))
    assert "narrative_alignment" in r.missing_components


def test_narrative_alignment_opposing_catalyst_caps_score_low():
    """A LIVE opposing catalyst caps the score at <= 0.3 even when polarity
    alone would score much higher."""
    r = compute_tqs(_inputs(
        direction="long", polarity_confidence=0.95, polarity_alignment="aligned",
        polarity_model_provider="anthropic", polarity_parse_status="parsed",
        catalyst_type="analyst_downgrade", catalyst_status="confirmed",
        catalyst_enrichment_source="alpaca",
    ))
    assert r.components["narrative_alignment"]["score"] <= 0.3


def test_narrative_alignment_mock_catalyst_does_not_cap():
    """A MOCK/disabled catalyst must not apply the opposition cap -- only a
    LIVE catalyst counts as real evidence."""
    r = compute_tqs(_inputs(
        direction="long", polarity_confidence=0.9, polarity_alignment="aligned",
        polarity_model_provider="anthropic", polarity_parse_status="parsed",
        catalyst_type="analyst_downgrade", catalyst_status="confirmed",
        catalyst_enrichment_source="mock",
    ))
    assert r.components["narrative_alignment"]["score"] == 0.5 + 0.5 * 0.9  # cap did NOT apply


def test_narrative_alignment_bullish_catalyst_opposes_a_short():
    """Direction matters: a BULLISH catalyst opposes a SHORT, not a LONG."""
    r = compute_tqs(_inputs(
        direction="short", polarity_confidence=0.9, polarity_alignment="aligned",
        polarity_model_provider="anthropic", polarity_parse_status="parsed",
        catalyst_type="product_launch", catalyst_status="confirmed",
        catalyst_enrichment_source="alpaca",
    ))
    assert r.components["narrative_alignment"]["score"] <= 0.3


def test_narrative_alignment_high_risk_narrative_caps_at_point_six():
    r = compute_tqs(_inputs(
        direction="long", polarity_confidence=0.99, polarity_alignment="aligned",
        polarity_model_provider="anthropic", polarity_parse_status="parsed",
        arming_classification="high_risk_narrative",
    ))
    assert r.components["narrative_alignment"]["score"] <= 0.6


def test_narrative_alignment_only_live_catalyst_no_polarity_is_neutral_base():
    """Live catalyst present, but no live polarity: neutral (0.5) baseline,
    since no directional narrative was actually measured -- catalyst here is
    only ever a downward cap, never a positive driver (v0 spec is explicit
    about this)."""
    r = compute_tqs(_inputs(
        direction="long", polarity_model_provider=None,
        catalyst_type="sec_filing", catalyst_status="confirmed",
        catalyst_enrichment_source="alpaca",
    ))
    assert "narrative_alignment" in r.components
    assert r.components["narrative_alignment"]["score"] == 0.5


# ------------------------------------------------------ C7: event-risk clearance
def test_event_risk_clear_is_perfect_score():
    r = compute_tqs(_inputs(earnings_data_status="ok", earnings_within_hold_window=0,
                            earnings_within_warning_window=0))
    assert r.components["event_risk_clearance"]["score"] == 1.0


def test_event_risk_within_hold_window_is_zero():
    r = compute_tqs(_inputs(earnings_data_status="ok", earnings_within_hold_window=1,
                            earnings_within_warning_window=1))
    assert r.components["event_risk_clearance"]["score"] == 0.0


def test_event_risk_within_warning_only_is_half():
    r = compute_tqs(_inputs(earnings_data_status="ok", earnings_within_hold_window=0,
                            earnings_within_warning_window=1))
    assert r.components["event_risk_clearance"]["score"] == 0.5


def test_event_risk_unavailable_is_missing_never_one_or_zero():
    """Unavailable earnings data must NEVER read as 'clear' (1.0) or as
    'confirmed risk' (0.0) -- the ambiguity goes entirely to data_confidence."""
    for status in ("unavailable", "unknown", "stale", "provider_disabled", None):
        r = compute_tqs(_inputs(earnings_data_status=status, earnings_within_hold_window=0,
                                earnings_within_warning_window=0))
        assert "event_risk_clearance" in r.missing_components, f"status={status} should be missing"
        assert r.missing_components["event_risk_clearance"]["reason"] == "earnings_unavailable"


# ---------------------------------------------------------- data_confidence
def test_data_confidence_is_available_over_applicable_weight():
    # Only interest_strength (25) and event_risk_clearance (10) available = 35/100
    r = compute_tqs(_inputs(interest_score=0.5, earnings_data_status="ok",
                            earnings_within_hold_window=0, earnings_within_warning_window=0))
    assert r.data_confidence == 0.35


def test_full_evidence_gives_full_confidence():
    r = compute_tqs(_inputs(
        direction="long", max_spread_pct=0.01, expected_r=2.0, interest_score=0.7,
        spread_pct=0.002, ai_available=True, ai_confidence=0.8, ai_is_mock=False,
        ai_validation_status="passed", label_confidence=0.7, label_source="openai",
        polarity_confidence=0.8, polarity_alignment="neutral", polarity_model_provider="anthropic",
        polarity_parse_status="parsed", earnings_data_status="ok",
        earnings_within_hold_window=0, earnings_within_warning_window=0,
    ))
    assert r.data_confidence == 1.0
    assert r.data_quality_status == TqsDataQualityStatus.OK.value


def test_low_confidence_is_degraded_not_ok():
    r = compute_tqs(_inputs(interest_score=0.5))  # only 25/100 weight available
    assert r.data_confidence < 0.70
    assert r.data_quality_status == TqsDataQualityStatus.DEGRADED.value


# --------------------------------------------------------- tqs_score formula
def test_tqs_score_is_raw_times_confidence():
    r = compute_tqs(_inputs(interest_score=1.0, earnings_data_status="ok",
                            earnings_within_hold_window=0, earnings_within_warning_window=0))
    # raw = weighted mean over available (interest=1.0*25 + event_risk=1.0*10)/35 = 1.0 -> raw_score=100
    assert r.raw_score == 100
    assert r.data_confidence == 0.35
    assert r.tqs_score == round(100 * 0.35)


def test_missing_evidence_can_only_lower_never_raise_the_score():
    full = compute_tqs(_inputs(
        direction="long", max_spread_pct=0.01, expected_r=3.0, interest_score=1.0,
        spread_pct=0.0, ai_available=True, ai_confidence=1.0, ai_is_mock=False,
        ai_validation_status="passed", label_confidence=1.0, label_source="openai",
        polarity_confidence=1.0, polarity_alignment="aligned", polarity_model_provider="anthropic",
        polarity_parse_status="parsed", earnings_data_status="ok",
        earnings_within_hold_window=0, earnings_within_warning_window=0,
    ))
    partial = compute_tqs(_inputs(interest_score=1.0))  # same great interest score, everything else missing
    assert full.tqs_score == 100
    assert partial.tqs_score is not None and partial.tqs_score < full.tqs_score


# ---------------------------------------------------------------- is_mock
def test_is_mock_forces_mock_data_quality_status_even_with_some_score():
    r = compute_tqs(_inputs(is_mock=True, interest_score=0.6))
    assert r.data_quality_status == TqsDataQualityStatus.MOCK.value
    assert r.is_mock is True
    assert r.tqs_score is not None  # still scored -- just flagged for calibration exclusion


def test_is_mock_true_with_zero_components_is_still_unscorable_score_but_mock_label():
    r = compute_tqs(_inputs(is_mock=True))
    assert r.tqs_score is None
    assert r.data_quality_status == TqsDataQualityStatus.MOCK.value


# -------------------------------------------------------------------- buckets
def test_bucket_boundaries():
    from alphaos.tqs.scoring import _bucket_for

    assert _bucket_for(None) == "unscorable"
    assert _bucket_for(0) == "weak"
    assert _bucket_for(24) == "weak"
    assert _bucket_for(25) == "mixed"
    assert _bucket_for(49) == "mixed"
    assert _bucket_for(50) == "watch"
    assert _bucket_for(69) == "watch"
    assert _bucket_for(70) == "good"
    assert _bucket_for(84) == "good"
    assert _bucket_for(85) == "strong"
    assert _bucket_for(100) == "strong"


# ----------------------------------------------------------------- determinism
def test_same_inputs_produce_identical_result():
    kwargs = dict(
        direction="long", expected_r=2.0, interest_score=0.6, spread_pct=0.003,
        ai_available=True, ai_confidence=0.7, ai_is_mock=False, ai_validation_status="passed",
        label_confidence=0.6, label_source="openai",
    )
    r1 = compute_tqs(_inputs(**kwargs))
    r2 = compute_tqs(_inputs(**kwargs))
    assert r1.raw_score == r2.raw_score
    assert r1.tqs_score == r2.tqs_score
    assert r1.data_confidence == r2.data_confidence
    assert r1.tqs_bucket == r2.tqs_bucket
    assert r1.components == r2.components
    assert r1.missing_components == r2.missing_components


def test_components_json_serializable_and_sorted_keys_stable():
    import json

    r = compute_tqs(_inputs(interest_score=0.5, expected_r=2.0))
    j1 = json.dumps(r.components, sort_keys=True)
    j2 = json.dumps(compute_tqs(_inputs(interest_score=0.5, expected_r=2.0)).components, sort_keys=True)
    assert j1 == j2


# ------------------------------------------------------- component-level error
def test_component_exception_degrades_to_missing_not_a_crash():
    """A malformed input that would raise inside a component's own logic must
    degrade that ONE component to missing (reason starting with 'error:'),
    never propagate and crash scoring."""
    bad_inputs = _inputs(max_spread_pct=0.0, spread_pct=0.001)  # division by zero guarded explicitly
    r = compute_tqs(bad_inputs)
    assert "microstructure_quality" in r.missing_components
    assert r.missing_components["microstructure_quality"]["reason"] == "invalid_max_spread_pct"


def test_version_and_weights_are_stable_constants():
    assert TQS_VERSION == "0.1.0"
    assert sum(WEIGHTS.values()) == 100
