"""Execution-local atomic ledger operations; no schema or historical migration."""
from __future__ import annotations

import json
import math
from contextlib import contextmanager
from copy import copy
from hashlib import sha256

from alphaos.util import timeutils


class _DeferredConnection:
    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def commit(self):
        # JournalStore's individual inserts commit; this scope owns the commit.
        pass


@contextmanager
def atomic(journal):
    """Use an independent journal facade without changing the shared object.

    The write lock covers read/modify/write and crash recovery. Nested execution
    scopes reuse the outer transaction. Broker requests never run in this scope.
    """
    if isinstance(journal.conn, _DeferredConnection):
        yield journal
        return
    journal.conn.execute("BEGIN IMMEDIATE")
    scoped = copy(journal)
    scoped.conn = _DeferredConnection(journal.conn)
    try:
        yield scoped
        journal.conn.commit()
    except BaseException:
        journal.conn.rollback()
        raise


def metadata(order):
    value = order.get("raw_request_json") or {}
    return json.loads(value) if isinstance(value, str) else dict(value)


def update(journal, table, key, identifier, values):
    values = {k: json.dumps(v, default=str) if k.endswith("_json") and not isinstance(v, str) else v
              for k, v in values.items()}
    journal.conn.execute(
        f"UPDATE {table} SET " + ", ".join(f"{name} = ?" for name in values) + f" WHERE {key} = ?",
        (*values.values(), identifier),
    )


def record_cumulative_fill(journal, order, norm, position_id=None):
    """Append only the newly observed quantity/notional, under an atomic scope.

    Alpaca order snapshots report cumulative filled_qty and average price. The
    difference in cumulative notional gives the new fill's actual average cost.
    """
    qty = float(norm.get("filled_qty") or 0)
    average = norm.get("filled_avg_price")
    prior = journal.one(
        "SELECT COALESCE(SUM(qty),0) qty, COALESCE(SUM(qty*price),0) notional "
        "FROM paper_fills WHERE order_id=?", (order["order_id"],),
    )
    old_qty, old_notional = float(prior["qty"]), float(prior["notional"])
    if qty < old_qty - 1e-8:
        raise ValueError("broker cumulative fill quantity moved backwards")
    if qty <= old_qty + 1e-8:
        return 0.0
    if average is None or not math.isfinite(float(average)) or float(average) <= 0:
        raise ValueError("broker fill has no valid execution price")
    delta = qty - old_qty
    price = (qty * float(average) - old_notional) / delta
    if not math.isfinite(price) or price <= 0:
        raise ValueError("broker cumulative fill implies an invalid incremental price")
    filled_at = norm.get("filled_at") or timeutils.stamp().utc
    fill_id = "fill_" + sha256(f"{order['order_id']}:{qty:.12g}".encode()).hexdigest()[:24]
    journal.insert("paper_fills", {
        "fill_id": fill_id, "order_id": order["order_id"],
        "broker_order_id": norm.get("broker_order_id"), "position_id": position_id,
        "symbol": order["symbol"], "side": order["side"], "qty": delta, "price": price,
        "execution_source": "alpaca_paper", "execution_provider": "alpaca_paper",
        "data_provider": order.get("data_provider"), "data_feed": order.get("data_feed"),
        "fill_source": "alpaca_paper", "fill_price_basis": "alpaca_fill", "filled_at": filled_at,
        "trade_id": order.get("trade_id"),
    })
    update(journal, "paper_orders", "order_id", order["order_id"], {
        "filled_at": filled_at, "entry_price": average,
        "raw_response_json": {**norm, "fill_timestamp_source": "broker" if norm.get("filled_at") else "observed"},
    })
    return delta


def exit_totals(journal, position):
    return journal.one(
        "SELECT COALESCE(SUM(qty),0) qty, COALESCE(SUM(qty*price),0) notional, "
        "MAX(filled_at) filled_at FROM paper_fills WHERE position_id=? AND side IN ('sell','buy_to_cover')",
        (position["position_id"],),
    )
