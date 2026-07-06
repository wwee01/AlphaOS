"""LLM-derived last30days narrative polarity (Roadmap 2.7).

Reads last30days CLUSTER EVIDENCE for one candidate and classifies the recent
narrative as bullish / bearish / neutral / unclear, with confidence, source
coverage quality, and a narrative-driver type (fundamental / catalyst /
social_momentum / meme_hype / squeeze_risk / mixed / unclear).

CONTEXT, not execution authority. Polarity can ARM an override upgrade ONLY when
it is directionally aligned, high-confidence, well-covered, and not contradicted
by the official catalyst — and even then it never bypasses the risk / freshness /
spread / sizing gates or manual approval, and never submits an order. Hype / meme
/ social-momentum / squeeze narratives are NOT auto-suppressed: they are flagged
as HIGH-RISK and may arm only as ``high_risk_narrative`` (manual-only, warned).

The model is a CLASSIFIER. The arming decision is recomputed DETERMINISTICALLY on
the AlphaOS side (``_decide_arming``) from the model's classification + config —
never trusted blindly. Fails safe to ``unclear`` / non-arming on any error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from alphaos.constants import (
    HIGH_RISK_NARRATIVE_TYPES,
    HIGH_RISK_NARRATIVE_WARNING,
    POLARITY_PROMPT_VERSION,
    SOURCE_COVERAGE_RANK,
    ArmingClassification,
    DirectionAlignment,
    HypeRisk,
    NarrativeDriverType,
    PolarityParseStatus,
    Severity,
    SentimentLabel,
    SourceCoverageQuality,
    TradeDirection,
)
from alphaos import lineage
from alphaos.util import structured_json
from alphaos.util.ids import new_id

HTTP_TIMEOUT = 40

POLARITY_SYSTEM_PROMPT = (
    "You classify the RECENT (last ~30 days) narrative around a stock ticker from "
    "social/community/web evidence. You are NOT deciding whether to trade — you only "
    "classify narrative polarity and the narrative-driver type. Rules:\n"
    "- Do NOT equate popularity or loudness with investability.\n"
    "- Hype, meme behaviour, social momentum, and squeeze dynamics CAN be short-term "
    "drivers, but you MUST classify them as a high-risk narrative-driver type "
    "(social_momentum / meme_hype / squeeze_risk), not as fundamental evidence.\n"
    "- Separate bullish/bearish narrative from official catalyst strength.\n"
    "- If evidence is weak, mixed, irrelevant, meme-like, or unclear, choose "
    "'unclear' or 'neutral'. Be conservative.\n"
    "- Return a single JSON object ONLY. No markdown, no prose outside JSON."
)

# Strict output keys we ask for (parsed defensively; missing -> safe defaults).
POLARITY_OUTPUT_KEYS = [
    "sentiment_label", "sentiment_score", "confidence", "source_coverage_quality",
    "narrative_driver_type", "hype_or_manipulation_risk", "official_catalyst_conflict",
    "reasoning_summary", "evidence_items_used",
]


@dataclass
class PolarityEvidence:
    candidate_id: str
    symbol: str
    direction: str                      # long | short | unknown
    structure_hint: Optional[str]
    provider: Optional[str]
    cluster_titles: list = field(default_factory=list)
    cluster_summaries: list = field(default_factory=list)
    source_coverage: list = field(default_factory=list)
    source_coverage_count: int = 0
    catalyst_summary: Optional[str] = None
    eval_decision: Optional[str] = None
    label_decision: Optional[str] = None

    @property
    def has_evidence(self) -> bool:
        return bool(self.cluster_titles)


@dataclass
class PolarityResult:
    candidate_id: str
    symbol: str
    provider: Optional[str]
    model: str
    prompt_template_version: str
    sentiment_label: str
    sentiment_score: Optional[float]
    confidence: float
    direction_alignment: str
    source_coverage_quality: str
    narrative_driver_type: str
    hype_or_manipulation_risk: str
    requires_user_attention: bool
    official_catalyst_conflict: bool
    should_arm_override: bool
    arming_classification: str
    warning_message: str
    reasoning_summary: str
    evidence_items: list
    raw_json: Optional[dict]
    parse_status: str
    # PR4: measurement-only AI-call lineage. None for mock/skipped/error paths
    # (no real prompt was sent); populated by _live_classify for the real call.
    model_provider: Optional[str] = None
    prompt_hash: Optional[str] = None
    system_prompt_hash: Optional[str] = None
    # PR9.5: real token usage for cost accounting. None for mock/skipped/error
    # paths (no real API call was made); populated by _live_classify.
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None

    @property
    def is_high_risk(self) -> bool:
        return self.arming_classification == ArmingClassification.HIGH_RISK_NARRATIVE.value

    def to_row(self, scan_batch_id: Optional[str] = None, packet_id: Optional[str] = None) -> dict:
        return {
            "polarity_id": new_id("pol"),
            "candidate_id": self.candidate_id,
            "packet_id": packet_id,
            "scan_batch_id": scan_batch_id,
            "symbol": self.symbol,
            "provider": self.provider,
            "model": self.model,
            "prompt_template_version": self.prompt_template_version,
            "sentiment_label": self.sentiment_label,
            "sentiment_score": self.sentiment_score,
            "confidence": self.confidence,
            "direction_alignment": self.direction_alignment,
            "source_coverage_quality": self.source_coverage_quality,
            "narrative_driver_type": self.narrative_driver_type,
            "hype_or_manipulation_risk": self.hype_or_manipulation_risk,
            "requires_user_attention": 1 if self.requires_user_attention else 0,
            "official_catalyst_conflict": 1 if self.official_catalyst_conflict else 0,
            "should_arm_override": 1 if self.should_arm_override else 0,
            "arming_classification": self.arming_classification,
            "warning_message": self.warning_message,
            "reasoning_summary": self.reasoning_summary,
            "evidence_json": self.evidence_items,
            "raw_response_json": self.raw_json,
            "parse_status": self.parse_status,
            "model_provider": self.model_provider,
            "prompt_hash": self.prompt_hash,
            "system_prompt_hash": self.system_prompt_hash,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


def _extract_usage(resp) -> Optional[dict]:
    """PR9.5: best-effort token usage from an OpenAI ChatCompletion response,
    for real cost accounting (cost_guard previously only counted
    openai_evaluations, undercounting real AI spend 2-3x). Returns None if
    unavailable -- cost accounting must never affect or break the
    classification it's measuring."""
    usage = getattr(resp, "usage", None)
    if usage is None:
        return None
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }


def _enum(value, allowed: set, default: str) -> str:
    v = str(value or "").strip().lower()
    return v if v in allowed else default


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


_SENTIMENTS = {SentimentLabel.BULLISH.value, SentimentLabel.BEARISH.value,
               SentimentLabel.NEUTRAL.value, SentimentLabel.UNCLEAR.value}
_COVERAGE = {q.value for q in SourceCoverageQuality}
_DRIVERS = {d.value for d in NarrativeDriverType}
_HYPE = {h.value for h in HypeRisk}


class Last30DaysPolarityClassifier:
    def __init__(self, settings, journal=None):
        self.s = settings
        self.journal = journal
        self.model = settings.last30days_polarity_model
        self.use_mock = settings.is_mock or not settings.has_openai_key

    # ---------------------------------------------------------------- public
    def classify(self, ev: PolarityEvidence) -> PolarityResult:
        """Classify narrative polarity. NEVER raises. Returns a non-arming
        ``skipped`` result when disabled or when there is no evidence."""
        if not self.s.last30days_polarity_enabled:
            return self._skipped(ev, PolarityParseStatus.SKIPPED.value)
        if not ev.has_evidence:
            return self._skipped(ev, PolarityParseStatus.SKIPPED.value)
        try:
            parsed, raw, status, ai_lineage, usage = (self._mock_classify(ev) if self.use_mock
                                                       else self._live_classify(ev))
        except Exception as exc:  # fail-safe: never crash the scan
            if self.journal is not None:
                self.journal.log_system_event(
                    Severity.WARNING, "polarity",
                    f"polarity classify failed for {ev.symbol}; failing safe.", {"error": str(exc)},
                )
            return self._error(ev, PolarityParseStatus.MODEL_ERROR.value)
        return self._build(ev, parsed, raw, status, ai_lineage, usage)

    # --------------------------------------------------- deterministic arming
    @staticmethod
    def _alignment(sentiment: str, direction: str) -> str:
        is_long = direction == TradeDirection.LONG.value
        is_short = direction == TradeDirection.SHORT.value
        if not (is_long or is_short):
            return DirectionAlignment.UNCLEAR.value
        if sentiment == SentimentLabel.BULLISH.value:
            return DirectionAlignment.ALIGNED.value if is_long else DirectionAlignment.CONFLICTING.value
        if sentiment == SentimentLabel.BEARISH.value:
            return DirectionAlignment.ALIGNED.value if is_short else DirectionAlignment.CONFLICTING.value
        if sentiment == SentimentLabel.NEUTRAL.value:
            return DirectionAlignment.NEUTRAL.value
        return DirectionAlignment.UNCLEAR.value

    def _decide_arming(self, parsed: dict, direction: str) -> tuple:
        """Pure, deterministic safety decision: (alignment, should_arm,
        arming_classification). The model's own should_arm is ignored — AlphaOS
        decides from the classification + config thresholds."""
        s = self.s
        sentiment = parsed["sentiment_label"]
        conf = parsed["confidence"]
        cov = parsed["source_coverage_quality"]
        driver = parsed["narrative_driver_type"]
        hype = parsed["hype_or_manipulation_risk"]
        conflict = parsed["official_catalyst_conflict"]
        alignment = self._alignment(sentiment, direction)

        min_cov_rank = SOURCE_COVERAGE_RANK.get(s.last30days_polarity_min_source_coverage,
                                                SOURCE_COVERAGE_RANK[SourceCoverageQuality.MEDIUM.value])
        gate = (
            s.last30days_polarity_enabled
            and s.last30days_polarity_arming_allowed
            and alignment == DirectionAlignment.ALIGNED.value
            and conf >= s.last30days_polarity_min_confidence
            and SOURCE_COVERAGE_RANK.get(cov, 0) >= min_cov_rank
            and not conflict
        )
        if not gate:
            return alignment, False, ArmingClassification.NON_ARMING.value
        high_risk = driver in HIGH_RISK_NARRATIVE_TYPES or hype in (HypeRisk.MEDIUM.value, HypeRisk.HIGH.value)
        cls = (ArmingClassification.HIGH_RISK_NARRATIVE.value if high_risk
               else ArmingClassification.NORMAL_DRIVER.value)
        return alignment, True, cls

    # --------------------------------------------------------------- builders
    def _build(self, ev: PolarityEvidence, parsed: dict, raw, status: str,
               ai_lineage: Optional[dict] = None, usage: Optional[dict] = None) -> PolarityResult:
        alignment, should_arm, cls = self._decide_arming(parsed, ev.direction)
        high_risk = cls == ArmingClassification.HIGH_RISK_NARRATIVE.value
        conflict = bool(parsed["official_catalyst_conflict"])
        attention = bool(high_risk or conflict
                         or parsed["hype_or_manipulation_risk"] in (HypeRisk.MEDIUM.value, HypeRisk.HIGH.value))
        warning = HIGH_RISK_NARRATIVE_WARNING if high_risk else ""
        ai_lineage = ai_lineage or {}
        usage = usage or {}
        return PolarityResult(
            candidate_id=ev.candidate_id, symbol=ev.symbol, provider=ev.provider, model=self.model,
            prompt_template_version=POLARITY_PROMPT_VERSION,
            sentiment_label=parsed["sentiment_label"], sentiment_score=parsed["sentiment_score"],
            confidence=parsed["confidence"], direction_alignment=alignment,
            source_coverage_quality=parsed["source_coverage_quality"],
            narrative_driver_type=parsed["narrative_driver_type"],
            hype_or_manipulation_risk=parsed["hype_or_manipulation_risk"],
            requires_user_attention=attention, official_catalyst_conflict=conflict,
            should_arm_override=should_arm, arming_classification=cls, warning_message=warning,
            reasoning_summary=parsed["reasoning_summary"], evidence_items=parsed["evidence_items_used"],
            raw_json=raw, parse_status=status,
            model_provider=ai_lineage.get("model_provider"),
            prompt_hash=ai_lineage.get("prompt_hash"),
            system_prompt_hash=ai_lineage.get("system_prompt_hash"),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
        )

    def _empty_parsed(self) -> dict:
        return {
            "sentiment_label": SentimentLabel.UNCLEAR.value, "sentiment_score": None, "confidence": 0.0,
            "source_coverage_quality": SourceCoverageQuality.LOW.value,
            "narrative_driver_type": NarrativeDriverType.UNCLEAR.value,
            "hype_or_manipulation_risk": HypeRisk.NONE.value, "official_catalyst_conflict": False,
            "reasoning_summary": "", "evidence_items_used": [],
        }

    def _skipped(self, ev, status) -> PolarityResult:
        return self._build(ev, self._empty_parsed(), None, status)

    def _error(self, ev, status) -> PolarityResult:
        return self._build(ev, self._empty_parsed(), None, status)

    # ----------------------------------------------------------------- parse
    def _coerce(self, obj: dict) -> dict:
        """Coerce a raw model object to a safe, fully-typed parsed dict."""
        conf = _f(obj.get("confidence"), 0.0) or 0.0
        return {
            "sentiment_label": _enum(obj.get("sentiment_label"), _SENTIMENTS, SentimentLabel.UNCLEAR.value),
            "sentiment_score": _f(obj.get("sentiment_score"), None),
            "confidence": max(0.0, min(1.0, conf)),
            "source_coverage_quality": _enum(obj.get("source_coverage_quality"), _COVERAGE,
                                             SourceCoverageQuality.LOW.value),
            "narrative_driver_type": _enum(obj.get("narrative_driver_type"), _DRIVERS,
                                           NarrativeDriverType.UNCLEAR.value),
            "hype_or_manipulation_risk": _enum(obj.get("hype_or_manipulation_risk"), _HYPE, HypeRisk.NONE.value),
            "official_catalyst_conflict": bool(obj.get("official_catalyst_conflict", False)),
            "reasoning_summary": str(obj.get("reasoning_summary") or "")[:400],
            "evidence_items_used": obj.get("evidence_items_used") or [],
        }

    # ------------------------------------------------------------------- live
    def _live_classify(self, ev: PolarityEvidence):  # pragma: no cover - live, real API
        from openai import OpenAI

        client = OpenAI(api_key=self.s.openai_api_key)
        user_prompt = self._build_user_prompt(ev)
        # PR4: measurement-only AI-call lineage (model provider + content hashes
        # of the actual prompt sent, never the raw prompt body).
        ai_lineage = lineage.ai_call_lineage(
            provider="openai", prompt=user_prompt, system_prompt=POLARITY_SYSTEM_PROMPT,
        )
        resp = client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            # gpt-5.x rejects max_tokens; max_completion_tokens is the supported param.
            max_completion_tokens=600,
            messages=[
                {"role": "system", "content": POLARITY_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            timeout=HTTP_TIMEOUT,
        )
        usage = _extract_usage(resp)  # PR9.5: real cost accounting
        content = resp.choices[0].message.content
        obj = structured_json.parse_json_object(content)   # raises on invalid JSON
        return self._coerce(obj), obj, PolarityParseStatus.PARSED.value, ai_lineage, usage

    @staticmethod
    def _build_user_prompt(ev: PolarityEvidence) -> str:
        import json
        payload = {
            "symbol": ev.symbol,
            "candidate_direction": ev.direction,
            "deterministic_structure": ev.structure_hint,
            "last30days_provider": ev.provider,
            "source_coverage": ev.source_coverage,
            "source_coverage_count": ev.source_coverage_count,
            "cluster_titles": ev.cluster_titles[:12],
            "cluster_summaries": ev.cluster_summaries[:6],
            "official_catalyst_summary": ev.catalyst_summary,
            "ai_eval_decision": ev.eval_decision,
            "ai_label_decision": ev.label_decision,
        }
        return (
            "Classify the recent narrative for this candidate. Return ONLY a JSON object "
            "with keys: sentiment_label (bullish|bearish|neutral|unclear), sentiment_score "
            "(-1..1), confidence (0..1), source_coverage_quality (low|medium|high), "
            "narrative_driver_type (fundamental|catalyst|social_momentum|meme_hype|"
            "squeeze_risk|mixed|unclear), hype_or_manipulation_risk (none|low|medium|high), "
            "official_catalyst_conflict (bool), reasoning_summary (short string), "
            "evidence_items_used (list of {title, source, relevance}).\n\nEVIDENCE:\n"
            + json.dumps(payload, default=str)
        )

    # ------------------------------------------------------------------- mock
    _BULL = ("bull", "buy", "undervalued", "upgrade", "beat", "surge", "rally", "breakout",
             "strong", "printer", "moon", "rocket", "up ", "soar", "record")
    _BEAR = ("bear", "sell", "downgrade", "miss", "crash", "plunge", "weak", "lawsuit",
             "probe", "cut", "fraud", "drop", "fall", "warning")
    _HYPEW = ("🚀", "moon", "yolo", "meme", "squeeze", "printer", "rocket", "diamond", "to the moon")

    def _mock_classify(self, ev: PolarityEvidence):
        """Deterministic, offline classification from cluster titles (keyword based).
        Lets tests/validation exercise polarity without a real API call."""
        text = " ".join(ev.cluster_titles).lower()
        bull = sum(1 for w in self._BULL if w in text)
        bear = sum(1 for w in self._BEAR if w in text)
        hype = sum(1 for w in self._HYPEW if w in text)
        n = ev.source_coverage_count or len(ev.source_coverage)

        if bull > bear:
            sentiment = SentimentLabel.BULLISH.value
        elif bear > bull:
            sentiment = SentimentLabel.BEARISH.value
        elif bull == bear and bull > 0:
            sentiment = SentimentLabel.NEUTRAL.value
        else:
            sentiment = SentimentLabel.UNCLEAR.value

        coverage = (SourceCoverageQuality.HIGH.value if n >= 3
                    else SourceCoverageQuality.MEDIUM.value if n == 2
                    else SourceCoverageQuality.LOW.value)
        if hype >= 2:
            driver, hyperisk = NarrativeDriverType.MEME_HYPE.value, HypeRisk.HIGH.value
        elif hype == 1:
            driver, hyperisk = NarrativeDriverType.SOCIAL_MOMENTUM.value, HypeRisk.MEDIUM.value
        elif sentiment in (SentimentLabel.BULLISH.value, SentimentLabel.BEARISH.value):
            driver, hyperisk = NarrativeDriverType.MIXED.value, HypeRisk.LOW.value
        else:
            driver, hyperisk = NarrativeDriverType.UNCLEAR.value, HypeRisk.NONE.value

        margin = abs(bull - bear)
        conf = round(min(0.9, 0.45 + 0.12 * margin + (0.1 if n >= 2 else 0.0)), 3) if sentiment in (
            SentimentLabel.BULLISH.value, SentimentLabel.BEARISH.value) else 0.3
        score = round((bull - bear) / max(1, bull + bear), 3) if (bull + bear) else 0.0
        obj = {
            "sentiment_label": sentiment, "sentiment_score": score, "confidence": conf,
            "source_coverage_quality": coverage, "narrative_driver_type": driver,
            "hype_or_manipulation_risk": hyperisk, "official_catalyst_conflict": False,
            "reasoning_summary": f"MOCK polarity: bull={bull} bear={bear} hype={hype} cover={n}",
            "evidence_items_used": [{"title": t[:80], "source": "mock", "relevance": "medium"}
                                    for t in ev.cluster_titles[:3]],
        }
        return self._coerce(obj), {"mock": True, **obj}, PolarityParseStatus.PARSED.value, None, None
