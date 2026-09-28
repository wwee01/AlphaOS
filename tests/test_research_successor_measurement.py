from copy import deepcopy

import pytest

from alphaos.research.successor_measurement import reconstruct_bracket
from alphaos.learning.outcomes_engine import forward_window_stats


def fixture():
    plan = {
        "symbol": "AAA",
        "direction": "long",
        "entry": 100.0,
        "stop": 90.0,
        "target": 120.0,
        "decision_at_utc": "2026-06-30T15:00:00Z",
    }
    bars = [
        {
            "bar_id": d,
            "symbol": "AAA",
            "market_date": d,
            "open": 100.0,
            "high": 110.0,
            "low": 98.0,
            "close": 103.0,
            "source_feed": "saved-test",
            "created_at_utc": "2026-07-07T01:00:00Z",
        }
        for d in ["2026-07-01", "2026-07-02", "2026-07-06"]
    ]
    return plan, bars


def test_three_session_bracket_is_not_three_session_endpoint_when_stop_hits():
    p, bars = fixture()
    bars[0]["low"] = 89.0
    r = reconstruct_bracket(p, bars, 3, "2026-07-08T00:00:00Z")
    assert r["value"] == -1.0 and r["result"] == "stop_hit"
    assert r["exit_session"] == "2026-07-01"
    assert r["dependence_end_utc"] == "2026-07-01T20:00:00+00:00"
    assert r["scheduled_dates"] == ["2026-07-01", "2026-07-02", "2026-07-06"]
    old_proxy = forward_window_stats(100., 90., "long", bars, 3)["r"]
    assert old_proxy == 0.3 and r["value"] != old_proxy


def test_missing_later_session_does_not_select_only_early_resolutions():
    p, bars = fixture()
    bars[0]["high"] = 121.0
    r = reconstruct_bracket(p, bars[:1], 3, "2026-07-08T00:00:00Z")
    assert r["status"] == "blocked" and r["reason"] == "missing_scheduled_bars"


@pytest.mark.parametrize(
    "case,reason",
    [
        ("duplicate", "duplicate_bar_date"),
        ("bad_ohlc", "incoherent_bar_prices"),
        ("future", "bar_recorded_after_analysis"),
        ("levels", "invalid_direction_or_level_order"),
        ("ambiguous", "ambiguous_or_unavailable_replay"),
        ("horizon", "unfixed_or_invalid_horizon"),
        ("nonfinite", "missing_or_invalid_levels"),
    ],
)
def test_malformed_source_never_certified(case, reason):
    p, bars = fixture()
    horizon = 3
    if case == "duplicate":
        bars.append(deepcopy(bars[0]))
    elif case == "bad_ohlc":
        bars[0]["close"] = 200.0
    elif case == "future":
        bars[0]["created_at_utc"] = "2027-01-01T00:00:00Z"
    elif case == "levels":
        p["stop"] = 105.0
    elif case == "ambiguous":
        bars[0].update(low=89.0, high=121.0)
    elif case == "horizon":
        horizon = None
    else:
        p["entry"] = float("nan")
    # Noncanonical prices must be rejected before an artifact hash is claimed.
    if case == "nonfinite":
        with pytest.raises(ValueError):
            reconstruct_bracket(p, bars, horizon, "2026-07-08T00:00:00Z")
    else:
        assert (
            reconstruct_bracket(p, bars, horizon, "2026-07-08T00:00:00Z")["reason"]
            == reason
        )


def test_ten_session_policy_cannot_finish_at_five_or_enter_same_ET_date():
    p, bars = fixture()
    p["decision_at_utc"] = "2026-07-01T00:30:00Z"  # June30 ET
    r = reconstruct_bracket(p, bars, 10, "2026-07-08T00:00:00Z")
    assert r["reason"] == "horizon_not_mature"
    assert r["scheduled_dates"][0] == "2026-07-01"
    assert len(r["scheduled_dates"]) == 10


def test_cash_is_explicit_zero_over_same_declared_horizon():
    p, _ = fixture()
    p.update(decision="no_action", entry=None, stop=None, target=None)
    r = reconstruct_bracket(p, [], 3, "2026-07-08T00:00:00Z")
    assert r["status"] == "complete" and r["value"] == 0.0
    assert r["scheduled_end_bound_utc"] == r["dependence_end_utc"]


def test_same_day_partial_bar_receipt_is_not_a_completed_daily_bar():
    p, bars = fixture()
    bars[0]["created_at_utc"] = "2026-07-01T15:00:00Z"
    assert reconstruct_bracket(p, bars, 3, "2026-07-08T00:00:00Z")["reason"] == "bar_first_recorded_before_completed_session"


def test_short_barrier_and_mixed_feeds_are_explicit():
    p, bars = fixture()
    p.update(direction="short", stop=120., target=90.)
    bars[1]["low"] = 89.
    r = reconstruct_bracket(p, bars, 3, "2026-07-08T00:00:00Z")
    assert r["value"] == .5 and r["exit_session"] == "2026-07-02"
    bars[-1]["source_feed"] = "different"
    assert reconstruct_bracket(p, bars, 3, "2026-07-08T00:00:00Z")["reason"] == "mixed_bar_feeds"
