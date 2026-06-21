"""News service: orchestrates Benzinga + web fallback and enforces the
no-mock-news rule for the runtime path.

Returns real items or ``NEWS_UNAVAILABLE``. Fixture-labelled items are filtered
out defensively so they can never reach the proposal engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

from alphaos.constants import NewsStatus, Severity, TEST_FIXTURE_NEWS_LABEL
from alphaos.news.benzinga_client import BenzingaClient
from alphaos.news.web_news_client import WebNewsClient
from alphaos.util import timeutils
from alphaos.util.ids import new_id


@dataclass
class NewsItem:
    symbol: str
    provider: str
    source_url: str = ""
    source_name: str = ""
    headline: str = ""
    published_at: Optional[str] = None
    fetched_at: Optional[str] = None
    summary: str = ""
    sentiment: Optional[str] = None
    catalyst_type: Optional[str] = None
    timestamp_confidence: str = "unknown"
    parsing_notes: str = ""
    is_fixture: bool = False
    label: Optional[str] = None

    def as_dict(self) -> dict:
        return asdict(self)


class NewsService:
    def __init__(self, settings, journal=None):
        self.settings = settings
        self.journal = journal
        self.benzinga = BenzingaClient(settings, journal)
        self.web = WebNewsClient(settings, journal)

    def get_news(self, symbol: str, persist: bool = True) -> tuple[list[NewsItem], NewsStatus]:
        """Return (items, status). status is NEWS_UNAVAILABLE when nothing real
        is found. Never returns fabricated/fixture news in the runtime path."""
        items = self.benzinga.fetch(symbol)
        if not items:
            items = self.web.fetch(symbol)

        # Defensive: a fixture must never escape into runtime, regardless of
        # which connector produced it.
        clean = [i for i in items if not i.is_fixture and i.label != TEST_FIXTURE_NEWS_LABEL]
        dropped = len(items) - len(clean)
        if dropped and self.journal is not None:
            self.journal.log_system_event(
                Severity.CRITICAL,
                "news",
                f"Filtered {dropped} fixture-labelled news item(s) out of runtime for {symbol}.",
            )

        if persist and self.journal is not None:
            for it in clean:
                self._persist(it)

        if not clean:
            return [], NewsStatus.NEWS_UNAVAILABLE
        return clean, NewsStatus.AVAILABLE

    def _persist(self, item: NewsItem) -> None:
        self.journal.insert(
            "news_items",
            {
                "news_id": new_id("news"),
                "symbol": item.symbol,
                "provider": item.provider,
                "source_url": item.source_url,
                "source_name": item.source_name,
                "headline": item.headline,
                "published_at": item.published_at,
                "fetched_at": item.fetched_at or timeutils.now_utc().isoformat(),
                "summary": item.summary,
                "sentiment": item.sentiment,
                "catalyst_type": item.catalyst_type,
                "timestamp_confidence": item.timestamp_confidence,
                "parsing_notes": item.parsing_notes,
                "is_fixture": 1 if item.is_fixture else 0,
                "label": item.label,
            },
        )
