"""DEFERRED: isolated web-news scraper.

Disabled in v1. Kept as a labelled seam only — every entry point raises
``deferred in v1`` and never scrapes or returns fabricated news.

Activation trigger: see connectors/deferred/DEFERRED.md ("News layer").
"""

from __future__ import annotations

from alphaos.constants import DEFERRED_IN_V1


class WebNewsConnector:
    """Reserved web-news scraper. Inert in v1."""

    name = "web_scrape"

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(DEFERRED_IN_V1)

    def fetch(self, symbol: str):
        raise NotImplementedError(DEFERRED_IN_V1)
