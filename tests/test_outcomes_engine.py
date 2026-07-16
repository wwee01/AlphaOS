"""Pure compute for the counterfactual outcome ledger (Fable 5 review PR2).
Every function here is network-free and journal-free — pure math over plain
dicts/lists — so these tests need no fixtures beyond bar lists. Sign
conventions must match position_manager.py's live watchdog exactly (this is
what makes a replay a faithful reproduction of what AlphaOS would have done,
not an invented convention)."""

from __future__ import annotations

from alphaos.learning.outcomes_engine import (
    forward_window_stats,
    replay_bracket,
    signed_r,
    signed_return_pct,
)


# ------------------------------------------------------------------- signed_*
def test_signed_return_pct_long_favorable():
    assert signed_return_pct(100.0, 110.0, "long") == 0.1


def test_signed_return_pct_short_favorable():
    # short: price falling is favorable
    assert signed_return_pct(100.0, 90.0, "short") == 0.1


def test_signed_r_long():
    assert signed_r(reference=100.0, price=115.0, direction="long", stop=90.0) == 1.5


def test_signed_r_short():
    assert signed_r(reference=100.0, price=85.0, direction="short", stop=110.0) == 1.5


def test_signed_r_none_without_stop():
    assert signed_r(100.0, 110.0, "long", stop=None) is None
    assert signed_r(100.0, 110.0, "long", stop=0) is None


def test_signed_return_pct_none_on_missing_inputs():
    assert signed_return_pct(None, 100.0, "long") is None
    assert signed_return_pct(100.0, None, "long") is None


# ------------------------------------------------------------ forward window
def test_forward_window_stats_uses_last_bar_close_for_point_in_time():
    bars = [
        {"date": "d1", "high": 102, "low": 99, "close": 101},
        {"date": "d2", "high": 108, "low": 100, "close": 107},
        {"date": "d3", "high": 105, "low": 96, "close": 104},
    ]
    s = forward_window_stats(reference=100.0, stop=90.0, direction="long", bars=bars, n_days=3)
    assert s["bars_used"] == 3
    assert s["r"] == round((104 - 100) / 10, 4)                # last bar's close
    assert s["return_pct"] == round((104 - 100) / 100, 4)
    assert s["max_favorable_r"] == round((108 - 100) / 10, 4)  # best high across window
    assert s["max_adverse_r"] == round((96 - 100) / 10, 4)     # worst low across window


def test_forward_window_stats_partial_window_signals_bars_used():
    bars = [{"date": "d1", "high": 102, "low": 99, "close": 101}]
    s = forward_window_stats(100.0, 90.0, "long", bars, n_days=5)
    assert s["bars_used"] == 1     # fewer than n_days -> caller decides pending/partial


def test_forward_window_stats_empty_bars_is_safe():
    s = forward_window_stats(100.0, 90.0, "long", [], n_days=3)
    assert s == {"return_pct": None, "r": None, "max_favorable_r": None,
                "max_adverse_r": None, "bars_to_favorable": None,
                "bars_to_adverse": None, "bars_used": 0}


def test_forward_window_stats_return_pct_works_without_stop():
    bars = [{"date": "d1", "high": 105, "low": 99, "close": 103}]
    s = forward_window_stats(100.0, None, "long", bars, n_days=1)
    assert s["return_pct"] == 0.03
    assert s["r"] is None and s["max_favorable_r"] is None   # can't R-normalize without a stop
    assert s["bars_to_favorable"] is None and s["bars_to_adverse"] is None


# --------------------------------------------------------- time-to-excursion (EVID-1)
def test_forward_window_stats_bars_to_favorable_and_adverse_first_touch():
    """The favorable extreme occurs on bar 2 (high=108), the adverse extreme
    on bar 3 (low=96) -- bars_to_favorable/adverse must report the 1-indexed
    position of each, not just the magnitude."""
    bars = [
        {"date": "d1", "high": 102, "low": 99, "close": 101},
        {"date": "d2", "high": 108, "low": 100, "close": 107},
        {"date": "d3", "high": 105, "low": 96, "close": 104},
    ]
    s = forward_window_stats(reference=100.0, stop=90.0, direction="long", bars=bars, n_days=3)
    assert s["bars_to_favorable"] == 2
    assert s["bars_to_adverse"] == 3


def test_forward_window_stats_bars_to_excursion_reports_first_occurrence_on_tie():
    """Two bars tie the same favorable extreme (high=108) -- bars_to_favorable
    must report the FIRST one (bar 1), i.e. time-to-first-touch, never a later
    bar that happens to re-tie."""
    bars = [
        {"date": "d1", "high": 108, "low": 99, "close": 101},
        {"date": "d2", "high": 108, "low": 100, "close": 107},
    ]
    s = forward_window_stats(reference=100.0, stop=90.0, direction="long", bars=bars, n_days=2)
    assert s["max_favorable_r"] == round((108 - 100) / 10, 4)
    assert s["bars_to_favorable"] == 1


# ---------------------------------------------------------------- bracket replay
def test_replay_bracket_target_hit_long():
    bars = [
        {"date": "d1", "high": 102, "low": 98, "close": 101},
        {"date": "d2", "high": 112, "low": 100, "close": 110},   # target=110 breached (high>=110)
    ]
    r = replay_bracket(entry=100.0, stop=90.0, target=110.0, direction="long", bars=bars)
    assert r["result"] == "target_hit"
    assert r["replay_r"] == 1.0   # risk=10 (100-90), reward=10 (110-100) -> RR=1.0
    assert r["replay_exit_reason"] == "target"


def test_replay_bracket_stop_hit_long():
    bars = [
        {"date": "d1", "high": 102, "low": 99, "close": 100},
        {"date": "d2", "high": 103, "low": 88, "close": 90},   # stop=90 breached (low<=90)
    ]
    r = replay_bracket(entry=100.0, stop=90.0, target=120.0, direction="long", bars=bars)
    assert r["result"] == "stop_hit"
    assert r["replay_r"] == -1.0
    assert r["replay_exit_reason"] == "stop"


def test_replay_bracket_target_hit_short():
    # short: entry=100, stop=110 (risk=10), target=85 (reward=15, RR=1.5)
    bars = [{"date": "d1", "high": 105, "low": 84, "close": 90}]   # low<=85 -> target
    r = replay_bracket(entry=100.0, stop=110.0, target=85.0, direction="short", bars=bars)
    assert r["result"] == "target_hit"
    assert r["replay_r"] == 1.5


def test_replay_bracket_stop_hit_short():
    bars = [{"date": "d1", "high": 111, "low": 95, "close": 108}]   # high>=110 -> stop
    r = replay_bracket(entry=100.0, stop=110.0, target=80.0, direction="short", bars=bars)
    assert r["result"] == "stop_hit"
    assert r["replay_r"] == -1.0


def test_replay_bracket_neither_marks_to_market_at_window_close():
    bars = [
        {"date": "d1", "high": 102, "low": 99, "close": 101},
        {"date": "d2", "high": 103, "low": 100, "close": 102},
    ]
    r = replay_bracket(entry=100.0, stop=90.0, target=150.0, direction="long", bars=bars)
    assert r["result"] == "neither"
    assert r["replay_r"] == round((102 - 100) / 10, 4)
    assert r["replay_exit_reason"] == "window_exhausted"


def test_replay_bracket_ambiguous_same_bar_never_guesses():
    # a single day's range touches both stop AND target -> can't order from daily OHLC
    bars = [{"date": "d1", "high": 115, "low": 85, "close": 100}]
    r = replay_bracket(entry=100.0, stop=90.0, target=110.0, direction="long", bars=bars)
    assert r["result"] == "ambiguous_same_bar"
    assert r["replay_r"] is None   # never invented
    assert r["replay_exit_reason"] == "both_levels_touched_same_bar"


def test_replay_bracket_unavailable_no_bars():
    r = replay_bracket(100.0, 90.0, 110.0, "long", bars=[])
    assert r["result"] == "unavailable" and r["replay_r"] is None


def test_replay_bracket_unavailable_no_levels():
    r = replay_bracket(100.0, None, 110.0, "long", bars=[{"high": 105, "low": 99}])
    assert r["result"] == "unavailable"
    assert replay_bracket(None, 90.0, 110.0, "long", bars=[{"high": 105, "low": 99}])["result"] == "unavailable"


def test_replay_bracket_stop_breach_checked_before_later_target_breach():
    """Stop hit on an EARLIER bar must win even if target breaches on a later
    bar — the replay must stop at the first breach chronologically."""
    bars = [
        {"date": "d1", "high": 101, "low": 88, "close": 90},    # stop hit here
        {"date": "d2", "high": 130, "low": 95, "close": 125},   # target would hit here, but too late
    ]
    r = replay_bracket(entry=100.0, stop=90.0, target=110.0, direction="long", bars=bars)
    assert r["result"] == "stop_hit"
