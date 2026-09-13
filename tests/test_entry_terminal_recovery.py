"""Terminal zero-fill brackets need no children, but require proof of no exposure."""
import pytest
from requests import ReadTimeout
from types import SimpleNamespace

from alphaos.execution import ledger_ops, protection_watchdog
from alphaos.execution.entry_submission import pending_entry_intent
from test_entry_submission_recovery import restart, setup


def terminal_setup(tmp_path, monkeypatch, status="rejected"):
    fake, settings, journal, manager, proposal = setup(tmp_path, monkeypatch)
    fake.response_error, fake.lookup_error = ReadTimeout("response lost"), ReadTimeout("lookup unavailable")
    first = manager.execute_proposal(proposal)
    assert first.submission_uncertain and fake.submits == 1
    order = fake._find(proposal.symbol)
    order.status, order.legs = status, []
    fake.lookup_error = None
    return fake, settings, journal, manager, proposal, first, order


@pytest.mark.parametrize("status", ["rejected", "canceled", "expired"])
@pytest.mark.parametrize("legacy", [False, True])
def test_restart_recovers_matching_terminal_zero_without_legs_once(tmp_path, monkeypatch, status, legacy):
    fake, settings, journal, manager, proposal, first, _ = terminal_setup(tmp_path, monkeypatch, status)
    if legacy:
        with ledger_ops.atomic(journal) as txn:
            ledger_ops.update(txn, "paper_orders", "order_id", first.order["order_id"], {
                "raw_request_json": {"purpose": "paper_entry", "phase": "unknown"}})
    journal, manager = restart(fake, settings, journal)
    result = manager.reconcile()
    assert result["reconciled"] == 1 and not result["errors"]
    row = journal.order_by_id(first.order["order_id"])
    assert ledger_ops.metadata(row)["phase"] == "acknowledged"
    assert row["client_order_id"] == proposal.proposal_id and row["broker_order_id"]
    assert not pending_entry_intent(journal) and not protection_watchdog.has_blocking_incident(journal)
    assert not journal.query("SELECT * FROM paper_fills") and not journal.open_positions()
    assert manager.reconcile()["reconciled"] == 0
    assert manager.execute_proposal(proposal).blocked and fake.submits == 1


@pytest.mark.parametrize("cause", ["filled", "active", "parent_identity", "symbol", "side", "qty", "limit", "tif",
                                   "position", "open_order", "local_position", "position_read", "orders_read",
                                   "invalid_positions", "invalid_orders", "late_position", "foreign_second_parent"])
def test_terminal_recovery_refuses_conflicting_or_incomplete_evidence(tmp_path, monkeypatch, cause):
    fake, _, journal, manager, proposal, first, order = terminal_setup(tmp_path, monkeypatch)
    if cause == "filled":
        order.filled_qty, order.filled_avg_price = 1, 100
    elif cause == "active":
        order.status = "accepted"
    elif cause == "parent_identity":
        order.client_order_id = "other"
    elif cause == "symbol":
        order.symbol = "OTHER"
    elif cause == "side":
        order.side = "sell"
    elif cause == "qty":
        order.qty = 11
    elif cause == "limit":
        order.limit_price = 101
    elif cause == "tif":
        order.time_in_force = "day" if order.time_in_force == "gtc" else "gtc"
    elif cause == "position":
        fake.set_position(proposal.symbol, 1)
    elif cause == "open_order":
        fake.add_stray_order(proposal.symbol)
    elif cause == "local_position":
        manager.positions.open_position(first.order, 100)
    elif cause in {"position_read", "orders_read"}:
        def unavailable():
            raise ReadTimeout("snapshot unavailable")
        monkeypatch.setattr(manager.alpaca, "list_positions" if cause == "position_read" else "list_open_orders", unavailable)
    elif cause == "invalid_positions":
        monkeypatch.setattr(manager.alpaca, "list_positions", lambda: [{"symbol": "OTHER", "qty": float("nan")}])
    elif cause == "invalid_orders":
        monkeypatch.setattr(manager.alpaca, "list_open_orders", lambda: [{"symbol": "OTHER"}])
    elif cause == "late_position":
        reads = iter([[], [{"symbol": proposal.symbol, "qty": 1}]])
        monkeypatch.setattr(manager.alpaca, "list_positions", lambda: next(reads))
    elif cause == "foreign_second_parent":
        fake.mutate_nested = lambda parent: setattr(parent, "id", "other-parent")
    result = manager.reconcile()
    assert result["errors"] and pending_entry_intent(journal)
    assert protection_watchdog.has_blocking_incident(journal)
    assert not journal.query("SELECT * FROM paper_fills")
    assert manager.execute_proposal(proposal).submission_uncertain and fake.submits == 1


def test_terminal_zero_cannot_clear_existing_local_fill_without_position(tmp_path, monkeypatch):
    fake, _, journal, manager, _, first, _ = terminal_setup(tmp_path, monkeypatch)
    journal.insert("paper_fills", {"fill_id": "evidence", "order_id": first.order["order_id"],
                                  "symbol": "XYZ", "side": "buy", "qty": 1, "price": 100})
    assert manager.reconcile()["errors"] and pending_entry_intent(journal)
    assert len(journal.query("SELECT * FROM paper_fills")) == 1 and fake.submits == 1


def test_terminal_zero_cannot_clear_contradictory_child_fill(tmp_path, monkeypatch):
    fake, _, journal, manager, proposal, _, order = terminal_setup(tmp_path, monkeypatch)
    order.legs = [SimpleNamespace(id=role, order_type=kind, symbol=proposal.symbol, side="sell",
                                qty=proposal.qty, filled_qty=filled, filled_avg_price=price if filled else None,
                                status="filled" if filled else "canceled", time_in_force=order.time_in_force,
                                limit_price=price if kind == "limit" else None,
                                stop_price=price if kind == "stop" else None)
                  for role, kind, price, filled in [("target", "limit", proposal.target, 1),
                                                    ("stop", "stop", proposal.stop, 0)]]
    result = manager.reconcile()
    assert result["errors"] and pending_entry_intent(journal)
    assert "contradictory protective fill" in result["errors"][0]["error"]
    assert not journal.query("SELECT * FROM paper_fills") and fake.submits == 1


def test_direct_terminal_submit_response_also_requires_flatness(tmp_path, monkeypatch):
    fake, _, journal, manager, proposal = setup(tmp_path, monkeypatch)
    original = fake.submit
    def reject(spec):
        order = original(spec)
        order.status, order.legs = "rejected", []
        fake.set_position(proposal.symbol, 1)
        return order
    fake.submit = reject
    assert manager.execute_proposal(proposal).submission_uncertain
    assert pending_entry_intent(journal) and fake.submits == 1
