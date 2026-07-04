"""TQS v0 -- pure scoring formula (PR7).

TQS (Trade Quality Score) is a SHADOW-ONLY, measurement-only composite:

* an attention-worthiness RANKING signal, not a probability, not expected
  return, not a sizing signal, not an approval/gating signal;
* a falsifiable hypothesis to be compared, later, against
  candidate_outcomes/trade_outcomes -- not an assumption baked into behavior.

NO DECISION PATH MAY READ THIS MODULE'S OUTPUT. TQS is computed strictly
AFTER a scan batch's decisions are already committed (see
alphaos/tqs/batch.py's call site inside orchestrator.run_scan_once), so it
cannot influence what it measures by construction, not merely by discipline.
Grep for "tqs"/"alphaos.tqs" in alphaos/risk/, alphaos/approval.py, and every
orchestrator decide/approve/execute method should always return nothing.

Everything in this module is a PURE function: no I/O, no DB access, no clock
reads, no RNG. All inputs are plain values already extracted from the
journal by alphaos/tqs/inputs.py (the "enricher" layer, which does the I/O),
mirroring the established enricher/pure-compute split used elsewhere in this
codebase (alphaos/earnings/earnings_provider.py vs earnings_enricher.py's
compute_proximity_flags(), alphaos/news/catalyst_enricher.py).

Component weights, normalization rules, and bucket thresholds are CODE
CONSTANTS tied to TQS_VERSION -- deliberately not settings. Any change to any
of them is a new version: old rows keep their original tqs_version forever
(no retro-rescoring), and comparing scores across versions is invalid by
definition. See TQS_VERSION docstring below.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from alphaos.constants import (
    ArmingClassification,
    BEARISH_CATALYST_TYPES,
    BULLISH_CATALYST_TYPES,
    CatalystStatus,
    DirectionAlignment,
    EarningsDataStatus,
    EnrichmentSource,
    LabelSource,
    PolarityParseStatus,
    TqsBucket,
    TqsDataQualityStatus,
    TradeDirection,
)

# TQS_VERSION: bump on ANY change to weights, normalization, or bucket
# thresholds. Rows are never retro-rescored -- a version change only affects
# rows computed going forward; existing rows keep the version they were
# computed under so cross-version comparison is never silently invalid.
TQS_VERSION = "0.1.0"

# Component weights (sum = 100). Not env-tunable -- see module docstring.
WEIGHTS = {
    "reward_risk_geometry": 15,
    "interest_strength": 25,
    "microstructure_quality": 10,
    "ai_conviction": 20,
    "label_conviction": 10,
    "narrative_alignment": 10,
    "event_risk_clearance": 10,
}
assert sum(WEIGHTS.values()) == 100

# Source field documented per component -- purely a static, hardcoded string
# for explainability in components_json. Never derived from user/AI content.
_SOURCES = {
    "reward_risk_geometry": "openai_evaluations.expected_r / trade_proposals.expected_r",
    "interest_strength": "candidates.interest_score",
    "microstructure_quality": "price_snapshots.spread_pct (via candidates.price_snapshot_id)",
    "ai_conviction": "openai_evaluations.confidence",
    "label_conviction": "candidates.label_confidence",
    "narrative_alignment": "last30days_polarity.confidence + candidate_catalysts opposition check",
    "event_risk_clearance": "candidates.earnings_within_hold_window/earnings_within_warning_window",
}

# Bucket thresholds (score >= threshold -> label), checked highest-first.
# v0-arbitrary: chosen for digest readability, not calibrated against
# outcomes -- part of the version, not a tuned tier system.
_BUCKET_THRESHOLDS = (
    (85, TqsBucket.STRONG.value),
    (70, TqsBucket.GOOD.value),
    (50, TqsBucket.WATCH.value),
    (25, TqsBucket.MIXED.value),
    (0, TqsBucket.WEAK.value),
)

# data_confidence at/above this reads as 'ok'; below reads as 'degraded'.
_DEGRADED_BELOW_CONFIDENCE = 0.70


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


@dataclass
class TqsComponentInputs:
    """Plain, already-extracted values for ONE candidate or ONE proposal.
    Built by alphaos/tqs/inputs.py (does the DB reads); consumed here (does
    none). Every field is Optional/defaulted so a partially-populated input
    (the normal case -- most candidates never reach every enrichment stage)
    degrades individual components to 'missing' rather than raising."""

    symbol: str
    direction: Optional[str] = None
    max_spread_pct: float = 0.01
    is_mock: bool = False

    # C1: reward:risk geometry
    expected_r: Optional[float] = None

    # C2: interest strength
    interest_score: Optional[float] = None

    # C3: microstructure quality
    spread_pct: Optional[float] = None

    # C4: AI evaluator conviction
    ai_available: bool = False
    ai_confidence: Optional[float] = None
    ai_is_mock: bool = False
    ai_validation_status: Optional[str] = None

    # C5: labeller conviction
    label_confidence: Optional[float] = None
    label_source: Optional[str] = None  # 'openai' | 'mock' | 'fail_safe' | None

    # C6: narrative / catalyst direction alignment
    polarity_confidence: Optional[float] = None
    polarity_alignment: Optional[str] = None       # DirectionAlignment value
    polarity_model_provider: Optional[str] = None  # None => mock/unavailable
    polarity_parse_status: Optional[str] = None
    catalyst_type: Optional[str] = None
    catalyst_status: Optional[str] = None
    catalyst_enrichment_source: Optional[str] = None
    arming_classification: Optional[str] = None

    # C7: event-risk clearance
    earnings_data_status: Optional[str] = None
    earnings_within_hold_window: Optional[int] = None
    earnings_within_warning_window: Optional[int] = None


@dataclass
class TqsResult:
    raw_score: Optional[int]           # 0-100, or None if unscorable
    data_confidence: float             # 0.00-1.00
    tqs_score: Optional[int]           # 0-100, or None if unscorable
    tqs_bucket: str
    data_quality_status: str
    is_mock: bool
    components: dict = field(default_factory=dict)          # name -> {score, weight, source}
    missing_components: dict = field(default_factory=dict)  # name -> {weight, reason, source}


# ------------------------------------------------------------- components
# Each returns (score_0_to_1_or_None, missing_reason_or_None). Never raises --
# compute_tqs() wraps each call defensively regardless, as a second layer.

def _c_reward_risk_geometry(i: TqsComponentInputs):
    if i.expected_r is None:
        return None, "missing_expected_r"
    return _clamp((i.expected_r - 1.0) / 2.0), None


def _c_interest_strength(i: TqsComponentInputs):
    if i.interest_score is None:
        return None, "missing_interest_score"
    return _clamp(i.interest_score), None


def _c_microstructure_quality(i: TqsComponentInputs):
    if i.spread_pct is None or i.spread_pct < 0:
        return None, "missing_spread_pct"
    if not i.max_spread_pct or i.max_spread_pct <= 0:
        return None, "invalid_max_spread_pct"
    return 1.0 - _clamp(i.spread_pct / i.max_spread_pct), None


def _c_ai_conviction(i: TqsComponentInputs):
    if not i.ai_available:
        return None, "no_evaluation"
    if i.ai_is_mock:
        return None, "mock_ai"
    if i.ai_validation_status not in (None, "", "passed"):
        return None, "validation_failed"
    if i.ai_confidence is None:
        return None, "missing_confidence"
    return _clamp(i.ai_confidence), None


def _c_label_conviction(i: TqsComponentInputs):
    if i.label_source is None:
        return None, "not_labelled"
    if i.label_source == LabelSource.MOCK.value:
        return None, "mock_labeller"
    if i.label_source == LabelSource.FAIL_SAFE.value:
        return None, "labeller_failsafe"
    if i.label_confidence is None:
        return None, "missing_label_confidence"
    return _clamp(i.label_confidence), None


def _catalyst_is_live(i: TqsComponentInputs) -> bool:
    return (
        i.catalyst_enrichment_source not in (None, EnrichmentSource.MOCK.value,
                                             EnrichmentSource.DISABLED.value,
                                             EnrichmentSource.NONE.value)
        and i.catalyst_status in (CatalystStatus.CONFIRMED.value, CatalystStatus.POSSIBLE.value)
    )


def _c_narrative_alignment(i: TqsComponentInputs):
    live_polarity = (i.polarity_model_provider is not None
                     and i.polarity_parse_status == PolarityParseStatus.PARSED.value)
    live_catalyst = _catalyst_is_live(i)
    if not live_polarity and not live_catalyst:
        return None, "no_live_narrative_or_catalyst"

    if live_polarity:
        conf = _clamp(i.polarity_confidence if i.polarity_confidence is not None else 0.0)
        if i.polarity_alignment == DirectionAlignment.ALIGNED.value:
            base = 0.5 + 0.5 * conf
        elif i.polarity_alignment == DirectionAlignment.CONFLICTING.value:
            base = 0.5 - 0.5 * conf
        else:  # NEUTRAL or UNCLEAR -- measured, just not directional
            base = 0.5
    else:
        base = 0.5  # only catalyst live: no directional narrative measured

    if live_catalyst and i.catalyst_type:
        is_long = (i.direction or TradeDirection.LONG.value) != TradeDirection.SHORT.value
        opposing = (i.catalyst_type in BEARISH_CATALYST_TYPES) if is_long \
            else (i.catalyst_type in BULLISH_CATALYST_TYPES)
        if opposing:
            base = min(base, 0.3)

    if i.arming_classification == ArmingClassification.HIGH_RISK_NARRATIVE.value:
        base = min(base, 0.6)

    return _clamp(base), None


def _c_event_risk_clearance(i: TqsComponentInputs):
    if i.earnings_data_status != EarningsDataStatus.OK.value:
        return None, "earnings_unavailable"
    if i.earnings_within_hold_window:
        return 0.0, None
    if i.earnings_within_warning_window:
        return 0.5, None
    return 1.0, None


_COMPONENT_FUNCS = {
    "reward_risk_geometry": _c_reward_risk_geometry,
    "interest_strength": _c_interest_strength,
    "microstructure_quality": _c_microstructure_quality,
    "ai_conviction": _c_ai_conviction,
    "label_conviction": _c_label_conviction,
    "narrative_alignment": _c_narrative_alignment,
    "event_risk_clearance": _c_event_risk_clearance,
}


def _bucket_for(tqs_score: Optional[int]) -> str:
    if tqs_score is None:
        return TqsBucket.UNSCORABLE.value
    for threshold, label in _BUCKET_THRESHOLDS:
        if tqs_score >= threshold:
            return label
    return TqsBucket.WEAK.value  # pragma: no cover -- 0 is always covered above


def compute_tqs(inputs: TqsComponentInputs) -> TqsResult:
    """Deterministic, pure. Never raises -- any component whose computation
    errors degrades to missing (reason 'error:<ExceptionType>') rather than
    propagating; the caller (batch.py) logs those specifically. Never
    fabricates a score when zero components are available."""
    components: dict = {}
    missing: dict = {}
    for name, weight in WEIGHTS.items():
        try:
            score, reason = _COMPONENT_FUNCS[name](inputs)
        except Exception as exc:  # defensive second layer; components are pure above
            score, reason = None, f"error:{exc.__class__.__name__}"
        if score is None:
            missing[name] = {"weight": weight, "reason": reason or "unknown", "source": _SOURCES[name]}
        else:
            components[name] = {"score": round(score, 4), "weight": weight, "source": _SOURCES[name]}

    applicable_weight = sum(WEIGHTS.values())
    available_weight = sum(c["weight"] for c in components.values())
    data_confidence = round(available_weight / applicable_weight, 2) if applicable_weight else 0.0

    if available_weight == 0:
        raw_score = None
        tqs_score = None
    else:
        raw = sum(c["score"] * c["weight"] for c in components.values()) / available_weight
        raw_score = round(raw * 100)
        tqs_score = round(raw_score * data_confidence)

    tqs_bucket = _bucket_for(tqs_score)

    if inputs.is_mock:
        data_quality_status = TqsDataQualityStatus.MOCK.value
    elif available_weight == 0:
        data_quality_status = TqsDataQualityStatus.UNSCORABLE.value
    elif data_confidence >= _DEGRADED_BELOW_CONFIDENCE:
        data_quality_status = TqsDataQualityStatus.OK.value
    else:
        data_quality_status = TqsDataQualityStatus.DEGRADED.value

    return TqsResult(
        raw_score=raw_score, data_confidence=data_confidence, tqs_score=tqs_score,
        tqs_bucket=tqs_bucket, data_quality_status=data_quality_status, is_mock=inputs.is_mock,
        components=components, missing_components=missing,
    )
