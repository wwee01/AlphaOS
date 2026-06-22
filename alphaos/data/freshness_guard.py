"""Data-freshness guard (Alpaca / IEX aware).

On the free IEX tier quotes can be sparse, so the guard matters MORE, not less.
It gates on:
* quote age and bar age, with thresholds that differ by market session,
* missing quote/bar (never treated as "fresh enough"),
* closed session (no live-entry proposals),
* material price drift between proposal generation and approval/execution.

Decisions are based on the provider's own quote/bar timestamps; ``received_at``
is recorded only to estimate API/cache/network delay. The cross-provider /
source-mismatch check is reserved for multi-provider mode (inert in v1, since
there is exactly one active data source).
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

from alphaos.constants import FreshnessStatus, MarketSession, ReasonCode
from alphaos.util import timeutils


def quote_crossed_or_invalid(snapshot: dict) -> bool:
    """True if a present quote is crossed or non-positive (bad IEX data).

    A crossed/zero quote (ask <= 0, bid <= 0, or ask < bid) yields a negative
    spread that would otherwise slip a ``spread_pct > max`` gate. Missing
    quotes (None) are handled by the freshness guard, not here.
    """
    bid = snapshot.get("bid")
    ask = snapshot.get("ask")
    if ask is not None and ask <= 0:
        return True
    if bid is not None and bid <= 0:
        return True
    if bid is not None and ask is not None and ask < bid:
        return True
    return False


@dataclass(frozen=True)
class FreshnessReport:
    provider: Optional[str]
    feed: Optional[str]
    quote_timestamp: Optional[str]
    bar_timestamp: Optional[str]
    quote_age_seconds: Optional[float]
    bar_age_seconds: Optional[float]
    data_delay_seconds: Optional[float]   # received_at - quote_timestamp
    received_at: Optional[str]
    market_session: str
    is_usable: bool
    freshness_status: str
    block_reason: Optional[str]
    # kept for back-compat with earlier callers
    source_timestamp: Optional[str] = None
    age_seconds: Optional[float] = None

    def as_dict(self) -> dict:
        return asdict(self)


class FreshnessGuard:
    def __init__(
        self,
        max_quote_age_rth: float = 60.0,
        max_bar_age_rth: float = 180.0,
        max_quote_age_premarket: float = 300.0,
        max_bar_age_premarket: float = 600.0,
        max_price_drift_bps: float = 50.0,
    ):
        self.max_quote_age_rth = float(max_quote_age_rth)
        self.max_bar_age_rth = float(max_bar_age_rth)
        self.max_quote_age_premarket = float(max_quote_age_premarket)
        self.max_bar_age_premarket = float(max_bar_age_premarket)
        self.max_price_drift_bps = float(max_price_drift_bps)

    @classmethod
    def from_settings(cls, settings) -> "FreshnessGuard":
        return cls(
            max_quote_age_rth=settings.max_quote_age_seconds_rth,
            max_bar_age_rth=settings.max_bar_age_seconds_rth,
            max_quote_age_premarket=settings.max_quote_age_seconds_premarket,
            max_bar_age_premarket=settings.max_bar_age_seconds_premarket,
            max_price_drift_bps=settings.max_price_drift_bps_since_proposal,
        )

    # ------------------------------------------------------------- thresholds
    def _thresholds(self, session: str) -> tuple[float, float]:
        if session == MarketSession.REGULAR.value:
            return self.max_quote_age_rth, self.max_bar_age_rth
        # premarket / afterhours use the more lenient pre/post thresholds
        return self.max_quote_age_premarket, self.max_bar_age_premarket

    # --------------------------------------------------------------- assess
    def assess(self, snapshot: dict, now=None) -> FreshnessReport:
        provider = snapshot.get("provider")
        feed = snapshot.get("feed")
        session = snapshot.get("market_session") or timeutils.market_session(now).value
        quote_ts = snapshot.get("quote_timestamp") or snapshot.get("source_timestamp")
        bar_ts = snapshot.get("bar_timestamp")
        received_at = snapshot.get("received_at")

        data_delay = None
        if quote_ts and received_at:
            data_delay = timeutils.age_seconds(quote_ts, timeutils.parse_iso(received_at))

        def report(is_usable, status, reason, q_age=None, b_age=None):
            return FreshnessReport(
                provider=provider, feed=feed, quote_timestamp=quote_ts, bar_timestamp=bar_ts,
                quote_age_seconds=q_age, bar_age_seconds=b_age, data_delay_seconds=data_delay,
                received_at=received_at, market_session=session, is_usable=is_usable,
                freshness_status=status, block_reason=reason,
                source_timestamp=quote_ts, age_seconds=q_age,
            )

        # Closed session: no live-entry proposals.
        if session == MarketSession.CLOSED.value:
            return report(False, FreshnessStatus.CLOSED_SESSION.value, ReasonCode.CLOSED_SESSION.value)

        max_quote_age, max_bar_age = self._thresholds(session)

        # --- Quote checks ---
        if not quote_ts:
            return report(False, FreshnessStatus.MISSING.value, ReasonCode.MISSING_QUOTE.value)
        quote_age = timeutils.age_seconds(quote_ts, now)
        if quote_age is None:
            return report(False, FreshnessStatus.MISSING.value, ReasonCode.MISSING_QUOTE.value)
        if quote_age < -5:
            return report(False, FreshnessStatus.UNVERIFIABLE.value, ReasonCode.UNVERIFIABLE_DATA.value, quote_age)
        if quote_age > max_quote_age:
            return report(False, FreshnessStatus.STALE.value, ReasonCode.STALE_QUOTE.value, quote_age)

        # --- Bar checks ---
        if not bar_ts:
            return report(False, FreshnessStatus.MISSING.value, ReasonCode.MISSING_BAR.value, quote_age)
        bar_age = timeutils.age_seconds(bar_ts, now)
        if bar_age is None:
            return report(False, FreshnessStatus.MISSING.value, ReasonCode.MISSING_BAR.value, quote_age)
        if bar_age < -5:
            return report(False, FreshnessStatus.UNVERIFIABLE.value, ReasonCode.UNVERIFIABLE_DATA.value, quote_age, bar_age)
        if bar_age > max_bar_age:
            return report(False, FreshnessStatus.STALE.value, ReasonCode.STALE_BAR.value, quote_age, bar_age)

        return report(True, FreshnessStatus.USABLE.value, None, quote_age, bar_age)

    # ----------------------------------------------------------- price drift
    def price_drift_bps(self, reference_price: Optional[float], current_price: Optional[float]) -> Optional[float]:
        if not reference_price or current_price is None:
            return None
        return abs(current_price - reference_price) / reference_price * 10_000.0

    def check_price_drift(self, reference_price, current_price) -> tuple[bool, Optional[float]]:
        """Return (ok, drift_bps). Blocks when drift exceeds the configured bps."""
        bps = self.price_drift_bps(reference_price, current_price)
        if bps is None:
            return False, None
        return bps <= self.max_price_drift_bps, round(bps, 2)

    # --------------------------------------------- reserved for multi-provider
    def cross_provider_consistent(self, *snapshots) -> bool:  # pragma: no cover
        """Reserved for multi-provider mode. Inert in v1 (single data source)."""
        return True
