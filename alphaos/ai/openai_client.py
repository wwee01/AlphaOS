"""OpenAI primary scoring engine.

* Live (key present, non-mock): calls OpenAI with a JSON-object response format,
  instructs JSON-only, and parses defensively via util.structured_json.
* Mock (no key or mock mode): produces a deterministic, schema-valid evaluation
  WITHOUT fabricating news — it honors the supplied news status. With no
  verifiable news it returns 'watch'/'reject' (NO_VERIFIABLE_NEWS), matching the
  news-confirmed momentum playbook.

Either way the output conforms to the same OpenAIEvaluation structure, so the
journal looks identical regardless of source.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

from alphaos.ai import prompt_templates as pt
from alphaos.constants import (
    Decision,
    NewsStatus,
    ReasonCode,
    Severity,
    TradeDirection,
)
from alphaos.util import structured_json
from alphaos.util.ids import new_id

HTTP_TIMEOUT = 30


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
    catalyst_type: Optional[str] = None
    sentiment: Optional[str] = None
    risk_flags: list = field(default_factory=list)
    raw: Optional[dict] = None
    is_mock: bool = False

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
            "sentiment": self.sentiment,
            "risk_flags_json": self.risk_flags,
            "raw_json": self.raw or {},
            "is_mock": 1 if self.is_mock else 0,
        }


class OpenAIClient:
    def __init__(self, settings, journal=None):
        self.settings = settings
        self.journal = journal
        self.use_mock = settings.is_mock or not settings.has_openai_key
        self.model = settings.openai_primary_model

    def evaluate(
        self,
        candidate: dict,
        snapshot: dict,
        news_items: list[dict],
        news_status: NewsStatus | str,
        freshness_status: str = "usable",
    ) -> OpenAIEvaluation:
        status = news_status.value if isinstance(news_status, NewsStatus) else str(news_status)
        if self.use_mock:
            return self._mock_eval(candidate, snapshot, news_items, status, freshness_status)
        try:
            return self._live_eval(candidate, snapshot, news_items, status, freshness_status)
        except Exception as exc:  # pragma: no cover - live path
            if self.journal is not None:
                self.journal.log_system_event(
                    Severity.ERROR,
                    "openai",
                    f"OpenAI evaluation failed for {candidate.get('symbol')}; rejecting.",
                    {"error": str(exc)},
                )
            # Fail safe: a failed evaluation is a rejection, never a silent pass.
            return self._rejection(
                candidate, "OpenAI call failed; rejected for safety.", [ReasonCode.OPENAI_REJECT.value]
            )

    # ------------------------------------------------------------------- mock
    def _mock_eval(self, candidate, snapshot, news_items, status, freshness_status):
        symbol = candidate.get("symbol")
        direction = candidate.get("direction") or TradeDirection.LONG.value
        last = snapshot.get("last_price")

        # Stale/unverifiable data => reject (never trade on bad data).
        if freshness_status != "usable":
            return self._rejection(
                candidate,
                f"Data freshness '{freshness_status}'; cannot trade on unreliable data.",
                [ReasonCode.STALE_DATA.value],
                freshness_status=freshness_status,
            )

        # No verifiable news => not 'propose' (news-confirmed momentum playbook).
        if status == NewsStatus.NEWS_UNAVAILABLE.value or not news_items:
            return OpenAIEvaluation(
                eval_id=new_id("eval"),
                candidate_id=candidate.get("candidate_id", ""),
                symbol=symbol,
                model="mock",
                direction=direction,
                entry=last,
                stop=None,
                target=None,
                max_holding_days=None,
                expected_r=None,
                confidence=0.2,
                decision=Decision.WATCH.value,
                reasoning_summary=(
                    "No verifiable news catalyst. News-confirmed momentum playbook "
                    "requires a catalyst; downgraded to watch."
                ),
                news_sources=[],
                data_freshness_status=freshness_status,
                catalyst_type=None,
                sentiment="unclear",
                risk_flags=[ReasonCode.NO_VERIFIABLE_NEWS.value],
                raw={"mock": True, "news_status": status},
                is_mock=True,
            )

        # News present + usable data => propose a structured swing trade.
        momentum = float(candidate.get("momentum_score") or 0.5)
        confidence = round(min(0.95, 0.45 + 0.5 * momentum), 3)
        entry = float(last) if last else 100.0
        if direction == TradeDirection.SHORT.value:
            stop = round(entry * 1.03, 2)
            target = round(entry * 0.94, 2)
        else:
            stop = round(entry * 0.97, 2)
            target = round(entry * 1.06, 2)
        risk_per_share = abs(entry - stop)
        reward = abs(target - entry)
        expected_r = round(reward / risk_per_share, 2) if risk_per_share else None
        sources = [n.get("source_url") or n.get("source_name") for n in news_items]
        catalyst = news_items[0].get("catalyst_type") or "news_catalyst"

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
            decision=Decision.PROPOSE.value,
            reasoning_summary=(
                f"Momentum continuation with a news catalyst ({catalyst}); "
                f"swing 1-5d, {direction}."
            ),
            news_sources=[s for s in sources if s],
            data_freshness_status=freshness_status,
            catalyst_type=catalyst,
            sentiment="bullish" if direction == TradeDirection.LONG.value else "bearish",
            risk_flags=[],
            raw={"mock": True, "news_status": status},
            is_mock=True,
        )

    def _rejection(self, candidate, reason, flags, freshness_status="usable"):
        return OpenAIEvaluation(
            eval_id=new_id("eval"),
            candidate_id=candidate.get("candidate_id", ""),
            symbol=candidate.get("symbol"),
            model="mock" if self.use_mock else self.model,
            direction=candidate.get("direction") or TradeDirection.LONG.value,
            entry=None,
            stop=None,
            target=None,
            max_holding_days=None,
            expected_r=None,
            confidence=0.0,
            decision=Decision.REJECT.value,
            reasoning_summary=reason,
            news_sources=[],
            data_freshness_status=freshness_status,
            catalyst_type=None,
            sentiment="unclear",
            risk_flags=flags,
            raw={"mock": self.use_mock},
            is_mock=self.use_mock,
        )

    # ------------------------------------------------------------------- live
    def _live_eval(self, candidate, snapshot, news_items, status, freshness_status):  # pragma: no cover
        from openai import OpenAI  # lazy import; optional dependency

        client = OpenAI(api_key=self.settings.openai_api_key)
        user_prompt = pt.build_openai_user_prompt(candidate, snapshot, news_items, freshness_status)
        resp = client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": pt.OPENAI_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            timeout=HTTP_TIMEOUT,
        )
        text = resp.choices[0].message.content
        obj = structured_json.parse_json_object(text)
        structured_json.require_keys(obj, pt.OPENAI_EVAL_KEYS)
        return self._from_json(candidate, obj, freshness_status)

    def _from_json(self, candidate, obj, freshness_status):  # pragma: no cover - live path
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
            news_sources=obj.get("news_sources", []),
            data_freshness_status=obj.get("data_freshness_status", freshness_status),
            catalyst_type=obj.get("catalyst_type"),
            sentiment=obj.get("sentiment"),
            risk_flags=obj.get("risk_flags", []),
            raw=obj,
            is_mock=False,
        )
