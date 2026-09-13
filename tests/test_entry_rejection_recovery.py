"""Actual SDK rejection -> durable restart -> reviewed recovery, with no re-POST."""
from copy import deepcopy
import json

import pytest

from alphaos.broker.alpaca_client import AlpacaClient
from alphaos.execution import ledger_ops, protection_watchdog
from alphaos.execution.entry_intent_resolution import resolve_entry_intent
from alphaos.execution.entry_submission import pending_entry_intent
from alphaos.journal.journal_store import JournalStore
from test_entry_submission_recovery import restart, setup
from test_entry_transport_proof import response


def rejected_entry(tmp_path, monkeypatch, *, held=False):
    from requests.adapters import HTTPAdapter

    fake, settings, journal, manager, proposal = setup(tmp_path, monkeypatch)
    if held:
        from test_entry_held_recovery import ordinary_holding

        ordinary_holding(fake, journal, manager)
    client = AlpacaClient(settings, journal)
    client._trading_client()._session.trust_env = False
    manager.alpaca = client
    attempts = []

    def no_socket(*args, **kwargs):
        raise AssertionError("test must not open a network connection")

    def send(adapter, request, **kwargs):
        assert request.method == "POST" and request.url == "https://paper-api.alpaca.markets/v2/orders"
        attempts.append(json.loads(request.body))
        result = response(403, request, b'{"code":40310000,"message":"potential wash trade detected. use complex orders"}')
        result.headers["Content-Type"] = "application/json"
        return result

    monkeypatch.setattr("urllib3.util.connection.create_connection", no_socket)
    monkeypatch.setattr(HTTPAdapter, "send", send)
    result = manager.execute_proposal(proposal)
    assert result.submission_uncertain and len(attempts) == 1
    assert ledger_ops.metadata(result.order)["broker_rejection_proof"]["rejection"]["http_status"] == 403
    assert pending_entry_intent(journal) and protection_watchdog.has_blocking_incident(journal)
    journal, manager = restart(fake, settings, journal)
    return fake, journal, manager, proposal, attempts


def test_actual_wash_trade_rejection_in_held_symbol_has_audited_exit(tmp_path, monkeypatch):
    fake, journal, manager, proposal, attempts = rejected_entry(tmp_path, monkeypatch, held=True)
    before_positions = journal.open_positions()
    before_fills = journal.query("SELECT * FROM paper_fills")
    preview = resolve_entry_intent(manager, proposal.proposal_id)
    assert preview["ok"]
    attribution = preview["evidence"]["exposure_verification"]["attributed_positions"]
    assert len(attribution) == 1 and attribution[0]["qty"] == 5
    result = resolve_entry_intent(manager, proposal.proposal_id, apply=True, note="wash-trade rejection of separate add-on")
    assert result["applied"] and result["status"] == "abandoned_rejected"
    assert journal.open_positions() == before_positions and journal.query("SELECT * FROM paper_fills") == before_fills
    assert not pending_entry_intent(journal) and not protection_watchdog.has_blocking_incident(journal)
    assert manager.execute_proposal(proposal).blocked and fake.submits == 1 and len(attempts) == 1


def test_real_rejection_survives_restart_and_audited_resolution_preserves_identity(tmp_path, monkeypatch):
    fake, journal, manager, proposal, attempts = rejected_entry(tmp_path, monkeypatch)
    for _ in range(5):
        assert manager.reconcile()["errors"]
    before = journal.query("SELECT * FROM paper_orders")
    events = journal.query("SELECT * FROM order_events")
    approvals = journal.query("SELECT * FROM approvals")
    preview = resolve_entry_intent(manager, proposal.proposal_id)
    assert preview["ok"] and preview["evidence"]["proof"]["kind"] == "broker_confirmed_rejection"
    assert journal.query("SELECT * FROM paper_orders") == before
    assert journal.query("SELECT * FROM order_events") == events
    assert not resolve_entry_intent(manager, proposal.proposal_id, apply=True)["ok"]
    result = resolve_entry_intent(manager, proposal.proposal_id, apply=True, note="verified broker rejection")
    assert result["applied"] and result["status"] == "abandoned_rejected"
    current = journal.order_by_id(before[0]["order_id"])
    assert current["state"] == "rejected" and current["client_order_id"] == proposal.proposal_id
    assert current["broker_order_id"] is None
    assert ledger_ops.metadata(current)["broker_rejection_proof"] == ledger_ops.metadata(before[0])["broker_rejection_proof"]
    assert not pending_entry_intent(journal) and not protection_watchdog.has_blocking_incident(journal)
    assert not journal.query("SELECT * FROM paper_fills") and not journal.open_positions()
    assert journal.query("SELECT * FROM approvals") == approvals
    assert manager.execute_proposal(proposal).blocked and fake.submits == 0 and len(attempts) == 1
    recorded = journal.query("SELECT * FROM order_events")
    assert resolve_entry_intent(manager, proposal.proposal_id, apply=True, note="repeat")["status"] == "already_resolved"
    assert journal.query("SELECT * FROM order_events") == recorded


@pytest.mark.parametrize("field", ["client_order_id", "symbol", "qty", "side", "limit_price", "time_in_force",
                                  "take_profit", "stop_loss", "response_body_sha256", "http_status", "error_code",
                                  "classification", "unrecognized_code", "wrong_status_classification"])
def test_rejection_proof_cannot_release_different_or_changed_intent(tmp_path, monkeypatch, field):
    _, journal, manager, proposal, _ = rejected_entry(tmp_path, monkeypatch)
    row = journal.one("SELECT * FROM paper_orders")
    meta = deepcopy(ledger_ops.metadata(row))
    proof = meta["broker_rejection_proof"]["rejection"]
    if field == "unrecognized_code":
        proof["error_code"] = 40399999
    elif field == "wrong_status_classification":
        proof.update(http_status=422, error_code=42210000)
    elif field in {"response_body_sha256", "http_status", "error_code", "classification"}:
        proof[field] = "invalid"
    else:
        proof["request"][field] = "invalid"
    ledger_ops.update(journal, "paper_orders", "order_id", row["order_id"], {"raw_request_json": meta})
    journal.conn.commit()
    result = resolve_entry_intent(manager, proposal.proposal_id, apply=True, note="must refuse")
    assert not result["ok"] and pending_entry_intent(journal)


def test_rejection_resolution_audit_failure_is_atomic_and_other_incidents_remain(tmp_path, monkeypatch):
    _, journal, manager, proposal, _ = rejected_entry(tmp_path, monkeypatch)
    unrelated = protection_watchdog.open_protection_incident(
        journal, {"position_id": "unrelated", "symbol": "OTHER"}, protection_status="unverifiable",
        severity="critical", detail="unrelated broker uncertainty")
    before = journal.one("SELECT * FROM paper_orders")
    original_log = JournalStore.log_system_event

    def fail(*args, **kwargs):
        raise OSError("audit unavailable")

    monkeypatch.setattr(JournalStore, "log_system_event", fail)
    result = resolve_entry_intent(manager, proposal.proposal_id, apply=True, note="reviewed")
    assert not result["ok"] and "audit unavailable" in result["reason"]
    assert journal.one("SELECT * FROM paper_orders") == before and pending_entry_intent(journal)
    monkeypatch.setattr(JournalStore, "log_system_event", original_log)
    assert resolve_entry_intent(manager, proposal.proposal_id, apply=True, note="reviewed")["applied"]
    assert journal.one("SELECT * FROM protection_checks WHERE check_id=?", (unrelated,))["resolved_at_utc"] is None


def test_rejection_proof_does_not_override_broker_order_appearing_during_resolution(tmp_path, monkeypatch):
    _, journal, manager, proposal, _ = rejected_entry(tmp_path, monkeypatch)
    reads = []

    def lookup(client_id):
        reads.append(client_id)
        return None if len(reads) == 1 else {"broker_order_id": "unexpected"}

    monkeypatch.setattr(manager.alpaca, "get_order_by_client_id", lookup)
    result = resolve_entry_intent(manager, proposal.proposal_id, apply=True, note="must refuse")
    assert not result["ok"] and "appeared" in result["reason"]
    assert pending_entry_intent(journal) and len(reads) == 2
