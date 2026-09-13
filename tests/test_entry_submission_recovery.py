"""Lost entry responses remain real paper attempts and recover without a POST."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest
from requests import ReadTimeout

from alphaos.broker.alpaca_client import AlpacaClient
from alphaos.execution import ledger_ops, protection_watchdog
from alphaos.execution.entry_submission import pending_entry_intent
from alphaos.execution.order_manager import OrderManager
from alphaos.journal.journal_store import JournalStore
from conftest import make_proposal
from test_alpaca_paper_execution import FakeTradingClient, _paper_om, _seed_proposal


class NotFound(Exception):
    status_code = 404


class RecoverableBroker(FakeTradingClient):
    def __init__(self):
        super().__init__()
        self.submits = 0
        self.lookup_error = None
        self.response_error = None
        self.before_post = None
        self.mutate_lookup = None
        self.mutate_nested = None

    def submit(self, spec):
        self.submits += 1
        if self.before_post:
            self.before_post(spec)
        order = super().submit(spec)
        if self.response_error:
            raise self.response_error
        return order

    def get_order_by_client_id(self, client_id):
        if self.lookup_error:
            raise self.lookup_error
        order = next((o for o in self.orders.values() if o.client_order_id == client_id), None)
        if order is None:
            raise NotFound("client ID does not exist")
        order = deepcopy(order)
        if self.mutate_lookup:
            self.mutate_lookup(order)
        return order

    def get_order_by_id(self, oid):
        order = deepcopy(super().get_order_by_id(oid))
        if self.mutate_nested:
            self.mutate_nested(order)
        return order


def setup(tmp_path, monkeypatch):
    monkeypatch.setattr("alphaos.util.alerts.send_alert", lambda *a, **k: None)
    fake = RecoverableBroker()
    settings, memory, _ = _paper_om(fake, ENTRY_ORDER_STALENESS_ENABLED="false",
                                  TIME_EXIT_ENFORCEMENT_ENABLED="false")
    memory.close()
    journal = JournalStore(str(tmp_path / "entry.db"))
    manager = OrderManager(settings, journal, alpaca=AlpacaClient(settings, journal, trading_client=fake))
    proposal = make_proposal(symbol="XYZ", entry=100, stop=97, target=106, qty=10)
    _seed_proposal(journal, proposal)
    return fake, settings, journal, manager, proposal


def restart(fake, settings, journal):
    path = journal.db_path
    journal.close()
    journal = JournalStore(path)
    manager = OrderManager(settings, journal, alpaca=AlpacaClient(settings, journal, trading_client=fake))
    return journal, manager


def paper_row(journal):
    return journal.one("SELECT * FROM paper_orders WHERE execution_source='alpaca_paper'")


def test_before_post_identity_is_durable_and_duplicate_call_never_posts(tmp_path, monkeypatch):
    fake, _, journal, manager, proposal = setup(tmp_path, monkeypatch)
    def inspect_before_post(spec):
        separate = JournalStore(journal.db_path)
        row = paper_row(separate)
        assert row["client_order_id"] == proposal.proposal_id == spec["client_order_id"]
        assert row["broker_order_id"] is None and ledger_ops.metadata(row)["phase"] == "submitting"
        assert row["qty"] == 10 and row["symbol"] == "XYZ"
        separate.close()
    fake.before_post = inspect_before_post
    fake.response_error, fake.lookup_error = ReadTimeout("accepted; response lost"), ReadTimeout("lookup unavailable")
    first = manager.execute_proposal(proposal)
    assert first.submission_uncertain and not first.blocked and first.state == "submitted"
    assert manager.execute_proposal(proposal).order["order_id"] == first.order["order_id"]
    assert fake.submits == 1 and len(journal.query("SELECT * FROM paper_orders")) == 1
    assert pending_entry_intent(journal) and protection_watchdog.has_blocking_incident(journal)
    other = make_proposal(symbol="OTHER", entry=100, stop=97, target=106, qty=10)
    assert manager.execute_proposal(other).blocked
    assert fake.submits == 1


def test_accept_then_timeout_recovers_immediately_and_clears_intent_incident(tmp_path, monkeypatch):
    fake, _, journal, manager, proposal = setup(tmp_path, monkeypatch)
    fake.response_error = ReadTimeout("accepted; response lost")
    result = manager.execute_proposal(proposal)
    assert not result.blocked and not result.submission_uncertain and result.state == "accepted"
    assert result.order["client_order_id"] == proposal.proposal_id
    assert result.order["broker_order_id"] == fake._find("XYZ").id
    assert pending_entry_intent(journal) is None and protection_watchdog.has_blocking_incident(journal) is None
    history = journal.query("SELECT * FROM protection_checks")
    assert history and all(row["resolved_at_utc"] for row in history)
    manager.execute_proposal(proposal)
    assert fake.submits == 1 and journal.count_open_positions() == 0


def test_restart_recovers_partial_then_full_entry_exactly_once(tmp_path, monkeypatch):
    fake, settings, journal, manager, proposal = setup(tmp_path, monkeypatch)
    fake.response_error, fake.lookup_error = ReadTimeout("accepted; response lost"), ReadTimeout("offline")
    result = manager.execute_proposal(proposal)
    row_id = result.order["order_id"]
    journal, manager = restart(fake, settings, journal)
    order = fake._find("XYZ")
    fake.lookup_error = None
    order.status, order.filled_qty, order.filled_avg_price = "partially_filled", 4, 100
    order.filled_at = "2026-06-22T14:00:00Z"
    fake.set_position("XYZ", 4)
    first = manager.reconcile()
    assert len(first["opened"]) == 1 and first["errors"]
    assert journal.open_positions()[0]["qty"] == 4 and pending_entry_intent(journal)
    assert journal.one("SELECT * FROM paper_fills")["order_id"] == row_id
    order.status, order.filled_qty, order.filled_avg_price = "filled", 10, 101.2
    order.filled_at = "2026-06-22T14:00:07Z"
    fake.set_position("XYZ", 10, avg_entry_price=101.2)
    recovered = manager.reconcile()
    assert recovered["errors"] == []
    assert pending_entry_intent(journal) is None and protection_watchdog.has_blocking_incident(journal) is None
    manager.reconcile()
    fills = journal.query("SELECT * FROM paper_fills ORDER BY id")
    assert [(f["qty"], f["price"]) for f in fills] == [(4, 100), (6, 102)]
    assert fills[-1]["filled_at"] == order.filled_at
    pos = journal.open_positions()[0]
    assert pos["qty"] == 10 and pos["avg_entry_price"] == 101.2 and pos["order_id"] == row_id
    assert fake.submits == 1


@pytest.mark.parametrize("accepted", [False, True])
def test_process_death_has_durable_gate_before_any_incident(tmp_path, monkeypatch, accepted):
    fake, settings, journal, manager, proposal = setup(tmp_path, monkeypatch)
    if accepted:
        fake.response_error = SystemExit("process died after accepted")
    else:
        def crash(spec):
            raise SystemExit("process died before POST")
        fake.before_post = crash
    with pytest.raises(SystemExit):
        manager.execute_proposal(proposal)
    assert pending_entry_intent(journal) and not journal.query("SELECT * FROM protection_checks")
    journal, manager = restart(fake, settings, journal)
    # This direct call uses no broker endpoints, including empty list snapshots.
    assert manager.execute_proposal(proposal).submission_uncertain
    manager.reconcile()
    assert fake.submits == 1
    if accepted:
        assert pending_entry_intent(journal) is None
    else:
        assert pending_entry_intent(journal) and protection_watchdog.has_blocking_incident(journal)
        assert paper_row(journal)["state"] == "submitted"


def test_temporary_404_never_rejects_or_resubmits_then_can_recover(tmp_path, monkeypatch):
    fake, _, journal, manager, proposal = setup(tmp_path, monkeypatch)
    fake.response_error, fake.lookup_error = ReadTimeout("lost"), NotFound("temporarily absent")
    result = manager.execute_proposal(proposal)
    for _ in range(3):
        assert manager.reconcile()["errors"]
    assert result.submission_uncertain and paper_row(journal)["state"] == "submitted"
    assert "not found" in ledger_ops.metadata(paper_row(journal))["last_verification_error"]
    fake.lookup_error = None
    assert manager.reconcile()["errors"] == []
    assert pending_entry_intent(journal) is None and fake.submits == 1


@pytest.mark.parametrize("field,value", [
    ("client_order_id", "foreign"), ("symbol", "FOREIGN"), ("side", "sell"),
    ("qty", 11), ("qty", float("nan")), ("qty", float("inf")), ("filled_qty", 11),
])
def test_mismatched_recovery_cannot_book_fills(tmp_path, monkeypatch, field, value):
    fake, _, journal, manager, proposal = setup(tmp_path, monkeypatch)
    fake.response_error = ReadTimeout("lost")
    def mutate(order):
        order.status, order.filled_qty, order.filled_avg_price = "filled", 10, 100
        setattr(order, field, value)
    fake.mutate_lookup = mutate
    assert manager.execute_proposal(proposal).submission_uncertain
    manager.reconcile()
    assert journal.count_open_positions() == 0 and not journal.query("SELECT * FROM paper_fills")
    assert fake.submits == 1 and pending_entry_intent(journal)


def test_second_nested_lookup_must_match_first_parent_id(tmp_path, monkeypatch):
    fake, _, journal, manager, proposal = setup(tmp_path, monkeypatch)
    fake.response_error = ReadTimeout("lost")
    def wrong_parent(order):
        order.id = "wrong-parent"
        order.status, order.filled_qty, order.filled_avg_price = "filled", 10, 100
    fake.mutate_nested = wrong_parent
    assert manager.execute_proposal(proposal).submission_uncertain
    assert not journal.query("SELECT * FROM paper_fills") and journal.count_open_positions() == 0
    assert paper_row(journal)["broker_order_id"] is None


def test_unverified_conditional_protection_keeps_recovered_intent_blocking(tmp_path, monkeypatch):
    fake, _, journal, manager, proposal = setup(tmp_path, monkeypatch)
    fake.response_error, fake.lookup_error = ReadTimeout("lost"), ReadTimeout("offline")
    manager.execute_proposal(proposal)
    fake.lookup_error = None
    order = fake._find("XYZ")
    order.legs[1].status = "canceled"
    assert manager.reconcile()["errors"] and pending_entry_intent(journal)
    assert protection_watchdog.has_blocking_incident(journal)
    order.legs[1].status = "held"
    assert manager.reconcile()["errors"] == []
    assert not pending_entry_intent(journal) and not protection_watchdog.has_blocking_incident(journal)
    assert fake.submits == 1


@pytest.mark.parametrize("change", ["entry_price", "stop_price", "target_price", "tif", "leg_tif",
                                    "leg_qty", "leg_side", "duplicate_role", "duplicate_id"])
def test_changed_authorized_bracket_cannot_release_entry_gate(tmp_path, monkeypatch, change):
    fake, _, journal, manager, proposal = setup(tmp_path, monkeypatch)
    fake.response_error = ReadTimeout("lost")
    def mutate(order):
        if change == "entry_price":
            order.limit_price = 110
        elif change == "stop_price":
            order.legs[1].stop_price = 90
        elif change == "target_price":
            order.legs[0].limit_price = 101
        elif change == "tif":
            order.time_in_force = "day"
        elif change == "leg_tif":
            order.legs[1].time_in_force = "day"
        elif change == "leg_qty":
            order.legs[1].qty = 1
        elif change == "leg_side":
            order.legs[1].side = "buy"
        elif change == "duplicate_role":
            order.legs[1].stop_price = None
            order.legs[1].order_type = "limit"
            order.legs[1].limit_price = 106
        else:
            order.legs[1].id = order.legs[0].id
    fake.mutate_nested = mutate
    assert manager.execute_proposal(proposal).submission_uncertain
    assert pending_entry_intent(journal) and protection_watchdog.has_blocking_incident(journal)
    assert paper_row(journal)["time_in_force"] == "gtc"
    assert not journal.query("SELECT * FROM paper_fills") and fake.submits == 1


def test_unknown_entry_finishes_after_position_closed_and_final_verification_failed(tmp_path, monkeypatch):
    fake, _, journal, manager, proposal = setup(tmp_path, monkeypatch)
    fake.response_error, fake.lookup_error = ReadTimeout("lost"), ReadTimeout("offline")
    manager.execute_proposal(proposal)
    fake.lookup_error = None
    fake.fill_entry("XYZ", 100)
    fake.fill_leg("XYZ", "take_profit", 106)
    original = fake.get_all_positions
    calls = 0
    def fail_final_verification():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ReadTimeout("flatness verification unavailable after local close")
        return original()
    fake.get_all_positions = fail_final_verification
    result = manager.reconcile()
    assert result["errors"] and journal.one("SELECT * FROM positions")["status"] == "closed"
    assert pending_entry_intent(journal)
    fake.get_all_positions = original
    assert manager.reconcile()["errors"] == []
    assert not pending_entry_intent(journal) and not protection_watchdog.has_blocking_incident(journal)
    assert len(journal.query("SELECT * FROM exits")) == 1 and fake.submits == 1


def test_concurrent_same_proposal_cannot_claim_second_post(tmp_path, monkeypatch):
    fake, settings, journal, _, proposal = setup(tmp_path, monkeypatch)
    reached_post, release_post = Event(), Event()
    def wait_at_post(spec):
        reached_post.set()
        assert release_post.wait(5)
    fake.before_post = wait_at_post
    def execute():
        local = JournalStore(journal.db_path)
        try:
            manager = OrderManager(settings, local, alpaca=AlpacaClient(settings, local, trading_client=fake))
            return manager.execute_proposal(proposal)
        finally:
            local.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(execute)
        assert reached_post.wait(5)
        try:
            second = pool.submit(execute).result(timeout=5)
            assert second.submission_uncertain
            assert fake.submits == 1
        finally:
            release_post.set()
        assert first.result(timeout=5).state == "accepted"
    assert len(journal.query("SELECT * FROM paper_orders")) == 1 and fake.submits == 1


def test_orchestrator_repeat_observes_intent_without_risk_or_broker_actions(tmp_path, monkeypatch):
    from alphaos.orchestrator import Orchestrator
    fake, _, journal, manager, proposal = setup(tmp_path, monkeypatch)
    fake.response_error, fake.lookup_error = ReadTimeout("lost"), ReadTimeout("offline")
    original = manager.execute_proposal(proposal)
    # Missing risk/approval methods deliberately prove this path is a read of
    # the existing attempt, independent of preflights for a new submission.
    orchestrator = SimpleNamespace(orders=manager, journal=journal)
    repeated = Orchestrator._execute(orchestrator, proposal)
    assert repeated.submission_uncertain and not repeated.blocked
    assert repeated.order["order_id"] == original.order["order_id"] and fake.submits == 1
    assert len(journal.query("SELECT * FROM paper_orders")) == 1
