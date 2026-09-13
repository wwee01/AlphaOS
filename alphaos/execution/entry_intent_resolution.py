"""Audited recovery of provably unsent or broker-rejected paper entries.

An empty broker snapshot is corroboration, not proof of non-submission. Legacy
unknown intents and crashes after the dispatch claim must reconcile to an actual
broker order. The operator cannot turn an ambiguous POST into available capacity.
"""
from __future__ import annotations

from alphaos.constants import Severity
from alphaos.execution import ledger_ops
from alphaos.execution.entry_submission import EntrySubmission, PENDING_PHASES, is_entry_intent
from alphaos.execution.entry_rejection import validate_rejection_proof
from alphaos.execution.entry_recovery_exposure import recheck_entry_absence, verify_entry_absence
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
    transport = proof.get("transport") or {}
    if (meta.get("phase") == "unknown" and proof.get("version") == 1
            and proof.get("kind") == "transport_never_sent" and proof.get("observed_at_utc")
            and transport.get("type") == "alpaca_entry_transport_not_sent" and transport.get("version") == 1
            and transport.get("reason") in {"connect_timeout", "new_connection_failure"}):
        return proof
    rejection = meta.get("broker_rejection_proof") or {}
    if (meta.get("phase") == "unknown" and meta.get("dispatch_started_at")
            and rejection.get("version") == 1 and rejection.get("kind") == "broker_confirmed_rejection"
            and rejection.get("observed_at_utc")):
        validate_rejection_proof(row, rejection.get("rejection"))
        return rejection
    raise ValueError("submission is ambiguous; a 404, elapsed time or operator note cannot release it")


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
            if meta.get("phase") in {"abandoned_unsent", "abandoned_rejected"}:
                return {**result, "ok": True, "status": "already_resolved", "resolution": meta["resolution"]}
            if meta.get("phase") not in PENDING_PHASES:
                raise ValueError("entry is not awaiting submission recovery")
            proof = _proof(row)
            rejected = proof["kind"] == "broker_confirmed_rejection"
            # Both identity reads must be explicit 404s. Authentication/network
            # failures propagate; missing data never becomes an empty snapshot.
            if manager.alpaca.get_order_by_client_id(row["client_order_id"]) is not None:
                raise ValueError("broker knows this client ID; use normal reconciliation")
            exposure = verify_entry_absence(journal, manager.alpaca, row)
            if manager.alpaca.get_order_by_client_id(row["client_order_id"]) is not None:
                raise ValueError("broker order appeared during recovery; use normal reconciliation")
            evidence = {"proof": proof, "observed_at_utc": timeutils.stamp().utc,
                        "client_id_404_reads": 2, "exposure_verification": exposure}
            if not apply:
                return {**result, "ok": True, "status": "eligible_for_abandonment", "evidence": evidence,
                        "next_action": "review evidence, then repeat with --apply --note and an audit reason"}
            resolution = {**evidence, "resolved_by": resolved_by.strip(), "note": note.strip()}
            phase, state = ("abandoned_rejected", "rejected") if rejected else ("abandoned_unsent", "failed")
            with ledger_ops.atomic(journal) as txn:
                current = txn.order_by_id(row["order_id"])
                # Even a direct sender/reconciler that bypassed outer ownership
                # cannot race past this compare-and-set or revive its send right.
                if current != row or _proof(current) != proof:
                    raise ValueError("entry changed during recovery; repeat the preview")
                recheck_entry_absence(txn, current, exposure)
                ledger_ops.update(txn, "paper_orders", "order_id", row["order_id"], {
                    "state": state, "raw_request_json": {**meta, "phase": phase, "resolution": resolution},
                })
                EntrySubmission._event(txn, {**current, "state": state}, meta["phase"], phase)
                txn.conn.execute("UPDATE trade_proposals SET status='blocked' WHERE proposal_id=? "
                                 "AND status IN ('pending_approval','approved','submitted')", (row["proposal_id"],))
                txn.conn.execute(
                    "UPDATE protection_checks SET resolved_at_utc=?, resolved_by=?, resolution_note=? "
                    "WHERE position_id=? AND resolved_at_utc IS NULL",
                    (evidence["observed_at_utc"], resolved_by.strip(), note.strip(), f"entry_{row['order_id']}"))
                message = "Broker-rejected paper entry resolved" if rejected else "Provably unsent paper entry abandoned"
                txn.log_system_event(Severity.INFO, "entry_intent_resolution", message,
                                     {"order_id": row["order_id"], **resolution})
            return {**result, "ok": True, "applied": True, "status": phase, "resolution": resolution}
    except Exception as exc:
        return {**result, "status": "refused", "reason": str(exc)}
