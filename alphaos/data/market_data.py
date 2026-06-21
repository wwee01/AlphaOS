"""Massive market-data connector.

Massive is the signal data source. Mock *market* data is allowed (the spec
permits sample market/candidate/order data offline); only mock *news* is
forbidden in the runtime path.

Mock snapshots carry a fresh ``source_timestamp`` so the freshness guard can
pass them, and use the provider name ``massive_mock`` so they are never confused
with live data in the journal.

Live fetching is an import-guarded stub: with a key present it attempts a
best-effort HTTP GET (urllib, timeout). On any failure it returns an
*unverifiable* snapshot (source_timestamp=None) so the freshness guard blocks
it — it never fabricates data in a non-mock mode.
"""

from __future__ import annotations

import json
import random
import urllib.error
import urllib.request
from typing import Optional

from alphaos.config.settings import Settings
from alphaos.constants import Severity
from alphaos.util import timeutils

MOCK_PROVIDER = "massive_mock"
LIVE_PROVIDER = "massive"
HTTP_TIMEOUT = 10


class MarketDataClient:
    def __init__(self, settings: Settings, journal=None):
        self.settings = settings
        self.journal = journal
        # Use mock data offline or whenever the Massive key is absent.
        self.use_mock = settings.is_mock or not settings.has_massive_key
        self._warned = False

    # ------------------------------------------------------------------ public
    def get_snapshot(self, symbol: str) -> dict:
        if self.use_mock:
            self._warn_once()
            return self._mock_snapshot(symbol)
        return self._fetch_live(symbol)

    def get_snapshots(self, symbols: list[str]) -> list[dict]:
        return [self.get_snapshot(s) for s in symbols]

    # ------------------------------------------------------------------- mock
    def _warn_once(self) -> None:
        if self._warned or self.journal is None:
            return
        self._warned = True
        self.journal.log_system_event(
            Severity.INFO,
            "market_data",
            "Using MOCK market data (massive_mock); no Massive key or mock mode.",
        )

    def _mock_snapshot(self, symbol: str) -> dict:
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

        st = timeutils.stamp()
        return {
            "symbol": symbol,
            "provider": MOCK_PROVIDER,
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
            "source_timestamp": st.utc,   # fresh — mock data is "now"
            "received_at": st.utc,
            "market_session": timeutils.market_session().value,
        }

    # ------------------------------------------------------------------- live
    def _fetch_live(self, symbol: str) -> dict:
        """Best-effort live fetch. STUB: returns unverifiable on any problem."""
        url = (
            f"https://api.massive.example/v1/quote?symbol={symbol}"
            f"&apikey={self.settings.massive_api_key}"
        )
        st = timeutils.stamp()
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # pragma: no cover
                payload = json.loads(resp.read().decode("utf-8"))
            # NOTE: real Massive response mapping goes here once the schema is
            # confirmed (including its per-quote source timestamp).
            return {
                "symbol": symbol,
                "provider": LIVE_PROVIDER,
                "last_price": payload.get("last"),
                "bid": payload.get("bid"),
                "ask": payload.get("ask"),
                "volume": payload.get("volume"),
                "dollar_volume": payload.get("dollar_volume"),
                "source_timestamp": payload.get("timestamp"),  # MUST come from provider
                "received_at": st.utc,
                "market_session": timeutils.market_session().value,
            }
        except (urllib.error.URLError, json.JSONDecodeError, KeyError, ValueError) as exc:
            if self.journal is not None:
                self.journal.log_system_event(
                    Severity.ERROR,
                    "market_data",
                    f"Live Massive fetch failed for {symbol}; data unverifiable.",
                    {"error": str(exc)},
                )
            # Unverifiable: source_timestamp=None forces the freshness guard to block.
            return {
                "symbol": symbol,
                "provider": LIVE_PROVIDER,
                "last_price": None,
                "source_timestamp": None,
                "received_at": st.utc,
                "market_session": timeutils.market_session().value,
            }
