"""Validation + safe coercion for AI category/playbook label output (Roadmap 2.3).

Prompt wording is not trusted. The AI labeller's output is coerced here so the
hard rules ALWAYS hold, regardless of what the model returns:

* ``primary_label`` MUST be in ``OFFICIAL_LABELS`` — anything else is coerced to
  ``Other/Unclassified`` and the decision floored to WATCH. The AI can never mint
  a new official label.
* non-official secondary labels + any ``suggested_new_tags`` are kept ONLY as
  UNOFFICIAL ``candidate_tags`` — never promoted.
* ``Other/Unclassified`` never auto-proposes.
* a decision below the confidence floor cannot propose.
* missing/garbage fields degrade safely; this function never raises.

The label decision is ADVISORY: downstream it can only DOWNGRADE the trade
decision, never create a PROPOSE. So a malformed label is always safe.
"""

from __future__ import annotations

from typing import Optional

from alphaos.constants import Decision, LABEL_OTHER, OFFICIAL_LABELS, ProposalReadiness

# Required keys in a well-formed AI label object.
LABEL_OUTPUT_KEYS = [
    "symbol", "primary_label", "secondary_labels", "direction", "decision",
    "confidence", "reason_for_label", "thesis_stub", "invalidation", "main_risk",
    "risk_tags", "missing_context", "suggested_new_tags",
]

THESIS_MAX_CHARS = 400

_READINESS = {r.value for r in ProposalReadiness}

_DECISION_MAP = {
    "propose": Decision.PROPOSE.value,
    "watch": Decision.WATCH.value,
    "reject": Decision.REJECT.value,
}


def normalize_decision(raw) -> Optional[str]:
    return _DECISION_MAP.get(str(raw or "").strip().lower())


def _as_list(v) -> list:
    return list(v) if isinstance(v, list) else []


def coerce_and_validate(obj: dict, settings) -> tuple[dict, str]:
    """Return ``(clean_fields, validation_status)``. Never raises.

    ``validation_status`` is ``"passed"`` for clean official output, else a short
    failure reason (``invalid_label`` / ``missing_decision`` / ``other_downgraded``
    / ``low_confidence``). The returned fields are always safe to persist + act on.
    """
    status = "passed"
    primary = str(obj.get("primary_label") or "").strip()

    secondary_raw = _as_list(obj.get("secondary_labels"))
    suggested = [str(t) for t in _as_list(obj.get("suggested_new_tags")) if t]
    secondary = [s for s in secondary_raw if s in OFFICIAL_LABELS]
    unofficial = [str(s) for s in secondary_raw if s not in OFFICIAL_LABELS] + suggested

    decision = normalize_decision(obj.get("decision"))
    try:
        confidence = max(0.0, min(1.0, float(obj.get("confidence"))))
    except (TypeError, ValueError):
        confidence = 0.0

    # The AI may ONLY pick an official primary label.
    if primary not in OFFICIAL_LABELS:
        if primary:
            unofficial = [primary] + unofficial
        primary = LABEL_OTHER
        decision = Decision.WATCH.value
        status = "invalid_label"

    if decision is None:
        decision = Decision.WATCH.value
        if status == "passed":
            status = "missing_decision"

    # Other/Unclassified never auto-proposes.
    if primary == LABEL_OTHER and decision == Decision.PROPOSE.value:
        decision = Decision.WATCH.value
        if status == "passed":
            status = "other_downgraded"

    # Low confidence cannot propose.
    if decision == Decision.PROPOSE.value and confidence < settings.label_min_confidence_to_propose:
        decision = Decision.WATCH.value
        if status == "passed":
            status = "low_confidence"

    clean = {
        "primary_label": primary,
        "secondary_labels": secondary,
        "candidate_tags": [t for t in unofficial if t],   # UNOFFICIAL only
        "risk_tags": [str(t) for t in _as_list(obj.get("risk_tags"))],
        "direction": str(obj.get("direction") or "none").strip().lower(),
        "decision": decision,
        "confidence": round(confidence, 3),
        "reason_for_label": str(obj.get("reason_for_label") or "")[:THESIS_MAX_CHARS],
        "thesis_stub": str(obj.get("thesis_stub") or "")[:THESIS_MAX_CHARS],
        "invalidation": str(obj.get("invalidation") or "")[:THESIS_MAX_CHARS],
        "main_risk": str(obj.get("main_risk") or "")[:THESIS_MAX_CHARS],
        "missing_context": [str(m) for m in _as_list(obj.get("missing_context"))],
        "suggested_new_tags": suggested,
        # Roadmap 2.8 (Part B) — ADVISORY reasoning only. These NEVER change the
        # decision (already finalized above); they explain WHY and what would
        # upgrade the candidate, for Armed Watch visibility + learning.
        "missing_conditions": [str(m) for m in _as_list(obj.get("missing_conditions"))],
        "upgrade_blockers": [str(b) for b in _as_list(obj.get("upgrade_blockers"))],
        "proposal_readiness": (str(obj.get("proposal_readiness") or "").strip().lower()
                               if str(obj.get("proposal_readiness") or "").strip().lower() in _READINESS
                               else ProposalReadiness.UNCLEAR.value),
        "what_would_upgrade": str(obj.get("what_would_upgrade") or "")[:THESIS_MAX_CHARS],
    }
    return clean, status
