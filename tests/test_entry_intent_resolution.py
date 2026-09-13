"""Operator recovery proves absence of dispatch, not merely a broker 404."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest
from requests import ConnectionError, ReadTimeout

from alphaos.__main__ import build_parser, cmd_entry_intent_resolve
from alphaos.constants import ProtectionPath
from alphaos.execution import ledger_ops, protection_watchdog
from alphaos.execution.entry_intent_resolution import resolve_entry_intent
from alphaos.execution.entry_submission import EntrySubmission, pending_entry_intent
from alphaos.journal.journal_store import JournalStore
from alphaos.risk.account_exposure import entry_submission_lock
from alphaos.scheduler.ownership import monitor_ownership
from test_entry_submission_recovery import NotFound, restart, setup


def prepared(manager, proposal):
    return EntrySubmission(manager).create(proposal, ProtectionPath.BROKER_NATIVE_BRACKET)[0]


def test_prepared_crash_recovers_after_restart_without_erasing_history(tmp_path, monkeypatch):
    fake, settings, journal, manager, proposal = setup(tmp_path, monkeypatch)
    row = prepared(manager, proposal)
    journal, manager = restart(fake, settings, journal)
    # Ordinary reconciliation's 404 must preserve the durable no-dispatch proof.
    for _ in range(2):
        assert manager.reconcile()["errors"]
    assert ledger_ops.metadata(journal.order_by_id(row["order_id"]))["phase"] == "prepared"
    original_orders = journal.query("SELECT * FROM paper_orders")
    original_events = journal.query("SELECT * FROM order_events")
    preview = resolve_entry_intent(manager, proposal.proposal_id)
    assert preview["ok"] and preview["status"] == "eligible_for_abandonment"
    assert journal.query("SELECT * FROM paper_orders") == original_orders
    assert journal.query("SELECT * FROM order_events") == original_events
    unrelated = protection_watchdog.open_protection_incident(journal, {"position_id": "elsewhere", "symbol": "OTHER"},
                                                            protection_status="unverifiable", severity="critical", detail="unrelated")
    result = resolve_entry_intent(manager, row["order_id"], apply=True, note="restart before dispatch claim")
    assert result["ok"] and result["applied"] and not pending_entry_intent(journal)
    current = journal.order_by_id(row["order_id"])
    assert current["state"] == "failed" and current["client_order_id"] == proposal.proposal_id
    assert not journal.query("SELECT * FROM paper_fills") and not journal.open_positions()
    assert len(journal.query("SELECT * FROM paper_orders")) == 1
    assert journal.one("SELECT * FROM protection_checks WHERE check_id=?", (unrelated,))["resolved_at_utc"] is None
    assert all(r["resolved_at_utc"] for r in journal.query("SELECT * FROM protection_checks WHERE position_id=?", (f"entry_{row['order_id']}",)))
    assert journal.one("SELECT * FROM system_events WHERE category='entry_intent_resolution'")
    events = journal.query("SELECT * FROM order_events")
    assert resolve_entry_intent(manager, row["order_id"], apply=True, note="again")["status"] == "already_resolved"
    assert journal.query("SELECT * FROM order_events") == events
    assert manager.execute_proposal(proposal).blocked and fake.submits == 0


@pytest.mark.parametrize("failure", [ConnectionError("connection refused"), ReadTimeout("lost response")])
def test_generic_transport_exception_and_404_cannot_be_operator_overridden(tmp_path, monkeypatch, failure):
    fake, _, journal, manager, proposal = setup(tmp_path, monkeypatch)
    def fail(spec):
        raise failure
    fake.before_post = fail
    assert manager.execute_proposal(proposal).submission_uncertain
    for _ in range(3):
        result = resolve_entry_intent(manager, proposal.proposal_id, apply=True, note="operator believes no order")
        assert not result["ok"] and "ambiguous" in result["reason"]
    assert pending_entry_intent(journal) and fake.submits == 1


def test_legacy_submitting_cannot_be_relabelled_unsent(tmp_path, monkeypatch):
    _, _, journal, manager, proposal = setup(tmp_path, monkeypatch)
    row = prepared(manager, proposal)
    with ledger_ops.atomic(journal) as txn:
        ledger_ops.update(txn, "paper_orders", "order_id", row["order_id"], {
            "raw_request_json": {"purpose": "paper_entry", "phase": "submitting"}})
    result = resolve_entry_intent(manager, row["order_id"], apply=True, note="old crash")
    assert not result["ok"] and "no durable proof" in result["reason"]
    assert pending_entry_intent(journal)


@pytest.mark.parametrize("cause", ["claim", "broker_id", "lookup_failure", "position", "open_order", "late_order", "invalid_positions", "invalid_orders", "no_note"])
def test_recovery_refuses_conflicting_or_incomplete_evidence(tmp_path, monkeypatch, cause):
    fake, _, journal, manager, proposal = setup(tmp_path, monkeypatch)
    row = prepared(manager, proposal)
    if cause == "claim":
        EntrySubmission(manager).claim_dispatch(row)
    elif cause == "broker_id":
        with ledger_ops.atomic(journal) as txn:
            ledger_ops.update(txn, "paper_orders", "order_id", row["order_id"], {"broker_order_id": "known"})
    elif cause == "lookup_failure":
        fake.lookup_error = ReadTimeout("unavailable")
    elif cause == "position":
        fake.set_position("XYZ", 1)
    elif cause == "open_order":
        monkeypatch.setattr(manager.alpaca, "list_open_orders", lambda: [{"symbol": "XYZ", "broker_order_id": "other"}])
    elif cause == "late_order":
        calls = iter([None, {"broker_order_id": "appeared"}])
        monkeypatch.setattr(manager.alpaca, "get_order_by_client_id", lambda cid: next(calls))
    elif cause == "invalid_positions":
        monkeypatch.setattr(manager.alpaca, "list_positions", lambda: [{"symbol": "OTHER", "qty": float("nan")}])
    elif cause == "invalid_orders":
        monkeypatch.setattr(manager.alpaca, "list_open_orders", lambda: [{"symbol": "OTHER"}])
    result = resolve_entry_intent(manager, row["order_id"], apply=True, note="" if cause == "no_note" else "recovery")
    assert not result["ok"] and pending_entry_intent(journal)
    assert fake.submits == 0 and not journal.query("SELECT * FROM system_events WHERE category='entry_intent_resolution'")


def test_accepted_but_temporarily_absent_remains_recoverable_normally(tmp_path, monkeypatch):
    fake, _, journal, manager, proposal = setup(tmp_path, monkeypatch)
    fake.response_error, fake.lookup_error = ReadTimeout("accepted, response lost"), NotFound("temporarily absent")
    assert manager.execute_proposal(proposal).submission_uncertain
    assert not resolve_entry_intent(manager, proposal.proposal_id, apply=True, note="looks absent")["ok"]
    fake.lookup_error = None
    assert manager.reconcile()["errors"] == []
    assert not pending_entry_intent(journal) and fake.submits == 1


def test_abandonment_revokes_paused_senders_claim(tmp_path, monkeypatch):
    fake, _, _, manager, proposal = setup(tmp_path, monkeypatch)
    row = prepared(manager, proposal)
    assert resolve_entry_intent(manager, row["order_id"], apply=True, note="sender stopped")["ok"]
    with pytest.raises(ValueError, match="no longer available"):
        EntrySubmission(manager).claim_dispatch(row)
    assert manager.execute_proposal(proposal).blocked and fake.submits == 0


def test_monitor_ownership_prevents_recovery_during_reconciliation(tmp_path, monkeypatch):
    _, _, journal, manager, proposal = setup(tmp_path, monkeypatch)
    row = prepared(manager, proposal)
    acquired, release = Event(), Event()
    def holder():
        with monitor_ownership(journal) as owns:
            assert owns
            acquired.set()
            assert release.wait(5)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(holder)
        assert acquired.wait(5)
        try:
            result = resolve_entry_intent(manager, row["order_id"], apply=True, note="busy")
            assert not result["ok"] and "monitor is active" in result["reason"]
            assert pending_entry_intent(journal)
        finally:
            release.set()
        future.result()


def test_cli_defaults_to_preview_and_propagates_refusal(capsys):
    args = build_parser().parse_args(["entry_intent_resolve", "proposal_1"])
    assert not args.apply and args.note == ""
    calls = []
    def resolve(identifier, **kwargs):
        calls.append((identifier, kwargs))
        return {"ok": False, "reason": "ambiguous"}
    assert cmd_entry_intent_resolve(SimpleNamespace(entry_intent_resolve=resolve), args.identifier, args.apply, args.note) == 1
    assert calls == [("proposal_1", {"apply": False, "note": "", "resolved_by": "cli"})]
    assert "ambiguous" in capsys.readouterr().out


def test_entry_owner_and_mid_snapshot_dispatch_claim_cannot_be_overridden(tmp_path, monkeypatch):
    fake, _, journal, manager, proposal = setup(tmp_path, monkeypatch)
    row = prepared(manager, proposal)
    with entry_submission_lock(journal):
        result = resolve_entry_intent(manager, row["order_id"], apply=True, note="busy sender")
        assert not result["ok"] and "submission is in progress" in result["reason"]
    calls = 0
    def claim_during_snapshot(client_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            EntrySubmission(manager).claim_dispatch(row)
        return None
    monkeypatch.setattr(manager.alpaca, "get_order_by_client_id", claim_during_snapshot)
    result = resolve_entry_intent(manager, row["order_id"], apply=True, note="racing sender")
    assert not result["ok"] and "changed during recovery" in result["reason"]
    assert pending_entry_intent(journal) and fake.submits == 0


def test_audit_failure_rolls_back_abandonment_and_incident_resolution(tmp_path, monkeypatch):
    _, _, journal, manager, proposal = setup(tmp_path, monkeypatch)
    row = prepared(manager, proposal)
    EntrySubmission(manager).incident(row, "process stopped before dispatch")
    before = journal.order_by_id(row["order_id"])
    events = journal.query("SELECT * FROM order_events")
    def failed_audit(*args, **kwargs):
        raise OSError("audit unavailable")
    monkeypatch.setattr(JournalStore, "log_system_event", failed_audit)
    result = resolve_entry_intent(manager, row["order_id"], apply=True, note="recovery")
    assert not result["ok"] and "audit unavailable" in result["reason"]
    assert journal.order_by_id(row["order_id"]) == before
    assert journal.query("SELECT * FROM order_events") == events
    assert protection_watchdog.has_blocking_incident(journal)
