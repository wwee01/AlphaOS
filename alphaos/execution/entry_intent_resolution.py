"""Audited abandonment of provably unsent paper entries; never a retry/override.

An empty broker snapshot is corroboration, not proof of non-submission. Legacy
unknown intents and crashes after the dispatch claim must reconcile to an actual
broker order. The operator cannot turn an ambiguous POST into available capacity.
"""
from __future__ import annotations

import math

from alphaos.constants import Severity
from alphaos.execution import ledger_ops
from alphaos.execution.entry_submission import EntrySubmission, PENDING_PHASES, is_entry_intent
from alphaos.risk.account_exposure import entry_submission_lock
from alphaos.scheduler.ownership import monitor_ownership
from alphaos.util import timeutils


def _proof(row):
    meta = ledger_ops.metadata(row)
    if row.get("broker_order_id") or meta.get("dispatch_version") != 1:
        raise ValueError("no durable proof that this entry was never sent; reconcile its broker identity")
    if meta.get("phase") == "prepared" and meta.get("dispatch_started_at") is None:
        return {"kind": "dispatch_not_claimed", "version": 1}
    proof = meta.get("no_dispatch_proof") or {}
    if (meta.get("phase") == "unknown" and proof.get("version") == 1
            and proof.get("kind") == "transport_never_sent" and proof.get("observed_at_utc")):
        return proof
    raise ValueError("submission is ambiguous; a 404, elapsed time or operator note cannot release it")


def _no_local_exposure(journal, row):
    if journal.one("SELECT 1 FROM positions WHERE order_id=? OR (symbol=? AND status='open') LIMIT 1",
                   (row["order_id"], row["symbol"])):
        raise ValueError("local position evidence contradicts an unsent entry")
    if journal.one("SELECT 1 FROM paper_fills WHERE order_id=? LIMIT 1", (row["order_id"],)):
        raise ValueError("local fill evidence contradicts an unsent entry")


def _flat_positions(rows, symbol):
    if not isinstance(rows, list):
        raise ValueError("broker position snapshot is incomplete")
    for position in rows:
        qty = float(position["qty"])
        if not position.get("symbol") or not math.isfinite(qty):
            raise ValueError("broker position snapshot is invalid")
        if position["symbol"] == symbol and abs(qty) > 0:
            raise ValueError("broker still has exposure for this symbol")


def _no_orders(rows, row):
    if not isinstance(rows, list):
        raise ValueError("broker open-order snapshot is incomplete")
    for order in rows:
        if not order.get("broker_order_id") or not order.get("symbol"):
            raise ValueError("broker open-order identity is incomplete")
        if order["symbol"] == row["symbol"] or order.get("client_order_id") == row["client_order_id"]:
            raise ValueError("broker still has a potentially related open order")
        _no_orders(order.get("legs") or [], row)


def resolve_entry_intent(manager, identifier, *, apply=False, note="", resolved_by="cli"):
    """Preview by default; apply retains the failed identity and every audit row."""
    result = {"ok": False, "applied": False, "identifier": identifier}
    try:
        if not manager.real_paper or manager.alpaca is None:
            raise ValueError("entry intent recovery requires the configured paper broker")
        manager.alpaca.preflight()
        if apply and (not note.strip() or not resolved_by.strip()):
            raise ValueError("an operator identity and nonempty audit note are required to apply")
        with entry_submission_lock(manager.journal), monitor_ownership(manager.journal) as acquired:
            if not acquired:
                raise ValueError("execution monitor is active; retry recovery after it completes")
            journal = manager.journal
            rows = journal.query(
                "SELECT * FROM paper_orders WHERE execution_source='alpaca_paper' AND order_type='bracket' "
                "AND (order_id=? OR client_order_id=? OR proposal_id=?)", (identifier, identifier, identifier))
            if len(rows) != 1 or not is_entry_intent(rows[0]):
                raise ValueError("identifier must match exactly one durable paper entry")
            row = rows[0]
            result.update(order_id=row["order_id"], client_order_id=row["client_order_id"], symbol=row["symbol"])
            meta = ledger_ops.metadata(row)
            if meta.get("phase") == "abandoned_unsent":
                return {**result, "ok": True, "status": "already_resolved", "resolution": meta["resolution"]}
            if meta.get("phase") not in PENDING_PHASES:
                raise ValueError("entry is not awaiting submission recovery")
            proof = _proof(row)
            _no_local_exposure(journal, row)
            # Both identity reads must be explicit 404s. Authentication/network
            # failures propagate; missing data never becomes an empty snapshot.
            if manager.alpaca.get_order_by_client_id(row["client_order_id"]) is not None:
                raise ValueError("broker knows this client ID; use normal reconciliation")
            _flat_positions(manager.alpaca.list_positions(), row["symbol"])
            _no_orders(manager.alpaca.list_open_orders(), row)
            _flat_positions(manager.alpaca.list_positions(), row["symbol"])
            if manager.alpaca.get_order_by_client_id(row["client_order_id"]) is not None:
                raise ValueError("broker order appeared during recovery; use normal reconciliation")
            evidence = {"proof": proof, "observed_at_utc": timeutils.stamp().utc,
                        "client_id_404_reads": 2, "symbol_flat_reads": 2, "related_open_orders": 0}
            if not apply:
                return {**result, "ok": True, "status": "eligible_for_abandonment", "evidence": evidence,
                        "next_action": "review evidence, then repeat with --apply --note and an audit reason"}
            resolution = {**evidence, "resolved_by": resolved_by.strip(), "note": note.strip()}
            with ledger_ops.atomic(journal) as txn:
                current = txn.order_by_id(row["order_id"])
                # Even a direct sender/reconciler that bypassed outer ownership
                # cannot race past this compare-and-set or revive its send right.
                if current != row or _proof(current) != proof:
                    raise ValueError("entry changed during recovery; repeat the preview")
                _no_local_exposure(txn, current)
                ledger_ops.update(txn, "paper_orders", "order_id", row["order_id"], {
                    "state": "failed", "raw_request_json": {**meta, "phase": "abandoned_unsent", "resolution": resolution},
                })
                EntrySubmission._event(txn, {**current, "state": "failed"}, meta["phase"], "abandoned_unsent")
                txn.conn.execute("UPDATE trade_proposals SET status='blocked' WHERE proposal_id=? "
                                 "AND status IN ('pending_approval','approved','submitted')", (row["proposal_id"],))
                txn.conn.execute(
                    "UPDATE protection_checks SET resolved_at_utc=?, resolved_by=?, resolution_note=? "
                    "WHERE position_id=? AND resolved_at_utc IS NULL",
                    (evidence["observed_at_utc"], resolved_by.strip(), note.strip(), f"entry_{row['order_id']}"))
                txn.log_system_event(Severity.INFO, "entry_intent_resolution", "Provably unsent paper entry abandoned",
                                     {"order_id": row["order_id"], **resolution})
            return {**result, "ok": True, "applied": True, "status": "abandoned_unsent", "resolution": resolution}
    except Exception as exc:
        return {**result, "status": "refused", "reason": str(exc)}
