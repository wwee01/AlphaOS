"""Regression evidence for the September audit; SDK-shaped, offline, multi-pass."""
from copy import deepcopy
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from alphaos.broker.alpaca_client import AlpacaClient, AlpacaSafetyError
from alphaos.execution import ledger_ops, protection_watchdog
from alphaos.execution.order_manager import OrderManager
from alphaos.execution.time_exit_lifecycle import TimeExitLifecycle
from alphaos.journal.journal_store import JournalStore
from alphaos.safety import KillSwitch
from test_time_exit_enforcement import (
    FakeTradingClient, _paper_om, _open_broker_position, _opened_date_for, _NOW_DATE, _NOW,
)


def setup(tmp_path, *, persistent=False, direction="long"):
    fake = FakeTradingClient()
    journal = JournalStore(str(tmp_path / "ledger.db") if persistent else ":memory:")
    settings, journal, manager = _paper_om(fake, journal=journal,
                                          kill_switch=KillSwitch(str(tmp_path / "KILL_SWITCH")))
    pos, boid = _open_broker_position(journal, fake, qty=10, direction=direction,
                                     opened_market_date=_opened_date_for(_NOW_DATE, 5))
    return fake, settings, journal, manager, pos, boid


def restart(fake, settings, journal, tmp_path):
    path = journal.db_path
    journal.close()
    journal = JournalStore(path)
    manager = OrderManager(settings, journal, alpaca=AlpacaClient(settings, journal, trading_client=fake),
                           kill_switch=KillSwitch(str(tmp_path / "KILL_SWITCH")))
    return journal, manager


def complete(fake, order_id, qty=10, price=106):
    order = fake.orders[order_id]
    order.status, order.filled_qty, order.filled_avg_price = "filled", qty, price
    order.filled_at = "2026-07-14T17:00:02Z"
    fake.set_broker_qty(order.symbol, 0)


def add_entry(journal, pos, boid):
    journal.insert("paper_orders", {
        "order_id": pos["order_id"], "symbol": pos["symbol"], "direction": pos["direction"],
        "side": "sell_short" if pos["direction"] == "short" else "buy", "qty": 10,
        "broker_order_id": boid, "order_type": "bracket", "state": "accepted",
        "entry_price": 100, "take_profit_price": 112, "stop_loss_price": 94,
        "execution_source": "alpaca_paper", "data_provider": "alpaca", "data_feed": "iex",
    })


def test_sdk_request_oco_requires_nested_take_profit_and_parent_shape(tmp_path):
    fake, settings, journal, manager, pos, _ = setup(tmp_path)
    captured = []

    class SDKRecorder:
        def submit_order(self, request):
            captured.append(request.model_dump(exclude_none=True))
            return fake.submit_oco({"symbol": "XLV", "qty": 10, "side": "sell", "target": 112,
                                    "stop": 94, "tif": "gtc", "client_order_id": request.client_order_id})

    client = AlpacaClient(settings, journal, trading_client=SDKRecorder())
    normalized = client.submit_protective_oco("XLV", 10, "long", 94, 112, "gtc", "stable-id")
    assert captured[0]["take_profit"]["limit_price"] == 112
    assert captured[0]["stop_loss"]["stop_price"] == 94
    assert captured[0]["client_order_id"] == "stable-id"
    assert {leg["role"] for leg in normalized["legs"]} == {"take_profit", "stop_loss"}
    parent = next(leg for leg in normalized["legs"] if leg["role"] == "take_profit")
    assert parent["broker_order_id"] == normalized["broker_order_id"]


def test_sdk_reduction_is_exact_quantity_and_has_stable_id(tmp_path):
    fake, settings, journal, _, _, boid = setup(tmp_path)
    captured = []
    class Recorder:
        def submit_order(self, request):
            captured.append(request.model_dump(exclude_none=True))
            return fake.orders[boid]
    client = AlpacaClient(settings, journal, trading_client=Recorder())
    client.submit_position_reduction("XLV", 4, "short", "close-id")
    assert captured[0]["qty"] == 4
    assert captured[0]["side"] == "buy"
    assert captured[0]["client_order_id"] == "close-id"


def test_partial_entry_and_completion_preserve_quantity_vwap_and_broker_time(tmp_path):
    fake, _, journal, manager, pos, boid = setup(tmp_path)
    add_entry(journal, pos, boid)
    journal.conn.execute("DELETE FROM positions")
    journal.conn.commit()
    order = fake.orders[boid]
    order.status, order.filled_qty, order.filled_avg_price = "partially_filled", 4, 100
    order.filled_at = "2026-07-14T16:00:00Z"
    fake.set_broker_qty("XLV", 4)
    manager.settings = deepcopy(manager.settings)
    object.__setattr__(manager.settings, "time_exit_enforcement_enabled", False)
    first = manager.reconcile()
    assert len(first["stale_partial_fill_alerts"]) == 1
    assert journal.one("SELECT * FROM positions")["qty"] == 4
    order.status, order.filled_qty, order.filled_avg_price = "filled", 10, 101.2
    order.filled_at = "2026-07-14T16:01:00Z"
    fake.set_broker_qty("XLV", 10)
    manager.reconcile()
    manager.reconcile()
    p = journal.one("SELECT * FROM positions")
    assert p["qty"] == 10 and p["avg_entry_price"] == 101.2
    fills = journal.query("SELECT * FROM paper_fills ORDER BY id")
    assert [(f["qty"], f["price"]) for f in fills] == [(4, 100), (6, 102)]
    assert fills[0]["filled_at"] == "2026-07-14T16:00:00Z"
    assert fills[1]["filled_at"] == "2026-07-14T16:01:00Z"


def test_async_close_persists_across_restart_and_disabled_flag(tmp_path):
    fake, settings, journal, manager, pos, _ = setup(tmp_path, persistent=True)
    fake.close_fill_price["XLV"] = None
    first = manager.enforce_time_exits(now=_NOW)
    close_id = first["deferred"][0]["pending_close_order"]
    journal, manager = restart(fake, settings, journal, tmp_path)
    object.__setattr__(settings, "time_exit_enforcement_enabled", False)
    complete(fake, close_id)
    result = manager.reconcile()
    assert len(result["time_exits_closed"]) == 1
    manager.reconcile()
    assert len([op for op, _ in fake.call_log if op == "close"]) == 1
    assert journal.one("SELECT * FROM positions")["status"] == "closed"
    outcome = journal.one("SELECT * FROM trade_outcomes")
    assert outcome["qty"] == 10 and outcome["gross_pnl"] == 60
    assert outcome["created_at_utc"] == "2026-07-14T17:00:02+00:00"
    assert len(journal.query("SELECT * FROM exits")) == 1


def test_partial_liquidation_accumulates_after_restart_without_duplicate_order(tmp_path):
    fake, settings, journal, manager, pos, _ = setup(tmp_path, persistent=True)
    fake.close_fill_qty["XLV"], fake.close_fill_price["XLV"] = 6, 105
    first = manager.enforce_time_exits(now=_NOW)
    close_id = first["deferred"][0]["pending_close_order"]
    assert journal.one("SELECT * FROM positions")["qty"] == 4
    manager.enforce_time_exits(now=_NOW)
    assert len(journal.query("SELECT * FROM paper_fills")) == 1
    journal, manager = restart(fake, settings, journal, tmp_path)
    complete(fake, close_id)
    manager.enforce_time_exits(now=_NOW)
    assert len([op for op, _ in fake.call_log if op == "close"]) == 1
    fills = journal.query("SELECT * FROM paper_fills ORDER BY id")
    assert [(f["qty"], f["price"]) for f in fills] == [(6, 105), (4, 107.5)]
    assert journal.one("SELECT * FROM trade_outcomes")["gross_pnl"] == 60


def test_canceled_partial_close_restores_only_verified_residual(tmp_path):
    fake, _, journal, manager, _, _ = setup(tmp_path)
    fake.close_fill_qty["XLV"], fake.close_fill_price["XLV"] = 6, 105
    first = manager.enforce_time_exits(now=_NOW)
    fake.orders[first["deferred"][0]["pending_close_order"]].status = "canceled"
    result = manager.enforce_time_exits(now=_NOW)
    assert result["deferred"][0]["reprotected"] is True
    assert fake._by_symbol["XLV"].qty == 4
    assert journal.one("SELECT * FROM positions")["qty"] == 4


def test_accept_then_transport_error_is_recovered_by_client_id(tmp_path):
    fake, _, journal, manager, _, _ = setup(tmp_path)
    original = fake.submit_reduction
    def ambiguous(spec):
        original(spec)
        raise TimeoutError("response lost after accept")
    fake.submit_reduction = ambiguous
    result = manager.enforce_time_exits(now=_NOW)
    assert len(result["closed"]) == 1
    assert len([op for op, _ in fake.call_log if op == "close"]) == 1
    assert not any(op == "oco" for op, _ in fake.call_log)


@pytest.mark.parametrize("accepted", [False, True])
def test_process_crash_during_submit_recovers_durable_intent(tmp_path, accepted):
    fake, settings, journal, manager, _, _ = setup(tmp_path, persistent=True)
    original = fake.submit_reduction
    def crash(spec):
        if accepted:
            original(spec)
        raise SystemExit("process ended during submission")
    fake.submit_reduction = crash
    with pytest.raises(SystemExit):
        manager.enforce_time_exits(now=_NOW)
    assert any(ledger_ops.metadata(o).get("phase") == "submitting"
               for o in journal.query("SELECT * FROM paper_orders"))
    fake.submit_reduction = original
    journal, manager = restart(fake, settings, journal, tmp_path)
    result = manager.enforce_time_exits(now=_NOW)
    if accepted:
        assert len(result["closed"]) == 1
    else:
        assert result["deferred"][0]["reprotected"] is True
    assert len([op for op, _ in fake.call_log if op == "close"]) == int(accepted)


def test_unknown_acceptance_blocks_oco_and_duplicate_close(tmp_path):
    fake, _, journal, manager, _, _ = setup(tmp_path)
    fake.submit_reduction = lambda spec: (_ for _ in ()).throw(TimeoutError("unknown accept"))
    fake.get_order_by_client_id = lambda cid: (_ for _ in ()).throw(TimeoutError("unknown lookup"))
    manager.enforce_time_exits(now=_NOW)
    manager.enforce_time_exits(now=_NOW)
    assert not any(op == "oco" for op, _ in fake.call_log)
    assert protection_watchdog.has_blocking_incident(journal)


def test_asymmetric_cancel_is_blocking_and_retries_without_false_protection(tmp_path):
    fake, _, journal, manager, _, _ = setup(tmp_path)
    tp = fake.leg("XLV", "take_profit")
    fake.cancel_result_status[tp.id] = "pending_cancel"
    first = manager.enforce_time_exits(now=_NOW)
    assert first["deferred"][0]["critical"]
    assert fake.leg("XLV", "stop_loss").status == "canceled"
    assert not any(op == "close" for op, _ in fake.call_log)
    assert protection_watchdog.has_blocking_incident(journal)
    tp.status = "canceled"
    fake.cancel_result_status.clear()
    assert len(manager.enforce_time_exits(now=_NOW)["closed"]) == 1


def test_halted_recovery_refuses_changed_quantity(tmp_path):
    fake, _, journal, manager, _, _ = setup(tmp_path)
    manager.kill_switch.engage("no new risk")
    def failed(spec):
        fake.set_broker_qty("XLV", 8)
        raise RuntimeError("position changed")
    fake.submit_reduction = failed
    manager.enforce_time_exits(now=_NOW)
    assert not any(op == "oco" for op, _ in fake.call_log)
    assert protection_watchdog.has_blocking_incident(journal)


def test_foreign_live_order_prevents_protection_cancellation(tmp_path):
    fake, _, journal, manager, _, _ = setup(tmp_path)
    manager.alpaca.list_open_orders = lambda: [{"broker_order_id": "foreign", "symbol": "XLV", "status": "new"}]
    manager.enforce_time_exits(now=_NOW)
    assert not any(op in ("cancel", "close", "oco") for op, _ in fake.call_log)
    assert protection_watchdog.has_blocking_incident(journal)


def test_overnight_does_not_begin_removing_protection(tmp_path):
    fake, _, _, manager, _, _ = setup(tmp_path)
    assert manager.enforce_time_exits(now=_NOW.replace(hour=5))["closed"] == []
    assert not any(op in ("cancel", "close", "oco") for op, _ in fake.call_log)


def test_saved_canceling_intent_cannot_resume_cancellation_overnight(tmp_path):
    fake, settings, journal, manager, pos, _ = setup(tmp_path, persistent=True)
    TimeExitLifecycle(manager)._new(pos, "time_exit", "canceling")
    journal, manager = restart(fake, settings, journal, tmp_path)
    manager.enforce_time_exits(now=_NOW.replace(hour=5))
    assert not any(op in ("cancel", "close", "oco") for op, _ in fake.call_log)
    assert protection_watchdog.has_blocking_incident(journal)


def test_broker_early_close_calendar_overrides_regular_clock(tmp_path):
    fake, _, _, manager, _, _ = setup(tmp_path)
    manager.alpaca.get_calendar = lambda start, end: [
        {"date": start.isoformat(), "open": "09:30:00", "close": "13:00:00"}]
    result = manager.enforce_time_exits(now=_NOW)  # 17:00UTC = 13:00ET
    assert result["deferred"][0]["session_closed"]
    assert not any(op in ("cancel", "close", "oco") for op, _ in fake.call_log)


def test_position_change_during_order_read_prevents_cancellation(tmp_path):
    fake, _, journal, manager, _, _ = setup(tmp_path)
    def changed():
        fake.set_broker_qty("XLV", 8)
        return []
    manager.alpaca.list_open_orders = changed
    manager.enforce_time_exits(now=_NOW)
    assert not any(op in ("cancel", "close", "oco") for op, _ in fake.call_log)
    assert protection_watchdog.has_blocking_incident(journal)


@pytest.mark.parametrize("field,value", [("qty", 9), ("side", "buy"), ("symbol", "OTHER"),
                                         ("status", "pending_cancel")])
def test_recovery_requires_each_leg_quantity_identity_and_live_raw_status(tmp_path, field, value):
    fake, _, journal, manager, _, _ = setup(tmp_path)
    original = fake.submit_oco
    def malformed(spec):
        order = original(spec)
        setattr(order.legs[0], field, value)
        return order
    fake.submit_oco = malformed
    fake.raise_on_close_for.add("XLV")
    first = manager.enforce_time_exits(now=_NOW)
    second = manager.enforce_time_exits(now=_NOW)
    assert not any(o.get("reprotected") for result in (first, second) for o in result["deferred"])
    assert protection_watchdog.has_blocking_incident(journal)
    assert len([op for op, _ in fake.call_log if op == "oco"]) == 1


def test_watchdog_quantity_mismatch_and_partial_entry_block_new_risk(tmp_path):
    fake, settings, journal, manager, pos, boid = setup(tmp_path)
    protection_watchdog.run_watchdog_pass(journal, manager.alpaca, settings)
    assert not protection_watchdog.has_blocking_incident(journal)
    fake.set_broker_qty("XLV", 8)
    protection_watchdog.run_watchdog_pass(journal, manager.alpaca, settings)
    assert protection_watchdog.has_blocking_incident(journal)["protection_status"] == "unverifiable"
    fake.set_broker_qty("XLV", 10)
    fake.orders[boid].status, fake.orders[boid].filled_qty = "partially_filled", 10
    fake.orders[boid].qty = 20
    protection_watchdog.run_watchdog_pass(journal, manager.alpaca, settings)
    assert protection_watchdog.has_blocking_incident(journal)["protection_status"] == "unprotected"


def test_failed_outcome_write_rolls_back_exit_and_retries_once(tmp_path):
    fake, _, journal, manager, _, _ = setup(tmp_path)
    real = journal.conn
    class FailOutcome:
        def execute(self, sql, *args, **kwargs):
            if sql.startswith("INSERT INTO trade_outcomes"):
                raise RuntimeError("disk failure")
            return real.execute(sql, *args, **kwargs)
        def __getattr__(self, name):
            return getattr(real, name)
    journal.conn = FailOutcome()
    result = manager.enforce_time_exits(now=_NOW)
    assert result["errors"][0]["closed_mismatch"]
    assert not journal.query("SELECT * FROM exits")
    assert journal.one("SELECT * FROM positions")["status"] == "open"
    journal.conn = real
    assert len(manager.enforce_time_exits(now=_NOW)["closed"]) == 1
    assert len(journal.query("SELECT * FROM exits")) == 1
    assert len([op for op, _ in fake.call_log if op == "close"]) == 1


def test_read_only_account_and_history_preserve_missing_fields(tmp_path):
    _, settings, journal, _, _, _ = setup(tmp_path)
    from pydantic import BaseModel, Field
    class History(BaseModel):
        equity: list[float]
        cashflow: dict = Field(default_factory=dict)
    class Recorder:
        def get_account(self):
            return {"equity": "100", "last_equity": "99"}
        def get_portfolio_history(self, request):
            return History(equity=[100])
        def get_calendar(self, request):
            return [SimpleNamespace(date=date(2026, 7, 14), open=datetime(2026, 7, 14, 9, 30),
                                    close=datetime(2026, 7, 14, 16))]
        def get_orders(self, request):
            assert request.limit == 500 and request.nested is True and request.status == "open"
            return []
    client = AlpacaClient(settings, journal, trading_client=Recorder())
    account = client.get_account()
    assert account["equity"] == 100 and account["last_equity"] == 99 and account["cash"] is None
    assert "cashflow" not in client.get_portfolio_history(period="7D", timeframe="1D", cashflow_types="ALL",
                                                          end=datetime(2026, 7, 14, tzinfo=timezone.utc))
    assert client.get_calendar(date(2026, 7, 14), date(2026, 7, 14))[0]["open"] == "2026-07-14T09:30:00"
    assert client.list_open_orders() == []
    client._client.get_orders = lambda request: [object()] * 500
    with pytest.raises(AlpacaSafetyError):
        client.list_open_orders()
