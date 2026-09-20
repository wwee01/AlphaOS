"""Versioned read-time gross/net accounting. Never writes or repairs source rows."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

VERSION = "trade_accounting_v1"


def _number(row, key):
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"missing_or_invalid_{key}")
    return float(value)


def _json(value):
    try:
        result = json.loads(value) if isinstance(value, str) else value
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def source_sha256(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def corrected_attribution_basis(row):
    """Retain the original claim; correct its label without changing its number."""
    original = row.get("r_basis")
    corrected = {"realized_net": "realized_gross", "net_vs_gross": "gross_vs_gross"}.get(original, original)
    return {"version": VERSION, "stored_basis": original, "corrected_basis": corrected,
            "stored_attribution_version": row.get("attribution_version"),
            "correction_required": original != corrected,
            "reason": "trade_outcomes.realized_r is before modeled costs" if original != corrected else None}


def build_trade_accounting(outcome, position, orders, fills):
    """Recompute both R values only from complete, consistent ledger evidence.

    No inferred risk denominator from rounded realized_r. Missing costs, risk,
    fills or mismatched links mean unavailable/invalid, never zero-cost profit.
    Stored fees are not broker-invoice verification. Legacy execution timestamps
    remain unverified unless an explicit broker cumulative receipt is present.
    Even then it is the reported cumulative order fill time, not a claim of
    exact individual increment execution time.
    """
    result: dict[str, Any] = {"version": VERSION, "status": "unavailable", "gross_r": None, "net_r": None,
              "stored_realized_r": (outcome or {}).get("realized_r"), "stored_realized_r_basis": "gross",
              "risk_dollars": None, "blockers": [], "source_sha256": None,
              "modeled_costs": (outcome or {}).get("costs"),
              "cost_basis": "ledger_modeled_trade_costs", "broker_fees_verified": False,
              "fill_timestamps": [], "lessons_generated": False}
    if not outcome or not position:
        result["blockers"] = ["closed_outcome_and_position_required"]
        return result
    try:
        result["source_sha256"] = source_sha256({"outcome": outcome, "position": position,
                                                "orders": orders, "fills": fills})
        if (outcome.get("position_id") != position.get("position_id") or
                outcome.get("symbol") != position.get("symbol")):
            raise ValueError("outcome_position_mismatch")
        if outcome.get("direction") not in {"long", "short"} or outcome.get("direction") != position.get("direction"):
            raise ValueError("direction_mismatch")
        qty, entry, exit_price = (_number(outcome, key) for key in ("qty", "entry_price", "exit_price"))
        stop = _number(position, "stop_price")
        entry_order: dict[str, Any] = next((row for row in orders if row.get("order_id") == position.get("order_id")), {})
        original_stop = _number(entry_order, "stop_loss_price")
        if not math.isclose(original_stop, stop, rel_tol=1e-12, abs_tol=1e-8):
            raise ValueError("position_stop_differs_from_original_entry_order")
        costs, gross, net = (_number(outcome, key) for key in ("costs", "gross_pnl", "net_pnl"))
        if min(qty, entry, exit_price, stop) <= 0 or costs < 0:
            raise ValueError("nonpositive_quantity_price_or_negative_cost")
        risk = abs(entry - stop) * qty
        if risk <= 0 or not math.isfinite(risk):
            raise ValueError("zero_original_risk")
        expected_gross = (exit_price - entry) * qty * (-1 if outcome["direction"] == "short" else 1)
        if not math.isclose(gross, round(expected_gross, 2), abs_tol=1e-8) or not math.isclose(net, round(gross-costs, 2), abs_tol=1e-8):
            raise ValueError("gross_cost_net_mismatch")
        if not math.isclose(_number(outcome, "realized_r"), round(expected_gross / risk, 3), abs_tol=1e-8):
            raise ValueError("stored_gross_r_risk_mismatch")
        by_order = {row["order_id"]: row for row in orders}
        entry_id = position.get("order_id")
        entry_side = "sell" if outcome["direction"] == "short" else "buy"
        exit_side = "buy_to_cover" if outcome["direction"] == "short" else "sell"
        entries, exits = [], []
        for fill in fills:
            order = by_order.get(fill.get("order_id"))
            if (not order or fill.get("position_id") != position.get("position_id") or
                    fill.get("symbol") != outcome.get("symbol") or order.get("symbol") != outcome.get("symbol")):
                raise ValueError("fill_order_position_mismatch")
            if fill.get("order_id") == entry_id and fill.get("side") == entry_side:
                entries.append(fill)
            elif fill.get("order_id") != entry_id and fill.get("side") == exit_side:
                exits.append(fill)
            else:
                raise ValueError("fill_direction_or_role_mismatch")
            receipt = _json(fill.get("raw_broker_fill_reference"))
            matching_receipt = False
            if receipt.get("version") == "broker_cumulative_fill_v1":
                try:
                    matching_receipt = (
                        bool(fill.get("broker_order_id")) and
                        receipt.get("broker_order_id") == fill.get("broker_order_id") and
                        receipt.get("broker_reported_filled_at") == fill.get("filled_at") and
                        receipt.get("observed_at_utc") == fill.get("created_at_utc") and
                        receipt.get("timestamp_source") == "broker" and
                        math.isclose(_number(receipt, "incremental_qty"), _number(fill, "qty"), rel_tol=1e-12, abs_tol=1e-8) and
                        math.isclose(_number(receipt, "incremental_price"), _number(fill, "price"), rel_tol=1e-12, abs_tol=1e-8))
                except ValueError:
                    pass
            result["fill_timestamps"].append({
                "fill_id": fill.get("fill_id"), "ledger_filled_at": fill.get("filled_at"),
                "ledger_recorded_at_utc": fill.get("created_at_utc"),
                "broker_reported_filled_at": receipt.get("broker_reported_filled_at") if matching_receipt else None,
                "timestamp_basis": "recorded_broker_cumulative_receipt" if matching_receipt else "legacy_or_observation_unverified",
                "individual_increment_execution_time_verified": False,
            })
        for group, expected_price in ((entries, entry), (exits, exit_price)):
            group_qty = sum(_number(fill, "qty") for fill in group)
            if not group or any(_number(fill, "qty") <= 0 or _number(fill, "price") <= 0 for fill in group):
                raise ValueError("complete_positive_fills_required")
            vwap = sum(_number(fill, "qty") * _number(fill, "price") for fill in group) / group_qty
            if not math.isclose(group_qty, qty, abs_tol=1e-8) or not math.isclose(vwap, expected_price, abs_tol=1e-8):
                raise ValueError("cumulative_fills_disagree_with_outcome")
        result.update(status="verified_ledger_arithmetic", risk_dollars=risk,
                      gross_r=expected_gross/risk, net_r=net/risk,
                      gross_pnl=gross, net_pnl=net, entry_qty=qty, exit_qty=qty,
                      risk_basis="absolute_actual_entry_minus_original_order_stop_times_total_qty",
                      entry_vwap=entry, exit_vwap=exit_price,
                      recorded_fill_commissions=[fill.get("commission") for fill in fills])
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        result.update(status="invalid_or_incomplete_evidence", blockers=[str(exc)], gross_r=None, net_r=None)
    return result


def accounting_for_outcome(journal, outcome):
    """Read all cumulative entry and exit evidence, including partial-exit orders."""
    position = journal.one("SELECT * FROM positions WHERE position_id=?", (outcome["position_id"],))
    fills = journal.query("SELECT * FROM paper_fills WHERE position_id=? ORDER BY id", (outcome["position_id"],))
    order_ids = sorted({fill["order_id"] for fill in fills})
    orders = [journal.one("SELECT * FROM paper_orders WHERE order_id=?", (oid,)) for oid in order_ids]
    return build_trade_accounting(outcome, position, [row for row in orders if row], fills)
