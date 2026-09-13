"""Recovery attributes a held symbol; it never assumes every holding is ours."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

from alphaos.execution import ledger_ops
from alphaos.execution.entry_recovery_exposure import recheck_entry_absence, verify_entry_absence
from alphaos.journal.journal_store import JournalStore


class ReadOnlyBroker:
    def __init__(self, journal):
        self.journal = journal
        self.positions = []
        self.open_orders = []
        self.orders = {}
        self.position_calls = 0
        self.order_calls = []
        self.on_positions = None
        self.on_order = None

    def list_positions(self):
        assert not self.journal.conn.in_transaction
        self.position_calls += 1
        if self.on_positions:
            self.on_positions(self)
        return deepcopy(self.positions)

    def list_open_orders(self):
        assert not self.journal.conn.in_transaction
        return deepcopy(self.open_orders)

    def get_order(self, oid):
        assert not self.journal.conn.in_transaction
        self.order_calls.append(oid)
        if self.on_order:
            self.on_order(self, oid)
        return deepcopy(self.orders[oid])


def failed_intent(journal):
    journal.insert("paper_orders", {
        "order_id": "failed", "client_order_id": "proposal_failed", "proposal_id": "proposal_failed",
        "broker_order_id": "broker_failed", "symbol": "XYZ", "direction": "long", "side": "buy",
        "qty": 5, "order_type": "bracket", "order_class": "bracket", "execution_source": "alpaca_paper",
        "raw_request_json": {"purpose": "paper_entry", "phase": "unknown"},
    })
    return journal.order_by_id("failed")


def seed_verified_holding(journal, broker, *, suffix="old", direction="long", qty=5, replacement=False, exited=0):
    """Realistic N4 fixture: durable entry, position, fills and fresh protection.

    Unlike the archived minimal N4 probe, the existing position's ownership is
    established positively. This fixture is reusable by caller integration tests.
    """
    position_id, order_id = "pos_" + suffix, "entry_" + suffix
    parent_id, client_id = "broker_" + suffix, "proposal_" + suffix
    entry_side, exit_side = ("buy", "sell") if direction == "long" else ("sell", "buy")
    local_entry, local_exit = ("buy", "sell") if direction == "long" else ("sell_short", "buy_to_cover")
    total = qty + exited
    stop, target = (97, 106) if direction == "long" else (103, 94)
    parent = {"broker_order_id": parent_id, "client_order_id": client_id, "symbol": "XYZ", "side": entry_side,
              "qty": total, "filled_qty": total, "filled_avg_price": 100, "order_class": "bracket",
              "status": "filled", "time_in_force": "gtc", "limit_price": 100, "stop_price": None,
              "legs": [
                  {"broker_order_id": parent_id + "_tp", "role": "take_profit", "symbol": "XYZ", "side": exit_side,
                   "qty": total, "filled_qty": exited, "status": "partially_filled" if exited else "new",
                   "limit_price": target, "stop_price": None, "time_in_force": "gtc"},
                  {"broker_order_id": parent_id + "_sl", "role": "stop_loss", "symbol": "XYZ", "side": exit_side,
                   "qty": qty, "filled_qty": 0, "status": "held", "limit_price": None, "stop_price": stop,
                   "time_in_force": "gtc"},
              ]}
    journal.insert("paper_orders", {"order_id": order_id, "client_order_id": client_id, "proposal_id": client_id,
                                    "broker_order_id": parent_id, "symbol": "XYZ", "direction": direction,
                                    "side": local_entry, "qty": total, "order_type": "bracket", "order_class": "bracket",
                                    "time_in_force": "gtc",
                                    "execution_source": "alpaca_paper", "trade_id": "trade_" + suffix,
                                    "state": "filled", "raw_request_json": {"purpose": "paper_entry", "phase": "acknowledged"}})
    journal.insert("positions", {"position_id": position_id, "order_id": order_id, "broker_order_id": parent_id,
                                 "proposal_id": client_id, "trade_id": "trade_" + suffix, "symbol": "XYZ",
                                 "direction": direction, "qty": qty, "status": "open", "stop_price": stop,
                                 "target_price": target, "execution_source": "alpaca_paper", "max_holding_days": 5})
    journal.insert("paper_fills", {"fill_id": "fill_" + suffix, "order_id": order_id, "broker_order_id": parent_id,
                                  "position_id": position_id, "symbol": "XYZ", "side": local_entry, "qty": total,
                                  "price": 100, "execution_source": "alpaca_paper", "trade_id": "trade_" + suffix})
    broker.orders[parent_id] = parent
    if exited:
        leg = parent["legs"][0]
        exit_order_id = "exit_" + suffix
        journal.insert("paper_orders", {"order_id": exit_order_id, "broker_order_id": leg["broker_order_id"],
                                        "symbol": "XYZ", "side": local_exit, "qty": total,
                                        "execution_source": "alpaca_paper", "trade_id": "trade_" + suffix,
                                        "raw_request_json": {"purpose": "position_exit", "position_id": position_id}})
        journal.insert("paper_fills", {"fill_id": "exitfill_" + suffix, "order_id": exit_order_id,
                                      "broker_order_id": leg["broker_order_id"], "position_id": position_id,
                                      "symbol": "XYZ", "side": local_exit, "qty": exited, "price": target,
                                      "execution_source": "alpaca_paper", "trade_id": "trade_" + suffix})
        broker.orders[leg["broker_order_id"]] = deepcopy(leg)
    protection = parent
    if replacement:
        protection_id = "oco_" + suffix
        replacement_client = "client_" + protection_id
        protection = {"broker_order_id": protection_id, "client_order_id": replacement_client,
                      "symbol": "XYZ", "side": exit_side, "qty": qty, "filled_qty": 0,
                      "order_class": "oco", "status": "new", "limit_price": target, "stop_price": None,
                      "time_in_force": "gtc", "legs": [
                          {"broker_order_id": protection_id, "role": "take_profit", "symbol": "XYZ", "side": exit_side,
                           "qty": qty, "filled_qty": 0, "status": "new", "limit_price": target, "stop_price": None,
                           "time_in_force": "gtc"},
                          {"broker_order_id": protection_id + "_sl", "role": "stop_loss", "symbol": "XYZ", "side": exit_side,
                           "qty": qty, "filled_qty": 0, "status": "held", "stop_price": stop, "limit_price": None,
                           "time_in_force": "gtc"},
                      ]}
        journal.insert("paper_orders", {"order_id": "reprotect_" + suffix, "broker_order_id": protection_id,
                                        "client_order_id": replacement_client, "symbol": "XYZ", "side": local_exit,
                                        "qty": qty, "order_type": "oco", "execution_source": "alpaca_paper",
                                        "time_in_force": "gtc",
                                        "raw_request_json": {"purpose": "reprotect", "position_id": position_id, "phase": "protected"}})
        journal.conn.execute("UPDATE positions SET broker_order_id=? WHERE position_id=?", (protection_id, position_id))
        journal.conn.commit()
        broker.orders[protection_id] = protection
        for leg in parent["legs"]:
            leg["status"] = "canceled"
    broker.open_orders.append(deepcopy(protection if replacement else protection["legs"][0]))
    broker.positions = [{"symbol": "XYZ", "qty": sum(p["qty"] for p in journal.open_positions()) * (-1 if direction == "short" else 1),
                         "side": direction}]
    return {"position_id": position_id, "entry_id": order_id, "parent": parent, "protection": protection}


@pytest.fixture
def case(tmp_path):
    journal = JournalStore(str(tmp_path / "recovery.db"))
    broker = ReadOnlyBroker(journal)
    row = failed_intent(journal)
    yield journal, broker, row
    journal.close()


def terminal(row):
    return {"broker_order_id": row["broker_order_id"], "client_order_id": row["client_order_id"],
            "symbol": row["symbol"], "status": "rejected", "filled_qty": 0, "legs": []}


def test_flat_recovery_and_atomic_recheck_are_read_only(case):
    journal, broker, row = case
    before = list(journal.conn.iterdump())
    evidence = verify_entry_absence(journal, broker, row, terminal_order=terminal(row))
    assert evidence["attributed_positions"] == [] and broker.position_calls == 2
    with ledger_ops.atomic(journal) as txn:
        recheck_entry_absence(txn, row, evidence)
    assert before == list(journal.conn.iterdump())


@pytest.mark.parametrize("direction,replacement,exited", [("long", False, 0), ("short", False, 0),
                                                        ("long", True, 0), ("short", True, 0),
                                                        ("long", False, 2), ("short", False, 2), ("long", True, 2)])
def test_n4_known_held_symbol_can_recover_with_fresh_complete_lineage(case, direction, replacement, exited):
    journal, broker, row = case
    seed_verified_holding(journal, broker, direction=direction, replacement=replacement, exited=exited)
    before = list(journal.conn.iterdump())
    evidence = verify_entry_absence(journal, broker, row, terminal_order=terminal(row))
    assert evidence["broker_position"] == {"symbol": "XYZ", "qty": 5, "direction": direction}
    assert evidence["attributed_positions"][0]["exit_fills"] == exited
    assert broker.position_calls == 2
    assert all(broker.order_calls.count(oid) == 2 for oid in set(broker.order_calls))
    assert before == list(journal.conn.iterdump())
    with ledger_ops.atomic(journal) as txn:
        recheck_entry_absence(txn, row, evidence)


def test_two_separate_fully_attributed_holdings(case):
    journal, broker, row = case
    seed_verified_holding(journal, broker)
    seed_verified_holding(journal, broker, suffix="second", qty=3)
    evidence = verify_entry_absence(journal, broker, row)
    assert len(evidence["attributed_positions"]) == 2 and evidence["broker_position"]["qty"] == 8


@pytest.mark.parametrize("cause", ["unowned", "qty", "direction", "missing_entry", "missing_fill", "wrong_fill_broker", "own_position", "own_fill", "own_child_fill", "foreign_order", "related_other_symbol", "partial_entry", "legacy_oco", "wrong_entry_client", "wrong_protection_price", "unknown_exit_parent"])
def test_ambiguous_or_target_related_exposure_blocks(case, cause):
    journal, broker, row = case
    held = seed_verified_holding(journal, broker, replacement=cause == "legacy_oco", exited=2 if cause == "unknown_exit_parent" else 0)
    norm = terminal(row)
    if cause == "unowned":
        journal.conn.execute("DELETE FROM positions")
    elif cause == "qty":
        broker.positions[0]["qty"] = 6
    elif cause == "direction":
        broker.positions[0].update(qty=-5, side="short")
    elif cause == "missing_entry":
        journal.conn.execute("DELETE FROM paper_orders WHERE order_id='entry_old'")
    elif cause == "missing_fill":
        journal.conn.execute("DELETE FROM paper_fills")
    elif cause == "wrong_fill_broker":
        journal.conn.execute("UPDATE paper_fills SET broker_order_id='unknown'")
    elif cause == "own_position":
        journal.conn.execute("UPDATE positions SET order_id='failed'")
    elif cause == "own_fill":
        journal.conn.execute("UPDATE paper_fills SET order_id='failed'")
    elif cause == "own_child_fill":
        norm["legs"] = [{"broker_order_id": "failed_child", "filled_qty": 1}]
    elif cause == "foreign_order":
        broker.open_orders.append({"broker_order_id": "foreign", "symbol": "XYZ"})
    elif cause == "related_other_symbol":
        broker.open_orders.append({"broker_order_id": "foreign", "symbol": "ABC", "client_order_id": row["client_order_id"]})
    elif cause == "partial_entry":
        held["parent"].update(status="partially_filled", qty=8)
    elif cause == "legacy_oco":
        journal.conn.execute("UPDATE paper_orders SET raw_request_json='{}' WHERE order_id='reprotect_old'")
    elif cause == "wrong_entry_client":
        held["parent"]["client_order_id"] = "someone_else"
    elif cause == "wrong_protection_price":
        held["protection"]["legs"][1]["stop_price"] = 1
    elif cause == "unknown_exit_parent":
        held["parent"]["legs"][0]["broker_order_id"] = "replacement_unknown_tp"
    journal.conn.commit()
    with pytest.raises(ValueError):
        verify_entry_absence(journal, broker, row, terminal_order=norm)


@pytest.mark.parametrize("role,status", [("stop_loss", "pending_cancel"), ("stop_loss", "pending_new"),
                                        ("take_profit", "held"), ("take_profit", "unknown")])
def test_nonworking_protection_never_certifies_held_symbol(case, role, status):
    journal, broker, row = case
    held = seed_verified_holding(journal, broker)
    next(leg for leg in held["protection"]["legs"] if leg["role"] == role)["status"] = status
    with pytest.raises(ValueError, match="protection"):
        verify_entry_absence(journal, broker, row)


@pytest.mark.parametrize("mutation", ["positions", "ledger_fill", "ledger_intent", "protective_fill", "protective_cancel"])
def test_changes_during_broker_reads_block_recovery(case, mutation):
    journal, broker, row = case
    seed_verified_holding(journal, broker)
    def change(current):
        if current.position_calls != 2:
            return
        if mutation == "positions":
            current.positions[0]["qty"] = 4
        elif mutation == "ledger_fill":
            journal.conn.execute("UPDATE paper_fills SET qty=4")
            journal.conn.commit()
        elif mutation == "ledger_intent":
            journal.conn.execute("UPDATE paper_orders SET state='rejected' WHERE order_id='failed'")
            journal.conn.commit()
        elif mutation == "protective_fill":
            current.orders["broker_old"]["legs"][0]["filled_qty"] = 1
        elif mutation == "protective_cancel":
            current.orders["broker_old"]["legs"][1]["status"] = "pending_cancel"
    broker.on_positions = change
    with pytest.raises(ValueError, match="changed|not fully attributable"):
        verify_entry_absence(journal, broker, row)


@pytest.mark.parametrize("mutation", ["position", "fill", "intent", "new_order"])
def test_finalization_fingerprint_rejects_intervening_local_changes(case, mutation):
    journal, broker, row = case
    seed_verified_holding(journal, broker)
    evidence = verify_entry_absence(journal, broker, row)
    calls = list(broker.order_calls)
    with ledger_ops.atomic(journal) as txn:
        if mutation == "position":
            txn.conn.execute("UPDATE positions SET qty=4")
        elif mutation == "fill":
            txn.conn.execute("UPDATE paper_fills SET qty=4")
        elif mutation == "intent":
            txn.conn.execute("UPDATE paper_orders SET qty=6 WHERE order_id='failed'")
        else:
            txn.insert("paper_orders", {"order_id": "late", "symbol": "XYZ"})
        with pytest.raises(ValueError, match="changed"):
            recheck_entry_absence(txn, row, evidence)
    assert calls == broker.order_calls


def test_no_broker_reads_inside_transaction_and_no_unlocked_finalization(case):
    journal, broker, row = case
    with ledger_ops.atomic(journal) as txn, pytest.raises(ValueError, match="cannot run inside"):
        verify_entry_absence(txn, broker, row)
    evidence = verify_entry_absence(journal, broker, row)
    with pytest.raises(ValueError, match="requires an atomic"):
        recheck_entry_absence(journal, row, evidence)


def test_archived_minimal_n4_holding_without_history_remains_unverifiable(case):
    journal, broker, row = case
    seed_verified_holding(journal, broker)
    journal.conn.execute("DELETE FROM paper_orders WHERE order_id='entry_old'")
    journal.conn.execute("DELETE FROM paper_fills")
    journal.conn.commit()
    with pytest.raises(ValueError, match="missing.*entry lineage"):
        verify_entry_absence(journal, broker, row)


def test_two_local_origins_cannot_count_one_broker_entry_twice(case):
    journal, broker, row = case
    seed_verified_holding(journal, broker, replacement=True)
    seed_verified_holding(journal, broker, suffix="second", replacement=True)
    journal.conn.execute("UPDATE paper_orders SET broker_order_id='broker_old',client_order_id='proposal_old' WHERE order_id='entry_second'")
    journal.conn.execute("UPDATE paper_fills SET broker_order_id='broker_old' WHERE order_id='entry_second'")
    journal.conn.commit()
    with pytest.raises(ValueError, match="entry is multiply attributed"):
        verify_entry_absence(journal, broker, row)


def test_two_local_exit_rows_cannot_subtract_one_broker_fill_twice(case):
    journal, broker, row = case
    seed_verified_holding(journal, broker, qty=3, exited=2)
    existing = journal.order_by_id("exit_old")
    extra = {key: value for key, value in existing.items() if key != "id"}
    extra["order_id"] = "duplicate_exit"
    journal.insert("paper_orders", extra)
    fill = journal.one("SELECT * FROM paper_fills WHERE order_id='exit_old'")
    extra_fill = {key: value for key, value in fill.items() if key != "id"}
    extra_fill.update(fill_id="duplicate_fill", order_id="duplicate_exit")
    journal.insert("paper_fills", extra_fill)
    journal.conn.execute("UPDATE positions SET qty=1")
    journal.conn.commit()
    broker.positions[0]["qty"] = 1
    with pytest.raises(ValueError, match="exit fills are multiply attributed"):
        verify_entry_absence(journal, broker, row)


def test_conflicting_client_id_for_known_oco_is_not_ignored(case):
    journal, broker, row = case
    seed_verified_holding(journal, broker, replacement=True)
    broker.open_orders[0]["client_order_id"] = "wrong_client"
    with pytest.raises(ValueError, match="client identity"):
        verify_entry_absence(journal, broker, row)


@pytest.mark.parametrize("holding_days,passes", [(0, True), (5, False), (None, False)])
def test_day_protection_honors_existing_intraday_policy(case, holding_days, passes):
    journal, broker, row = case
    held = seed_verified_holding(journal, broker)
    journal.conn.execute("UPDATE paper_orders SET time_in_force='day' WHERE order_id='entry_old'")
    journal.conn.execute("UPDATE positions SET max_holding_days=?", (holding_days,))
    journal.conn.commit()
    for node in [held["parent"], *held["parent"]["legs"], *broker.open_orders]:
        node["time_in_force"] = "day"
    if passes:
        assert verify_entry_absence(journal, broker, row)["attributed_positions"]
    else:
        with pytest.raises(ValueError, match="expire intraday"):
            verify_entry_absence(journal, broker, row)


@pytest.mark.parametrize("cause", ["missing_working_target", "wrong_remaining_qty", "wrong_exit_quantity", "opposing_local", "malformed_positions", "malformed_orders"])
def test_missing_or_inconsistent_broker_evidence_stays_blocked(case, cause):
    journal, broker, row = case
    held = seed_verified_holding(journal, broker, exited=2 if cause == "wrong_exit_quantity" else 0)
    if cause == "missing_working_target":
        broker.open_orders = []
    elif cause == "wrong_remaining_qty":
        held["parent"]["legs"][1]["qty"] = 8
    elif cause == "wrong_exit_quantity":
        broker.orders["broker_old_tp"]["filled_qty"] = 3
    elif cause == "opposing_local":
        seed_verified_holding(journal, broker, suffix="short", direction="short")
    elif cause == "malformed_positions":
        broker.positions.append({"symbol": "OTHER", "qty": float("nan")})
    else:
        broker.open_orders.append({"symbol": "OTHER"})
    with pytest.raises(ValueError):
        verify_entry_absence(journal, broker, row)


def test_read_only_reopened_ledger_works_without_changing_file_bytes(tmp_path):
    path = str(tmp_path / "readonly.db")
    journal = JournalStore(path)
    row = failed_intent(journal)
    broker = ReadOnlyBroker(journal)
    seed_verified_holding(journal, broker)
    journal.close()
    before = sha256(Path(path).read_bytes()).hexdigest()
    readonly = JournalStore(path, read_only=True)
    broker.journal = readonly
    try:
        assert verify_entry_absence(readonly, broker, row)["attributed_positions"]
    finally:
        readonly.close()
    assert sha256(Path(path).read_bytes()).hexdigest() == before


def test_extra_protective_leg_cannot_be_certified_as_known_lineage(case):
    journal, broker, row = case
    held = seed_verified_holding(journal, broker)
    held["protection"]["legs"].append({**held["protection"]["legs"][0], "role": "unknown", "broker_order_id": "extra"})
    with pytest.raises(ValueError, match="ambiguous leg lineage"):
        verify_entry_absence(journal, broker, row)


def test_target_linked_order_cannot_hide_fill_under_its_other_broker_id(case):
    journal, broker, row = case
    journal.insert("paper_orders", {"order_id": "related", "proposal_id": row["proposal_id"],
                                    "broker_order_id": "related_broker", "symbol": "XYZ"})
    journal.insert("paper_fills", {"fill_id": "target_related", "order_id": "legacy_missing_order", "symbol": "XYZ",
                                  "broker_order_id": "related_broker", "qty": 1})
    with pytest.raises(ValueError, match="local fill evidence"):
        verify_entry_absence(journal, broker, row)
