"""Live Alpaca market-data provider (free IEX tier).

Uses Alpaca's market-data REST API snapshot endpoint:
``GET {ALPACA_DATA_BASE_URL}/v2/stocks/{symbol}/snapshot?feed=iex``

IMPORTANT v1 constraints:
* This is MARKET DATA only. It never calls execution code.
* Free/IEX data is limited (sparse quotes for some symbols) — that is why the
  freshness guard gates hard on quote/bar age.
* Missing credentials do NOT silently fall back to mock/Massive/anything; the
  snapshot is returned with null timestamps so the freshness guard blocks it.

Real network calls are exercised only behind ``RUN_LIVE_ALPACA_TESTS=true``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from alphaos.config.settings import ALPACA_DATA_BASE_URL
from alphaos.constants import Severity
from alphaos.data.providers.base import MarketDataProvider
from alphaos.util import timeutils

PROVIDER = "alpaca"
HTTP_TIMEOUT = 10


class AlpacaDataProvider(MarketDataProvider):
    name = PROVIDER
    is_mock = False

    def __init__(self, settings, journal=None):
        self.settings = settings
        self.journal = journal
        self.feed = settings.market_data_feed

    def get_snapshot(self, symbol: str) -> dict:
        received = timeutils.stamp().utc
        if not self.settings.has_alpaca_keys:
            # No silent fallback — return unusable data so the guard blocks it.
            self._log(Severity.ERROR, f"No Alpaca creds; market data unavailable for {symbol}.")
            return self._empty(symbol, received)
        try:  # pragma: no cover - live network path (gated test only)
            url = f"{ALPACA_DATA_BASE_URL}/v2/stocks/{symbol}/snapshot?feed={self.feed}"
            req = urllib.request.Request(
                url,
                headers={
                    "APCA-API-KEY-ID": self.settings.alpaca_api_key,
                    "APCA-API-SECRET-KEY": self.settings.alpaca_secret_key,
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            return self._map(symbol, payload, received)
        except (urllib.error.URLError, json.JSONDecodeError, KeyError, ValueError) as exc:
            self._log(Severity.ERROR, f"Alpaca data fetch failed for {symbol}: {exc}")
            return self._empty(symbol, received)

    # ------------------------------------------------------------------ mapping
    def _map(self, symbol: str, payload: dict, received: str) -> dict:  # pragma: no cover - live
        quote = payload.get("latestQuote") or {}
        trade = payload.get("latestTrade") or {}
        minute_bar = payload.get("minuteBar") or {}
        daily_bar = payload.get("dailyBar") or {}
        prev_bar = payload.get("prevDailyBar") or {}

        bid = quote.get("bp")
        ask = quote.get("ap")
        last = trade.get("p") or daily_bar.get("c")
        prev_close = prev_bar.get("c")
        volume = daily_bar.get("v")
        prev_volume = prev_bar.get("v")
        spread = (ask - bid) if (bid is not None and ask is not None) else None
        spread_pct = (spread / last) if (spread is not None and last) else None
        change_pct = ((last - prev_close) / prev_close) if (last and prev_close) else None
        rel_volume = (volume / prev_volume) if (volume and prev_volume) else None

        return {
            "symbol": symbol,
            "provider": PROVIDER,
            "feed": self.feed,
            "is_mock": False,
            "last_price": last,
            "prev_close": prev_close,
            "bid": bid,
            "ask": ask,
            "spread": round(spread, 4) if spread is not None else None,
            "spread_pct": round(spread_pct, 6) if spread_pct is not None else None,
            "volume": volume,
            "avg_volume": prev_volume,
            "rel_volume": round(rel_volume, 3) if rel_volume is not None else None,
            "dollar_volume": round(last * volume, 2) if (last and volume) else None,
            "change_pct": round(change_pct, 4) if change_pct is not None else None,
            "bar_open": daily_bar.get("o"),
            "bar_high": daily_bar.get("h"),
            "bar_low": daily_bar.get("l"),
            "bar_close": daily_bar.get("c"),
            "quote_timestamp": quote.get("t"),
            "bar_timestamp": minute_bar.get("t") or daily_bar.get("t"),
            "source_timestamp": quote.get("t"),
            "received_at": received,
            "market_session": timeutils.market_session().value,
        }

    def _empty(self, symbol: str, received: str) -> dict:
        return {
            "symbol": symbol,
            "provider": PROVIDER,
            "feed": self.feed,
            "is_mock": False,
            "last_price": None,
            "prev_close": None,
            "bid": None,
            "ask": None,
            "spread": None,
            "spread_pct": None,
            "volume": None,
            "avg_volume": None,
            "rel_volume": None,
            "dollar_volume": None,
            "change_pct": None,
            "bar_open": None,
            "bar_high": None,
            "bar_low": None,
            "bar_close": None,
            "quote_timestamp": None,
            "bar_timestamp": None,
            "source_timestamp": None,
            "received_at": received,
            "market_session": timeutils.market_session().value,
        }

    def _log(self, sev, msg: str) -> None:
        if self.journal is not None:
            self.journal.log_system_event(sev, "market_data", msg)
