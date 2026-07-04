"""TQS v0 -- shadow-only Trade Quality Score (PR7).

TQS is a deterministic, explainable, SHADOW-ONLY composite of setup quality
and evidence coverage. It is an attention-worthiness RANKING signal, NOT a
probability, NOT expected return, NOT a sizing signal, and NOT an
approval/gating signal. Its only claim is a falsifiable hypothesis: "higher
scores should rank-order toward better forward outcomes" -- to be tested
later against candidate_outcomes/trade_outcomes, never assumed true here.

NO DECISION PATH MAY IMPORT OR READ FROM THIS PACKAGE. Scoring runs strictly
AFTER a scan batch's decisions are already committed to the journal (see
``score_scan_batch``'s call site at the very end of
``Orchestrator.run_scan_once``), so it cannot influence what it measures by
construction -- not merely by discipline. If you are adding a call to
anything in ``alphaos.tqs`` from ``alphaos/risk/``, ``alphaos/approval.py``,
or any orchestrator decide/approve/execute method, stop: that is out of
scope for this package's entire reason for existing.
"""

from __future__ import annotations

from alphaos.tqs.batch import score_candidate, score_proposal, score_scan_batch
from alphaos.tqs.scoring import (
    TQS_VERSION,
    WEIGHTS,
    TqsComponentInputs,
    TqsResult,
    compute_tqs,
)

__all__ = [
    "TQS_VERSION",
    "WEIGHTS",
    "TqsComponentInputs",
    "TqsResult",
    "compute_tqs",
    "score_candidate",
    "score_proposal",
    "score_scan_batch",
]
