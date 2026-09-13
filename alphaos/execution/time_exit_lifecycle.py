"""Durable, paper-only reduction lifecycle using existing order/fill tables.

The write-ahead intent precedes cancellations and submissions. A stable client
ID recovers an ambiguous submission; an unknown broker view never authorizes a
second order. No broker request is made while holding a database transaction.
"""
from __future__ import annotations

from datetime import datetime, time
from hashlib import sha256
from zoneinfo import ZoneInfo

from alphaos.constants import ProtectionStatus, Severity
from alphaos.execution import ledger_ops, protection_watchdog
from alphaos.execution.protection_contract import verified_live_roles
from alphaos.util import alerts, timeutils
from alphaos.util.ids import new_id

TERMINAL = {"canceled", "cancelled", "expired", "rejected", "filled"}
ACTIVE_PHASES = {"canceling", "submitting", "working", "unknown"}


class TimeExitLifecycle:
    def __init__(self, manager):
        self.om = manager
        self.journal = manager.journal
        self.broker = manager.alpaca

    def orders(self, position_id, purpose=None):
        rows = self.journal.query(
            "SELECT * FROM paper_orders WHERE execution_source='alpaca_paper' "
            "AND order_type IN ('market','oco') ORDER BY id",
        )
        return [row for row in rows if ledger_ops.metadata(row).get("position_id") == position_id
                and (purpose is None or ledger_ops.metadata(row).get("purpose") == purpose)]

    def pending(self, pos):
        return [row for row in self.orders(pos["position_id"])
                if ledger_ops.metadata(row).get("phase") in ACTIVE_PHASES]

    def _set(self, row, phase, norm=None):
        values = {"raw_request_json": {**ledger_ops.metadata(row), "phase": phase}}
        if norm:
            values.update({"broker_order_id": norm.get("broker_order_id"),
                           "state": norm.get("state"), "raw_response_json": norm})
        with ledger_ops.atomic(self.journal) as journal:
            ledger_ops.update(journal, "paper_orders", "order_id", row["order_id"], values)
            if ledger_ops.metadata(row).get("phase") != phase or row.get("state") != values.get("state", row.get("state")):
                journal.insert("order_events", {
                    "event_id": new_id("oev"), "order_id": row["order_id"],
                    "broker_order_id": values.get("broker_order_id", row.get("broker_order_id")),
                    "prev_state": row.get("state"), "new_state": values.get("state", row.get("state")),
                    "execution_source": "alpaca_paper", "message": f"durable execution phase: {phase}",
                })
        row.update(values)
        return row

    def incident(self, pos, detail, *, unknown=False, flat=False, deferred=True):
        status = (ProtectionStatus.CLOSED_MISMATCH.value if flat else
                  ProtectionStatus.UNVERIFIABLE.value if unknown else ProtectionStatus.UNPROTECTED.value)
        detail = f"{pos['symbol']}: {detail}"
        protection_watchdog.open_protection_incident(
            self.journal, pos, protection_status=status, severity=Severity.CRITICAL.value,
            detail=detail, broker_position_exists=False if flat else None,
        )
        self.journal.log_system_event(Severity.CRITICAL, "time_exit_enforcement", detail,
                                      {"position_id": pos["position_id"]})
        alerts.send_alert(self.om.settings, "AlphaOS: execution needs review", detail,
                          priority="high", journal=self.journal)
        return {"ok": False, "deferred": deferred, "critical": True, "position_id": pos["position_id"],
                "symbol": pos["symbol"], "error": detail, "closed_mismatch": flat, "broker_flat": flat}

    def _fresh(self, pos, allowed_ids=()):
        """Verify quantity, direction, unique local ownership and competing orders."""
        duplicates = self.journal.query(
            "SELECT position_id FROM positions WHERE status='open' AND symbol=?",
            (pos["symbol"],),
        )
        if len(duplicates) != 1:
            raise ValueError("symbol has ambiguous local position ownership")
        positions = self.broker.list_positions()
        bp = next((p for p in positions if p["symbol"] == pos["symbol"]), None)
        if bp is None:
            raise ValueError("broker is flat while local position is open")
        qty = abs(float(bp.get("qty") or 0))
        side = bp.get("side") or ("short" if float(bp.get("qty") or 0) < 0 else "long")
        if side != pos["direction"] or abs(qty - float(pos["qty"])) > 1e-6:
            raise ValueError("broker/local quantity or direction mismatch")
        if qty <= 0:
            raise ValueError("no verified quantity to protect or reduce")
        parents = self.broker.list_open_orders()
        orders = parents + [{**leg, "symbol": parent.get("symbol")}
                            for parent in parents for leg in parent.get("legs", [])]
        competing = [o for o in orders if o.get("symbol") == pos["symbol"]
                     and o.get("broker_order_id") not in set(allowed_ids)
                     and (o.get("status") or "").lower() not in TERMINAL]
        if competing:
            raise ValueError("untracked or competing broker orders reserve this position")
        # A fill can move quantity while the open-order snapshot is fetched.
        # Read position last as well, immediately before the caller's submission.
        again = next((p for p in self.broker.list_positions() if p["symbol"] == pos["symbol"]), None)
        if again is None or abs(abs(float(again.get("qty") or 0)) - qty) > 1e-6:
            raise ValueError("broker quantity changed while verifying orders")
        again_side = again.get("side") or ("short" if float(again.get("qty") or 0) < 0 else "long")
        if again_side != side:
            raise ValueError("broker direction changed while verifying orders")
        return qty

    def _regular_session(self, now):
        """Use the broker calendar, including holidays and early closing days."""
        now = timeutils.to_et(now)
        day = now.date()
        sessions = self.broker.get_calendar(day, day)
        def boundary(value):
            value = str(value)
            if "T" in value or " " in value:
                result = datetime.fromisoformat(value)
            else:
                result = datetime.combine(day, time.fromisoformat(value))
            return result.replace(tzinfo=ZoneInfo("America/New_York")) if result.tzinfo is None else result
        session = next((row for row in sessions if str(row["date"])[:10] == day.isoformat()), None)
        return bool(session and boundary(session["open"]) <= now < boundary(session["close"]))

    def _new(self, pos, purpose, phase):
        # A deterministic sequence+UNIQUE order_id elects one submission owner
        # even when two processes observed the same state before this lock.
        with ledger_ops.atomic(self.journal) as journal:
            existing = self.orders(pos["position_id"], purpose)
            for row in existing:
                if ledger_ops.metadata(row).get("phase") in ACTIVE_PHASES:
                    return row, False
            digest = sha256(f"{pos['position_id']}:{purpose}:{len(existing)}".encode()).hexdigest()[:28]
            order_id = f"ord_{digest}"
            total = float(pos["qty"]) + float(ledger_ops.exit_totals(journal, pos)["qty"])
            row = {
                "order_id": order_id, "client_order_id": f"aos_{digest}",
                "symbol": pos["symbol"], "direction": pos["direction"],
                "side": "buy_to_cover" if pos["direction"] == "short" else "sell",
                "qty": pos["qty"], "order_type": "oco" if purpose == "reprotect" else "market",
                "order_class": "oco" if purpose == "reprotect" else "simple",
                "state": "submitted", "execution_source": "alpaca_paper",
                "execution_provider": "alpaca_paper", "execution_mode": "alpaca_paper",
                "proposal_id": pos.get("proposal_id"), "candidate_id": pos.get("candidate_id"),
                "trade_id": pos.get("trade_id"), "strategy": pos.get("strategy"),
                "time_in_force": "gtc" if purpose == "reprotect" else "day",
                "submitted_at": timeutils.stamp().utc,
                "raw_request_json": {"purpose": purpose, "position_id": pos["position_id"],
                                     "original_qty": total, "phase": phase,
                                     "protection_order_id": pos.get("broker_order_id")},
            }
            journal.insert("paper_orders", row)
            return row, True

    def _lookup(self, row):
        if row.get("broker_order_id"):
            return self.broker.get_order(row["broker_order_id"])
        return self.broker.get_order_by_client_id(row["client_order_id"])

    def _observe_close(self, pos, row, norm):
        if norm.get("symbol") != pos["symbol"] or norm.get("side") != (
                "buy" if pos["direction"] == "short" else "sell"):
            return self.incident(pos, "closing order identity/side does not match its durable intent", unknown=True)
        if float(norm.get("filled_qty") or 0) > float(row["qty"]) + 1e-6:
            return self.incident(pos, "closing fill exceeds the verified submitted quantity", unknown=True)
        self._set(row, "working", norm)
        if (norm.get("filled_qty") or 0) > 0:
            try:
                outcome = self.om._record_broker_exit(pos, norm, "time_expiry", order=row)
            except Exception as exc:
                try:
                    flat = not any(p["symbol"] == pos["symbol"] for p in self.broker.list_positions())
                except Exception:
                    flat = False
                return self.incident(pos, f"could not record liquidation fill ({exc}); durable intent retained",
                                     unknown=not flat, flat=flat, deferred=False)
            if outcome:
                self._set(row, "complete", norm)
                return {"ok": True, "position_id": pos["position_id"], "symbol": pos["symbol"], "exit": outcome}
        pos = self.journal.one("SELECT * FROM positions WHERE position_id=?", (pos["position_id"],))
        if float(pos["qty"]) <= 1e-6:
            return self.incident(pos, "fill accounting implies zero residual; broker flatness still unverified", unknown=True)
        if (norm.get("status") or "").lower() in TERMINAL:
            self._set(row, "terminal", norm)
            return self.recover(pos, "liquidation became terminal before the whole position was closed")
        # A market order is working, not a protective stop. Block other entries
        # until it finishes; never overlay a second liquidation or OCO on it.
        result = self.incident(pos, "tracked liquidation is pending; residual has no protective stop")
        result["pending_close_order"] = norm.get("broker_order_id")
        result["partial_fill"] = bool(norm.get("filled_qty"))
        result["filled_qty"] = norm.get("filled_qty")
        return result

    def advance(self, pos, now=None):
        now = now or timeutils.now_utc()
        pending = self.pending(pos)
        protection = next((r for r in pending if ledger_ops.metadata(r)["purpose"] == "reprotect"), None)
        if protection:
            return self.recover(pos, "resuming protection recovery", row=protection)
        row = next((r for r in pending if ledger_ops.metadata(r)["purpose"] == "time_exit"), None)
        if row and ledger_ops.metadata(row)["phase"] != "canceling":
            try:
                norm = self._lookup(row)
            except Exception as exc:
                return self.incident(pos, f"closing order state is unknown ({exc}); no duplicate submitted", unknown=True)
            if norm:
                return self._observe_close(pos, row, norm)
            # A pre-submit crash or a definitely absent submission. Do not retry
            # blind; return the existing position to protection first.
            self._set(row, "terminal")
            return self.recover(pos, "durable closing intent has no broker order")

        try:
            regular = self._regular_session(now)
        except Exception as exc:
            return self.incident(pos, f"broker session cannot be verified ({exc}); protection untouched", unknown=True)
        if not regular:
            # An already-started cancellation may have removed both legs. It
            # may recover verified protection, but must not queue a market close.
            if row:
                recovered = self.recover(pos, "regular session ended during protective cancellation")
                if recovered.get("reprotected"):
                    self._set(row, "terminal")
                return recovered
            return {"ok": False, "deferred": True, "position_id": pos["position_id"],
                    "symbol": pos["symbol"], "session_closed": True}

        try:
            norm = self.broker.get_order(pos.get("broker_order_id"))
            legs = norm.get("legs") or []
            if norm.get("order_class") == "bracket" and norm.get("status") == "partially_filled":
                raise ValueError("entry remains partially filled; unfilled remainder needs review")
            if not legs:
                raise ValueError("cannot verify protective order lineage/legs")
            allowed = [norm.get("broker_order_id")] + [leg.get("broker_order_id") for leg in legs]
            self._fresh(pos, allowed)
        except Exception as exc:
            return self.incident(pos, f"time exit cannot establish fresh position/order ownership ({exc})",
                                 unknown=True, flat="broker is flat" in str(exc))
        if any((leg.get("filled_qty") or 0) > 0 for leg in legs):
            for leg in legs:
                if (leg.get("filled_qty") or 0) > 0:
                    self.om._record_broker_exit(pos, leg, "target" if leg["role"] == "take_profit" else "stop")
            return {"ok": False, "deferred": True, "position_id": pos["position_id"], "symbol": pos["symbol"], "stale_view": True}
        if row is None:
            row, owner = self._new(pos, "time_exit", "canceling")
            if not owner:
                return {"ok": False, "deferred": True, "position_id": pos["position_id"], "symbol": pos["symbol"]}

        cancel_errors = []
        for leg in legs:
            if (leg.get("status") or "").lower() in TERMINAL:
                continue
            try:
                self.broker.cancel_order(leg["broker_order_id"])
            except Exception as exc:
                cancel_errors.append(str(exc))
        verified = True
        # Re-read EVERY leg, including ones whose earlier normalized state looked
        # canceled. A pending_cancel is a live request, not terminal proof.
        for leg in legs:
            try:
                checked = self.broker.get_order(leg["broker_order_id"])
                if checked.get("filled_qty"):
                    self.om._record_broker_exit(pos, {**checked, "role": leg["role"]},
                                                "target" if leg["role"] == "take_profit" else "stop")
                    verified = False
                elif (checked.get("status") or "").lower() not in TERMINAL:
                    verified = False
            except Exception:
                verified = False
        if not verified:
            result = self.incident(pos, "protective cancellation is incomplete/unknown; protection may be absent", unknown=True)
            result["cancel_errors"] = cancel_errors
            return result
        # A cancel endpoint may error because its sibling already canceled it.
        # Verified terminal reads, not request return codes, decide safety.
        try:
            qty = self._fresh(pos)
        except Exception as exc:
            return self.incident(pos, f"post-cancel broker state is not safe for liquidation ({exc})", unknown=True)
        if abs(qty - float(row["qty"])) > 1e-6:
            return self.incident(pos, "position quantity changed during cancellation", unknown=True)
        self._set(row, "submitting")  # committed before the network side effect
        try:
            close = self.broker.submit_position_reduction(pos["symbol"], qty, pos["direction"], row["client_order_id"])
        except Exception as exc:
            try:
                close = self.broker.get_order_by_client_id(row["client_order_id"])
            except Exception as lookup_exc:
                self._set(row, "unknown")
                return self.incident(pos, f"close submission/lookup is ambiguous ({exc}; {lookup_exc})", unknown=True)
            if close is None:
                self._set(row, "terminal")
                return self.recover(pos, f"close was not accepted ({exc})")
        return self._observe_close(pos, row, close)

    def recover(self, pos, reason, row=None):
        """Restore verified existing exposure, including while entries are halted.

        Fresh quantity/direction, no competing orders, durable client ID and a
        re-read of both protective legs are mandatory. Unknown state is never
        transformed into permission to submit.
        """
        try:
            if row:
                norm = self._lookup(row)
                if norm:
                    return self._adopt_protection(pos, row, norm)
                self._set(row, "terminal")
                return self.incident(pos, "protection intent is absent at broker; operator review required", unknown=True)
            existing = next((r for r in self.pending(pos)
                             if ledger_ops.metadata(r)["purpose"] == "reprotect"), None)
            if existing:
                return self.recover(pos, reason, existing)
            old_protection = self.broker.get_order(pos.get("broker_order_id"))
            if any((leg.get("status") or "").lower() not in TERMINAL
                   for leg in old_protection.get("legs", [])):
                raise ValueError("prior protective orders are not verified terminal; cannot overlay another OCO")
            qty = self._fresh(pos)
        except Exception as exc:
            return self.incident(pos, f"cannot safely restore protection ({reason}; {exc})", unknown=True)
        row, owner = self._new(pos, "reprotect", "submitting")
        if not owner:
            return self.recover(pos, reason, row)
        try:
            norm = self.broker.submit_protective_oco(
                symbol=pos["symbol"], qty=qty, direction=pos["direction"], stop=pos["stop_price"],
                target=pos["target_price"], tif=self.om.settings.protective_order_time_in_force,
                client_order_id=row["client_order_id"],
            )
        except Exception as exc:
            try:
                norm = self.broker.get_order_by_client_id(row["client_order_id"])
            except Exception as lookup_exc:
                self._set(row, "unknown")
                return self.incident(pos, f"protection submit/lookup ambiguous ({exc}; {lookup_exc})", unknown=True)
            if norm is None:
                self._set(row, "terminal")
                return self.incident(pos, f"protection replacement rejected ({exc})", deferred=False)
        return self._adopt_protection(pos, row, norm)

    def _adopt_protection(self, pos, row, norm):
        if not norm.get("broker_order_id"):
            return self.incident(pos, "protective response has no broker_order_id", unknown=True, deferred=False)
        self._set(row, "working", norm)
        try:
            checked = self.broker.get_order(norm["broker_order_id"])
            legs = checked.get("legs") or []
            allowed = [checked["broker_order_id"]] + [leg.get("broker_order_id") for leg in legs]
            self._fresh(pos, allowed)
            expected_side = "buy" if pos["direction"] == "short" else "sell"
            if checked.get("symbol") != pos["symbol"] or checked.get("side") != expected_side:
                raise ValueError("protective order identity/side mismatch")
            live = verified_live_roles(checked, pos["qty"], pos["symbol"], expected_side)
            if not {"stop_loss", "take_profit"} <= live:
                raise ValueError("both protective legs are not verified live with matching remaining quantity")
            with ledger_ops.atomic(self.journal) as journal:
                ledger_ops.update(journal, "positions", "position_id", pos["position_id"],
                                  {"broker_order_id": checked["broker_order_id"]})
                ledger_ops.update(journal, "paper_orders", "order_id", row["order_id"],
                                  {"raw_request_json": {**ledger_ops.metadata(row), "phase": "protected"}})
        except Exception as exc:
            return self.incident(pos, f"replacement protection cannot be verified/persisted (broker_order_id={norm.get('broker_order_id')}; {exc})", unknown=True, deferred=False)
        return {"ok": False, "deferred": True, "reprotected": True, "position_id": pos["position_id"],
                "symbol": pos["symbol"], "new_broker_order_id": checked["broker_order_id"]}
