"""TQS v0 -- journal-reading input builders (PR7).

This is the ONLY module in alphaos/tqs/ that touches the database. It reads
already-committed rows and assembles a plain ``TqsComponentInputs`` for
``alphaos.tqs.scoring.compute_tqs()`` to consume. Mirrors the established
enricher/pure-compute split elsewhere in this codebase (e.g.
alphaos/earnings/earnings_provider.py doing the I/O, earnings_enricher.py's
compute_proximity_flags() staying pure).

READ-ONLY: every function here is a SELECT. Nothing in this module writes to
the journal, and nothing here is called before the decisions it reads have
already been committed (see alphaos/tqs/batch.py's call site).
"""

from __future__ import annotations

from typing import Optional

from alphaos.tqs.scoring import TqsComponentInputs


def _latest(journal, sql: str, candidate_id: str) -> Optional[dict]:
    return journal.one(sql + " ORDER BY id DESC LIMIT 1", (candidate_id,))


def build_candidate_inputs(journal, settings, candidate_row: dict) -> TqsComponentInputs:
    """Assemble TQS inputs for ONE candidate row (already fetched by the
    caller). Every join is a best-effort SELECT -- a missing related row
    (last30days_polarity/candidate_catalysts never ran, no eval somehow)
    yields None fields, which compute_tqs() degrades to 'missing' per
    component; this function never raises and never fabricates a value."""
    candidate_id = candidate_row["candidate_id"]

    eval_row = _latest(
        journal,
        "SELECT expected_r, confidence, is_mock, validation_status FROM openai_evaluations "
        "WHERE candidate_id = ?",
        candidate_id,
    ) or {}

    spread_pct = None
    snapshot_id = candidate_row.get("price_snapshot_id")
    if snapshot_id:
        snap = journal.one(
            "SELECT spread_pct FROM price_snapshots WHERE snapshot_id = ?", (snapshot_id,)
        )
        if snap:
            spread_pct = snap.get("spread_pct")

    polarity_row = _latest(
        journal,
        "SELECT confidence, direction_alignment, model_provider, parse_status "
        "FROM last30days_polarity WHERE candidate_id = ?",
        candidate_id,
    ) or {}

    catalyst_row = _latest(
        journal,
        "SELECT catalyst_type, catalyst_status, enrichment_source FROM candidate_catalysts "
        "WHERE candidate_id = ?",
        candidate_id,
    ) or {}

    return TqsComponentInputs(
        symbol=candidate_row["symbol"],
        direction=candidate_row.get("direction"),
        max_spread_pct=settings.max_spread_pct,
        is_mock=bool(eval_row.get("is_mock")),
        expected_r=eval_row.get("expected_r"),
        interest_score=candidate_row.get("interest_score"),
        spread_pct=spread_pct,
        ai_available=bool(eval_row),
        ai_confidence=eval_row.get("confidence"),
        ai_is_mock=bool(eval_row.get("is_mock")),
        ai_validation_status=eval_row.get("validation_status"),
        label_confidence=candidate_row.get("label_confidence"),
        label_source=candidate_row.get("label_source"),
        polarity_confidence=polarity_row.get("confidence"),
        polarity_alignment=polarity_row.get("direction_alignment"),
        polarity_model_provider=polarity_row.get("model_provider"),
        polarity_parse_status=polarity_row.get("parse_status"),
        catalyst_type=catalyst_row.get("catalyst_type"),
        catalyst_status=catalyst_row.get("catalyst_status"),
        catalyst_enrichment_source=catalyst_row.get("enrichment_source"),
        arming_classification=candidate_row.get("arming_classification"),
        earnings_data_status=candidate_row.get("earnings_data_status"),
        earnings_within_hold_window=candidate_row.get("earnings_within_hold_window"),
        earnings_within_warning_window=candidate_row.get("earnings_within_warning_window"),
    )


def build_proposal_inputs(journal, settings, candidate_row: dict, proposal_row: dict) -> TqsComponentInputs:
    """Same underlying candidate evidence, but reward:risk geometry reflects
    the PROPOSAL's own expected_r/direction -- the actually-built trade,
    which can differ slightly from the raw evaluation (sizing/rounding)."""
    inputs = build_candidate_inputs(journal, settings, candidate_row)
    inputs.direction = proposal_row.get("direction") or inputs.direction
    inputs.expected_r = proposal_row.get("expected_r")
    return inputs
