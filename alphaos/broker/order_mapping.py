"""Normalize Alpaca order/position objects into SDK-agnostic dicts.

Keeping the mapping here (pure functions, no SDK import) means the order
manager and reconciliation logic never touch alpaca-py types, and the lifecycle
can be tested hermetically with a fake client that returns the same shapes.
"""

from __future__ import annotations

from typing import Optional

from alphaos.constants import OrderState


def _s(value) -> Optional[str]:
    """Stringify an enum-or-str-or-None (alpaca uses enums; fakes use strings)."""
    if value is None:
        return None
    return value.value if hasattr(value, "value") else str(value)


def _f(value) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# Alpaca order status -> our lifecycle state.
_STATUS_MAP = {
    "new": OrderState.ACCEPTED,
    "accepted": OrderState.ACCEPTED,
    "pending_new": OrderState.ACCEPTED,
    "accepted_for_bidding": OrderState.ACCEPTED,
    "held": OrderState.ACCEPTED,
    "partially_filled": OrderState.PARTIALLY_FILLED,
    "filled": OrderState.FILLED,
    "done_for_day": OrderState.SUBMITTED,
    "canceled": OrderState.CANCELLED,
    "pending_cancel": OrderState.CANCELLED,
    "rejected": OrderState.REJECTED,
    "expired": OrderState.EXPIRED,
    "replaced": OrderState.REPLACED,
    "pending_replace": OrderState.REPLACED,
}


def map_status(status) -> str:
    s = (_s(status) or "").lower()
    return _STATUS_MAP.get(s, OrderState.SUBMITTED).value


def _leg_role(leg) -> str:
    otype = (_s(getattr(leg, "order_type", None)) or _s(getattr(leg, "type", None)) or "").lower()
    has_stop = getattr(leg, "stop_price", None) is not None or "stop" in otype
    has_limit = getattr(leg, "limit_price", None) is not None and "limit" in otype
    if has_stop:
        return "stop_loss"
    if has_limit:
        return "take_profit"
    return "other"


def normalize_leg(leg) -> dict:
    return {
        "broker_order_id": _s(getattr(leg, "id", None)),
        "role": _leg_role(leg),
        "status": _s(getattr(leg, "status", None)),
        "state": map_status(getattr(leg, "status", None)),
        "filled_qty": _f(getattr(leg, "filled_qty", None)),
        "filled_avg_price": _f(getattr(leg, "filled_avg_price", None)),
        "limit_price": _f(getattr(leg, "limit_price", None)),
        "stop_price": _f(getattr(leg, "stop_price", None)),
        "time_in_force": _s(getattr(leg, "time_in_force", None)),
    }


def normalize_order(order) -> dict:
    legs = getattr(order, "legs", None) or []
    return {
        "broker_order_id": _s(getattr(order, "id", None)),
        "client_order_id": _s(getattr(order, "client_order_id", None)),
        "symbol": _s(getattr(order, "symbol", None)),
        "side": _s(getattr(order, "side", None)),
        "qty": _f(getattr(order, "qty", None)),
        "order_class": _s(getattr(order, "order_class", None)),
        "status": _s(getattr(order, "status", None)),
        "state": map_status(getattr(order, "status", None)),
        "filled_qty": _f(getattr(order, "filled_qty", None)),
        "filled_avg_price": _f(getattr(order, "filled_avg_price", None)),
        "limit_price": _f(getattr(order, "limit_price", None)),
        "stop_price": _f(getattr(order, "stop_price", None)),
        "submitted_at": _s(getattr(order, "submitted_at", None)),
        "filled_at": _s(getattr(order, "filled_at", None)),
        "time_in_force": _s(getattr(order, "time_in_force", None)),
        "legs": [normalize_leg(leg) for leg in legs],
    }


def normalize_position(pos) -> dict:
    return {
        "symbol": _s(getattr(pos, "symbol", None)),
        "qty": _f(getattr(pos, "qty", None)),
        "side": _s(getattr(pos, "side", None)),
        "avg_entry_price": _f(getattr(pos, "avg_entry_price", None)),
        "market_value": _f(getattr(pos, "market_value", None)),
        "unrealized_pl": _f(getattr(pos, "unrealized_pl", None)),
        "current_price": _f(getattr(pos, "current_price", None)),
    }
