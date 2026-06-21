"""Data-freshness guard (resolved decision #7).

No proposal, order, modification, or exit may rely on data that is stale or
unverifiable. The guard decides usability primarily from ``source_timestamp``
(the provider's own stamp); ``received_at`` is used only to estimate API/cache/
network delay.

If a provider does not expose a usable ``source_timestamp``, the data is treated
as UNVERIFIABLE and blocked — never as fresh. That condition is meant to be
surfaced as a blocking system event by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

from alphaos.constants import FreshnessStatus, MarketSession, ReasonCode
from alphaos.util import timeutils


@dataclass(frozen=True)
class FreshnessReport:
    provider: str
    source_timestamp: Optional[str]
    received_at: Optional[str]
    data_delay_seconds: Optional[float]   # received_at - source_timestamp (network/cache)
    age_seconds: Optional[float]          # now - source_timestamp (the primary gate)
    market_session: str
    is_usable: bool
    freshness_status: str
    block_reason: Optional[str]

    def as_dict(self) -> dict:
        return asdict(self)


class FreshnessGuard:
    def __init__(self, max_data_age_seconds: float = 120.0):
        self.max_age = float(max_data_age_seconds)

    def assess(self, snapshot: dict, now=None) -> FreshnessReport:
        """Assess one market-data snapshot.

        ``snapshot`` is expected to carry: provider, source_timestamp,
        received_at (optional), and optionally a pre-computed market_session.
        """
        provider = snapshot.get("provider", "unknown")
        source_ts = snapshot.get("source_timestamp")
        received_at = snapshot.get("received_at")
        session = snapshot.get("market_session") or timeutils.market_session(now).value

        # Network/cache delay is diagnostic only.
        data_delay: Optional[float] = None
        if source_ts and received_at:
            recv_dt = timeutils.parse_iso(received_at)
            data_delay = timeutils.age_seconds(source_ts, recv_dt)

        # No parseable source timestamp => unverifiable => blocked.
        age = timeutils.age_seconds(source_ts, now) if source_ts else None
        if source_ts is None or age is None:
            return FreshnessReport(
                provider=provider,
                source_timestamp=source_ts,
                received_at=received_at,
                data_delay_seconds=data_delay,
                age_seconds=None,
                market_session=session,
                is_usable=False,
                freshness_status=FreshnessStatus.UNVERIFIABLE.value,
                block_reason=ReasonCode.UNVERIFIABLE_DATA.value,
            )

        # Future-dated timestamps are treated as unverifiable (clock skew/bad feed).
        if age < -5:
            return FreshnessReport(
                provider=provider,
                source_timestamp=source_ts,
                received_at=received_at,
                data_delay_seconds=data_delay,
                age_seconds=age,
                market_session=session,
                is_usable=False,
                freshness_status=FreshnessStatus.UNVERIFIABLE.value,
                block_reason=ReasonCode.UNVERIFIABLE_DATA.value,
            )

        if age > self.max_age:
            return FreshnessReport(
                provider=provider,
                source_timestamp=source_ts,
                received_at=received_at,
                data_delay_seconds=data_delay,
                age_seconds=age,
                market_session=session,
                is_usable=False,
                freshness_status=FreshnessStatus.STALE.value,
                block_reason=ReasonCode.STALE_DATA.value,
            )

        return FreshnessReport(
            provider=provider,
            source_timestamp=source_ts,
            received_at=received_at,
            data_delay_seconds=data_delay,
            age_seconds=age,
            market_session=session,
            is_usable=True,
            freshness_status=FreshnessStatus.USABLE.value,
            block_reason=None,
        )
