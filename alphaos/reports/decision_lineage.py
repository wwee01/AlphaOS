"""Decision lineage report (PR4). Answers "which code/config/model/prompt/
data/scheduler context produced this decision?" for any one decision, by
resolving whatever ID the caller passes to the underlying ``candidate_id``
and then gathering every row across the pipeline that traces back to it.

PURE READ. Never writes anything, never touches a gate/eval/labeller/risk/
execution/approval path -- this is a reporting/inspection tool over the
lineage stamps PR4 adds elsewhere, not a decision input.
"""

from __future__ import annotations

from typing import Optional

# (id_column, table) pairs tried in order to resolve an arbitrary decision_id
# down to the candidate_id every other table hangs off of.
_ID_LOOKUPS = (
    ("candidate_id", "candidates"),
    ("proposal_id", "trade_proposals"),
    ("rejection_id", "rejected_candidates"),
    ("adjustment_id", "decision_adjustments"),
    ("override_id", "user_decision_overrides"),
    ("outcome_id", "candidate_outcomes"),
    ("outcome_id", "trade_outcomes"),
    ("eval_id", "openai_evaluations"),
    ("review_id", "claude_reviews"),
    ("polarity_id", "last30days_polarity"),
)


def _resolve_candidate_id(journal, decision_id: str) -> Optional[str]:
    """Try decision_id against every known ID column until one resolves to a
    candidate_id (or decision_id itself, if it's already a candidate_id)."""
    for id_col, table in _ID_LOOKUPS:
        row = journal.one(f"SELECT * FROM {table} WHERE {id_col} = ?", (decision_id,))
        if row:
            return row.get("candidate_id")
    return None


def _rows(journal, table: str, candidate_id: str, id_col: str = "candidate_id") -> list:
    return journal.query(
        f"SELECT * FROM {table} WHERE {id_col} = ? ORDER BY id", (candidate_id,)
    )


def build_decision_lineage_report(journal, decision_id: str) -> dict:
    """Full lineage reconstruction for `decision_id` (accepts a candidate_id,
    proposal_id, rejection_id, adjustment_id, override_id, outcome_id
    (candidate_outcomes or trade_outcomes), eval_id, review_id, or
    polarity_id). Every key is always present -- empty list/None rather than
    omitted when nothing was found, so a caller can distinguish "not found"
    from "found, but no data in this category"."""
    candidate_id = _resolve_candidate_id(journal, decision_id)
    if candidate_id is None:
        return {"found": False, "queried_id": decision_id}

    candidate = journal.one("SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,))
    evaluations = _rows(journal, "openai_evaluations", candidate_id)
    claude_reviews = _rows(journal, "claude_reviews", candidate_id)
    polarity = _rows(journal, "last30days_polarity", candidate_id)
    # PR5: earnings-proximity has its own lineage_id (unlike candidate_catalysts/
    # candidate_last30days, which carry no lineage_id of their own and are
    # deliberately not included here -- see last30days_polarity for the same
    # own-lineage-id pattern this follows).
    earnings = _rows(journal, "candidate_earnings", candidate_id)
    decision_adjustments = _rows(journal, "decision_adjustments", candidate_id)
    proposals = _rows(journal, "trade_proposals", candidate_id)
    rejects = _rows(journal, "rejected_candidates", candidate_id)
    overrides = _rows(journal, "user_decision_overrides", candidate_id)
    candidate_outcomes = _rows(journal, "candidate_outcomes", candidate_id)
    trade_outcomes = _rows(journal, "trade_outcomes", candidate_id)

    # Every lineage_id referenced anywhere in this decision's history -- almost
    # always one (the candidate/proposal/reject/etc share the same scan-time
    # snapshot), but a decision's journey can span multiple lineage snapshots
    # (e.g. a user override made under a later code/config state).
    all_rows = (
        ([candidate] if candidate else []) + evaluations + claude_reviews + polarity
        + earnings + decision_adjustments + proposals + rejects + overrides
        + candidate_outcomes + trade_outcomes
    )
    lineage_ids = sorted({r.get("lineage_id") for r in all_rows if r.get("lineage_id")})
    lineage_snapshots = [
        journal.one("SELECT * FROM lineage_snapshots WHERE lineage_id = ?", (lid,))
        for lid in lineage_ids
    ]
    lineage_snapshots = [s for s in lineage_snapshots if s]

    # Scheduler lineage: transitive via scan_batch_id -> scheduler_runs (PR2.5)
    # / job_runs (PR3) -- no per-decision scheduler columns were added (PR4
    # deliberately reuses this existing chain rather than duplicating it).
    scan_batch_ids = sorted({
        r.get("scan_batch_id") for r in
        ([candidate] if candidate else []) + decision_adjustments + proposals + rejects
        if r.get("scan_batch_id")
    })
    scheduler_runs = []
    job_runs = []
    for sbid in scan_batch_ids:
        scheduler_runs.extend(journal.query(
            "SELECT * FROM scheduler_runs WHERE scan_batch_id = ? ORDER BY id", (sbid,)
        ))
    if scheduler_runs:
        scheduler_run_ids = sorted({r.get("scheduler_run_id") for r in scheduler_runs if r.get("scheduler_run_id")})
        for run_id in scheduler_run_ids:
            job_runs.extend(journal.query(
                "SELECT * FROM job_runs WHERE scheduler_run_id = ? ORDER BY id", (run_id,)
            ))

    return {
        "found": True,
        "queried_id": decision_id,
        "candidate_id": candidate_id,
        "candidate": candidate,
        "openai_evaluations": evaluations,
        "claude_reviews": claude_reviews,
        "last30days_polarity": polarity,
        "candidate_earnings": earnings,
        "decision_adjustments": decision_adjustments,
        "trade_proposals": proposals,
        "rejected_candidates": rejects,
        "user_decision_overrides": overrides,
        "candidate_outcomes": candidate_outcomes,
        "trade_outcomes": trade_outcomes,
        "lineage_snapshots": lineage_snapshots,
        "scan_batch_ids": scan_batch_ids,
        "scheduler_runs": scheduler_runs,
        "job_runs": job_runs,
    }
