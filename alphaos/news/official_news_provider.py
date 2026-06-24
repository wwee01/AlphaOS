"""Official news/catalyst provider abstraction (Roadmap 2.4).

A small, swappable interface so AlphaOS can later use Alpaca / Benzinga / SEC /
earnings-calendar sources without rewriting the enrichment pipeline. v1 ships a
deterministic MOCK provider (offline, hermetic) and a lazy Alpaca provider behind
config (disabled by default; never breaks if creds/SDK are missing).

This layer returns NORMALIZED official articles only. It is OFFICIAL/market news
— NOT social sentiment, NOT broad web scraping, NOT last30days (those are
explicit follow-ups and are not implemented here).
"""

from __future__ import annotations

import abc
import random
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Optional

from alphaos.constants import CatalystType
from alphaos.util import timeutils


@dataclass
class NewsArticle:
    """Normalized official-news article (the internal provider schema)."""

    source: str
    title: str
    summary: str
    external_id: str            # url or provider id
    published_at_utc: str
    symbols: list = field(default_factory=list)
    category: str = CatalystType.COMPANY_NEWS.value   # catalyst-type hint
    relevance_score: Optional[float] = None

    def as_dict(self) -> dict:
        return asdict(self)


class OfficialNewsProvider(abc.ABC):
    name = "base"

    @abc.abstractmethod
    def get_news_for_symbol(self, symbol: str, lookback_hours: float) -> list[NewsArticle]:
        ...

    def get_news_for_symbols(self, symbols: list[str], lookback_hours: float) -> dict[str, list[NewsArticle]]:
        return {s: self.get_news_for_symbol(s, lookback_hours) for s in symbols}


class MockOfficialNewsProvider(OfficialNewsProvider):
    """Deterministic, offline, clearly-mock official-news provider.

    Per symbol+trading-day it returns a reproducible scenario (a recent relevant
    headline, nothing, or two conflicting analyst actions). Sources are labelled
    ``MOCK_NEWS`` so nothing is mistaken for live data.
    """

    name = "mock"

    def get_news_for_symbol(self, symbol: str, lookback_hours: float) -> list[NewsArticle]:
        rng = random.Random(f"{symbol}:{timeutils.market_date()}")
        roll = rng.random()
        now = timeutils.now_utc()

        def mk(cat: str, mins_ago: int, title: str, summary: str, idx: int = 0) -> NewsArticle:
            return NewsArticle(
                source="MOCK_NEWS",
                title=title,
                summary=summary,
                external_id=f"mock://{symbol}/{idx}",
                published_at_utc=(now - timedelta(minutes=mins_ago)).isoformat(),
                symbols=[symbol],
                category=cat,
                relevance_score=round(rng.uniform(0.55, 0.95), 2),
            )

        if roll < 0.45:
            cat = rng.choice([
                CatalystType.EARNINGS.value, CatalystType.ANALYST_UPGRADE.value,
                CatalystType.COMPANY_NEWS.value, CatalystType.PRODUCT_LAUNCH.value,
                CatalystType.SECTOR_NEWS.value,
            ])
            n = 2 if rng.random() < 0.4 else 1   # sometimes corroborated -> confirmed
            return [
                mk(cat, rng.randint(15, 600), f"{symbol}: {cat.replace('_', ' ')} reported",
                   f"Mock official {cat.replace('_', ' ')} headline for {symbol} (no real data).", i)
                for i in range(n)
            ]
        if roll < 0.72:
            return []   # -> none_found
        # conflicting analyst actions
        return [
            mk(CatalystType.ANALYST_UPGRADE.value, 70, f"{symbol} upgraded by a desk", "Mock upgrade.", 0),
            mk(CatalystType.ANALYST_DOWNGRADE.value, 95, f"{symbol} downgraded by another desk", "Mock downgrade.", 1),
        ]


class AlpacaNewsProvider(OfficialNewsProvider):  # pragma: no cover - live, disabled by default
    """Lazy Alpaca official-news provider (alpaca-py NewsClient). Used ONLY when
    NEWS_ENRICHMENT_PROVIDER=alpaca. Never breaks if the SDK/creds are missing —
    it raises, and the enricher fails safe."""

    name = "alpaca"

    def __init__(self, settings):
        self.settings = settings

    def get_news_for_symbol(self, symbol: str, lookback_hours: float) -> list[NewsArticle]:
        from alpaca.data.historical.news import NewsClient
        from alpaca.data.requests import NewsRequest

        client = NewsClient(api_key=self.settings.alpaca_api_key, secret_key=self.settings.alpaca_secret_key)
        start = timeutils.now_utc() - timedelta(hours=float(lookback_hours))
        resp = client.get_news(NewsRequest(symbols=symbol, start=start,
                                           limit=int(self.settings.news_max_articles_per_symbol)))
        out: list[NewsArticle] = []
        for a in getattr(resp, "news", []) or []:
            out.append(NewsArticle(
                source=getattr(a, "source", "alpaca") or "alpaca",
                title=getattr(a, "headline", "") or "",
                summary=(getattr(a, "summary", "") or "")[:280],
                external_id=str(getattr(a, "id", "") or getattr(a, "url", "")),
                published_at_utc=str(getattr(a, "created_at", "") or ""),
                symbols=list(getattr(a, "symbols", []) or [symbol]),
                category=CatalystType.COMPANY_NEWS.value,
            ))
        return out


def make_news_provider(settings) -> Optional[OfficialNewsProvider]:
    """Build the configured provider, or None if disabled. Never raises."""
    if not settings.news_enrichment_enabled:
        return None
    provider = (settings.news_enrichment_provider or "mock").lower()
    if provider in ("disabled", "none", ""):
        return None
    if provider == "alpaca":
        if not settings.has_alpaca_keys:   # no creds -> behave as disabled (fail open)
            return None
        return AlpacaNewsProvider(settings)
    return MockOfficialNewsProvider()
