"""NYSE market holiday calendar (operator request, 2026-07-11): AlphaOS ran a
full scan+digest cadence on a real Saturday with zero day-of-week awareness --
the digest fired normally as if trading had happened. Covers:

* us_market_holidays()/is_trading_day() against REAL, independently-known
  NYSE holiday dates across multiple years (spot-checked against the actual
  published NYSE calendar, not just re-deriving the same formula back).
* The New Year's Day Saturday exception (NYSE does NOT observe it the
  preceding Friday, unlike every other fixed-date holiday -- e.g. 2022).
* The Juneteenth pre-2022 exclusion (not a NYSE holiday before 2022).
* timeutils.market_session() now returns CLOSED on a holiday, not just a
  weekend.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from alphaos.constants import MarketSession
from alphaos.util import timeutils
from alphaos.util.market_calendar import (
    is_trading_day,
    is_us_market_holiday,
    nth_trading_day_after,
    trading_days_between,
    us_market_holidays,
)


# --------------------------------------------------------------- known dates
def test_2026_fixed_and_floating_holidays_match_the_real_published_calendar():
    holidays = us_market_holidays(2026)
    expected = {
        date(2026, 1, 1),    # New Year's Day (Thursday, no shift)
        date(2026, 1, 19),   # MLK Day (3rd Monday of Jan)
        date(2026, 2, 16),   # Presidents Day (3rd Monday of Feb)
        date(2026, 4, 3),    # Good Friday (Easter 2026 = Apr 5)
        date(2026, 5, 25),   # Memorial Day (last Monday of May)
        date(2026, 6, 19),   # Juneteenth (Friday, no shift)
        date(2026, 7, 3),    # Independence Day OBSERVED (Jul 4 is a Saturday)
        date(2026, 9, 7),    # Labor Day (1st Monday of Sep)
        date(2026, 11, 26),  # Thanksgiving (4th Thursday of Nov)
        date(2026, 12, 25),  # Christmas Day (Friday, no shift)
    }
    assert holidays == expected


def test_2022_new_years_day_saturday_is_not_observed_the_preceding_friday():
    """Jan 1, 2022 fell on a Saturday. Every OTHER fixed-date holiday shifts
    a Saturday occurrence to the preceding Friday -- New Year's Day is the
    one documented exception (closing the market on the last trading day of
    the prior year is not NYSE convention). Dec 31, 2021 was a normal
    trading day; Jan 1, 2022 itself is also not a holiday entry (a specific
    date isn't a trading day anyway since it's a Saturday, but it must not
    additionally appear in the set, and Dec 31 must not appear either)."""
    holidays_2022 = us_market_holidays(2022)
    # The phantom entry a removed exception would produce lands in the
    # 2022 set (computed as new_years - 1 day while year=2022), NOT the
    # 2021 set -- asserting against 2021 here would pass regardless of
    # whether the exception exists, silently failing to guard it.
    assert date(2021, 12, 31) not in holidays_2022
    assert date(2022, 1, 1) not in holidays_2022
    # Every OTHER 2022 fixed-date holiday that falls on a weekend still
    # shifts normally -- Juneteenth (Jun 19, 2022) was a Sunday, observed
    # Monday Jun 20; Christmas (Dec 25, 2022) was a Sunday, observed Monday
    # Dec 26.
    assert date(2022, 6, 20) in holidays_2022
    assert date(2022, 12, 26) in holidays_2022


def test_juneteenth_is_not_a_holiday_before_its_2022_nyse_start_year():
    assert date(2020, 6, 19) not in us_market_holidays(2020)
    assert date(2021, 6, 19) not in us_market_holidays(2021)
    assert date(2022, 6, 20) in us_market_holidays(2022)  # first year, Sunday-observed-Monday


def test_saturday_and_sunday_holidays_shift_to_the_correct_weekday():
    # July 4, 2020 was a Saturday -> observed Friday Jul 3.
    assert date(2020, 7, 3) in us_market_holidays(2020)
    assert date(2020, 7, 4) not in us_market_holidays(2020)
    # July 4, 2021 was a Sunday -> observed Monday Jul 5.
    assert date(2021, 7, 5) in us_market_holidays(2021)
    assert date(2021, 7, 4) not in us_market_holidays(2021)


# ------------------------------------------------------------- is_trading_day
def test_is_trading_day_false_on_a_real_saturday():
    """The exact date + bug the operator reported: 2026-07-11 is a Saturday."""
    assert is_trading_day(date(2026, 7, 11)) is False


def test_is_trading_day_false_on_a_real_sunday():
    assert is_trading_day(date(2026, 7, 12)) is False


def test_is_trading_day_true_on_an_ordinary_weekday():
    assert is_trading_day(date(2026, 7, 13)) is True  # a Monday, no holiday


def test_is_trading_day_false_on_a_weekday_holiday():
    """Christmas Day 2026 falls on a Friday -- a weekday, but still not a
    trading day. This is the case a weekday-only check would miss."""
    assert is_trading_day(date(2026, 12, 25)) is False
    assert is_us_market_holiday(date(2026, 12, 25)) is True


def test_is_trading_day_true_the_day_before_and_after_a_holiday():
    assert is_trading_day(date(2026, 12, 24)) is True
    assert is_trading_day(date(2026, 12, 28)) is True  # next Monday after the holiday+weekend


# ------------------------------------ trading_days_between / nth_trading_day_after (HOLD-1)
def test_trading_days_between_the_thursday_entry_example():
    """The exact HOLD-1 walkthrough: entered Thursday 2026-07-09, no holiday
    in the window. Fri=1, Sat/Sun stay at 1 (not trading days), Mon=2, Tue=3
    -- so a max_days=3 position expires Tuesday, never over the weekend."""
    entered = date(2026, 7, 9)
    assert trading_days_between(entered, date(2026, 7, 10)) == 1  # Fri
    assert trading_days_between(entered, date(2026, 7, 11)) == 1  # Sat (unchanged)
    assert trading_days_between(entered, date(2026, 7, 12)) == 1  # Sun (unchanged)
    assert trading_days_between(entered, date(2026, 7, 13)) == 2  # Mon
    assert trading_days_between(entered, date(2026, 7, 14)) == 3  # Tue


def test_trading_days_between_same_date_is_zero():
    d = date(2026, 7, 9)
    assert trading_days_between(d, d) == 0


def test_trading_days_between_end_before_start_is_zero():
    assert trading_days_between(date(2026, 7, 9), date(2026, 7, 1)) == 0


def test_trading_days_between_a_holiday_inside_the_window_extends_the_count():
    """A holiday inside the window doesn't advance the count -- Good Friday
    2026 is April 3rd. Entry Wednesday April 1: Thu Apr 2=1, Good Friday Apr
    3 stays at 1 (not a trading day), Sat/Sun stay at 1, Monday Apr 6=2."""
    entered = date(2026, 4, 1)
    assert is_trading_day(date(2026, 4, 3)) is False  # Good Friday
    assert trading_days_between(entered, date(2026, 4, 2)) == 1  # Thu
    assert trading_days_between(entered, date(2026, 4, 3)) == 1  # Good Friday, no advance
    assert trading_days_between(entered, date(2026, 4, 4)) == 1  # Sat, no advance
    assert trading_days_between(entered, date(2026, 4, 6)) == 2  # Monday


def test_nth_trading_day_after_matches_trading_days_between_inverse():
    entered = date(2026, 7, 9)
    for n in (1, 2, 3, 5):
        d = nth_trading_day_after(entered, n)
        assert trading_days_between(entered, d) == n


def test_nth_trading_day_after_skips_weekend():
    assert nth_trading_day_after(date(2026, 7, 9), 1) == date(2026, 7, 10)  # Fri
    assert nth_trading_day_after(date(2026, 7, 9), 2) == date(2026, 7, 13)  # Mon, skips Sat/Sun


def test_nth_trading_day_after_rejects_non_positive_n():
    with pytest.raises(ValueError):
        nth_trading_day_after(date(2026, 7, 9), 0)


# --------------------------------------------------------- market_session()
def test_market_session_closed_on_a_holiday_during_what_would_be_regular_hours():
    """Before this fix, market_session() was weekend-aware only -- a holiday
    at a normal regular-hours instant would misclassify as REGULAR."""
    # Dec 25, 2026, 11:00 ET (EST = UTC-5 in December) -- would be REGULAR
    # hours on an ordinary weekday.
    dt = datetime(2026, 12, 25, 16, 0, tzinfo=timezone.utc)
    assert timeutils.market_session(dt) == MarketSession.CLOSED


def test_market_session_still_regular_on_an_ordinary_trading_day():
    # Dec 24, 2026, 11:00 ET (EST = UTC-5) -- an ordinary weekday, regular hours.
    dt = datetime(2026, 12, 24, 16, 0, tzinfo=timezone.utc)
    assert timeutils.market_session(dt) == MarketSession.REGULAR


def test_market_session_still_closed_on_a_weekend_unchanged_from_before():
    # Confirms the pre-existing weekend behavior wasn't disturbed by adding
    # the holiday check.
    dt = datetime(2026, 7, 11, 16, 0, tzinfo=timezone.utc)  # Saturday
    assert timeutils.market_session(dt) == MarketSession.CLOSED
