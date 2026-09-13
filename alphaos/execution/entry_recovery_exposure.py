"""Read-only attribution for failed-entry recovery in an already held symbol.

This proves absence of exposure attributable to an intent, never absence of a
dispatch. Callers must independently prove terminal-zero or a known unsent
request, hold execution ownership, and recheck this evidence in finalization.
No transaction may span the broker reads performed here.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from copy import deepcopy

from alphaos.execution import ledger_ops
from alphaos.execution.protection_contract import verified_live_roles
from alphaos.util import timeutils

_VERSION = 1
_EPS = 1e-6
_TABLES = ("paper_orders", "positions", "paper_fills")
_IDENTIFIERS = ("order_id", "position_id", "broker_order_id", "client_order_id", "proposal_id", "trade_id")
_ORDER_FIELDS = ("broker_order_id", "client_order_id", "symbol", "side", "qty", "filled_qty",
                 "filled_avg_price", "order_class", "status", "limit_price", "stop_price", "time_in_force")


def _number(value, *, positive=False):
    try:
        result = float(value)
    except (ValueError, TypeError):
        raise ValueError("invalid quantity in entry recovery evidence") from None
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError("invalid quantity in entry recovery evidence")
    return result


def _same(a, b):
    return abs(_number(a) - _number(b)) <= _EPS


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _snapshot(journal, scope):
    """Include the symbol and linked rows, even if a linked row has a bad symbol."""
    rows = {table: journal.query(f"SELECT * FROM {table} ORDER BY id") for table in _TABLES}
    ids = {key: set(scope.get(key, [])) for key in _IDENTIFIERS}
    chosen = {table: {} for table in _TABLES}
    changed = True
    while changed:
        changed = False
        for table, items in rows.items():
            for item in items:
                if item["id"] in chosen[table]:
                    continue
                if item.get("symbol") != scope["symbol"] and not any(
                        item.get(key) and item[key] in ids[key] for key in _IDENTIFIERS):
                    continue
                chosen[table][item["id"]] = item
                for key in _IDENTIFIERS:
                    if item.get(key):
                        ids[key].add(item[key])
                changed = True
    return {table: sorted(items.values(), key=lambda item: item["id"]) for table, items in chosen.items()}


def _order_view(order):
    if not isinstance(order, dict):
        raise ValueError("broker order evidence is unavailable")
    legs = order.get("legs") or []
    if not isinstance(legs, list) or any(not isinstance(leg, dict) for leg in legs):
        raise ValueError("broker order legs are unavailable")
    return {**{key: order.get(key) for key in _ORDER_FIELDS},
            "legs": sorted([{**{key: leg.get(key) for key in _ORDER_FIELDS}, "role": leg.get("role")}
                            for leg in legs], key=lambda leg: str(leg.get("broker_order_id")))}


def _position_view(rows, symbol):
    if not isinstance(rows, list):
        raise ValueError("broker position evidence is unavailable")
    found = None
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not row.get("symbol") or row["symbol"] in seen:
            raise ValueError("broker position evidence is unavailable or duplicated")
        seen.add(row["symbol"])
        qty = _number(row.get("qty"))
        if row["symbol"] != symbol:
            continue
        side = row.get("side")
        if abs(qty) > _EPS:
            if side not in {"long", "short"} or (qty < 0 and side != "short"):
                raise ValueError("broker position direction is unverifiable")
            found = {"symbol": symbol, "qty": abs(qty), "direction": side}
    return found or {"symbol": symbol, "qty": 0.0, "direction": None}


def _assert_balance(view, qty, direction):
    if not _same(view["qty"], qty) or (qty > _EPS and view["direction"] != direction):
        raise ValueError("broker still has exposure for this symbol that is not fully attributable to other ledger positions")


def verify_entry_absence(journal, broker, row, *, terminal_order=None):
    """Return auditable, read-only evidence; raise ValueError on any ambiguity.

    The caller owns failure/unsent proof and a final fresh failed-parent read.
    Our two position reads bracket other holdings' fresh order/protection reads.
    A final order re-read detects fills or protection changes during collection.
    """
    if journal.conn.in_transaction:
        raise ValueError("entry recovery broker reads cannot run inside a transaction")
    target = journal.order_by_id(row["order_id"])
    if not target or not target.get("symbol") or not target.get("client_order_id"):
        raise ValueError("entry recovery intent identity is unavailable")
    if target.get("execution_source") != "alpaca_paper" or ledger_ops.metadata(target).get("purpose") != "paper_entry":
        raise ValueError("entry recovery requires a durable paper entry intent")
    for key in ("order_id", "client_order_id", "symbol"):
        if row.get(key) != target.get(key):
            raise ValueError("entry intent changed before exposure verification")
    failed_ids = {target["broker_order_id"]} if target.get("broker_order_id") else set()
    if terminal_order is not None:
        if (terminal_order.get("status") not in {"rejected", "canceled", "cancelled", "expired"}
                or _number(terminal_order.get("filled_qty")) != 0
                or terminal_order.get("client_order_id") != target["client_order_id"]
                or terminal_order.get("symbol") != target["symbol"]
                or not terminal_order.get("broker_order_id")):
            raise ValueError("failed entry does not have terminal-zero evidence")
        if failed_ids and terminal_order["broker_order_id"] not in failed_ids:
            raise ValueError("failed entry broker identity changed")
        for node in [terminal_order, *(terminal_order.get("legs") or [])]:
            if not isinstance(node, dict) or _number(node.get("filled_qty")) != 0 or not node.get("broker_order_id"):
                raise ValueError("failed entry has a related fill or unverifiable leg")
            failed_ids.add(node["broker_order_id"])
    scope = {"symbol": target["symbol"], **{key: [target[key]] if target.get(key) else [] for key in _IDENTIFIERS}}
    scope["broker_order_id"] = sorted(failed_ids)
    local = _snapshot(journal, scope)
    if next((item for item in local["paper_orders"] if item["order_id"] == target["order_id"]), None) != target:
        raise ValueError("entry intent changed before exposure verification")
    fingerprint = _hash(local)
    related_keys = {key: {target[key]} if target.get(key) else set()
                    for key in ("order_id", "client_order_id", "proposal_id", "trade_id")}
    related_keys["broker_order_id"] = failed_ids

    def related(item):
        return any(item.get(key) and item[key] in values for key, values in related_keys.items())

    expanded = True
    while expanded:
        expanded = False
        for order in local["paper_orders"]:
            if related(order):
                for key, values in related_keys.items():
                    if order.get(key) and order[key] not in values:
                        values.add(order[key])
                        expanded = True
    if any(related(pos) for pos in local["positions"]):
        raise ValueError("local position evidence contradicts an unsent entry")
    if any(related(fill) for fill in local["paper_fills"]):
        raise ValueError("local fill evidence contradicts an unsent entry")
    positions = [pos for pos in local["positions"] if pos["symbol"] == target["symbol"] and pos["status"] == "open"]
    orders = {order["order_id"]: order for order in local["paper_orders"]}
    if len(orders) != len(local["paper_orders"]):
        raise ValueError("local order attribution is duplicated")
    first = _position_view(broker.list_positions(), target["symbol"])
    observed = {}

    def get_order(oid):
        if not oid or oid in failed_ids:
            raise ValueError("holding has missing or failed-intent broker lineage")
        if oid not in observed:
            value = broker.get_order(oid)
            if not isinstance(value, dict) or value.get("broker_order_id") != oid:
                raise ValueError("holding broker order identity is unverifiable")
            observed[oid] = deepcopy(value)
        return observed[oid]

    def identity(order, norm, side):
        if (order.get("execution_source") != "alpaca_paper" or norm.get("symbol") != target["symbol"]
                or order.get("symbol") != target["symbol"] or norm.get("side") != side
                or order.get("broker_order_id") != norm.get("broker_order_id")
                or not order.get("client_order_id") or order["client_order_id"] != norm.get("client_order_id")):
            raise ValueError("held order has unverifiable broker/client identity")

    allowed = {}
    required_open = set()
    attributed = []
    used_entries, used_protection = set(), set()
    used_entry_brokers, used_entry_clients, used_exit_brokers, used_positions = set(), set(), set(), set()
    total = 0.0
    direction = None
    for pos in positions:
        if pos["position_id"] in used_positions:
            raise ValueError("local position attribution is duplicated")
        used_positions.add(pos["position_id"])
        qty = _number(pos.get("qty"), positive=True)
        if pos.get("execution_source") != "alpaca_paper" or pos.get("direction") not in {"long", "short"}:
            raise ValueError("held position provenance is unverifiable")
        if direction is not None and direction != pos["direction"]:
            raise ValueError("opposing local holdings cannot prove broker net attribution")
        direction = pos["direction"]
        entry_side, exit_side = ("buy", "sell") if direction == "long" else ("sell", "buy")
        fill_entry, fill_exit = ("buy", "sell") if direction == "long" else ("sell_short", "buy_to_cover")
        entry = orders.get(pos.get("order_id"))
        if not entry or entry["order_id"] in used_entries or entry.get("side") != fill_entry:
            raise ValueError("held position has missing or multiply attributed entry lineage")
        used_entries.add(entry["order_id"])
        if any(pos.get(key) != entry.get(key) for key in ("proposal_id", "trade_id") if pos.get(key) or entry.get(key)):
            raise ValueError("held position identity disagrees with its entry")
        parent = get_order(entry.get("broker_order_id"))
        identity(entry, parent, entry_side)
        if parent["broker_order_id"] in used_entry_brokers or parent["client_order_id"] in used_entry_clients:
            raise ValueError("broker entry is multiply attributed to local positions")
        used_entry_brokers.add(parent["broker_order_id"])
        used_entry_clients.add(parent["client_order_id"])
        entry_qty = _number(entry.get("qty"), positive=True)
        if (parent.get("status") != "filled" or parent.get("order_class") != "bracket"
                or not _same(parent.get("qty"), entry_qty) or not _same(parent.get("filled_qty"), entry_qty)):
            raise ValueError("held entry is not a verified fully filled bracket")
        fills = [fill for fill in local["paper_fills"]
                 if fill.get("order_id") == entry["order_id"] or fill.get("position_id") == pos["position_id"]]
        entry_filled, exit_filled = 0.0, 0.0
        exits = defaultdict(float)
        for fill in fills:
            amount = _number(fill.get("qty"), positive=True)
            if (fill.get("execution_source") != "alpaca_paper" or fill.get("symbol") != target["symbol"]
                    or fill.get("position_id") != pos["position_id"]):
                raise ValueError("held fill attribution is unverifiable")
            if fill.get("order_id") == entry["order_id"]:
                if fill.get("side") != fill_entry or fill.get("broker_order_id") != parent["broker_order_id"]:
                    raise ValueError("held entry fill identity is unverifiable")
                entry_filled += amount
            else:
                exit_order = orders.get(fill.get("order_id"))
                meta = ledger_ops.metadata(exit_order) if exit_order else {}
                if (not exit_order or fill.get("side") != fill_exit or exit_order.get("side") != fill_exit
                        or meta.get("purpose") not in {"position_exit", "time_exit", "reprotect"}
                        or meta.get("position_id") != pos["position_id"]
                        or fill.get("broker_order_id") != exit_order.get("broker_order_id")):
                    raise ValueError("held exit fill lineage is unverifiable")
                exit_filled += amount
                exits[exit_order["order_id"]] += amount
        if not _same(entry_filled, entry_qty) or not _same(entry_filled - exit_filled, qty):
            raise ValueError("held quantity is not explained by its entry and exit fills")
        for order_id, filled in exits.items():
            exit_order = orders[order_id]
            if exit_order.get("broker_order_id") in used_exit_brokers:
                raise ValueError("broker exit fills are multiply attributed")
            used_exit_brokers.add(exit_order.get("broker_order_id"))
            norm = get_order(exit_order.get("broker_order_id"))
            # Leg-generated local exit records have no client ID. Their broker
            # ID must instead be a child of an independently known parent below.
            if (norm.get("symbol") != target["symbol"] or norm.get("side") != exit_side
                    or exit_order.get("execution_source") != "alpaca_paper"
                    or not _same(norm.get("filled_qty"), filled)):
                raise ValueError("held exit fills disagree with fresh broker evidence")
            if exit_order.get("client_order_id"):
                identity(exit_order, norm, exit_side)
            elif norm["broker_order_id"] not in {leg.get("broker_order_id") for leg in parent.get("legs", [])}:
                raise ValueError("held exit has no known broker parent lineage")
        protection_id = pos.get("broker_order_id")
        if protection_id in used_protection:
            raise ValueError("protection is multiply attributed to local positions")
        used_protection.add(protection_id)
        protection = get_order(protection_id)
        protection_order = entry
        if protection_id != parent["broker_order_id"]:
            matches = [order for order in orders.values() if order.get("broker_order_id") == protection_id]
            if len(matches) != 1:
                raise ValueError("replacement protection has no unique local lineage")
            replacement = matches[0]
            protection_order = replacement
            meta = ledger_ops.metadata(replacement)
            if (meta.get("purpose") != "reprotect" or meta.get("position_id") != pos["position_id"]
                    or meta.get("phase") != "protected" or protection.get("order_class") != "oco"):
                raise ValueError("replacement protection has unverifiable local lineage")
            identity(replacement, protection, exit_side)
        legs = protection.get("legs")
        if (not isinstance(legs, list) or len(legs) != 2 or any(not isinstance(leg, dict) for leg in legs)
                or {leg.get("role") for leg in legs} != {"take_profit", "stop_loss"}):
            raise ValueError("held protection has ambiguous leg lineage")
        if verified_live_roles(protection, qty, target["symbol"], exit_side) != {"take_profit", "stop_loss"}:
            raise ValueError("held position does not have freshly verified complete protection")
        expected_tif = protection_order.get("time_in_force")
        if expected_tif not in {"day", "gtc"} or protection.get("time_in_force") != expected_tif:
            raise ValueError("held protection time in force is unverifiable")
        if expected_tif == "day" and (pos.get("max_holding_days") is None or _number(pos["max_holding_days"]) >= 1):
            raise ValueError("held overnight protection cannot expire intraday")
        for leg in protection["legs"]:
            price_key, local_key = ("stop_price", "stop_price") if leg["role"] == "stop_loss" else ("limit_price", "target_price")
            if not _same(leg.get(price_key), pos.get(local_key)) or leg.get("time_in_force") != expected_tif:
                raise ValueError("held protection terms do not match the ledger")
            if leg["status"] != "held":
                required_open.add(leg["broker_order_id"])
        for node in [protection, *protection["legs"]]:
            oid = node["broker_order_id"]
            if oid in allowed and allowed[oid]["position_id"] != pos["position_id"]:
                raise ValueError("broker protection is multiply attributed")
            # A normalized OCO repeats its parent as the take-profit leg. Keep
            # the parent's client identity when the synthetic leg omits it.
            previous = allowed.get(oid, {}).get("order", {})
            allowed[oid] = {"position_id": pos["position_id"], "order": {
                **node, "client_order_id": node.get("client_order_id") or previous.get("client_order_id")}}
        attributed.append({"position_id": pos["position_id"], "entry_order_id": entry["order_id"],
                           "qty": qty, "direction": direction, "entry_fills": entry_filled,
                           "exit_fills": exit_filled, "protection_order_id": protection_id})
        total += qty
    _assert_balance(first, total, direction)
    open_orders = broker.list_open_orders()
    if not isinstance(open_orders, list):
        raise ValueError("broker open-order evidence is unavailable")
    seen_open = set()

    def visit(node):
        if not isinstance(node, dict) or not node.get("broker_order_id") or not node.get("symbol"):
            raise ValueError("broker open-order evidence is unavailable")
        oid = node["broker_order_id"]
        if oid in failed_ids or node.get("client_order_id") == target["client_order_id"]:
            raise ValueError("broker still has a potentially related open order")
        if node["symbol"] == target["symbol"]:
            known = allowed.get(oid)
            if not known:
                raise ValueError("broker still has a potentially related open order")
            expected = known["order"]
            if (node.get("client_order_id") and expected.get("client_order_id")
                    and node["client_order_id"] != expected["client_order_id"]):
                raise ValueError("open protection client identity changed during entry recovery")
            for key in ("symbol", "side", "status", "time_in_force"):
                if node.get(key) != expected.get(key):
                    raise ValueError("open protection changed during entry recovery")
            for key in ("qty", "filled_qty", "limit_price", "stop_price"):
                if node.get(key) is None and expected.get(key) is None:
                    continue
                if not _same(node.get(key), expected.get(key)):
                    raise ValueError("open protection changed during entry recovery")
            seen_open.add(oid)
        legs = node.get("legs") or []
        if not isinstance(legs, list):
            raise ValueError("broker open-order legs are unavailable")
        for leg in legs:
            visit(leg)

    for order in open_orders:
        visit(order)
    if required_open - seen_open:
        raise ValueError("working protection is absent from fresh open-order evidence")
    second = _position_view(broker.list_positions(), target["symbol"])
    _assert_balance(second, total, direction)
    if first != second:
        raise ValueError("broker positions changed during entry recovery")
    for oid, original in observed.items():
        if _order_view(broker.get_order(oid)) != _order_view(original):
            raise ValueError("broker fills or protection changed during entry recovery")
    if _hash(_snapshot(journal, scope)) != fingerprint:
        raise ValueError("local exposure evidence changed during entry recovery")
    return {"version": _VERSION, "observed_at_utc": timeutils.stamp().utc,
            "order_id": target["order_id"], "client_order_id": target["client_order_id"], "symbol": target["symbol"],
            "position_reads": 2, "broker_position": second, "attributed_positions": attributed,
            "broker_position_reads": [first, second],
            "related_open_orders": 0, "verified_protection_ids": sorted(allowed),
            "broker_orders": {oid: _order_view(order) for oid, order in sorted(observed.items())},
            "scope": scope, "local_fingerprint": fingerprint}


def recheck_entry_absence(journal, row, evidence):
    """Recheck only local evidence inside the caller's atomic finalization."""
    if not journal.conn.in_transaction:
        raise ValueError("entry recovery finalization requires an atomic transaction")
    if (evidence.get("version") != _VERSION or evidence.get("order_id") != row.get("order_id")
            or evidence.get("client_order_id") != row.get("client_order_id") or evidence.get("symbol") != row.get("symbol")):
        raise ValueError("entry recovery evidence does not match the intent")
    if _hash(_snapshot(journal, evidence["scope"])) != evidence.get("local_fingerprint"):
        raise ValueError("local exposure evidence changed before entry recovery finalization")
