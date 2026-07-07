"""News service — v1 NO-NEWS mode.

News is disabled in v1: the candidate scanner and OpenAI evaluation run on
price/volume/structure only. This service does NOT import or call any news
connector (Benzinga / web scraper are deferred). It exists so the rest of the
system has a stable seam, and it always reports the no-news status.

It never fabricates news and never returns fixture news at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

from alphaos.constants import NewsStatus


@dataclass
class NewsItem:
    """Retained for schema/test stability; not produced at runtime in v1."""

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

    @property
    def enabled(self) -> bool:
        return self.settings.news_enabled  # always False in v1 (load fails otherwise)

    def get_news(self, symbol: str, persist: bool = True) -> tuple[list[NewsItem], NewsStatus]:
        """Always returns ([], DISABLED_V1) in v1. No connectors are touched."""
        return [], NewsStatus.DISABLED_V1
