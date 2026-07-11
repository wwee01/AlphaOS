"""HOLD-1 (operator-reported, 2026-07-12): trading-day holding-period
semantics. ``max_holding_days`` was enforced in CALENDAR days
(``PositionManager._check_exit``) while the replay engine that gives the
number its meaning (``outcomes_engine.replay_bracket``) has always indexed
over TRADING-day bars -- a 3-day swing entered Thursday could force-expire
Sunday against a stale weekend price, and the same gap biased attribution ΔR
against live holds. Ruling: ``max_holding_days`` now means TRADING days,
matching the replay engine.

Covers:
* the Thursday-entry walkthrough (no expiry over the weekend, expiry lands
  on the correct trading day)
* the weekend/holiday fake-fill guard, independent of the trading-day count
* a holiday inside the hold window extending the exit correctly
* ``holding_trading_days`` journaled additively at close, ``holding_days``
  (calendar) left exactly as before
* live enforcement and the replay engine's own bar-offset convention
  resolving to the IDENTICAL expiry session for the same synthetic calendar

All datetimes are fixed/injected (``timeutils.now_utc`` monkeypatched) --
never wall-clock -- so this suite is green regardless of the day it runs on.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from alphaos.execution.position_manager import PositionManager
from alphaos.learning.outcomes_engine import replay_bracket
from alphaos.util import timeutils
from alphaos.util.ids import new_id
from alphaos.util.market_calendar import (
    is_trading_day,
    nth_trading_day_after,
    trading_days_between,
)
from conftest import make_settings

# 17:00 UTC lands mid-day ET regardless of EST/EDT (13:00 EDT or 12:00 EST) --
# safely inside the same ET calendar date as the UTC date for every fixture
# below, so date arithmetic on the UTC date and the ET date always agree.
_MIDDAY_UTC = timezone.utc


def _utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 17, 0, 0, tzinfo=_MIDDAY_UTC)


def _freeze(monkeypatch, d: date) -> None:
    monkeypatch.setattr(timeutils, "now_utc", lambda: _utc(d))


def _order_row(**overrides):
    row = {
        "order_id": new_id("ord"), "symbol": "NVDA", "direction": "long", "qty": 10.0,
        "strategy": "swing", "stop_loss_price": 50.0, "take_profit_price": 300.0,
        "execution_source": "simulated_internal", "is_short": 0, "is_demo": 0,
        "trade_id": new_id("trade"), "proposal_id": None,
    }
    row.update(overrides)
    return row


def _open(journal, monkeypatch, entry_date: date, max_holding_days: int, **overrides):
    """Open a position at a frozen entry instant, then return the freshly
    re-fetched row (so opened_at/opened_market_date reflect the frozen
    time exactly the way a real fill would)."""
    _freeze(monkeypatch, entry_date)
    settings = make_settings(ALPHAOS_MODE="mock")
    pm = PositionManager(settings, journal)
    row = _order_row(**overrides)
    proposal_id = new_id("prop")
    journal.insert("trade_proposals", {
        "proposal_id": proposal_id, "candidate_id": new_id("cand"), "symbol": row["symbol"],
        "direction": row["direction"], "strategy": "swing", "entry": 100.0, "stop": 50.0,
        "target": 300.0, "max_holding_days": max_holding_days, "qty": 10.0,
        "risk_per_share": 50.0, "dollar_risk": 500.0, "expected_r": 4.0,
        "same_day_exit_eligible": 0, "status": "pending_approval",
    })
    row["proposal_id"] = proposal_id
    position_id = pm.open_position(row, 100.0)
    pos = journal.one("SELECT * FROM positions WHERE position_id = ?", (position_id,))
    return pm, pos


# ------------------------------------------------------- the Thursday walkthrough
def test_thursday_entry_max_3_no_expiry_over_the_weekend_expires_tuesday(journal, monkeypatch):
    entry_date = date(2026, 7, 9)  # Thursday, no holiday nearby
    _, pos = _open(journal, monkeypatch, entry_date, max_holding_days=3)

    # Fri/Sat/Sun/Mon: trading_days_between is 1, 1, 1, 2 -- never >= 3.
    for label, d in (("Fri", date(2026, 7, 10)), ("Sat", date(2026, 7, 11)),
                     ("Sun", date(2026, 7, 12)), ("Mon", date(2026, 7, 13))):
        _freeze(monkeypatch, d)
        pm2 = PositionManager(make_settings(ALPHAOS_MODE="mock"), journal)
        assert pm2._check_exit(pos, 150.0) is None, f"unexpected time_expiry on {label} ({d})"

    # Tuesday: trading_days_between == 3 >= max_days, and Tuesday IS a trading day.
    _freeze(monkeypatch, date(2026, 7, 14))
    pm3 = PositionManager(make_settings(ALPHAOS_MODE="mock"), journal)
    assert pm3._check_exit(pos, 150.0) == "time_expiry"


# ------------------------------------------------------- weekend/holiday fake-fill guard
def test_time_expiry_never_fires_on_a_saturday_even_when_the_count_is_already_past_max(journal, monkeypatch):
    """Entry Monday, max_days=1: by Tuesday trading_days_between is already 1
    (>= max_days) and stays >= 1 forever after. The following Saturday the
    count is well past max_days, yet no time_expiry may fire -- guard (a)
    (is_trading_day(now)) must independently block it, not just the count."""
    entry_date = date(2026, 7, 6)  # Monday
    _, pos = _open(journal, monkeypatch, entry_date, max_holding_days=1)

    saturday = date(2026, 7, 11)
    assert trading_days_between(entry_date, saturday) >= 1  # count condition alone would fire
    _freeze(monkeypatch, saturday)
    pm = PositionManager(make_settings(ALPHAOS_MODE="mock"), journal)
    assert pm._check_exit(pos, 150.0) is None


def test_time_expiry_never_fires_on_a_sunday_or_an_nyse_holiday(journal, monkeypatch):
    entry_date = date(2026, 7, 6)  # Monday
    _, pos = _open(journal, monkeypatch, entry_date, max_holding_days=1)

    sunday = date(2026, 7, 12)
    assert trading_days_between(entry_date, sunday) >= 1
    _freeze(monkeypatch, sunday)
    pm = PositionManager(make_settings(ALPHAOS_MODE="mock"), journal)
    assert pm._check_exit(pos, 150.0) is None

    # Independence Day (observed) 2026 -- Friday, Jul 3.
    holiday = date(2026, 7, 3)
    assert is_trading_day(holiday) is False
    entry2 = date(2026, 6, 29)  # Monday before, max_days=1 -> count is well past 1 by the 3rd
    _, pos2 = _open(journal, monkeypatch, entry2, max_holding_days=1)
    assert trading_days_between(entry2, holiday) >= 1
    _freeze(monkeypatch, holiday)
    pm2 = PositionManager(make_settings(ALPHAOS_MODE="mock"), journal)
    assert pm2._check_exit(pos2, 150.0) is None


# ------------------------------------------------------- a holiday extends the window
def test_a_holiday_inside_the_hold_window_extends_the_exit_correctly(journal, monkeypatch):
    """Entry Wednesday before Good Friday (2026-04-03). Without a holiday, a
    3-trading-day hold from a Wednesday would expire the FOLLOWING Monday
    (Thu=1, Fri=2, Mon=3). With Good Friday not counting as a trading day,
    the same hold instead expires Tuesday -- one trading day later."""
    entry_date = date(2026, 4, 1)  # Wednesday
    assert is_trading_day(date(2026, 4, 3)) is False  # Good Friday
    _, pos = _open(journal, monkeypatch, entry_date, max_holding_days=3)

    # Monday Apr 6: would have been trading day 3 (and thus expiry) with NO
    # holiday in the window -- must NOT expire here, proving the holiday
    # pushed the count back by one trading day.
    _freeze(monkeypatch, date(2026, 4, 6))
    pm_mon = PositionManager(make_settings(ALPHAOS_MODE="mock"), journal)
    assert pm_mon._check_exit(pos, 150.0) is None

    # Tuesday Apr 7: the real 3rd trading day (Thu=1, Fri holiday=skip, Mon=2, Tue=3).
    _freeze(monkeypatch, date(2026, 4, 7))
    pm_tue = PositionManager(make_settings(ALPHAOS_MODE="mock"), journal)
    assert pm_tue._check_exit(pos, 150.0) == "time_expiry"


# ------------------------------------------------------- journaled additive column
def test_holding_trading_days_journaled_on_close_holding_days_unchanged(journal, monkeypatch):
    entry_date = date(2026, 7, 9)  # Thursday
    pm, pos = _open(journal, monkeypatch, entry_date, max_holding_days=5)

    close_date = date(2026, 7, 14)  # Tuesday -- 3 trading days after entry, 5 calendar days
    _freeze(monkeypatch, close_date)
    pm2 = PositionManager(make_settings(ALPHAOS_MODE="mock"), journal)
    result = pm2.close_position(pos["position_id"], 150.0, "target")
    assert result is not None

    row = journal.one(
        "SELECT holding_days, holding_trading_days FROM trade_outcomes WHERE position_id = ?",
        (pos["position_id"],),
    )
    # calendar: exactly 5.0 days elapsed (both frozen instants are 17:00 UTC).
    assert row["holding_days"] == 5.0
    # trading days: Fri(1)/Mon(2)/Tue(3) -- matches trading_days_between exactly.
    assert row["holding_trading_days"] == 3
    assert row["holding_trading_days"] == trading_days_between(entry_date, close_date)


def test_holding_trading_days_null_when_opened_at_unparseable(journal, monkeypatch):
    """Fails safe like every other unknown-never-zero measurement in this
    codebase: an unparseable/missing entry timestamp yields NULL, never a
    guessed 0."""
    entry_date = date(2026, 7, 9)
    pm, pos = _open(journal, monkeypatch, entry_date, max_holding_days=5)
    journal.conn.execute(
        "UPDATE positions SET opened_at = NULL, opened_market_date = NULL WHERE position_id = ?",
        (pos["position_id"],),
    )
    journal.conn.commit()
    pos = journal.one("SELECT * FROM positions WHERE position_id = ?", (pos["position_id"],))

    _freeze(monkeypatch, date(2026, 7, 14))
    pm2 = PositionManager(make_settings(ALPHAOS_MODE="mock"), journal)
    pm2.close_position(pos["position_id"], 150.0, "target")

    row = journal.one(
        "SELECT holding_days, holding_trading_days FROM trade_outcomes WHERE position_id = ?",
        (pos["position_id"],),
    )
    assert row["holding_days"] is None
    assert row["holding_trading_days"] is None


# ------------------------------------------------------- live vs. replay equivalence
def test_live_rule_and_replay_bar_offset_convention_agree_on_the_same_expiry_session():
    """Cross-check the binding requirement itself: derive the expiry session
    from BOTH the live rule (trading_days_between + is_trading_day) and
    outcomes_engine.replay_bracket's own bar-offset convention (bars
    strictly after the decision day, window[:max_days]) for the SAME
    synthetic calendar, and assert they land on the identical trading date."""
    entry_date = date(2026, 7, 9)  # Thursday
    max_days = 3

    # One daily bar per REAL trading day for the next 10 calendar days after
    # entry -- exactly what a real bars provider would return (no bar for a
    # day the market was never open).
    bars = []
    d = entry_date
    for _ in range(10):
        d += timedelta(days=1)
        if is_trading_day(d):
            bars.append({"date": d.isoformat(), "high": 100.0, "low": 100.0, "close": 100.0})

    # Replay side: stop/target symmetric and far away so NEITHER is ever
    # touched -- forces "neither"/window_exhausted, whose consumed window is
    # exactly bars[:max_days].
    replay = replay_bracket(entry=100.0, stop=50.0, target=150.0, direction="long",
                            bars=bars, max_days=max_days)
    assert replay["result"] == "neither"
    replay_exit_date = date.fromisoformat(bars[max_days - 1]["date"])

    # Live side: the first ET date on which _check_exit's own guard combo
    # would fire.
    live_exit_date = nth_trading_day_after(entry_date, max_days)
    assert is_trading_day(live_exit_date)
    assert trading_days_between(entry_date, live_exit_date) >= max_days

    assert replay_exit_date == live_exit_date


def test_live_position_manager_fires_time_expiry_exactly_on_the_replay_derived_session(journal, monkeypatch):
    """Same cross-check, but driving the REAL PositionManager._check_exit
    instead of the pure calendar helpers -- proves the wiring, not just the
    math."""
    entry_date = date(2026, 7, 9)  # Thursday
    max_days = 3
    _, pos = _open(journal, monkeypatch, entry_date, max_holding_days=max_days)

    live_exit_date = nth_trading_day_after(entry_date, max_days)

    # The day before must NOT expire.
    day_before = live_exit_date - timedelta(days=1)
    _freeze(monkeypatch, day_before)
    pm_before = PositionManager(make_settings(ALPHAOS_MODE="mock"), journal)
    assert pm_before._check_exit(pos, 150.0) is None

    # The replay-derived session itself must expire.
    _freeze(monkeypatch, live_exit_date)
    pm_at = PositionManager(make_settings(ALPHAOS_MODE="mock"), journal)
    assert pm_at._check_exit(pos, 150.0) == "time_expiry"
