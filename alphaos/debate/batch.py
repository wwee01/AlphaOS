"""PR14: Red-Team Debate v0 -- shadow-only, bear-only batch runner.

``score_debate_batch()`` MUST be called exactly once per scan, at the very
end of ``Orchestrator.run_scan_once()``, strictly AFTER every decision for
that batch has already been committed -- mirroring TQS's own call-site
guarantee (see ``alphaos/tqs/__init__.py``'s module docstring) so "the bear
agent cannot influence this scan's decisions" is true by construction, not
merely by discipline.

Scope is deliberately narrower than TQS: only rows that actually became a
PROPOSE decision get a bear vote -- a WATCH/REJECT was never going to be
traded, so debating it teaches nothing about "is this specific trade going
to lose" and would only spend real API budget for zero signal.

NOTE: "PROPOSE decision" is identified via ``candidates.status = 'proposed'``
(set unconditionally, exactly once, only on Orchestrator._handle_proposal's
success path), NOT ``trade_proposals.status``. ``TradeProposal.status``
defaults to the literal string "proposed" in-memory (alphaos/strategy/
proposal.py), but _handle_proposal ALWAYS overwrites it to 'blocked' or
'pending_approval' before the row is ever inserted -- so no row in
trade_proposals ever actually persists with status='proposed'. Worse,
'blocked' is overloaded: both the risk-blocked-before-propose branch AND a
same-call auto-submit-execution-block AFTER a genuine propose use it, so
trade_proposals.status alone cannot disambiguate them. candidates.status is
the one unambiguous signal: 'proposed' only on the real propose path,
'rejected' (via _reject_candidate) on the risk-blocked path.

Cost discipline: bear-debate is a genuinely paid LLM call (unlike TQS, pure
math, zero marginal cost). It is gated by a TIGHTER, separate daily sub-cap
(``check_debate_budget``) nested INSIDE the existing shared 30-day AI cost
cap (``check_scan_budget``) -- both must have room, checked once up front;
the daily sub-cap's remaining count is then tracked with a local decrementing
counter for the rest of the batch (mirroring ``Orchestrator.run_scan_once``'s
own ``enrich_budget`` pattern for last30days/earnings enrichment), not a
fresh DB query per item. If a single batch would exceed the day's remaining
budget, the excess rows are journaled as skipped, never silently dropped.

Fail-safe and idempotent, matching TQS's own posture: any exception voting
on one proposal is caught, logged, and skipped; a second call for the same
scan_batch_id inserts zero new rows (pre-checked here for efficiency, and
enforced regardless at the DB level by ``agent_votes``' own
``idx_agent_votes_proposal_role`` unique index).
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from alphaos import lineage
from alphaos.ai.bear_debater import BearDebater
from alphaos.constants import CandidateStatus, Severity
from alphaos.scheduler.cost_guard import check_debate_budget, check_scan_budget, debate_calls_today
from alphaos.util import timeutils


def _already_voted(journal, proposal_id: str, agent_role: str = "bear") -> bool:
    return journal.one(
        "SELECT 1 FROM agent_votes WHERE proposal_id = ? AND agent_role = ?",
        (proposal_id, agent_role),
    ) is not None


def vote_on_proposal(journal, settings, debater: BearDebater, candidate_row: dict,
                     proposal_row: dict, scan_batch_id: Optional[str]) -> Optional[str]:
    """Cast and store ONE bear vote for one already-committed proposal.
    Never raises: any failure is logged and returns None -- a missing
    shadow vote is a measurement gap, never a reason to touch the real
    pipeline. Skips demo proposals and proposals already voted on (the
    latter making this safe to call twice for the same batch)."""
    try:
        if proposal_row.get("is_demo"):
            return None
        proposal_id = proposal_row["proposal_id"]
        if _already_voted(journal, proposal_id):
            return None
        vote = debater.debate(candidate_row, proposal_row, scan_batch_id)
        row = vote.to_row()
        # AUDIT FIX (correctness/scope-safety, both independently flagged):
        # every other AI-producing table (openai_evaluations, claude_reviews,
        # last30days_polarity, tqs_scores) stamps lineage_id; this one didn't.
        row["lineage_id"] = lineage.get_or_create_lineage_id(journal, settings)
        stamp = timeutils.stamp()
        row["created_at_utc"] = stamp.utc
        row["created_at_sgt"] = stamp.local_sgt
        journal.insert("agent_votes", row)
        return vote.vote_id
    except sqlite3.IntegrityError:
        # AUDIT FIX (correctness NIT): a genuine concurrent double-vote on the
        # same (proposal_id, agent_role) is an expected, idempotent race (the
        # _already_voted pre-check above only narrows the window, same as
        # TQS's own belt-and-suspenders idiom) -- treat it as a silent no-op,
        # matching tqs/batch.py's _insert_result, not a WARNING-level event.
        return None
    except Exception as exc:  # fail-safe: never let debate touch the real pipeline
        journal.log_system_event(
            Severity.WARNING, "debate",
            f"Bear debate failed for proposal {proposal_row.get('proposal_id', '?')}; skipped.",
            {"error": str(exc), "candidate_id": candidate_row.get("candidate_id")},
        )
        return None


def score_debate_batch(journal, settings, scan_batch_id: Optional[str]) -> dict:
    """Vote on every eligible PROPOSE-decision row in ``scan_batch_id``.
    MUST be called exactly once, at the very end of run_scan_once(),
    strictly AFTER the batch's decisions are already committed -- see the
    module docstring. Never raises. Returns a summary dict for the
    caller's own logging only -- nothing reads this dict to make a
    decision."""
    empty = {"voted": 0, "skipped": 0, "budget_exhausted": False}
    if not scan_batch_id:
        return empty

    ok_30d, detail_30d = check_scan_budget(settings, journal)
    if not ok_30d:
        journal.log_system_event(
            Severity.INFO, "debate",
            f"Bear debate sat out scan_batch_id={scan_batch_id}: shared 30d cap -- {detail_30d}.",
        )
        return {"voted": 0, "skipped": 0, "budget_exhausted": True}

    ok_daily, detail_daily = check_debate_budget(settings, journal)
    if not ok_daily:
        journal.log_system_event(
            Severity.INFO, "debate",
            f"Bear debate sat out scan_batch_id={scan_batch_id}: daily cap -- {detail_daily}.",
        )
        return {"voted": 0, "skipped": 0, "budget_exhausted": True}

    try:
        proposals = journal.query(
            "SELECT tp.* FROM trade_proposals tp "
            "JOIN candidates c ON c.candidate_id = tp.candidate_id "
            "WHERE tp.scan_batch_id = ? AND c.status = ?",
            (scan_batch_id, CandidateStatus.PROPOSED.value),
        )
    except Exception as exc:
        journal.log_system_event(
            Severity.WARNING, "debate", "Bear debate batch failed to read proposals; skipped.",
            {"error": str(exc), "scan_batch_id": scan_batch_id},
        )
        return empty

    debater = BearDebater(settings, journal)
    remaining = settings.debate_max_calls_per_day - debate_calls_today(journal)
    voted = 0
    skipped = 0
    skipped_budget_cap = 0
    for prop in proposals:
        if remaining <= 0:
            skipped_budget_cap += 1
            continue
        candidate_row = journal.one(
            "SELECT * FROM candidates WHERE candidate_id = ?", (prop["candidate_id"],),
        )
        if not candidate_row:
            skipped += 1
            continue
        vote_id = vote_on_proposal(journal, settings, debater, candidate_row, prop, scan_batch_id)
        if vote_id:
            voted += 1
            remaining -= 1
        else:
            skipped += 1

    if skipped_budget_cap:
        journal.log_system_event(
            Severity.INFO, "debate",
            f"Bear debate daily cap exhausted mid-batch for scan_batch_id={scan_batch_id}; "
            f"{skipped_budget_cap} proposal(s) not debated.",
        )

    return {
        "voted": voted,
        "skipped": skipped + skipped_budget_cap,
        "budget_exhausted": skipped_budget_cap > 0,
    }
