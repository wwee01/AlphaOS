"""AB-EVAL-1: the replay engine -- invokes the SAME production
``OpenAIClient.raw_evaluate()`` / ``.post_process()`` chain the live
evaluator runs (see ``alphaos/ai/openai_client.py``), with ONLY the model
name parameterized via a frozen-dataclass settings clone
(``dataclasses.replace``). No forked second prompt: this module never
imports ``alphaos.ai.prompt_templates`` and defines no prompt-building
function of its own -- see ``tests/test_ab_eval.py``'s AST structural test,
which asserts exactly that.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Optional

from alphaos.ai.openai_client import OpenAIClient, OpenAIEvaluation
from alphaos.constants import Decision, ReasonCode

RR_FLOOR = "RR_FLOOR"
NO_ATR = "NO_ATR"


class _ReadOnlyJournal:
    """Minimal read-through proxy handed to the replay ``OpenAIClient``:
    forwards ``.scalar()`` (so ``_apply_atr_stop`` sees REAL
    ``atr_history`` coverage -- a replay that always saw "no ATR data"
    would fabricate every downgrade as NO_ATR) but swallows
    ``.log_system_event()`` so a shadow replay call never writes an
    indistinguishable-from-live INFO row into the production
    ``system_events`` table. Shadow/read-only law: the harness's OWN
    per-packet-isolation logging goes through the REAL journal directly,
    tagged ``category='ab_eval'`` (see ``alphaos/ab_eval/run.py``) -- the
    correct place for replay-run observability to live, distinguishable
    from live-path events."""

    def __init__(self, real_journal: Any):
        self._real = real_journal

    def scalar(self, sql: str, params: tuple = ()) -> Any:
        return self._real.scalar(sql, params) if self._real is not None else None

    def log_system_event(self, *args: Any, **kwargs: Any) -> None:
        return None


@dataclass
class ReplayResult:
    model: str
    raw: OpenAIEvaluation
    final: OpenAIEvaluation
    downgrade_reason: Optional[str]


def _downgrade_reason(raw: OpenAIEvaluation, final: OpenAIEvaluation) -> Optional[str]:
    """NULL unless a raw 'propose' was downgraded by the pipeline.
    Distinguishes the two INSTR-1 mechanisms via the reason code
    ``_rejection()`` actually stamped into ``risk_flags`` -- never
    re-derives the decision independently (one source of truth: whatever
    ``post_process()`` itself decided)."""
    if raw.decision != Decision.PROPOSE.value or final.decision == Decision.PROPOSE.value:
        return None
    flags = final.risk_flags or []
    if ReasonCode.NO_ATR_DATA.value in flags:
        return NO_ATR
    if ReasonCode.REWARD_RISK_TOO_LOW.value in flags:
        return RR_FLOOR
    return None


def replay_packet(fixture: dict, model: str, settings: Any, real_journal: Any) -> ReplayResult:
    """Replays ONE corpus fixture through ONE model, via the production
    ``OpenAIClient.raw_evaluate()`` -> ``.post_process()`` chain -- the
    exact same two methods ``evaluate()`` itself calls, in the same order.
    Only ``settings.openai_primary_model`` is parameterized
    (``dataclasses.replace`` on the frozen ``Settings`` dataclass);
    everything else -- the prompt-build path, validation, the ATR-stop/
    reward:risk post-processing -- is the exact same production code the
    live evaluator runs. ``real_journal`` is wrapped in ``_ReadOnlyJournal``
    (read-through for ATR lookups, write-swallowing for log events) --
    shadow, read-only, per the spec's own non-goals."""
    replay_settings = replace(settings, openai_primary_model=model)
    client = OpenAIClient(replay_settings, journal=_ReadOnlyJournal(real_journal))
    candidate = fixture["candidate"]
    snapshot = fixture["snapshot"]
    freshness_status = fixture.get("freshness_status") or "usable"

    raw = client.raw_evaluate(candidate, snapshot, freshness_status)
    final = client.post_process(raw, candidate)
    return ReplayResult(
        model=model, raw=raw, final=final, downgrade_reason=_downgrade_reason(raw, final),
    )
