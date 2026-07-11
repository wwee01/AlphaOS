"""NYSE market holiday calendar (full-closure days only).

Self-contained, stdlib-only computation (no network call, no bundled data
file, no new dependency -- matches this codebase's stated "core runs on the
standard library alone" policy, see requirements.txt). The ten holidays NYSE
observes as full closures are either a fixed calendar date (with an
"observed" weekend-shift rule) or a floating Nth-weekday-of-month rule; only
Good Friday needs a year's Easter date, computed here via the standard
Anonymous Gregorian / Meeus algorithm.

Half-days (early 1pm ET close, e.g. the day after Thanksgiving) are NOT
covered -- this module answers "is the market fully closed today", not
"what time does it close today". See ``timeutils.market_session``'s
docstring for that scope boundary.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

# NYSE recognized Juneteenth as a market holiday starting in 2022; computing
# it for earlier years would misclassify real historical trading days.
_JUNETEENTH_FIRST_YEAR = 2022


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    """The nth occurrence of ``weekday`` (Mon=0..Sun=6) in ``month``/``year``.

    ``n=1`` for the 1st occurrence, ``n=-1`` for the LAST occurrence in the
    month (Memorial Day is "last Monday of May", not a fixed nth)."""
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))
        return d
    if month == 12:
        last_day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, month + 1, 1) - timedelta(days=1)
    return last_day - timedelta(days=(last_day.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    """Anonymous Gregorian algorithm (Meeus/Jones/Butcher) -- the standard
    stdlib-only way to compute the Gregorian Easter date for any year."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ll = (32 + 2 * e + 2 * i - h - k) % 7  # standard algorithm's "L" (avoids ambiguous `l`)
    m = (a + 11 * h + 22 * ll) // 451
    month, day = divmod(h + ll - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(d: date, *, shift_saturday: bool = True) -> date:
    """A Saturday holiday is observed the preceding Friday, a Sunday holiday
    the following Monday -- standard NYSE rule for every fixed-date holiday
    EXCEPT New Year's Day (see ``shift_saturday``)."""
    if d.weekday() == 5 and shift_saturday:  # Saturday
        return d - timedelta(days=1)
    if d.weekday() == 6:  # Sunday
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=64)
def us_market_holidays(year: int) -> frozenset[date]:
    """Every NYSE full-closure holiday date observed in ``year``.

    Pure computation, cached per year (called from a scheduler tick every
    few minutes -- lru_cache avoids re-deriving Easter/nth-weekday math on
    every call)."""
    holidays = set()

    # New Year's Day: Sunday -> observed Monday, but a Saturday New Year's
    # Day is NOT observed the preceding Friday (that would close the market
    # on the last trading day of the prior year, which NYSE does not do --
    # e.g. Jan 1, 2022 fell on a Saturday and NYSE traded normally on
    # Fri Dec 31, 2021).
    new_years = date(year, 1, 1)
    if new_years.weekday() != 5:
        holidays.add(_observed(new_years))

    holidays.add(_nth_weekday_of_month(year, 1, 0, 3))    # MLK Day: 3rd Mon of Jan
    holidays.add(_nth_weekday_of_month(year, 2, 0, 3))    # Presidents Day: 3rd Mon of Feb
    holidays.add(_easter_sunday(year) - timedelta(days=2))  # Good Friday
    holidays.add(_nth_weekday_of_month(year, 5, 0, -1))   # Memorial Day: last Mon of May

    if year >= _JUNETEENTH_FIRST_YEAR:
        holidays.add(_observed(date(year, 6, 19)))        # Juneteenth

    holidays.add(_observed(date(year, 7, 4)))             # Independence Day
    holidays.add(_nth_weekday_of_month(year, 9, 0, 1))    # Labor Day: 1st Mon of Sep
    holidays.add(_nth_weekday_of_month(year, 11, 3, 4))   # Thanksgiving: 4th Thu of Nov
    holidays.add(_observed(date(year, 12, 25)))           # Christmas Day

    return frozenset(holidays)


def is_us_market_holiday(d: date) -> bool:
    return d in us_market_holidays(d.year)


def is_trading_day(d: date) -> bool:
    """Weekend OR NYSE full-closure holiday -> False. Does not account for
    a mid-day/early-close (half day) -- those remain trading days here."""
    if d.weekday() >= 5:  # Sat/Sun
        return False
    return not is_us_market_holiday(d)
