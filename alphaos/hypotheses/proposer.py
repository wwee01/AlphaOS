"""HGEN-1: the deterministic substrate -- candidate schema validation,
duplicate detection, evidence-availability checking, mechanical risk
classification, and the draft-quarantine intake/accept/reject ceremony.

Zero LLM calls anywhere in this module (see ``alphaos/hypotheses/
generator.py`` for the LLM-calling layer, built and tested strictly AFTER
this substrate per the build protocol). ``intake_draft()`` is the single
chokepoint every candidate (generated OR manually authored) must pass
through before a ``hypothesis_drafts`` row exists -- see schema.py's own
module comment on that table for the full quarantine invariant.

THE LOAD-BEARING SAFETY PROPERTY: nothing in this module ever writes to
``hypothesis_proposals`` or ``preregistrations``, and never calls
``alphaos.stats.preregistration.register_hypothesis()`` or
``evaluate_hypothesis()``. The only bridge from a draft to the real registry
is ``accept_draft()``, which calls ``alphaos.hypotheses.registry.
propose_hypothesis()`` -- the SAME function every one of PR12's 8 seeded
hypotheses goes through, never a second, separately-tuned registration path.
Until that call happens, a draft cannot possibly shift the seeded family's
BH-FDR q-values (see ``alphaos.stats.fdr``'s own module docstring: the family
is "every evaluated preregistration," and a quarantined draft touches none
of that).
"""

from __future__ import annotations

from typing import Optional

from alphaos.constants import Severity
from alphaos.hypotheses import queries as hyp_queries
from alphaos.hypotheses.constants import (
    DraftStatus,
    RISK_CLASS_STEP_UP,
    RiskClass,
    SEEDED_HYPOTHESES,
)
from alphaos.hypotheses.registry import propose_hypothesis
from alphaos.util.ids import new_id

# The metric-function whitelist: the SAME query functions resolver.py can
# compute, enumerated directly from queries.METRIC_FUNCTIONS's own dispatch
# table -- never a second, independently-maintained list that could drift
# from what actually exists. H-AI-1's metric_fn_name (None, links to
# BASELINE's own preregistration) is deliberately excluded: a generated
# hypothesis always computes its own evidence, it never links to another
# feature's row.
METRIC_WHITELIST = frozenset(hyp_queries.METRIC_FUNCTIONS.keys())

_VALID_DIRECTIONS = frozenset({"positive", "negative", "either"})
_VALID_RISK_CLASSES = frozenset(rc.value for rc in RiskClass)

# --------------------------------------------------------------------------
# Reference direction per whitelisted metric_fn_name -- a build-time judgment
# call (the spec's duplicate-detection rule references "metric_fn_name +
# direction match" but no seeded hypothesis carries a structured direction
# field). Derived by reading each seeded hypothesis's own claim text in
# constants.py (auditable, not invented): H-TQS-1/H-CAT-1/H-INT-1 claim a
# one-sided POSITIVE effect; H-POL-1 claims divergence UNDERperforms (i.e. a
# NEGATIVE delta for the tested arm); H-WIN-1/H-TTL-1/H-REJ-1 explicitly
# frame "either direction is informative" in their own claim text. Matching
# is EXACT (no "either" wildcard) -- see check_duplicate()'s own docstring
# for why a wildcard was rejected as too restrictive for v0's usefulness.
_SEEDED_METRIC_DIRECTION: dict[str, str] = {
    "h_tqs_1_rows": "positive",
    "h_cat_1_rows": "positive",
    "h_int_1_rows": "positive",
    "h_win_1_rows": "either",
    "h_ttl_1_rows": "either",
    "h_rej_1_rows": "either",
    "h_pol_1_rows": "negative",
}

# Required tables per whitelisted metric_fn_name, read directly off each
# h_xxx_1_rows() function's own SQL in queries.py -- the evidence-
# availability check's data source. Every one of these tables is created
# unconditionally by JournalStore.init_schema() (schema.py's own SCHEMA
# list) regardless of any feature flag, so this check passes trivially
# today; it stays a REAL check (not a formality) against a future world
# where a metric's own required table might not always exist (e.g. an
# optional/deferred provider table).
_METRIC_REQUIRED_TABLES: dict[str, tuple[str, ...]] = {
    "h_tqs_1_rows": ("tqs_scores", "candidate_outcomes", "trade_proposals"),
    "h_cat_1_rows": ("candidate_catalysts", "candidate_outcomes", "trade_proposals"),
    "h_int_1_rows": ("candidates", "candidate_outcomes", "trade_proposals"),
    "h_win_1_rows": ("candidates", "scan_batches", "candidate_outcomes", "trade_proposals"),
    "h_ttl_1_rows": ("trade_proposals", "candidate_outcomes"),
    "h_rej_1_rows": ("attribution_records",),
    "h_pol_1_rows": ("last30days_polarity", "candidate_outcomes", "trade_proposals"),
}

# The metric's own already-written documentation text (constants.py's
# SEEDED_HYPOTHESES) -- reused verbatim as the generated hypothesis's
# metric_description. Since metric_fn_name is drawn from the SAME whitelist
# a seeded hypothesis already uses, it computes the exact same numbers; the
# description of what those numbers ARE does not change just because a
# different claim/direction is being tested against them.
_SEEDED_METRIC_DESCRIPTION: dict[str, str] = {
    h["metric_fn_name"]: h["metric"] for h in SEEDED_HYPOTHESES if h["metric_fn_name"]
}

# The metric's own base risk class (constants.py's SEEDED_HYPOTHESES) -- the
# mechanical classifier's starting point (see classify_risk()'s own
# docstring for the "reusing an existing metric_fn_name inherits its
# existing evidentiary risk class" rule + the card-linkage step-up rule).
_SEEDED_METRIC_BASE_CLASS: dict[str, str] = {
    h["metric_fn_name"]: h["risk_class"] for h in SEEDED_HYPOTHESES if h["metric_fn_name"]
}


class CandidateSchemaError(ValueError):
    """Raised by ``validate_candidate_schema()`` on ANY schema violation --
    loud and immediate, never a silent coercion. Lists every violation found
    (not just the first), matching this codebase's "an operator-invoked
    action gets a complete error, not a first-failure stub" convention (see
    ``registry.mark_hypothesis_status()``'s own ValueError posture)."""


def validate_candidate_schema(candidate: dict) -> None:
    """Strict intake validation for a draft candidate dict. Required keys:
    ``title`` (non-empty str), ``claim_text`` (non-empty str),
    ``metric_fn_name`` (must be in ``METRIC_WHITELIST`` -- schema.py's
    ``hypothesis_drafts.metric_fn_name`` column stays nullable for a
    hypothetical future H-AI-1-shaped row, but intake itself requires a
    real, computable metric; "do not invent new ones" per the build spec),
    ``proposed_risk_class`` (one of A/B/C), ``direction`` (one of positive/
    negative/either). Optional: ``card_id`` (str or None).

    Raises ``CandidateSchemaError`` collecting EVERY violation found, never
    just the first -- an operator (or a generator producing a batch) should
    see the complete picture in one pass, not fix-and-retry one field at a
    time."""
    violations: list[str] = []

    title = candidate.get("title")
    if not isinstance(title, str) or not title.strip():
        violations.append("title: required non-empty string")

    claim_text = candidate.get("claim_text")
    if not isinstance(claim_text, str) or not claim_text.strip():
        violations.append("claim_text: required non-empty string")

    metric_fn_name = candidate.get("metric_fn_name")
    if metric_fn_name not in METRIC_WHITELIST:
        violations.append(
            f"metric_fn_name: {metric_fn_name!r} not in the whitelist {sorted(METRIC_WHITELIST)} "
            "-- a draft may only reuse an existing, resolver-computable metric function, never "
            "invent a new one"
        )

    proposed_risk_class = candidate.get("proposed_risk_class")
    if proposed_risk_class not in _VALID_RISK_CLASSES:
        violations.append(
            f"proposed_risk_class: {proposed_risk_class!r} must be one of {sorted(_VALID_RISK_CLASSES)}"
        )

    direction = candidate.get("direction")
    if direction not in _VALID_DIRECTIONS:
        violations.append(f"direction: {direction!r} must be one of {sorted(_VALID_DIRECTIONS)}")

    card_id = candidate.get("card_id")
    if card_id is not None and not isinstance(card_id, str):
        violations.append(f"card_id: must be a string or None, got {type(card_id).__name__}")

    if violations:
        raise CandidateSchemaError(
            f"candidate failed schema validation ({len(violations)} violation(s)): "
            + "; ".join(violations)
        )


def _normalize_text(s: str) -> str:
    """Case/whitespace-insensitive normalization for exact-match duplicate
    comparison -- collapse runs of whitespace, lowercase, strip. Deliberately
    NOT fuzzy/NLP similarity: "hard-block" per the build spec means a
    confident, explainable match, not a probabilistic one an operator would
    have to second-guess."""
    return " ".join(s.strip().lower().split())


def check_duplicate(journal, candidate: dict) -> dict:
    """Hard-block duplicate detection against BOTH ``hypothesis_proposals``
    (the real, already-registered hypotheses -- including all 8 seeded ones)
    AND existing non-rejected ``hypothesis_drafts`` rows (status in
    ``draft``/``accepted`` -- a rejected draft is not "still in flight" and
    must not itself block a fresh attempt).

    Two independent match rules, either is sufficient:
    1. Normalized title/claim_text exact match (case/whitespace-insensitive)
       against the OTHER side's title-or-claim text.
    2. ``metric_fn_name`` + ``direction`` EXACT match (not a wildcard on
       "either" -- an earlier design that treated "either" as matching any
       direction was rejected: every seeded hypothesis whose metric_fn_name
       happens to be framed as "either direction informative" would then
       hard-block EVERY future generated claim on that same metric,
       regardless of direction, which defeats the point of allowing v0 to
       explore genuinely different claims about an already-whitelisted
       metric). For comparison against ``hypothesis_proposals`` (which
       carries no structured direction field), the seeded hypothesis's own
       reference direction (``_SEEDED_METRIC_DIRECTION``, derived from its
       claim text) stands in.

    Returns ``{"is_duplicate": bool, "match_type": Optional[str],
    "matched_against": Optional[dict], "checked_proposals": int,
    "checked_drafts": int}``. A duplicate is never silently dropped -- the
    caller (``intake_draft()``) records this result on a REJECTED draft row,
    never omits the row entirely, so the attempt itself is auditable."""
    norm_title = _normalize_text(candidate["title"])
    norm_claim = _normalize_text(candidate["claim_text"])
    metric_fn_name = candidate["metric_fn_name"]
    direction = candidate["direction"]

    proposals = journal.query("SELECT hypothesis_id, claim, metric_fn_name FROM hypothesis_proposals")
    for row in proposals:
        if _normalize_text(row["claim"]) in (norm_title, norm_claim):
            return {
                "is_duplicate": True, "match_type": "text_match_hypothesis_proposals",
                "matched_against": {"hypothesis_id": row["hypothesis_id"]},
                "checked_proposals": len(proposals), "checked_drafts": 0,
            }
        ref_direction = _SEEDED_METRIC_DIRECTION.get(row["metric_fn_name"])
        if row["metric_fn_name"] == metric_fn_name and ref_direction == direction:
            return {
                "is_duplicate": True, "match_type": "metric_direction_match_hypothesis_proposals",
                "matched_against": {"hypothesis_id": row["hypothesis_id"]},
                "checked_proposals": len(proposals), "checked_drafts": 0,
            }

    drafts = journal.query(
        "SELECT draft_id, title, claim_text, metric_fn_name, direction FROM hypothesis_drafts "
        "WHERE status != ?",
        (DraftStatus.REJECTED.value,),
    )
    for row in drafts:
        if _normalize_text(row["title"]) == norm_title or _normalize_text(row["claim_text"]) == norm_claim:
            return {
                "is_duplicate": True, "match_type": "text_match_hypothesis_drafts",
                "matched_against": {"draft_id": row["draft_id"]},
                "checked_proposals": len(proposals), "checked_drafts": len(drafts),
            }
        if row["metric_fn_name"] == metric_fn_name and row["direction"] == direction:
            return {
                "is_duplicate": True, "match_type": "metric_direction_match_hypothesis_drafts",
                "matched_against": {"draft_id": row["draft_id"]},
                "checked_proposals": len(proposals), "checked_drafts": len(drafts),
            }

    return {
        "is_duplicate": False, "match_type": None, "matched_against": None,
        "checked_proposals": len(proposals), "checked_drafts": len(drafts),
    }


def check_evidence_availability(journal, metric_fn_name: str) -> dict:
    """Whether ``metric_fn_name``'s claimed evidence is actually computable:
    in the whitelist AND every table its query reads from actually exists
    (introspected via ``journal.query()`` against SQLite's own
    ``sqlite_master`` -- the same generic query surface every other read in
    this codebase uses, not a private/test-only helper). Returns
    ``{"metric_fn_name", "required_tables", "missing_tables", "available"}``
    -- recorded verbatim as a draft's ``evidence_check_json``."""
    if metric_fn_name not in METRIC_WHITELIST:
        return {
            "metric_fn_name": metric_fn_name, "required_tables": [],
            "missing_tables": [], "available": False,
        }
    required = _METRIC_REQUIRED_TABLES.get(metric_fn_name, ())
    existing = {
        r["name"] for r in journal.query("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing = [t for t in required if t not in existing]
    return {
        "metric_fn_name": metric_fn_name, "required_tables": list(required),
        "missing_tables": missing, "available": not missing,
    }


def classify_risk(candidate: dict) -> dict:
    """Mechanical risk classification -- PURE, no DB access. Base class comes
    from the SEEDED hypothesis that already uses this same ``metric_fn_name``
    (``_SEEDED_METRIC_BASE_CLASS``, sourced from constants.py's own frozen
    class definitions per the build spec's "rules derived from constants.py's
    existing class definitions" instruction): reusing an already-classified,
    already-whitelisted metric function inherits that metric's own
    evidentiary risk class, since nothing about the QUERY changed, only the
    claim/direction being tested against it.

    Ambiguity trigger (the one case this v0 classifier recognizes): the
    candidate specifies a ``card_id``. A card-linked hypothesis can gate a
    real card promotion decision (``alphaos.cards.promotion``/
    ``autonomy_readiness``) -- a strictly bigger blast radius than the bare
    evidentiary claim the base class was calibrated for -- so ANY card
    linkage on a generated/manual draft steps the mechanical class UP one
    notch (capped at C), never down, regardless of what the base class or
    the candidate's own ``proposed_risk_class`` say.

    The mechanical class ALWAYS wins over ``proposed_risk_class`` when they
    differ (both are recorded; floors are still derived exclusively from
    the mechanical class via ``RISK_CLASS_FLOORS`` inside
    ``registry.propose_hypothesis()`` -- this function never computes or
    returns a floor itself).

    Returns ``{"mechanical_risk_class", "proposed_risk_class", "ambiguous",
    "reason"}``."""
    metric_fn_name = candidate["metric_fn_name"]
    proposed = candidate["proposed_risk_class"]
    card_id = candidate.get("card_id")

    base_class = _SEEDED_METRIC_BASE_CLASS.get(metric_fn_name)
    if base_class is None:
        # Defensive fallback -- unreachable via validate_candidate_schema()
        # (which already enforces metric_fn_name in METRIC_WHITELIST, and
        # every whitelisted name has a seeded base class by construction),
        # but "ambiguity -> default UP" means "can't determine at all"
        # defaults to the strictest class, never a guess in the middle.
        return {
            "mechanical_risk_class": RiskClass.C.value, "proposed_risk_class": proposed,
            "ambiguous": True,
            "reason": f"no known base risk class for metric_fn_name={metric_fn_name!r}; defaulted to C",
        }

    if card_id is not None:
        stepped = RISK_CLASS_STEP_UP.get(base_class, base_class)
        return {
            "mechanical_risk_class": stepped, "proposed_risk_class": proposed,
            "ambiguous": True,
            "reason": (
                f"card_id={card_id!r} linkage steps the base class ({base_class}) up to "
                f"{stepped} -- a card-linked hypothesis can gate a real promotion decision, "
                "a bigger blast radius than the bare metric claim"
            ),
        }

    return {
        "mechanical_risk_class": base_class, "proposed_risk_class": proposed,
        "ambiguous": False,
        "reason": f"inherited from metric_fn_name={metric_fn_name!r}'s own seeded risk class",
    }


def intake_draft(
    journal,
    candidate: dict,
    source: str,
    model_id: Optional[str] = None,
    model_provider: Optional[str] = None,
    prompt_hash: Optional[str] = None,
    system_prompt_hash: Optional[str] = None,
    lineage_id: Optional[str] = None,
) -> dict:
    """The single chokepoint every candidate passes through before a
    ``hypothesis_drafts`` row exists. ``source`` must be ``'generated'`` or
    ``'manual'``.

    1. ``validate_candidate_schema()`` -- raises ``CandidateSchemaError``
       (no row written at all) on ANY violation. Schema errors are an
       intake-time hard failure, never coerced, never quarantined as a
       rejected row (there is nothing valid to even quarantine).
    2. ``check_duplicate()`` -- a duplicate is NOT raised; it is recorded as
       a ``status='rejected'`` draft row with ``rejected_reason`` set, so
       the attempt stays auditable (never silently dropped).
    3. ``check_evidence_availability()`` + ``classify_risk()`` -- run
       regardless of the duplicate outcome, so a rejected row is still fully
       informative for an operator reviewing why.

    Returns the inserted ``hypothesis_drafts`` row (whatever its status)."""
    if source not in ("generated", "manual"):
        raise ValueError(f"intake_draft: source must be 'generated' or 'manual', got {source!r}")

    validate_candidate_schema(candidate)

    dup = check_duplicate(journal, candidate)
    evidence = check_evidence_availability(journal, candidate["metric_fn_name"])
    risk = classify_risk(candidate)

    status = DraftStatus.REJECTED.value if dup["is_duplicate"] else DraftStatus.DRAFT.value
    rejected_reason = (
        f"duplicate ({dup['match_type']}) of {dup['matched_against']}" if dup["is_duplicate"] else None
    )

    draft_id = new_id("hdraft")
    journal.insert("hypothesis_drafts", {
        "draft_id": draft_id,
        "title": candidate["title"],
        "claim_text": candidate["claim_text"],
        "metric_fn_name": candidate["metric_fn_name"],
        "direction": candidate["direction"],
        "card_id": candidate.get("card_id"),
        "proposed_risk_class": candidate["proposed_risk_class"],
        "mechanical_risk_class": risk["mechanical_risk_class"],
        "status": status,
        "source": source,
        "model_id": model_id,
        "model_provider": model_provider,
        "prompt_hash": prompt_hash,
        "system_prompt_hash": system_prompt_hash,
        "lineage_id": lineage_id,
        "evidence_check_json": evidence,
        "duplicate_check_json": dup,
        "rejected_reason": rejected_reason,
    })
    return journal.one("SELECT * FROM hypothesis_drafts WHERE draft_id = ?", (draft_id,))


# -------------------------------------------------------------- accept/reject
MAX_CONCURRENT_TESTING_GENERATED = 4


def _count_concurrent_testing_generated(journal) -> int:
    """How many generated-source drafts are ALREADY accepted into
    hypothesis_proposals AND still sitting in status='testing' -- the
    concurrent-testing acceptance cap (build spec #4). Traced via
    ``accepted_hypothesis_id`` (the only link from a draft to its real
    registry row), never a separate counter column that could drift from
    the two tables' own actual state."""
    row = journal.one(
        "SELECT COUNT(*) AS n FROM hypothesis_drafts d "
        "JOIN hypothesis_proposals p ON p.hypothesis_id = d.accepted_hypothesis_id "
        "WHERE d.source = 'generated' AND d.status = 'accepted' AND p.status = 'testing'"
    )
    return row["n"] if row else 0


def accept_draft(journal, draft_id: str, decided_by: str, now=None) -> dict:
    """The authorship act -- the ONLY path from a quarantined draft to the
    real registry. Calls ``registry.propose_hypothesis()`` with the draft's
    own fields, using the MECHANICAL risk class (never ``proposed_risk_class``
    -- floors remain non-settable by construction, exactly like every seeded
    spec). Raises ``ValueError`` on any misuse (unknown draft_id, wrong
    status, the concurrent-testing acceptance cap, ``decided_by='system'``)
    -- an operator-invoked CLI action gets a loud, immediate error, never a
    silently swallowed no-op (same posture as
    ``registry.mark_hypothesis_status()``)."""
    if decided_by == "system":
        raise ValueError("accept_draft: decided_by must be a real operator identity, not 'system'")

    draft = journal.one("SELECT * FROM hypothesis_drafts WHERE draft_id = ?", (draft_id,))
    if draft is None:
        raise ValueError(f"no such draft_id: {draft_id!r}")
    if draft["status"] != DraftStatus.DRAFT.value:
        raise ValueError(
            f"{draft_id!r} is {draft['status']!r}, not 'draft' -- only a pending draft can be accepted"
        )

    concurrent = _count_concurrent_testing_generated(journal)
    if draft["source"] == "generated" and concurrent >= MAX_CONCURRENT_TESTING_GENERATED:
        raise ValueError(
            f"CONCURRENT_TESTING_CAP: {concurrent} generated-source hypotheses are already "
            f"'testing' in the registry (cap={MAX_CONCURRENT_TESTING_GENERATED}) -- accept fewer "
            "concurrently, or wait for one to resolve"
        )

    metric_fn_name = draft["metric_fn_name"]
    metric_description = _SEEDED_METRIC_DESCRIPTION.get(
        metric_fn_name, f"{metric_fn_name} (whitelisted metric; direction={draft['direction']})"
    )
    spec = {
        "hypothesis_id": new_id("H-GEN"),
        "risk_class": draft["mechanical_risk_class"],
        "claim": draft["claim_text"],
        "metric": metric_description,
        "success_floor": 0.0,  # documentation only, never a mechanical gate -- see constants.py's own module docstring
        "metric_fn_name": metric_fn_name,
        "card_id": draft.get("card_id"),
    }
    hyp_row = propose_hypothesis(journal, spec, now=now)

    from alphaos.util import timeutils
    stamp = timeutils.stamp(now)
    journal.conn.execute(
        "UPDATE hypothesis_drafts SET status = ?, accepted_hypothesis_id = ?, "
        "accepted_at_utc = ?, accepted_by = ? WHERE draft_id = ? AND status = 'draft'",
        (DraftStatus.ACCEPTED.value, hyp_row["hypothesis_id"], stamp.utc, decided_by, draft_id),
    )
    journal.conn.commit()

    journal.log_system_event(
        Severity.INFO, "hypothesis_drafts",
        f"Draft {draft_id} accepted by {decided_by} -> {hyp_row['hypothesis_id']} "
        f"(risk_class={draft['mechanical_risk_class']}).",
        {"draft_id": draft_id, "hypothesis_id": hyp_row["hypothesis_id"], "decided_by": decided_by},
    )
    return journal.one("SELECT * FROM hypothesis_drafts WHERE draft_id = ?", (draft_id,))


def reject_draft(journal, draft_id: str, decided_by: str, reason: str) -> dict:
    """Record an operator rejection of a pending draft. Raises ``ValueError``
    on unknown draft_id / wrong status / ``decided_by='system'``."""
    if decided_by == "system":
        raise ValueError("reject_draft: decided_by must be a real operator identity, not 'system'")
    if not reason or not reason.strip():
        raise ValueError("reject_draft: reason is required")

    draft = journal.one("SELECT * FROM hypothesis_drafts WHERE draft_id = ?", (draft_id,))
    if draft is None:
        raise ValueError(f"no such draft_id: {draft_id!r}")
    if draft["status"] != DraftStatus.DRAFT.value:
        raise ValueError(
            f"{draft_id!r} is {draft['status']!r}, not 'draft' -- only a pending draft can be rejected"
        )

    cursor = journal.conn.execute(
        "UPDATE hypothesis_drafts SET status = ?, rejected_reason = ? WHERE draft_id = ? AND status = 'draft'",
        (DraftStatus.REJECTED.value, reason, draft_id),
    )
    journal.conn.commit()
    if cursor.rowcount == 0:
        raise ValueError(f"{draft_id!r} was changed by a concurrent operator between read and write")

    journal.log_system_event(
        Severity.INFO, "hypothesis_drafts",
        f"Draft {draft_id} rejected by {decided_by}: {reason}",
        {"draft_id": draft_id, "decided_by": decided_by, "reason": reason},
    )
    return journal.one("SELECT * FROM hypothesis_drafts WHERE draft_id = ?", (draft_id,))


def list_drafts(journal, status: Optional[str] = None) -> list[dict]:
    """PURE READ -- every draft (optionally filtered by status), newest first."""
    if status is not None:
        return journal.query(
            "SELECT * FROM hypothesis_drafts WHERE status = ? ORDER BY id DESC", (status,)
        )
    return journal.query("SELECT * FROM hypothesis_drafts ORDER BY id DESC")
