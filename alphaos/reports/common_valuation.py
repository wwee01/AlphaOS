"""Manual evidence collection for an explicit common final-minute NAV.

This is not broker equity. Cash plus signed holdings are marked using the
same feed's exact final regular-session minute as SPY. Broker activity APIs
do not promise complete economic-event knowledge at read time: a successful
reconstruction therefore remains ineligible for strict investment returns.
Nothing in this module schedules work, changes gates, or places orders.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from alphaos.reports.close_equity import _calendar_time
from alphaos.util import timeutils
from alphaos.util.market_calendar import is_trading_day, trading_days_between

METHOD = "common_final_minute_nav_candidate_v1"
CATEGORY = "common_valuation_evidence"
SOURCE_LIMITATION = (
    "Activity pagination proves only the returned creation-time query was exhausted. "
    "Late postings and settlement-date activities require independent reconciliation; "
    "two stable account reads do not certify complete economic-event knowledge."
)


def _dt(value):
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("evidence timestamps must include a timezone")
    return result.astimezone(timezone.utc)


def _number(value, *, positive=False):
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        raise ValueError("non-finite or invalid numeric evidence")
    return number


def evidence_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _positions(rows):
    if not isinstance(rows, list):
        raise ValueError("position response is incomplete")
    result = {}
    for row in rows:
        symbol = row.get("symbol")
        if not symbol or symbol in result or row.get("asset_class") != "us_equity":
            raise ValueError("unknown, duplicate or unsupported holding")
        qty = _number(row["qty"])
        if qty == 0 or row.get("side") not in {"long", "short"} or (qty < 0 and row["side"] != "short"):
            raise ValueError("holding direction is unverifiable")
        result[symbol] = abs(qty) * (-1 if row["side"] == "short" else 1)
    return result


def _request_bounds(part):
    start, end = _dt(part["requested_at_utc"]), _dt(part["received_at_utc"])
    if start > end:
        raise ValueError("request receipt precedes dispatch")
    return start, end


def _activity_rows(part, previous_close, finish):
    request = part["request"]
    if (part.get("pagination_exhausted") is not True or request.get("activity_types") is not None
            or request.get("category") is not None or request.get("direction") != "asc"
            or _dt(request["after"]) > previous_close or _dt(request["until"]) < finish):
        raise ValueError("activity query coverage is incomplete")
    rows = part["rows"]
    if not isinstance(rows, list):
        raise ValueError("activity response is incomplete")
    ids = [row.get("id") for row in rows]
    if None in ids or "" in ids or len(ids) != len(set(ids)):
        raise ValueError("activity IDs are missing or duplicated")
    return rows


def reconcile_common_valuation(bundle):
    """Pure reconstruction or explicit blockers; preserve every input and hash.

    Even a numerically matched reconstruction is a candidate measurement.
    There is intentionally no caller-provided flag that can promote it.
    """
    result = {"method": METHOD, "status": "blocked", "comparison_eligible": False,
              "reconstructed_nav": None, "benchmark_price": None, "blockers": [],
              "source_limitations": [SOURCE_LIMITATION], "input_sha256": evidence_hash(bundle),
              "raw_evidence": bundle, "dividend_treatment": "SPY price return; dividends are not reinvested",
              "strict_comparison_blockers": ["economic_activity_completeness_unverified", "method_not_activated"]}
    try:
        close_at = _calendar_time(bundle["session"], "close")
        previous_close = _calendar_time(bundle["previous_session"], "close")
        market_day, previous_day = timeutils.market_date(close_at), timeutils.market_date(previous_close)
        if not is_trading_day(market_day) or not is_trading_day(previous_day) or trading_days_between(previous_day, market_day) != 1:
            raise ValueError("calendar does not identify adjacent trading sessions")
        source_at = close_at - timedelta(minutes=1)
        now = _dt(bundle["captured_at_utc"])
        if previous_close >= close_at or close_at + timedelta(minutes=15) > now:
            raise ValueError("valuation session is unfinished or previous session is invalid")
        result.update(market_date=str(bundle["session"]["date"])[:10],
                      valuation_cutoff_utc=close_at.isoformat(), source_interval_start_utc=source_at.isoformat(),
                      source_interval_end_exclusive_utc=close_at.isoformat())
        accounts = [bundle[key] for key in ("account_before", "account_after")]
        positions = [bundle[key] for key in ("positions_before", "positions_after")]
        ordered_parts = [accounts[0], positions[0], bundle["bars"], bundle["activities_before"],
                         accounts[1], positions[1], bundle["activities_after"]]
        bounds = [_request_bounds(part) for part in ordered_parts]
        if bounds[0][0] <= close_at or bounds[-1][1] > now or any(a[1] > b[0] for a, b in zip(bounds, bounds[1:])):
            raise ValueError("capture order or request timing is unverifiable")
        account_values = [part["value"] for part in accounts]
        if any(value.get("currency") != "USD" or not value.get("account_fingerprint") for value in account_values):
            raise ValueError("account identity or currency is unverifiable")
        if account_values[0]["account_fingerprint"] != account_values[1]["account_fingerprint"]:
            raise ValueError("account identity changed during capture")
        cash = [_number(value["cash"]) for value in account_values]
        held = [_positions(part["value"]) for part in positions]
        if cash[0] != cash[1] or held[0] != held[1]:
            raise ValueError("cash or holdings changed during capture")
        before = _activity_rows(bundle["activities_before"], previous_close, bounds[1][1])
        after = _activity_rows(bundle["activities_after"], previous_close, bounds[5][1])
        if before != after:
            raise ValueError("account activities changed during capture")
        activity_summary: dict[str, Any] = {"fills": 0, "internal_cash": 0.0, "external_cash": 0.0, "external_event_count": 0,
                            "unknown_or_ambiguous": []}
        for activity in after:
            kind = activity.get("activity_type")
            if kind == "FILL":
                at = _dt(activity["transaction_time"])
                if activity.get("type") not in {"fill", "partial_fill"} or not activity.get("order_id"):
                    raise ValueError("fill or correction activity is unverifiable")
                _number(activity["qty"], positive=True)
                _number(activity["price"], positive=True)
                if activity.get("side") not in {"buy", "sell"} or not activity.get("symbol"):
                    raise ValueError("fill terms are unverifiable")
                if at >= close_at:
                    raise ValueError("post-cutoff fill prevents reconstructing closing holdings/cash")
                if at > previous_close:
                    activity_summary["fills"] += 1
            else:
                # NTA.date can be a settlement date, not a posting timestamp.
                # Even a timestamp-shaped date must not be promoted to one.
                amount = _number(activity["net_amount"]) if activity.get("net_amount") is not None else None
                at = activity.get("transaction_time")
                if not at or kind not in {"FEE", "DIV", "INT", "CSD", "CSW"}:
                    activity_summary["unknown_or_ambiguous"].append({"id": activity["id"], "activity_type": kind,
                                                                  "net_amount": amount, "date": activity.get("date")})
                    continue
                at = _dt(at)
                if amount is None or at >= close_at:
                    raise ValueError("post-cutoff or unknown cash activity prevents reconstructing closing cash")
                if at > previous_close:
                    if kind in {"CSD", "CSW"}:
                        activity_summary["external_cash"] += amount
                        activity_summary["external_event_count"] += 1
                    else:
                        activity_summary["internal_cash"] += amount
        result["activity_summary"] = activity_summary
        if activity_summary["unknown_or_ambiguous"]:
            raise ValueError("date-only, unknown or corporate activity requires cash/quantity reconciliation")
        bars = bundle["bars"]
        request = bars["request"]
        symbols = sorted(set(held[0]) | {"SPY"})
        if (request.get("symbols") != symbols or request.get("timeframe") != "1Min"
                or request.get("adjustment") != "raw" or request.get("feed") not in {"iex", "sip"}
                or request.get("feed") != bundle.get("configured_feed")
                or _dt(request["start"]) != source_at or _dt(request["end"]) >= close_at
                or _dt(request["end"]) < source_at or bars.get("pagination_exhausted") is not True):
            raise ValueError("common mark request provenance is unverifiable")
        if set(bars["rows"]) != set(symbols):
            raise ValueError("missing or unexpected common-mark symbol")
        marks = {}
        for symbol in symbols:
            rows = bars["rows"][symbol]
            if len(rows) != 1 or _dt(rows[0]["t"]) != source_at:
                raise ValueError(f"missing or ambiguous exact final-minute bar for {symbol}")
            bar = rows[0]
            price = _number(bar["c"], positive=True)
            if not _number(bar["l"], positive=True) <= price <= _number(bar["h"], positive=True):
                raise ValueError("bar close is outside its valid high/low")
            _number(bar["v"], positive=True)
            marks[symbol] = price
        nav = cash[0] + sum(qty * marks[symbol] for symbol, qty in held[0].items())
        _number(nav, positive=True)
        result.update(status="reconstructed_candidate", reconstructed_nav=nav, benchmark_price=marks["SPY"],
                      cash=cash[0], signed_holdings=held[0], marks=marks, feed=request["feed"], adjustment="raw",
                      convention="cash plus signed closing holdings marked at the last qualifying trade in each exact final regular-session minute",
                      cashflow_status="external_present" if activity_summary["external_event_count"] else "none_observed",
                      account_fingerprint=account_values[0]["account_fingerprint"])
        if activity_summary["external_event_count"]:
            result["strict_comparison_blockers"].append("external_flows_require_subperiod_valuations")
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        result["blockers"].append(str(exc))
    return result


def collect_common_valuation(source, now=None):
    """Manual only, GET-only source; return evidence without writing any ledger."""
    now = _dt(now or timeutils.now_utc())
    calendar = source.calendar(timeutils.market_date(now) - timedelta(days=14), timeutils.market_date(now))
    completed = sorted([row for row in calendar if _calendar_time(row, "close") + timedelta(minutes=15) <= now],
                       key=lambda row: row["date"])
    if len(completed) < 2 or len({row["date"] for row in calendar}) != len(calendar):
        raise ValueError("two distinct completed calendar sessions are required")
    previous, session = completed[-2:]
    close_at, previous_close = _calendar_time(session, "close"), _calendar_time(previous, "close")
    first_account = source.account()
    first_positions = source.positions()
    symbols = sorted(set(_positions(first_positions["value"])) | {"SPY"})
    bars = source.final_minute_bars(symbols, close_at)
    # Widen the creation-time query to retain delayed/date-only evidence. This
    # is useful reconciliation input, never a promise that all late events exist.
    activity_start = previous_close - timedelta(days=7)
    first_activities = source.activities(activity_start)
    second_account = source.account()
    second_positions = source.positions()
    second_activities = source.activities(activity_start)
    bundle = {"session": session, "previous_session": previous,
              "configured_feed": source.settings.market_data_feed,
              "calendar_response": calendar, "captured_at_utc": source.now().isoformat(),
              "account_before": first_account, "positions_before": first_positions,
              "bars": bars, "activities_before": first_activities, "account_after": second_account,
              "positions_after": second_positions, "activities_after": second_activities}
    return reconcile_common_valuation(bundle)


def record_common_valuation(journal, result):
    """Explicit append-only diagnostic storage; never modifies valuation rows."""
    from alphaos.constants import Severity
    journal.log_system_event(Severity.INFO if result["status"] == "reconstructed_candidate" else Severity.WARNING,
                             CATEGORY, f"{METHOD}: {result['status']}; strict comparison remains unavailable", result)
