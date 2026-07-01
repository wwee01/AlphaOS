"""Official news provider abstraction (Roadmap 2.4) — deterministic mock + factory."""

from __future__ import annotations

import datetime

from alphaos.config.settings import load_settings
from alphaos.news.official_news_provider import (
    AlpacaNewsProvider,
    MockOfficialNewsProvider,
    _news_items,
    make_news_provider,
)


def _s(**over):
    return load_settings(load_env_file=False, env={"ALPHAOS_MODE": "mock", **over})


class _Art:
    """Minimal stand-in for an alpaca-py news article object."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _NewsSet:
    """Mimics alpaca-py ``NewsSet``: articles live at ``.data['news']`` and via
    ``__getitem__``, and there is deliberately NO ``.news`` attribute — that is
    the exact shape the live parser must handle."""

    def __init__(self, news):
        self.data = {"news": news}
        self.next_page_token = None

    def __getitem__(self, key):
        return self.data[key]


def _article(**over):
    base = dict(
        id=123,
        headline="AAPL jumps on earnings beat",
        source="benzinga",
        url="https://example.com/a",
        summary="",
        content="Body text",
        created_at=datetime.datetime(2026, 6, 30, 16, 5, 26, tzinfo=datetime.timezone.utc),
        symbols=["AAPL", "SPY"],
    )
    base.update(over)
    return _Art(**base)


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


def test_alpaca_parse_reads_newsset_not_dot_news():
    # Regression: alpaca-py returns a NewsSet exposing articles at ['news'] /
    # .data['news'] but NOT .news. A getattr(resp, "news") returned [] and made
    # every live catalyst look like none_found. Guard against that exact shape.
    resp = _NewsSet([_article()])
    assert not hasattr(resp, "news")                 # the trap the old code fell into
    arts = AlpacaNewsProvider._parse_response(resp, "AAPL")
    assert len(arts) == 1
    a = arts[0]
    assert a.title == "AAPL jumps on earnings beat"
    assert a.source == "benzinga"
    assert a.external_id == "123"
    assert "AAPL" in a.symbols
    # datetime -> ISO string (T separator) so downstream age/stale parsing works
    assert a.published_at_utc == "2026-06-30T16:05:26+00:00"
    # summary falls back to content when the summary field is empty
    assert a.summary == "Body text"


def test_alpaca_parse_empty_and_none_never_raise():
    assert AlpacaNewsProvider._parse_response(_NewsSet([]), "AAPL") == []
    assert AlpacaNewsProvider._parse_response(None, "AAPL") == []


def test_news_items_handles_known_shapes():
    art = _article()
    assert _news_items(_NewsSet([art])) == [art]      # NewsSet (.data['news'])
    assert _news_items({"news": [art]}) == [art]      # raw dict
    assert _news_items([art]) == [art]                # bare list
    assert _news_items(None) == []                    # nothing
    assert _news_items(_Art(news=[art])) == [art]     # legacy .news attribute
