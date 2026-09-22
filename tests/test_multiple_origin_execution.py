"""Independent origins share aggregate stock, never fills or protection ownership."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from alphaos.config.settings import load_settings
from alphaos.execution import ledger_ops, protection_watchdog
from alphaos.execution.order_manager import OrderManager
from alphaos.execution.position_attribution import recheck_symbol_attribution, verify_symbol_attribution
from alphaos.execution.time_exit_lifecycle import TimeExitLifecycle
from alphaos.journal.journal_store import JournalStore
from alphaos.risk.account_exposure import read_exposure
from alphaos.safety import KillSwitch
from alphaos.util import alerts
from test_entry_recovery_exposure import ReadOnlyBroker, seed_verified_holding

NOW = datetime(2026, 9, 28, 17, tzinfo=timezone.utc)
TERMINAL = {"filled", "canceled", "cancelled", "rejected", "expired"}


class Broker(ReadOnlyBroker):
    def __init__(self, journal):
        super().__init__(journal)
        self.writes = []
        self.partial_close = None

    def get_order(self, oid):
        item = super().get_order(oid)
        if item.get("legs"):
            item["legs"] = [{**deepcopy(self.orders[leg["broker_order_id"]]), "role": leg["role"], "legs": []}
                            for leg in item["legs"]]
        return item

    def list_open_orders(self):
        assert not self.journal.conn.in_transaction
        return [self.get_order(oid) for oid, order in self.orders.items()
                if order["status"] not in TERMINAL | {"held"}]

    def get_account(self):
        return {"equity": 100000, "last_equity": 100000, "trading_blocked": False}

    def get_calendar(self, start, end):
        return [{"date": str(start), "open": "09:30:00", "close": "16:00:00"}]

    def cancel_order(self, oid):
        self.writes.append(("cancel", oid))
        self.orders[oid].update(status="canceled", state="cancelled")

    def get_order_by_client_id(self, client_id):
        return next((self.get_order(oid) for oid, item in self.orders.items()
                     if item.get("client_order_id") == client_id), None)

    def submit_position_reduction(self, symbol, qty, direction, client_order_id):
        self.writes.append(("reduce", qty))
        filled = qty if self.partial_close is None else self.partial_close
        oid = "close_" + client_order_id
        self.orders[oid] = {"broker_order_id": oid, "client_order_id": client_order_id, "symbol": symbol,
                            "side": "sell", "qty": qty, "filled_qty": filled, "filled_avg_price": 102,
                            "status": "filled" if filled == qty else "partially_filled",
                            "state": "filled" if filled == qty else "partially_filled",
                            "filled_at": NOW.isoformat(), "order_class": "simple", "order_type": "market",
                            "time_in_force": "day", "legs": []}
        self.positions[0]["qty"] -= filled
        return self.get_order(oid)


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setattr(alerts, "send_alert", lambda *a, **k: None)
    journal = JournalStore(str(tmp_path / "origins.db"))
    broker = Broker(journal)
    for suffix in ("first", "second"):
        seed_verified_holding(journal, broker, suffix=suffix, qty=21)
    for parent in list(broker.orders.values()):
        for leg in parent["legs"]:
            broker.orders[leg["broker_order_id"]] = deepcopy(leg)
    journal.conn.execute("UPDATE positions SET avg_entry_price=100")
    journal.conn.execute("UPDATE paper_orders SET limit_price=100,stop_loss_price=97,take_profit_price=106")
    journal.conn.commit()
    broker.positions[0]["market_value"] = 4200
    settings = load_settings(load_env_file=False, env={"ALPHAOS_MODE": "paper", "REAL_TRADING_ENABLED": "false",
        "ALPACA_API_KEY": "offline", "ALPACA_SECRET_KEY": "offline", "ALPACA_PAPER": "true",
        "REAL_PAPER_EXECUTION": "true", "ALPHAOS_DB_PATH": journal.db_path})
    manager = OrderManager(settings, journal, alpaca=broker, kill_switch=KillSwitch(str(tmp_path / "HALT")))
    yield journal, broker, manager
    journal.close()


def position(journal, suffix="first"):
    return journal.one("SELECT * FROM positions WHERE position_id=?", ("pos_" + suffix,))


def ids(suffix="first"):
    return ["broker_" + suffix, "broker_" + suffix + "_tp", "broker_" + suffix + "_sl"]


def fill(broker, qty, average=105, suffix="first"):
    parent, target, stop = ids(suffix)
    old = broker.orders[target]["filled_qty"]
    broker.orders[target].update(filled_qty=qty, filled_avg_price=average, filled_at=NOW.isoformat(),
        status="filled" if qty == 21 else "partially_filled", state="filled" if qty == 21 else "partially_filled")
    broker.orders[stop]["qty"] = 21 - qty
    if qty == 21:
        broker.orders[stop]["status"] = "canceled"
    broker.positions[0]["qty"] -= qty - old
    return broker.get_order(target)


def test_two_21_origins_preserve_two_slots_and_aggregate_gross(case):
    journal, broker, manager = case
    result = read_exposure(broker, journal)
    assert result["open_positions"] == 2 and result["broker_position_slots"] == 1
    assert result["gross_committed_notional"] == 4200
    for suffix in ("first", "second"):
        p = position(journal, suffix)
        check = protection_watchdog.check_position(journal, broker, p, {"XYZ": broker.positions[0]}, manager.settings)
        assert check.protection_status == "protected" and check.broker_qty == 42 and check.qty_match
        assert "aggregate 42" in check.detail and "origin 21" in check.detail
        assert TimeExitLifecycle(manager)._fresh(p, ids(suffix)) == 21
    assert broker.writes == []


@pytest.mark.parametrize("mutation", ["aggregate", "side", "fill", "shared_child", "missing_stop", "overprotect", "terms", "unknown_order"])
def test_ambiguous_origin_never_authorizes_cancel_or_reduce(case, mutation):
    journal, broker, manager = case
    if mutation == "aggregate":
        broker.positions[0]["qty"] = 43
    elif mutation == "side":
        broker.positions[0].update(side="short", qty=-42)
    elif mutation == "fill":
        journal.conn.execute("UPDATE paper_fills SET qty=20 WHERE position_id='pos_second'")
        journal.conn.commit()
    elif mutation == "shared_child":
        broker.orders["broker_second"]["legs"][0]["broker_order_id"] = "broker_first_tp"
    elif mutation == "missing_stop":
        broker.orders["broker_second_sl"]["status"] = "canceled"
    elif mutation == "overprotect":
        broker.orders["broker_first_tp"]["qty"] = 42
    elif mutation == "terms":
        broker.orders["broker_second_sl"]["stop_price"] = 96
    else:
        broker.orders["foreign"] = {"broker_order_id": "foreign", "symbol": "XYZ", "status": "new", "qty": 1, "side": "sell"}
    result = TimeExitLifecycle(manager).advance(position(journal), NOW)
    assert result.get("critical") and not broker.writes
    with pytest.raises(ValueError):
        read_exposure(broker, journal)


def test_one_origin_closes_once_with_sibling_unchanged(case):
    journal, broker, manager = case
    sibling = position(journal, "second")
    sibling_orders = {oid: deepcopy(broker.orders[oid]) for oid in ids("second")}
    receipt = fill(broker, 21)
    assert manager._record_broker_exit(position(journal), receipt, "target") is not None
    assert position(journal)["status"] == "closed"
    assert position(journal, "second") == sibling
    assert broker.positions[0]["qty"] == 21
    assert manager._record_broker_exit(position(journal), receipt, "target") is None
    assert journal.scalar("SELECT COUNT(*) FROM trade_outcomes") == 1
    assert sibling_orders == {oid: broker.orders[oid] for oid in ids("second")}


def test_partial_then_restart_then_final_cumulative_fill(case):
    journal, broker, manager = case
    assert manager._record_broker_exit(position(journal), fill(broker, 7, 104), "target") is None
    assert position(journal)["qty"] == 14
    # Recreate the manager to discard any transient execution object state.
    manager = OrderManager(manager.settings, journal, alpaca=broker, kill_switch=manager.kill_switch)
    assert manager._record_broker_exit(position(journal), fill(broker, 21, 105), "target") is not None
    fills = journal.query("SELECT qty,price FROM paper_fills WHERE position_id='pos_first' AND side='sell' ORDER BY id")
    assert [f["qty"] for f in fills] == [7, 14]
    assert sum(f["qty"] * f["price"] for f in fills) == pytest.approx(2205)
    assert position(journal, "second")["qty"] == 21


def test_time_close_cancels_and_reduces_only_target_origin(case):
    journal, broker, manager = case
    before = {oid: deepcopy(broker.orders[oid]) for oid in ids("second")}
    result = TimeExitLifecycle(manager).advance(position(journal), NOW)
    assert result.get("ok"), result
    assert broker.writes == [("cancel", "broker_first_tp"), ("cancel", "broker_first_sl"), ("reduce", 21)]
    assert before == {oid: broker.orders[oid] for oid in ids("second")}
    assert journal.scalar("SELECT qty FROM trade_outcomes") == 21


@pytest.mark.parametrize("mutation", ["foreign", "side", "sibling"])
def test_unowned_exit_receipt_is_rejected_before_bookkeeping(case, mutation):
    journal, broker, manager = case
    norm = fill(broker, 7)
    norm.update({"broker_order_id": "foreign"} if mutation == "foreign" else
                {"side": "buy"} if mutation == "side" else {"broker_order_id": "broker_second_tp"})
    before = list(journal.conn.iterdump())
    with pytest.raises(ValueError):
        manager._record_broker_exit(position(journal), norm, "target")
    assert before == list(journal.conn.iterdump())


def test_atomic_recheck_catches_sibling_change_without_network(case):
    journal, broker, manager = case
    proof = verify_symbol_attribution(journal, broker, "XYZ")
    calls = broker.position_calls
    with ledger_ops.atomic(journal) as tx:
        tx.conn.execute("UPDATE positions SET qty=20 WHERE position_id='pos_second'")
        with pytest.raises(ValueError):
            recheck_symbol_attribution(tx, proof)
    assert broker.position_calls == calls


def test_cents_rounding_matches_actual_submission_not_arbitrary_tolerance(case):
    journal, broker, manager = case
    journal.conn.execute("UPDATE positions SET stop_price=97.0043")
    journal.conn.commit()
    assert read_exposure(broker, journal)["open_positions"] == 2
    broker.orders["broker_second_sl"]["stop_price"] = 96.99
    with pytest.raises(ValueError):
        read_exposure(broker, journal)


def test_stale_watchdog_proof_does_not_clear_incident(case):
    journal, broker, manager = case
    p = position(journal)
    protection_watchdog.open_protection_incident(journal, p, protection_status="unverifiable", severity="critical", detail="old mismatch")
    result = protection_watchdog.check_position(journal, broker, p, {"XYZ": broker.positions[0]}, manager.settings)
    assert result.protection_status == "protected"
    journal.conn.execute("UPDATE positions SET qty=20 WHERE position_id='pos_second'")
    journal.conn.commit()
    protection_watchdog._record_check(journal, p, result, None)
    assert result.protection_status == "unverifiable"
    assert protection_watchdog.has_blocking_incident(journal) is not None


@pytest.mark.parametrize("mutation", ["shared_entry", "shared_child"])
def test_shared_exit_lineage_cannot_book_even_a_partial(case, mutation):
    journal, broker, manager = case
    norm = fill(broker, 7, suffix="second")
    if mutation == "shared_entry":
        journal.conn.execute("UPDATE positions SET order_id='entry_second',broker_order_id='broker_second' WHERE position_id='pos_first'")
        journal.conn.commit()
    else:
        broker.orders["broker_first"]["legs"][0]["broker_order_id"] = "broker_second_tp"
    before = list(journal.conn.iterdump())
    with pytest.raises(ValueError):
        manager._record_broker_exit(position(journal), norm, "target")
    assert before == list(journal.conn.iterdump())


def test_zero_residual_open_origin_waits_for_finalization_not_protection(case):
    journal, broker, manager = case
    norm = fill(broker, 21)
    broker.positions[0]["qty"] = 22
    assert manager._record_broker_exit(position(journal), norm, "target") is None
    broker.positions[0]["qty"] = 21
    check = protection_watchdog.check_position(journal, broker, position(journal), {"XYZ": broker.positions[0]}, manager.settings)
    assert check.protection_status == "unverifiable" and check.stop_live is not True
    assert manager._record_broker_exit(position(journal), norm, "target") is not None
    assert position(journal, "second")["qty"] == 21


def test_recovered_open_entry_rechecks_sibling_before_clearing_intent(case):
    from alphaos.execution.entry_submission import EntrySubmission
    journal, broker, manager = case
    row = journal.order_by_id("entry_first")
    with ledger_ops.atomic(journal) as tx:
        ledger_ops.update(tx, "paper_orders", "order_id", row["order_id"],
                          {"raw_request_json": {"purpose": "paper_entry", "phase": "unknown"}})
    row = journal.order_by_id(row["order_id"])
    entry = EntrySubmission(manager)
    verify = entry._verify_recovery
    def stale(*args):
        proof = verify(*args)
        journal.conn.execute("UPDATE positions SET qty=20 WHERE position_id='pos_second'")
        journal.conn.commit()
        return proof
    entry._verify_recovery = stale
    with pytest.raises(ValueError, match="changed"):
        entry.finish(row, broker.get_order(row["broker_order_id"]), verify_recovery=True)
    assert ledger_ops.metadata(journal.order_by_id(row["order_id"]))["phase"] == "unknown"
