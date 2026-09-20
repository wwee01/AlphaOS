"""Pure accounting bridge between two archived common-valuation captures.

This proves arithmetic against returned broker activities, not activity-feed
completeness, total-return benchmark economics, or research activation. It
never fetches data, writes the journal, or marks a comparison eligible.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from alphaos.reports.common_valuation import _dt, evidence_hash, reconcile_common_valuation

METHOD = "returned_activity_cash_quantity_bridge_v1"
CASH_TOLERANCE = Decimal("0.01")  # account cash is reported to cents
QUANTITY_TOLERANCE = Decimal("0.000000001")


def _decimal(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("invalid accounting number")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid accounting number") from exc
    if not result.is_finite():
        raise ValueError("nonfinite accounting number")
    return result


def _quantities(bundle):
    return {
        row["symbol"]: abs(_decimal(row["qty"])) * (-1 if row["side"] == "short" else 1)
        for row in bundle["positions_after"]["value"]
    }


def reconcile_cash_quantity_bridge(start_capture, end_capture):
    """Revalidate raw receipts, reconcile all movements, retain every blocker.

    Only adjacent sessions and the same account/feed are supported. External
    transfers, even offsetting ones, are counted separately from trading P&L.
    A cash or quantity match alone never certifies omitted offsetting events.
    """
    result: dict[str, Any] = {
        "method": METHOD, "status": "blocked", "comparison_eligible": False,
        "blockers": [], "source_receipts": {},
        "strict_comparison_blockers": ["economic_activity_completeness_unverified",
                                       "method_not_activated", "SPY_dividends_not_reinvested"],
    }
    try:
        # Derived status/marks supplied by a caller are never trusted.
        bundles = [capture["raw_evidence"] for capture in (start_capture, end_capture)]
        verified = [reconcile_common_valuation(bundle) for bundle in bundles]
        if any(mark["status"] != "reconstructed_candidate" for mark in verified):
            raise ValueError("both endpoint raw bundles must reconstruct")
        for name, supplied, mark in zip(("start", "end"), (start_capture, end_capture), verified):
            if supplied.get("input_sha256") != mark["input_sha256"]:
                raise ValueError("archived endpoint receipt does not match its raw bundle")
            result["source_receipts"][name] = mark["input_sha256"]
        start, end = verified
        a, b = bundles
        t0, t1 = [_dt(mark["valuation_cutoff_utc"]) for mark in verified]
        if (t0 >= t1 or b["previous_session"]["date"] != a["session"]["date"]
                or _dt(a["captured_at_utc"]) >= t1):
            raise ValueError("adjacent endpoints captured before the next endpoint are required")
        if (start["account_fingerprint"] != end["account_fingerprint"]
                or start["feed"] != end["feed"]):
            raise ValueError("endpoint account or feed differs")
        activity_query = b["activities_after"]["request"]
        if _dt(activity_query["after"]) > t0:
            raise ValueError("end activity query does not cover the whole bridge")

        old_rows = {row["id"]: row for row in a["activities_after"]["rows"]}
        new_rows = {row["id"]: row for row in b["activities_after"]["rows"]}
        # The API query is creation-time based, so transaction_time cannot
        # prove why a retained receipt disappears from a later query. Compare
        # every shared ID regardless of transaction time; a missing old ID
        # needs a wider query or independent coverage review.
        for key, row in old_rows.items():
            if key not in new_rows:
                raise ValueError("previously observed activity missing; creation-query coverage review required")
            if new_rows[key] != row:
                raise ValueError("previously observed activity changed")

        cash0, cash1 = [_decimal(bundle["account_after"]["value"]["cash"]) for bundle in bundles]
        quantities0, quantities1 = [_quantities(bundle) for bundle in bundles]
        expected = dict(quantities0)
        fill_cash = internal_cash = external_cash = Decimal(0)
        internal_by_type: dict[str, Decimal] = {}
        movements = []
        external_count = 0
        for row in new_rows.values():
            at = _dt(row["transaction_time"])
            if at < t0:
                if row["id"] not in old_rows:
                    raise ValueError("late prior-period activity requires endpoint restatement review")
                continue
            if at >= t1:
                raise ValueError("post-endpoint activity is not supported")
            kind = row["activity_type"]
            if kind == "FILL":
                qty, price = _decimal(row["qty"]), _decimal(row["price"])
                signed = qty if row["side"] == "buy" else -qty
                movement = -signed * price
                fill_cash += movement
                expected[row["symbol"]] = expected.get(row["symbol"], Decimal(0)) + signed
            elif kind in {"DIV", "FEE", "INT", "CSD", "CSW"}:
                movement = _decimal(row["net_amount"])
                if kind in {"CSD", "CSW"}:
                    if (kind == "CSD" and movement < 0) or (kind == "CSW" and movement > 0):
                        raise ValueError("external transfer direction conflicts with cash amount")
                    external_count += 1
                    external_cash += movement
                else:
                    internal_cash += movement
                    internal_by_type[kind] = internal_by_type.get(kind, Decimal(0)) + movement
            else:
                raise ValueError("unsupported economic activity")
            movements.append({"activity_id": row["id"], "activity_type": kind,
                              "transaction_time": at.isoformat(), "cash_movement": str(movement),
                              "source_sha256": evidence_hash(row)})
        cash_residual = cash1 - (cash0 + fill_cash + internal_cash + external_cash)
        qty_residuals = {symbol: quantities1.get(symbol, Decimal(0)) - expected.get(symbol, Decimal(0))
                         for symbol in set(expected) | set(quantities1)}
        result.update(
            start_market_date=start["market_date"], end_market_date=end["market_date"],
            cash_start=str(cash0), cash_end=str(cash1), fill_cash=str(fill_cash),
            internal_cash=str(internal_cash), external_net_inflow=str(external_cash),
            external_event_count=external_count, cash_residual=str(cash_residual),
            quantity_residuals={k: str(v) for k, v in sorted(qty_residuals.items())},
            internal_cash_by_type={k: str(v) for k, v in sorted(internal_by_type.items())},
            activities=sorted(movements, key=lambda row: (row["transaction_time"], row["activity_id"])),
            cash_tolerance=str(CASH_TOLERANCE), quantity_tolerance=str(QUANTITY_TOLERANCE),
        )
        if abs(cash_residual) > CASH_TOLERANCE:
            result["blockers"].append("cash_does_not_reconcile")
        if any(abs(v) > QUANTITY_TOLERANCE for v in qty_residuals.values()):
            result["blockers"].append("holdings_do_not_reconcile")
        if external_count:
            result["strict_comparison_blockers"].append("external_flows_require_subperiod_valuations")
        if not result["blockers"]:
            result["status"] = "returned_activities_reconciled"
            # Dollar wealth change, NOT a time-weighted investment return.
            result["wealth_change_less_external_flows"] = str(
                _decimal(end["reconstructed_nav"]) - _decimal(start["reconstructed_nav"]) - external_cash)
            result["cost_scope"] = "broker cash movements only; operating costs and omitted/late activities remain unverified"
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        result["blockers"].append(str(exc))
    return result
