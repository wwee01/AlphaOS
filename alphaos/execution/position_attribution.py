"""Fresh per-origin ownership of a broker's aggregate symbol position.

Broker reads never span a write transaction. A target may cancel or reduce only
its own instructions; all sibling origins remain independently protected.
Prices use the same explicit cents formatting as AlpacaClient submission.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy

from alphaos.execution import ledger_ops
from alphaos.execution.entry_recovery_exposure import (
    _EPS, _assert_balance, _hash, _number, _order_view, _position_view, _same, _snapshot,
)
from alphaos.execution.protection_contract import verified_live_roles
from alphaos.util import timeutils

_VERSION = 1
TERMINAL = {"filled", "canceled", "cancelled", "rejected", "expired"}


def verify_symbol_attribution(journal, broker, symbol, *, target_position_id=None,
                              allowed_target_order_ids=(), allow_pending_entries=False):
    if journal.conn.in_transaction:
        raise ValueError("broker attribution cannot run inside a transaction")
    if not symbol:
        raise ValueError("missing symbol for attribution")
    scope = {"symbol": symbol}
    local = _snapshot(journal, scope)
    fingerprint = _hash(local)
    failed_ids = set()
    positions = [pos for pos in local["positions"] if pos["symbol"] == symbol and pos["status"] == "open"]
    orders = {order["order_id"]: order for order in local["paper_orders"]}
    if len(orders) != len(local["paper_orders"]):
        raise ValueError("local order attribution is duplicated")
    if target_position_id and target_position_id not in {p["position_id"] for p in positions}:
        raise ValueError("target origin is not open")
    first = _position_view(broker.list_positions(), symbol)
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
        if (order.get("execution_source") != "alpaca_paper" or norm.get("symbol") != symbol
                or order.get("symbol") != symbol or norm.get("side") != side
                or order.get("broker_order_id") != norm.get("broker_order_id")
                or not order.get("client_order_id") or order["client_order_id"] != norm.get("client_order_id")):
            raise ValueError("held order has unverifiable broker/client identity")

    owners: dict[str, str] = {}
    allowed: dict[str, dict] = {}
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
        qty = _number(pos.get("qty"))
        if qty < 0:
            raise ValueError("negative local residual quantity")
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
        own_ids = set()
        def own(norm):
            for node in [norm, *(norm.get("legs") or [])]:
                oid = node.get("broker_order_id")
                if (not oid or node.get("symbol") != symbol
                        or (oid in owners and owners[oid] != pos["position_id"])):
                    raise ValueError("broker order has missing, foreign or shared origin lineage")
                if node is not norm and node.get("side") != exit_side:
                    raise ValueError("protective leg has wrong direction")
                owners[oid] = pos["position_id"]
                own_ids.add(oid)
        own(parent)
        entry_qty = _number(entry.get("qty"), positive=True)
        if (parent.get("status") != "filled" or parent.get("order_class") != "bracket"
                or not _same(parent.get("qty"), entry_qty) or not _same(parent.get("filled_qty"), entry_qty)):
            raise ValueError("held entry is not a verified fully filled bracket")
        fills = [fill for fill in local["paper_fills"]
                 if fill.get("order_id") == entry["order_id"] or fill.get("position_id") == pos["position_id"]]
        entry_filled, exit_filled = 0.0, 0.0
        exits: dict[str, float] = defaultdict(float)
        for fill in fills:
            amount = _number(fill.get("qty"), positive=True)
            if (fill.get("execution_source") != "alpaca_paper" or fill.get("symbol") != symbol
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
        own_parents = [parent]
        for candidate in orders.values():
            meta = ledger_ops.metadata(candidate)
            if meta.get("position_id") != pos["position_id"] or meta.get("purpose") != "reprotect":
                continue
            if not candidate.get("broker_order_id"):
                if meta.get("phase") in {"submitting", "unknown", "working"}:
                    raise ValueError("replacement protection identity is unresolved")
                continue
            candidate_norm = get_order(candidate["broker_order_id"])
            identity(candidate, candidate_norm, exit_side)
            if candidate_norm.get("order_class") != "oco":
                raise ValueError("replacement protection is not OCO")
            own(candidate_norm)
            own_parents.append(candidate_norm)
        for order_id, filled in exits.items():
            exit_order = orders[order_id]
            if exit_order.get("broker_order_id") in used_exit_brokers:
                raise ValueError("broker exit fills are multiply attributed")
            used_exit_brokers.add(exit_order.get("broker_order_id"))
            norm = get_order(exit_order.get("broker_order_id"))
            # Leg-generated local exit records have no client ID. Their broker
            # ID must instead be a child of an independently known parent below.
            if (norm.get("symbol") != symbol or norm.get("side") != exit_side
                    or exit_order.get("execution_source") != "alpaca_paper"
                    or not _same(norm.get("filled_qty"), filled)):
                raise ValueError("held exit fills disagree with fresh broker evidence")
            if exit_order.get("client_order_id"):
                identity(exit_order, norm, exit_side)
            elif norm["broker_order_id"] not in own_ids:
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
            protection_order = matches[0]
            meta = ledger_ops.metadata(protection_order)
            if (meta.get("purpose") != "reprotect" or meta.get("position_id") != pos["position_id"]
                    or meta.get("phase") != "protected" or protection.get("order_class") != "oco"):
                raise ValueError("replacement protection has unverifiable local lineage")
            identity(protection_order, protection, exit_side)
        target_mode = pos["position_id"] == target_position_id
        if target_mode and not set(allowed_target_order_ids) <= own_ids:
            raise ValueError("target allowance contains foreign or unowned broker identity")
        # Terminal/canceling target protection is allowed only for this origin.
        # Siblings must retain a complete independent pair throughout.
        for owned_parent in own_parents:
            legs = owned_parent.get("legs")
            if (not isinstance(legs, list) or len(legs) != 2
                    or {leg.get("role") for leg in legs} != {"take_profit", "stop_loss"}
                    or len({leg.get("broker_order_id") for leg in legs}) != 2):
                raise ValueError("held protection has ambiguous leg lineage")
            for leg in legs:
                amount = _number(leg.get("qty"))
                filled = _number(leg.get("filled_qty"))
                if not 0 <= filled <= amount:
                    raise ValueError("invalid protective cumulative quantities")
                direct = get_order(leg["broker_order_id"])
                for key in ("broker_order_id", "symbol", "side", "status", "time_in_force"):
                    if direct.get(key) != leg.get(key):
                        raise ValueError("direct protective child differs from nested parent")
                for key in ("qty", "filled_qty", "stop_price", "limit_price"):
                    if direct.get(key) is None and leg.get(key) is None:
                        continue
                    if not _same(direct.get(key), leg.get(key)):
                        raise ValueError("direct protective child terms differ from nested parent")
                if leg.get("status") not in TERMINAL:
                    price_key, local_key = ("stop_price", "stop_price") if leg["role"] == "stop_loss" else ("limit_price", "target_price")
                    if (not _same(amount - filled, qty)
                            or not _same(leg.get(price_key), round(_number(pos.get(local_key)), 2))
                            or leg.get("time_in_force") != protection_order.get("time_in_force")):
                        raise ValueError("active protective child exceeds origin residual or changed terms")
            if owned_parent is protection and qty > _EPS and not target_mode:
                if verified_live_roles(protection, qty, symbol, exit_side) != {"take_profit", "stop_loss"}:
                    raise ValueError("sibling position lacks freshly verified complete protection")
                expected_tif = protection_order.get("time_in_force")
                if expected_tif not in {"day", "gtc"} or protection.get("time_in_force") != expected_tif:
                    raise ValueError("held protection time in force is unverifiable")
                if expected_tif == "day" and (pos.get("max_holding_days") is None or _number(pos["max_holding_days"]) >= 1):
                    raise ValueError("held overnight protection cannot expire intraday")
                for leg in legs:
                    key, local_key = ("stop_price", "stop_price") if leg["role"] == "stop_loss" else ("limit_price", "target_price")
                    if not _same(leg.get(key), round(_number(pos.get(local_key)), 2)) or leg.get("time_in_force") != expected_tif:
                        raise ValueError("held protection terms differ from submitted cent-rounded terms")
                    if leg["status"] != "held":
                        required_open.add(leg["broker_order_id"])
            for node in [owned_parent, *legs]:
                oid = node["broker_order_id"]
                is_parent_entry = oid == parent["broker_order_id"]
                if is_parent_entry:
                    continue  # a filled buy entry is not a reducing instruction
                if target_mode:
                    permit = oid in allowed_target_order_ids and qty > _EPS
                else:
                    permit = owned_parent is protection and qty > _EPS
                if not permit:
                    if node.get("status") not in TERMINAL:
                        raise ValueError("unpermitted active order reserves this origin")
                    continue
                previous = allowed.get(oid, {}).get("order", {})
                allowed[oid] = {"position_id": pos["position_id"], "order": {
                    **node, "client_order_id": node.get("client_order_id") or previous.get("client_order_id")}}
        attributed.append({"position_id": pos["position_id"], "entry_order_id": entry["order_id"],
                           "qty": qty, "direction": direction, "entry_fills": entry_filled,
                           "exit_fills": exit_filled, "protection_order_id": protection_id})
        total += qty
    pending_ids = set()
    from alphaos.execution.entry_submission import PENDING_PHASES
    represented_entries = {p.get("order_id") for p in local["positions"]}
    for candidate in orders.values():
        meta = ledger_ops.metadata(candidate)
        if (candidate.get("symbol") == symbol and candidate["order_id"] not in represented_entries
                and meta.get("purpose") == "paper_entry" and meta.get("phase") in PENDING_PHASES):
            raise ValueError("same-symbol entry intent remains unresolved")
    if allow_pending_entries:
        from alphaos.execution.entry_submission import EntrySubmission
        for candidate in orders.values():
            if candidate.get("symbol") != symbol or candidate["order_id"] in represented_entries:
                continue
            meta = ledger_ops.metadata(candidate)
            if meta.get("purpose") != "paper_entry":
                continue
            if meta.get("phase") in PENDING_PHASES:
                raise ValueError("same-symbol entry intent remains unresolved")
            if candidate.get("state") not in {"submitted", "accepted", "pending", "partially_filled", "new"}:
                continue
            norm = get_order(candidate.get("broker_order_id"))
            EntrySubmission.validate(candidate, norm, nested=True)
            if _number(norm.get("filled_qty")) != 0 or norm.get("status") not in {"new", "accepted", "pending_new", "accepted_for_bidding"}:
                raise ValueError("unreconciled pending entry fill")
            for node in [norm, *(norm.get("legs") or [])]:
                oid = node["broker_order_id"]
                if oid in owners or oid in pending_ids:
                    raise ValueError("pending entry shares another origin's broker identity")
                pending_ids.add(oid)
                allowed[oid] = {"position_id": None, "order": node}
    _assert_balance(first, total, direction)
    open_orders = broker.list_open_orders()
    if not isinstance(open_orders, list):
        raise ValueError("broker open-order evidence is unavailable")
    seen_open = set()

    def visit(node):
        if not isinstance(node, dict) or not node.get("broker_order_id") or not node.get("symbol"):
            raise ValueError("broker open-order evidence is unavailable")
        oid = node["broker_order_id"]
        if oid in failed_ids:
            raise ValueError("broker still has a potentially related open order")
        if node["symbol"] == symbol:
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
    middle = _position_view(broker.list_positions(), symbol)
    _assert_balance(middle, total, direction)
    if first != middle:
        raise ValueError("broker positions changed during origin attribution")
    for oid, original in observed.items():
        if _order_view(broker.get_order(oid)) != _order_view(original):
            raise ValueError("broker fills or protection changed during entry recovery")
    second = _position_view(broker.list_positions(), symbol)
    _assert_balance(second, total, direction)
    if first != second:
        raise ValueError("broker positions changed during origin attribution")
    if _hash(_snapshot(journal, scope)) != fingerprint:
        raise ValueError("local exposure evidence changed during entry recovery")
    return {"version": _VERSION, "observed_at_utc": timeutils.stamp().utc,
            "kind": "symbol_attribution", "symbol": symbol, "target_position_id": target_position_id,
            "position_reads": 3, "broker_position": second, "attributed_positions": attributed,
            "broker_position_reads": [first, middle, second],
            "related_open_orders": 0, "verified_protection_ids": sorted(set(allowed) - pending_ids),
            "broker_orders": {oid: _order_view(order) for oid, order in sorted(observed.items())},
            "scope": scope, "local_fingerprint": fingerprint}


def recheck_symbol_attribution(journal, evidence):
    """Reject a stale local proof under the same lock as its dependent mutation."""
    if not journal.conn.in_transaction:
        raise ValueError("origin attribution recheck requires an atomic transaction")
    if evidence.get("version") != _VERSION or evidence.get("kind") != "symbol_attribution":
        raise ValueError("invalid origin attribution evidence")
    if _hash(_snapshot(journal, evidence["scope"])) != evidence.get("local_fingerprint"):
        raise ValueError("local origin evidence changed before finalization")


def verify_exit_origin(journal, broker, pos, norm, order=None):
    """Validate an observed exit BEFORE accounting its fill to one origin.

    Aggregate balance may already have changed because this receipt is not yet
    booked. Only exact parent-child or durable reduction ownership permits it.
    """
    scope = {"symbol": pos["symbol"]}
    local = _snapshot(journal, scope)
    oid = norm.get("broker_order_id")
    side = "buy" if pos["direction"] == "short" else "sell"
    if not oid or norm.get("symbol") != pos["symbol"] or norm.get("side") != side:
        raise ValueError("exit receipt identity or direction differs from origin")
    entries = {r["order_id"]: r for r in local["paper_orders"]}
    entry = entries.get(pos["order_id"])
    if not entry or not entry.get("broker_order_id"):
        raise ValueError("exit origin has no entry lineage")
    # Prove uniqueness across the full open-origin graph before a partial
    # receipt can reduce any local quantity. Aggregate finalization is too late.
    graph: dict[str, str] = {}
    graph_parents = {}
    entry_ids, client_ids = set(), set()
    for origin in local["positions"]:
        if origin.get("symbol") != pos["symbol"] or origin.get("status") != "open":
            continue
        origin_entry = entries.get(origin.get("order_id"))
        if (not origin_entry or origin_entry["order_id"] in entry_ids
                or not origin_entry.get("client_order_id") or origin_entry["client_order_id"] in client_ids
                or origin_entry.get("symbol") != pos["symbol"]
                or origin_entry.get("execution_source") != "alpaca_paper"
                or origin.get("execution_source") != "alpaca_paper"
                or any(origin.get(key) != origin_entry.get(key) for key in ("proposal_id", "trade_id"))):
            raise ValueError("exit origin entry identity is missing or multiply attributed")
        entry_ids.add(origin_entry["order_id"])
        client_ids.add(origin_entry["client_order_id"])
        origin_parents = [origin_entry]
        for candidate in entries.values():
            meta = ledger_ops.metadata(candidate)
            if (meta.get("position_id") == origin["position_id"]
                    and meta.get("purpose") in {"reprotect", "time_exit"} and candidate.get("broker_order_id")):
                origin_parents.append(candidate)
        for origin_parent in origin_parents:
            observed = broker.get_order(origin_parent.get("broker_order_id"))
            expected_side = ("sell" if origin["direction"] == "short" else "buy") if origin_parent is origin_entry else (
                "buy" if origin["direction"] == "short" else "sell")
            if (observed.get("broker_order_id") != origin_parent.get("broker_order_id")
                    or observed.get("client_order_id") != origin_parent.get("client_order_id")
                    or observed.get("symbol") != pos["symbol"] or observed.get("side") != expected_side):
                raise ValueError("origin broker parent identity disagrees with ledger")
            graph_parents[observed["broker_order_id"]] = _order_view(observed)
            for node in [observed, *(observed.get("legs") or [])]:
                broker_id = node.get("broker_order_id")
                if not broker_id or (broker_id in graph and graph[broker_id] != origin["position_id"]):
                    raise ValueError("exit parent or child is shared by distinct origins")
                graph[broker_id] = origin["position_id"]
    if graph.get(oid) != pos["position_id"]:
        raise ValueError("exit receipt belongs to a different or unknown origin")
    owned = set()
    parents = [entry]
    for candidate in entries.values():
        meta = ledger_ops.metadata(candidate)
        if meta.get("position_id") == pos["position_id"] and meta.get("purpose") in {"reprotect", "time_exit"}:
            if candidate.get("broker_order_id"):
                parents.append(candidate)
    for parent_row in parents:
        parent = broker.get_order(parent_row["broker_order_id"])
        if _order_view(parent) != graph_parents.get(parent_row["broker_order_id"]):
            raise ValueError("exit parent lineage changed during ownership verification")
        if (parent.get("broker_order_id") != parent_row["broker_order_id"]
                or parent.get("symbol") != pos["symbol"]
                or not parent_row.get("client_order_id")
                or parent.get("client_order_id") != parent_row["client_order_id"]):
            raise ValueError("exit parent identity differs from durable origin")
        if parent_row is not entry:
            if parent.get("side") != side:
                raise ValueError("exit parent direction differs from origin")
            owned.add(parent["broker_order_id"])
        for leg in parent.get("legs") or []:
            if leg.get("symbol") != pos["symbol"] or leg.get("side") != side or not leg.get("broker_order_id"):
                raise ValueError("exit child identity differs from origin")
            owned.add(leg["broker_order_id"])
    if oid not in owned:
        raise ValueError("exit receipt has no verified origin parent")
    # A durable reduction row must itself name this origin, not a sibling.
    if order and (ledger_ops.metadata(order).get("position_id") != pos["position_id"]
                  or order.get("broker_order_id") != oid):
        raise ValueError("exit order belongs to another origin")
    fresh = broker.get_order(oid)
    for key in ("broker_order_id", "symbol", "side", "status"):
        if fresh.get(key) != norm.get(key):
            raise ValueError("exit receipt changed before booking")
    for key in ("qty", "filled_qty", "filled_avg_price"):
        if not _same(fresh.get(key), norm.get(key)):
            raise ValueError("exit cumulative receipt changed before booking")
    return {"version": _VERSION, "kind": "symbol_attribution", "scope": scope,
            "local_fingerprint": _hash(local)}
