"""Preserve provisional broker closing-minute evidence, without claiming EOD parity.

The broker model documents left-labelled windows: source + one minute is
bound to the calendar close. Observed intraday values differ from the daily
EOD series, so this version NEVER authorizes strict SPY-close comparisons.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from alphaos.broker.alpaca_client import AlpacaClient
from alphaos.util import timeutils
from alphaos.util.ids import new_id

VALUATION_METHOD = "broker_closing_minute_provisional_v3"
TIMING_STATUS = "provisional_broker_closing_window"
_ET = ZoneInfo("America/New_York")
_MINUTE = timedelta(minutes=1)
# Deliberately narrow: unfamiliar nonzero activities require review.
_INTERNAL_FLOWS = {"FEE"}
_EXTERNAL_FLOWS = {"CSD", "CSW"}


def _calendar_time(row: dict, key: str) -> datetime:
    value = str(row[key])
    if "T" not in value and " " not in value:
        value = f"{str(row['date'])[:10]}T{value}"
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_ET)
    if timeutils.market_date(dt).isoformat() != str(row["date"])[:10]:
        raise ValueError(f"calendar {key} does not match its session date")
    return dt.astimezone(timezone.utc)


def _calendar_close(row: dict) -> datetime:
    return _calendar_time(row, "close")


def daily_close_evidence(history: dict, calendar: list[dict], now: datetime) -> list[dict]:
    """Validate arrays and retain the documented one-minute closing window.

    Function name remains compatible with the capture caller. Daily/midnight
    observations are rejected, never silently rebound to another session.
    Every returned row remains ineligible for strict comparative performance.
    """
    if history.get("timeframe") != "1Min":
        raise ValueError("closing-window evidence requires timeframe=1Min")
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    stamps, equities = history.get("timestamp", []), history.get("equity", [])
    if len(stamps) != len(equities):
        raise ValueError("portfolio history timestamp/equity lengths differ")
    raw_cashflows = history.get("cashflow")
    cashflow_known = isinstance(raw_cashflows, dict)
    cashflows = raw_cashflows if isinstance(raw_cashflows, dict) else {}
    if any(not isinstance(values, list) or len(values) != len(stamps) for values in cashflows.values()):
        raise ValueError("portfolio history cashflow lengths differ")
    points = {}
    for i, (epoch, equity) in enumerate(zip(stamps, equities)):
        source = datetime.fromtimestamp(float(epoch), timezone.utc)
        if source in points:
            raise ValueError("duplicate portfolio-history timestamp")
        if source.second or source.microsecond:
            raise ValueError("portfolio-history timestamp is not a minute boundary")
        flows = {str(getattr(k, "value", k)): float(v[i]) for k, v in cashflows.items()}
        if not all(math.isfinite(v) for v in flows.values()):
            raise ValueError("non-finite cashflow")
        points[source] = {"timestamp": epoch, "equity": equity, "cashflow": flows}
    rows = []
    seen_dates = set()
    for day in calendar:
        d = str(day["date"])[:10]
        if d in seen_dates:
            raise ValueError("duplicate calendar session")
        seen_dates.add(d)
        close_at = _calendar_close(day)
        open_at = _calendar_time(day, "open")
        if open_at >= close_at:
            raise ValueError("invalid calendar session bounds")
        if close_at + timedelta(minutes=15) > now:
            continue
        source_at = close_at - _MINUTE
        point = points.get(source_at)
        if point is None:
            continue  # no nearby-minute, daily-history or account-read fallback
        value = point["equity"]
        if value is None or not math.isfinite(float(value)) or float(value) <= 0:
            continue
        expected = {open_at + i * _MINUTE for i in range(int((close_at - open_at).total_seconds() // 60))}
        coverage_complete = expected <= points.keys()
        if not coverage_complete:
            continue  # avoid freezing a partial first day or a gapped cashflow window
        # Preserve all reported cashflow events on this session's ET date,
        # including its final boundary point. Do not infer overnight coverage.
        events = [{"source_timestamp_utc": ts.isoformat(), "amounts": p["cashflow"]}
                  for ts, p in sorted(points.items())
                  if timeutils.market_date(ts).isoformat() == d and any(p["cashflow"].values())]
        kinds = {k for e in events for k, v in e["amounts"].items() if v != 0}
        unknown_types = sorted(kinds - _INTERNAL_FLOWS - _EXTERNAL_FLOWS)
        known_coverage = cashflow_known and coverage_complete
        flow_status = ("unknown" if not known_coverage or unknown_types else
                       "present" if kinds & _EXTERNAL_FLOWS else
                       "internal_only" if kinds & _INTERNAL_FLOWS else "none_reported")
        external_net = (sum(v for e in events for k, v in e["amounts"].items() if k in _EXTERNAL_FLOWS)
                        if known_coverage and not unknown_types else None)
        raw = {
            **point, "timeframe": "1Min", "intraday_reporting_requested": "market_hours",
            "cashflow_types_requested": "ALL", "calendar_open": open_at.isoformat(),
            "calendar_close": close_at.isoformat(), "source_window_start_utc": source_at.isoformat(),
            "documented_window_end_utc": close_at.isoformat(), "timing_status": TIMING_STATUS,
            "comparison_eligible": False,
            "timing_note": "Documented intraday closing window; parity with broker daily EOD valuation is unverified.",
            "cashflow_events": events, "cashflow_status": flow_status,
            "reported_external_cashflow_net": external_net, "unknown_nonzero_activity_types": unknown_types,
            "regular_session_minutes_complete": coverage_complete,
            "cashflow_coverage_note": "Reported events in the requested market-hours history; overnight attribution is not independently verified. FEE remains internal P&L, without adjustment.",
        }
        rows.append({
            "market_date": d, "equity": float(value), "equity_source": "broker_portfolio_history_intraday",
            "valuation_method_version": VALUATION_METHOD, "valuation_at_utc": close_at.isoformat(),
            "source_timestamp_utc": source_at.isoformat(), "captured_at_utc": now.isoformat(),
            "cashflow_net": external_net, "cashflow_status": flow_status,
            "raw_evidence_json": json.dumps(raw, sort_keys=True), "is_mock": 0,
            "comparison_eligible": False,
        })
    return sorted(rows, key=lambda row: row["market_date"])


def capture_close_equity(journal, settings, now=None, alpaca_client=None) -> dict:
    """One seven-day intraday history read; append provisional evidence only."""
    if settings.is_mock or not settings.has_alpaca_keys:
        return {"status": "unavailable", "reason": "no paper broker", "written": 0}
    now = now or timeutils.now_utc()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    client = alpaca_client if alpaca_client is not None else AlpacaClient(settings, journal)
    start = timeutils.market_date(now) - timedelta(days=7)
    calendar = client.get_calendar(start=start, end=timeutils.market_date(now))
    history = client.get_portfolio_history(period="7D", timeframe="1Min", end=now,
                                           intraday_reporting="market_hours", cashflow_types="ALL")
    rows = daily_close_evidence(history, calendar, now)
    written = 0
    for row in rows:
        try:
            stored = {k: v for k, v in row.items() if k != "comparison_eligible"}
            journal.insert("equity_close_snapshots", {"snapshot_id": new_id("eqclose"), **stored})
            written += 1
        except sqlite3.IntegrityError:
            existing = journal.one(
                "SELECT equity, cashflow_status, raw_evidence_json FROM equity_close_snapshots "
                "WHERE market_date = ? AND valuation_method_version = ?",
                (row["market_date"], VALUATION_METHOD),
            )
            if not existing:
                raise
            if (existing["equity"] != row["equity"] or existing["cashflow_status"] != row["cashflow_status"]
                    or existing["raw_evidence_json"] != row["raw_evidence_json"]):
                raise ValueError(f"broker revised recorded closing-window evidence for {row['market_date']}; review required")
    return {"status": "provisional" if rows else "unavailable", "written": written,
            "eligible_observations": 0, "provisional_observations": len(rows),
            "timing_status": TIMING_STATUS, "valuation_method": VALUATION_METHOD}
