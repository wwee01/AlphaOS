"""Earnings-proximity provider abstraction (PR5, live provider added EARN-1).

A small, swappable interface so AlphaOS can know whether a symbol has an
upcoming earnings event.

* ``MockEarningsProximityProvider`` -- deterministic, offline, hermetic (the
  TEST default, and the default whenever no live provider is configured).
  Mirrors alphaos/research/last30days_provider.py's mock-seeding convention
  exactly (seeded on symbol + market_date, re-rolls daily). mock != real:
  no earnings-conditioned card or hypothesis may go live on this provider.

* ``AlphaVantageEarningsProvider`` (EARN-1) -- the live vendor (cost floor;
  see alphaos-pr-implementation-specs.md's EARN-1 section for the vendor
  choice). Reads ONLY from ``earnings_calendar_cache``, populated by the
  once-daily ``alphaos.reports.earnings_calendar_service`` job -- this
  class never makes a live HTTP call itself (mirrors INSTR-1's ATR
  live-path/capture-job split: the capture job is the write side, this is
  the read side).

Both live behind the same ``make_earnings_provider`` factory +
``EarningsProximityProvider`` interface -- zero call-site changes anywhere
that consumes earnings data (the PR5 design goal this PR fulfills).
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


class AlphaVantageEarningsProvider(EarningsProximityProvider):
    """EARN-1: the live earnings-calendar provider. Reads ONLY from
    ``earnings_calendar_cache`` (populated by the once-daily
    ``alphaos.reports.earnings_calendar_service.update_earnings_calendar``
    job) -- never a live HTTP call from this class itself.

    A flat ``_CONFIDENCE_OK`` is used for every OK-status row: the vendor
    exposes no per-row reliability signal, so a more granular number would
    fabricate precision the data doesn't actually support (this codebase's
    own "unknown != safe" law applied to false precision, not just missing
    data).
    """

    name = "alpha_vantage"
    _CONFIDENCE_OK = 0.75

    # Alpha Vantage's own literal timeOfTheDay values, mapped onto this
    # codebase's EarningsTiming vocabulary -- anything else (blank/unknown
    # vendor value) stays UNKNOWN, never guessed.
    _TIMING_MAP = {
        "pre-market": EarningsTiming.BEFORE_OPEN.value,
        "post-market": EarningsTiming.AFTER_CLOSE.value,
    }

    def __init__(self, journal, staleness_days: int = 3):
        self.journal = journal
        self.staleness_days = staleness_days

    def _cache_is_stale(self) -> bool:
        """True if the capture job has never run, or its most recent
        capture is older than ``staleness_days`` -- fails toward STALE
        (never silently trusts an arbitrarily old cache)."""
        latest = self.journal.one("SELECT MAX(created_at_utc) AS d FROM earnings_calendar_cache")
        last_captured = (latest or {}).get("d")
        if not last_captured:
            return True  # never captured -- treated exactly like a stale cache
        age = timeutils.age_seconds(last_captured)
        if age is None:
            return True  # unparseable timestamp -- fail toward stale, never toward trusted
        return (age / 86400.0) > self.staleness_days

    def get_earnings_for_symbol(self, symbol: str) -> EarningsProximityResult:
        fetched_at = timeutils.to_iso(timeutils.now_utc())
        if self._cache_is_stale():
            return EarningsProximityResult(
                symbol=symbol, earnings_date=None, earnings_timing=EarningsTiming.UNKNOWN.value,
                confidence=0.0, source=self.name, status=EarningsDataStatus.STALE.value,
                fetched_at_utc=fetched_at,
            )

        today = timeutils.market_date().isoformat()
        row = self.journal.one(
            "SELECT report_date, timing FROM earnings_calendar_cache "
            "WHERE symbol = ? AND report_date >= ? ORDER BY report_date ASC, id DESC LIMIT 1",
            (symbol, today),
        )
        if not row:
            # No upcoming report_date for this symbol in the cache -- could
            # be a genuinely quiet company or a vendor coverage gap; can't
            # distinguish, so UNAVAILABLE is the honest label (matches the
            # mock provider's own "no data found" semantics).
            return EarningsProximityResult(
                symbol=symbol, earnings_date=None, earnings_timing=EarningsTiming.UNKNOWN.value,
                confidence=0.0, source=self.name, status=EarningsDataStatus.UNAVAILABLE.value,
                fetched_at_utc=fetched_at,
            )

        timing = self._TIMING_MAP.get((row.get("timing") or "").strip().lower(), EarningsTiming.UNKNOWN.value)
        return EarningsProximityResult(
            symbol=symbol, earnings_date=row["report_date"], earnings_timing=timing,
            confidence=self._CONFIDENCE_OK, source=self.name, status=EarningsDataStatus.OK.value,
            fetched_at_utc=fetched_at,
        )


def make_earnings_provider(
    settings, journal=None, force: bool = False,
) -> Optional[EarningsProximityProvider]:
    """Build the configured provider, or None if disabled. Never raises.

    ``force=True`` ignores the ``earnings_proximity_enabled`` master switch
    (for a future manual probe CLI, mirroring last30days_probe's semantics).
    ``journal`` is required for the live (``alpha_vantage``) provider, which
    reads ``earnings_calendar_cache`` -- the mock provider ignores it.
    """
    if not force and not settings.earnings_proximity_enabled:
        return None
    provider = (settings.earnings_proximity_provider or "mock").lower()
    if provider in ("disabled", "none", ""):
        return None
    if provider == "alpha_vantage":
        if journal is None:
            # Can't read earnings_calendar_cache without a journal. Fail
            # safe to disabled (None -> the caller's own PROVIDER_DISABLED
            # status), NEVER silently substitute the mock provider here --
            # mock != real (module docstring), so a caller that asked for
            # the live provider must get an honest "unavailable," not
            # fabricated random dates masquerading as real data.
            return None
        return AlphaVantageEarningsProvider(journal, staleness_days=settings.earnings_calendar_staleness_days)
    # "static" is an accepted alias for "mock" per this PR's spec ("mock/static/
    # local fixture") -- both resolve to the same deterministic implementation.
    return MockEarningsProximityProvider()
