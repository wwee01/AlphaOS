"""Constructed calendar/price fixtures; no provider or ledger calls."""
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import date, datetime, timedelta

import pytest

from alphaos.learning.close_ref_engine import (
    CalendarPending, calendar_receipt, combine_legs, observe_leg, resolve_window, retry_until,
)
from alphaos.util.market_calendar import is_trading_day


def stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def calendar_rows(start="2026-11-16", end="2026-12-18"):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    return [{"date": (day := first + timedelta(days=i)).isoformat(), "open": "09:30",
             "close": "13:00" if day == date(2026, 11, 27) else "16:00"}
            for i in range((last - first).days + 1) if is_trading_day(first + timedelta(days=i))]


def receipt(rows=None, start="2026-11-16", end="2026-12-18"):
    return calendar_receipt(calendar_rows(start, end) if rows is None else rows,
                            provider="fixture_calendar", start=start, end=end,
                            requested_at=stamp("2026-11-18T12:00:00Z"),
                            fetched_at=stamp("2026-11-18T12:00:01Z"))


def window(decision="2026-11-16T14:35:00Z"):
    return resolve_window(stamp(decision), receipt())


def leg(w=None, horizon=5, symbol="ABC", bars=None, **kwargs):
    w = w or window()
    defaults = dict(provider="alpaca", feed="iex", adjustment="raw",
                    requested_at=w.endpoint(horizon).close + timedelta(seconds=1),
                    fetched_at=w.endpoint(horizon).close + timedelta(seconds=2),
                    now=w.endpoint(horizon).close + timedelta(seconds=2))
    defaults.update(kwargs)
    if bars is None:
        bars = [{"date": w.reference.date, "close": 100.0},
                {"date": w.endpoint(horizon).date, "close": 110.0}]
    return observe_leg(w, horizon, symbol, bars, **defaults)


def test_contract_example_counts_scheduled_sessions_not_bars():
    w = window()
    assert (w.reference.date, w.endpoint_5.date, w.endpoint_10.date) == (
        "2026-11-16", "2026-11-23", "2026-12-01")
    assert leg(w).return_fraction == pytest.approx(.1)
    assert leg(w, 10).return_fraction == pytest.approx(.1)


@pytest.mark.parametrize("decision,reference", [
    ("2026-11-16T20:59:59.999999Z", "2026-11-16"),
    ("2026-11-16T21:00:00Z", "2026-11-17"),
    ("2026-11-16T21:00:00.000001Z", "2026-11-17"),
    ("2026-11-21T15:00:00Z", "2026-11-23"),
    ("2026-11-26T15:00:00Z", "2026-11-27"),
    ("2026-11-27T17:59:59Z", "2026-11-27"),
    ("2026-11-27T18:00:00Z", "2026-11-30"),
    ("2026-11-27T13:00:00-05:00", "2026-11-30"),
])
def test_strict_actual_close_and_non_sessions(decision, reference):
    assert window(decision).reference.date == reference


def test_early_close_counts_as_session_and_matures_at_actual_close():
    w = window("2026-11-19T15:00:00Z")
    assert w.endpoint_5.date == "2026-11-27"
    assert w.endpoint_5.close == stamp("2026-11-27T18:00:00Z")
    assert leg(w).status == "complete"


def test_calendar_is_owned_and_hash_includes_actual_hours():
    rows = calendar_rows()
    r = receipt(rows)
    before = r.sha256
    rows[0]["close"] = "13:00"
    assert r.sessions[0].close == stamp("2026-11-16T21:00:00Z")
    assert r.sha256 == before
    assert receipt(rows).sha256 != before
    with pytest.raises(FrozenInstanceError):
        r.provider = "changed"


@pytest.mark.parametrize("mutation", ["gap", "duplicate", "bad_hours", "weekend", "bad_date"])
def test_calendar_refuses_missing_interior_and_invalid_rows(mutation):
    rows = calendar_rows()
    if mutation == "gap":
        rows.pop(2)
    elif mutation == "duplicate":
        rows.append(dict(rows[0]))
    elif mutation == "bad_hours":
        rows[0]["close"] = "09:29"
    elif mutation == "weekend":
        rows.append({"date": "2026-11-21", "open": "09:30", "close": "16:00"})
    else:
        rows[0]["date"] = "2026-11-16T00:00:00Z"
    with pytest.raises(CalendarPending):
        receipt(rows)


def test_insufficient_calendar_never_uses_static_hours_or_future_guess():
    with pytest.raises(CalendarPending, match="forward_sessions"):
        resolve_window(stamp("2026-11-16T15:00:00Z"), receipt(start="2026-11-16", end="2026-11-20"))
    with pytest.raises(CalendarPending, match="outside"):
        resolve_window(stamp("2026-11-13T15:00:00Z"), receipt())
    with pytest.raises(ValueError, match="aware"):
        resolve_window(datetime(2026, 11, 16, 15), receipt())


@pytest.mark.parametrize("horizon", [0, 1, 4, 6, 9, True, 5.0])
def test_only_exact_five_and_ten_session_horizons(horizon):
    with pytest.raises(ValueError):
        leg(horizon=horizon)


@pytest.mark.parametrize("value", [None, 0, -1, True, "100", float("nan"), float("inf"), 10**1000])
def test_invalid_prices_remain_pending(value):
    result = leg(bars=[{"date": "2026-11-16", "close": value},
                       {"date": "2026-11-23", "close": 110}])
    assert result.status == "pending"
    assert result.return_fraction is None


def test_no_nearest_date_or_partial_bar_count_substitution():
    result = leg(bars=[{"date": "2026-11-16", "close": 100},
                       {"date": "2026-11-20", "close": 110},
                       {"date": "2026-11-24", "close": 120}])
    assert result.reason == "exact_endpoint_bar_missing"
    assert result.status == "pending"


def test_duplicate_endpoint_does_not_pick_last_value():
    result = leg(bars=[{"date": "2026-11-16", "close": 100},
                       {"date": "2026-11-23", "close": 110},
                       {"date": "2026-11-23", "close": 111}])
    assert result.reason == "duplicate_endpoint_bar"


def test_request_before_close_cannot_be_rescued_by_late_response():
    w = window()
    result = leg(w, requested_at=w.endpoint_5.close - timedelta(microseconds=1))
    assert result.status == "pending"
    assert result.reason == "endpoint_not_closed_at_request"


def test_retry_ten_calendar_days_then_unavailable_even_if_late_prices_arrive():
    w = window()
    deadline = retry_until(w.endpoint_5)
    assert deadline == stamp("2026-12-03T21:00:00Z")
    assert leg(w, bars=[], now=deadline).status == "pending"
    late = deadline + timedelta(microseconds=1)
    result = leg(w, now=late, requested_at=late, fetched_at=late)
    assert result.status == "unavailable"
    assert result.reason == "retry_window_expired"
    assert result.return_fraction is None


def test_spy_is_independent_and_unsigned_not_direction_signed():
    asset = leg()
    benchmark = leg(symbol="SPY", bars=[])
    saved = asdict(asset)
    pair = combine_legs(asset, benchmark)
    assert pair["status"] == "pending" and pair["benchmark_status"] == "pending"
    assert asdict(asset) == saved
    benchmark = leg(symbol="SPY", bars=[{"date": "2026-11-16", "close": 100},
                                       {"date": "2026-11-23", "close": 98}])
    pair = combine_legs(asset, benchmark)
    assert pair["r"] == pytest.approx(.1)
    assert pair["b"] == pytest.approx(-.02)
    assert pair["status"] == "complete"


@pytest.mark.parametrize("field,value", [
    ("symbol", "QQQ"), ("feed", "sip"), ("provider", "other"),
    ("adjustment", "all"), ("reference_date", "2026-11-17"),
    ("endpoint_date", "2026-11-24"), ("horizon", 10), ("calendar_sha256", "different"),
])
def test_pair_requires_identical_window_and_price_basis(field, value):
    with pytest.raises(ValueError, match="conventions differ"):
        combine_legs(leg(), replace(leg(symbol="SPY"), **{field: value}))


def test_timestamp_chronology_and_finite_complete_contract():
    with pytest.raises(ValueError, match="chronology"):
        leg(fetched_at=stamp("2026-11-23T20:00:00Z"))
    with pytest.raises(ValueError, match="finite"):
        combine_legs(replace(leg(), return_fraction=float("nan")), leg(symbol="SPY"))


@pytest.mark.parametrize("field,value", [("reference_close", None), ("endpoint_close", None),
                                         ("reference_close", 0.0), ("endpoint_close", float("inf")),
                                         ("return_fraction", .9)])
def test_constructed_complete_leg_must_retain_actual_prices(field, value):
    with pytest.raises(ValueError):
        combine_legs(replace(leg(), **{field: value}), leg(symbol="SPY"))


def test_terminal_benchmark_unavailable_does_not_make_pair_pending_forever():
    asset = leg()
    benchmark = leg(symbol="SPY", bars=[], now=retry_until(window().endpoint_5) + timedelta(seconds=1))
    pair = combine_legs(asset, benchmark)
    assert pair["status"] == "unavailable"
    assert pair["candidate_status"] == "complete"
    assert pair["benchmark_status"] == "unavailable"
    assert asset.return_fraction == pytest.approx(.1)


def test_separate_retries_reuse_frozen_window_not_new_calendar_fetch_identity():
    w = window()
    asset = leg(w)
    benchmark = leg(w, symbol="SPY", requested_at=w.endpoint_5.close + timedelta(days=1),
                    fetched_at=w.endpoint_5.close + timedelta(days=1, seconds=1),
                    now=w.endpoint_5.close + timedelta(days=1, seconds=1))
    assert combine_legs(asset, benchmark)["status"] == "complete"
    later_calendar = replace(w.calendar, fetched_at_utc="2026-11-19T12:00:01+00:00")
    changed_window = replace(w, calendar=later_calendar)
    with pytest.raises(ValueError, match="conventions"):
        combine_legs(asset, leg(changed_window, symbol="SPY"))
