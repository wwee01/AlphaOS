"""Time helpers.

Every AlphaOS event is stamped in UTC, Asia/Singapore local, and the US market
timezone (America/New_York). We centralize that here so timestamps are
consistent and so market-session classification has a single definition.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, time, timezone, date
from typing import Optional

try:  # zoneinfo is stdlib on 3.9+; tzdata package backs it on minimal systems.
    from zoneinfo import ZoneInfo

    _UTC = ZoneInfo("UTC")
    _SGT = ZoneInfo("Asia/Singapore")
    _ET = ZoneInfo("America/New_York")
    _TZ_OK = True
except Exception:  # pragma: no cover - extreme fallback
    _UTC = timezone.utc
    _SGT = timezone.utc
    _ET = timezone.utc
    _TZ_OK = False


from alphaos.constants import MarketSession


@dataclass(frozen=True)
class Stamp:
    """A single instant expressed in the three timezones AlphaOS cares about."""

    utc: str           # ISO-8601, e.g. 2026-06-21T13:30:00+00:00
    local_sgt: str     # Asia/Singapore
    market_et: str     # America/New_York

    def as_dict(self) -> dict:
        return asdict(self)


def now_utc() -> datetime:
    return datetime.now(tz=_UTC)


def to_iso(dt: datetime) -> str:
    """ISO-8601 string, always timezone-aware (assumes UTC if naive)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC)
    return dt.isoformat()


def to_et(dt: datetime) -> datetime:
    """``dt`` converted to America/New_York (assumes UTC if naive) -- the
    single definition of "market time" every session/curve computation
    should share, matching ``market_session()``'s own conversion."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC)
    return dt.astimezone(_ET)


def stamp(dt: Optional[datetime] = None) -> Stamp:
    """Build a three-timezone Stamp for ``dt`` (defaults to now)."""
    if dt is None:
        dt = now_utc()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC)
    return Stamp(
        utc=dt.astimezone(_UTC).isoformat(),
        local_sgt=dt.astimezone(_SGT).isoformat(),
        market_et=dt.astimezone(_ET).isoformat(),
    )


def parse_iso(value: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp; returns None on failure (defensive)."""
    if not value:
        return None
    try:
        v = value.strip()
        # Support trailing 'Z'.
        if v.endswith("Z"):
            v = v[:-1] + "+00:00"
        # Alpaca emits RFC3339 with nanoseconds; datetime.fromisoformat only
        # accepts up to microseconds, so truncate the fractional part to 6 digits.
        import re as _re

        m = _re.match(r"^(.*\.\d{6})\d+(.*)$", v)
        if m:
            v = m.group(1) + m.group(2)
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_UTC)
        return dt
    except (ValueError, TypeError):
        return None


def age_seconds(source_timestamp: str, reference: Optional[datetime] = None) -> Optional[float]:
    """Seconds elapsed between ``source_timestamp`` and ``reference`` (now if None).

    Returns None when the source timestamp cannot be parsed — callers must treat
    that as 'unverifiable', never as 'fresh'.
    """
    src = parse_iso(source_timestamp)
    if src is None:
        return None
    ref = reference or now_utc()
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=_UTC)
    return (ref - src).total_seconds()


# Regular US equity session in market-local time.
_REG_OPEN = time(9, 30)
_REG_CLOSE = time(16, 0)
_PRE_OPEN = time(4, 0)
_AFT_CLOSE = time(20, 0)


def market_session(dt: Optional[datetime] = None) -> MarketSession:
    """Classify the US market session for an instant (weekends => CLOSED).

    This is a calendar-naive approximation (no holiday table in v1); holidays
    are a known gap, surfaced in the README.
    """
    if dt is None:
        dt = now_utc()
    et = dt.astimezone(_ET)
    if et.weekday() >= 5:  # Sat/Sun
        return MarketSession.CLOSED
    t = et.time()
    if _PRE_OPEN <= t < _REG_OPEN:
        return MarketSession.PREMARKET
    if _REG_OPEN <= t < _REG_CLOSE:
        return MarketSession.REGULAR
    if _REG_CLOSE <= t < _AFT_CLOSE:
        return MarketSession.AFTERHOURS
    return MarketSession.CLOSED


def market_date(dt: Optional[datetime] = None) -> date:
    """The US-market calendar date for an instant (used for same-day logic)."""
    if dt is None:
        dt = now_utc()
    return dt.astimezone(_ET).date()


def tz_available() -> bool:
    return _TZ_OK
