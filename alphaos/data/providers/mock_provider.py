"""Offline mock data provider — Alpaca-shaped, clearly labelled as mock.

Mock snapshots:
* use ``provider="alpaca_mock"`` and ``is_mock=True`` so nothing confuses them
  with live data (System Health/logs/reports surface mock mode),
* share the exact schema of the live Alpaca provider,
* carry fresh quote/bar timestamps and simulate a regular session so the
  pipeline can be exercised deterministically offline.

This is the only place v1 fabricates *market* data, and it is explicitly mock.
"""

from __future__ import annotations

import random

from alphaos.constants import MarketDataFeed, MarketSession
from alphaos.data.providers.base import MarketDataProvider
from alphaos.util import timeutils

PROVIDER = "alpaca_mock"


class MockDataProvider(MarketDataProvider):
    name = PROVIDER
    is_mock = True

    def __init__(self, feed: str = MarketDataFeed.IEX.value):
        self.feed = feed

    def get_snapshot(self, symbol: str) -> dict:
        # Deterministic per symbol+trading-day so a scan is reproducible.
        seed = f"{symbol}:{timeutils.market_date()}"
        rng = random.Random(seed)
        last = round(rng.uniform(15.0, 450.0), 2)
        change_pct = rng.uniform(-0.05, 0.09)
        prev_close = round(last / (1 + change_pct), 2)
        volume = rng.randint(400_000, 30_000_000)
        avg_volume = max(1.0, volume / rng.uniform(0.5, 3.0))
        rel_volume = volume / avg_volume
        dollar_volume = round(last * volume, 2)
        spread = round(last * rng.uniform(0.0002, 0.004), 4)
        bid = round(last - spread / 2, 4)
        ask = round(last + spread / 2, 4)
        spread_pct = round(spread / last, 6) if last else None
        high = round(last * (1 + abs(rng.uniform(0, 0.03))), 2)
        low = round(prev_close * (1 - abs(rng.uniform(0, 0.03))), 2)

        ts = timeutils.stamp().utc  # fresh — mock data is "now"
        return {
            "symbol": symbol,
            "provider": PROVIDER,
            "feed": self.feed,
            "is_mock": True,
            "last_price": last,
            "prev_close": prev_close,
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "spread_pct": spread_pct,
            "volume": volume,
            "avg_volume": round(avg_volume, 0),
            "rel_volume": round(rel_volume, 3),
            "dollar_volume": dollar_volume,
            "change_pct": round(change_pct, 4),
            "bar_open": prev_close,
            "bar_high": high,
            "bar_low": low,
            "bar_close": last,
            "quote_timestamp": ts,
            "bar_timestamp": ts,
            "source_timestamp": ts,
            "received_at": ts,
            # Simulate a regular session offline so the loop runs; clearly mock.
            "market_session": MarketSession.REGULAR.value,
        }
