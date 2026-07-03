"""OpenAI primary scoring engine — v1 NO-NEWS mode.

The active v1 playbook is *momentum continuation (no-news baseline)*. The model
evaluates on price/volume/structure only and must NOT invent a catalyst:
* output carries the sentinels ``catalyst='not_available_v1'``,
  ``news_status='disabled_v1'``, ``news_sources=[]``,
* output is validated; any invented/inferred catalyst => the evaluation is
  rejected and marked ``invented_catalyst_in_no_news_mode``.

Mock (no key / offline) produces a deterministic, schema-valid no-news
evaluation. Live (key present) calls OpenAI with a JSON-object response format
and the no-news prompt, then validates defensively.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from alphaos.ai import prompt_templates as pt
from alphaos.ai.validation import enforce_no_news_sentinels, validate_no_news_eval
from alphaos import lineage
from alphaos.constants import (
    CATALYST_NOT_AVAILABLE_V1,
    Decision,
    NEWS_STATUS_DISABLED_V1,
    ReasonCode,
    Severity,
    TradeDirection,
)
from alphaos.util import structured_json
from alphaos.util.ids import new_id

HTTP_TIMEOUT = 30
PROPOSE_MOMENTUM_THRESHOLD = 0.40


@dataclass
class OpenAIEvaluation:
    eval_id: str
    candidate_id: str
    symbol: str
    model: str
    direction: str
    entry: Optional[float]
    stop: Optional[float]
    target: Optional[float]
    max_holding_days: Optional[int]
    expected_r: Optional[float]
    confidence: Optional[float]
    decision: str
    reasoning_summary: str
    news_sources: list = field(default_factory=list)
    data_freshness_status: str = "usable"
    catalyst_type: Optional[str] = CATALYST_NOT_AVAILABLE_V1
    news_status: str = NEWS_STATUS_DISABLED_V1
    sentiment: Optional[str] = None
    risk_flags: list = field(default_factory=list)
    validation_status: str = "passed"
    raw: Optional[dict] = None
    is_mock: bool = False
    # PR4: measurement-only AI-call lineage. None for mock/rejection paths
    # (no real prompt was sent); populated by _live_eval for the real API call.
    model_provider: Optional[str] = None
    prompt_hash: Optional[str] = None
    system_prompt_hash: Optional[str] = None

    def to_row(self) -> dict:
        return {
            "eval_id": self.eval_id,
            "candidate_id": self.candidate_id,
            "symbol": self.symbol,
            "model": self.model,
            "direction": self.direction,
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "max_holding_days": self.max_holding_days,
            "expected_r": self.expected_r,
            "confidence": self.confidence,
            "decision": self.decision,
            "reasoning_summary": self.reasoning_summary,
            "news_sources_json": self.news_sources,
            "data_freshness_status": self.data_freshness_status,
            "catalyst_type": self.catalyst_type,
            "news_status": self.news_status,
            "sentiment": self.sentiment,
            "risk_flags_json": self.risk_flags,
            "validation_status": self.validation_status,
            "raw_json": self.raw or {},
            "is_mock": 1 if self.is_mock else 0,
            # --- Trade Packet v1 metadata (audit only; no decision change) ---
            "prompt_template_version": pt.PROMPT_TEMPLATE_VERSION,
            "schema_version": "v1",
            "thesis_summary": self.reasoning_summary,
            "expected_hold_days": self.max_holding_days,
            "same_day_exit_allowed": None,
            "counter_thesis": None,
            "reasons_to_reject": None,
            "model_provider": self.model_provider,
            "prompt_hash": self.prompt_hash,
            "system_prompt_hash": self.system_prompt_hash,
        }


class OpenAIClient:
    def __init__(self, settings, journal=None):
        self.settings = settings
        self.journal = journal
        self.use_mock = settings.is_mock or not settings.has_openai_key
        self.model = settings.openai_primary_model

    def evaluate(self, candidate: dict, snapshot: dict, freshness_status: str = "usable") -> OpenAIEvaluation:
        """Evaluate a candidate in no-news mode (the v1 path)."""
        if self.use_mock:
            evaluation = self._mock_eval(candidate, snapshot, freshness_status)
        else:
            try:
                evaluation = self._live_eval(candidate, snapshot, freshness_status)
            except Exception as exc:  # pragma: no cover - live path
                if self.journal is not None:
                    self.journal.log_system_event(
                        Severity.ERROR, "openai",
                        f"OpenAI evaluation failed for {candidate.get('symbol')}; rejecting.",
                        {"error": str(exc)},
                    )
                return self._rejection(candidate, "OpenAI call failed; rejected for safety.",
                                       [ReasonCode.OPENAI_REJECT.value])
        return self._enforce_min_reward_risk(evaluation, candidate)

    def _enforce_min_reward_risk(self, evaluation: OpenAIEvaluation, candidate: dict) -> OpenAIEvaluation:
        """A proposal must clear the configured minimum reward:risk. Guards the
        live engine (which sets its own levels); a no-op for the mock baseline
        whose reward:risk equals TARGET_REWARD_RISK by construction."""
        floor = self.settings.min_reward_risk
        if (
            evaluation.decision == Decision.PROPOSE.value
            and floor > 0
            and evaluation.expected_r is not None
            and evaluation.expected_r < floor
        ):
            if self.journal is not None:
                self.journal.log_system_event(
                    Severity.INFO, "openai",
                    f"{evaluation.symbol}: reward:risk {evaluation.expected_r} below "
                    f"minimum {floor}; downgraded to reject.",
                )
            return self._rejection(
                candidate,
                f"reward:risk {evaluation.expected_r} below minimum {floor}.",
                [ReasonCode.REWARD_RISK_TOO_LOW.value],
                freshness_status=evaluation.data_freshness_status,
            )
        return evaluation

    # ------------------------------------------------------------------- mock
    def _mock_eval(self, candidate, snapshot, freshness_status) -> OpenAIEvaluation:
        symbol = candidate.get("symbol")
        direction = candidate.get("direction") or TradeDirection.LONG.value
        last = snapshot.get("last_price")

        if freshness_status != "usable":
            return self._rejection(
                candidate, f"Data freshness '{freshness_status}'; cannot trade on unreliable data.",
                [ReasonCode.STALE_DATA.value], freshness_status=freshness_status,
            )

        momentum = float(candidate.get("momentum_score") or 0.0)
        entry = float(last) if last else None
        if entry is None:
            return self._rejection(candidate, "No usable price; rejected.", [ReasonCode.STALE_DATA.value])

        # Stop is a configurable fraction of entry; the target sits at that
        # distance scaled by the configured reward:risk (so expected_r == rr).
        stop_pct = self.settings.stop_loss_pct
        rr = self.settings.target_reward_risk
        if direction == TradeDirection.SHORT.value:
            stop = round(entry * (1 + stop_pct), 2)
            target = round(entry * (1 - stop_pct * rr), 2)
        else:
            stop = round(entry * (1 - stop_pct), 2)
            target = round(entry * (1 + stop_pct * rr), 2)
        risk_per_share = abs(entry - stop)
        expected_r = round(abs(target - entry) / risk_per_share, 2) if risk_per_share else None
        confidence = round(min(0.9, 0.4 + 0.5 * momentum), 3)

        # No-news baseline: momentum/structure decides propose vs watch.
        decision = Decision.PROPOSE.value if momentum >= PROPOSE_MOMENTUM_THRESHOLD else Decision.WATCH.value
        reasoning = (
            "No-news momentum baseline: thesis from price action, volume, relative "
            "strength, and trend structure; no catalyst used."
            if decision == Decision.PROPOSE.value
            else "No-news baseline: momentum/structure too weak to propose; watching."
        )

        return OpenAIEvaluation(
            eval_id=new_id("eval"),
            candidate_id=candidate.get("candidate_id", ""),
            symbol=symbol,
            model="mock",
            direction=direction,
            entry=entry,
            stop=stop,
            target=target,
            max_holding_days=3,
            expected_r=expected_r,
            confidence=confidence,
            decision=decision,
            reasoning_summary=reasoning,
            news_sources=[],
            data_freshness_status=freshness_status,
            catalyst_type=CATALYST_NOT_AVAILABLE_V1,
            news_status=NEWS_STATUS_DISABLED_V1,
            sentiment="bullish" if direction == TradeDirection.LONG.value else "bearish",
            risk_flags=[],
            validation_status="passed",
            raw={"mock": True, "mode": "no_news_v1"},
            is_mock=True,
        )

    def _rejection(self, candidate, reason, flags, freshness_status="usable", validation_status="passed"):
        return OpenAIEvaluation(
            eval_id=new_id("eval"),
            candidate_id=candidate.get("candidate_id", ""),
            symbol=candidate.get("symbol"),
            model="mock" if self.use_mock else self.model,
            direction=candidate.get("direction") or TradeDirection.LONG.value,
            entry=None, stop=None, target=None, max_holding_days=None,
            expected_r=None, confidence=0.0,
            decision=Decision.REJECT.value,
            reasoning_summary=reason,
            news_sources=[],
            data_freshness_status=freshness_status,
            catalyst_type=CATALYST_NOT_AVAILABLE_V1,
            news_status=NEWS_STATUS_DISABLED_V1,
            sentiment="unclear",
            risk_flags=flags,
            validation_status=validation_status,
            raw={"mock": self.use_mock},
            is_mock=self.use_mock,
        )

    # ------------------------------------------------------------------- live
    def _live_eval(self, candidate, snapshot, freshness_status) -> OpenAIEvaluation:  # pragma: no cover
        from openai import OpenAI  # lazy import; optional dependency

        client = OpenAI(api_key=self.settings.openai_api_key)
        user_prompt = pt.build_no_news_user_prompt(candidate, snapshot, freshness_status)
        # PR4: measurement-only AI-call lineage (model provider + content hashes of
        # the actual prompt sent, never the raw prompt body) -- stamped onto
        # whichever OpenAIEvaluation this call ends up returning below.
        ai_lineage = lineage.ai_call_lineage(
            provider="openai", prompt=user_prompt, system_prompt=pt.NO_NEWS_SYSTEM_PROMPT,
        )
        resp = client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": pt.NO_NEWS_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            timeout=HTTP_TIMEOUT,
        )
        obj = structured_json.parse_json_object(resp.choices[0].message.content)
        structured_json.require_keys(obj, pt.NO_NEWS_EVAL_KEYS)

        # Enforce no-news output: reject any invented/inferred catalyst.
        failure = validate_no_news_eval(obj)
        if failure:
            if self.journal is not None:
                self.journal.log_system_event(
                    Severity.WARNING, "openai",
                    f"Rejected {candidate.get('symbol')}: {failure}.",
                )
            rej = self._rejection(
                candidate, f"failed_validation: {failure}",
                [ReasonCode.INVENTED_CATALYST.value], freshness_status=freshness_status,
                validation_status=failure,
            )
            return self._with_ai_lineage(rej, ai_lineage)

        obj = enforce_no_news_sentinels(obj)
        evaluation = self._from_json(candidate, obj, freshness_status)
        return self._with_ai_lineage(evaluation, ai_lineage)

    @staticmethod
    def _with_ai_lineage(evaluation: OpenAIEvaluation, ai_lineage: dict) -> OpenAIEvaluation:
        evaluation.model_provider = ai_lineage.get("model_provider")
        evaluation.prompt_hash = ai_lineage.get("prompt_hash")
        evaluation.system_prompt_hash = ai_lineage.get("system_prompt_hash")
        return evaluation

    def _from_json(self, candidate, obj, freshness_status) -> OpenAIEvaluation:  # pragma: no cover
        try:
            decision = Decision(str(obj.get("decision", "reject")).lower()).value
        except ValueError:
            decision = Decision.REJECT.value
        return OpenAIEvaluation(
            eval_id=new_id("eval"),
            candidate_id=candidate.get("candidate_id", ""),
            symbol=obj.get("symbol") or candidate.get("symbol"),
            model=self.model,
            direction=str(obj.get("direction", "long")).lower(),
            entry=obj.get("entry"),
            stop=obj.get("stop"),
            target=obj.get("target"),
            max_holding_days=obj.get("max_holding_days"),
            expected_r=obj.get("expected_r"),
            confidence=obj.get("confidence"),
            decision=decision,
            reasoning_summary=obj.get("reasoning_summary", ""),
            news_sources=[],
            data_freshness_status=obj.get("data_freshness_status", freshness_status),
            catalyst_type=CATALYST_NOT_AVAILABLE_V1,
            news_status=NEWS_STATUS_DISABLED_V1,
            sentiment=obj.get("sentiment"),
            risk_flags=obj.get("risk_flags", []),
            validation_status="passed",
            raw=obj,
            is_mock=False,
        )
