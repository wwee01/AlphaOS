"""INSTR-1 (part 1): verifies AlpacaDataProvider._map() is actually wired to
the new curve-normalized formula. _map() has no live-network dependency (it
is pure dict transformation) and previously had zero test coverage at all
(only exercised behind RUN_LIVE_ALPACA_TESTS) -- direct construction here,
no network, no wall-clock dependence.
"""

from __future__ import annotations

from alphaos.data.providers.alpaca_data import AlpacaDataProvider
from conftest import make_settings


def _provider():
    return AlpacaDataProvider(make_settings())


def _payload(daily_volume, prev_volume):
    return {
        "latestQuote": {"bp": 99.9, "ap": 100.1, "t": "2026-07-09T14:00:00Z"},
        "latestTrade": {"p": 100.0},
        "minuteBar": {"t": "2026-07-09T14:00:00Z"},
        "dailyBar": {"o": 99.0, "h": 101.0, "l": 98.5, "c": 100.0, "v": daily_volume,
                     "t": "2026-07-09T14:00:00Z"},
        "prevDailyBar": {"c": 98.0, "v": prev_volume},
    }


def test_map_uses_curve_normalized_formula_not_the_old_ratio():
    """1 hour after the open (14:00 UTC == 10:00 ET during EDT), expected
    fraction = 0.15 (30-min breakpoint... wait, 30 min in is 10:00 ET which
    IS the 30-minute breakpoint). volume=300_000, prev=1_000_000 ->
    old formula = 0.3; v2 = 300_000 / (1_000_000 * 0.15) = 2.0."""
    provider = _provider()
    row = provider._map("AAPL", _payload(daily_volume=300_000, prev_volume=1_000_000), "2026-07-09T14:00:00+00:00")

    assert row["rel_volume"] == 2.0
    assert row["rel_volume"] != 300_000 / 1_000_000  # not the old formula's 0.3


def test_map_rel_volume_none_when_before_the_open():
    provider = _provider()
    row = provider._map(
        "AAPL", _payload(daily_volume=1000, prev_volume=1_000_000), "2026-07-09T09:00:00+00:00",
    )
    assert row["rel_volume"] is None


def test_map_rel_volume_none_when_prev_volume_missing():
    provider = _provider()
    payload = _payload(daily_volume=300_000, prev_volume=None)
    row = provider._map("AAPL", payload, "2026-07-09T14:00:00+00:00")
    assert row["rel_volume"] is None


def test_map_still_populates_volume_and_avg_volume_fields_unchanged():
    """The raw ingredient fields (volume/avg_volume) are untouched by this
    fix -- only the derived rel_volume formula changed."""
    provider = _provider()
    row = provider._map("AAPL", _payload(daily_volume=300_000, prev_volume=1_000_000), "2026-07-09T14:00:00+00:00")
    assert row["volume"] == 300_000
    assert row["avg_volume"] == 1_000_000
