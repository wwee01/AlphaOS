"""Durable paper entry ownership; an uncertain POST is never retried.

The proposal ID is already the broker client ID. Persist that identity before
the first POST so a process death, timeout, or lost response cannot orphan it.
No database transaction is held across a broker request.
"""
from __future__ import annotations

import math
from hashlib import sha256

from alphaos.constants import OrderState, ProtectionStatus, Severity
from alphaos.execution import ledger_ops, order_schema, protection_watchdog
from alphaos.execution.protection_contract import verified_live_roles
from alphaos.util import alerts, timeutils
from alphaos.util.ids import new_id

PENDING_PHASES = {"prepared", "submitting", "unknown", "reconciling"}
TERMINAL_ZERO = {"rejected", "canceled", "cancelled", "expired"}


def is_entry_intent(row):
    return ledger_ops.metadata(row).get("purpose") == "paper_entry"


def pending_entry_intent(journal):
    for row in journal.query("SELECT * FROM paper_orders WHERE execution_source='alpaca_paper' "
                             "AND order_type='bracket' ORDER BY id"):
        if is_entry_intent(row) and ledger_ops.metadata(row).get("phase") in PENDING_PHASES:
            return row
    return None


def existing_entry_intent(journal, proposal_id):
    return journal.one("SELECT * FROM paper_orders WHERE proposal_id=? "
                       "AND execution_source='alpaca_paper' AND order_type='bracket' "
                       "ORDER BY id LIMIT 1", (proposal_id,))


class EntrySubmission:
    def __init__(self, manager):
        self.manager, self.journal, self.broker = manager, manager.journal, manager.alpaca

    def existing(self, proposal_id):
        return existing_entry_intent(self.journal, proposal_id)

    def create(self, proposal, protection):
        order_id = "ord_" + sha256(f"entry:{proposal.proposal_id}".encode()).hexdigest()[:28]
        provider, feed = self.manager._data_labels()
        row = order_schema.build_order_row(
            order_id=order_id, proposal=proposal, side=order_schema.side_for_entry(proposal.direction),
            order_type="bracket", execution_source="alpaca_paper", execution_provider="alpaca_paper",
            execution_mode="alpaca_paper", data_provider=provider, data_feed=feed,
            fill_price_basis="alpaca_fill", protection_path=protection.value,
            state=OrderState.SUBMITTED.value, qty=proposal.qty, entry_price=proposal.entry,
            take_profit_price=proposal.target, stop_loss_price=proposal.stop, limit_price=proposal.entry,
            time_in_force=self.broker._resolve_tif(proposal), client_order_id=proposal.proposal_id,
            raw_request={"purpose": "paper_entry", "proposal_id": proposal.proposal_id,
                         "phase": "prepared", "dispatch_version": 1, "dispatch_started_at": None},
            submitted_at=timeutils.stamp().utc,
        )
        with ledger_ops.atomic(self.journal) as journal:
            existing = self.existing(proposal.proposal_id)
            if existing:
                return existing, False
            if pending_entry_intent(journal):
                raise ValueError("another paper entry submission remains unverified")
            journal.insert("paper_orders", row, mirror=True)
            self._event(journal, row, "approved", "prepared")
        return self.journal.order_by_id(order_id), True

    def claim_dispatch(self, row):
        """Consume the send right atomically; an abandoned intent can never POST.

        A crash after this claim is ambiguous even if the HTTP call has not yet
        started. Only the prepared state proves that no sender crossed this gate.
        """
        with ledger_ops.atomic(self.journal) as journal:
            current = journal.order_by_id(row["order_id"])
            meta = ledger_ops.metadata(current)
            if (meta.get("phase") != "prepared" or meta.get("dispatch_version") != 1
                    or meta.get("dispatch_started_at") is not None or current.get("broker_order_id")):
                raise ValueError("entry dispatch right is no longer available")
            ledger_ops.update(journal, "paper_orders", "order_id", row["order_id"], {
                "raw_request_json": {**meta, "phase": "submitting", "dispatch_started_at": timeutils.stamp().utc},
            })
            self._event(journal, current, "prepared", "submitting")
        return self.journal.order_by_id(row["order_id"])

    def not_sent(self, row, detail, transport_proof):
        """Retain a typed transport proof for an explicit operator resolution."""
        with ledger_ops.atomic(self.journal) as journal:
            current = journal.order_by_id(row["order_id"])
            meta = ledger_ops.metadata(current)
            if (meta.get("phase") != "submitting" or meta.get("dispatch_version") != 1
                    or not meta.get("dispatch_started_at") or current.get("broker_order_id")):
                raise ValueError("cannot attach no-send proof to a changed entry")
            ledger_ops.update(journal, "paper_orders", "order_id", row["order_id"], {
                "raw_request_json": {**meta, "phase": "unknown", "last_verification_error": detail,
                                     "no_dispatch_proof": {"version": 1, "kind": "transport_never_sent",
                                                           "transport": transport_proof,
                                                           "observed_at_utc": timeutils.stamp().utc}},
            })
            self._event(journal, current, "submitting", "unknown")
        self.incident(row, detail)
        return self.journal.order_by_id(row["order_id"])

    @staticmethod
    def _event(journal, row, previous, phase):
        journal.insert("order_events", {
            "event_id": new_id("oev"), "order_id": row["order_id"],
            "broker_order_id": row.get("broker_order_id"), "prev_state": row.get("state"),
            "new_state": row.get("state"), "execution_source": "alpaca_paper",
            "message": f"durable paper entry phase: {previous} -> {phase}",
        })

    def rejected(self, row, proof):
        """Keep a verified POST rejection gated until audited operator recovery."""
        from alphaos.execution.entry_rejection import validate_rejection_proof

        with ledger_ops.atomic(self.journal) as journal:
            current = journal.order_by_id(row["order_id"])
            meta = ledger_ops.metadata(current)
            if (meta.get("phase") != "submitting" or meta.get("dispatch_version") != 1
                    or not meta.get("dispatch_started_at") or current.get("broker_order_id")):
                raise ValueError("cannot attach rejection proof to a changed entry")
            validate_rejection_proof(current, proof)
            ledger_ops.update(journal, "paper_orders", "order_id", row["order_id"], {
                "raw_request_json": {**meta, "phase": "unknown",
                                     "last_verification_error": "broker rejected entry; audited recovery required",
                                     "broker_rejection_proof": {"version": 1, "kind": "broker_confirmed_rejection",
                                                                "rejection": proof,
                                                                "observed_at_utc": timeutils.stamp().utc}},
            })
            self._event(journal, current, "submitting", "unknown")
        self.incident(row, "broker rejected entry; review entry_intent_resolve before releasing its gate")
        return self.journal.order_by_id(row["order_id"])

    @staticmethod
    def validate(row, norm, *, nested=False):
        side = "sell" if row["direction"] == "short" else "buy"
        if (not norm.get("broker_order_id") or norm.get("client_order_id") != row["client_order_id"]
                or norm.get("symbol") != row["symbol"] or norm.get("side") != side
                or norm.get("order_class") != "bracket"
                or (row.get("broker_order_id") and norm["broker_order_id"] != row["broker_order_id"])):
            raise ValueError("entry response does not match durable broker/client identity, symbol, or side")
        qty, filled = float(norm["qty"]), float(norm["filled_qty"])
        if (not math.isfinite(qty) or not math.isfinite(filled) or qty <= 0
                or abs(qty - float(row["qty"])) > 1e-6 or not 0 <= filled <= qty):
            raise ValueError("entry response has invalid or unauthorized cumulative quantities")
        def same_price(actual, intended):
            value = float(actual)
            return math.isfinite(value) and value > 0 and abs(value - round(float(intended), 2)) <= 1e-6
        if (not same_price(norm.get("limit_price"), row["limit_price"])
                or norm.get("time_in_force") != row["time_in_force"]):
            raise ValueError("entry response changed the authorized limit price or time in force")
        if nested:
            legs = norm.get("legs") or []
            # A broker rejection/cancellation before any fill may never create
            # child orders. Parent identity and intended terms above remain
            # mandatory; finish separately proves flatness before releasing it.
            if not legs and norm.get("status") in TERMINAL_ZERO and filled == 0:
                return
            ids = {leg.get("broker_order_id") for leg in legs}
            if (len(legs) != 2 or len(ids) != 2 or None in ids or "" in ids
                    or norm["broker_order_id"] in ids
                    or {leg.get("role") for leg in legs} != {"stop_loss", "take_profit"}):
                raise ValueError("entry response lacks unique complete protective lineage")
            for leg in legs:
                leg_qty, leg_filled = float(leg["qty"]), float(leg["filled_qty"])
                price_key, intended = (("stop_price", row["stop_loss_price"]) if leg["role"] == "stop_loss"
                                       else ("limit_price", row["take_profit_price"]))
                if (leg.get("symbol") != row["symbol"] or leg.get("side") != ("buy" if side == "sell" else "sell")
                        or not math.isfinite(leg_qty) or not math.isfinite(leg_filled)
                        or not 0 < leg_qty <= qty or not 0 <= leg_filled <= leg_qty
                        or not same_price(leg.get(price_key), intended)
                        or leg.get("time_in_force") != row["time_in_force"]):
                    raise ValueError("entry response changed authorized protective instructions")

    def lookup(self, row):
        norm = self.broker.get_order_by_client_id(row["client_order_id"])
        if norm is None:
            raise LookupError("broker explicitly reports client ID not found; submission remains unresolved, no retry")
        self.validate(row, norm)
        # Client-ID lookup need not include nested legs. Re-read the identified
        # parent with nested=True, and verify that it is the same order.
        parent_id = norm["broker_order_id"]
        norm = self.broker.get_order(parent_id)
        self.validate({**row, "broker_order_id": parent_id}, norm, nested=True)
        return norm

    def observe(self, row, norm):
        self.validate(row, norm, nested=True)
        values = {"broker_order_id": norm["broker_order_id"], "state": norm.get("state") or "submitted",
                  "raw_response_json": norm, "time_in_force": norm.get("time_in_force") or row["time_in_force"],
                  "submitted_at": norm.get("submitted_at") or row["submitted_at"],
                  "accepted_at": norm.get("accepted_at") or row.get("accepted_at"),
                  "filled_at": norm.get("filled_at") or row.get("filled_at")}
        with ledger_ops.atomic(self.journal) as journal:
            current = journal.order_by_id(row["order_id"])
            self.validate(current, norm, nested=True)
            ledger_ops.update(journal, "paper_orders", "order_id", row["order_id"], values)
        row.update(values)
        return row

    def uncertain(self, row, detail, *, force=False):
        with ledger_ops.atomic(self.journal) as journal:
            current = journal.order_by_id(row["order_id"])
            meta = ledger_ops.metadata(current)
            # A different reconciler may already have recovered this POST.
            if meta.get("phase") == "acknowledged" and not force:
                return current
            phase = "prepared" if meta.get("phase") == "prepared" else "unknown"
            if meta.get("phase") != phase:
                self._event(journal, current, meta.get("phase"), phase)
            ledger_ops.update(journal, "paper_orders", "order_id", row["order_id"], {
                "raw_request_json": {**meta, "phase": phase, "last_verification_error": detail},
            })
        self.incident(row, detail)
        return self.journal.order_by_id(row["order_id"])

    def incident(self, row, detail):
        detail = f"{row['symbol']}: paper entry {row['client_order_id']} is unverified: {detail}"
        incident_id = protection_watchdog.open_protection_incident(
            self.journal, {"position_id": f"entry_{row['order_id']}", "symbol": row["symbol"]},
            protection_status=ProtectionStatus.UNVERIFIABLE.value, severity=Severity.CRITICAL.value, detail=detail,
        )
        if incident_id:
            self.journal.log_system_event(Severity.CRITICAL, "entry_submission", detail,
                                          {"order_id": row["order_id"], "incident_id": incident_id})
            alerts.send_alert(self.manager.settings, "AlphaOS: entry submission needs review", detail,
                              priority="high", journal=self.journal)
        return {"critical": True, "order_id": row["order_id"], "error": detail}

    def _verify_recovery(self, row, norm):
        if norm.get("status") in TERMINAL_ZERO and norm.get("filled_qty") == 0:
            from alphaos.execution.entry_recovery_exposure import verify_entry_absence

            self.validate(row, norm, nested=True)
            if any(leg.get("filled_qty") != 0 for leg in norm.get("legs", [])):
                raise ValueError("terminal zero-fill entry has contradictory protective fill evidence")
            evidence = verify_entry_absence(self.journal, self.broker, row, terminal_order=norm)
            # Attribution does not establish non-acceptance. Re-read the exact
            # failed entry after the exposure checks, independently of holdings.
            fresh = self.lookup(row)
            if (fresh.get("status") not in TERMINAL_ZERO or fresh.get("filled_qty") != 0
                    or any(leg.get("filled_qty") != 0 for leg in fresh.get("legs", []))
                    or {leg["broker_order_id"] for leg in fresh.get("legs", [])}
                    != {leg["broker_order_id"] for leg in norm.get("legs", [])}):
                raise ValueError("terminal zero-fill entry changed during recovery")
            return evidence
        pos = self.journal.one("SELECT * FROM positions WHERE order_id=?", (row["order_id"],))
        if pos and pos["status"] == "open":
            broker_positions = {p["symbol"]: p for p in self.broker.list_positions()}
            check = protection_watchdog.check_position(self.journal, self.broker, pos, broker_positions,
                                                       self.manager.settings)
            if check.protection_status != ProtectionStatus.PROTECTED.value or check.tif_appropriate is False:
                raise ValueError(f"recovered entry protection is not fully verified: {check.detail}")
        elif (norm.get("filled_qty") or 0) == 0 and norm.get("status") not in TERMINAL_ZERO:
            # An accepted unfilled bracket has conditional legs. Validate their
            # identity, quantities and prices without claiming an open holding.
            if norm.get("status") not in {"new", "accepted", "pending_new", "accepted_for_bidding"}:
                raise ValueError("recovered entry is not a confirmed working bracket")
            proof = {**norm, "status": "filled", "filled_qty": norm["qty"],
                     "legs": [{**leg, "status": "new" if leg.get("status") == "held" else leg.get("status")}
                              for leg in norm.get("legs", [])]}
            exit_side = "buy" if row["direction"] == "short" else "sell"
            if verified_live_roles(proof, row["qty"], row["symbol"], exit_side) != {"stop_loss", "take_profit"}:
                raise ValueError("recovered unfilled entry lacks verified conditional stop/target instructions")
        else:
            if any(p["symbol"] == row["symbol"] and abs(float(p["qty"])) > 1e-6
                   for p in self.broker.list_positions()):
                raise ValueError("terminal entry recovery still has unexplained broker exposure")

    def finish(self, row, norm, *, verify_recovery=False):
        terminal_zero = norm.get("status") in TERMINAL_ZERO and norm.get("filled_qty") == 0
        expected = self.journal.order_by_id(row["order_id"])
        evidence = None
        if verify_recovery or terminal_zero:
            evidence = self._verify_recovery(row, norm)
        with ledger_ops.atomic(self.journal) as journal:
            current = journal.order_by_id(row["order_id"])
            if evidence is not None:
                from alphaos.execution.entry_recovery_exposure import recheck_entry_absence

                if current != expected:
                    raise ValueError("entry changed during terminal recovery")
                recheck_entry_absence(journal, row, evidence)
            meta = ledger_ops.metadata(current)
            if meta.get("phase") != "acknowledged":
                self._event(journal, current, meta.get("phase"), "acknowledged")
            ledger_ops.update(journal, "paper_orders", "order_id", row["order_id"], {
                "raw_request_json": {**meta, "phase": "acknowledged",
                                     **({"recovery_evidence": evidence} if evidence is not None else {})},
            })
            journal.conn.execute(
                "UPDATE protection_checks SET resolved_at_utc=?, resolved_by='entry_reconciled', "
                "resolution_note='matching broker entry and protection verified' "
                "WHERE position_id=? AND resolved_at_utc IS NULL",
                (timeutils.stamp().utc, f"entry_{row['order_id']}"),
            )
        return self.journal.order_by_id(row["order_id"])
