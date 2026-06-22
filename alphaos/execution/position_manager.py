"""Position manager.

Opens positions from fills, runs the stop/target/time watchdog over open
positions, and closes them — recording exits, outcomes, the same-day
classification, and a simulated exit order so the lifecycle stays complete.

Costs are recorded as a field but modelled as 0.0 in v1 (a documented gap);
net_pnl therefore equals gross_pnl for now. MFE/MAE are exit-time approximations
until intra-trade path tracking is added.
"""

from __future__ import annotations

from typing import Optional

from alphaos.constants import (
    ExecutionSource,
    OrderState,
    Severity,
    TradeDirection,
    target_profile_bundle,
)
from alphaos.constants import ExecutionProvider
from alphaos.data.freshness_guard import FreshnessGuard
from alphaos.execution import exit_rules
from alphaos.execution.costs import CostModel
from alphaos.util import timeutils
from alphaos.util.ids import new_id

FILL_PRICE_BASIS = "latest_quote_or_bar"


class PositionManager:
    def __init__(self, settings, journal, market_data=None):
        self.settings = settings
        self.journal = journal
        self._market = market_data  # lazily created in monitor() if needed
        self.freshness = FreshnessGuard.from_settings(settings)
        self.cost_model = CostModel.from_settings(settings)

    def _data_labels(self) -> tuple[str, str]:
        provider = "alpaca_mock" if self.settings.offline_mode else "alpaca"
        return provider, self.settings.market_data_feed

    # -------------------------------------------------------------- open
    def open_position(self, order_row: dict, fill_price: float) -> str:
        proposal = self.journal.proposal_by_id(order_row.get("proposal_id")) or {}
        position_id = new_id("pos")
        self.journal.insert(
            "positions",
            {
                "position_id": position_id,
                "order_id": order_row["order_id"],
                "symbol": order_row["symbol"],
                "direction": order_row["direction"],
                "strategy": order_row.get("strategy"),
                "qty": order_row["qty"],
                "avg_entry_price": fill_price,
                "stop_price": order_row.get("stop_loss_price"),
                "target_price": order_row.get("take_profit_price"),
                "max_holding_days": proposal.get("max_holding_days"),
                "opened_at": timeutils.now_utc().isoformat(),
                "opened_market_date": timeutils.market_date().isoformat(),
                "status": "open",
                "current_price": fill_price,
                "unrealized_pnl": 0.0,
                "execution_source": order_row.get("execution_source"),
                "broker_order_id": order_row.get("broker_order_id"),
                "is_short": order_row.get("is_short", 0),
                "requires_margin": proposal.get("requires_margin", 0),
                "is_demo": order_row.get("is_demo", 0),
                # Target-profile evidence relayed from the proposal (tracking only).
                **target_profile_bundle(proposal),
            },
        )
        return position_id

    # ------------------------------------------------------------- monitor
    def monitor(self, price_overrides: Optional[dict] = None) -> list[dict]:
        """Watchdog pass over open positions. Returns the list of exits made.

        ``price_overrides`` (symbol -> price) lets callers/tests drive prices;
        otherwise live/mock market data is used. Stale data never triggers an
        exit — it is logged and skipped.
        """
        exits: list[dict] = []
        # Broker-managed (alpaca_paper) positions have their exits handled by the
        # Alpaca bracket OCO and are reconciled separately — the local watchdog
        # only manages simulated_internal positions, so it never double-exits.
        open_positions = [
            p for p in self.journal.open_positions()
            if p.get("execution_source") != ExecutionSource.ALPACA_PAPER.value
        ]
        if not open_positions:
            return exits

        market = None
        if price_overrides is None:
            market = self._market
            if market is None:
                from alphaos.data.market_data import MarketDataClient

                market = MarketDataClient(self.settings, self.journal)

        for pos in open_positions:
            sym = pos["symbol"]
            if price_overrides is not None and sym in price_overrides:
                price = float(price_overrides[sym])
            else:
                snap = market.get_snapshot(sym)
                report = self.freshness.assess(snap)
                if not report.is_usable:
                    self.journal.log_system_event(
                        Severity.WARNING, "monitor",
                        f"Skipping {sym}: data {report.freshness_status}; will not exit on bad data.",
                    )
                    continue
                price = snap.get("last_price")
            if price is None:
                continue

            decision = self._check_exit(pos, price)
            if decision is not None:
                reason = decision
                ex = self.close_position(pos["position_id"], price, reason, triggered_by="watchdog")
                if ex:
                    exits.append(ex)
            else:
                self._mark_to_market(pos, price)
        return exits

    def _check_exit(self, pos: dict, price: float) -> Optional[str]:
        direction = pos["direction"]
        stop = pos.get("stop_price")
        target = pos.get("target_price")
        if direction == TradeDirection.SHORT.value:
            if stop is not None and price >= stop:
                return "stop"
            if target is not None and price <= target:
                return "target"
        else:
            if stop is not None and price <= stop:
                return "stop"
            if target is not None and price >= target:
                return "target"
        # Time expiry (swing horizon). Day-trade experiment uses max_holding_days=0.
        max_days = pos.get("max_holding_days")
        if max_days and max_days > 0:
            opened = timeutils.parse_iso(pos.get("opened_at"))
            if opened is not None:
                held = (timeutils.now_utc() - opened).total_seconds() / 86400.0
                if held >= max_days:
                    return "time_expiry"
        return None

    def _mark_to_market(self, pos: dict, price: float) -> None:
        qty = pos["qty"] or 0
        entry = pos["avg_entry_price"] or 0
        if pos["direction"] == TradeDirection.SHORT.value:
            upnl = (entry - price) * qty
        else:
            upnl = (price - entry) * qty
        self.journal.conn.execute(
            "UPDATE positions SET current_price = ?, unrealized_pnl = ? WHERE position_id = ?",
            (price, round(upnl, 2), pos["position_id"]),
        )
        self.journal.conn.commit()

    # --------------------------------------------------------------- close
    def close_position(
        self,
        position_id: str,
        exit_price: float,
        exit_reason: str,
        triggered_by: str = "system",
        execution_source: Optional[str] = None,
        broker_order_id: Optional[str] = None,
    ) -> Optional[dict]:
        pos = self.journal.one("SELECT * FROM positions WHERE position_id = ?", (position_id,))
        if not pos or pos["status"] != "open":
            return None

        qty = float(pos["qty"] or 0)
        entry = float(pos["avg_entry_price"] or 0)
        direction = pos["direction"]
        is_short = direction == TradeDirection.SHORT.value
        pnl_per_share = (entry - exit_price) if is_short else (exit_price - entry)
        gross_pnl = round(pnl_per_share * qty, 2)
        costs = self.cost_model.costs(qty, entry, exit_price).total
        net_pnl = round(gross_pnl - costs, 2)
        risk_per_share = abs(entry - (pos["stop_price"] or entry)) or None
        realized_r = round(pnl_per_share / risk_per_share, 3) if risk_per_share else None
        return_pct = round((pnl_per_share / entry), 4) if entry else None

        opened = timeutils.parse_iso(pos.get("opened_at"))
        holding_days = (
            round((timeutils.now_utc() - opened).total_seconds() / 86400.0, 4) if opened else None
        )
        same_day = exit_rules.is_same_day_exit(pos.get("opened_market_date"))
        classification = exit_rules.classify_exit(exit_reason, net_pnl)

        # Record the exit order. For broker-reconciled (alpaca_paper) closes this
        # represents the real Alpaca leg fill; otherwise it's an internal sim exit.
        exit_order_id = self._record_exit_order(
            pos, exit_price, exit_reason,
            execution_source=execution_source or ExecutionSource.INTERNAL_SIM.value,
            broker_order_id=broker_order_id,
        )

        exit_id = new_id("exit")
        self.journal.insert(
            "exits",
            {
                "exit_id": exit_id,
                "position_id": position_id,
                "order_id": pos.get("order_id"),
                "exit_order_id": exit_order_id,
                "symbol": pos["symbol"],
                "exit_price": exit_price,
                "qty": qty,
                "exit_reason": exit_reason,
                "classification": classification.value,
                "is_same_day": 1 if same_day else 0,
                "triggered_by": triggered_by,
                "market_date": timeutils.market_date().isoformat(),
                "target_profile": target_profile_bundle(pos)["target_profile"],
            },
        )

        mfe = max(0.0, return_pct) if return_pct is not None else None
        mae = min(0.0, return_pct) if return_pct is not None else None
        self.journal.insert(
            "trade_outcomes",
            {
                "outcome_id": new_id("out"),
                "position_id": position_id,
                "symbol": pos["symbol"],
                "direction": direction,
                "strategy": pos.get("strategy"),
                "entry_price": entry,
                "exit_price": exit_price,
                "qty": qty,
                "gross_pnl": gross_pnl,
                "costs": costs,
                "net_pnl": net_pnl,
                "return_pct": return_pct,
                "realized_r": realized_r,
                "holding_days": holding_days,
                "is_same_day": 1 if same_day else 0,
                "classification": classification.value,
                "mfe": mfe,
                "mae": mae,
                "win": 1 if net_pnl > 0 else 0,
                # Target-profile evidence relayed from the position (tracking only).
                **target_profile_bundle(pos),
            },
        )

        self.journal.conn.execute(
            "UPDATE positions SET status = 'closed', current_price = ?, unrealized_pnl = 0 WHERE position_id = ?",
            (exit_price, position_id),
        )
        self.journal.conn.commit()

        self.journal.log_system_event(
            Severity.INFO, "execution",
            f"Closed {pos['symbol']} @ {exit_price} ({exit_reason}/{classification.value}, "
            f"net {net_pnl}, same_day={same_day}).",
            {"position_id": position_id, "exit_id": exit_id},
        )
        return {
            "exit_id": exit_id,
            "position_id": position_id,
            "symbol": pos["symbol"],
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "classification": classification.value,
            "is_same_day": same_day,
            "net_pnl": net_pnl,
            "realized_r": realized_r,
        }

    def _record_exit_order(self, pos: dict, exit_price: float, exit_reason: str,
                           execution_source: Optional[str] = None,
                           broker_order_id: Optional[str] = None) -> str:
        from alphaos.execution.order_schema import side_for_exit

        order_id = new_id("ord")
        side = side_for_exit(pos["direction"])
        st = timeutils.stamp()
        src = execution_source or ExecutionSource.INTERNAL_SIM.value
        is_real = src == ExecutionSource.ALPACA_PAPER.value
        exec_provider = "alpaca_paper" if is_real else ExecutionProvider.SIMULATED_INTERNAL.value
        exec_mode = "alpaca_paper" if is_real else "internal_simulation"
        fill_source = "alpaca_paper" if is_real else "internal_sim"
        data_provider, data_feed = self._data_labels()
        self.journal.insert(
            "paper_orders",
            {
                "order_id": order_id,
                "broker_order_id": broker_order_id or new_id("sim"),
                "proposal_id": None,
                "candidate_id": None,
                "symbol": pos["symbol"],
                "direction": pos["direction"],
                "side": side,
                "order_type": "market",
                "qty": pos["qty"],
                "entry_price": exit_price,
                "execution_source": src,
                "execution_provider": exec_provider,
                "execution_mode": exec_mode,
                "data_provider": data_provider,
                "data_feed": data_feed,
                "fill_price_basis": "alpaca_fill" if is_real else FILL_PRICE_BASIS,
                "protection_path": None,
                "state": OrderState.FILLED.value,
                "is_short": pos.get("is_short", 0),
                "strategy": pos.get("strategy"),
                "is_demo": pos.get("is_demo", 0),
                "target_profile": target_profile_bundle(pos)["target_profile"],
                "filled_at": st.utc,
                "raw_response_json": {"simulated": not is_real, "exit_reason": exit_reason, "fill_source": fill_source},
            },
            mirror=True,
        )
        self.journal.insert(
            "order_events",
            {
                "event_id": new_id("oev"),
                "order_id": order_id,
                "prev_state": OrderState.SUBMITTED.value,
                "new_state": OrderState.FILLED.value,
                "execution_source": src,
                "message": f"exit fill ({exit_reason})",
            },
            mirror=True,
        )
        self.journal.insert(
            "paper_fills",
            {
                "fill_id": new_id("fill"),
                "order_id": order_id,
                "symbol": pos["symbol"],
                "side": side,
                "qty": pos["qty"],
                "price": exit_price,
                "execution_source": src,
                "execution_provider": exec_provider,
                "data_provider": data_provider,
                "data_feed": data_feed,
                "fill_source": fill_source,
                "fill_price_basis": "alpaca_fill" if is_real else FILL_PRICE_BASIS,
                "filled_at": st.utc,
            },
            mirror=True,
        )
        return order_id
