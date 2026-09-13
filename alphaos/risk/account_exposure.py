"""Fresh paper-account bounds. Unknown state never becomes free buying capacity."""
from __future__ import annotations

import math
from contextlib import contextmanager
from pathlib import Path

from alphaos.execution.protection_contract import verified_live_roles
from alphaos.util import timeutils


class ExposureUnavailable(ValueError):
    pass


def _number(value, label, *, positive=False):
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ExposureUnavailable(f"missing {label}") from None
    if not math.isfinite(value) or (positive and value <= 0):
        raise ExposureUnavailable(f"invalid {label}")
    return value


def summarize_exposure(account: dict, positions: list[dict], orders: list[dict], known_entries: dict, known_reductions: set | None = None) -> dict:
    known_reductions = known_reductions or set()
    equity = _number(account.get("equity"), "account equity", positive=True)
    last_equity = _number(account.get("last_equity"), "previous closing equity", positive=True)
    if account.get("trading_blocked") is not False:
        raise ExposureUnavailable("account trading permission is blocked or unknown")
    by_symbol = {}
    gross = 0.0
    for pos in positions:
        symbol = pos.get("symbol")
        qty = abs(_number(pos.get("qty"), "position quantity"))
        if not symbol or not qty or symbol in by_symbol:
            raise ExposureUnavailable("invalid or duplicate broker position")
        if pos.get("side") not in {"long", "short"}:
            raise ExposureUnavailable("unknown position side")
        value = abs(_number(pos.get("market_value"), "position market value"))
        if value <= 0:
            raise ExposureUnavailable("nonzero position has no market value")
        gross += value
        by_symbol[symbol] = pos
    pending_slots = 0
    pending = 0.0
    seen = set()
    for order in orders:
        oid = order.get("broker_order_id")
        if not oid or oid in seen:
            raise ExposureUnavailable("missing or duplicate open order identity")
        seen.add(oid)
        symbol = order.get("symbol")
        qty = _number(order.get("qty"), "open order quantity", positive=True)
        filled = _number(order.get("filled_qty"), "open order filled quantity")
        if not 0 <= filled <= qty:
            raise ExposureUnavailable("invalid cumulative fill quantity")
        remaining = qty - filled
        if not remaining:
            continue
        if oid in known_entries:
            row = known_entries[oid]
            if symbol != row["symbol"]:
                raise ExposureUnavailable("entry order symbol differs from ledger")
            # Limit entry remains the worst authorized notional per share.
            price = _number(order.get("limit_price"), "pending entry limit", positive=True)
            pending += remaining * price
            if filled == 0 or symbol not in by_symbol:
                pending_slots += 1
        else:
            # Only verified, reducing stop/target orders consume no new exposure.
            held = by_symbol.get(symbol)
            expected_side = "sell" if held and held.get("side") == "long" else "buy"
            if (oid not in known_reductions or not held or order.get("side") != expected_side
                    or remaining > abs(float(held["qty"]))):
                raise ExposureUnavailable("unrecognized open order could increase exposure")
            if not (order.get("stop_price") or order.get("limit_price")):
                raise ExposureUnavailable("unreconciled independent close order")
    return {"equity": equity, "gross_committed_notional": gross + pending,
            "open_positions": len(by_symbol) + pending_slots,
            "broker_position_slots": len(by_symbol), "pending_entry_slots": pending_slots,
            "account_pnl_today": equity - last_equity,
            "pending_entry_notional": pending, "observed_at_utc": timeutils.stamp().utc}


def read_exposure(client, journal) -> dict:
    if client is None:
        raise ExposureUnavailable("paper broker is unavailable")
    account = client.get_account()
    positions = client.list_positions()
    orders = client.list_open_orders()
    refreshed_positions = client.list_positions()
    def quantities(rows):
        return sorted((r.get("symbol"), r.get("side"), r.get("qty")) for r in rows)
    if quantities(positions) != quantities(refreshed_positions):
        raise ExposureUnavailable("broker position changed during exposure check; retry after reconciliation")
    positions = refreshed_positions
    local_positions = journal.open_positions()
    local_by_symbol = {p["symbol"]: p for p in local_positions}
    known_reductions = set()
    for held in positions:
        local = local_by_symbol.get(held.get("symbol"))
        if not local or abs(float(local["qty"])) != abs(float(held["qty"])):
            raise ExposureUnavailable("broker position is not reconciled to the local ledger")
        if not local.get("broker_order_id"):
            raise ExposureUnavailable("position lacks broker protection lineage")
        parent = client.get_order(local["broker_order_id"])
        expected_side = "sell" if held["side"] == "long" else "buy"
        if parent.get("order_class") == "oco":
            known_reductions.add(parent["broker_order_id"])
        for leg in parent.get("legs", []):
            if leg.get("role") in {"stop_loss", "take_profit"}:
                known_reductions.add(leg["broker_order_id"])
        live_roles = verified_live_roles(parent, abs(float(held["qty"])), held["symbol"], expected_side)
        if not {"stop_loss", "take_profit"} <= live_roles:
            raise ExposureUnavailable("current matching stop and target protection is not verified")
    known = {r["broker_order_id"]: r for r in journal.query(
        "SELECT broker_order_id, symbol FROM paper_orders WHERE proposal_id IS NOT NULL "
        "AND broker_order_id IS NOT NULL AND order_class = 'bracket'"
    )}
    result = summarize_exposure(account, positions, orders, known, known_reductions)
    # A filled but not yet reconciled position does not erase a slot; broker
    # reality covers missing local fills, local records cover unresolved exits.
    result["open_positions"] = max(result["broker_position_slots"], journal.count_open_positions()) + result["pending_entry_slots"]
    return result


@contextmanager
def entry_submission_lock(journal):
    """Serialize the final fresh check and submission across local writers.

    A file lock releases on crash. Do not hold a SQLite write lock during a
    broker request. Only new-entry callers use this, never protective exits.
    """
    import fcntl
    if journal.db_path in {":memory:", ""}:
        raise ExposureUnavailable("paper entry submission requires a durable ledger")
    path = Path(journal.db_path).resolve().with_suffix(".entry.lock")
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ExposureUnavailable("another entry submission is in progress") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
