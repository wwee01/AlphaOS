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

import copy
from dataclasses import dataclass, replace
from typing import Any, Optional

from alphaos.ai.openai_client import OpenAIClient, OpenAIEvaluation
from alphaos.constants import Decision, ReasonCode

RR_FLOOR = "RR_FLOOR"
NO_ATR = "NO_ATR"
# Audit NIT (2026-07-20): a raw propose that became a final non-propose
# WITHOUT either known reason code stamped means a third downgrade path
# exists that this module doesn't know about -- surfaced loudly as its own
# sentinel value rather than hidden inside NULL ("no downgrade happened").
UNKNOWN = "UNKNOWN"


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
    # INSTR-2: the prompt version this replay actually ran under -- the
    # second half of the (model, prompt_version) arm.
    prompt_version: str
    raw: OpenAIEvaluation
    final: OpenAIEvaluation
    downgrade_reason: Optional[str]


def _downgrade_reason(raw: OpenAIEvaluation, final: OpenAIEvaluation) -> Optional[str]:
    """NULL unless a raw 'propose' was downgraded by the pipeline.
    Distinguishes the two INSTR-1 mechanisms via the reason code
    ``_rejection()`` actually stamped into ``risk_flags`` -- never
    re-derives the decision independently (one source of truth: whatever
    ``post_process()`` itself decided). A downgrade carrying NEITHER known
    code returns the ``UNKNOWN`` sentinel, never NULL -- a future third
    downgrade path must show up in the autopsy, not hide."""
    if raw.decision != Decision.PROPOSE.value or final.decision == Decision.PROPOSE.value:
        return None
    flags = final.risk_flags or []
    if ReasonCode.NO_ATR_DATA.value in flags:
        return NO_ATR
    if ReasonCode.REWARD_RISK_TOO_LOW.value in flags:
        return RR_FLOOR
    return UNKNOWN


def replay_packet(fixture: dict, arm: tuple, settings: Any, real_journal: Any) -> ReplayResult:
    """Replays ONE corpus fixture through ONE arm (``(model,
    prompt_version)``), via the production ``OpenAIClient
    ._augment_snapshot_for_prompt()`` -> ``.raw_evaluate()`` ->
    ``.post_process()`` chain (INSTR-2) -- the exact same three steps
    ``evaluate()`` itself calls, in the same order. Only
    ``settings.openai_primary_model``/``settings.openai_prompt_version`` are
    parameterized (``dataclasses.replace`` on the frozen ``Settings``
    dataclass); everything else -- the prompt-build path, validation, the
    ATR-stop/reward:risk post-processing -- is the exact same production
    code the live evaluator runs. ``real_journal`` is wrapped in
    ``_ReadOnlyJournal`` (read-through for ATR lookups, write-swallowing
    for log events) -- shadow, read-only, per the spec's own non-goals.

    ``_augment_snapshot_for_prompt`` returns a fresh COPY of
    ``fixture["snapshot"]`` when a v2 arm is active, and the SAME object
    unchanged for a v1/mock arm -- neither path ever mutates the shared
    fixture dict, so multiple arms replaying the SAME fixture object (the
    normal multi-arm run shape) can never contaminate each other's view of
    it, regardless of call order."""
    model, prompt_version = arm
    replay_settings = replace(settings, openai_primary_model=model, openai_prompt_version=prompt_version)
    client = OpenAIClient(replay_settings, journal=_ReadOnlyJournal(real_journal))
    candidate = fixture["candidate"]
    snapshot = fixture["snapshot"]
    freshness_status = fixture.get("freshness_status") or "usable"

    snapshot = client._augment_snapshot_for_prompt(snapshot, candidate)
    raw = client.raw_evaluate(candidate, snapshot, freshness_status)
    # post_process gets a COPY: _apply_atr_stop mutates its argument in
    # place (stop/expected_r/stop_source assignment -- fine on the live
    # path, where nobody needs the pre-override object afterwards), which
    # would otherwise silently clobber the raw verdict this harness exists
    # to capture -- the stored "raw_stop" would be the ATR-overridden stop,
    # not the model's own (caught empirically on the first RR_FLOOR demo
    # run: raw_stop read 90.0 where the model returned 97.0).
    final = client.post_process(copy.copy(raw), candidate)
    return ReplayResult(
        model=model, prompt_version=prompt_version, raw=raw, final=final,
        downgrade_reason=_downgrade_reason(raw, final),
    )
