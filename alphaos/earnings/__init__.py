"""Earnings-proximity provider + enrichment (PR5).

Event-risk AWARENESS, not execution authority: flags whether a candidate's
intended holding window contains an earnings event. Advisory only -- never
hard-blocks a trade by default, never bypasses a gate or manual approval, and
is never fed into the AI eval/labeller prompt (unlike last30days). Missing/
disabled/stale data is always surfaced as such, never silently treated as
"safe" (no earnings nearby).
"""

from __future__ import annotations

from alphaos.earnings.earnings_enricher import (
    EarningsProximityContext,
    EarningsProximityEnricher,
    compute_proximity_flags,
    recompute_with_hold_days,
)
from alphaos.earnings.earnings_provider import (
    EarningsProximityProvider,
    EarningsProximityResult,
    MockEarningsProximityProvider,
    make_earnings_provider,
)

__all__ = [
    "EarningsProximityContext",
    "EarningsProximityEnricher",
    "EarningsProximityProvider",
    "EarningsProximityResult",
    "MockEarningsProximityProvider",
    "compute_proximity_flags",
    "make_earnings_provider",
    "recompute_with_hold_days",
]
