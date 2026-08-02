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

from alphaos.broker.alpaca_client import AlpacaClient, AlpacaSafetyError
from alphaos.constants import (
    ExecutionProvider,
    ExecutionSource,
    OrderState,
    ProposalStatus,
    ProtectionPath,
    ReasonCode,
    Severity,
)
from alphaos.data.freshness_guard import FreshnessGuard
from alphaos.execution import entry_staleness, order_schema, protection_watchdog
from alphaos.execution.position_manager import PositionManager
from alphaos.safety import KillSwitch, real_trading_guard
from alphaos.util import alerts, timeutils
from alphaos.util.ids import new_id

FILL_PRICE_BASIS = "latest_quote_or_bar"
EXEC_MODE_SIM = "internal_simulation"

# ENTRY-TTL-1: system_events category for staleness cancels/partial-fill
# alerts (kept distinct from "execution"/"reconcile" so an operator can
# filter this mechanism's audit trail on its own).
ENTRY_STALENESS_EVENT_CATEGORY = "entry_staleness"
# Dedupe marker category for the partial-fill alert (spec 3.5: alert once,
# not every monitor pass -- see OrderManager._alert_partial_fill_once).
PARTIAL_FILL_ALERT_CATEGORY = "entry_staleness_partial_fill"


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
        market_data=None,
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
        # ENTRY-TTL-1: market snapshot + freshness assessment for the drift
        # leg (same dependency-threading pattern as PositionManager's own
        # optional ``market_data``/``self._market``). Lazily built in
        # ``_staleness_price_snapshot`` if still None when actually needed --
        # most reconcile() passes touch zero unfilled entries, so this must
        # not force a MarketDataClient construction (and its mock-mode
        # "market data is mocked" system_event write) on every reconcile().
        self._market = market_data
        self.freshness = FreshnessGuard.from_settings(settings)

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
            # Audit F8: fall back to our own clock if the broker didn't echo
            # submitted_at -- ENTRY-TTL-1's TTL leg fails TOWARD cancellation
            # on a missing submission time, so an SDK field-shape change here
            # would otherwise silently become "cancel every unfilled entry at
            # age 0". The broker echo stays preferred (ground truth).
            raw_response=norm, submitted_at=norm.get("submitted_at") or timeutils.stamp().utc,
            filled_at=norm.get("filled_at"),
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
        # ENTRY-TTL-1 (audit MAJOR-2): order_ids whose broker re-read SUCCEEDED
        # this pass. The staleness pass below only ever cancels rows in this
        # set -- a row whose get_order failed this pass has an UNVERIFIED
        # local state (it may have filled at the broker without the ledger
        # knowing), and cancelling on unverified state is exactly the
        # orphaned-position hole the audit reproduced.
        synced_this_pass: set = set()
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
            synced_this_pass.add(order_id)

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

        # ENTRY-TTL-1: staleness pass runs AFTER the state-sync loop above,
        # so a fill that happened THIS pass is already reflected in
        # ``positions`` before any staleness decision is made (spec 3.2).
        # synced_order_ids restricts cancellation to rows whose broker state
        # was successfully verified THIS pass (audit MAJOR-2).
        stale = self._cancel_stale_entries(synced_order_ids=synced_this_pass)
        results["stale_cancelled"] = stale["cancelled"]
        results["stale_errors"] = stale["errors"]
        results["stale_partial_fill_alerts"] = stale["partial_fill_alerts"]
        return results

    # --------------------------------------------- ENTRY-TTL-1: staleness
    def _cancel_stale_entries(self, now=None, synced_order_ids: Optional[set] = None) -> dict:
        """Auto-cancel unfilled alpaca_paper bracket ENTRY orders whose
        thesis has aged out (TTL leg) or that the market has moved
        decisively past (drift leg) -- see ``entry_staleness.evaluate`` for
        the pure trigger logic. A separate, independently unit-testable
        method (spec's own requirement): callable with an injected ``now``
        and a mock/fake broker WITHOUT running a full monitor pass.

        Deliberately NOT gated on the kill switch (spec 3.3): an engaged
        kill switch that left stale GTC entries live at the broker would
        itself be a hole in the kill switch -- cancellation only ever
        REDUCES prospective exposure, mirroring ``run_monitor_job``'s own
        kill-switch exemption for the identical reason.

        Scope (spec 3.1): every alpaca_paper BRACKET order in
        submitted/accepted/partially_filled with NO ``positions`` row yet.
        ``partially_filled`` is explicitly OUT OF SCOPE for auto-cancel
        (alert-only, spec 3.5) -- the filled portion is a real position and
        remainder-handling policy is an operator decision, not this
        mechanism's to make.

        ``synced_order_ids`` (audit MAJOR-2): when given (the reconcile()
        call path always gives it), only rows whose broker state was
        successfully re-read THIS pass are eligible for cancellation --
        never a row whose get_order failed, whose local state is therefore
        unverified. ``None`` (direct unit-test invocation) applies no such
        restriction; the verify-after-cancel re-read inside
        ``_cancel_order_row`` remains the universal backstop either way.
        """
        result: dict = {"cancelled": [], "errors": [], "partial_fill_alerts": [],
                        "missing_broker_id_alerts": []}
        if not self.settings.entry_order_staleness_enabled:
            return result
        if not (self.real_paper and self.broker_connected and self.alpaca):
            return result
        now = now or timeutils.now_utc()

        rows = self.journal.query(
            "SELECT * FROM paper_orders WHERE execution_source = ? AND order_type = 'bracket' "
            "AND state IN (?, ?, ?)",
            (
                ExecutionSource.ALPACA_PAPER.value,
                OrderState.SUBMITTED.value,
                OrderState.ACCEPTED.value,
                OrderState.PARTIALLY_FILLED.value,
            ),
        )
        for row in rows:
            order_id = row["order_id"]
            # Re-read from the ledger (not a cached value) so a fill this
            # SAME pass's state-sync loop above already mirrored is never
            # raced against -- protective legs of a filled entry are never
            # touched (spec 3.1/3.5, test 9's swap-style probe).
            pos = self.journal.one("SELECT * FROM positions WHERE order_id = ?", (order_id,))
            if pos is not None:
                continue
            if row["state"] == OrderState.PARTIALLY_FILLED.value:
                if self._alert_partial_fill_once(row):
                    result["partial_fill_alerts"].append(order_id)
                continue
            if synced_order_ids is not None and order_id not in synced_order_ids:
                # Audit MAJOR-2: this row's broker state could NOT be
                # verified this pass (get_order failed in the sync loop) --
                # its local state may be stale, and a cancel issued on
                # stale state is how a fill becomes an orphaned broker
                # position. Skip; retry next pass.
                continue
            if not row.get("broker_order_id"):
                # Audit MINOR-2: no broker_order_id means nothing to cancel
                # AT the broker and an unbounded retry/log-spam loop if
                # attempted every pass. Alert the operator ONCE (same dedupe
                # mechanism as the partial-fill alert) and skip.
                if self._alert_missing_broker_id_once(row):
                    result["missing_broker_id_alerts"].append(order_id)
                continue

            intended_entry = (
                row.get("intended_entry_price") or row.get("entry_price") or row.get("limit_price")
            )
            # Audit F6: the drift leg is the ONLY consumer of a price
            # snapshot -- in TTL-only mode (drift leg disabled via 0) a
            # snapshot fetch would be pure waste (~hundreds of pointless
            # market-data calls/day against quota). Skip the fetch entirely;
            # evaluate() receives price_usable=False, exactly the state the
            # fail-safe split already handles.
            if self.settings.entry_order_max_adverse_drift_pct > 0:
                last_price, price_usable = self._staleness_price_snapshot(row["symbol"], now)
            else:
                last_price, price_usable = None, False
            decision = entry_staleness.evaluate(
                direction=row["direction"],
                submitted_at=row.get("submitted_at"),
                intended_entry_price=intended_entry,
                now=now,
                last_price=last_price,
                price_usable=price_usable,
                ttl_trading_days=self.settings.entry_order_ttl_trading_days,
                max_adverse_drift_pct=self.settings.entry_order_max_adverse_drift_pct,
            )
            if not decision.should_cancel:
                continue
            outcome = self._cancel_order_row(
                row, decision.as_detail(), ReasonCode.ORDER_STALE_CANCELLED.value,
                alert_title=f"AlphaOS: stale entry auto-cancelled — {row['symbol']}",
            )
            (result["cancelled"] if outcome["ok"] else result["errors"]).append(outcome)
        return result

    def _staleness_price_snapshot(self, symbol: str, now=None) -> tuple:
        """(last_price, price_usable) for the drift leg -- a
        FreshnessGuard-assessed snapshot ONLY; any stale/missing/closed-
        session read reports usable=False (never guessed, spec 3.1). Lazily
        builds a MarketDataClient exactly like ``PositionManager.monitor()``'s
        own precedent (see its module docstring), so a reconcile pass that
        touches zero unfilled entries -- the common case -- never constructs
        one. ``now`` is threaded through to FreshnessGuard.assess() so the
        freshness read is judged against the SAME injected clock as the TTL
        leg (house law: no wall-clock-dependent test behavior) -- production
        callers pass the real ``now`` computed once at the top of
        ``_cancel_stale_entries``."""
        market = self._market
        if market is None:
            from alphaos.data.market_data import MarketDataClient

            market = MarketDataClient(self.settings, self.journal)
            self._market = market
        snap = market.get_snapshot(symbol)
        report = self.freshness.assess(snap, now=now)
        if not report.is_usable:
            return None, False
        return snap.get("last_price"), True

    def _alert_partial_fill_once(self, row: dict) -> bool:
        """spec 3.5: a partially_filled entry is OUT OF SCOPE for auto-cancel
        in v1 -- alert loudly, take no action. Deduped via a marker embedded
        in the ``system_events`` message (spec test 5/idempotency: a second
        pass over the SAME still-partially-filled order must not re-alert
        every monitor tick forever). Returns True iff a NEW alert was sent."""
        order_id = row["order_id"]
        marker = f"order_id={order_id}"
        already = self.journal.one(
            "SELECT 1 FROM system_events WHERE category = ? AND message LIKE ? LIMIT 1",
            (PARTIAL_FILL_ALERT_CATEGORY, f"%{marker}%"),
        )
        if already:
            return False
        detail = {
            "order_id": order_id, "symbol": row["symbol"],
            "broker_order_id": row.get("broker_order_id"),
        }
        self.journal.log_system_event(
            Severity.WARNING, PARTIAL_FILL_ALERT_CATEGORY,
            f"PARTIALLY_FILLED entry {row['symbol']} ({marker}) is out of scope for "
            f"ENTRY-TTL-1 auto-cancel -- the filled portion is a real position; "
            f"remainder-handling is an operator decision (spec Non-goals). Alerting "
            f"once, no action taken.",
            detail,
        )
        alerts.send_alert(
            self.settings,
            title=f"AlphaOS: partial fill needs review — {row['symbol']}",
            message=f"Order {order_id} ({row['symbol']}) is partially filled; "
                    f"ENTRY-TTL-1 will never auto-cancel or modify it. Review manually.",
            priority="default",
            journal=self.journal,
        )
        return True

    def _alert_missing_broker_id_once(self, row: dict) -> bool:
        """Audit MINOR-2: an unfilled entry row with NO broker_order_id
        cannot be cancelled at the broker and would otherwise produce an
        unbounded retry + log-spam loop (one attempt per monitor tick,
        forever). Alert the operator once -- same message-marker dedupe as
        ``_alert_partial_fill_once`` -- then skip on every later pass.
        Returns True iff a NEW alert was sent."""
        order_id = row["order_id"]
        marker = f"order_id={order_id}"
        already = self.journal.one(
            "SELECT 1 FROM system_events WHERE category = ? AND message LIKE ? LIMIT 1",
            (ENTRY_STALENESS_EVENT_CATEGORY, f"%missing broker_order_id%{marker}%"),
        )
        if already:
            return False
        self.journal.log_system_event(
            Severity.WARNING, ENTRY_STALENESS_EVENT_CATEGORY,
            f"Unfilled entry {row['symbol']} has missing broker_order_id ({marker}) -- "
            f"cannot be cancelled at the broker; needs operator review. Alerting once, "
            f"skipping on all later passes.",
            {"order_id": order_id, "symbol": row["symbol"]},
        )
        alerts.send_alert(
            self.settings,
            title=f"AlphaOS: order needs review — {row['symbol']} (no broker id)",
            message=f"Order {order_id} ({row['symbol']}) is unfilled but carries no "
                    f"broker_order_id; the staleness watchdog cannot cancel it. Review manually.",
            priority="default", journal=self.journal,
        )
        return True

    # Raw Alpaca statuses that TERMINALLY confirm "this order is dead and
    # nothing more can fill" -- checked on the RAW status string, not the
    # normalized OrderState, because order_mapping deliberately folds
    # 'pending_cancel' into CANCELLED for display purposes while a
    # pending_cancel order is NOT yet terminally dead at the broker.
    _TERMINAL_NO_FILL_RAW_STATUSES = ("canceled", "cancelled", "expired", "rejected")

    def _cancel_order_row(self, row: dict, trigger_detail: dict, reason_code: str, *, alert_title: str) -> dict:
        """Side-effecting cancel flow shared by the automated staleness pass
        AND the operator-invoked CLI cancel (spec 3.7: "targeted manual
        cancel through the SAME code path"). ``trigger_detail`` is the audit
        detail payload -- ``StalenessDecision.as_detail()`` for an automated
        cancel, or ``{"trigger": "operator"}`` for a CLI cancel.

        Race handling (spec 3.5) -- REWRITTEN per the cancellation-safety
        audit's MAJOR-1 finding. Alpaca's DELETE /v2/orders/{id} returns 204
        "cancel request ACCEPTED" (asynchronous), and a cancel is accepted
        even on a partially_filled order (the remainder cancels; the filled
        shares stay). The original flow treated a non-raising cancel as
        proof the order never filled, wrote state='cancelled'/'expired',
        and reconcile()'s terminal_no_fill skip then never looked at the
        row again -- a fill that landed between the last get_order and the
        cancel became a REAL broker position with no local ledger row, no
        stop/target monitoring, and no alert, permanently and silently.

        Now: a non-raising cancel is only a REQUEST. The ledger writes are
        gated on a VERIFY-AFTER-CANCEL ``get_order`` re-read:
          - re-read shows ``filled_qty > 0``  -> the cancel RACED A FILL:
            open the position for the filled quantity (same
            ``_open_real_position`` path reconcile's own fill handling
            uses), mark the proposal 'filled' (guarded), alert loudly.
            Nothing is ever marked cancelled/expired on this branch.
          - re-read shows a TERMINAL no-fill status (canceled/expired/
            rejected, raw broker status) with zero filled -> the clean
            case: write cancelled + expired as before.
          - re-read shows anything else (pending_cancel still processing,
            or the re-read itself failed) -> DEFER: write NOTHING. The row
            stays in a live state locally, so the next reconcile pass
            re-syncs it and the staleness pass re-fires if still due --
            convergence by retry, never by assumption.

        A broker error on the cancel call itself is still treated as
        BENIGN-DEFER exactly as before (log, write nothing).
        """
        order_id, boid = row["order_id"], row.get("broker_order_id")
        try:
            self.alpaca.cancel_order(boid)
        except Exception as exc:
            self.journal.log_system_event(
                Severity.INFO, ENTRY_STALENESS_EVENT_CATEGORY,
                f"cancel_order failed for {row['symbol']} ({order_id}); likely filled "
                f"meanwhile -- the next reconcile pass will mirror the true broker state. "
                f"Proposal NOT expired, order NOT marked cancelled.",
                {"order_id": order_id, "broker_order_id": boid, "error": str(exc), **trigger_detail},
            )
            return {"ok": False, "order_id": order_id, "error": str(exc), **trigger_detail}

        # --- verify-after-cancel (audit MAJOR-1/MAJOR-2) ---
        try:
            norm = self.alpaca.get_order(boid)
        except Exception as exc:
            self.journal.log_system_event(
                Severity.WARNING, ENTRY_STALENESS_EVENT_CATEGORY,
                f"cancel for {row['symbol']} ({order_id}) was ACCEPTED by the broker but the "
                f"verify-after-cancel re-read failed -- deferring ALL ledger writes; the next "
                f"reconcile pass re-syncs this order and the staleness pass re-fires if still due.",
                {"order_id": order_id, "broker_order_id": boid, "error": str(exc), **trigger_detail},
            )
            return {"ok": False, "order_id": order_id, "deferred": True, "error": str(exc), **trigger_detail}

        if (norm.get("filled_qty") or 0) > 0:
            # The cancel raced a fill (full, or partial with remainder
            # cancelled): the filled shares are a REAL position. Mirror
            # reality exactly like reconcile's own fill handling would.
            position_id = self._open_real_position(row, norm)
            if norm.get("state"):
                self.journal.conn.execute(
                    "UPDATE paper_orders SET state = ? WHERE order_id = ?", (norm["state"], order_id)
                )
            if row.get("proposal_id"):
                self.journal.conn.execute(
                    "UPDATE trade_proposals SET status = 'filled' "
                    "WHERE proposal_id = ? AND status NOT IN ('rejected', 'blocked', 'filled')",
                    (row["proposal_id"],),
                )
            self.journal.conn.commit()
            self.journal.log_system_event(
                Severity.WARNING, ENTRY_STALENESS_EVENT_CATEGORY,
                f"Staleness cancel for {row['symbol']} ({order_id}) RACED A FILL: "
                f"filled_qty={norm.get('filled_qty')} -- position {position_id} opened and is now "
                f"monitored normally. Proposal marked 'filled', NOT expired.",
                {"order_id": order_id, "broker_order_id": boid, "position_id": position_id,
                 "filled_qty": norm.get("filled_qty"), **trigger_detail},
            )
            alerts.send_alert(
                self.settings,
                title=f"AlphaOS: cancel raced a fill — {row['symbol']} position opened",
                message=f"{row['symbol']} entry order {order_id} filled "
                        f"({norm.get('filled_qty')} shares) just as the staleness cancel landed. "
                        f"The position is open and monitored normally; nothing was lost.",
                priority="default", journal=self.journal,
            )
            return {"ok": False, "order_id": order_id, "raced_fill": True,
                    "position_id": position_id, **trigger_detail}

        raw_status = (norm.get("status") or "").lower()
        if raw_status not in self._TERMINAL_NO_FILL_RAW_STATUSES:
            # pending_cancel (or anything non-terminal): the broker hasn't
            # finished processing. Write NOTHING -- the row stays live
            # locally so the next pass re-checks; a fill in the processing
            # window is caught by that pass's own sync or this method's
            # own re-fire.
            self.journal.log_system_event(
                Severity.INFO, ENTRY_STALENESS_EVENT_CATEGORY,
                f"cancel for {row['symbol']} ({order_id}) accepted but broker still reports "
                f"status={raw_status!r} (filled_qty=0) -- deferring ledger writes to the next pass.",
                {"order_id": order_id, "broker_order_id": boid, "status": raw_status, **trigger_detail},
            )
            return {"ok": False, "order_id": order_id, "deferred": True,
                    "status": raw_status, **trigger_detail}

        known_states = {s.value for s in OrderState}
        prev_state = OrderState(row["state"]) if row.get("state") in known_states else OrderState.SUBMITTED
        self._event(
            order_id, boid, prev_state, OrderState.CANCELLED, ExecutionSource.ALPACA_PAPER.value,
            {"reason_code": reason_code, **trigger_detail},
        )
        self.journal.conn.execute(
            "UPDATE paper_orders SET state = ? WHERE order_id = ?",
            (OrderState.CANCELLED.value, order_id),
        )
        if row.get("proposal_id"):
            # Additive-lifecycle law (mirrors reconcile()'s own status-guard
            # at its 'filled' transition above): never resurrect a
            # rejected/blocked/already-filled proposal into 'expired'.
            self.journal.conn.execute(
                "UPDATE trade_proposals SET status = ? "
                "WHERE proposal_id = ? AND status NOT IN ('rejected', 'blocked', 'filled')",
                (ProposalStatus.EXPIRED.value, row["proposal_id"]),
            )
        self.journal.conn.commit()

        self.journal.log_system_event(
            Severity.WARNING, ENTRY_STALENESS_EVENT_CATEGORY,
            f"Cancelled entry {row['symbol']} ({order_id}): {reason_code} {trigger_detail}",
            {"order_id": order_id, "broker_order_id": boid, "reason_code": reason_code, **trigger_detail},
        )
        alerts.send_alert(
            self.settings, title=alert_title,
            message=f"{row['symbol']} {row['direction']} entry order {order_id} cancelled "
                    f"({reason_code}): {trigger_detail}",
            priority="default", journal=self.journal,
        )
        return {"ok": True, "order_id": order_id, **trigger_detail}

    def cancel_order_operator(self, identifier: str) -> dict:
        """Operator-invoked targeted cancel (spec 3.7):
        ``python -m alphaos cancel_order <proposal_id|order_id>``. The SAME
        cancel code path as the automated staleness pass, reason
        ORDER_CANCELLED_BY_OPERATOR. Refuses (ok=False) for an unknown id or
        a non-cancellable order (already filled/cancelled/terminal, not an
        alpaca_paper order, or the broker isn't connected) -- the CLI
        translates ``ok=False`` into exit code 1."""
        row = self.journal.one("SELECT * FROM paper_orders WHERE order_id = ?", (identifier,))
        if row is None:
            row = self.journal.one(
                "SELECT * FROM paper_orders WHERE proposal_id = ? ORDER BY id DESC LIMIT 1",
                (identifier,),
            )
        if row is None:
            return {"ok": False, "error": f"no paper_orders row found for id {identifier!r}"}
        if row.get("execution_source") != ExecutionSource.ALPACA_PAPER.value:
            return {
                "ok": False,
                "error": f"order {row['order_id']} is not an alpaca_paper order; "
                         f"operator cancel only targets real broker orders",
            }
        if row.get("state") not in (OrderState.SUBMITTED.value, OrderState.ACCEPTED.value):
            return {
                "ok": False,
                "error": f"order {row['order_id']} is not cancellable (state={row.get('state')!r})",
            }
        pos = self.journal.one("SELECT * FROM positions WHERE order_id = ?", (row["order_id"],))
        if pos is not None:
            return {
                "ok": False,
                "error": f"order {row['order_id']} already has an open position; "
                         f"cancel targets UNFILLED entries only",
            }
        if not (self.real_paper and self.broker_connected and self.alpaca):
            return {"ok": False, "error": "broker not connected; cannot cancel a live order"}
        return self._cancel_order_row(
            row, {"trigger": "operator"}, ReasonCode.ORDER_CANCELLED_BY_OPERATOR.value,
            alert_title=f"AlphaOS: order cancelled by operator — {row['symbol']}",
        )

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
