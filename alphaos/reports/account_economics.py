"""Versioned, read-only account/SPY economics from archived GET receipts.

This is a descriptive cash-basis measurement as of the returned sources, not
broker EOD equity, accrued/reinvested total return, or strategy activation.
No modeled trading friction is deducted from broker cash a second time.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from alphaos.reports.close_equity import _calendar_time
from alphaos.reports.common_valuation import _dt, _request_bounds, evidence_hash
from alphaos.util.market_calendar import is_trading_day

METHOD = "common_minute_reported_cash_economics_v1"
ET = ZoneInfo("America/New_York")
ZERO = Decimal(0)
CASH_TOLERANCE = Decimal("0.01")
QTY_TOLERANCE = Decimal("0.000000001")
INTERNAL = {"FEE", "INT", "DIV"}
EXTERNAL = {"CSD", "CSW"}
ACTIVITY_KEYS = {
    "id",
    "activity_type",
    "activity_sub_type",
    "created_at",
    "date",
    "status",
    "transaction_time",
    "order_id",
    "symbol",
    "qty",
    "price",
    "side",
    "type",
    "cum_qty",
    "leaves_qty",
    "order_status",
    "currency",
    "net_amount",
    "per_share_amount",
}


def number(value, *, positive=False):
    if isinstance(value, bool) or value is None:
        raise ValueError("missing or invalid accounting number")
    try:
        out = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid accounting number") from exc
    if not out.is_finite() or (positive and out <= ZERO):
        raise ValueError("nonfinite or invalid accounting number")
    return out


def quantities(rows):
    if not isinstance(rows, list):
        raise ValueError("holding list missing")
    result = {}
    for row in rows:
        symbol, side = row["symbol"], row["side"]
        qty = number(row["qty"])
        if (
            not isinstance(symbol, str)
            or not symbol
            or symbol in result
            or qty == ZERO
            or row.get("asset_class") != "us_equity"
            or side not in {"long", "short"}
            or (qty < ZERO and side != "short")
        ):
            raise ValueError("unknown or duplicate holding")
        result[symbol] = abs(qty) * (-1 if side == "short" else 1)
    return result


def validate_pages(part, kind):
    """Cross-check archived requests, page boundaries and terminal evidence."""
    pages, request = part["pages"], deepcopy(part["request"])
    if (
        not isinstance(pages, list)
        or not pages
        or len(pages) > (2 if kind == "bars" else 10)
    ):
        raise ValueError("missing or excessive receipt pages")
    if "symbols" in request:
        if not isinstance(request["symbols"], list) or len(
            set(request["symbols"])
        ) != len(request["symbols"]):
            raise ValueError("malformed requested symbols")
        request["symbols"] = ",".join(request["symbols"])
    total, prior_end, token = 0, None, None
    bounds = []
    rows = part["rows"]
    for i, page in enumerate(pages):
        expected = {**request, **({"page_token": token} if token is not None else {})}
        if page["request"] != expected:
            raise ValueError("page request differs from declared coverage")
        begin, end = _request_bounds(page)
        if prior_end is not None and begin < prior_end:
            raise ValueError("page receipt order invalid")
        bounds.append((begin, end))
        prior_end = end
        if kind == "activities":
            count, size = page["row_count"], request["page_size"]
            if (
                type(count) is not int
                or type(size) is not int
                or not 0 <= count <= size <= 100
                or size <= 0
            ):
                raise ValueError("activity page count invalid")
            total += count
            if (
                total > len(rows)
                or (i < len(pages) - 1 and count != size)
                or (i == len(pages) - 1 and count >= size)
            ):
                raise ValueError("activity page termination or count invalid")
            token = rows[total - 1]["id"] if count else None
        else:
            token = page["next_page_token"]
            if i < len(pages) - 1 and (not isinstance(token, str) or not token):
                raise ValueError("missing continuation token")
            if i == len(pages) - 1 and token not in {None, ""}:
                raise ValueError("nonterminal page falsely marked exhausted")
    if kind == "activities" and total != len(rows):
        raise ValueError("activity total differs from page counts")
    if _request_bounds(part) != (bounds[0][0], bounds[-1][1]):
        raise ValueError("aggregate receipt bounds differ from pages")


def activities(part):
    request = part["request"]
    if (
        part.get("pagination_exhausted") is not True
        or request.get("activity_types") is not None
        or request.get("category") is not None
        or request.get("date") is not None
        or request.get("direction") != "asc"
        or not isinstance(part["rows"], list)
    ):
        raise ValueError("activity query incomplete or filtered")
    validate_pages(part, "activities")
    rows = {}
    for raw in part["rows"]:
        row = {k: v for k, v in raw.items() if k in ACTIVITY_KEYS}
        key = row.get("id")
        if not isinstance(key, str) or not key or key in rows:
            raise ValueError("activity ID missing or duplicated")
        rows[key] = row
    return rows


def event(row):
    """Return cash/quantity and an honest time interval, never date→fake time."""
    kind = row["activity_type"]
    if kind == "FILL":
        if (
            row.get("type") not in {"fill", "partial_fill"}
            or not row.get("order_id")
            or not row.get("symbol")
            or row.get("side") not in {"buy", "sell"}
        ):
            raise ValueError("unsupported fill/correction")
        qty, price = (
            number(row["qty"], positive=True),
            number(row["price"], positive=True),
        )
        signed = qty if row["side"] == "buy" else -qty
        at = _dt(row["transaction_time"])
        return {
            "lo": at,
            "hi": at,
            "cash": -signed * price,
            "qty": signed,
            "symbol": row["symbol"],
            "kind": kind,
            "time_basis": "broker_transaction_time",
        }
    if kind not in INTERNAL | EXTERNAL or row.get("currency", "USD") != "USD":
        raise ValueError("unsupported economic activity: " + str(kind))
    amount = number(row["net_amount"])
    if (kind == "CSD" and amount < ZERO) or (kind == "CSW" and amount > ZERO):
        raise ValueError("external transfer direction conflicts with amount")
    if row.get("status") not in {None, "executed"}:
        raise ValueError("nonexecuted cash activity")
    if row.get("transaction_time"):
        lo = hi = _dt(row["transaction_time"])
        basis = "broker_transaction_time"
    else:
        # NTA.date may mean occurrence OR settlement, never assert posting time.
        # Only inclusion wholly inside/outside an interval is supported.
        day = date.fromisoformat(str(row["date"])[:10])
        lo = datetime.combine(day, time(), ET).astimezone(timezone.utc)
        hi = datetime.combine(day + timedelta(days=1), time(), ET).astimezone(
            timezone.utc
        )
        basis = "broker_date_interval_occurrence_or_settlement"
    return {
        "lo": lo,
        "hi": hi,
        "cash": amount,
        "qty": ZERO,
        "symbol": row.get("symbol"),
        "kind": kind,
        "time_basis": basis,
    }


def relation(movement, at):
    if movement["hi"] < at or (movement["hi"] == at and movement["lo"] != at):
        return -1
    if movement["lo"] >= at:
        return 1
    raise ValueError("date-only cash activity straddles a valuation endpoint")


def endpoint(capture):
    b = capture["raw_evidence"]
    digest = evidence_hash(b)
    if capture.get("input_sha256") != digest:
        raise ValueError("endpoint raw receipt hash mismatch")
    cutoff = _calendar_time(b["session"], "close")
    open_at = _calendar_time(b["session"], "open")
    now = _dt(b["captured_at_utc"])
    if (
        open_at >= cutoff
        or cutoff + timedelta(minutes=15) > now
        or not is_trading_day(cutoff.astimezone(ET).date())
    ):
        raise ValueError("unfinished or invalid valuation session")
    names = (
        "account_before",
        "positions_before",
        "bars",
        "activities_before",
        "account_after",
        "positions_after",
        "activities_after",
    )
    bounds = [_request_bounds(b[name]) for name in names]
    if (
        bounds[0][0] <= cutoff
        or bounds[-1][1] > now
        or any(x[1] > y[0] for x, y in zip(bounds, bounds[1:]))
    ):
        raise ValueError("capture timing or sequence unverifiable")
    accounts = [b[k]["value"] for k in ("account_before", "account_after")]
    if any(
        a.get("currency") != "USD" or not a.get("account_fingerprint") for a in accounts
    ):
        raise ValueError("account identity/currency missing")
    if accounts[0]["account_fingerprint"] != accounts[1]["account_fingerprint"]:
        raise ValueError("account identity changed")
    cash = [number(a["cash"]) for a in accounts]
    holdings = [
        quantities(b[k]["value"]) for k in ("positions_before", "positions_after")
    ]
    before, after = [
        activities(b[k]) for k in ("activities_before", "activities_after")
    ]
    if cash[0] != cash[1] or holdings[0] != holdings[1] or before != after:
        raise ValueError("cash/holdings/activities unstable during capture")
    for name, through in (
        ("activities_before", bounds[1][1]),
        ("activities_after", bounds[5][1]),
    ):
        req = b[name]["request"]
        if _dt(req["after"]) > cutoff or _dt(req["until"]) < through:
            raise ValueError("activity query does not cover valuation to snapshot")
    held, value = dict(holdings[0]), cash[0]
    reversals = []
    for key, row in after.items():
        move = event(row)
        if move["lo"] > now or (row.get("created_at") and _dt(row["created_at"]) > now):
            raise ValueError("future activity")
        if relation(move, cutoff) == 1:
            if move["time_basis"] != "broker_transaction_time":
                raise ValueError(
                    "date-only post-cutoff cash cannot be reversed as a known posting"
                )
            value -= move["cash"]
            if move["qty"]:
                held[move["symbol"]] = held.get(move["symbol"], ZERO) - move["qty"]
            reversals.append(key)
    held = {k: v for k, v in held.items() if v}
    bars = b["bars"]
    validate_pages(bars, "bars")
    req = bars["request"]
    source_at = cutoff - timedelta(minutes=1)
    needed = set(held) | {"SPY"}
    if (
        req.get("timeframe") != "1Min"
        or req.get("adjustment") != "raw"
        or req.get("feed") not in {"iex", "sip"}
        or req["feed"] != b["configured_feed"]
        or _dt(req["start"]) != source_at
        or not source_at <= _dt(req["end"]) < cutoff
        or bars.get("pagination_exhausted") is not True
        or not needed <= set(req["symbols"])
        or set(bars["rows"]) != set(req["symbols"])
    ):
        raise ValueError("common final-minute marks missing or request invalid")
    marks = {}
    for symbol, rows in bars["rows"].items():
        if len(rows) != 1 or _dt(rows[0]["t"]) != source_at:
            raise ValueError("missing or duplicate exact common-minute bar")
        bar = rows[0]
        price = number(bar["c"], positive=True)
        if (
            not number(bar["l"], positive=True)
            <= price
            <= number(bar["h"], positive=True)
        ):
            raise ValueError("invalid bar price")
        number(bar["v"], positive=True)
        marks[symbol] = price
    nav = value + sum((q * marks[s] for s, q in held.items()), ZERO)
    number(nav, positive=True)
    return {
        "cutoff": cutoff,
        "cash": value,
        "held": held,
        "marks": marks,
        "nav": nav,
        "rows": after,
        "raw": b,
        "sha256": digest,
        "feed": req["feed"],
        "reversals": reversals,
        "account": accounts[0]["account_fingerprint"],
    }


def operating_cost(start, end, spec):
    """Declared monthly overhead, calendar-second allocation; not a broker fee."""
    if spec.get("basis") != "declared_monthly_budget":
        raise ValueError("unsupported operating-cost evidence basis")
    rate = number(spec["monthly_usd"])
    if rate < ZERO:
        raise ValueError("negative operating-cost budget")
    zone = ZoneInfo(spec["timezone"])
    cursor, finish = start.astimezone(zone), end.astimezone(zone)
    total = ZERO
    while cursor < finish:
        first = cursor.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        next_month = (
            first.replace(year=first.year + 1, month=1)
            if first.month == 12
            else first.replace(month=first.month + 1)
        )
        stop = min(finish, next_month)
        seconds = Decimal(
            str(
                (
                    stop.astimezone(timezone.utc) - cursor.astimezone(timezone.utc)
                ).total_seconds()
            )
        )
        month_seconds = Decimal(
            str(
                (
                    next_month.astimezone(timezone.utc) - first.astimezone(timezone.utc)
                ).total_seconds()
            )
        )
        total += rate * seconds / month_seconds
        cursor = stop
    return total


def benchmark_distributions(part, start, end, symbols):
    validate_pages(part, "corporate_actions")
    req = part["request"]
    t0, t1 = start["cutoff"], end["cutoff"]
    d0, d1 = t0.astimezone(ET).date(), t1.astimezone(ET).date()
    if (
        part.get("pagination_exhausted") is not True
        or req.get("types") is not None
        or req.get("data_quality") != "all"
        or not symbols <= set(req["symbols"])
        or date.fromisoformat(req["start"]) > d0 - timedelta(days=366)
        or date.fromisoformat(req["end"]) < d1 + timedelta(days=366)
    ):
        raise ValueError(
            "corporate-action query incomplete or missing wide process-date coverage"
        )
    if _dt(part["received_at_utc"]) < _dt(end["raw"]["captured_at_utc"]):
        raise ValueError("corporate-action evidence predates ending capture")
    total, seen, receipts = ZERO, set(), []
    for kind, rows in part["rows"].items():
        if not isinstance(rows, list):
            raise ValueError("corporate-action rows malformed")
        for row in rows:
            key = row.get("id")
            if not key or key in seen:
                raise ValueError("corporate-action ID missing or duplicated")
            seen.add(key)
            # Reject unknown types in the investment window rather than
            # guessing a share adjustment or treating a split as a return.
            effective = date.fromisoformat(row.get("ex_date") or row["effective_date"])
            if kind != "cash_dividends":
                if d0 < effective <= d1:
                    raise ValueError(
                        "unsupported corporate action in measurement interval: " + kind
                    )
                continue
            symbol = row["symbol"]
            rate = number(row["rate"], positive=True)
            payable = date.fromisoformat(row["payable_date"])
            if (d0 < effective <= d1 or d0 < payable <= d1) and row.get(
                "status"
            ) not in {None, "confirmed", "processed"}:
                raise ValueError("canceled or unconfirmed dividend announcement")
            if (
                payable < effective
                or row.get("special")
                or row.get("foreign")
                or row.get("due_bill_on_date")
                or row.get("due_bill_off_date")
            ):
                raise ValueError("unsupported dividend terms")
            # A purchase at the opening close is entitled only if ex-date is
            # later. Cash remains unreinvested; no payment before payable date.
            if symbol == "SPY" and d0 < effective <= payable <= d1:
                total += rate
                receipts.append(
                    {
                        "id": key,
                        "per_share": str(rate),
                        "ex_date": str(effective),
                        "payable_date": str(payable),
                        "source_sha256": evidence_hash(row),
                    }
                )
    return total, receipts


def fill_orders(rows):
    """Incremental fill qty is additive; cum_qty is an independent check."""
    grouped: dict[str, list] = {}
    for row in rows.values():
        if row["activity_type"] == "FILL":
            grouped.setdefault(row["order_id"], []).append(row)
    summaries = []
    for order_id, fills in sorted(grouped.items()):
        fills.sort(
            key=lambda row: (
                _dt(row["transaction_time"]),
                number(row.get("cum_qty", row["qty"])),
            )
        )
        identity = {(row["symbol"], row["side"]) for row in fills}
        if len(identity) != 1:
            raise ValueError("fill order identity changed")
        qty, notional, prior = ZERO, ZERO, None
        for row in fills:
            q, price = (
                number(row["qty"], positive=True),
                number(row["price"], positive=True),
            )
            qty += q
            notional += q * price
            if row.get("cum_qty") is not None:
                cumulative = number(row["cum_qty"], positive=True)
                if cumulative < q or (prior is not None and cumulative - prior != q):
                    raise ValueError("incremental/cumulative fill quantities disagree")
                prior = cumulative
            else:
                prior = None
        symbol, side = next(iter(identity))
        summaries.append(
            {
                "order_id": order_id,
                "symbol": symbol,
                "side": side,
                "reported_incremental_qty": str(qty),
                "reported_notional": str(notional),
                "reported_vwap": str(notional / qty),
                "fill_count": len(fills),
                "final_cumulative_qty": str(prior) if prior is not None else None,
            }
        )
    return summaries


def measure_account_economics(bundle):
    result: dict[str, Any] = {
        "method": METHOD,
        "status": "blocked",
        "comparison_eligible": False,
        "research_activation": False,
        "blockers": [],
        "percentage_comparison_blockers": [],
        "basis": "as-of reported cash-basis common final-minute NAV; not accrued/reinvested total return",
        "source_limitations": [
            "Late or revised activities/corporate actions can revise this as-of measurement.",
            "Unreported fees are not certified absent; cash-basis dividends exclude unpaid receivables.",
            "The hypothetical SPY allocation has fractional shares and no trading friction.",
        ],
    }
    try:
        result["input_sha256"] = evidence_hash(bundle)
        if bundle.get("schema") != "cash_basis_economic_inputs_v1":
            raise ValueError("unknown economic input schema")
        a, b = [endpoint(bundle[k]) for k in ("start_capture", "end_capture")]
        t0, t1 = a["cutoff"], b["cutoff"]
        if t0 >= t1 or _dt(a["raw"]["captured_at_utc"]) >= t1:
            raise ValueError("ordered independently archived endpoints required")
        if a["account"] != b["account"] or a["feed"] != b["feed"]:
            raise ValueError("account/feed differs between endpoints")
        result["reported_order_fills"] = fill_orders(b["rows"])
        if _dt(b["raw"]["activities_after"]["request"]["after"]) > _dt(
            a["raw"]["activities_after"]["request"]["after"]
        ):
            raise ValueError(
                "ending creation-time query does not cover retained opening activities"
            )
        for key, old in a["rows"].items():
            if b["rows"].get(key) != old:
                raise ValueError("retained opening activity missing or revised")
        expected = dict(a["held"])
        fill_cash = internal = external = ZERO
        by_type: dict[str, Decimal] = {}
        movements, flows = [], []
        for key, row in b["rows"].items():
            move = event(row)
            if relation(move, t0) == -1:
                if key not in a["rows"]:
                    raise ValueError(
                        "late prior-period activity requires opening restatement review"
                    )
                continue
            if relation(move, t1) == 1:
                continue  # reversed at endpoint, not included in the interval
            kind, amount = move["kind"], move["cash"]
            if kind == "FILL":
                fill_cash += amount
                expected[move["symbol"]] = (
                    expected.get(move["symbol"], ZERO) + move["qty"]
                )
            elif kind in EXTERNAL:
                external += amount
                flows.append(
                    {
                        "id": key,
                        "amount": str(amount),
                        "time_basis": move["time_basis"],
                        "from_utc": move["lo"].isoformat(),
                        "to_utc": move["hi"].isoformat(),
                    }
                )
            else:
                internal += amount
                by_type[kind] = by_type.get(kind, ZERO) + amount
            movements.append(
                {
                    "id": key,
                    "kind": kind,
                    "cash": str(amount),
                    "quantity": str(move["qty"]),
                    "symbol": move["symbol"],
                    "time_basis": move["time_basis"],
                    "source_sha256": evidence_hash(row),
                }
            )
        cash_residual = b["cash"] - a["cash"] - fill_cash - internal - external
        qres = {
            s: b["held"].get(s, ZERO) - expected.get(s, ZERO)
            for s in set(expected) | set(b["held"])
        }
        result.update(
            start_at_utc=t0.isoformat(),
            end_at_utc=t1.isoformat(),
            start_source_sha256=a["sha256"],
            end_source_sha256=b["sha256"],
            start_nav=str(a["nav"]),
            end_nav=str(b["nav"]),
            cash_start=str(a["cash"]),
            cash_end=str(b["cash"]),
            fill_cash=str(fill_cash),
            internal_cash=str(internal),
            internal_by_type={k: str(v) for k, v in sorted(by_type.items())},
            external_net_inflow=str(external),
            external_events=flows,
            cash_residual=str(cash_residual),
            cash_tolerance=str(CASH_TOLERANCE),
            quantity_residuals={k: str(v) for k, v in sorted(qres.items())},
            start_quantities={k: str(v) for k, v in a["held"].items()},
            end_quantities={k: str(v) for k, v in b["held"].items()},
            start_marks={k: str(v) for k, v in a["marks"].items()},
            end_marks={k: str(v) for k, v in b["marks"].items()},
            endpoint_reversals={"start": a["reversals"], "end": b["reversals"]},
            activities=movements,
        )
        if abs(cash_residual) > CASH_TOLERANCE:
            raise ValueError("cash_does_not_reconcile")
        if any(abs(q) > QTY_TOLERANCE for q in qres.values()):
            raise ValueError("holdings_do_not_reconcile")
        symbols = set(a["held"]) | set(b["held"]) | {"SPY"}
        symbols.update(m["symbol"] for m in movements if m["kind"] == "FILL")
        dividend, receipts = benchmark_distributions(
            bundle["corporate_actions"], a, b, symbols
        )
        gain = b["nav"] - a["nav"] - external
        benchmark_return = (b["marks"]["SPY"] + dividend) / a["marks"]["SPY"] - 1
        result.update(
            status="reconciled_asof",
            account_gain_usd=str(gain),
            market_and_execution_gain_usd=str(gain - internal),
            reported_broker_fee_cash=str(by_type.get("FEE", ZERO)),
            broker_fee_completeness="not_certified",
            account_paid_dividend_cash=str(by_type.get("DIV", ZERO)),
            benchmark_cash_dividend_per_share=str(dividend),
            benchmark_dividends=receipts,
            benchmark_distribution_basis="hypothetical published payable-date schedule; not broker paid-cash certification",
            benchmark_return=str(benchmark_return),
            benchmark_gain_usd=str(a["nav"] * benchmark_return),
            modeled_trade_cost_deduction="0",
            fill_price_basis="actual reported executions; slippage already in cash",
            final_or_accrued_comparison_eligible=False,
        )
        result["dividend_convention"] = (
            "Account: reported paid cash only. Hypothetical SPY: entitlement after opening purchase, "
            "cash on announced payable date, held without reinvestment. Unpaid receivables excluded from both. "
            "Actual broker dividend payment may post later than the announced date."
        )
        if flows:
            # Dollar profit remains useful; do not invent reinvestment prices,
            # a time-weighted return, or equality from offsetting transfer net0.
            result["percentage_comparison_blockers"].append(
                "external_flows_require_matching_subperiod_valuations"
            )
            result["benchmark_return"] = result["benchmark_gain_usd"] = None
        else:
            result.update(
                comparison_eligible=True,
                account_return=str(gain / a["nav"]),
                excess_gain_usd=str(gain - a["nav"] * benchmark_return),
                excess_return=str(gain / a["nav"] - benchmark_return),
            )
        if bundle.get("operating_cost") is not None:
            cost = operating_cost(t0, t1, bundle["operating_cost"])
            result.update(
                operating_cost_usd=str(cost),
                operating_cost_basis=deepcopy(bundle["operating_cost"]),
                account_gain_after_declared_overhead_usd=str(gain - cost),
            )
        else:
            result["operating_cost_status"] = (
                "not_supplied; account gain excludes external operating overhead"
            )
    except (
        KeyError,
        TypeError,
        ValueError,
        AttributeError,
        OverflowError,
        InvalidOperation,
    ) as exc:
        result["status"] = "blocked"
        result["comparison_eligible"] = False
        result["blockers"].append(str(exc))
        # Partial arithmetic is diagnostic only. Never leave reportable gain,
        # return or excess conclusions after a later validation failure.
        for key in list(result):
            if (
                key.endswith("return")
                or "gain" in key
                or key
                in {
                    "operating_cost_usd",
                    "reported_broker_fee_cash",
                    "account_paid_dividend_cash",
                    "benchmark_cash_dividend_per_share",
                    "benchmark_dividends",
                }
            ):
                result.pop(key)
    return result
