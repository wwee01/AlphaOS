"""Isolated web-news fallback connector.

Used only when Benzinga is unavailable/incomplete. Polite scraping only: short
timeouts, error handling, full source logging, and explicit failure reporting.
It does NOT bypass paywalls, auth, robots restrictions, or anti-bot protections.

In v1 this is a conservative stub: it returns nothing by default (no fabricated
news), records what it would fetch, and provides the data shape a real isolated
connector must populate (source URL, publisher, headline, published/fetched
timestamps, summary, timestamp confidence, parsing errors).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alphaos.constants import Severity

if TYPE_CHECKING:  # pragma: no cover
    from alphaos.news.news_service import NewsItem

PROVIDER = "web_scrape"


class WebNewsClient:
    def __init__(self, settings, journal=None):
        self.settings = settings
        self.journal = journal

    @property
    def available(self) -> bool:
        # The scraping connector is intentionally inert in v1 (stub). Even when
        # enabled it must never run in mock mode.
        return False and not self.settings.is_mock

    def fetch(self, symbol: str) -> list["NewsItem"]:
        if self.settings.is_mock:
            return []
        if self.journal is not None:
            self.journal.log_system_event(
                Severity.INFO,
                "news",
                f"Web news fallback is a v1 stub; no scrape performed for {symbol}.",
            )
        # A real implementation returns NewsItem(provider='web_scrape', ...) with
        # full source logging and timestamp_confidence. Never returns mock data.
        return []
