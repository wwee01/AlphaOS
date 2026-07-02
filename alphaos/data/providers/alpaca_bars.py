"""Historical daily-bar provider (Alpaca free IEX tier).

A separate, narrow capability from ``MarketDataClient`` (which only serves the
CURRENT snapshot): this fetches historical daily OHLCV bars for a symbol/date
range, used by the measurement layer (MFE/MAE backfill, forward-outcome
tracking, bracket replay) — never by the live scan/eval/risk/execution path.

Same pattern as ``alpaca_data.py``: raw REST (no SDK response-shape surprises),
fails safe to an empty list on any error (never raises, never blocks a caller),
real network calls only exercised behind ``RUN_LIVE_ALPACA_TESTS=true``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Optional

from alphaos.config.settings import ALPACA_DATA_BASE_URL
from alphaos.constants import Severity

HTTP_TIMEOUT = 15


class AlpacaBarsProvider:
    name = "alpaca"

    def __init__(self, settings, journal=None):
        self.settings = settings
        self.journal = journal

    def get_daily_bars(self, symbol: str, start: str, end: str, limit: int = 200) -> list[dict]:
        """Daily OHLCV bars for ``symbol`` in [start, end] (YYYY-MM-DD, inclusive).
        Returns ``[]`` on any error/missing-creds — callers must treat that as
        "bars unavailable", never as "zero real bars exist"."""
        if not self.settings.has_alpaca_keys:
            self._log(Severity.WARNING, f"No Alpaca creds; historical bars unavailable for {symbol}.")
            return []
        try:  # pragma: no cover - live network path (gated test only)
            url = (
                f"{ALPACA_DATA_BASE_URL}/v2/stocks/{symbol}/bars"
                f"?timeframe=1Day&start={start}&end={end}&limit={int(limit)}"
                f"&feed={self.settings.market_data_feed}&adjustment=raw"
            )
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
            return self._map(payload)
        except (urllib.error.URLError, json.JSONDecodeError, KeyError, ValueError) as exc:
            self._log(Severity.WARNING, f"Historical bars fetch failed for {symbol}: {exc}")
            return []

    @staticmethod
    def _map(payload: dict) -> list[dict]:  # pragma: no cover - live
        bars = payload.get("bars") or []
        out = []
        for b in bars:
            ts = b.get("t") or ""
            out.append({
                "date": ts[:10] if ts else None,
                "open": b.get("o"), "high": b.get("h"),
                "low": b.get("l"), "close": b.get("c"),
                "volume": b.get("v"),
            })
        return out

    def _log(self, sev, msg: str) -> None:
        if self.journal is not None:
            self.journal.log_system_event(sev, "market_data", msg)


def make_bars_provider(settings, journal=None) -> Optional[AlpacaBarsProvider]:
    """Build the live bars provider, or None in mock/offline mode (nothing to
    fetch against; callers should already have injected fixture bars in tests)."""
    if settings.is_mock or settings.offline_mode:
        return None
    return AlpacaBarsProvider(settings, journal)
