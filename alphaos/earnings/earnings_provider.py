"""Earnings-proximity provider abstraction (PR5).

A small, swappable interface so AlphaOS can know whether a symbol has an
upcoming earnings event, without committing to any particular data vendor
yet. v1 ships only:

* ``MockEarningsProximityProvider`` -- deterministic, offline, hermetic (the
  TEST default and the safe default when enabled without a live provider
  configured). Mirrors alphaos/research/last30days_provider.py's mock-seeding
  convention exactly (seeded on symbol + market_date, re-rolls daily).

A live provider (real earnings-calendar API/vendor) can be added later behind
the same ``make_earnings_provider`` factory + ``EarningsProximityProvider``
interface without any caller needing to change -- this is a design goal of
this PR, not a promise this PR implements a live provider.
"""

from __future__ import annotations

import abc
import random
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from alphaos.constants import EarningsDataStatus, EarningsTiming
from alphaos.util import timeutils


@dataclass
class EarningsProximityResult:
    """Raw provider output for one symbol. ``status`` never defaults to OK --
    a provider that doesn't know must say so explicitly (UNAVAILABLE/UNKNOWN),
    never leave the caller to assume "no earnings nearby"."""

    symbol: str
    earnings_date: Optional[str]   # ISO calendar date (YYYY-MM-DD), or None
    earnings_timing: str = EarningsTiming.UNKNOWN.value
    confidence: float = 0.0
    source: str = "unknown"
    status: str = EarningsDataStatus.UNKNOWN.value
    fetched_at_utc: str = ""


class EarningsProximityProvider(abc.ABC):
    name = "base"

    @abc.abstractmethod
    def get_earnings_for_symbol(self, symbol: str) -> EarningsProximityResult:
        ...


class MockEarningsProximityProvider(EarningsProximityProvider):
    """Deterministic, offline, clearly-mock earnings calendar.

    Per symbol+market-day it returns a reproducible scenario: a known earnings
    date spread across near-term/in-window/far-out (so tests and dry-runs see
    a realistic mix), or -- for a minority of symbols -- no data at all
    (``unavailable``), so "missing data" paths are exercised by construction,
    not left untested.
    """

    name = "mock"

    def get_earnings_for_symbol(self, symbol: str) -> EarningsProximityResult:
        rng = random.Random(f"earn:{symbol}:{timeutils.market_date()}")
        fetched_at = timeutils.to_iso(timeutils.now_utc())
        roll = rng.random()

        if roll < 0.15:  # no data found for this symbol -- explicit UNAVAILABLE, not "safe"
            return EarningsProximityResult(
                symbol=symbol, earnings_date=None, earnings_timing=EarningsTiming.UNKNOWN.value,
                confidence=0.0, source=self.name, status=EarningsDataStatus.UNAVAILABLE.value,
                fetched_at_utc=fetched_at,
            )

        # Spread across [-10, +45] calendar days so a scan naturally sees a mix
        # of "just reported", "inside a 1-5 day hold window", "inside the 7-day
        # warning window", and "safely far out" -- deterministic per symbol/day.
        days_out = rng.randint(-10, 45)
        earnings_date = (timeutils.market_date() + timedelta(days=days_out)).isoformat()
        timing = rng.choice([
            EarningsTiming.BEFORE_OPEN.value, EarningsTiming.AFTER_CLOSE.value, EarningsTiming.UNKNOWN.value,
        ])
        confidence = round(rng.uniform(0.6, 0.95), 2)
        return EarningsProximityResult(
            symbol=symbol, earnings_date=earnings_date, earnings_timing=timing,
            confidence=confidence, source=self.name, status=EarningsDataStatus.OK.value,
            fetched_at_utc=fetched_at,
        )


def make_earnings_provider(settings, force: bool = False) -> Optional[EarningsProximityProvider]:
    """Build the configured provider, or None if disabled. Never raises.

    ``force=True`` ignores the ``earnings_proximity_enabled`` master switch
    (for a future manual probe CLI, mirroring last30days_probe's semantics).
    """
    if not force and not settings.earnings_proximity_enabled:
        return None
    provider = (settings.earnings_proximity_provider or "mock").lower()
    if provider in ("disabled", "none", ""):
        return None
    # "static" is an accepted alias for "mock" per this PR's spec ("mock/static/
    # local fixture") -- both resolve to the same deterministic implementation
    # until a live provider exists.
    return MockEarningsProximityProvider()
