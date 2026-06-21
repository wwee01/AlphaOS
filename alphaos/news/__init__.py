"""News layer — v1 runs in NO-NEWS mode.

The active runtime imports nothing from the deferred Benzinga/web connectors.
Only the no-news ``NewsService`` and the ``NewsItem`` shape live here.
"""

from alphaos.news.news_service import NewsItem, NewsService

__all__ = ["NewsItem", "NewsService"]
