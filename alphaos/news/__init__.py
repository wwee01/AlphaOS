"""News connectors (Benzinga primary, isolated web fallback) + news service.

Hard rule (resolved decision): mock/fixture news is NEVER used for runtime
candidate evaluation. Connectors return real items or nothing. Fixtures carry
the TEST_FIXTURE_NEWS label and are filtered out defensively here, so they can
only ever be used inside tests.
"""

from alphaos.news.news_service import NewsItem, NewsService

__all__ = ["NewsItem", "NewsService"]
