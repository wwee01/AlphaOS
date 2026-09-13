"""Pure proof of working broker protection; display-state mappings are not proof."""
from __future__ import annotations

import math

_WORKING = {"new", "accepted", "partially_filled"}
_TOLERANCE = 1e-6


def _quantities(row):
    try:
        total, filled = float(row["qty"]), float(row["filled_qty"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (math.isfinite(total) and math.isfinite(filled) and total > 0 and 0 <= filled <= total):
        return None
    return total, total - filled


def _valid_price(value):
    try:
        price = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(price) and price > 0


def verified_live_roles(parent: dict, qty: float, symbol: str, exit_side: str) -> set[str]:
    """Return proven live roles for the current holding; unknowns fail closed.

    A held stop is a valid conditional broker order only behind an activated
    parent and a working target. Held targets, pending requests and unknown raw
    statuses never prove protection. Quantities are remaining, not original.
    """
    try:
        qty = float(qty)
    except (TypeError, ValueError):
        return set()
    if not math.isfinite(qty) or qty <= 0 or not symbol or exit_side not in {"buy", "sell"}:
        return set()
    parent_qty = _quantities(parent)
    if not parent_qty or not parent.get("broker_order_id") or parent.get("symbol") != symbol:
        return set()
    order_class = parent.get("order_class")
    if order_class == "bracket":
        entry_side = "buy" if exit_side == "sell" else "sell"
        activated = (parent.get("status") == "filled" and parent.get("side") == entry_side
                     and parent_qty[1] <= _TOLERANCE and parent_qty[0] + _TOLERANCE >= qty)
    elif order_class == "oco":
        activated = (parent.get("status") in _WORKING and parent.get("side") == exit_side
                     and abs(parent_qty[1] - qty) <= _TOLERANCE)
    else:
        activated = False
    if not activated:
        return set()
    legs = parent.get("legs") or []
    by_role = {}
    ids = set()
    for leg in legs:
        role = leg.get("role")
        if role not in {"stop_loss", "take_profit"}:
            continue
        oid = leg.get("broker_order_id")
        if not oid or oid in ids or role in by_role:
            return set()  # duplicate/missing lineage is not a valid OCO pair
        ids.add(oid)
        by_role[role] = leg
    live = set()
    valid = {}
    for role, leg in by_role.items():
        amounts = _quantities(leg)
        valid[role] = bool(amounts and abs(amounts[1] - qty) <= _TOLERANCE
                           and leg.get("symbol") == symbol and leg.get("side") == exit_side
                           and _valid_price(leg.get("stop_price" if role == "stop_loss" else "limit_price")))
        if valid[role] and leg.get("status") in _WORKING:
            live.add(role)
    stop = by_role.get("stop_loss", {})
    if valid.get("stop_loss") and stop.get("status") == "held" and "take_profit" in live:
        live.add("stop_loss")
    return live
