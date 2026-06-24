"""Official news provider abstraction (Roadmap 2.4) — deterministic mock + factory."""

from __future__ import annotations

from alphaos.config.settings import load_settings
from alphaos.news.official_news_provider import MockOfficialNewsProvider, make_news_provider


def _s(**over):
    return load_settings(load_env_file=False, env={"ALPHAOS_MODE": "mock", **over})


def test_mock_provider_is_deterministic_and_labelled():
    p = MockOfficialNewsProvider()
    a = p.get_news_for_symbol("AAPL", 48)
    b = p.get_news_for_symbol("AAPL", 48)
    assert [x.title for x in a] == [x.title for x in b]
    for art in a:
        assert art.source == "MOCK_NEWS"          # clearly mock, never mistaken for live
        assert "AAPL" in art.symbols


def test_factory_defaults_to_mock():
    assert isinstance(make_news_provider(_s()), MockOfficialNewsProvider)


def test_factory_disabled_returns_none():
    assert make_news_provider(_s(NEWS_ENRICHMENT_ENABLED="false")) is None
    assert make_news_provider(_s(NEWS_ENRICHMENT_PROVIDER="disabled")) is None


def test_factory_alpaca_without_creds_fails_open_to_none():
    # alpaca provider needs creds; missing creds -> behave as disabled (never crash).
    assert make_news_provider(_s(NEWS_ENRICHMENT_PROVIDER="alpaca")) is None
