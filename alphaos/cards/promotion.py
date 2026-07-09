"""PR13 slice 2: the card promotion/manual-demotion state machine.

Fable5 consult (2026-07-10, focused, not full-panel) drew the load-bearing
distinction this module implements: **graduation vs. mutation**.
*Graduation* moves an EXISTING card version's state from ``shadow`` to
``live_eligible`` -- content untouched, no new version minted. *Mutation*
would mint a new version with real content changes (PR13.5's own
diff-materialization ceremony) -- but PR12 proposes no diff content today
(deferred to v1.1 per PD#4), so mutation has no producer and is
deliberately NOT built here. Every one of the 8 seeded hypotheses that
names a card (H-CAT-1, H-POL-1) is graduation-shaped ("does this shadow
card's thesis hold"), never mutation-shaped ("change 0.66 to 0.60") -- so
v0 needs only the graduation half, fully built and fixture-tested, armed
and waiting for real evidence rather than "built but inert" the way EXP-1
currently is.

Card state transitions ONLY happen through this module (spec's own law).
This module NEVER writes to ``setup_cards`` or any ``cards/*.yaml`` file --
graduation changes what a card is ALLOWED to do (live_eligible vs shadow),
never what it IS (its own registered, YAML-sourced content); see
``card_promote()``'s own docstring for exactly what gets written instead.

Gates on OPERATOR-SET ``MET`` (via
``alphaos.hypotheses.registry.mark_hypothesis_status()``), never the raw
resolver verdict -- ``HypothesisStatus``'s own docstring warns that
auto-mapping a PORT-1 verdict to MET/FAILED risks a silent sign error
(H-POL-1 is negative-claimed: its CONFIRMING statistical outcome is
"divergent underperforms", i.e. PORT-1's own ``rejected`` verdict). "This
hypothesis's claim resolved TRUE" is itself a human adjudication, kept
strictly separate from this module's own, purely mechanical precondition
checks.

Terminal demotion (anti-double-jeopardy, spec audit B3) is checked against
BOTH ``card_demotions`` (slice 1's own automatic-trigger-only table, left
completely unmodified -- "don't reopen it" was this same consult's
explicit ruling) and this module's own ``promotion_decisions`` (manual
decisions, ``direction='demote'``) -- the full transition history is a
reporting-level UNION of the two tables, never a merged/shared one.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from alphaos.cards.scoreboard import compute_card_scoreboard
from alphaos.hypotheses.constants import HypothesisStatus, RiskClass
from alphaos.util import timeutils
from alphaos.util.ids import new_id

# q_value must be strictly below this to count as "reliable enough to act
# on" -- matches PORT-1's own DEFAULT_FDR_Q (alphaos.stats.fdr), reused
# verbatim rather than a second literal.
Q_VALUE_FLOOR = 0.1


def is_terminally_demoted(journal, card_id: str, card_version: int) -> bool:
    """True iff this exact (card_id, card_version) has EVER been demoted,
    by either mechanism (automatic via card_demotions, or manual via this
    module's own promotion_decisions). Per the spec's own anti-double-
    jeopardy law, a demoted VERSION is terminal -- this check never
    expires, never resets, and is the same check both ``card_promote()``
    and ``alphaos.cards.scoreboard.live_eligible_cards()`` rely on."""
    auto = journal.one(
        "SELECT 1 FROM card_demotions WHERE card_id = ? AND card_version = ?",
        (card_id, card_version),
    )
    if auto:
        return True
    manual = journal.one(
        "SELECT 1 FROM promotion_decisions WHERE card_id = ? AND card_version = ? AND direction = 'demote'",
        (card_id, card_version),
    )
    return manual is not None


def _latest_card_row(journal, card_id: str) -> Optional[dict]:
    return journal.one(
        "SELECT card_id, version AS card_version, state FROM setup_cards "
        "WHERE card_id = ? ORDER BY version DESC LIMIT 1",
        (card_id,),
    )


def check_promotion_preconditions(
    journal, hypothesis_id: str, research_ref: Optional[str] = None,
) -> dict:
    """PURE READ. Every precondition the spec names, checked in a fixed
    order, returning the FIRST unmet one as a loud, specific reason code --
    never a bare True/False. Returns:
    ``{"eligible": bool, "reason_code": Optional[str], "detail": str,
    "card_id": Optional[str], "card_version": Optional[int]}``.

    Reason codes: ``HYPOTHESIS_NOT_FOUND``, ``NO_CARD_ID`` (this hypothesis
    doesn't gate any card -- most of the 8 seeded ones don't),
    ``HYPOTHESIS_NOT_MET``, ``CARD_NOT_REGISTERED``, ``CARD_NOT_SHADOW``,
    ``CARD_VERSION_TERMINALLY_DEMOTED``, ``ALREADY_PROMOTED``,
    ``FLOORS_NOT_MET``, ``Q_VALUE_FLOOR``, ``RESEARCH_REF_MISSING``
    (risk_class='C' only -- PD#9, enforced at the actuator; unreachable
    today since no Class C hypothesis names a card, fixture-tested only).
    """
    h = journal.one("SELECT * FROM hypothesis_proposals WHERE hypothesis_id = ?", (hypothesis_id,))
    if h is None:
        return {"eligible": False, "reason_code": "HYPOTHESIS_NOT_FOUND",
                "detail": f"no such hypothesis_id: {hypothesis_id!r}", "card_id": None, "card_version": None}

    if not h["card_id"]:
        return {"eligible": False, "reason_code": "NO_CARD_ID",
                "detail": f"{hypothesis_id} does not gate any card promotion", "card_id": None, "card_version": None}

    card_id = h["card_id"]

    if h["status"] != HypothesisStatus.MET.value:
        return {"eligible": False, "reason_code": "HYPOTHESIS_NOT_MET",
                "detail": f"{hypothesis_id} status is {h['status']!r}, not 'met' -- an operator must "
                          "adjudicate it via mark_hypothesis_status() first",
                "card_id": card_id, "card_version": None}

    card = _latest_card_row(journal, card_id)
    if card is None:
        return {"eligible": False, "reason_code": "CARD_NOT_REGISTERED",
                "detail": f"no setup_cards row for card_id={card_id!r} -- the card must be registered "
                          "(state='shadow') before its gating hypothesis can promote it",
                "card_id": card_id, "card_version": None}

    card_version = card["card_version"]

    if card["state"] != "shadow":
        return {"eligible": False, "reason_code": "CARD_NOT_SHADOW",
                "detail": f"{card_id} v{card_version}'s registered state is {card['state']!r}, not 'shadow'",
                "card_id": card_id, "card_version": card_version}

    if is_terminally_demoted(journal, card_id, card_version):
        return {"eligible": False, "reason_code": "CARD_VERSION_TERMINALLY_DEMOTED",
                "detail": f"{card_id} v{card_version} was previously demoted -- terminal per the "
                          "anti-double-jeopardy law; only a NEW version can re-enter live_eligible",
                "card_id": card_id, "card_version": card_version}

    already = journal.one(
        "SELECT 1 FROM promotion_decisions WHERE card_id = ? AND card_version = ? AND direction = 'promote'",
        (card_id, card_version),
    )
    if already:
        return {"eligible": False, "reason_code": "ALREADY_PROMOTED",
                "detail": f"{card_id} v{card_version} already has a promotion decision on record",
                "card_id": card_id, "card_version": card_version}

    score = compute_card_scoreboard(journal, card_id, card_version)
    if not score["clears_floor"]:
        return {"eligible": False, "reason_code": "FLOORS_NOT_MET",
                "detail": f"{card_id} v{card_version}'s own scoreboard has not cleared its floor yet "
                          f"(effective_n={score['effective_n']}, span_days={score['span_days']})",
                "card_id": card_id, "card_version": card_version}

    q = h["last_q_value"]
    if q is None or q >= Q_VALUE_FLOOR:
        return {"eligible": False, "reason_code": "Q_VALUE_FLOOR",
                "detail": f"{hypothesis_id}'s last_q_value is {q!r}, not < {Q_VALUE_FLOOR}",
                "card_id": card_id, "card_version": card_version}

    if h["risk_class"] == RiskClass.C.value and not research_ref:
        return {"eligible": False, "reason_code": "RESEARCH_REF_MISSING",
                "detail": f"{hypothesis_id} is risk_class='C' -- a research_ref is required (PD#9)",
                "card_id": card_id, "card_version": card_version}

    return {"eligible": True, "reason_code": None, "detail": "all preconditions met",
            "card_id": card_id, "card_version": card_version}


def promote_card(
    journal, hypothesis_id: str, decided_by: str, research_ref: Optional[str] = None,
) -> dict:
    """Graduate the card named by ``hypothesis_id`` from shadow to
    live_eligible. Writes ONE ``promotion_decisions`` row
    (direction='promote') -- never touches ``setup_cards`` or any YAML
    file (graduation changes eligibility, not content). Refuses (raises
    ``ValueError`` with the precondition's own reason_code + detail) unless
    ``check_promotion_preconditions()`` returns eligible.

    ``decided_by`` must not be ``'system'`` (Prime Directive 3 -- promotion
    is never automatic, enforced here even though no automated caller of
    this function exists anywhere in this codebase)."""
    if decided_by == "system":
        raise ValueError("promote_card: decided_by must be a real operator identity, not 'system'")

    check = check_promotion_preconditions(journal, hypothesis_id, research_ref)
    if not check["eligible"]:
        raise ValueError(f"{check['reason_code']}: {check['detail']}")

    h = journal.one("SELECT * FROM hypothesis_proposals WHERE hypothesis_id = ?", (hypothesis_id,))
    now = timeutils.stamp()
    decision_id = new_id("promodec")
    evidence = {
        "hypothesis_status": h["status"],
        "hypothesis_claim": h["claim"],
        "last_verdict": h["last_verdict"],
        "last_q_value": h["last_q_value"],
        "last_reason": h["last_reason"],
    }
    try:
        journal.insert("promotion_decisions", {
            "decision_id": decision_id,
            "card_id": check["card_id"],
            "card_version": check["card_version"],
            "from_state": "shadow",
            "to_state": "live_eligible",
            "direction": "promote",
            "trigger": "manual",
            "hypothesis_id": hypothesis_id,
            "preregistration_id": h["prereg_id"],
            "decided_by": decided_by,
            "research_ref": research_ref,
            "evidence_json": evidence,
            "decided_at_utc": now.utc,
            "decided_at_sgt": now.local_sgt,
        })
    except sqlite3.IntegrityError as exc:
        raise ValueError(
            f"promote_card: {check['card_id']} v{check['card_version']} was promoted or demoted by a "
            f"concurrent decision between the precondition check and this write: {exc}"
        ) from exc
    return journal.one("SELECT * FROM promotion_decisions WHERE decision_id = ?", (decision_id,))


def demote_card(journal, card_id: str, card_version: int, decided_by: str, reason: str) -> dict:
    """Manual override demotion -- an operator's own judgment call, not
    evidence-gated (unlike promotion, no hypothesis/preregistration backing
    is required; an operator may demote for any reason, including
    "I don't trust this card", and that is intentionally a LOWER bar than
    promotion's, since removing trading permission is the safer direction
    to err in). Writes to THIS module's own ``promotion_decisions``
    (direction='demote') -- never to slice 1's ``card_demotions``, which
    stays exclusively the automatic-trigger table. Refuses if this exact
    (card_id, card_version) is already terminally demoted by either
    mechanism (a no-op demote-of-a-demoted-card is a caller bug, not a
    silent success)."""
    if decided_by == "system":
        raise ValueError("demote_card: decided_by must be a real operator identity, not 'system'")

    card = journal.one(
        "SELECT card_id, version AS card_version, state FROM setup_cards "
        "WHERE card_id = ? AND version = ?",
        (card_id, card_version),
    )
    if card is None:
        raise ValueError(f"CARD_NOT_REGISTERED: no setup_cards row for {card_id!r} v{card_version}")
    if is_terminally_demoted(journal, card_id, card_version):
        raise ValueError(
            f"CARD_VERSION_TERMINALLY_DEMOTED: {card_id} v{card_version} was already demoted"
        )

    now = timeutils.stamp()
    decision_id = new_id("promodec")
    try:
        journal.insert("promotion_decisions", {
            "decision_id": decision_id,
            "card_id": card_id,
            "card_version": card_version,
            "from_state": card["state"],
            "to_state": card["state"],
            "direction": "demote",
            "trigger": "manual",
            "hypothesis_id": None,
            "preregistration_id": None,
            "decided_by": decided_by,
            "research_ref": None,
            "evidence_json": {"reason": reason},
            "decided_at_utc": now.utc,
            "decided_at_sgt": now.local_sgt,
        })
    except sqlite3.IntegrityError as exc:
        raise ValueError(
            f"demote_card: {card_id} v{card_version} was demoted by a concurrent decision "
            f"between the check and this write: {exc}"
        ) from exc
    return journal.one("SELECT * FROM promotion_decisions WHERE decision_id = ?", (decision_id,))
