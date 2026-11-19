"""Pure, additive close_ref_v1 windows and unsigned price legs.

Campaign windows default to the approved, byte-pinned session table. The
explicit response-calendar constructor is retained for standalone fixtures;
campaign mapping never calls a calendar provider. Legacy signed outcomes are
not inputs, and this module never fetches data or writes outcomes.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from alphaos.reports.close_equity import _calendar_time
from alphaos.util.market_calendar import is_trading_day

METHOD = "close_ref_v1"
_ET = ZoneInfo("America/New_York")
PINNED_CALENDAR_SHA256 = "de9120847a4693f1fc76bac139a21623b34723c9ecb94b77f38a8aca8b38f57c"


class CalendarPending(ValueError):
    """The supplied calendar cannot yet establish the exact window."""


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("an aware timestamp is required")
    return value.astimezone(timezone.utc)


def _date(value: str) -> date:
    if not isinstance(value, str):
        raise ValueError("an ISO session date is required")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("an ISO session date is required")
    return parsed


def _sha(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Session:
    date: str
    open_at_utc: str
    close_at_utc: str

    @property
    def close(self) -> datetime:
        return datetime.fromisoformat(self.close_at_utc)


@dataclass(frozen=True)
class CalendarReceipt:
    provider: str
    requested_start: str
    requested_end: str
    requested_at_utc: str
    fetched_at_utc: str
    sessions: tuple[Session, ...]

    @property
    def sha256(self) -> str:
        return _sha(asdict(self))


@dataclass(frozen=True)
class PinnedCalendar:
    requested_start: str
    requested_end: str
    sessions: tuple[Session, ...]
    sha256: str


def pinned_calendar() -> PinnedCalendar:
    """Read the approved table, refusing any byte change rather than rebinding."""
    raw = Path(__file__).with_name("close_ref_calendar.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != PINNED_CALENDAR_SHA256:
        raise ValueError("pinned_calendar_bytes_changed")
    body = json.loads(raw)
    return PinnedCalendar(body["requested_start"], body["requested_end"],
                          tuple(Session(**row) for row in body["sessions"]),
                          PINNED_CALENDAR_SHA256)


def calendar_receipt(
    rows: list[dict], *, provider: str, start: str, end: str,
    requested_at: datetime, fetched_at: datetime,
) -> CalendarReceipt:
    """Own a complete bounded calendar response, retaining actual close times.

    The existing full-closure date calendar is only a completeness cross-check.
    It never supplies hours. A discrepancy (including an exceptional closure
    not known locally) stays pending for review instead of shifting endpoints.
    """
    first, last = _date(start), _date(end)
    requested, fetched = _utc(requested_at), _utc(fetched_at)
    if not provider or first > last or (last - first).days > 62 or fetched < requested:
        raise ValueError("invalid calendar request")
    if len(rows) > 64:
        raise CalendarPending("calendar_response_too_large")
    expected = {
        (first + timedelta(days=i)).isoformat()
        for i in range((last - first).days + 1)
        if is_trading_day(first + timedelta(days=i))
    }
    sessions: dict[str, Session] = {}
    try:
        for row in rows:
            day = _date(row["date"]).isoformat()
            if day in sessions or not first.isoformat() <= day <= last.isoformat():
                raise ValueError("duplicate or out-of-range session")
            opened, closed = _calendar_time(row, "open"), _calendar_time(row, "close")
            if opened >= closed:
                raise ValueError("invalid session bounds")
            sessions[day] = Session(day, opened.isoformat(), closed.isoformat())
    except (KeyError, TypeError, ValueError) as exc:
        raise CalendarPending("calendar_response_invalid") from exc
    if set(sessions) != expected:
        raise CalendarPending("calendar_session_coverage_unresolved")
    return CalendarReceipt(provider, start, end, requested.isoformat(), fetched.isoformat(),
                           tuple(sessions[key] for key in sorted(sessions)))


@dataclass(frozen=True)
class Window:
    decision_at_utc: str | None
    reference: Session
    endpoint_5: Session
    endpoint_10: Session
    calendar: CalendarReceipt | PinnedCalendar

    def endpoint(self, horizon: int) -> Session:
        if type(horizon) is not int or horizon not in (5, 10):
            raise ValueError("close_ref_v1 supports 5 and 10 sessions")
        return self.endpoint_5 if horizon == 5 else self.endpoint_10


def resolve_window(
    decision_at: datetime, calendar: CalendarReceipt | PinnedCalendar | None = None,
) -> Window:
    calendar = pinned_calendar() if calendar is None else calendar
    decision = _utc(decision_at)
    day = decision.astimezone(_ET).date().isoformat()
    if not calendar.requested_start <= day <= calendar.requested_end:
        raise CalendarPending("decision_date_outside_calendar_request")
    following = [session for session in calendar.sessions if session.close > decision]
    if len(following) < 11:
        raise CalendarPending("calendar_forward_sessions_unavailable")
    return Window(decision.isoformat(), following[0], following[5], following[10], calendar)


def window_to_dict(window: Window) -> dict:
    """Small immutable-calendar identity; never duplicate the whole table per row."""
    if not isinstance(window.calendar, PinnedCalendar):
        raise ValueError("campaign_window_requires_pinned_calendar")
    return {"decision_at_utc": window.decision_at_utc,
            "reference": asdict(window.reference), "endpoint_5": asdict(window.endpoint_5),
            "endpoint_10": asdict(window.endpoint_10), "calendar_version": window.calendar.sha256}


def window_from_dict(body: dict) -> Window:
    """Reload only exact pinned sessions and their scheduled offsets."""
    keys = {"decision_at_utc", "reference", "endpoint_5", "endpoint_10", "calendar_version"}
    if type(body) is not dict or set(body) != keys:
        raise ValueError("invalid_campaign_window")
    calendar = pinned_calendar()
    if body["calendar_version"] != calendar.sha256:
        raise ValueError("calendar_version_mismatch")
    reference = Session(**body["reference"])
    try:
        index = calendar.sessions.index(reference)
        expected_5, expected_10 = calendar.sessions[index + 5], calendar.sessions[index + 10]
    except (ValueError, IndexError) as exc:
        raise ValueError("window_outside_pinned_calendar") from exc
    if body["endpoint_5"] != asdict(expected_5) or body["endpoint_10"] != asdict(expected_10):
        raise ValueError("window_endpoints_mismatch")
    decision = body["decision_at_utc"]
    if decision is not None:
        resolved = resolve_window(datetime.fromisoformat(decision), calendar)
        if resolved.reference != reference:
            raise ValueError("window_decision_mismatch")
        decision = resolved.decision_at_utc
    return Window(decision, reference, expected_5, expected_10, calendar)


def retry_until(endpoint: Session) -> datetime:
    """Ten calendar days after the actual endpoint close, in exchange time."""
    return (endpoint.close.astimezone(_ET) + timedelta(days=10)).astimezone(timezone.utc)


@dataclass(frozen=True)
class PriceLeg:
    method: str
    symbol: str
    horizon: int
    reference_date: str
    endpoint_date: str
    provider: str
    feed: str
    adjustment: str
    requested_at_utc: str
    fetched_at_utc: str
    calendar_sha256: str
    status: str
    reason: str | None
    reference_close: float | None = None
    endpoint_close: float | None = None
    return_fraction: float | None = None


def observe_leg(
    window: Window, horizon: int, symbol: str, bars: list[dict], *,
    provider: str, feed: str, adjustment: str, requested_at: datetime,
    fetched_at: datetime, now: datetime,
) -> PriceLeg:
    """One exact-date unsigned leg; SPY is observed independently.

    Request time, not response completion time, must follow endpoint close.
    The caller must retain request provenance and may only persist a complete
    leg once. A missing candidate leg never authorizes replacing a frozen SPY
    leg (or vice versa).
    """
    endpoint = window.endpoint(horizon)
    requested, fetched, observed = _utc(requested_at), _utc(fetched_at), _utc(now)
    if not all(isinstance(value, str) and value for value in (symbol, provider, feed, adjustment)):
        raise ValueError("price provenance is required")
    if not requested <= fetched <= observed:
        raise ValueError("invalid price request chronology")
    base = PriceLeg(method=METHOD, symbol=symbol, horizon=horizon,
                    reference_date=window.reference.date, endpoint_date=endpoint.date,
                    provider=provider, feed=feed, adjustment=adjustment,
                    requested_at_utc=requested.isoformat(), fetched_at_utc=fetched.isoformat(),
                    calendar_sha256=window.calendar.sha256, status="pending", reason=None)

    def absent(reason: str) -> PriceLeg:
        status = "unavailable" if observed > retry_until(endpoint) else "pending"
        return replace(base, status=status, reason=reason)

    if observed > retry_until(endpoint):
        return absent("retry_window_expired")
    if requested < endpoint.close:
        return absent("endpoint_not_closed_at_request")
    wanted = {window.reference.date, endpoint.date}
    prices: dict[str, float] = {}
    for bar in bars:
        if not isinstance(bar, dict):
            return absent("invalid_bar_response")
        day = bar.get("date")
        if not isinstance(day, str):
            return absent("invalid_bar_date")
        if day not in wanted:
            continue
        if day in prices:
            return absent("duplicate_endpoint_bar")
        value = bar.get("close")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return absent("invalid_endpoint_close")
        try:
            valid = math.isfinite(value) and value > 0
        except OverflowError:
            valid = False
        if not valid:
            return absent("invalid_endpoint_close")
        prices[day] = float(value)
    if wanted != prices.keys():
        return absent("exact_endpoint_bar_missing")
    result = prices[endpoint.date] / prices[window.reference.date] - 1.0
    if not math.isfinite(result):
        return absent("nonfinite_return")
    return replace(base, status="complete", reason=None,
                   reference_close=prices[window.reference.date],
                   endpoint_close=prices[endpoint.date], return_fraction=result)


def combine_legs(asset: PriceLeg, benchmark: PriceLeg) -> dict:
    """Expose unsigned r and b only for identically matched complete legs."""
    fields = ("method", "horizon", "reference_date", "endpoint_date", "provider",
              "feed", "adjustment", "calendar_sha256")
    if benchmark.symbol != "SPY" or any(getattr(asset, key) != getattr(benchmark, key) for key in fields):
        raise ValueError("candidate and SPY measurement conventions differ")
    for leg in (asset, benchmark):
        if leg.status not in ("pending", "unavailable", "complete"):
            raise ValueError("invalid price leg status")
        if leg.status == "complete":
            prices = (leg.reference_close, leg.endpoint_close)
            if (any(type(value) is not float or not math.isfinite(value) or value <= 0
                    for value in prices) or
                    type(leg.return_fraction) is not float or not math.isfinite(leg.return_fraction)):
                raise ValueError("complete price legs require finite prices and returns")
            assert leg.reference_close is not None and leg.endpoint_close is not None
            if leg.return_fraction != leg.endpoint_close / leg.reference_close - 1.0:
                raise ValueError("complete price leg return contradicts prices")
    if asset.status != "complete" or benchmark.status != "complete":
        status = "unavailable" if "unavailable" in (asset.status, benchmark.status) else "pending"
        return {"method": METHOD, "status": status, "candidate_status": asset.status,
                "benchmark_status": benchmark.status, "r": None, "b": None}
    return {"method": METHOD, "status": "complete", "candidate_status": "complete",
            "benchmark_status": "complete", "r": asset.return_fraction,
            "b": benchmark.return_fraction}
