"""Natural asynchronous close receipt regression; fake broker, no network."""
from copy import deepcopy
import json

import pytest

from alphaos.execution import ledger_ops
from alphaos.execution.time_exit_lifecycle import TimeExitLifecycle
from alphaos.journal.journal_store import JournalStore
from alphaos.safety import KillSwitch
from alphaos.util import alerts
from test_execution_lifecycle_repair import complete, restart, setup
from test_time_exit_enforcement import (
    FakeTradingClient, _NOW, _NOW_DATE, _open_broker_position, _opened_date_for, _paper_om,
)


@pytest.fixture(autouse=True)
def no_alerts(monkeypatch):
    monkeypatch.setattr(alerts, "send_alert", lambda *a, **kw: True)


def close_with_receipt(tmp_path, monkeypatch, *, direction="long", persistent=False):
    fake, settings, journal, manager, pos, _ = setup(tmp_path, direction=direction, persistent=persistent)
    fake.close_fill_price["XLV"] = 105
    captured = []
    close = manager.positions.close_position

    def observe(*args, **kwargs):
        result = close(*args, **kwargs)
        row = journal.order_by_id(kwargs["recorded_exit_order_id"])
        captured.append(deepcopy(json.loads(row["raw_response_json"])))
        return result

    monkeypatch.setattr(manager.positions, "close_position", observe)
    result = manager.enforce_time_exits(now=_NOW)
    assert len(result["closed"]) == 1 and not result["errors"]
    row = journal.one("SELECT * FROM paper_orders WHERE order_type='market'")
    return fake, settings, journal, manager, pos, row, captured[0]


@pytest.mark.parametrize("direction", ["long", "short"])
def test_actual_time_close_preserves_exact_accounting_and_time_receipt(tmp_path, monkeypatch, direction):
    fake, _, journal, _, pos, row, at_close = close_with_receipt(tmp_path, monkeypatch, direction=direction)
    response = json.loads(row["raw_response_json"])
    assert response["accounting_receipt"] == at_close["accounting_receipt"]
    assert response["fill_timestamp_source"] == at_close["fill_timestamp_source"] == "broker"
    receipt = response["accounting_receipt"]
    assert receipt["version"] == "trade_close_accounting_v1"
    assert receipt["position_id"] == pos["position_id"]
    assert receipt["close_timestamp_source"] == "broker"
    assert receipt["accounting_recorded_at_utc"]
    assert ledger_ops.metadata(row)["phase"] == "complete"
    assert journal.count_rows("exits") == journal.count_rows("trade_outcomes") == 1
    assert len([x for x in fake.call_log if x[0] == "close"]) == 1


def test_async_restart_and_duplicate_processing_do_not_rewrite_close_receipt(tmp_path):
    fake, settings, journal, manager, _, _ = setup(tmp_path, persistent=True)
    fake.close_fill_price["XLV"] = None
    first = manager.enforce_time_exits(now=_NOW)
    close_id = first["errors"][0]["pending_close_order"]
    journal, manager = restart(fake, settings, journal, tmp_path)
    complete(fake, close_id)
    assert len(manager.enforce_time_exits(now=_NOW)["closed"]) == 1
    row = journal.one("SELECT * FROM paper_orders WHERE broker_order_id=?", (close_id,))
    recorded = json.loads(row["raw_response_json"])["accounting_receipt"]
    journal, manager = restart(fake, settings, journal, tmp_path)
    manager.enforce_time_exits(now=_NOW)
    manager.reconcile()
    after = journal.order_by_id(row["order_id"])
    assert json.loads(after["raw_response_json"])["accounting_receipt"] == recorded
    assert journal.count_rows("paper_fills") == journal.count_rows("trade_outcomes") == journal.count_rows("exits") == 1
    assert len([x for x in fake.call_log if x[0] == "close"]) == 1


def test_fresh_locked_row_wins_over_stale_caller_through_phase_updates(tmp_path, monkeypatch):
    _, settings, journal, manager, _, row, _ = close_with_receipt(tmp_path, monkeypatch, persistent=True)
    receipt = deepcopy(json.loads(row["raw_response_json"])["accounting_receipt"])
    stale = deepcopy(row)
    stale["raw_response_json"] = "{}"
    original_qty = ledger_ops.metadata(row)["original_qty"]
    stale["raw_request_json"] = json.dumps({**ledger_ops.metadata(row), "original_qty": 999})
    norm = manager.alpaca.get_order(row["broker_order_id"])
    lifecycle = TimeExitLifecycle(manager)
    lifecycle._set(stale, "working", norm)
    lifecycle._set(stale, "complete", norm)
    actual = journal.order_by_id(row["order_id"])
    assert json.loads(actual["raw_response_json"])["accounting_receipt"] == receipt
    assert ledger_ops.metadata(actual)["original_qty"] == original_qty
    lifecycle._set(stale, "complete", json.loads(actual["raw_response_json"]))
    assert json.loads(journal.order_by_id(row["order_id"])["raw_response_json"])["accounting_receipt"] == receipt


@pytest.mark.parametrize("mutation", ["null", "list", "unknown_version", "foreign_position", "foreign_exit", "foreign_entry", "wrong_qty", "wrong_net", "nan_cost"])
def test_invalid_existing_receipt_fails_without_row_or_event_changes(tmp_path, monkeypatch, mutation):
    _, _, journal, manager, _, row, _ = close_with_receipt(tmp_path, monkeypatch)
    response = json.loads(row["raw_response_json"])
    receipt = response["accounting_receipt"]
    if mutation == "null":
        response["accounting_receipt"] = None
    elif mutation == "list":
        response["accounting_receipt"] = []
    else:
        key, value = {"unknown_version": ("version", "unknown"), "foreign_position": ("position_id", "foreign"),
            "foreign_exit": ("exit_id", "foreign"), "foreign_entry": ("entry_order_id", "foreign"),
            "wrong_qty": ("qty", 9), "wrong_net": ("net_pnl", 999), "nan_cost": ("modeled_costs", float("nan"))}[mutation]
        receipt[key] = value
    ledger_ops.update(journal, "paper_orders", "order_id", row["order_id"], {"raw_response_json": response})
    journal.conn.commit()
    before = journal.order_by_id(row["order_id"])
    events = journal.query("SELECT * FROM order_events")
    with pytest.raises(ValueError, match="receipt|response"):
        TimeExitLifecycle(manager)._set(row, "working", manager.alpaca.get_order(row["broker_order_id"]))
    assert journal.order_by_id(row["order_id"]) == before
    assert journal.query("SELECT * FROM order_events") == events


@pytest.mark.parametrize("key", ["accounting_receipt", "fill_timestamp_source"])
def test_incoming_reserved_metadata_cannot_replace_local_evidence(tmp_path, monkeypatch, key):
    _, _, journal, manager, _, row, _ = close_with_receipt(tmp_path, monkeypatch)
    incoming = manager.alpaca.get_order(row["broker_order_id"])
    incoming[key] = {"forged": True} if key == "accounting_receipt" else "observed"
    before = journal.order_by_id(row["order_id"])
    with pytest.raises(ValueError, match="local|receipt"):
        TimeExitLifecycle(manager)._set(row, "complete", incoming)
    assert journal.order_by_id(row["order_id"]) == before


@pytest.mark.parametrize("key,value", [("broker_order_id", "foreign"), ("client_order_id", "foreign"),
    ("symbol", "FOREIGN"), ("side", "buy"), ("qty", 99), ("filled_qty", 0), ("filled_avg_price", 1)])
def test_receipt_cannot_be_attached_to_conflicting_broker_identity_or_fill(tmp_path, monkeypatch, key, value):
    _, _, journal, manager, _, row, _ = close_with_receipt(tmp_path, monkeypatch)
    incoming = manager.alpaca.get_order(row["broker_order_id"])
    incoming[key] = value
    before = journal.order_by_id(row["order_id"])
    with pytest.raises(ValueError, match="receipt"):
        TimeExitLifecycle(manager)._set(row, "complete", incoming)
    assert journal.order_by_id(row["order_id"]) == before


@pytest.mark.parametrize("raw", [None, "null", "[]", "legacy invalid json"])
def test_preclose_legacy_nonobject_is_not_fabricated_into_receipt(tmp_path, raw):
    _, _, journal, manager, pos, _ = setup(tmp_path)
    lifecycle = TimeExitLifecycle(manager)
    row, _ = lifecycle._new(pos, "time_exit", "canceling")
    ledger_ops.update(journal, "paper_orders", "order_id", row["order_id"], {"raw_response_json": raw})
    journal.conn.commit()
    norm = {"broker_order_id": "close", "state": "accepted"}
    lifecycle._set(row, "working", norm)
    assert json.loads(journal.order_by_id(row["order_id"])["raw_response_json"]) == norm
    incoming = {**norm, "accounting_receipt": {"version": "trade_close_accounting_v1"}}
    with pytest.raises(ValueError, match="local|receipt"):
        lifecycle._set(row, "complete", incoming)


def test_absent_historical_receipt_stays_absent(tmp_path, monkeypatch):
    _, _, journal, manager, _, row, _ = close_with_receipt(tmp_path, monkeypatch)
    norm = manager.alpaca.get_order(row["broker_order_id"])
    ledger_ops.update(journal, "paper_orders", "order_id", row["order_id"], {"raw_response_json": norm})
    journal.conn.commit()
    TimeExitLifecycle(manager)._set(row, "complete", norm)
    assert json.loads(journal.order_by_id(row["order_id"])["raw_response_json"]) == norm


@pytest.mark.parametrize("first,total", [(0.245981, 4.284099), (0.480726, 2.52748), (0.054701, 1.537961)])
def test_actual_fractional_partial_then_final_close_uses_existing_cumulative_tolerance(tmp_path, first, total):
    fake = FakeTradingClient()
    journal = JournalStore(":memory:")
    _, journal, manager = _paper_om(
        fake, journal=journal, kill_switch=KillSwitch(str(tmp_path / "KILL_SWITCH")),
    )
    pos, _ = _open_broker_position(
        journal, fake, qty=total, opened_market_date=_opened_date_for(_NOW_DATE, 5),
    )
    fake.close_fill_price["XLV"] = 105
    fake.close_fill_qty["XLV"] = first
    partial = manager.enforce_time_exits(now=_NOW)
    assert not partial["closed"] and partial["errors"]
    row = journal.one("SELECT * FROM paper_orders WHERE order_type='market'")
    complete(fake, row["broker_order_id"], qty=total, price=105)
    final = manager.enforce_time_exits(now=_NOW)
    booked = journal.one("SELECT SUM(qty) qty FROM paper_fills WHERE order_id=?", (row["order_id"],))["qty"]
    assert booked != total and abs(booked - total) < 1e-12
    assert len(final["closed"]) == 1 and not final["errors"]
    saved = journal.order_by_id(row["order_id"])
    assert ledger_ops.metadata(saved)["phase"] == "complete"
    assert json.loads(saved["raw_response_json"])["accounting_receipt"]["position_id"] == pos["position_id"]
    assert journal.count_rows("exits") == journal.count_rows("trade_outcomes") == 1
    assert journal.count_rows("paper_fills") == 2
    assert len([item for item in fake.call_log if item[0] == "close"]) == 1


@pytest.mark.parametrize("field", ["qty", "filled_qty", "filled_avg_price"])
@pytest.mark.parametrize("value", [True, "10", None, float("nan"), float("inf")])
def test_receipt_snapshot_numbers_must_be_finite_numeric_not_boolean(tmp_path, monkeypatch, field, value):
    _, _, journal, manager, _, row, _ = close_with_receipt(tmp_path, monkeypatch)
    norm = manager.alpaca.get_order(row["broker_order_id"])
    norm[field] = value
    before = journal.order_by_id(row["order_id"])
    events = journal.query("SELECT * FROM order_events")
    with pytest.raises(ValueError, match="receipt"):
        TimeExitLifecycle(manager)._set(row, "complete", norm)
    assert journal.order_by_id(row["order_id"]) == before
    assert journal.query("SELECT * FROM order_events") == events
