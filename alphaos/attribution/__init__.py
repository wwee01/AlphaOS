"""Attribution v2 -- counterfactual ΔR ledger (PR8).

Measurement-only: pairs a decision-DIVERGENCE event (a user override, a hard
gate block, a TTL expiry, or execution vs the frozen AlphaOS plan) with the R
already resolved by the outcome ledger (candidate_outcomes/trade_outcomes),
and journals it as a SEPARATE attribution_records row. NOT a probability, NOT
expected return, NOT a sizing/approval/gating signal, NOT a per-event moral
verdict -- an aggregate, falsifiable measurement of whether a deviation from
AlphaOS's own path added or cost value, to be read only in aggregate and only
above the sample floors in alphaos/reports/attribution.py.

NO DECISION PATH MAY IMPORT OR READ FROM THIS PACKAGE. Discovery and
resolution run strictly inside Orchestrator.outcomes_update() (see
alphaos/attribution/batch.py's call site), itself pure measurement already --
attribution never runs earlier and never influences a scan/approval/execution
decision by construction, not merely by discipline. If you are adding a call
to anything in ``alphaos.attribution`` from ``alphaos/risk/``,
``alphaos/approval.py``, or any orchestrator decide/approve/execute method,
stop: that is out of scope for this package's entire reason for existing.

This package also never imports or reads alphaos.tqs / tqs_scores -- TQS is,
at most, a report-time slice dimension joined in the reporting layer, never
an attribution input. No second replay engine either: every ΔR here is
computed from candidate_outcomes.replay_r / trade_outcomes.realized_r exactly
as alphaos/learning/outcomes_engine.py already resolved them.
"""

from __future__ import annotations

from alphaos.attribution.batch import discover_events, resolve_pending
from alphaos.attribution.resolve import ATTRIBUTION_VERSION, compute_data_quality

__all__ = [
    "ATTRIBUTION_VERSION",
    "compute_data_quality",
    "discover_events",
    "resolve_pending",
]
