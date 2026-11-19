"""Only injected calendar and daily-bar capabilities; no network or ledger."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from alphaos.learning import close_ref_engine as engine
from alphaos.learning.close_ref_sources import CloseRefSources, SliceYielded, make_sources
from test_close_ref_engine import calendar_rows, stamp, window


class Calendar:
    def __init__(self):
        self.calls = []

    def get_calendar(self, start, end):
        self.calls.append((start, end))
        return calendar_rows(start, end)


class Bars:
    def __init__(self):
        self.calls = []
        self.settings = SimpleNamespace(market_data_feed="iex")

    def get_daily_bars(self, symbol, *, start, end, limit):
        self.calls.append((symbol, start, end, limit))
        return [{"date": start, "close": 100}, {"date": "2026-11-23", "close": 110}]


def source(**kwargs):
    return CloseRefSources(Calendar(), Bars(), provider="alpaca", feed="iex",
                           now=lambda: stamp("2026-11-23T21:00:02Z"), **kwargs)


def test_pinned_calendar_preserves_early_close_without_broker_request():
    sources = source()
    w = sources.window(stamp("2026-11-19T15:00:00Z"))
    assert w.endpoint_5.close == stamp("2026-11-27T18:00:00Z")
    assert sources.calendar_client.calls == []
    assert w.calendar.sha256 == engine.pinned_calendar().sha256


def test_no_calendar_call_for_naive_timestamp_or_yield():
    sources = source()
    with pytest.raises(ValueError, match="aware"):
        sources.window(datetime(2026, 11, 16))
    assert not sources.calendar_client.calls
    sources.should_yield = lambda: True
    with pytest.raises(SliceYielded):
        sources.window(stamp("2026-11-16T15:00:00Z"))
    assert not sources.calendar_client.calls


def test_pinned_calendar_does_not_depend_on_broker_availability():
    sources = source()
    sources.calendar_client.get_calendar = lambda **kw: []
    assert sources.window(stamp("2026-11-16T15:00:00Z")).reference.date == "2026-11-16"
    assert not sources.calendar_client.calls


def test_provider_receipt_matches_query_and_separate_spy_cache_is_owned():
    sources = source()
    a = sources.observe(window(), 5, "ABC")
    b = sources.observe(window(), 5, "SPY")
    b["request"]["feed"] = "tampered"
    b["leg"]["return_fraction"] = 999
    again = sources.observe(window(), 5, "SPY")
    assert again["leg"]["return_fraction"] == pytest.approx(.1)
    assert again["request"]["feed"] == "iex"
    assert a["request"]["end"] == "2026-11-24"
    assert sources.bars_provider.calls == [
        ("ABC", "2026-11-16", "2026-11-24", 64),
        ("SPY", "2026-11-16", "2026-11-24", 64),
    ]
    # Another invocation has no persistent response cache.
    fresh = source()
    fresh.observe(window(), 5, "SPY")
    assert len(fresh.bars_provider.calls) == 1


def test_feed_change_cannot_relabel_cached_spy_or_new_request():
    sources = source()
    sources.observe(window(), 5, "SPY")
    sources.bars_provider.settings.market_data_feed = "sip"
    with pytest.raises(ValueError, match="feed_changed"):
        sources.observe(window(), 5, "SPY")
    assert len(sources.bars_provider.calls) == 1


@pytest.mark.parametrize("when,status", [("2026-11-23T20:59:59Z", "pending"),
                                         ("2026-12-03T21:00:01Z", "unavailable")])
def test_not_due_and_expired_never_call_provider(when, status):
    sources = source()
    sources.now = lambda: stamp(when)
    result = sources.observe(window(), 5, "ABC")
    assert result["leg"]["status"] == status
    assert result["request"] is None
    assert sources.bars_provider.calls == []


def test_yield_before_fetch_and_safe_provider_error_receipt():
    sources = source(should_yield=lambda: True)
    with pytest.raises(SliceYielded):
        sources.observe(window(), 5, "ABC")
    assert sources.bars_provider.calls == []
    sources.should_yield = lambda: False

    def failure(*a, **kw):
        raise RuntimeError("do not persist provider error text")

    sources.bars_provider.get_daily_bars = failure
    result = sources.observe(window(), 5, "ABC")
    assert result["leg"]["status"] == "pending"
    assert result["request"]["error_type"] == "RuntimeError"
    assert "do not persist" not in str(result)


def test_cached_response_keeps_original_request_time_and_expires():
    sources = source()
    first = sources.observe(window(), 5, "SPY")
    sources.now = lambda: stamp("2026-11-24T21:00:02Z")
    again = sources.observe(window(), 5, "SPY")
    assert first["request"] == again["request"]
    sources.now = lambda: engine.retry_until(window().endpoint_5) + timedelta(seconds=1)
    assert sources.observe(window(), 5, "SPY")["leg"]["status"] == "unavailable"
    assert len(sources.bars_provider.calls) == 1


def test_runtime_factory_does_not_construct_live_capabilities_in_mock():
    orch = SimpleNamespace(settings=SimpleNamespace(is_mock=True, offline_mode=False))
    assert make_sources(orch, Bars()) is None


def test_pinned_calendar_refuses_outside_coverage():
    sources = source()
    sources.calendar_client.get_calendar = lambda **kw: None
    with pytest.raises(engine.CalendarPending):
        sources.window(stamp("2027-07-01T15:00:00Z"))


def test_feed_changed_during_fetch_is_not_saved_with_stale_provenance():
    sources = source()
    original = sources.bars_provider.get_daily_bars

    def changed(*args, **kwargs):
        sources.bars_provider.settings.market_data_feed = "sip"
        return original(*args, **kwargs)

    sources.bars_provider.get_daily_bars = changed
    with pytest.raises(ValueError, match="feed_changed"):
        sources.observe(window(), 5, "SPY")
    assert not sources._spy
