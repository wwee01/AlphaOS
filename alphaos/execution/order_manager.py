"""Order manager.

Responsibilities:
* run the non-negotiable safety preflight before any order (real-trading guard,
  kill switch, mode, margin/short gate),
* choose the order-protection path per the hierarchy and log it,
* execute (v1: simulated fills; Alpaca paper connector is a guarded stub),
* record everything through the shared order schema + append-only order_events,
* open the resulting position.

Execution in v1 is simulated and labelled honestly: ``execution_source = mock``.
When in paper mode with Alpaca creds, the Alpaca connector's guardrails are run
first (it then raises AlpacaNotConnected, and we fall back to simulation with a
logged note). No code path can place a real-money order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from alphaos.broker.alpaca_client import AlpacaClient, AlpacaNotConnected, AlpacaSafetyError
from alphaos.constants import (
    ExecutionSource,
    OrderState,
    ProtectionPath,
    ReasonCode,
    Severity,
)
from alphaos.execution import order_schema
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

        # --- Paper-mode broker guardrails (when Alpaca creds present) --------
        if self.broker_connected:
            try:
                # Guardrails run inside submit_order; stub then raises NotConnected.
                self.alpaca.submit_order({"symbol": proposal.symbol})
            except AlpacaSafetyError as exc:
                return self._blocked(
                    proposal, ReasonCode.PAPER_SAFETY_FAILED.value, str(exc), Severity.CRITICAL,
                    protection_path=protection.value,
                )
            except AlpacaNotConnected:
                pass  # expected in v1 — fall back to simulated execution

        # --- Simulated fill (v1) --------------------------------------------
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

    def _simulate_fill(self, proposal, protection: ProtectionPath, fill_price) -> OrderResult:
        order_id = new_id("ord")
        price = float(fill_price if fill_price is not None else proposal.entry)
        side = order_schema.side_for_entry(proposal.direction)
        order_type = "bracket" if protection == ProtectionPath.BROKER_NATIVE_BRACKET else "market"
        st = timeutils.stamp()

        row = order_schema.build_order_row(
            order_id=order_id,
            proposal=proposal,
            side=side,
            order_type=order_type,
            execution_source=ExecutionSource.MOCK.value,  # honest: v1 fills are simulated
            protection_path=protection.value,
            state=OrderState.FILLED.value,
            qty=proposal.qty,
            entry_price=price,
            take_profit_price=proposal.target,
            stop_loss_price=proposal.stop,
            limit_price=proposal.entry,
            client_order_id=new_id("cli"),
            broker_order_id=new_id("sim"),
            raw_request={"proposal_id": proposal.proposal_id, "intended_source": self._intended_source()},
            raw_response={"simulated": True, "fill_price": price},
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
            self._event(order_id, row["broker_order_id"], prev, new, ExecutionSource.MOCK.value)

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
                "execution_source": ExecutionSource.MOCK.value,
                "filled_at": st.utc,
            },
            mirror=True,
        )

        position_id = self.positions.open_position(row, price)
        self.journal.log_system_event(
            Severity.INFO, "execution",
            f"Filled {proposal.symbol} x{proposal.qty} @ {price} "
            f"({proposal.direction}, {protection.value}, simulated).",
            {"order_id": order_id, "position_id": position_id},
        )
        return OrderResult(
            blocked=False, order=row, fills=[fill_id], protection_path=protection.value,
            state=OrderState.FILLED.value, position_id=position_id,
        )

    def _intended_source(self) -> str:
        return ExecutionSource.ALPACA_PAPER.value if self.broker_connected else ExecutionSource.MOCK.value

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
        row = order_schema.build_order_row(
            order_id=order_id,
            proposal=proposal,
            side=side,
            order_type="market",
            execution_source=ExecutionSource.MOCK.value,
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
