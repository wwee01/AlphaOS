"""Offline acceptance of Claude's D2/D3/D4/D6 failures and adverse variants."""
from copy import deepcopy

import pytest

from alphaos.execution import ledger_ops, protection_watchdog, time_exit_lifecycle
from alphaos.execution.time_exit_lifecycle import TimeExitLifecycle
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.util import alerts
from conftest import make_settings
from test_execution_lifecycle_repair import restart, setup
from test_time_exit_enforcement import _NOW


@pytest.fixture(autouse=True)
def offline_wait_and_alerts(monkeypatch):
    waits, pages = [], []
    monkeypatch.setattr(time_exit_lifecycle, "sleep", waits.append)
    monkeypatch.setattr(alerts, "send_alert", lambda *args, **kwargs: pages.append(kwargs.get("priority")) or True)
    return waits, pages


def fill_leg(fake, role, qty, *, status="partially_filled", price=112):
    leg = fake.leg("XLV", role)
    leg.status, leg.filled_qty, leg.filled_avg_price = status, qty, price
    leg.filled_at = "2026-07-14T16:59:59Z"
    fake.set_broker_qty("XLV", 10 - qty)
    return leg


def visible_target_only(fake):
    """Actual Alpaca OPEN response: top-level target, no held stop listed."""
    def get_orders():
        target = fake.leg("XLV", "take_profit")
        if target.status in time_exit_lifecycle.TERMINAL:
            return []
        top = deepcopy(target)
        top.order_class = fake._by_symbol["XLV"].order_class
        top.legs = []
        return [top]
    fake.get_orders = get_orders


def reductions(fake, manager):
    captured = []
    original = fake.submit_reduction
    def submit(spec):
        row = manager.journal.one("SELECT * FROM paper_orders WHERE client_order_id=?", (spec["client_order_id"],))
        assert row["qty"] == spec["qty"]
        assert ledger_ops.metadata(row)["phase"] == "submitting"
        captured.append(dict(spec))
        return original(spec)
    fake.submit_reduction = submit
    return captured


@pytest.mark.parametrize("direction", ["long", "short"])
@pytest.mark.parametrize("already_accounted", [False, True])
def test_partial_target_closes_only_residual_with_actual_open_order_shape(tmp_path, direction, already_accounted):
    fake, _, journal, manager, pos, _ = setup(tmp_path, direction=direction)
    tp = fill_leg(fake, "take_profit", 3)
    fake.leg("XLV", "stop_loss").status = "held"
    visible_target_only(fake)
    if already_accounted:
        manager._record_broker_exit(pos, manager.alpaca.get_order(tp.id), "target")
    submitted = reductions(fake, manager)
    result = manager.enforce_time_exits(now=_NOW)
    assert len(result["closed"]) == 1 and result["errors"] == []
    assert [s["qty"] for s in submitted] == [7]
    assert submitted[0]["side"] == ("sell" if direction == "long" else "buy")
    assert journal.one("SELECT qty FROM trade_outcomes")["qty"] == 10
    assert [(r["qty"], r["price"]) for r in journal.query("SELECT * FROM paper_fills ORDER BY id")] == [(3, 112), (7, 100)]
    manager.enforce_time_exits(now=_NOW)
    assert len(submitted) == 1


def test_terminal_partial_stop_recovers_six_shares_then_closes_after_restart(tmp_path):
    fake, settings, journal, manager, _, boid = setup(tmp_path, persistent=True)
    fill_leg(fake, "stop_loss", 4, status="canceled", price=94)
    fake.leg("XLV", "take_profit").status = "canceled"
    result = manager.enforce_time_exits(now=_NOW)
    assert result["deferred"][0]["reprotected"] and result["errors"] == []
    pos = journal.one("SELECT * FROM positions")
    assert pos["qty"] == 6 and pos["broker_order_id"] != boid
    checked = protection_watchdog.check_position(journal, manager.alpaca, pos,
        {"XLV": {"symbol": "XLV", "qty": 6, "side": "long"}}, settings)
    assert checked.protection_status == "protected"
    journal, manager = restart(fake, settings, journal, tmp_path)
    fake.close_fill_price["XLV"] = 100
    submitted = reductions(fake, manager)
    result = manager.enforce_time_exits(now=_NOW)
    assert len(result["closed"]) == 1 and submitted[0]["qty"] == 6
    assert len([op for op, _ in fake.call_log if op == "oco"]) == 1
    assert journal.one("SELECT qty,gross_pnl FROM trade_outcomes") == {"qty": 10, "gross_pnl": -24}


def test_canceling_intent_recovers_terminal_partial_offhours_while_halted(tmp_path):
    fake, settings, journal, manager, pos, _ = setup(tmp_path, persistent=True)
    intent, _ = TimeExitLifecycle(manager)._new(pos, "time_exit", "canceling")
    fill_leg(fake, "stop_loss", 4, status="canceled", price=94)
    fake.leg("XLV", "take_profit").status = "canceled"
    journal, manager = restart(fake, settings, journal, tmp_path)
    manager.kill_switch.engage("stop entries")
    result = manager.enforce_time_exits(now=_NOW.replace(hour=2))
    assert result["deferred"][0]["reprotected"]
    assert journal.one("SELECT qty FROM positions")["qty"] == 6
    assert ledger_ops.metadata(journal.order_by_id(intent["order_id"]))["phase"] == "terminal"
    assert not any(op in ("close", "cancel") for op, _ in fake.call_log)


@pytest.mark.parametrize("partial_qty", [3, 10])
def test_fill_during_cancel_is_booked_before_residual_submission(tmp_path, partial_qty):
    fake, _, journal, manager, _, _ = setup(tmp_path)
    original = fake.cancel_order_by_id
    tp = fake.leg("XLV", "take_profit")
    def cancel(oid):
        original(oid)
        if oid == tp.id:
            fill_leg(fake, "take_profit", partial_qty, status="filled" if partial_qty == 10 else "canceled")
    fake.cancel_order_by_id = cancel
    submitted = reductions(fake, manager)
    result = manager.enforce_time_exits(now=_NOW)
    assert len(result["closed"]) == 1 and result["errors"] == []
    assert [s["qty"] for s in submitted] == ([7] if partial_qty == 3 else [])
    assert journal.one("SELECT SUM(qty) AS qty FROM paper_fills")["qty"] == 10
    assert journal.count_rows("exits") == 1


def test_transient_cancel_finishes_in_same_pass_without_critical_page(tmp_path, offline_wait_and_alerts):
    waits, pages = offline_wait_and_alerts
    fake, _, journal, manager, _, _ = setup(tmp_path)
    tp = fake.leg("XLV", "take_profit")
    fake.cancel_result_status[tp.id] = "pending_cancel"
    original = fake.get_order_by_id
    reads = []
    def get_order(oid):
        if oid == tp.id and tp.status == "pending_cancel":
            reads.append(oid)
            if len(reads) == 2:
                tp.status = "canceled"
        return original(oid)
    fake.get_order_by_id = get_order
    result = manager.enforce_time_exits(now=_NOW)
    assert len(result["closed"]) == 1 and result["errors"] == []
    assert waits == [0.5] and "high" not in pages
    assert not protection_watchdog.has_blocking_incident(journal)


def test_pending_cancel_is_bounded_and_restart_closes_new_partial_residual(tmp_path, offline_wait_and_alerts):
    waits, _ = offline_wait_and_alerts
    fake, settings, journal, manager, _, _ = setup(tmp_path, persistent=True)
    tp = fake.leg("XLV", "take_profit")
    fake.cancel_result_status[tp.id] = "pending_cancel"
    first = manager.enforce_time_exits(now=_NOW)
    assert first["deferred"] == [] and first["errors"][0]["critical"]
    assert waits == [0.5] * 4
    first_cancel = next(i for i, (op, _) in enumerate(fake.call_log) if op == "cancel")
    assert len([1 for op, oid in fake.call_log[first_cancel:] if op == "get_order" and oid == tp.id]) == 5
    assert not any(op in ("close", "oco") for op, _ in fake.call_log)
    journal, manager = restart(fake, settings, journal, tmp_path)
    fill_leg(fake, "take_profit", 3, status="canceled")
    submitted = reductions(fake, manager)
    result = manager.enforce_time_exits(now=_NOW)
    assert len(result["closed"]) == 1 and submitted[0]["qty"] == 7
    assert len([1 for op, oid in fake.call_log if op == "cancel" and oid == tp.id]) == 1
    assert journal.one("SELECT qty FROM trade_outcomes")["qty"] == 10


def test_unknown_leg_read_never_authorizes_close_or_protection(tmp_path):
    fake, _, journal, manager, _, _ = setup(tmp_path)
    tp = fake.leg("XLV", "take_profit")
    original = fake.get_order_by_id
    def unknown(oid):
        if oid == tp.id:
            raise TimeoutError("no authoritative leg read")
        return original(oid)
    fake.get_order_by_id = unknown
    result = manager.enforce_time_exits(now=_NOW)
    assert result["errors"][0]["critical"] and result["deferred"] == []
    assert not any(op in ("close", "oco") for op, _ in fake.call_log)
    assert protection_watchdog.has_blocking_incident(journal)


@pytest.mark.parametrize("field,value", [("broker_order_id", "other-order"), ("symbol", "OTHER"), ("side", "buy")])
def test_wrong_terminal_order_response_never_proves_cancellation(tmp_path, field, value):
    fake, _, journal, manager, _, _ = setup(tmp_path)
    tp = fake.leg("XLV", "take_profit")
    original = manager.alpaca.get_order
    def wrong(oid):
        result = original(oid)
        if oid == tp.id and tp.status == "canceled":
            result[field] = value
        return result
    manager.alpaca.get_order = wrong
    result = manager.enforce_time_exits(now=_NOW)
    assert result["errors"][0]["critical"]
    assert not any(op in ("close", "oco") for op, _ in fake.call_log)
    assert protection_watchdog.has_blocking_incident(journal)


@pytest.mark.parametrize("location,field,value", [
    ("parent", "broker_order_id", "wrong-parent"), ("parent", "symbol", "OTHER"),
    ("parent", "side", "sell"), ("leg", "symbol", "OTHER"),
    ("leg", "side", "buy"), ("leg", "broker_order_id", "wrong-leg"),
    ("leg", "role", "other"),
])
@pytest.mark.parametrize("filled_qty", [0, 4])
def test_recovery_rejects_foreign_lineage_before_any_fill_booking(tmp_path, location, field, value, filled_qty):
    fake, _, journal, manager, pos, boid = setup(tmp_path)
    fill_leg(fake, "stop_loss", filled_qty, status="canceled", price=94)
    fake.leg("XLV", "take_profit").status = "canceled"
    original = manager.alpaca.get_order
    def wrong(oid):
        result = original(oid)
        if oid == boid:
            if location == "parent":
                result[field] = value
            else:
                result["legs"][1][field] = value
        return result
    manager.alpaca.get_order = wrong
    result = TimeExitLifecycle(manager).recover(pos, "terminal partial recovery")
    assert result["critical"] and not result.get("reprotected")
    assert journal.count_rows("paper_fills") == 0
    assert journal.one("SELECT qty FROM positions")["qty"] == 10
    assert not any(op in ("close", "oco", "cancel") for op, _ in fake.call_log)


def test_cancellation_crossing_session_close_restores_protection(tmp_path, monkeypatch):
    fake, _, journal, manager, _, _ = setup(tmp_path)
    now = _NOW.replace(hour=19, minute=59, second=59)
    clocks = iter([0, 2])
    monkeypatch.setattr(time_exit_lifecycle, "monotonic", lambda: next(clocks))
    result = manager.enforce_time_exits(now=now)
    assert result["deferred"][0]["reprotected"] and result["closed"] == []
    assert not any(op == "close" for op, _ in fake.call_log)
    assert journal.one("SELECT qty FROM positions")["qty"] == 10


def test_unchanged_incident_logs_and_pages_once_but_every_check_is_audited(tmp_path, offline_wait_and_alerts):
    _, pages = offline_wait_and_alerts
    fake, _, journal, manager, _, _ = setup(tmp_path)
    tp = fake.leg("XLV", "take_profit")
    fake.cancel_result_status[tp.id] = "pending_cancel"
    results = [manager.enforce_time_exits(now=_NOW)]
    results += [manager.enforce_time_exits(now=_NOW.replace(hour=2)) for _ in range(8)]
    assert all(len(r["errors"]) == 1 and r["deferred"] == [] for r in results)
    assert pages == ["high"]
    assert journal.count_rows("system_events", "category='time_exit_enforcement' AND severity='critical'") == 1
    assert journal.count_rows("protection_checks") == 9


def test_incident_severity_transition_alerts_again(tmp_path, offline_wait_and_alerts):
    _, pages = offline_wait_and_alerts
    _, _, _, manager, pos, _ = setup(tmp_path)
    lifecycle = TimeExitLifecycle(manager)
    first = lifecycle.incident(pos, "cannot verify", unknown=True)
    repeated = lifecycle.incident(pos, "still cannot verify", unknown=True)
    changed = lifecycle.incident(pos, "broker confirmed flat", flat=True)
    assert first["incident_id"] and repeated["incident_id"] is None and changed["incident_id"]
    assert pages == ["high", "high"]


def test_watchdog_interleaving_does_not_repage_unchanged_cancellation(tmp_path, offline_wait_and_alerts):
    _, pages = offline_wait_and_alerts
    fake, settings, journal, manager, _, _ = setup(tmp_path)
    tp = fake.leg("XLV", "take_profit")
    fake.cancel_result_status[tp.id] = "pending_cancel"
    for _ in range(3):
        manager.enforce_time_exits(now=_NOW)
        protection_watchdog.run_watchdog_pass(journal, manager.alpaca, settings)
    assert pages == ["high"]
    assert journal.count_rows("system_events", "category='time_exit_enforcement' AND severity='critical'") == 1
    assert protection_watchdog.has_blocking_incident(journal)


def test_repeat_incidents_preserve_observations_and_clear_after_verified_recovery(tmp_path):
    _, settings, journal, manager, pos, _ = setup(tmp_path)
    lifecycle = TimeExitLifecycle(manager)
    incidents = [lifecycle.incident(pos, f"same unsafe condition, pass {i}", unknown=True) for i in range(3)]
    assert incidents[0]["incident_id"] and all(i["incident_id"] is None for i in incidents[1:])
    observed = journal.query("SELECT check_id,detail FROM protection_checks ORDER BY id")
    assert len(observed) == 3
    assert journal.count_rows("protection_checks", "resolved_at_utc IS NULL") == 1
    report = protection_watchdog.run_watchdog_pass(journal, manager.alpaca, settings)
    assert report["protected"] == 1 and report["open_incident_count"] == 0
    assert not protection_watchdog.has_blocking_incident(journal)
    assert journal.query("SELECT check_id,detail FROM protection_checks ORDER BY id LIMIT 3") == observed


@pytest.mark.parametrize("condition", ["healthy", "unverifiable", "degraded_unproved", "degraded_verified"])
def test_legacy_duplicate_incidents_only_clear_on_verified_stop_protection(tmp_path, condition):
    fake, settings, journal, manager, pos, _ = setup(tmp_path)
    for i in range(3):
        journal.insert("protection_checks", {
            "check_id": f"legacy-{i}", "position_id": pos["position_id"], "symbol": pos["symbol"],
            "protection_status": "unverifiable", "severity": "critical", "detail": f"historical observation {i}",
        })
    original = journal.query("SELECT check_id,detail,created_at_utc FROM protection_checks ORDER BY id")
    if condition == "unverifiable":
        fake.get_order_by_id = lambda oid: (_ for _ in ()).throw(TimeoutError("unknown protection"))
    elif condition.startswith("degraded"):
        fake.leg("XLV", "take_profit").status = "canceled"
        fake.leg("XLV", "stop_loss").status = "held" if condition == "degraded_unproved" else "new"
    result = protection_watchdog.run_watchdog_pass(journal, manager.alpaca, settings)
    should_clear = condition in {"healthy", "degraded_verified"}
    assert bool(protection_watchdog.has_blocking_incident(journal)) is not should_clear
    if should_clear:
        assert result["open_incident_count"] == 0
    assert journal.query("SELECT check_id,detail,created_at_utc FROM protection_checks ORDER BY id LIMIT 3") == original


def test_monitor_reports_execution_errors_and_persists_error_count():
    journal = JournalStore(":memory:")
    orch = Orchestrator(settings=make_settings(), journal=journal)
    critical = {"critical": True, "error": "cancellation incomplete", "position_id": "p"}
    read_error = {"error": "broker read unavailable", "order_id": "o"}
    orch.orders.reconcile = lambda: {"errors": [read_error], "time_exits_errors": [critical]}
    orch.positions.monitor = lambda price_overrides=None: []
    result = orch.run_monitor_once()
    assert result["errors"] == [read_error, critical]
    assert result["time_exits_errors"] == [critical] and result["time_exits_deferred"] == []
    assert journal.one("SELECT error_count FROM scheduler_runs")["error_count"] == 2
    orch.close()
