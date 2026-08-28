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
    ProtectionStatus,
    ReasonCode,
    Severity,
)
from alphaos.data.freshness_guard import FreshnessGuard
from alphaos.execution import entry_staleness, order_schema, protection_watchdog
from alphaos.execution.position_manager import PositionManager
from alphaos.safety import KillSwitch, real_trading_guard
from alphaos.util import alerts, timeutils
from alphaos.util.ids import new_id
from alphaos.util.market_calendar import is_trading_day, trading_days_between

FILL_PRICE_BASIS = "latest_quote_or_bar"
EXEC_MODE_SIM = "internal_simulation"

# ENTRY-TTL-1: system_events category for staleness cancels/partial-fill
# alerts (kept distinct from "execution"/"reconcile" so an operator can
# filter this mechanism's audit trail on its own).
ENTRY_STALENESS_EVENT_CATEGORY = "entry_staleness"
# Dedupe marker category for the partial-fill alert (spec 3.5: alert once,
# not every monitor pass -- see OrderManager._alert_partial_fill_once).
PARTIAL_FILL_ALERT_CATEGORY = "entry_staleness_partial_fill"
# TIME-2: system_events category for the enforcement pass (kept distinct from
# "entry_staleness" -- a different mechanism with a different failure shape --
# and from "execution"/"reconcile", so an operator can filter this
# mechanism's audit trail on its own, same rationale as ENTRY-TTL-1's own
# category constant above).
TIME_EXIT_EVENT_CATEGORY = "time_exit_enforcement"


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

        # TIME-2: enforcement runs AFTER both the state-sync loop above AND
        # the staleness-cancel pass -- mirroring ENTRY-TTL-1's own wiring
        # and for the identical reason (spec 2): a fill or leg-close from
        # THIS SAME pass is already reflected in ``positions`` before any
        # time-exit decision is made here.
        expiry = self.enforce_time_exits()
        results["time_exits_closed"] = expiry["closed"]
        results["time_exits_deferred"] = expiry["deferred"]
        results["time_exits_errors"] = expiry["errors"]
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

    # ------------------------------------------------------- TIME-2: enforcement
    def enforce_time_exits(self, now=None) -> dict:
        """Close broker-managed positions whose OWN stamped max_holding_days
        window has elapsed (docs/roadmap/alphaos-time2-broker-time-exit-spec.md).

        Per position, in this exact order: cancel the live protective legs ->
        VERIFY the cancel (re-read; a non-raising cancel is not proof -- the
        ENTRY-TTL-1 lesson) -> close -> record through the SAME exit path
        every other exit uses. Every fail direction leaves the position OPEN
        AND PROTECTED, never naked -- see ``_enforce_time_exit_one`` and
        ``_recover_unprotected_position`` for exactly how each failure is
        handled. Never raises: an unexpected failure for ONE position is
        caught per-position (audit fixup MEDIUM-1) so it can never abort the
        rest of the pass.

        Eligibility mirrors ``PositionManager._check_exit``'s own two-guard
        rule EXACTLY (imported, never redefined): ``is_trading_day(now)`` AND
        ``trading_days_between(opened, now) >= max_holding_days``, using each
        position's OWN stamped value -- never a forced constant (operator
        ruling, 2026-08-26; see the spec's own header) -- PLUS a broker-
        position precondition (audit fixup BLOCKER-2, spec 2's own eligibility
        list: "the broker still reports the position open this pass -- never
        act on a stale view"). A single ``list_positions()`` snapshot is taken
        ONCE per pass (same convention as ``reconcile()``'s own single state-
        sync loop) -- a symbol the broker reports FLAT is a local/broker
        mismatch this method must never act on; the existing protection
        watchdog already detects and incidents that condition on its own
        scheduled pass."""
        result: dict = {"closed": [], "deferred": [], "errors": []}
        if not self.settings.time_exit_enforcement_enabled:
            return result
        if not (self.real_paper and self.broker_connected and self.alpaca):
            return result
        now = now or timeutils.now_utc()
        now_et_date = timeutils.to_et(now).date()

        try:
            broker_positions = self.alpaca.list_positions()
        except Exception as exc:
            # Audit fixup BLOCKER-2 / spec 2: without a fresh broker-position
            # read, ELIGIBILITY itself can't be verified honestly -- skip the
            # ENTIRE pass rather than act on an unknown/stale view.
            self.journal.log_system_event(
                Severity.WARNING, TIME_EXIT_EVENT_CATEGORY,
                "list_positions() failed while enforcing time exits; skipping this entire pass "
                "rather than acting on a stale/unknown broker view. Will retry next pass.",
                {"error": str(exc)},
            )
            return result
        broker_qty_by_symbol = {
            p["symbol"]: abs(p.get("qty") or 0) for p in broker_positions if p.get("symbol")
        }

        rows = self.journal.query(
            "SELECT * FROM positions WHERE execution_source = ? AND status = 'open'",
            (ExecutionSource.ALPACA_PAPER.value,),
        )
        for pos in rows:
            symbol, position_id = pos.get("symbol"), pos.get("position_id")
            try:
                if not self._time_exit_due(pos, now_et_date):
                    continue
                outcome = self._enforce_time_exit_one(pos, broker_qty_by_symbol)
            except Exception as exc:  # noqa: BLE001 - audit fixup MEDIUM-1
                # One position's unexpected failure must never abort the
                # pass for every OTHER position (this loop previously had no
                # such guard at all).
                try:
                    self.journal.log_system_event(
                        Severity.ERROR, TIME_EXIT_EVENT_CATEGORY,
                        f"{symbol} ({position_id}): unexpected error enforcing its time exit -- "
                        f"this position is skipped THIS PASS only; other positions are unaffected. "
                        f"Will retry.",
                        {"position_id": position_id, "error": str(exc)},
                    )
                except Exception:  # pragma: no cover - logging must never compound the failure
                    pass
                result["errors"].append({"ok": False, "position_id": position_id, "symbol": symbol,
                                         "error": str(exc), "unexpected": True})
                continue
            if outcome.get("ok"):
                result["closed"].append(outcome)
            elif outcome.get("deferred"):
                result["deferred"].append(outcome)
            else:
                result["errors"].append(outcome)
        return result

    def _time_exit_due(self, pos: dict, now_et_date) -> bool:
        """The exact two-guard rule ``PositionManager._check_exit`` enforces
        against a simulated_internal position, reused verbatim (never a
        second, independently-drifting definition) against a broker-managed
        one. ``max_holding_days`` is each position's OWN stamped value -- the
        evaluator picks 1-10 per setup under prompt v4; a legacy position
        stamped 3 (card-v2 era) is due at 3, never floored or forced."""
        max_days = pos.get("max_holding_days")
        if not (max_days and max_days > 0):
            return False
        # PositionManager._opened_et_date is the SAME helper _check_exit and
        # close_position()'s own holding_trading_days column both use --
        # reused here (not reimplemented) so the live exit check and every
        # other reader of "days held" agree on the exact same entry date.
        opened_et_date = self.positions._opened_et_date(pos)
        if opened_et_date is None:
            return False
        if not is_trading_day(now_et_date):
            return False
        return trading_days_between(opened_et_date, now_et_date) >= max_days

    def _enforce_time_exit_one(self, pos: dict, broker_qty_by_symbol: dict) -> dict:
        """Cancel -> verify -> close -> record for ONE position past its own
        window. Every branch returns a result dict; the caller
        (``enforce_time_exits``) additionally guards against an unexpected
        raise here (audit fixup MEDIUM-1) so this docstring makes no
        stronger claim than that."""
        symbol, position_id = pos["symbol"], pos["position_id"]

        # Audit fixup BLOCKER-2 / spec 2's own eligibility list: "the broker
        # still reports the position open this pass -- never act on a stale
        # view." A symbol the broker reports FLAT while the local ledger
        # still says open is exactly the protection watchdog's own
        # CLOSED_MISMATCH condition (it closed via a path this pass's state-
        # sync loop didn't observe -- e.g. a PRIOR pass's own deferred
        # liquidation completing between passes; this is precisely how the
        # original build's BLOCKER-2 defect was reachable). Stand down
        # entirely: no cancel, no close, no recovery attempt. The existing
        # protection watchdog already detects and incidents this condition
        # on its own scheduled pass.
        if not broker_qty_by_symbol.get(symbol):
            self.journal.log_system_event(
                Severity.WARNING, TIME_EXIT_EVENT_CATEGORY,
                f"{symbol} ({position_id}) is past its {pos.get('max_holding_days')}td window, but "
                f"the broker reports this symbol FLAT (local ledger still shows it open) -- standing "
                f"down; never acting on a stale view. The protection watchdog's own mismatch "
                f"detection will incident this on its next pass.",
                {"position_id": position_id},
            )
            # Audit fixup R3: this is a correct, intended stand-down, not a
            # failure -- classify it as a deferral (retried next pass, same
            # as every other "not safe to act this pass" outcome) so it
            # doesn't inflate the error count an operator reads off
            # enforce_time_exits()'s summary.
            return {"ok": False, "deferred": True, "position_id": position_id, "symbol": symbol,
                    "stale_view": True, "broker_flat": True}

        boid = pos.get("broker_order_id")
        if not boid:
            self.journal.log_system_event(
                Severity.WARNING, TIME_EXIT_EVENT_CATEGORY,
                f"{symbol} ({position_id}) is past its {pos.get('max_holding_days')}td window but "
                f"has no broker_order_id -- cannot locate its protective legs; needs operator review.",
                {"position_id": position_id},
            )
            return {"ok": False, "position_id": position_id, "symbol": symbol,
                    "error": "missing_broker_order_id"}

        # --- re-read the broker's CURRENT view of the bracket's legs (never
        # act on a stale local view) ---
        try:
            norm = self.alpaca.get_order(boid)
        except Exception as exc:
            self.journal.log_system_event(
                Severity.WARNING, TIME_EXIT_EVENT_CATEGORY,
                f"{symbol} ({position_id}): get_order failed while enforcing the time exit; "
                f"skipping this pass, will retry.",
                {"position_id": position_id, "error": str(exc)},
            )
            return {"ok": False, "deferred": True, "position_id": position_id, "symbol": symbol,
                    "error": str(exc)}

        legs = [leg for leg in (norm.get("legs") or []) if leg.get("role") in ("stop_loss", "take_profit")]
        if any((leg.get("filled_qty") or 0) > 0 for leg in legs):
            # A protective leg already filled (stop/target hit) since the
            # state-sync loop ran earlier THIS SAME pass -- the broker's view
            # has moved past our stale local one. Never act on it: the
            # normal bracket-leg-fill branch of reconcile() records the real
            # exit on the next pass.
            self.journal.log_system_event(
                Severity.INFO, TIME_EXIT_EVENT_CATEGORY,
                f"{symbol} ({position_id}): a protective leg already filled at the broker; "
                f"time-exit enforcement stands down for this position.",
                {"position_id": position_id},
            )
            return {"ok": False, "position_id": position_id, "symbol": symbol, "stale_view": True}

        # --- step 1: cancel every live protective leg ---
        open_legs = [leg for leg in legs if leg.get("state") not in
                    (OrderState.CANCELLED.value, OrderState.EXPIRED.value, OrderState.REJECTED.value)]
        cancel_errors = []
        for leg in open_legs:
            leg_boid = leg.get("broker_order_id")
            if not leg_boid:
                cancel_errors.append({"leg": leg.get("role"), "error": "missing_leg_broker_order_id"})
                continue
            try:
                self.alpaca.cancel_order(leg_boid)
            except Exception as exc:
                cancel_errors.append({"leg": leg.get("role"), "broker_order_id": leg_boid, "error": str(exc)})

        # --- step 2: VERIFY -- re-read every leg; a non-raising cancel is
        # only a REQUEST, never proof (the ENTRY-TTL-1 audit's lesson, spec
        # 2). Any leg not confirmed terminally cancelled -> STOP, no close. -
        verified = True
        verify_detail = []
        for leg in open_legs:
            leg_boid = leg.get("broker_order_id")
            if not leg_boid:
                verified = False
                continue
            try:
                leg_norm = self.alpaca.get_order(leg_boid)
            except Exception as exc:
                verified = False
                verify_detail.append({"leg": leg.get("role"), "broker_order_id": leg_boid, "error": str(exc)})
                continue
            if (leg_norm.get("filled_qty") or 0) > 0:
                # Raced a fill during the cancel window -- the same hole
                # ENTRY-TTL-1's audit found on the entry side. Never close on
                # top of this; the normal leg-fill reconcile path takes over.
                verified = False
                verify_detail.append({"leg": leg.get("role"), "broker_order_id": leg_boid, "raced_fill": True})
                continue
            raw_status = (leg_norm.get("status") or "").lower()
            if raw_status not in self._TERMINAL_NO_FILL_RAW_STATUSES:
                verified = False
                verify_detail.append({"leg": leg.get("role"), "broker_order_id": leg_boid, "status": raw_status})

        if cancel_errors or not verified:
            self.journal.log_system_event(
                Severity.WARNING, TIME_EXIT_EVENT_CATEGORY,
                f"{symbol} ({position_id}): time-exit cancel not fully verified -- NO close "
                f"submitted; the position stays open and protected. Will retry next pass.",
                {"position_id": position_id, "cancel_errors": cancel_errors, "verify_detail": verify_detail},
            )
            alerts.send_alert(
                self.settings,
                title=f"AlphaOS: time-exit cancel unverified — {symbol}",
                message=f"{symbol} ({position_id}) is past its {pos.get('max_holding_days')}td window "
                        f"but its protective legs could not be verified cancelled; left untouched "
                        f"and protected. Will retry next pass.",
                priority="high", journal=self.journal,
            )
            return {"ok": False, "deferred": True, "position_id": position_id, "symbol": symbol,
                    "cancel_errors": cancel_errors, "verify_detail": verify_detail}

        # --- step 3: close the now-unprotected (legs verified gone) position ---
        try:
            close_norm = self.alpaca.close_position(symbol)
        except Exception as exc:
            return self._recover_unprotected_position(pos, exc)

        exit_price = close_norm.get("filled_avg_price")
        if exit_price is None:
            # The liquidation order was accepted but hasn't reported a fill
            # yet -- never guess a price. The position is flat/closing at the
            # broker either way (no legs left to protect, so this is NOT the
            # naked case), just not yet RECORDED locally; defer to a later
            # pass rather than write a guessed exit. The broker-position
            # precondition at the top of this method is what makes that safe
            # (audit fixup BLOCKER-2): if this liquidation fills before the
            # next pass, that pass sees the symbol FLAT and stands down
            # instead of retrying close()/recovery blind.
            self.journal.log_system_event(
                Severity.WARNING, TIME_EXIT_EVENT_CATEGORY,
                f"{symbol} ({position_id}): close submitted but no fill price yet -- will record "
                f"once the broker reports a fill.",
                {"position_id": position_id, "broker_order_id": close_norm.get("broker_order_id")},
            )
            return {"ok": False, "deferred": True, "position_id": position_id, "symbol": symbol,
                    "pending_close_order": close_norm.get("broker_order_id")}

        # Audit fixup HIGH-1: a PARTIAL liquidation fill must never be
        # recorded as a full close -- PositionManager.close_position() always
        # uses the LOCAL qty, so doing so here would misstate the exit AND
        # leave the unfilled residual open at the broker with its protective
        # legs already cancelled (naked). Alpaca's close_position(symbol)
        # takes no qty parameter -- it always flattens whatever it CURRENTLY
        # holds, so a retry next pass is self-correcting for the symbol's
        # actual remaining quantity; this method does not itself attempt to
        # reconcile a running total across multiple partial fills (a known,
        # declared gap -- see the ticket report).
        filled_qty = close_norm.get("filled_qty") or 0
        local_qty = float(pos.get("qty") or 0)
        if filled_qty and local_qty and abs(filled_qty - local_qty) > 1e-6:
            detail = (f"{symbol}: liquidation PARTIALLY filled ({filled_qty}/{local_qty} shares) -- "
                      f"the residual is open at the broker with NO protective legs (already "
                      f"cancelled this pass). Never recording a full close on a partial fill.")
            self.journal.log_system_event(
                Severity.CRITICAL, TIME_EXIT_EVENT_CATEGORY, detail,
                {"position_id": position_id, "filled_qty": filled_qty, "local_qty": local_qty,
                 "broker_order_id": close_norm.get("broker_order_id")},
            )
            try:
                protection_watchdog.open_protection_incident(
                    self.journal, pos, protection_status=ProtectionStatus.UNPROTECTED.value,
                    severity=Severity.CRITICAL.value, detail=detail, stop_live=False, target_live=False,
                    broker_position_exists=True, broker_qty=filled_qty and (local_qty - filled_qty),
                )
            except Exception:  # pragma: no cover - the alert below is the load-bearing signal
                pass
            alerts.send_alert(
                self.settings,
                title=f"AlphaOS: CRITICAL — {symbol} partial close, residual unprotected",
                message=detail, priority="high", journal=self.journal,
            )
            return {"ok": False, "deferred": True, "position_id": position_id, "symbol": symbol,
                    "partial_fill": True, "filled_qty": filled_qty, "local_qty": local_qty}

        # --- step 4: record through the SAME exit path every other exit uses ---
        try:
            ex = self.positions.close_position(
                position_id, exit_price, "time_expiry", triggered_by="order_manager_time_exit",
                execution_source=ExecutionSource.ALPACA_PAPER.value,
                broker_order_id=close_norm.get("broker_order_id"),
            )
            self.journal.log_system_event(
                Severity.WARNING, TIME_EXIT_EVENT_CATEGORY,
                f"Time exit enforced for {symbol} ({position_id}): past its "
                f"{pos.get('max_holding_days')}td window -- legs cancelled and verified, closed @ "
                f"{exit_price}.",
                {"position_id": position_id, "broker_order_id": close_norm.get("broker_order_id")},
            )
        except Exception as exc:
            # Audit fixup MEDIUM-1: the broker close ALREADY SUCCEEDED at
            # this point -- this is a genuine ledger/broker mismatch
            # (closed_mismatch: broker flat, local still open), not an
            # unprotected-but-open position. Never let a DB/ledger failure
            # here escape unhandled -- it must not abort the rest of the
            # pass for OTHER positions, and it must leave a precise,
            # actionable record rather than a generic error.
            detail = (f"{symbol}: broker close SUCCEEDED (filled @ {exit_price}) but recording the "
                      f"exit failed ({exc}) -- the LOCAL ledger still shows this position open while "
                      f"the broker is flat. Reconcile manually; do NOT retry the time exit (there is "
                      f"nothing left to close at the broker).")
            self.journal.log_system_event(
                Severity.CRITICAL, TIME_EXIT_EVENT_CATEGORY, detail,
                {"position_id": position_id, "error": str(exc), "exit_price": exit_price,
                 "broker_order_id": close_norm.get("broker_order_id")},
            )
            try:
                protection_watchdog.open_protection_incident(
                    self.journal, pos, protection_status=ProtectionStatus.CLOSED_MISMATCH.value,
                    severity=Severity.CRITICAL.value, detail=detail, broker_position_exists=False,
                )
            except Exception:  # pragma: no cover - the alert below is the load-bearing signal
                pass
            try:
                alerts.send_alert(
                    self.settings, title=f"AlphaOS: CRITICAL — {symbol} exit not recorded",
                    message=detail, priority="high", journal=self.journal,
                )
            except Exception:  # pragma: no cover - never compound the failure
                pass
            return {"ok": False, "position_id": position_id, "symbol": symbol,
                    "error": str(exc), "closed_mismatch": True, "critical": True}
        return {"ok": True, "position_id": position_id, "symbol": symbol, "exit": ex}

    def _recover_unprotected_position(self, pos: dict, close_exc: Exception) -> dict:
        """Reached only when the protective legs were already cancelled and
        VERIFIED gone, and the subsequent close call raised -- the position
        MAY be open at the broker with NO protection (a raised close() is
        itself ambiguous: it could also mean the broker already considers
        the symbol flat, e.g. a prior deferred liquidation completed
        between passes -- audit fixup BLOCKER-2). Before ever submitting a
        replacement order, this re-checks the broker's CURRENT position
        state fresh (not the top-of-pass snapshot ``enforce_time_exits``
        already took): if the broker is flat, submitting a protective OCO
        would be an OPENING order onto nothing, not protection -- so this
        stands down and hands off to the SAME incident mechanism with the
        accurate ``closed_mismatch`` status instead. Only when the broker
        genuinely still shows the position open does this try once to
        re-place a stop+target OCO pair (gated on the kill switch --
        KillSwitch's own contract is "block all NEW orders", and this is
        one -- audit fixup HIGH-2); if that also fails (or is blocked), a
        CRITICAL protection incident is opened through the SAME mechanism
        the broker protection watchdog uses, so it blocks new entries
        exactly like any other unprotected position already does."""
        symbol, position_id = pos["symbol"], pos["position_id"]
        self.journal.log_system_event(
            Severity.ERROR, TIME_EXIT_EVENT_CATEGORY,
            f"{symbol} ({position_id}): close FAILED after protective legs were cancelled and "
            f"verified. Checking the broker's current position state before attempting recovery.",
            {"position_id": position_id, "error": str(close_exc)},
        )

        # Audit fixup BLOCKER-2: fresh (not stale-per-pass) broker read.
        try:
            broker_positions = self.alpaca.list_positions()
        except Exception as exc:
            broker_positions = None  # unknown -- fail toward NOT submitting blind, below
            list_error = str(exc)
        else:
            list_error = None
        broker_qty = None
        if broker_positions is not None:
            broker_qty = next(
                (abs(p.get("qty") or 0) for p in broker_positions if p.get("symbol") == symbol), 0.0
            )

        if broker_positions is not None and not broker_qty:
            # Confirmed flat: there is nothing left to protect. Never submit
            # a new order here -- that would OPEN a fresh naked position,
            # not protect one. Hand off with the ACCURATE status.
            detail = (f"{symbol}: time-exit close failed ({close_exc}), but the broker now reports "
                      f"this symbol FLAT -- it closed via some other path (most likely this same "
                      f"close order completing between passes). Standing down: NOT submitting a new "
                      f"protective order (that would open a fresh naked position). The LOCAL ledger "
                      f"still shows this position open -- reconcile manually.")
            self.journal.log_system_event(
                Severity.CRITICAL, TIME_EXIT_EVENT_CATEGORY, detail, {"position_id": position_id},
            )
            try:
                protection_watchdog.open_protection_incident(
                    self.journal, pos, protection_status=ProtectionStatus.CLOSED_MISMATCH.value,
                    severity=Severity.CRITICAL.value, detail=detail, broker_position_exists=False,
                    broker_qty=0.0,
                )
            except Exception:  # pragma: no cover - the alert below is the load-bearing signal
                pass
            alerts.send_alert(
                self.settings, title=f"AlphaOS: CRITICAL — {symbol} ledger/broker mismatch",
                message=detail, priority="high", journal=self.journal,
            )
            return {"ok": False, "position_id": position_id, "symbol": symbol,
                    "close_error": str(close_exc), "closed_mismatch": True, "critical": True}

        # The broker genuinely still shows this position open (or its state
        # is UNKNOWN -- list_positions() itself failed) -- either way, only
        # a genuine, verified-open broker position is ever eligible for a
        # replacement OCO below.
        replace_error = None
        new_boid = None
        if broker_positions is None:
            replace_error = f"could not verify current broker position state ({list_error}); refusing to submit a new order blind"
        elif self.kill_switch.is_engaged():
            # Audit fixup HIGH-2: KillSwitch's own contract is "presence
            # means engaged -- block all NEW orders." submit_protective_oco
            # is a new order. The cancel/close path above is deliberately
            # left ungated (an explicit, not-yet-ruled operator question --
            # see the ticket report); this ONE new-order path is not.
            replace_error = f"kill switch engaged ({self.kill_switch.reason()}); new-order submission blocked"
        else:
            try:
                new_order = self.alpaca.submit_protective_oco(
                    symbol=symbol, qty=pos.get("qty"), direction=pos.get("direction"),
                    stop=pos.get("stop_price"), target=pos.get("target_price"),
                    tif=self.settings.protective_order_time_in_force,
                )
            except Exception as exc:
                replace_error = str(exc)
            else:
                new_boid = new_order.get("broker_order_id")

        # Audit fixup R2: a submit that raised nothing but returned no
        # broker_order_id is NOT a successful re-protect -- there is nothing
        # to persist, so the next pass could never find the right legs.
        # Same remedy as R1 below: treat it as a replace failure, never as
        # success.
        if replace_error is None and not new_boid:
            replace_error = "protective OCO submitted but the broker response carried no broker_order_id"

        if replace_error is None:
            # Audit fixup BLOCKER-1 / R1: persist the RE-PLACED OCO's own
            # broker_order_id onto the position BEFORE reporting success.
            # Without this, the NEXT pass's ``_enforce_time_exit_one`` would
            # re-read the OLD (now dead, all-legs-cancelled) bracket parent,
            # see zero open legs, "verify" vacuously, and close the position
            # while the NEWLY placed stop/target legs stay live and
            # orphaned at the broker -- a naked short/long waiting to
            # trigger with no position or stop behind it. Pointing
            # broker_order_id at the NEW protective order means the next
            # pass cancels the RIGHT legs (and, as a side effect, fixes
            # protection_watchdog.check_position() too -- it also reads
            # this same column, so it now sees the position as genuinely
            # PROTECTED instead of opening a false CRITICAL "no downside
            # protection" incident on top of a real one).
            #
            # R1: the OCO is now LIVE at the broker -- if this write itself
            # fails (disk I/O, locked DB, full disk), reporting success
            # anyway reproduces the EXACT B1 orphan through a non-broker
            # failure: the position row keeps pointing at the dead bracket,
            # the next pass verifies vacuously against it, and closes while
            # the re-placed stop/target stay resting with nothing behind
            # them. So a failed persist here is NOT reported as success --
            # it falls through to the SAME CRITICAL incident path below,
            # exactly like a failed re-placement, turning a silent orphan
            # into a blocking incident a human sees.
            try:
                self.journal.conn.execute(
                    "UPDATE positions SET broker_order_id = ? WHERE position_id = ?",
                    (new_boid, position_id),
                )
                self.journal.conn.commit()
            except Exception as exc:
                replace_error = (
                    f"protective OCO submitted (broker_order_id={new_boid}) but persisting it "
                    f"locally failed ({exc}); treating as a failed re-placement so this is never "
                    f"silently reported as protected"
                )
            else:
                self.journal.log_system_event(
                    Severity.WARNING, TIME_EXIT_EVENT_CATEGORY,
                    f"{symbol} ({position_id}): protection re-placed after the time-exit close "
                    f"failed. Position remains open; will retry the time exit next pass.",
                    {"position_id": position_id, "close_error": str(close_exc), "new_broker_order_id": new_boid},
                )
                alerts.send_alert(
                    self.settings,
                    title=f"AlphaOS: time-exit close failed, protection restored — {symbol}",
                    message=f"{symbol} ({position_id})'s time-exit close failed ({close_exc}); its "
                            f"stop and target were re-placed. Position remains open; will retry.",
                    priority="high", journal=self.journal,
                )
                return {"ok": False, "deferred": True, "position_id": position_id, "symbol": symbol,
                        "close_error": str(close_exc), "reprotected": True, "new_broker_order_id": new_boid}

        # Re-placement was skipped/blocked/failed/unpersisted -- open a
        # CRITICAL protection incident through the exact same table/dedup/
        # supersede logic the periodic broker protection watchdog uses
        # (reused, not reimplemented), so this position blocks new entries
        # exactly like any other unprotected position and shows up in the
        # same incident queue an operator already knows to check.
        detail = (f"{symbol}: time-exit close failed ({close_exc}) AND re-placing its protective "
                  f"stop/target ALSO failed or was blocked ({replace_error}) -- this position may "
                  f"have NO stop/target at the broker. Manual intervention required immediately.")
        try:
            protection_watchdog.open_protection_incident(
                self.journal, pos, protection_status=ProtectionStatus.UNPROTECTED.value,
                severity=Severity.CRITICAL.value, detail=detail, stop_live=False, target_live=False,
                broker_position_exists=True, broker_qty=broker_qty,
            )
        except Exception:  # pragma: no cover - the alert below is the load-bearing signal
            pass
        alerts.send_alert(
            self.settings,
            title=f"AlphaOS: CRITICAL — {symbol} may be unprotected",
            message=f"{symbol} ({position_id})'s time-exit close failed and re-placing its "
                    f"protective stop/target also failed or was blocked ({replace_error}). This "
                    f"position may have no stop/target at the broker. Manual intervention required "
                    f"immediately.",
            priority="high", journal=self.journal,
        )
        return {"ok": False, "position_id": position_id, "symbol": symbol,
                "close_error": str(close_exc), "replace_error": replace_error, "critical": True}


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
