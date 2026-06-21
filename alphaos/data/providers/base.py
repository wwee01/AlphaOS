"""Abstract market-data provider + the canonical snapshot schema.

Every provider returns a dict with these keys so mock and live share one schema
(the freshness guard, scanner, risk engine, and execution all rely on it).
"""

from __future__ import annotations

import abc

# Canonical Alpaca-shaped snapshot fields (used by tests to assert parity).
SNAPSHOT_FIELDS = [
    "symbol",
    "provider",          # alpaca | alpaca_mock
    "feed",              # iex
    "is_mock",           # bool — never let mock masquerade as live
    "last_price",
    "prev_close",
    "bid",
    "ask",
    "spread",
    "spread_pct",
    "volume",
    "avg_volume",
    "rel_volume",
    "dollar_volume",
    "change_pct",
    "bar_open",
    "bar_high",
    "bar_low",
    "bar_close",
    "quote_timestamp",   # Alpaca latestQuote timestamp
    "bar_timestamp",     # Alpaca minuteBar/dailyBar timestamp
    "source_timestamp",  # == quote_timestamp (back-compat for the freshness guard)
    "received_at",
    "market_session",
]


class MarketDataProvider(abc.ABC):
    """Provider interface. Implementations MUST NOT call execution code."""

    #: stable provider label written into the journal
    name: str = "abstract"
    #: True for mock providers, so callers can label data honestly
    is_mock: bool = False

    @abc.abstractmethod
    def get_snapshot(self, symbol: str) -> dict:
        """Return one Alpaca-shaped snapshot for ``symbol`` (see SNAPSHOT_FIELDS)."""

    def get_snapshots(self, symbols: list[str]) -> list[dict]:
        return [self.get_snapshot(s) for s in symbols]
