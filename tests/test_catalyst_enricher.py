"""Catalyst enricher (Roadmap 2.4) — status derivation + fail-safe, all hermetic."""

from __future__ import annotations

import types
from datetime import timedelta

from alphaos.config.settings import load_settings
from alphaos.constants import CatalystStatus
from alphaos.news.catalyst_enricher import CatalystEnricher
from alphaos.news.official_news_provider import MockOfficialNewsProvider, NewsArticle
from alphaos.util import timeutils


def _s(**over):
    return load_settings(load_env_file=False, env={"ALPHAOS_MODE": "mock", **over})


def _pkt(sym="AMD"):
    return types.SimpleNamespace(symbol=sym)


class _Fake:
    name = "fake"

    def __init__(self, articles=None, raise_=False):
        self._a = articles or []
        self._raise = raise_

    def get_news_for_symbol(self, symbol, lookback_hours):
        if self._raise:
            raise RuntimeError("boom")
        return self._a


def _art(category, mins_ago=30, rel=0.7, idx=0):
    return NewsArticle(
        source="X", title=f"{category} headline", summary="s", external_id=f"id{idx}",
        published_at_utc=(timeutils.now_utc() - timedelta(minutes=mins_ago)).isoformat(),
        symbols=["AMD"], category=category, relevance_score=rel,
    )


def test_disabled_returns_unavailable():
    ctx = CatalystEnricher(_s(NEWS_ENRICHMENT_ENABLED="false")).enrich(_pkt())
    assert ctx.catalyst_status == CatalystStatus.UNAVAILABLE.value
    assert ctx.enrichment_status == "disabled"


def test_none_found_when_no_articles():
    ctx = CatalystEnricher(_s(), provider=_Fake([])).enrich(_pkt())
    assert ctx.catalyst_status == CatalystStatus.NONE_FOUND.value


def test_possible_single_recent_article():
    ctx = CatalystEnricher(_s(), provider=_Fake([_art("company_news", 30, rel=0.6)])).enrich(_pkt())
    assert ctx.catalyst_status == CatalystStatus.POSSIBLE.value
    assert ctx.catalyst_type == "company_news"


def test_confirmed_when_corroborated():
    ctx = CatalystEnricher(_s(), provider=_Fake([_art("earnings", 20, 0.7, 0), _art("earnings", 40, 0.7, 1)])).enrich(_pkt())
    assert ctx.catalyst_status == CatalystStatus.CONFIRMED.value
    assert ctx.source_count == 2


def test_conflicting_on_opposite_analyst_actions():
    ctx = CatalystEnricher(_s(), provider=_Fake([_art("analyst_upgrade", 30, 0.7, 0), _art("analyst_downgrade", 40, 0.7, 1)])).enrich(_pkt())
    assert ctx.catalyst_status == CatalystStatus.CONFLICTING.value
    assert "conflicting_headlines" in ctx.catalyst_risk_tags


def test_stale_when_older_than_max_age():
    ctx = CatalystEnricher(_s(NEWS_MAX_AGE_HOURS="24"), provider=_Fake([_art("company_news", 60 * 40)])).enrich(_pkt())
    assert ctx.catalyst_status == CatalystStatus.STALE.value
    assert "stale_catalyst" in ctx.catalyst_risk_tags


def test_provider_error_fails_safe_unavailable():
    ctx = CatalystEnricher(_s(), provider=_Fake(raise_=True)).enrich(_pkt())
    assert ctx.catalyst_status == CatalystStatus.UNAVAILABLE.value   # fail-open default
    assert ctx.enrichment_status == "error"


def test_provider_error_status_error_when_not_fail_open():
    ctx = CatalystEnricher(_s(NEWS_FAIL_OPEN_AS_UNAVAILABLE="false"), provider=_Fake(raise_=True)).enrich(_pkt())
    assert ctx.catalyst_status == CatalystStatus.ERROR.value


def test_context_is_compact_no_raw_articles():
    ctx = CatalystEnricher(_s(), provider=_Fake([_art("sector_news", 30)])).enrich(_pkt())
    pd = ctx.to_packet_dict()
    assert "url" not in pd and "title" not in pd            # no raw article bodies/urls
    assert all(isinstance(s, str) for s in pd["catalyst_sources"])  # source NAMES only
    assert "catalyst_not_company_specific" in pd["catalyst_risk_tags"]  # sector -> sympathy risk


def test_mock_enricher_is_deterministic():
    a = CatalystEnricher(_s(), provider=MockOfficialNewsProvider()).enrich(_pkt("AAPL"))
    b = CatalystEnricher(_s(), provider=MockOfficialNewsProvider()).enrich(_pkt("AAPL"))
    assert a.catalyst_status == b.catalyst_status and a.catalyst_type == b.catalyst_type
