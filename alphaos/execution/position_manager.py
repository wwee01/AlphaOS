"""Position manager.

Opens positions from fills, runs the stop/target/time watchdog over open
positions, and closes them — recording exits, outcomes, the same-day
classification, and a simulated exit order so the lifecycle stays complete.

Costs are recorded as a field but modelled as 0.0 in v1 (a documented gap);
net_pnl therefore equals gross_pnl for now. MFE/MAE are tracked intra-trade (one
``monitoring_snapshots`` row per open position per monitor pass, in R terms) and
folded into a running extremum, textbook-anchored at entry (R=0) so MFE>=0 and
MAE<=0 always; the final value at close is written onto
``trade_outcomes.mfe``/``.mae`` — see ``_fold_excursion``.
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
                # --- Trade Packet v1 traceability ---
                "trade_id": order_row.get("trade_id"),
                "candidate_id": (proposal or {}).get("candidate_id"),
                "proposal_id": (proposal or {}).get("proposal_id"),
                "eval_id": (proposal or {}).get("eval_id"),
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
        # The local watchdog owns exits for simulated_internal positions only.
        # Broker-managed (alpaca_paper) positions have their exits handled by the
        # Alpaca bracket OCO and applied via OrderManager.reconcile(); the
        # watchdog must NEVER exit them (it would fight the broker). It still
        # records an audit-only monitoring snapshot for them so the evidence chain
        # (proposal -> ... -> position -> monitor snapshot) stays complete.
        open_positions = self.journal.open_positions()
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
            broker_managed = pos.get("execution_source") == ExecutionSource.ALPACA_PAPER.value
            if price_overrides is not None and sym in price_overrides:
                price = float(price_overrides[sym])
                freshness_status = "override"
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
                freshness_status = report.freshness_status
            if price is None:
                continue

            if broker_managed:
                # Exits are owned by the broker OCO + reconcile(); never exit
                # a broker-managed position locally. Mark-to-market only.
                decision = None
                self._mark_to_market(pos, price)
            else:
                decision = self._check_exit(pos, price)
                if decision is not None:
                    ex = self.close_position(pos["position_id"], price, decision, triggered_by="watchdog")
                    if ex:
                        exits.append(ex)
                else:
                    self._mark_to_market(pos, price)
            # Audit snapshot AFTER any exit is acted on, and never allowed to
            # raise: a best-effort evidence write must never be able to suppress a
            # stop/target/time exit or abort the watchdog pass.
            try:
                self._record_monitoring_snapshot(
                    pos, price, decision, freshness_status, broker_managed=broker_managed
                )
            except Exception as exc:  # pragma: no cover - defensive (audit-only)
                try:
                    self.journal.log_system_event(
                        Severity.WARNING, "monitor",
                        f"monitoring snapshot failed for {pos.get('position_id')}; exit unaffected.",
                        {"error": str(exc)},
                    )
                except Exception:
                    pass
        return exits

    def _fold_excursion(self, position_id: str, unrealized_r: Optional[float]) -> tuple:
        """Fold one more price observation (``unrealized_r``, R terms) into the
        running MFE (max favorable) / MAE (max adverse) excursion for a position,
        using every ``monitoring_snapshots`` row recorded so far. Used both for
        the live per-pass snapshot AND at close (the exit tick counts as one more
        observation), so the closing MFE/MAE is never less complete than the
        snapshot history already collected.

        Textbook excursion semantics: the entry moment itself is an implicit
        R=0 observation, so MFE is always >= 0 and MAE is always <= 0 — a trade
        that was only ever favorable has MAE=0 (never dipped below entry), not
        a spuriously "adverse" positive value from the least-favorable-so-far
        OBSERVED point (Opus audit MEDIUM-1). Returns (None, None) only when
        R is genuinely undefined for this position (no usable stop, ever —
        stop_price is fixed for a position's life in v1, so if this
        observation has none, no prior one could have either)."""
        prior = self.journal.one(
            "SELECT MAX(mfe) AS max_mfe, MIN(mae) AS min_mae FROM monitoring_snapshots "
            "WHERE position_id = ?",
            (position_id,),
        ) or {}
        if unrealized_r is None and prior.get("max_mfe") is None:
            return None, None
        mfe_candidates = [0.0] + [v for v in (unrealized_r, prior.get("max_mfe")) if v is not None]
        mae_candidates = [0.0] + [v for v in (unrealized_r, prior.get("min_mae")) if v is not None]
        return max(mfe_candidates), min(mae_candidates)

    @staticmethod
    def _unrealized_r(pos: dict, price: float) -> tuple:
        """(unrealized_pnl, unrealized_r) at ``price`` for an open position. R is
        pnl_per_share / risk_per_share (risk_per_share = |entry - stop|); None
        when there's no usable stop to normalize against."""
        qty = float(pos.get("qty") or 0)
        entry = float(pos.get("avg_entry_price") or 0)
        is_short = pos.get("direction") == TradeDirection.SHORT.value
        unrealized_pnl = round(((entry - price) if is_short else (price - entry)) * qty, 2)
        risk_per_share = abs(entry - (pos.get("stop_price") or entry)) or None
        unrealized_r = (
            round(unrealized_pnl / (risk_per_share * qty), 4)
            if (risk_per_share and qty) else None
        )
        return unrealized_pnl, unrealized_r

    def _record_monitoring_snapshot(self, pos, price, decision, freshness_status,
                                    broker_managed: bool = False) -> None:
        """Write one monitoring_snapshots row per open position per pass (audit
        only; never influences the exit decision or mark-to-market). For
        broker-managed positions the watchdog takes no exit action, so the row is
        labelled ``broker_managed``."""
        unrealized_pnl, unrealized_r = self._unrealized_r(pos, price)
        mfe, mae = self._fold_excursion(pos["position_id"], unrealized_r)

        st = timeutils.stamp()
        self.journal.insert(
            "monitoring_snapshots",
            {
                "monitoring_snapshot_id": new_id("mon"),
                "position_id": pos["position_id"],
                "trade_id": pos.get("trade_id"),
                "symbol": pos["symbol"],
                "direction": pos.get("direction"),
                "snapshot_at_utc": st.utc,
                "snapshot_at_sgt": st.local_sgt,
                "market_session": timeutils.market_session().value,
                "current_price": price,
                "unrealized_pnl": unrealized_pnl,
                "unrealized_r": unrealized_r,
                "mfe": mfe,
                "mae": mae,
                "stop_price": pos.get("stop_price"),
                "target_price": pos.get("target_price"),
                "target_profile": pos.get("target_profile"),
                "stop_hit": 1 if decision == "stop" else 0,
                "target_hit": 1 if decision == "target" else 0,
                "time_stop_status": "expired" if decision == "time_expiry" else "active",
                "data_freshness_status": freshness_status,
                "action_taken": (
                    "broker_managed" if broker_managed
                    else ("exit_simulated" if decision is not None else "none")
                ),
            },
        )

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
                # --- Trade Packet v1 traceability ---
                "trade_id": pos.get("trade_id"),
                "candidate_id": pos.get("candidate_id"),
                "proposal_id": pos.get("proposal_id"),
                "hold_duration_minutes": (holding_days * 1440) if holding_days is not None else None,
                "same_day_exit_classification": (classification.value if same_day else "not_same_day"),
                "gross_pnl": gross_pnl,
                "estimated_costs": costs,
                "net_pnl": net_pnl,
                "realized_r": realized_r,
            },
        )

        # The exit tick is one more price observation: fold it into whatever
        # MFE/MAE the monitor already tracked across this position's life (in R
        # terms), rather than approximating from the single final return_pct.
        mfe, mae = self._fold_excursion(position_id, realized_r)
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
                "mfe_mae_source": "live_tracked",
                "win": 1 if net_pnl > 0 else 0,
                # --- Trade Packet v1 traceability ---
                "trade_id": pos.get("trade_id"),
                "candidate_id": pos.get("candidate_id"),
                "proposal_id": pos.get("proposal_id"),
                "exit_id": exit_id,
                "playbook_name": pos.get("strategy"),
                "outcome_classification": ("win" if net_pnl > 0 else "loss" if net_pnl < 0 else "breakeven"),
                "hold_duration_minutes": (holding_days * 1440) if holding_days is not None else None,
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
                "trade_id": pos.get("trade_id"),
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
                "trade_id": pos.get("trade_id"),
                "position_id": pos.get("position_id"),
            },
            mirror=True,
        )
        return order_id
