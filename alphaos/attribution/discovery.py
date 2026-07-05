"""Attribution v2 -- journal-reading discovery queries (PR8).

READ-ONLY: every function here is a SELECT. Nothing in this module writes to
the journal. Eligibility is re-derived from CURRENT journal state on every
call (never a live/streaming subscription), so re-running discovery after a
backlog is always safe and legacy history is picked up for free the first
time it runs.

Each ``find_*`` function returns source rows (trade_proposals /
user_decision_overrides) eligible for ONE attribution_type that do not
already have an attribution_records row under the CURRENT
ATTRIBUTION_VERSION -- the belt of the idempotency guarantee; the suspenders
are the two partial unique indexes in alphaos/journal/schema.py (see
batch.py, which catches the resulting sqlite3.IntegrityError).
"""

from __future__ import annotations

from typing import Optional

from alphaos.constants import AttributionType, Decision, ProposalStatus


def _not_yet_attributed(journal, *, table: str, id_column: str, attribution_type: str,
                        version: str, status_filter: str, params: tuple, limit: int) -> list[dict]:
    return journal.query(
        f"SELECT * FROM {table} WHERE {status_filter} AND NOT EXISTS ("
        f"  SELECT 1 FROM attribution_records ar WHERE ar.attribution_type = ? "
        f"  AND ar.{id_column} = {table}.{id_column} AND ar.attribution_version = ?"
        f") ORDER BY id ASC LIMIT ?",
        (*params, attribution_type, version, limit),
    )


def find_propose_user_rejected(journal, version: str, limit: int = 200) -> list[dict]:
    return _not_yet_attributed(
        journal, table="trade_proposals", id_column="proposal_id",
        attribution_type=AttributionType.PROPOSE_USER_REJECTED.value, version=version,
        status_filter="status = ?", params=(ProposalStatus.REJECTED.value,), limit=limit,
    )


def find_propose_approved_executed(journal, version: str, limit: int = 200) -> list[dict]:
    return _not_yet_attributed(
        journal, table="trade_proposals", id_column="proposal_id",
        attribution_type=AttributionType.PROPOSE_APPROVED_EXECUTED.value, version=version,
        status_filter="status IN (?, ?, ?)",
        params=(ProposalStatus.APPROVED.value, ProposalStatus.SUBMITTED.value, ProposalStatus.FILLED.value),
        limit=limit,
    )


def find_propose_expired(journal, version: str, limit: int = 200) -> list[dict]:
    return _not_yet_attributed(
        journal, table="trade_proposals", id_column="proposal_id",
        attribution_type=AttributionType.PROPOSE_EXPIRED.value, version=version,
        status_filter="status = ?", params=(ProposalStatus.EXPIRED.value,), limit=limit,
    )


def find_propose_blocked(journal, version: str, limit: int = 200) -> list[dict]:
    return _not_yet_attributed(
        journal, table="trade_proposals", id_column="proposal_id",
        attribution_type=AttributionType.PROPOSE_BLOCKED.value, version=version,
        status_filter="status = ?", params=(ProposalStatus.BLOCKED.value,), limit=limit,
    )


def find_user_override_trade(journal, version: str, limit: int = 200) -> list[dict]:
    """AlphaOS's own final decision was watch/reject/no-propose
    (alphaos_would_have_traded=0, stamped at override-creation time in
    orchestrator.py's _handle_user_override) AND the user's override actually
    produced a trade attempt (user_final_decision='propose' -- the
    WATCH_TO_TRADE/REJECT_TO_TRADE actions). Deliberately excludes
    PROPOSE_TO_REJECT overrides (user_final_decision='reject') -- those flow
    through find_propose_user_rejected instead via the proposal row they
    reject, not through this override-keyed path."""
    return _not_yet_attributed(
        journal, table="user_decision_overrides", id_column="override_id",
        attribution_type=AttributionType.USER_OVERRIDE_TRADE.value, version=version,
        status_filter="alphaos_would_have_traded = 0 AND user_final_decision = ?",
        params=(Decision.PROPOSE.value,), limit=limit,
    )


def candidate_outcome_for_proposal(journal, candidate_id: Optional[str], *,
                                   entry: Optional[float] = None, stop: Optional[float] = None,
                                   target: Optional[float] = None) -> Optional[dict]:
    """The AlphaOS-side candidate_outcomes row whose FROZEN levels match ONE
    specific proposal. candidate_type is 'proposal' or 'blocked' depending on
    the proposal's status AT SEED TIME (see
    alphaos/learning/outcomes_tracker.py::_classify_candidate) -- either
    label satisfies a lookup. Also checks 'user_override' -- an
    override-created proposal's frozen levels are seeded under THAT label
    (see _source_from_override), not 'proposal'/'blocked', so excluding it
    would silently strand every override-origin proposal without a
    counterfactual replay (PR8 audit LOW-2).

    candidate_outcomes seeds AT MOST ONE row of these types per candidate_id
    (frozen at first seed) -- if a candidate later grows a SECOND proposal,
    the existing row's frozen levels belong to the FIRST one. When ``entry``/
    ``stop``/``target`` are supplied (the caller's own proposal's levels),
    a row whose frozen levels don't match is treated as if none exists at
    all (returns None -> the caller's resolve function reports 'pending'/
    'candidate_outcome_not_yet_seeded') rather than silently borrowing a
    different proposal's replay (PR8 audit LOW-1) -- an honest miss, never a
    wrong number."""
    if not candidate_id:
        return None
    rows = journal.query(
        "SELECT * FROM candidate_outcomes WHERE candidate_id = ? "
        "AND candidate_type IN ('proposal', 'blocked', 'user_override') ORDER BY id DESC",
        (candidate_id,),
    )
    if not rows:
        return None
    if entry is None or stop is None or target is None:
        return rows[0]  # no levels to disambiguate against -- best-effort latest
    for row in rows:
        if (row.get("entry_reference_price"), row.get("stop_price"), row.get("target_price")) == \
           (entry, stop, target):
            return row
    return None


def candidate_outcome_for_override(journal, candidate_id: Optional[str]) -> Optional[dict]:
    if not candidate_id:
        return None
    return journal.one(
        "SELECT * FROM candidate_outcomes WHERE candidate_id = ? "
        "AND candidate_type = 'user_override' ORDER BY id DESC LIMIT 1",
        (candidate_id,),
    )


def trade_outcome_for_proposal(journal, proposal_id: Optional[str]) -> Optional[dict]:
    if not proposal_id:
        return None
    return journal.one(
        "SELECT * FROM trade_outcomes WHERE proposal_id = ? ORDER BY id DESC LIMIT 1",
        (proposal_id,),
    )
