"""Tradable-asset reference data (Alpaca trading API, EXP-0 only).

A separate, narrow capability from ``MarketDataClient``/``AlpacaBarsProvider``
(both market-data): this hits the TRADING account's ``/v2/assets`` endpoint
(``settings.alpaca_base_url`` -- paper by default, same base URL
``AlpacaClient`` already uses for orders/account reads), which is the only
Alpaca endpoint that lists the tradable universe at all. Used exclusively by
the one-off ``universe_build`` CLI (never the scan/eval/risk/execution path)
to screen candidates for the EXP-0 shadow tier.

Same pattern as ``alpaca_bars.py``/``alpaca_data.py``: raw REST (no SDK
response-shape surprises), fails safe to an empty list on any error, real
network calls only exercised behind ``RUN_LIVE_ALPACA_TESTS=true``.

HONESTY NOTE (read before trusting the ``is_probable_etf``/name-based filter):
Alpaca's asset object has no dedicated "is ETF" or "listing date" field -- both
``is_probable_etf`` and the caller's recent-IPO flag are BEST-EFFORT heuristics
(name-substring matching / bars-history depth), not authoritative facts. This
is exactly the reference-data gap the regime/text-archive reconciliation
flagged: TEXT-0's SEC company-facts capture is the designated authoritative
source for a later, better-informed universe screen (UNIV-D) -- this module
is deliberately not trying to be that now.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Optional

from alphaos.constants import Severity

HTTP_TIMEOUT = 20

# Best-effort, non-exhaustive: common name substrings for ETFs/ETNs/funds and
# well-known leveraged/inverse product families. A symbol matching none of
# these is NOT guaranteed to be a genuine single-company equity -- it is only
# not OBVIOUSLY one of these families. See module docstring.
_FUND_NAME_MARKERS = (
    "etf", "etn", " fund", "trust", "proshares", "direxion", "ishares",
    "vaneck", "invesco", "spdr", "wisdomtree", "graniteshares",
)


class AlpacaAssetsProvider:
    name = "alpaca"

    def __init__(self, settings, journal=None):
        self.settings = settings
        self.journal = journal

    def get_tradable_us_equities(self) -> list[dict]:
        """Active, tradable US-equity-class assets on NYSE/NASDAQ. Returns
        ``[]`` on any error/missing-creds -- callers must treat that as
        "screen unavailable", never as "zero tradable assets exist"."""
        if not self.settings.has_alpaca_keys:
            self._log(Severity.WARNING, "No Alpaca creds; asset screen unavailable.")
            return []
        try:  # pragma: no cover - live network path (gated test only)
            url = f"{self.settings.alpaca_base_url}/v2/assets?status=active&asset_class=us_equity"
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
            return self._filter(payload)
        except (urllib.error.URLError, json.JSONDecodeError, KeyError, ValueError) as exc:
            self._log(Severity.WARNING, f"Alpaca asset screen fetch failed: {exc}")
            return []

    @staticmethod
    def _filter(payload: list) -> list[dict]:  # pragma: no cover - live
        out = []
        for a in payload or []:
            if not a.get("tradable"):
                continue
            if a.get("exchange") not in ("NYSE", "NASDAQ"):
                continue
            out.append({
                "symbol": a.get("symbol"),
                "name": a.get("name") or "",
                "exchange": a.get("exchange"),
                "is_probable_etf": is_probable_etf(a.get("name") or ""),
            })
        return out

    def _log(self, sev, msg: str) -> None:
        if self.journal is not None:
            self.journal.log_system_event(sev, "universe_build", msg)


def is_probable_etf(name: str) -> bool:
    """Best-effort only -- see module docstring."""
    lowered = name.lower()
    return any(marker in lowered for marker in _FUND_NAME_MARKERS)


def make_assets_provider(settings, journal=None) -> Optional[AlpacaAssetsProvider]:
    """Build the live assets provider, or None in mock/offline mode (nothing
    to screen against; tests inject a fixture list directly instead)."""
    if settings.is_mock or settings.offline_mode:
        return None
    return AlpacaAssetsProvider(settings, journal)
