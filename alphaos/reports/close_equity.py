"""Append broker daily-close evidence without rewriting legacy account snapshots.

Alpaca daily history labels the beginning of each window; equity is valued
at its end. The calendar response supplies the actual session close,
including early closes. Never accept an unfinished session or infer a close
valuation from a live account read. This module submits no orders.
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

VALUATION_METHOD = "broker_daily_close_v2"
_ET = ZoneInfo("America/New_York")


def _calendar_close(row: dict) -> datetime:
    value = str(row["close"])
    if "T" not in value and " " not in value:
        value = f"{str(row['date'])[:10]}T{value}"
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_ET)
    if timeutils.market_date(dt).isoformat() != str(row["date"])[:10]:
        raise ValueError("calendar close does not match its session date")
    return dt.astimezone(timezone.utc)


def daily_close_evidence(history: dict, calendar: list[dict], now: datetime) -> list[dict]:
    """Validate broker-shaped arrays before yielding any immutable evidence."""
    if history.get("timeframe") != "1D":
        raise ValueError("daily close evidence requires timeframe=1D")
    stamps, equities = history.get("timestamp", []), history.get("equity", [])
    if len(stamps) != len(equities):
        raise ValueError("portfolio history timestamp/equity lengths differ")
    cashflows = history.get("cashflow")
    cashflow_known = isinstance(cashflows, dict)
    cashflows = cashflows if isinstance(cashflows, dict) else {}
    if cashflow_known and any(len(values) != len(stamps) for values in cashflows.values()):
        raise ValueError("portfolio history cashflow lengths differ")
    closes = {str(row["date"])[:10]: _calendar_close(row) for row in calendar}
    rows = []
    seen = set()
    for i, (epoch, value) in enumerate(zip(stamps, equities)):
        source_at = datetime.fromtimestamp(float(epoch), timezone.utc)
        d = timeutils.market_date(source_at).isoformat()
        if d in seen:
            raise ValueError("duplicate daily portfolio-history date")
        seen.add(d)
        close_at = closes.get(d)
        # Allow settling time; do not store an intraday value permanently.
        if close_at is None or close_at + timedelta(minutes=15) > now:
            continue
        if value is None or not math.isfinite(float(value)) or float(value) <= 0:
            continue
        flows = {str(k): float(v[i]) for k, v in cashflows.items()} if cashflow_known else None
        if flows is not None and not all(math.isfinite(v) for v in flows.values()):
            raise ValueError("non-finite cashflow")
        # Do not net offsetting cashflows to zero and silently call a day clean.
        flow_status = ("none_reported" if all(v == 0 for v in flows.values()) else "present") if flows is not None else "unknown"
        rows.append({
            "market_date": d,
            "equity": float(value),
            "equity_source": "broker_portfolio_history",
            "valuation_method_version": VALUATION_METHOD,
            "valuation_at_utc": close_at.isoformat(),
            "source_timestamp_utc": source_at.isoformat(),
            "captured_at_utc": now.isoformat(),
            "cashflow_net": sum(flows.values()) if flows is not None else None,
            "cashflow_status": flow_status,
            "raw_evidence_json": json.dumps({
                "timestamp": epoch, "equity": value, "cashflow": flows,
                "timeframe": "1D", "cashflow_types_requested": "ALL",
                "calendar_close": close_at.isoformat(),
            }, sort_keys=True),
            "is_mock": 0,
        })
    return sorted(rows, key=lambda row: row["market_date"])


def capture_close_equity(journal, settings, now=None, alpaca_client=None) -> dict:
    """Read seven calendar days to recover missed daily captures; no fallback.

    Unknown cashflows and days with reported flows remain visible evidence,
    but are excluded from unadjusted return comparisons by the report.
    """
    if settings.is_mock or not settings.has_alpaca_keys:
        return {"status": "unavailable", "reason": "no paper broker", "written": 0}
    now = now or timeutils.now_utc()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    client = alpaca_client if alpaca_client is not None else AlpacaClient(settings, journal)
    start = timeutils.market_date(now) - timedelta(days=7)
    calendar = client.get_calendar(start=start, end=timeutils.market_date(now))
    history = client.get_portfolio_history(period="7D", timeframe="1D", end=now, cashflow_types="ALL")
    rows = daily_close_evidence(history, calendar, now)
    written = 0
    for row in rows:
        try:
            journal.insert("equity_close_snapshots", {"snapshot_id": new_id("eqclose"), **row})
            written += 1
        except sqlite3.IntegrityError:
            # A repeat capture never edits previously recorded observations.
            existing = journal.one(
                "SELECT equity, cashflow_status FROM equity_close_snapshots "
                "WHERE market_date = ? AND valuation_method_version = ?",
                (row["market_date"], VALUATION_METHOD),
            )
            if not existing:
                raise
            if existing["equity"] != row["equity"] or existing["cashflow_status"] != row["cashflow_status"]:
                raise ValueError(f"broker revised recorded daily close for {row['market_date']}; review required")
    return {"status": "captured" if rows else "unavailable", "written": written,
            "eligible_observations": sum(r["cashflow_status"] == "none_reported" for r in rows),
            "valuation_method": VALUATION_METHOD}
