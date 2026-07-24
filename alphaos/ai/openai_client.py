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
from typing import TYPE_CHECKING, Optional, Union

if TYPE_CHECKING:
    from alphaos.scanner.scan_context import ScanContext

from alphaos.ai import prompt_templates as pt
from alphaos.ai.validation import enforce_no_news_sentinels, validate_no_news_eval
from alphaos import lineage
from alphaos.constants import (
    CATALYST_NOT_AVAILABLE_V1,
    Decision,
    NEWS_STATUS_DISABLED_V1,
    ReasonCode,
    Severity,
    TargetSource,
    TradeDirection,
)
from alphaos.data.atr import (  # noqa: F401 -- ATR_STOP_MULTIPLIER_V1 re-exported, see below
    ATR_RULES_V1,
    ATR_STOP_MULTIPLIER_V1,
    atr_stop_price,
)
from alphaos.util import structured_json
from alphaos.util.ids import new_id

HTTP_TIMEOUT = 30
PROPOSE_MOMENTUM_THRESHOLD = 0.40

# ATR_STOP_MULTIPLIER_V1 now lives in alphaos/data/atr.py (2026-07-09,
# relocated when BASELINE became a second consumer of this pure sizing-
# formula constant) -- re-exported here so this module's own existing
# ATR_STOP_MULTIPLIER_V1 reference below, and any external `from
# alphaos.ai.openai_client import ATR_STOP_MULTIPLIER_V1`, keep working
# unchanged.


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
    # PR9.5: real token usage for cost accounting. None for mock/rejection
    # paths (no real API call was made); populated by _live_eval.
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    # EVAL-1 addendum: the market-snapshot input to THIS evaluation, stamped
    # by OpenAIClient.evaluate() on every path (mock/live/rejection) -- the
    # only way a future eval harness could ever replay the primary
    # evaluator, since the snapshot was previously never persisted anywhere.
    snapshot: Optional[dict] = None
    # INSTR-1: set to TargetSource.ATR_V1 by _apply_atr_stop() when the
    # live evaluator's own stop was overridden with k*ATR(14). Transient --
    # NOT persisted in to_row() (openai_evaluations has no matching column);
    # the orchestrator reads this to set the FINAL trade_proposals row's own
    # already-existing stop_price_source column, the more relevant place for
    # this provenance to live (a candidate's raw evaluation vs the actual
    # proposed/executed trade).
    stop_source: Optional[str] = None
    # INSTR-2: the ACTIVE settings.openai_prompt_version at the moment
    # evaluate() produced this evaluation -- stamped on every path (mock/
    # live/post_process rejection), same "stamped LAST" reasoning as the
    # snapshot field below. Defaults to "v1" so every direct-constructed
    # test fixture (hundreds of them, across many test files) keeps working
    # unchanged. to_row() reads THIS field now, not the
    # prompt_templates.PROMPT_TEMPLATE_VERSION module literal -- editing
    # that literal in place would have been a live behavior change at
    # merge (see settings.py's own openai_prompt_version docstring).
    prompt_template_version: str = "v1"

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
            "prompt_template_version": self.prompt_template_version,
            "schema_version": "v1",
            "thesis_summary": self.reasoning_summary,
            "expected_hold_days": self.max_holding_days,
            "same_day_exit_allowed": None,
            "counter_thesis": None,
            "reasons_to_reject": None,
            "model_provider": self.model_provider,
            "prompt_hash": self.prompt_hash,
            "system_prompt_hash": self.system_prompt_hash,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "snapshot_json": self.snapshot or {},
        }


def _extract_usage(resp) -> Optional[dict]:
    """PR9.5: best-effort token usage from an OpenAI ChatCompletion response,
    for real cost accounting (cost_guard previously only counted
    openai_evaluations that were the FULL AI spend anyway here, but labeller/
    polarity calls elsewhere were invisible to it -- see cost_guard.py).
    Returns None if unavailable -- never affects/blocks the eval it's
    measuring."""
    usage = getattr(resp, "usage", None)
    if usage is None:
        return None
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }


def _latest_atr(journal, symbol: Optional[str]) -> Optional[float]:
    """INSTR-2: the ONE atr_history lookup definition -- extracted from
    ``_apply_atr_stop`` so that method and
    ``OpenAIClient._augment_snapshot_for_prompt`` share exactly one query,
    never two independently-written copies that could silently drift apart.
    Same SQL, same semantics as before extraction: most recent
    ``atr_14`` row for this symbol under the current ATR rules version.
    ``journal=None`` (rare -- the OpenAIClient constructor allows it) fails
    safe to ``None``, never a crash -- callers already treat "no ATR data"
    as a normal, expected outcome."""
    if journal is None:
        return None
    return journal.scalar(
        "SELECT atr_14 FROM atr_history WHERE symbol = ? AND rules_version = ? "
        "ORDER BY market_date DESC LIMIT 1",
        (symbol, ATR_RULES_V1),
    )


class OpenAIClient:
    def __init__(self, settings, journal=None):
        self.settings = settings
        self.journal = journal
        self.use_mock = settings.is_mock or not settings.has_openai_key
        self.model = settings.openai_primary_model

    def evaluate(self, candidate: "Union[dict, ScanContext]", snapshot: dict,
                freshness_status: str = "usable") -> OpenAIEvaluation:
        """Evaluate a candidate in no-news mode (the v1 path). Thin wrapper
        over the three factored steps below -- kept this way (AB-EVAL-1,
        extended INSTR-2) so there is exactly ONE place that augments the
        snapshot for the prompt, calls the raw model, then runs the
        post-processing chain, in that order; the A/B replay harness
        mirrors this same three-step sequence directly (on a reconstructed
        snapshot, through a settings clone with the model name AND prompt
        version parameterized) instead of re-deriving it."""
        snapshot = self._augment_snapshot_for_prompt(snapshot, candidate)
        evaluation = self.raw_evaluate(candidate, snapshot, freshness_status)
        evaluation = self.post_process(evaluation, candidate)
        # EVAL-1 addendum (extended INSTR-2): journal the snapshot input
        # (the augmented copy, when v2 is active -- so snapshot_json
        # archives exactly what the model was shown) alongside every real
        # evaluation (all paths, including rejections/fail-safes -- those
        # are precisely the examples a future replay harness needs most,
        # same "retention starts here" law EVAL-1 already applies to the
        # labeller), and stamp the ACTIVE prompt-template version. Both
        # stamped LAST, after post_process() -- that chain can swap in a
        # brand-new rejection object of its own, which would otherwise miss
        # either stamp if this ran before it. The primary evaluator's own
        # snapshot input was previously never persisted anywhere, making it
        # the one AI call in this codebase that could never be replayed
        # after the fact.
        evaluation.prompt_template_version = self.settings.openai_prompt_version
        evaluation.snapshot = snapshot
        return evaluation

    def _augment_snapshot_for_prompt(self, snapshot: dict,
                                     candidate: "Union[dict, ScanContext]") -> dict:
        """INSTR-2: no-op (returns ``snapshot`` UNCHANGED, same object) unless
        BOTH live (``not self.use_mock``) AND ``settings.openai_prompt_version
        == "v2"`` -- the mock baseline's prompt never changes (non-goal), and
        v1 stays byte-identical to before (merge-dark guarantee).

        When active, returns a COPY of ``snapshot`` with key ``"atr_policy"``
        set to a freshly-computed block -- ALWAYS recomputed here, ALWAYS
        overwriting any pre-existing ``"atr_policy"`` key the input snapshot
        might already carry, never read from it: a replayed v2-era fixture
        must never smuggle a stale archived block past the CURRENT
        atr_history state that ``_apply_atr_stop`` will enforce against
        later in this same evaluation (one query definition, ``_latest_atr``,
        shared by both reads -- they cannot disagree).

        No ATR data for this symbol (``None``/``<= 0``) => no key added =>
        the unchanged ``NO_ATR_DATA`` fail-safe in ``_apply_atr_stop``
        handles any raw propose downstream.

        The ATR read here has its OWN try/except, deliberately OUTSIDE
        ``raw_evaluate()``'s and ``post_process()``'s own containment (this
        runs BEFORE either) -- a transient DB error must degrade to a
        v1-shaped prompt (no block added), journaled as an ERROR, never
        propagate and abort the caller's scan loop."""
        if self.use_mock or self.settings.openai_prompt_version != "v2":
            return snapshot

        symbol = candidate.get("symbol")
        try:
            atr = _latest_atr(self.journal, symbol)
        except Exception as exc:
            if self.journal is not None:
                self.journal.log_system_event(
                    Severity.ERROR, "openai",
                    f"{symbol}: ATR read failed while building the v2 prompt; "
                    "degrading to a v1-shaped prompt for this evaluation.",
                    {"error": str(exc)},
                )
            return snapshot
        if atr is None or atr <= 0:
            return snapshot

        stop_multiplier = ATR_STOP_MULTIPLIER_V1
        risk_per_share = round(stop_multiplier * atr, 4)
        min_reward_risk = self.settings.min_reward_risk
        augmented = dict(snapshot)
        augmented["atr_policy"] = {
            "atr_14": atr,
            "stop_multiplier": stop_multiplier,
            "risk_per_share": risk_per_share,
            "min_reward_risk": min_reward_risk,
            "min_target_distance": round(min_reward_risk * risk_per_share, 4),
            "rules_version": ATR_RULES_V1,
        }
        return augmented

    def raw_evaluate(self, candidate: "Union[dict, ScanContext]", snapshot: dict,
                     freshness_status: str = "usable") -> OpenAIEvaluation:
        """The raw model call ONLY -- no ATR-stop override, no reward:risk
        floor. Exactly what ``evaluate()`` used to do before its own
        post-processing tail; factored out (AB-EVAL-1) so the A/B replay
        harness can invoke the SAME production mock/live call path -- with
        only ``self.model`` (via a ``dataclasses.replace`` settings clone)
        parameterized -- and inspect the model's own verdict before any
        pipeline override touches it. No forked second prompt: this method
        is the only route to ``prompt_templates.build_no_news_user_prompt``
        for the primary evaluator, live or replayed."""
        if self.use_mock:
            return self._mock_eval(candidate, snapshot, freshness_status)
        try:
            return self._live_eval(candidate, snapshot, freshness_status)
        except Exception as exc:  # pragma: no cover - live path
            if self.journal is not None:
                self.journal.log_system_event(
                    Severity.ERROR, "openai",
                    f"OpenAI evaluation failed for {candidate.get('symbol')}; rejecting.",
                    {"error": str(exc)},
                )
            return self._rejection(candidate, "OpenAI call failed; rejected for safety.",
                                   [ReasonCode.OPENAI_REJECT.value])

    def post_process(self, evaluation: OpenAIEvaluation,
                     candidate: "Union[dict, ScanContext]") -> OpenAIEvaluation:
        """The post-processing chain: ATR-stop override (live path only) ->
        reward:risk floor enforcement, in that order -- factored out
        (AB-EVAL-1) so the replay harness can apply the EXACT SAME pipeline
        to a raw verdict a second time (under a different model) without a
        second implementation. Behaviorally identical to the pre-refactor
        inline sequence: ``_apply_atr_stop`` is a no-op for any
        non-PROPOSE/no-entry evaluation (including every rejection
        ``raw_evaluate`` can return), so gating on ``self.use_mock`` here
        exactly reproduces the old "only after a successful live call"
        behavior without needing to know whether the live call actually
        succeeded. The try/except below is part of that same identity
        (audit HIGH, 2026-07-20): pre-refactor, ``_apply_atr_stop`` ran
        INSIDE ``evaluate()``'s live try -- a true exception (e.g. a
        transient SQLite error on the atr_history read) was contained to
        a journaled ERROR + safe OPENAI_REJECT rejection, never allowed
        to abort the caller's whole scan loop. NOTE: ``_apply_atr_stop``'s
        own NO_ATR/RR_FLOOR *rejection returns* are normal flow, not
        exceptions -- only genuine raises are contained here."""
        if not self.use_mock:
            # INSTR-1: LIVE path only -- the mock baseline's stop is
            # already a clean, deterministic, config-driven formula
            # (stop_loss_pct) that hundreds of existing tests depend on
            # producing PROPOSE decisions without any atr_history
            # fixture; overriding it here would need every one of those
            # tests to seed ATR data or start silently rejecting
            # everything. "mock != real" (same discipline EARN-1 will
            # apply to its own live-only provider).
            try:
                evaluation = self._apply_atr_stop(evaluation, candidate)
            except Exception as exc:
                if self.journal is not None:
                    self.journal.log_system_event(
                        Severity.ERROR, "openai",
                        f"OpenAI evaluation failed for {candidate.get('symbol')}; rejecting.",
                        {"error": str(exc)},
                    )
                evaluation = self._rejection(candidate, "OpenAI call failed; rejected for safety.",
                                             [ReasonCode.OPENAI_REJECT.value])
        evaluation = self._enforce_min_reward_risk(evaluation, candidate)
        return evaluation

    def _enforce_min_reward_risk(self, evaluation: OpenAIEvaluation,
                                 candidate: "Union[dict, ScanContext]") -> OpenAIEvaluation:
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

    def _apply_atr_stop(self, evaluation: OpenAIEvaluation,
                        candidate: "Union[dict, ScanContext]") -> OpenAIEvaluation:
        """INSTR-1: overrides the live evaluator's own stop with
        entry +/- k*ATR(14) -- a fixed percentage stop means wildly
        different things for a low-vol name vs a high-vol one; this makes
        "1R" mean a comparable thing regardless of which symbol got
        scanned. The AI-proposed TARGET is left untouched (only the stop
        changes); expected_r is recomputed from the NEW stop against that
        same target, so _enforce_min_reward_risk (which runs immediately
        after this, in evaluate()) correctly re-checks reward:risk against
        the real, ATR-widened-or-narrowed risk -- never the AI's own
        now-stale number.

        No ATR data for this symbol (never yet captured, or a newly-listed
        name) is NOT a silent fallback to the AI's own stop -- that would
        quietly ship the OLD, unfixed behavior under a version number that
        claims to be fixed. Fails safe to reject instead, matching this
        codebase's own "block, never wave through" exit-first invariant.
        """
        if evaluation.decision != Decision.PROPOSE.value or evaluation.entry is None:
            return evaluation

        atr = _latest_atr(self.journal, evaluation.symbol)
        if atr is None or atr <= 0:
            if self.journal is not None:
                self.journal.log_system_event(
                    Severity.INFO, "openai",
                    f"{evaluation.symbol}: no ATR(14) data available; rejecting -- "
                    "catalyst_momentum_v2 stops require it.",
                )
            return self._rejection(
                candidate,
                f"No ATR(14) data available for {evaluation.symbol}; cannot compute a v2 stop.",
                [ReasonCode.NO_ATR_DATA.value],
                freshness_status=evaluation.data_freshness_status,
            )

        entry = float(evaluation.entry)
        new_stop = atr_stop_price(entry, atr, evaluation.direction)

        risk_per_share = abs(entry - new_stop)
        new_expected_r = (
            round(abs(evaluation.target - entry) / risk_per_share, 2)
            if (evaluation.target is not None and risk_per_share)
            else evaluation.expected_r
        )

        evaluation.stop = round(new_stop, 2)
        evaluation.expected_r = new_expected_r
        evaluation.stop_source = TargetSource.ATR_V1.value
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
        # INSTR-2 (audit fixup, MEDIUM): gated on the ACTIVE settings
        # version, never on whatever "atr_policy" key happens to already
        # sit in ``snapshot``. Naively passing snapshot.get("atr_policy")
        # unconditionally was proven exploitable: a replayed v2-era fixture
        # (e.g. a future AB-EVAL-1 corpus built from post-cutover
        # snapshot_json, which DOES carry an archived atr_policy block)
        # would leak the ATR_STOP_POLICY section into what's supposed to be
        # a byte-identical v1 control-arm prompt, corrupting the exact
        # comparison the harness exists to produce. _augment_snapshot_for_
        # prompt()'s own v1/mock no-op deliberately returns ``snapshot``
        # UNCHANGED (never strips a pre-existing key -- see its own
        # docstring), so this gate is the one place that must not trust the
        # snapshot dict's own contents.
        user_prompt = pt.build_no_news_user_prompt(
            candidate, snapshot, freshness_status,
            atr_policy=snapshot.get("atr_policy") if self.settings.openai_prompt_version == "v2" else None,
        )
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
        usage = _extract_usage(resp)  # PR9.5: before any validation/rejection branch below
        obj = structured_json.parse_json_object(resp.choices[0].message.content)  # type: ignore[arg-type]
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
            return self._with_ai_lineage(rej, ai_lineage, usage)

        obj = enforce_no_news_sentinels(obj)
        evaluation = self._from_json(candidate, obj, freshness_status)
        return self._with_ai_lineage(evaluation, ai_lineage, usage)

    @staticmethod
    def _with_ai_lineage(
        evaluation: OpenAIEvaluation, ai_lineage: dict, usage: Optional[dict] = None,
    ) -> OpenAIEvaluation:
        evaluation.model_provider = ai_lineage.get("model_provider")
        evaluation.prompt_hash = ai_lineage.get("prompt_hash")
        evaluation.system_prompt_hash = ai_lineage.get("system_prompt_hash")
        if usage:
            evaluation.prompt_tokens = usage.get("prompt_tokens")
            evaluation.completion_tokens = usage.get("completion_tokens")
            evaluation.total_tokens = usage.get("total_tokens")
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
