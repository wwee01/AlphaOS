"""Shared order/event schema for mock AND Alpaca-paper execution.

Both execution sources build their ``paper_orders`` row through
``build_order_row`` so the journal is byte-for-byte identical in shape. The only
fields that legitimately differ are ``execution_source``, ``broker_order_id``,
and the raw request/response blobs.
"""

from __future__ import annotations

from typing import Optional

from alphaos.constants import TradeDirection

# The canonical set of columns a paper_orders row carries (excluding the DB id
# and the auto-stamped created_at_* columns). Used by tests to prove parity.
ORDER_ROW_FIELDS = [
    "order_id",
    "client_order_id",
    "broker_order_id",
    "proposal_id",
    "candidate_id",
    "symbol",
    "direction",
    "side",
    "order_type",
    "qty",
    "limit_price",
    "entry_price",
    "take_profit_price",
    "stop_loss_price",
    "time_in_force",
    "execution_source",
    "execution_provider",
    "execution_mode",
    "data_provider",
    "data_feed",
    "fill_price_basis",
    "protection_path",
    "state",
    "requires_margin",
    "is_short",
    "strategy",
    "is_demo",
    "submitted_at",
    "accepted_at",
    "filled_at",
    "raw_request_json",
    "raw_response_json",
]


def side_for_entry(direction: str) -> str:
    return "sell_short" if direction == TradeDirection.SHORT.value else "buy"


def side_for_exit(direction: str) -> str:
    return "buy_to_cover" if direction == TradeDirection.SHORT.value else "sell"


def build_order_row(
    *,
    order_id: str,
    proposal,
    side: str,
    order_type: str,
    execution_source: str,
    protection_path: Optional[str],
    state: str,
    qty: float,
    entry_price: Optional[float] = None,
    take_profit_price: Optional[float] = None,
    stop_loss_price: Optional[float] = None,
    limit_price: Optional[float] = None,
    time_in_force: str = "day",
    execution_provider: str = "simulated_internal",
    execution_mode: str = "internal_simulation",
    data_provider: Optional[str] = None,
    data_feed: Optional[str] = None,
    fill_price_basis: Optional[str] = None,
    broker_order_id: Optional[str] = None,
    client_order_id: Optional[str] = None,
    raw_request: Optional[dict] = None,
    raw_response: Optional[dict] = None,
    submitted_at: Optional[str] = None,
    accepted_at: Optional[str] = None,
    filled_at: Optional[str] = None,
) -> dict:
    """Build a single paper_orders row, identical in shape across sources."""
    return {
        "order_id": order_id,
        "client_order_id": client_order_id,
        "broker_order_id": broker_order_id,
        "proposal_id": getattr(proposal, "proposal_id", None),
        "candidate_id": getattr(proposal, "candidate_id", None),
        "symbol": proposal.symbol,
        "direction": proposal.direction,
        "side": side,
        "order_type": order_type,
        "qty": qty,
        "limit_price": limit_price,
        "entry_price": entry_price,
        "take_profit_price": take_profit_price,
        "stop_loss_price": stop_loss_price,
        "time_in_force": time_in_force,
        "execution_source": execution_source,
        "execution_provider": execution_provider,
        "execution_mode": execution_mode,
        "data_provider": data_provider,
        "data_feed": data_feed,
        "fill_price_basis": fill_price_basis,
        "protection_path": protection_path,
        "state": state,
        "requires_margin": 1 if proposal.requires_margin else 0,
        "is_short": 1 if proposal.direction == TradeDirection.SHORT.value else 0,
        "strategy": proposal.strategy,
        "is_demo": 1 if getattr(proposal, "is_demo", False) else 0,
        "submitted_at": submitted_at,
        "accepted_at": accepted_at,
        "filled_at": filled_at,
        "raw_request_json": raw_request or {},
        "raw_response_json": raw_response or {},
    }
