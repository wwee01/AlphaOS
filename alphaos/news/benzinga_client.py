"""Benzinga news connector (preferred source).

Used only when ``BENZINGA_API_KEY`` is present. With no key (or in mock mode) it
returns an empty list — it NEVER fabricates news. The live path is an
import-guarded urllib stub with a timeout; real response mapping is marked TODO.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING

from alphaos.constants import Severity
from alphaos.util import timeutils

if TYPE_CHECKING:  # pragma: no cover
    from alphaos.news.news_service import NewsItem

HTTP_TIMEOUT = 10
PROVIDER = "benzinga"


class BenzingaClient:
    def __init__(self, settings, journal=None):
        self.settings = settings
        self.journal = journal

    @property
    def available(self) -> bool:
        return self.settings.has_benzinga_key and not self.settings.is_mock

    def fetch(self, symbol: str) -> list["NewsItem"]:
        if not self.available:
            return []
        return self._fetch_live(symbol)

    def _fetch_live(self, symbol: str) -> list["NewsItem"]:  # pragma: no cover - live stub
        from alphaos.news.news_service import NewsItem

        params = urllib.parse.urlencode(
            {"token": self.settings.benzinga_api_key, "tickers": symbol, "pageSize": 10}
        )
        url = f"https://api.benzinga.com/api/v2/news?{params}"
        fetched_at = timeutils.now_utc().isoformat()
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            items: list[NewsItem] = []
            for art in payload if isinstance(payload, list) else payload.get("data", []):
                items.append(
                    NewsItem(
                        symbol=symbol,
                        provider=PROVIDER,
                        source_url=art.get("url", ""),
                        source_name="Benzinga",
                        headline=art.get("title", ""),
                        published_at=art.get("created"),
                        fetched_at=fetched_at,
                        summary=(art.get("teaser") or "")[:1000],
                        timestamp_confidence="high",
                        parsing_notes="benzinga api",
                    )
                )
            return items
        except (urllib.error.URLError, json.JSONDecodeError, KeyError, ValueError) as exc:
            if self.journal is not None:
                self.journal.log_system_event(
                    Severity.WARNING,
                    "news",
                    f"Benzinga fetch failed for {symbol}.",
                    {"error": str(exc)},
                )
            return []
