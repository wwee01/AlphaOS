"""Corrected-behaviour regressions for D1-D6 on 742312f, using the ORIGINAL triggers
from claude-review-probes-*-2026-09-13.py (which pinned the old broken behaviour)."""
import sys, os, time as _time
sys.path.insert(0, os.path.join(os.environ["ALPHAOS_REPO"], "tests"))
from datetime import datetime, timezone
import pytest

from alphaos.execution import protection_watchdog
from alphaos.execution import time_exit_lifecycle as tel
from alphaos.execution.order_manager import OrderManager
from alphaos.broker.alpaca_client import AlpacaClient
from alphaos.journal.journal_store import JournalStore
from alphaos.safety import KillSwitch
from alphaos.util import alerts, timeutils
from test_time_exit_enforcement import (
    FakeTradingClient, _paper_om, _open_broker_position, _opened_date_for, _NOW_DATE, _NOW,
)


@pytest.fixture
def alert_log(monkeypatch):
    calls = []
    monkeypatch.setattr(alerts, "send_alert", lambda *a, **k: calls.append(k.get("priority")) or True)
    return calls


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(tel, "CANCEL_VERIFY_DELAY_SECONDS", 0)


def _setup(tmp_path, persistent=False):
    fake = FakeTradingClient()
    journal = JournalStore(str(tmp_path / "ledger.db") if persistent else ":memory:")
    settings, journal, manager = _paper_om(fake, journal=journal, kill_switch=KillSwitch(str(tmp_path / "KS")))
    pos, boid = _open_broker_position(journal, fake, qty=10, opened_market_date=_opened_date_for(_NOW_DATE, 5))
    return fake, settings, journal, manager, pos, boid


def _sold(journal):
    return journal.one("SELECT COALESCE(SUM(qty),0) q FROM paper_fills WHERE side IN ('sell','buy_to_cover')")["q"]


# ================================================================== D2
def test_d2a_working_partial_take_profit_now_cancels_and_closes_residual(tmp_path, alert_log):
    fake, settings, journal, manager, pos, boid = _setup(tmp_path)
    tp = fake.leg("XLV", "take_profit")
    tp.status, tp.filled_qty, tp.filled_avg_price, tp.filled_at = "partially_filled", 3, 112.0, "2026-07-10T15:00:00Z"
    fake.set_broker_qty("XLV", 7)
    r = manager.enforce_time_exits(now=_NOW)
    assert len(r["closed"]) == 1, r
    closes = [o for o in fake.orders.values() if getattr(o, "order_class", None) == "simple" and o.side == "sell"]
    assert [c.qty for c in closes] == [7]                      # residual only, no oversell
    assert _sold(journal) == 10                                 # 3 (tp partial) + 7 (close)
    out = journal.one("SELECT * FROM trade_outcomes")
    assert out["qty"] == 10 and journal.one("SELECT exit_reason FROM exits")["exit_reason"] == "time_expiry"
    assert journal.one("SELECT status, qty FROM positions")["status"] == "closed"
    assert "high" not in alert_log                 # only the ordinary "closed" notification


def test_d2b_terminal_partial_residual_is_reprotected_then_exited_next_pass(tmp_path, alert_log):
    fake, settings, journal, manager, pos, boid = _setup(tmp_path)
    stop, tp = fake.leg("XLV", "stop_loss"), fake.leg("XLV", "take_profit")
    stop.status, stop.filled_qty, stop.filled_avg_price, stop.filled_at = "canceled", 4, 94.0, "2026-07-10T15:00:00Z"
    tp.status = "canceled"
    fake.set_broker_qty("XLV", 6)
    r1 = manager.enforce_time_exits(now=_NOW)
    assert r1["deferred"] and r1["deferred"][0].get("reprotected") is True, r1
    assert journal.one("SELECT qty FROM positions")["qty"] == 6
    oco = fake._by_symbol["XLV"]; assert oco.order_class == "oco" and oco.qty == 6
    # watchdog now sees it protected
    res = protection_watchdog.check_position(journal, manager.alpaca, journal.one("SELECT * FROM positions"),
                                             {"XLV": {"symbol": "XLV", "qty": 6.0, "side": "long"}}, settings)
    assert res.protection_status == "protected"
    # next due pass: cancel the OCO, close the 6 (fake needs an explicit fill price for an OCO parent)
    fake.close_fill_price["XLV"] = 105
    r2 = manager.enforce_time_exits(now=_NOW)
    assert len(r2["closed"]) == 1, r2
    assert _sold(journal) == 10 and journal.one("SELECT status FROM positions")["status"] == "closed"


def test_d2c_fill_during_cancellation_shrinks_the_close_to_verified_residual(tmp_path, alert_log):
    fake, settings, journal, manager, pos, boid = _setup(tmp_path)
    tp = fake.leg("XLV", "take_profit")
    tp.status, tp.filled_qty, tp.filled_avg_price, tp.filled_at = "partially_filled", 3, 112.0, "2026-07-10T15:00:00Z"
    fake.set_broker_qty("XLV", 7)
    orig_cancel = fake.cancel_order_by_id
    def cancel_with_late_fill(oid):
        if oid == tp.id:                       # 2 more shares fill as the cancel lands
            tp.filled_qty, tp.filled_avg_price = 5, 112.0
            fake.set_broker_qty("XLV", 5)
        orig_cancel(oid)
    fake.cancel_order_by_id = cancel_with_late_fill
    r = manager.enforce_time_exits(now=_NOW)
    assert len(r["closed"]) == 1, r
    closes = [o for o in fake.orders.values() if getattr(o, "order_class", None) == "simple" and o.side == "sell"]
    assert [c.qty for c in closes] == [5]
    assert _sold(journal) == 10
    row = journal.one("SELECT qty, raw_request_json FROM paper_orders WHERE order_type='market' AND client_order_id LIKE 'aos_%'")
    assert row["qty"] == 5                                      # intent shrank to the verified residual


def test_d2d_restart_mid_canceling_completes_without_duplicate(tmp_path, alert_log):
    fake, settings, journal, manager, pos, boid = _setup(tmp_path, persistent=True)
    tp = fake.leg("XLV", "take_profit")
    fake.cancel_result_status[tp.id] = "pending_cancel"
    r1 = manager.enforce_time_exits(now=_NOW)
    assert r1["errors"] and r1["errors"][0]["critical"]
    path = journal.db_path; journal.close()
    journal = JournalStore(path)
    manager = OrderManager(settings, journal, alpaca=AlpacaClient(settings, journal, trading_client=fake),
                           kill_switch=KillSwitch(str(tmp_path / "KS")))
    tp.status = "canceled"
    r2 = manager.enforce_time_exits(now=_NOW)
    assert len(r2["closed"]) == 1
    assert len([op for op, _ in fake.call_log if op == "close"]) == 1
    assert len(journal.query("SELECT * FROM paper_orders WHERE order_type='market'")) == 1


# ================================================================== D3
def test_d3a_short_pending_cancel_transition_closes_in_the_same_pass(tmp_path, alert_log):
    fake, settings, journal, manager, pos, boid = _setup(tmp_path)
    tp = fake.leg("XLV", "take_profit")
    fake.cancel_result_status[tp.id] = "pending_cancel"
    reads = {"n": 0}
    orig_get = fake.get_order_by_id
    def get_with_transition(oid):
        o = orig_get(oid)
        if oid == tp.id and o.status == "pending_cancel":
            reads["n"] += 1
            if reads["n"] >= 2:            # second verification read sees the cancel land
                o.status = "canceled"
        return o
    fake.get_order_by_id = get_with_transition
    r = manager.enforce_time_exits(now=_NOW)
    assert len(r["closed"]) == 1 and r["errors"] == [], r
    assert "high" not in alert_log                 # no incident page; only the "closed" notification
    assert journal.one("SELECT COUNT(*) n FROM protection_checks WHERE protection_status IN ('unprotected','unverifiable')")["n"] == 0


def test_d3b_persistently_pending_cancel_is_a_bounded_blocking_error(tmp_path, alert_log, monkeypatch):
    fake, settings, journal, manager, pos, boid = _setup(tmp_path)
    tp = fake.leg("XLV", "take_profit")
    fake.cancel_result_status[tp.id] = "pending_cancel"
    r = manager.enforce_time_exits(now=_NOW)
    assert r["closed"] == [] and r["deferred"] == [] and len(r["errors"]) == 1 and r["errors"][0]["critical"]
    verify_reads = [c for c in fake.call_log if c[0] == "get_order" and c[1] == tp.id]
    assert len(verify_reads) >= tel.CANCEL_VERIFY_ATTEMPTS            # bounded poll, then stop
    assert not any(op in ("close", "oco") for op, _ in fake.call_log)
    assert alert_log == ["high"]


def test_d3c_real_delay_budget_is_about_two_seconds(tmp_path, alert_log, monkeypatch):
    monkeypatch.setattr(tel, "CANCEL_VERIFY_DELAY_SECONDS", 0.5)
    fake, settings, journal, manager, pos, boid = _setup(tmp_path)
    fake.cancel_result_status[fake.leg("XLV", "take_profit").id] = "pending_cancel"
    t0 = _time.monotonic(); manager.enforce_time_exits(now=_NOW); dt = _time.monotonic() - t0
    assert 1.9 <= dt <= 3.5, dt


# ================================================================== D4
def test_d4a_unchanged_stuck_state_pages_once_across_passes(tmp_path, alert_log):
    fake, settings, journal, manager, pos, boid = _setup(tmp_path)
    fake.cancel_result_status[fake.leg("XLV", "take_profit").id] = "pending_cancel"
    manager.enforce_time_exits(now=_NOW)
    night = _NOW.replace(hour=2)
    for _ in range(8):
        manager.enforce_time_exits(now=night)
    assert alert_log == ["high"]
    crit = journal.query("SELECT * FROM system_events WHERE severity='critical' AND category='time_exit_enforcement'")
    assert len(crit) == 1
    assert len(journal.query("SELECT * FROM protection_checks")) == 9      # every pass still audited


def test_d4b_watchdog_interleave_then_recovery_then_new_incident_pages_again(tmp_path, alert_log):
    fake, settings, journal, manager, pos, boid = _setup(tmp_path)
    tp = fake.leg("XLV", "take_profit")
    fake.cancel_result_status[tp.id] = "pending_cancel"
    manager.enforce_time_exits(now=_NOW)                          # unverifiable, 1 page
    assert alert_log == ["high"]
    # watchdog pass in between: stop is canceled, tp pending -> UNPROTECTED transition
    settings2 = settings
    protection_watchdog.run_watchdog_pass(journal, manager.alpaca, settings2)
    open_inc = journal.query("SELECT protection_status FROM protection_checks WHERE resolved_at_utc IS NULL AND protection_status IN ('unprotected','unverifiable','closed_mismatch') ORDER BY id DESC")
    assert open_inc and open_inc[0]["protection_status"] == "unprotected"
    manager.enforce_time_exits(now=_NOW.replace(hour=2))          # still stuck: maps to the known UNPROTECTED, no new page
    assert alert_log == ["high"]
    # cancel lands; next in-session pass closes -> incident should resolve
    tp.status = "canceled"
    r = manager.enforce_time_exits(now=_NOW)
    assert len(r["closed"]) == 1
    protection_watchdog.run_watchdog_pass(journal, manager.alpaca, settings2)
    assert not protection_watchdog.has_blocking_incident(journal)


# ================================================================== D6
def test_d6_critical_outcomes_are_errors_not_deferrals(tmp_path, alert_log, monkeypatch):
    fake, settings, journal, manager, pos, boid = _setup(tmp_path)
    monkeypatch.setattr(timeutils, "now_utc", lambda: _NOW)
    fake.cancel_result_status[fake.leg("XLV", "take_profit").id] = "pending_cancel"
    r = manager.reconcile()
    assert r["time_exits_deferred"] == [] and len(r["time_exits_errors"]) == 1
    assert r["time_exits_errors"][0]["critical"] is True
