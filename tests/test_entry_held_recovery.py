"""Rejected add-ons reconcile without changing a separate ordinary holding."""
from copy import deepcopy

import pytest
from requests import ReadTimeout

from alphaos.execution import ledger_ops, protection_watchdog
from alphaos.execution.entry_intent_resolution import resolve_entry_intent
from alphaos.execution.entry_submission import pending_entry_intent
from conftest import make_proposal
from test_alpaca_paper_execution import _seed_proposal
from test_entry_submission_recovery import restart, setup


def ordinary_holding(fake, journal, manager):
    old = make_proposal(symbol="XYZ", entry=100, stop=97, target=106, qty=5)
    _seed_proposal(journal, old)
    assert manager.execute_proposal(old).state == "accepted"
    fake.fill_entry("XYZ", 100)
    fake.set_position("XYZ", 5)
    parent = fake._find("XYZ")
    parent.legs[1].status = "held"
    # Real Alpaca can list the target as a top-level order and hide a held stop.
    fake._stray_orders[parent.legs[0].id] = parent.legs[0]
    assert not manager.reconcile()["errors"]
    position = journal.open_positions()[0]
    assert position["qty"] == 5
    return position, parent


def pending_addon(tmp_path, monkeypatch):
    fake, settings, journal, manager, proposal = setup(tmp_path, monkeypatch)
    position, parent = ordinary_holding(fake, journal, manager)
    before = {"position": journal.open_positions(), "fills": journal.query("SELECT * FROM paper_fills"),
              "old_order": journal.order_by_id(position["order_id"]), "parent": deepcopy(parent.__dict__)}
    fake.response_error, fake.lookup_error = ReadTimeout("lost response"), ReadTimeout("lookup unavailable")
    result = manager.execute_proposal(proposal)
    assert result.submission_uncertain and fake.submits == 2
    failed = next(order for order in fake.orders.values() if order.client_order_id == proposal.proposal_id)
    failed.status, failed.filled_qty, failed.legs = "rejected", 0, []
    fake.lookup_error = None
    return fake, settings, journal, manager, proposal, before, failed


def test_terminal_zero_addon_recovers_through_restart_preserving_ordinary_holding(tmp_path, monkeypatch):
    fake, settings, journal, manager, proposal, before, _ = pending_addon(tmp_path, monkeypatch)
    journal, manager = restart(fake, settings, journal)
    result = manager.reconcile()
    assert result["errors"] == [] and not pending_entry_intent(journal)
    row = journal.one("SELECT * FROM paper_orders WHERE proposal_id=?", (proposal.proposal_id,))
    proof = ledger_ops.metadata(row)["recovery_evidence"]
    assert row["state"] == "rejected" and proof["attributed_positions"][0]["qty"] == 5
    assert journal.open_positions() == before["position"]
    assert journal.query("SELECT * FROM paper_fills") == before["fills"]
    assert not protection_watchdog.has_blocking_incident(journal)
    assert manager.execute_proposal(proposal).blocked and fake.submits == 2
    assert not manager.reconcile()["errors"] and fake.submits == 2


@pytest.mark.parametrize("cause", ["unowned_qty", "unknown_order", "missing_origin", "target_fill"])
def test_terminal_addon_cannot_hide_unexplained_or_related_exposure(tmp_path, monkeypatch, cause):
    fake, _, journal, manager, proposal, before, failed = pending_addon(tmp_path, monkeypatch)
    if cause == "unowned_qty":
        fake.set_position("XYZ", 6)
    elif cause == "unknown_order":
        fake.add_stray_order("XYZ")
    elif cause == "missing_origin":
        journal.conn.execute("DELETE FROM paper_orders WHERE order_id=?", (before["position"][0]["order_id"],))
        journal.conn.commit()
    else:
        failed.filled_qty, failed.filled_avg_price = 1, 100
    result = manager.reconcile()
    assert result["errors"] and pending_entry_intent(journal)
    assert not resolve_entry_intent(manager, proposal.proposal_id, apply=True, note="must refuse")["ok"]
    assert fake.submits == 2


def test_normal_reconciliation_restores_verified_fill_before_recovery(tmp_path, monkeypatch):
    fake, _, journal, manager, _, before, _ = pending_addon(tmp_path, monkeypatch)
    journal.conn.execute("DELETE FROM paper_fills")
    journal.conn.commit()
    # Reconciliation processes the original filled entry first. The recovery
    # helper must use its newly broker-confirmed ledger, not invent an exemption
    # for missing fills. Direct missing-fill refusal is covered by helper tests.
    assert manager.reconcile()["errors"] == [] and not pending_entry_intent(journal)
    fills = journal.query("SELECT * FROM paper_fills")
    assert len(fills) == 1 and fills[0]["qty"] == 5
    assert fills[0]["position_id"] == before["position"][0]["position_id"]
    assert fills[0]["broker_order_id"] == before["position"][0]["broker_order_id"] and fake.submits == 2


def test_terminal_parent_changing_after_attribution_keeps_gate_closed(tmp_path, monkeypatch):
    fake, _, journal, manager, proposal, _, failed = pending_addon(tmp_path, monkeypatch)
    calls = 0
    original = manager.alpaca.list_positions

    def change_after_second_read():
        nonlocal calls
        calls += 1
        result = original()
        if calls == 2:
            failed.status, failed.filled_qty, failed.filled_avg_price = "partially_filled", 1, 100
        return result

    monkeypatch.setattr(manager.alpaca, "list_positions", change_after_second_read)
    result = manager.reconcile()
    assert result["errors"] and pending_entry_intent(journal) and fake.submits == 2
