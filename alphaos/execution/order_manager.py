"""Order manager.

Responsibilities:
* run the non-negotiable safety preflight before any order (real-trading guard,
  kill switch, mode, margin/short gate),
* choose the order-protection path per the hierarchy and log it,
* execute (v1: simulated fills; Alpaca paper connector is a guarded stub),
* record everything through the shared order schema + append-only order_events,
* open the resulting position.

Execution in v1 is simulated internally and labelled honestly:
``execution_provider = simulated_internal`` / ``execution_mode =
internal_simulation`` / ``fill_source = internal_sim``. A fill is NEVER labelled
as an Alpaca paper fill unless it comes from the real Alpaca paper API. When in
paper mode with Alpaca creds, the Alpaca connector's guardrails are run first (it
then raises AlpacaNotConnected, and we fall back to simulation with a logged
note). No code path can place a real-money order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from alphaos.broker.alpaca_client import AlpacaClient, AlpacaNotConnected, AlpacaSafetyError
from alphaos.constants import (
    ExecutionProvider,
    ExecutionSource,
    OrderState,
    ProtectionPath,
    ReasonCode,
    Severity,
)

FILL_PRICE_BASIS = "latest_quote_or_bar"
EXEC_MODE_SIM = "internal_simulation"
from alphaos.execution import order_schema, protection_watchdog
from alphaos.execution.position_manager import PositionManager
from alphaos.safety import KillSwitch, real_trading_guard
from alphaos.util import timeutils
from alphaos.util.ids import new_id


@dataclass
class OrderResult:
    blocked: bool
    order: Optional[dict] = None
    fills: list = field(default_factory=list)
    protection_path: Optional[str] = None
    state: Optional[str] = None
    position_id: Optional[str] = None
    block_reason: Optional[str] = None
    detail: Optional[str] = None


class OrderManager:
    def __init__(
        self,
        settings,
        journal,
        position_manager: Optional[PositionManager] = None,
        kill_switch: Optional[KillSwitch] = None,
        alpaca: Optional[AlpacaClient] = None,
    ):
        self.settings = settings
        self.journal = journal
        self.positions = position_manager or PositionManager(settings, journal)
        self.kill_switch = kill_switch or KillSwitch()
        self.alpaca = alpaca
        self.real_paper = settings.real_paper_execution
        self.broker_connected = settings.is_paper and settings.has_alpaca_keys
        if self.broker_connected and self.alpaca is None:
            self.alpaca = AlpacaClient(settings, journal)

    # ----------------------------------------------------------- public API
    def execute_proposal(self, proposal, fill_price: Optional[float] = None) -> OrderResult:
        """Run safety preflight, choose protection, execute, and open a position."""
        # --- Safety preflight (defense in depth) -----------------------------
        guard = real_trading_guard(self.settings)
        if not guard.allowed:
            return self._blocked(proposal, ReasonCode.REAL_TRADING_BLOCKED.value, guard.reason, Severity.CRITICAL)

        if self.kill_switch.is_engaged():
            return self._blocked(
                proposal, ReasonCode.KILL_SWITCH_ACTIVE.value,
                f"kill switch engaged: {self.kill_switch.reason()}", Severity.CRITICAL,
            )

        blocking = protection_watchdog.has_blocking_incident(self.journal)
        if blocking:
            return self._blocked(
                proposal, ReasonCode.PROTECTION_INTEGRITY_FAILURE.value,
                f"protection incident {blocking['check_id']} unresolved: {blocking['detail']}",
                Severity.CRITICAL,
            )

        if proposal.requires_margin and not proposal.margin_approved:
            return self._blocked(
                proposal, ReasonCode.MARGIN_APPROVAL_REQUIRED.value,
                "trade needs margin/borrow/leverage; explicit approval required first.", Severity.WARNING,
            )

        # --- Order-protection hierarchy --------------------------------------
        protection = self._choose_protection(proposal)
        if protection == ProtectionPath.BLOCKED_NO_VALID_EXIT_PROTECTION:
            return self._blocked(
                proposal, ReasonCode.NO_VALID_EXIT_PROTECTION.value,
                "no broker bracket and no verifiable watchdog exit; trade blocked.",
                Severity.ERROR, protection_path=protection.value,
            )

        # --- Route: real Alpaca paper execution, else internal simulation ----
        if self.real_paper:
            if not self.broker_connected:
                return self._blocked(
                    proposal, ReasonCode.PAPER_SAFETY_FAILED.value,
                    "EXECUTION_PROVIDER=alpaca_paper but Alpaca paper not connected.",
                    Severity.CRITICAL, protection_path=protection.value,
                )
            return self._submit_alpaca_paper(proposal, protection)

        return self._simulate_fill(proposal, protection, fill_price)

    # ----------------------------------------------------------- internals
    def _choose_protection(self, proposal) -> ProtectionPath:
        valid_exit = (
            proposal.stop is not None
            and proposal.target is not None
            and proposal.qty
            and proposal.qty > 0
        )
        if not valid_exit:
            return ProtectionPath.BLOCKED_NO_VALID_EXIT_PROTECTION
        # Prefer broker-native bracket where supported; the watchdog
        # (position_manager) always backs it up and is always verifiable here.
        if self.broker_connected and self.alpaca and self.alpaca.capabilities().get("bracket"):
            return ProtectionPath.BROKER_NATIVE_BRACKET
        # Mock simulator models a native bracket (entry + TP + SL, OCO).
        if self.settings.is_mock:
            return ProtectionPath.BROKER_NATIVE_BRACKET
        # Otherwise: entry + watchdog-managed exits (verifiable via monitor).
        return ProtectionPath.ENTRY_PLUS_WATCHDOG

    def _data_labels(self) -> tuple[str, str]:
        """The market-data provider/feed that priced this fill (honest labels)."""
        provider = "alpaca_mock" if self.settings.offline_mode else "alpaca"
        return provider, self.settings.market_data_feed

    def _simulate_fill(self, proposal, protection: ProtectionPath, fill_price) -> OrderResult:
        order_id = new_id("ord")
        price = float(fill_price if fill_price is not None else proposal.entry)
        side = order_schema.side_for_entry(proposal.direction)
        order_type = "bracket" if protection == ProtectionPath.BROKER_NATIVE_BRACKET else "market"
        st = timeutils.stamp()
        data_provider, data_feed = self._data_labels()
        src = ExecutionSource.INTERNAL_SIM.value

        row = order_schema.build_order_row(
            order_id=order_id,
            proposal=proposal,
            side=side,
            order_type=order_type,
            # v1 fills are internal simulations — never an Alpaca paper fill.
            execution_source=src,
            execution_provider=ExecutionProvider.SIMULATED_INTERNAL.value,
            execution_mode=EXEC_MODE_SIM,
            data_provider=data_provider,
            data_feed=data_feed,
            fill_price_basis=FILL_PRICE_BASIS,
            protection_path=protection.value,
            state=OrderState.FILLED.value,
            qty=proposal.qty,
            entry_price=price,
            take_profit_price=proposal.target,
            stop_loss_price=proposal.stop,
            limit_price=proposal.entry,
            client_order_id=new_id("cli"),
            broker_order_id=new_id("sim"),
            raw_request={"proposal_id": proposal.proposal_id},
            raw_response={"simulated": True, "fill_price": price, "fill_source": src},
            submitted_at=st.utc,
            accepted_at=st.utc,
            filled_at=st.utc,
        )
        self.journal.insert("paper_orders", row, mirror=True)

        # Append-only lifecycle events.
        for prev, new in (
            (OrderState.APPROVED, OrderState.SUBMITTED),
            (OrderState.SUBMITTED, OrderState.ACCEPTED),
            (OrderState.ACCEPTED, OrderState.FILLED),
        ):
            self._event(order_id, row["broker_order_id"], prev, new, src)

        fill_id = new_id("fill")
        self.journal.insert(
            "paper_fills",
            {
                "fill_id": fill_id,
                "order_id": order_id,
                "broker_order_id": row["broker_order_id"],
                "symbol": proposal.symbol,
                "side": side,
                "qty": proposal.qty,
                "price": price,
                "commission": 0.0,
                "execution_source": src,
                "execution_provider": ExecutionProvider.SIMULATED_INTERNAL.value,
                "data_provider": data_provider,
                "data_feed": data_feed,
                "fill_source": "internal_sim",
                "fill_price_basis": FILL_PRICE_BASIS,
                "filled_at": st.utc,
                # --- Trade Packet v1 traceability ---
                "trade_id": getattr(proposal, "trade_id", None),
            },
            mirror=True,
        )

        position_id = self.positions.open_position(row, price)
        # Back-link the fill to the opened position (best-effort; must never abort
        # an otherwise-successful fill/open).
        if position_id:
            try:
                self.journal.conn.execute(
                    "UPDATE paper_fills SET position_id = ? WHERE fill_id = ?", (position_id, fill_id)
                )
                self.journal.conn.commit()
            except Exception:  # pragma: no cover - audit back-link is best-effort
                pass
        self.journal.log_system_event(
            Severity.INFO, "execution",
            f"Filled {proposal.symbol} x{proposal.qty} @ {price} "
            f"({proposal.direction}, {protection.value}, simulated_internal, data={data_provider}/{data_feed}).",
            {"order_id": order_id, "position_id": position_id},
        )
        return OrderResult(
            blocked=False, order=row, fills=[fill_id], protection_path=protection.value,
            state=OrderState.FILLED.value, position_id=position_id,
        )

    # ------------------------------------------------- real Alpaca paper path
    def _submit_alpaca_paper(self, proposal, protection: ProtectionPath) -> OrderResult:
        """Submit a real broker-native bracket to the Alpaca PAPER API."""
        try:
            norm = self.alpaca.submit_bracket(proposal)
        except AlpacaSafetyError as exc:
            return self._blocked(proposal, ReasonCode.PAPER_SAFETY_FAILED.value, str(exc),
                                 Severity.CRITICAL, protection_path=protection.value)
        except Exception as exc:  # pragma: no cover - network/SDK failure
            self.journal.log_system_event(
                Severity.ERROR, "execution", f"Alpaca paper submit failed for {proposal.symbol}.",
                {"error": str(exc)},
            )
            return self._blocked(proposal, ReasonCode.ALPACA_SUBMIT_FAILED.value, str(exc),
                                 Severity.ERROR, protection_path=protection.value)

        order_id = new_id("ord")
        side = order_schema.side_for_entry(proposal.direction)
        state = norm.get("state") or OrderState.SUBMITTED.value
        filled_price = norm.get("filled_avg_price")
        data_provider, data_feed = self._data_labels()
        src = ExecutionSource.ALPACA_PAPER.value

        # Prefer the broker's own echoed TIF (the ground truth of what was actually
        # accepted) over our outgoing intent; fall back to the intent only if the
        # broker didn't echo one back (e.g. a minimal fake in tests).
        time_in_force = norm.get("time_in_force") or self.alpaca._resolve_tif(proposal)
        row = order_schema.build_order_row(
            order_id=order_id, proposal=proposal, side=side, order_type="bracket",
            execution_source=src, execution_provider=ExecutionProvider.ALPACA_PAPER.value,
            execution_mode="alpaca_paper", data_provider=data_provider, data_feed=data_feed,
            fill_price_basis="alpaca_fill", protection_path=protection.value, state=state,
            qty=proposal.qty, entry_price=(filled_price if filled_price is not None else proposal.entry),
            take_profit_price=proposal.target, stop_loss_price=proposal.stop, limit_price=proposal.entry,
            time_in_force=time_in_force,
            broker_order_id=norm.get("broker_order_id"), client_order_id=norm.get("client_order_id"),
            raw_request={"proposal_id": proposal.proposal_id},
            raw_response=norm, submitted_at=norm.get("submitted_at"), filled_at=norm.get("filled_at"),
        )
        self.journal.insert("paper_orders", row, mirror=True)
        self._event(order_id, norm.get("broker_order_id"), OrderState.APPROVED, OrderState.SUBMITTED, src)
        if state != OrderState.SUBMITTED.value:
            self._event(order_id, norm.get("broker_order_id"), OrderState.SUBMITTED, OrderState(state), src,
                        {"alpaca_status": norm.get("status")})

        position_id = None
        if state == OrderState.FILLED.value and (norm.get("filled_qty") or 0) > 0:
            position_id = self._open_real_position(row, norm)
            self.journal.log_system_event(
                Severity.INFO, "execution",
                f"Alpaca PAPER bracket FILLED {proposal.symbol} @ {filled_price} (real paper order).",
                {"order_id": order_id, "position_id": position_id, "broker_order_id": norm.get("broker_order_id")},
            )
        else:
            self.journal.log_system_event(
                Severity.INFO, "execution",
                f"Alpaca PAPER bracket submitted {proposal.symbol} (status={norm.get('status')}); "
                f"awaiting fill — will reconcile.",
                {"order_id": order_id, "broker_order_id": norm.get("broker_order_id")},
            )
        return OrderResult(blocked=False, order=row, protection_path=protection.value,
                           state=state, position_id=position_id)

    def _open_real_position(self, row: dict, norm: dict) -> str:
        st = timeutils.stamp()
        fill_id = new_id("fill")
        self.journal.insert(
            "paper_fills",
            {
                "fill_id": fill_id, "order_id": row["order_id"],
                "broker_order_id": norm.get("broker_order_id"), "symbol": row["symbol"],
                "side": row["side"], "qty": norm.get("filled_qty") or row["qty"],
                "price": norm.get("filled_avg_price") or row["entry_price"],
                "execution_source": ExecutionSource.ALPACA_PAPER.value,
                "execution_provider": ExecutionProvider.ALPACA_PAPER.value,
                "data_provider": row["data_provider"], "data_feed": row["data_feed"],
                "fill_source": "alpaca_paper", "fill_price_basis": "alpaca_fill", "filled_at": st.utc,
                # --- Trade Packet v1 traceability ---
                "trade_id": row.get("trade_id"),
            },
            mirror=True,
        )
        position_id = self.positions.open_position(row, norm.get("filled_avg_price") or row["entry_price"])
        if position_id:
            try:
                self.journal.conn.execute(
                    "UPDATE paper_fills SET position_id = ? WHERE fill_id = ?", (position_id, fill_id)
                )
                self.journal.conn.commit()
            except Exception:  # pragma: no cover - audit back-link is best-effort
                pass
        return position_id

    def reconcile(self) -> dict:
        """Reconcile open Alpaca paper orders against the broker: open positions
        on entry fills, close them when a bracket leg (TP/SL) fills. Exits are
        managed by Alpaca's OCO, not the local watchdog."""
        results = {"reconciled": 0, "opened": [], "exits": []}
        if not (self.real_paper and self.broker_connected and self.alpaca):
            return results
        terminal_no_fill = {OrderState.REJECTED.value, OrderState.CANCELLED.value,
                            OrderState.EXPIRED.value, OrderState.FAILED.value}
        rows = self.journal.query(
            "SELECT * FROM paper_orders WHERE execution_source = ? AND order_type = 'bracket'",
            (ExecutionSource.ALPACA_PAPER.value,),
        )
        for row in rows:
            order_id, boid = row["order_id"], row.get("broker_order_id")
            pos = self.journal.one("SELECT * FROM positions WHERE order_id = ?", (order_id,))
            if pos and pos["status"] == "closed":
                continue
            if pos is None and row["state"] in terminal_no_fill:
                continue
            try:
                norm = self.alpaca.get_order(boid)
            except Exception as exc:  # pragma: no cover - network
                self.journal.log_system_event(
                    Severity.WARNING, "reconcile", f"get_order failed for {boid}.", {"error": str(exc)}
                )
                continue
            results["reconciled"] += 1

            if norm.get("state") and norm["state"] != row["state"]:
                self._event(order_id, boid, OrderState(row["state"]) if row["state"] else OrderState.SUBMITTED,
                            OrderState(norm["state"]), ExecutionSource.ALPACA_PAPER.value, {"reconcile": True})
                self.journal.conn.execute(
                    "UPDATE paper_orders SET state = ? WHERE order_id = ?", (norm["state"], order_id)
                )
                self.journal.conn.commit()

            # Entry fill -> open position.
            if pos is None and (norm.get("filled_qty") or 0) > 0:
                pid = self._open_real_position(row, norm)
                results["opened"].append(pid)
                pos = self.journal.one("SELECT * FROM positions WHERE position_id = ?", (pid,))
                # Status lifecycle: the proposal was 'submitted' at approval; the
                # entry fill is what makes it 'filled'. Never resurrect a
                # rejected/blocked proposal.
                if pid and row.get("proposal_id"):
                    self.journal.conn.execute(
                        "UPDATE trade_proposals SET status = 'filled' "
                        "WHERE proposal_id = ? AND status NOT IN ('rejected', 'blocked', 'filled')",
                        (row["proposal_id"],),
                    )
                    self.journal.conn.commit()

            # Bracket leg fill -> close position (TP=target, SL=stop), via OCO.
            if pos and pos["status"] == "open":
                for leg in norm.get("legs", []):
                    if leg.get("role") in ("take_profit", "stop_loss") \
                            and leg.get("state") == OrderState.FILLED.value and (leg.get("filled_qty") or 0) > 0:
                        reason = "target" if leg["role"] == "take_profit" else "stop"
                        exit_price = leg.get("filled_avg_price") or (
                            pos["target_price"] if reason == "target" else pos["stop_price"]
                        )
                        ex = self.positions.close_position(
                            pos["position_id"], exit_price, reason, triggered_by="alpaca_reconcile",
                            execution_source=ExecutionSource.ALPACA_PAPER.value,
                            broker_order_id=leg.get("broker_order_id"),
                        )
                        if ex:
                            results["exits"].append(ex)
                        break
        return results

    def _event(self, order_id, broker_order_id, prev: OrderState, new: OrderState, source: str, detail=None):
        self.journal.insert(
            "order_events",
            {
                "event_id": new_id("oev"),
                "order_id": order_id,
                "broker_order_id": broker_order_id,
                "prev_state": prev.value if isinstance(prev, OrderState) else prev,
                "new_state": new.value if isinstance(new, OrderState) else new,
                "execution_source": source,
                "message": f"{prev} -> {new}",
                "detail_json": detail or {},
            },
            mirror=True,
        )

    def _blocked(self, proposal, reason_code, detail, severity, protection_path=None) -> OrderResult:
        """Persist a rejected order attempt + system event + rejection record."""
        order_id = new_id("ord")
        side = order_schema.side_for_entry(proposal.direction)
        data_provider, data_feed = self._data_labels()
        row = order_schema.build_order_row(
            order_id=order_id,
            proposal=proposal,
            side=side,
            order_type="market",
            execution_source=ExecutionSource.INTERNAL_SIM.value,
            execution_provider=ExecutionProvider.SIMULATED_INTERNAL.value,
            execution_mode=EXEC_MODE_SIM,
            data_provider=data_provider,
            data_feed=data_feed,
            protection_path=protection_path,
            state=OrderState.REJECTED.value,
            qty=proposal.qty,
            entry_price=proposal.entry,
            take_profit_price=proposal.target,
            stop_loss_price=proposal.stop,
            raw_request={"proposal_id": proposal.proposal_id},
            raw_response={"blocked": True, "reason_code": reason_code, "detail": detail},
        )
        self.journal.insert("paper_orders", row, mirror=True)
        self._event(order_id, None, OrderState.APPROVED, OrderState.REJECTED, ExecutionSource.MOCK.value,
                    {"reason_code": reason_code, "detail": detail})
        self.journal.log_system_event(
            severity, "execution",
            f"BLOCKED order for {proposal.symbol}: {reason_code} — {detail}",
            {"order_id": order_id, "proposal_id": proposal.proposal_id},
        )
        self.journal.insert(
            "rejected_candidates",
            {
                "rejection_id": new_id("rej"),
                "candidate_id": proposal.candidate_id,
                "symbol": proposal.symbol,
                "stage": "execution",
                "reason_code": reason_code,
                "reason_detail": detail,
                "direction": proposal.direction,
                "would_be_entry": proposal.entry,
                "would_be_stop": proposal.stop,
            },
        )
        return OrderResult(
            blocked=True, order=row, protection_path=protection_path,
            state=OrderState.REJECTED.value, block_reason=reason_code, detail=detail,
        )
