"""TQS v0 -- scan-batch scoring orchestration (PR7).

``score_scan_batch()`` is called EXACTLY ONCE per scan, at the very end of
``Orchestrator.run_scan_once()``, strictly AFTER every decision for that
batch has already been committed. This ordering is what makes "TQS cannot
influence decisions" true BY CONSTRUCTION rather than by discipline alone:
every row this module reads is already final by the time scoring begins.

Fail-safe: any exception scoring one candidate/proposal is caught, logged as
a system_event, and skipped -- it never fails the scan and never blocks
scoring the rest of the batch. Idempotent: a second call for the same
scan_batch_id inserts zero new rows -- pre-checked here for efficiency, and
enforced at the DB level regardless by tqs_scores' partial unique indexes
(see alphaos/journal/schema.py), the same belt-and-suspenders pattern PR3's
job_runs lock and PR4's lineage_snapshots seeding already use.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from alphaos import lineage
from alphaos.constants import Severity, TqsSourceType
from alphaos.tqs.inputs import build_candidate_inputs, build_proposal_inputs
from alphaos.tqs.scoring import TQS_VERSION, TqsResult, compute_tqs
from alphaos.util import timeutils
from alphaos.util.ids import new_id


def _already_scored(journal, source_type: str, candidate_id: str, proposal_id: Optional[str]) -> bool:
    if source_type == TqsSourceType.PROPOSAL.value:
        return journal.one(
            "SELECT 1 FROM tqs_scores WHERE source_type = ? AND candidate_id = ? "
            "AND proposal_id = ? AND tqs_version = ?",
            (source_type, candidate_id, proposal_id, TQS_VERSION),
        ) is not None
    return journal.one(
        "SELECT 1 FROM tqs_scores WHERE source_type = ? AND candidate_id = ? AND tqs_version = ?",
        (source_type, candidate_id, TQS_VERSION),
    ) is not None


def _log_component_errors(journal, symbol: str, result: TqsResult) -> None:
    errors = {k: v for k, v in result.missing_components.items() if v["reason"].startswith("error:")}
    if errors:
        journal.log_system_event(
            Severity.WARNING, "tqs",
            f"TQS component error(s) for {symbol}; degraded to missing, scoring continued.",
            {"errors": errors},
        )


def _insert_result(journal, settings, *, source_type: str, candidate_row: dict,
                   proposal_row: Optional[dict], result: TqsResult) -> Optional[str]:
    tqs_id = new_id("tqs")
    row = {
        "tqs_id": tqs_id,
        "source_type": source_type,
        "candidate_id": candidate_row["candidate_id"],
        "proposal_id": proposal_row["proposal_id"] if proposal_row else None,
        "scan_batch_id": candidate_row.get("scan_batch_id"),
        "symbol": candidate_row["symbol"],
        "direction": (proposal_row or candidate_row).get("direction"),
        "tqs_version": TQS_VERSION,
        "raw_score": result.raw_score,
        "data_confidence": result.data_confidence,
        "tqs_score": result.tqs_score,
        "tqs_bucket": result.tqs_bucket,
        "components_json": result.components,
        "missing_components_json": result.missing_components,
        "data_quality_status": result.data_quality_status,
        "is_mock": result.is_mock,
        "lineage_id": lineage.get_or_create_lineage_id(journal, settings),
        "computed_at_utc": timeutils.to_iso(timeutils.now_utc()),
    }
    try:
        journal.insert("tqs_scores", row)
    except sqlite3.IntegrityError:
        return None  # another caller already scored this exact row -- idempotent no-op
    return tqs_id


def score_candidate(journal, settings, candidate_row: dict) -> Optional[str]:
    """Score ONE candidate row. Never raises: any failure is logged and
    returns None -- a missing shadow score is a measurement gap, never a
    reason to touch the real pipeline. Skips demo rows and rows already
    scored under the current TQS_VERSION."""
    try:
        if candidate_row.get("status") == "demo":
            return None
        candidate_id = candidate_row["candidate_id"]
        if _already_scored(journal, TqsSourceType.CANDIDATE.value, candidate_id, None):
            return None
        inputs = build_candidate_inputs(journal, settings, candidate_row)
        result = compute_tqs(inputs)
        _log_component_errors(journal, candidate_row.get("symbol", "?"), result)
        return _insert_result(journal, settings, source_type=TqsSourceType.CANDIDATE.value,
                              candidate_row=candidate_row, proposal_row=None, result=result)
    except Exception as exc:  # fail-safe: never let scoring touch the real pipeline
        journal.log_system_event(
            Severity.WARNING, "tqs",
            f"TQS scoring failed for candidate {candidate_row.get('symbol', '?')}; skipped.",
            {"error": str(exc), "candidate_id": candidate_row.get("candidate_id")},
        )
        return None


def score_proposal(journal, settings, candidate_id: str, proposal_id: str) -> Optional[str]:
    """Score ONE proposal row (a candidate that became a proposal) -- a
    SEPARATE, additional row alongside (not a replacement for) the
    candidate-level score, recomputed against the proposal's own
    expected_r/direction. Never raises. Skips demo proposals and rows
    already scored under the current TQS_VERSION."""
    try:
        proposal_row = journal.one("SELECT * FROM trade_proposals WHERE proposal_id = ?", (proposal_id,))
        if not proposal_row or proposal_row.get("is_demo"):
            return None
        candidate_row = journal.one("SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,))
        if not candidate_row:
            return None
        if _already_scored(journal, TqsSourceType.PROPOSAL.value, candidate_id, proposal_id):
            return None
        inputs = build_proposal_inputs(journal, settings, candidate_row, proposal_row)
        result = compute_tqs(inputs)
        _log_component_errors(journal, proposal_row.get("symbol", "?"), result)
        return _insert_result(journal, settings, source_type=TqsSourceType.PROPOSAL.value,
                              candidate_row=candidate_row, proposal_row=proposal_row, result=result)
    except Exception as exc:  # fail-safe: never let scoring touch the real pipeline
        journal.log_system_event(
            Severity.WARNING, "tqs", f"TQS scoring failed for proposal {proposal_id}; skipped.",
            {"error": str(exc), "candidate_id": candidate_id},
        )
        return None


def score_scan_batch(journal, settings, scan_batch_id: Optional[str]) -> dict:
    """Score every eligible candidate in ``scan_batch_id`` (and, for those
    that became a proposal, a separate proposal-level row too). MUST be
    called exactly once, at the very end of run_scan_once(), strictly AFTER
    the batch's decisions are already committed -- see the module docstring
    for why this ordering is the actual safety property, not the fail-safe
    wrapping alone. Never raises. Returns a summary dict for the caller's own
    logging only -- nothing reads this dict to make a decision."""
    if not scan_batch_id:
        return {"scored_candidates": 0, "scored_proposals": 0, "skipped": 0}
    try:
        # EXP-1: shadow-tier candidates share scan_batch_id with the core
        # scan (EXP-0's shadow pass rides the same batch) -- shadow_tier = 0
        # keeps TQS a CORE-book measurement, never silently pooling shadow
        # rows into tqs_scores (which would then leak into H-TQS-1's own
        # core-only hypothesis evidence via alphaos/hypotheses/queries.py).
        candidates = journal.query(
            "SELECT * FROM candidates WHERE scan_batch_id = ? AND shadow_tier = 0", (scan_batch_id,)
        )
    except Exception as exc:
        journal.log_system_event(
            Severity.WARNING, "tqs", "TQS batch scoring failed to read candidates; skipped.",
            {"error": str(exc), "scan_batch_id": scan_batch_id},
        )
        return {"scored_candidates": 0, "scored_proposals": 0, "skipped": 0}

    scored_candidates = 0
    scored_proposals = 0
    skipped = 0
    for cand in candidates:
        if cand.get("status") == "demo":
            skipped += 1
            continue
        if score_candidate(journal, settings, cand):
            scored_candidates += 1
        proposal_row = journal.one(
            "SELECT proposal_id FROM trade_proposals WHERE candidate_id = ? "
            "ORDER BY id DESC LIMIT 1", (cand["candidate_id"],),
        )
        if proposal_row and score_proposal(journal, settings, cand["candidate_id"], proposal_row["proposal_id"]):
            scored_proposals += 1

    return {"scored_candidates": scored_candidates, "scored_proposals": scored_proposals, "skipped": skipped}
