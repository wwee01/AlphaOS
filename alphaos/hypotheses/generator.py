"""HGEN-1: the LLM-calling layer -- thin, default-off, operator-triggered
only. NO scheduler job, NO cron wiring in this build (``HYPOTHESIS_GEN_
RECURRING_ENABLED`` ships as a settings flag now, default false, and is
INERT -- no code path reads it beyond settings-load validation; recurring
generation is a FUTURE arming, see G2 below and docs/ALPHAOS_MASTER_
REFERENCE.md).

Mock-path convention mirrors ``alphaos.ai.openai_client.OpenAIClient``
(``use_mock = settings.is_mock or not settings.has_openai_key``) -- NOT
``BearDebater``'s Anthropic-keyed convention -- because the generator
stamps ``model_id`` from ``settings.openai_primary_model`` (the operator's
own primary model), not a separate reviewer model.

Runtime gate G1 (re-checked EVERY run -- the flag alone is never sufficient,
CANARY's own convention: ``canary_enabled`` alone doesn't run anything
against an empty corpus either) refuses generation unless at least one
hypothesis has actually resolved with a verdict. Today that count is zero,
so this feature is INERT-BY-DATA even with ``HYPOTHESIS_GEN_SHADOW_ENABLED``
on -- that is CORRECT and intended (see ``check_g1_gate()``'s own
docstring).

Cost discipline mirrors PR14's bear-debate: a TIGHTER daily sub-cap
(``check_hypothesis_gen_budget``) nested INSIDE the shared 30-day AI cost
cap (``check_scan_budget``), both checked up front before any real call.
"""

from __future__ import annotations

import random

from alphaos import lineage
from alphaos.ai import prompt_templates as pt
from alphaos.constants import Severity
from alphaos.hypotheses import proposer
from alphaos.hypotheses.constants import DraftStatus
from alphaos.scheduler import cost_guard

HTTP_TIMEOUT = 30

DEFAULT_EXEMPLAR_LIMIT = 50

# Hard-block generation while this many drafts already sit unreviewed
# (status='draft') -- the unreviewed-draft ceiling (build spec #4). Not a
# settings knob: this is a structural backstop against the generator
# outrunning operator review capacity, not a cost control (that's the
# calls-per-day caps below), so it stays a code constant like
# MAX_CONCURRENT_TESTING_GENERATED in proposer.py.
UNREVIEWED_DRAFT_CEILING = 10

_MOCK_DIRECTIONS = ("positive", "negative", "either")


# --------------------------------------------------------------- G1 gate
def check_g1_gate(journal) -> tuple[bool, str]:
    """Runtime gate G1: generation refuses unless at least one hypothesis
    has resolved with a verdict (``status='resolved' AND last_verdict IS
    NOT NULL AND resolved_at_utc IS NOT NULL``). Re-checked on EVERY call to
    ``run_hypothesis_generate()`` -- ``settings.hypothesis_gen_shadow_
    enabled`` being True is never, by itself, sufficient (same "the flag
    alone is never enough" posture CANARY already established: an enabled-
    but-dataless feature must still be a safe no-op, not a green light).
    Zero hypotheses have resolved as of this build (2026-07-11) -- this
    gate is CORRECT to fail today, and will start passing the first time
    ``alphaos hypothesis_resolve`` actually resolves one (earliest
    2026-08-07, H-WIN-1's own ``analysis_not_before`` -- see docs/
    ALPHAOS_MASTER_REFERENCE.md's G1 note)."""
    n = journal.scalar(
        "SELECT COUNT(*) FROM hypothesis_proposals WHERE status = 'resolved' "
        "AND last_verdict IS NOT NULL AND resolved_at_utc IS NOT NULL"
    ) or 0
    if n < 1:
        return False, (
            f"G1 gate: {n} resolved+verdicted hypothesis(es) in the registry (need >= 1) -- "
            "generation stays inert-by-data regardless of HYPOTHESIS_GEN_SHADOW_ENABLED"
        )
    return True, f"G1 gate: {n} resolved+verdicted hypothesis(es) -- clear to generate"


# ------------------------------------------------------------- exemplars
# Deliberately NO verdict predicate: every RESOLVED hypothesis feeds the
# prompt regardless of its own last_verdict (met/failed/inconclusive all
# teach the generator something -- a failed claim is exactly as
# informative an exemplar as a forward-test-candidate one). Kept as a
# module-level constant (not inlined in the function body) so a grep-based
# test can isolate exactly the WHERE clause and assert it names no verdict
# column, the same "grep-based, same style as the isolation-law tests"
# convention the build spec calls for.
EXEMPLAR_SELECT_SQL = (
    "SELECT hypothesis_id, risk_class, claim, metric_description, metric_fn_name, "
    "card_id, last_verdict, last_q_value, last_reason FROM hypothesis_proposals "
    "WHERE status = 'resolved' ORDER BY resolved_at_utc DESC LIMIT ?"
)


def select_exemplars(journal, limit: int = DEFAULT_EXEMPLAR_LIMIT) -> list[dict]:
    """Every resolved hypothesis (regardless of verdict), most recent
    first. ``last_verdict`` is included as a SELECTED column (the prompt
    shows it to the model for context) but never as a WHERE predicate --
    see ``EXEMPLAR_SELECT_SQL``'s own comment."""
    return journal.query(EXEMPLAR_SELECT_SQL, (limit,))


def card_summaries() -> list[dict]:
    """The current card set's compact summary (card_id/version/name/state)
    for the generator prompt. Reads YAML files directly (same "read fresh
    off disk, no caching" posture as ``cards.registry`` itself) -- never
    touches the DB, so this never fails just because a test's in-memory
    journal has no ``setup_cards`` rows synced yet. Fails toward an empty
    list, never raises (a prompt-context helper must never block
    generation over a card-loading hiccup)."""
    try:
        from alphaos.cards.registry import load_card_files
        cards = load_card_files()
    except Exception:
        return []
    return [
        {"card_id": c.get("card_id"), "version": c.get("version"),
         "name": c.get("name"), "state": c.get("state")}
        for c in cards
    ]


class HypothesisGenerator:
    """Thin LLM-calling wrapper, mock-path convention modeled on
    ``OpenAIClient`` (see module docstring). Produces raw candidate dicts --
    NOT yet validated/quarantined; every candidate still passes through
    ``proposer.intake_draft()`` before it becomes a real ``hypothesis_drafts``
    row."""

    def __init__(self, settings, journal=None):
        self.settings = settings
        self.journal = journal
        self.use_mock = settings.is_mock or not settings.has_openai_key
        self.model = settings.openai_primary_model

    def generate(self, exemplars: list[dict], card_summaries_: list[dict], n: int) -> tuple[list[dict], dict]:
        """Returns ``(candidates, meta)``. ``meta`` is
        ``{"model_provider", "prompt_hash", "system_prompt_hash", "is_mock"}``
        -- shared across every candidate in this batch (they all came from
        the SAME rendered prompt / same call), stamped onto each resulting
        draft by the caller. Never raises: a live-path failure degrades to
        zero candidates (a missing generation is a missed opportunity, never
        a reason to crash an operator-invoked CLI command), logged as a
        system event."""
        if self.use_mock:
            return self._mock_generate(exemplars, card_summaries_, n), {
                "model_provider": None, "prompt_hash": None, "system_prompt_hash": None, "is_mock": True,
            }
        try:
            return self._live_generate(exemplars, card_summaries_, n)
        except Exception as exc:  # pragma: no cover - live path
            if self.journal is not None:
                self.journal.log_system_event(
                    Severity.WARNING, "hypothesis_gen",
                    f"Hypothesis generation call failed; producing zero candidates. {exc}",
                    {"error": str(exc)},
                )
            return [], {"model_provider": None, "prompt_hash": None, "system_prompt_hash": None, "is_mock": False}

    def _mock_generate(self, exemplars: list[dict], card_summaries_: list[dict], n: int) -> list[dict]:
        whitelist = sorted(proposer.METRIC_WHITELIST)
        seed = f"hgen-mock-{len(exemplars)}-{len(card_summaries_)}-{n}"
        rng = random.Random(seed)
        out = []
        for i in range(max(0, n)):
            metric_fn_name = whitelist[i % len(whitelist)]
            direction = _MOCK_DIRECTIONS[i % len(_MOCK_DIRECTIONS)]
            suffix = rng.randrange(10_000, 99_999)
            out.append({
                "title": f"Mock candidate {i + 1} ({metric_fn_name}, {direction}) #{suffix}",
                "claim_text": (
                    f"Mock generated claim #{suffix}: {metric_fn_name} shows a {direction} "
                    "effect (deterministic mock -- no ANTHROPIC/OPENAI key or ALPHAOS_MODE=mock)."
                ),
                "metric_fn_name": metric_fn_name,
                "direction": direction,
                "proposed_risk_class": "B",
                "card_id": None,
            })
        return out

    def _live_generate(self, exemplars, card_summaries_, n):  # pragma: no cover - live path
        from openai import OpenAI  # lazy import; optional dependency
        from alphaos.util import structured_json

        client = OpenAI(api_key=self.settings.openai_api_key)
        whitelist = sorted(proposer.METRIC_WHITELIST)
        user_prompt = pt.build_hypothesis_gen_user_prompt(exemplars, card_summaries_, whitelist, n)
        ai_lineage = lineage.ai_call_lineage(
            provider="openai", prompt=user_prompt, system_prompt=pt.HYPOTHESIS_GEN_SYSTEM_PROMPT,
        )
        resp = client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": pt.HYPOTHESIS_GEN_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            timeout=HTTP_TIMEOUT,
        )
        obj = structured_json.parse_json_object(resp.choices[0].message.content)  # type: ignore[arg-type]
        structured_json.require_keys(obj, ["candidates"])
        candidates = obj.get("candidates") or []
        if not isinstance(candidates, list):
            candidates = []
        meta = {
            "model_provider": ai_lineage.get("model_provider"),
            "prompt_hash": ai_lineage.get("prompt_hash"),
            "system_prompt_hash": ai_lineage.get("system_prompt_hash"),
            "is_mock": False,
        }
        return candidates[: max(0, n)], meta


def run_hypothesis_generate(journal, settings) -> dict:
    """The orchestration function behind the ``hypothesis_generate`` CLI
    command. Never raises -- every refusal reason is returned in the result
    dict, matching ``run_canary()``'s own "empty/blocked run is a safe
    no-op, not a hard failure" posture. Checked in order:

    1. ``settings.hypothesis_gen_shadow_enabled`` (the master flag).
    2. G1 gate (re-checked every run -- see ``check_g1_gate()``).
    3. Unreviewed-draft ceiling (``UNREVIEWED_DRAFT_CEILING``).
    4. Shared 30-day AI cost cap, then the daily hypothesis-gen sub-cap
       (skipped entirely in mock mode -- no real spend to guard).

    Only once every check passes does this call ``HypothesisGenerator.
    generate()`` (one real call, up to ``settings.hypothesis_gen_max_
    proposals_per_run`` candidates) and run each candidate through
    ``proposer.intake_draft()``. A candidate that fails schema validation
    is skipped (never crashes the batch -- fail-safe, per-item isolation,
    same posture as ``score_debate_batch()``'s own per-vote try/except)."""
    result: dict = {
        "status": "skipped", "reason": None, "generated": 0, "intaken": 0,
        "duplicates": 0, "schema_errors": 0, "is_mock": None,
    }

    if not settings.hypothesis_gen_shadow_enabled:
        result["reason"] = "HYPOTHESIS_GEN_SHADOW_ENABLED is false"
        return result

    ok_g1, detail_g1 = check_g1_gate(journal)
    if not ok_g1:
        result["reason"] = detail_g1
        return result

    pending = journal.count_rows("hypothesis_drafts", "status = ?", (DraftStatus.DRAFT.value,))
    if pending >= UNREVIEWED_DRAFT_CEILING:
        result["reason"] = (
            f"UNREVIEWED_DRAFT_CEILING: {pending} draft(s) already awaiting review "
            f"(cap={UNREVIEWED_DRAFT_CEILING}) -- review/accept/reject some before generating more"
        )
        return result

    is_mock = bool(settings.is_mock or not settings.has_openai_key)
    if not is_mock:
        ok_30d, detail_30d = cost_guard.check_scan_budget(settings, journal)
        if not ok_30d:
            result["reason"] = f"shared 30d AI cost cap: {detail_30d}"
            return result
        ok_daily, detail_daily = cost_guard.check_hypothesis_gen_budget(settings, journal)
        if not ok_daily:
            result["reason"] = f"hypothesis-gen daily cap: {detail_daily}"
            return result

    n = settings.hypothesis_gen_max_proposals_per_run
    exemplars = select_exemplars(journal)
    cards = card_summaries()

    generator = HypothesisGenerator(settings, journal)
    candidates, meta = generator.generate(exemplars, cards, n)

    lineage_id = lineage.get_or_create_lineage_id(journal, settings) if candidates else None
    model_id = settings.openai_primary_model if meta.get("model_provider") else None

    intaken = duplicates = schema_errors = 0
    for cand in candidates:
        try:
            row = proposer.intake_draft(
                journal, cand, source="generated",
                model_id=model_id, model_provider=meta.get("model_provider"),
                prompt_hash=meta.get("prompt_hash"), system_prompt_hash=meta.get("system_prompt_hash"),
                lineage_id=lineage_id,
            )
            intaken += 1
            if row["status"] == DraftStatus.REJECTED.value:
                duplicates += 1
        except proposer.CandidateSchemaError as exc:
            schema_errors += 1
            journal.log_system_event(
                Severity.WARNING, "hypothesis_gen",
                f"Generated candidate failed schema validation; skipped. {exc}",
                {"error": str(exc)},
            )

    result.update({
        "status": "completed", "reason": None, "generated": len(candidates),
        "intaken": intaken, "duplicates": duplicates, "schema_errors": schema_errors,
        "is_mock": is_mock,
    })
    return result
