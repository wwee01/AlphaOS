"""Broker-vs-ledger reconciliation (Roadmap 1.5 hygiene).

Read-only detection of divergence between the Alpaca PAPER account and the local
ledger:
* orphan ledger positions — open in the ledger, but Alpaca has no matching position,
* orphan broker positions — held on Alpaca, but not tracked open in the ledger,
* orphan broker orders   — open on Alpaca for a symbol with no open ledger position.

Makes NO mutations — it only surfaces mismatches for an operator to resolve
(e.g. via the `flatten` command). Never touches real money.
"""

from __future__ import annotations

from alphaos.constants import ExecutionSource


def build_broker_ledger_report(journal, alpaca_client) -> dict:
    # Ledger side: open broker-managed (alpaca_paper) positions.
    ledger_positions = [
        p for p in journal.open_positions()
        if p.get("execution_source") == ExecutionSource.ALPACA_PAPER.value
    ]
    ledger_syms = {p["symbol"] for p in ledger_positions}

    broker_available = bool(alpaca_client) and getattr(alpaca_client, "is_safe_paper", False)
    broker_positions, broker_orders, broker_error = [], [], None
    if broker_available:
        try:
            broker_positions = alpaca_client.list_positions()
            broker_orders = alpaca_client.list_open_orders()
        except Exception as exc:  # pragma: no cover - network/SDK
            broker_error, broker_available = str(exc), False

    broker_syms = {p["symbol"] for p in broker_positions}

    orphan_ledger_positions = [
        {"symbol": p["symbol"], "position_id": p.get("position_id"),
         "qty": p.get("qty"), "trade_id": p.get("trade_id")}
        for p in ledger_positions if p["symbol"] not in broker_syms
    ]
    orphan_broker_positions = [
        {"symbol": p.get("symbol"), "qty": p.get("qty"), "side": p.get("side")}
        for p in broker_positions if p.get("symbol") not in ledger_syms
    ]
    orphan_broker_orders = [
        {"symbol": o.get("symbol"), "broker_order_id": o.get("broker_order_id"), "status": o.get("status")}
        for o in broker_orders if o.get("symbol") not in ledger_syms
    ]

    mismatches = len(orphan_ledger_positions) + len(orphan_broker_positions) + len(orphan_broker_orders)
    return {
        "broker_available": broker_available,
        "broker_error": broker_error,
        "ledger_open_positions": len(ledger_positions),
        "broker_open_positions": len(broker_positions),
        "broker_open_orders": len(broker_orders),
        "in_sync": broker_available and mismatches == 0,
        "mismatch_count": mismatches,
        "orphan_ledger_positions": orphan_ledger_positions,
        "orphan_broker_positions": orphan_broker_positions,
        "orphan_broker_orders": orphan_broker_orders,
        "note": (
            "broker unreachable — cannot reconcile (paper not connected)" if not broker_available
            else ("ledger and Alpaca paper are in sync" if mismatches == 0
                  else f"{mismatches} mismatch(es) — see orphan lists; consider the `flatten` command")
        ),
    }
