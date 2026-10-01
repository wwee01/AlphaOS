"""Real SQLite/process death and fake broker regression; no network or services."""

from __future__ import annotations

import json
import multiprocessing
import os
from types import SimpleNamespace

import pytest

from alphaos.execution import ledger_ops
from alphaos.execution.position_manager import PositionManager
from alphaos.execution.time_exit_lifecycle import (
    TimeExitLifecycle,
    CLOSE_CONFIRM_ATTEMPTS,
)
from alphaos.journal.journal_store import JournalStore
from alphaos.scheduler.job_runner import JobRunner, _JOB_FUNCS
from alphaos.scheduler.monitor_health import monitor_health
from alphaos.scheduler.monitor_recovery import (
    record_started,
    recover_interrupted_monitor,
)
from alphaos.scheduler.ownership import current_ownership, monitor_ownership
from alphaos.util import alerts, timeutils
from conftest import make_settings
from test_execution_lifecycle_repair import setup, restart, complete
from test_time_exit_enforcement import _NOW


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(alerts, "send_alert", lambda *a, **kw: True)
    monkeypatch.setattr("alphaos.execution.time_exit_lifecycle.sleep", lambda _: None)


def counts(journal):
    return {
        t: journal.count_rows(t)
        for t in ("paper_fills", "exits", "trade_outcomes", "trade_learning_records")
    }


def test_phase_failure_rolls_back_economic_close_then_recovers_once(
    tmp_path, monkeypatch
):
    fake, settings, journal, manager, pos, _ = setup(tmp_path, persistent=True)
    original = TimeExitLifecycle._set

    def fail(self, row, phase, *args, **kw):
        if phase == "complete":
            assert self.journal.position_by_id(pos["position_id"])["status"] == "closed"
            raise RuntimeError("crash before transaction commit")
        return original(self, row, phase, *args, **kw)

    monkeypatch.setattr(TimeExitLifecycle, "_set", fail)
    assert manager.enforce_time_exits(now=_NOW)["errors"]
    assert journal.position_by_id(pos["position_id"])["status"] == "open"
    assert counts(journal) == {
        "paper_fills": 1,
        "exits": 0,
        "trade_outcomes": 0,
        "trade_learning_records": 0,
    }
    row = journal.one("SELECT * FROM paper_orders WHERE order_type='market'")
    assert "accounting_receipt" not in json.loads(row["raw_response_json"])
    monkeypatch.setattr(TimeExitLifecycle, "_set", original)
    journal, manager = restart(fake, settings, journal, tmp_path)
    assert len(manager.enforce_time_exits(now=_NOW)["closed"]) == 1
    manager.enforce_time_exits(now=_NOW)
    assert (
        ledger_ops.metadata(journal.order_by_id(row["order_id"]))["phase"] == "complete"
    )
    assert counts(journal)["exits"] == counts(journal)["trade_outcomes"] == 1
    assert sum(op == "close" for op, _ in fake.call_log) == 1


def test_exception_after_close_return_has_already_committed_phase(
    tmp_path, monkeypatch
):
    fake, _, journal, manager, _, _ = setup(tmp_path)
    original = PositionManager.close_position

    def fail_after(self, *args, **kw):
        result = original(self, *args, **kw)
        assert result
        assert (
            ledger_ops.metadata(self.journal.order_by_id(kw["recorded_exit_order_id"]))[
                "phase"
            ]
            == "complete"
        )
        raise RuntimeError("after committed close")

    monkeypatch.setattr(PositionManager, "close_position", fail_after)
    assert manager.enforce_time_exits(now=_NOW)["errors"]
    assert journal.one("SELECT status FROM positions")["status"] == "closed"
    before = counts(journal)
    monkeypatch.setattr(PositionManager, "close_position", original)
    assert not manager.enforce_time_exits(now=_NOW)["errors"]
    assert counts(journal) == before
    assert sum(op == "close" for op, _ in fake.call_log) == 1


def legacy_working(tmp_path):
    fake, settings, journal, manager, pos, _ = setup(tmp_path, persistent=True)
    assert manager.enforce_time_exits(now=_NOW)["closed"]
    row = journal.one("SELECT * FROM paper_orders WHERE order_type='market'")
    with ledger_ops.atomic(journal) as tx:
        ledger_ops.update(
            tx,
            "paper_orders",
            "order_id",
            row["order_id"],
            {"raw_request_json": {**ledger_ops.metadata(row), "phase": "working"}},
        )
    return fake, settings, journal, manager, pos, row


def test_existing_closed_intent_stays_gated_unchanged_after_restart(tmp_path):
    fake, settings, journal, manager, _, row = legacy_working(tmp_path)
    tables = ("paper_orders", "paper_fills", "exits", "trade_outcomes", "order_events")
    before = {t: journal.query(f"SELECT * FROM {t}") for t in tables}
    assert monitor_health(journal, settings)["unfinished_closed_intents"] == [
        row["order_id"]
    ]
    journal, manager = restart(fake, settings, journal, tmp_path)
    result = manager.enforce_time_exits(now=_NOW)
    assert not result["closed"]
    assert "reviewed recovery required" in result["errors"][0]["error"]
    assert {t: journal.query(f"SELECT * FROM {t}") for t in tables} == before
    assert not monitor_health(journal, settings)["entry_ready"]
    assert sum(op == "close" for op, _ in fake.call_log) == 1


def test_bounded_confirmation_collects_delayed_cumulative_fill_in_same_pass(tmp_path):
    fake, _, journal, manager, _, _ = setup(tmp_path)
    fake.close_fill_price["XLV"] = None
    original = fake.get_order_by_id
    observed = []

    def get_order(oid):
        assert not journal.conn.in_transaction
        value = original(oid)
        if getattr(value, "order_class", None) == "simple":
            observed.append(oid)
            if len(observed) == 1:
                value.status, value.filled_qty, value.filled_avg_price = (
                    "partially_filled",
                    4,
                    104,
                )
                fake.set_broker_qty("XLV", 6)
            else:
                complete(fake, oid, price=106)
        return value

    fake.get_order_by_id = get_order
    result = manager.enforce_time_exits(now=_NOW)
    assert len(result["closed"]) == 1 and not result["errors"]
    assert len(observed) == 2
    assert journal.one("SELECT qty,price FROM paper_fills") == {"qty": 10, "price": 106}
    assert journal.one("SELECT gross_pnl FROM trade_outcomes")["gross_pnl"] == 60
    assert sum(op == "close" for op, _ in fake.call_log) == 1


def test_confirmation_budget_exhaustion_retains_pending_and_does_not_repost(tmp_path):
    fake, _, journal, manager, _, _ = setup(tmp_path)
    fake.close_fill_price["XLV"] = None
    result = manager.enforce_time_exits(now=_NOW)
    oid = result["errors"][0]["pending_close_order"]
    assert (
        sum(op == "get_order" and arg == oid for op, arg in fake.call_log)
        == CLOSE_CONFIRM_ATTEMPTS
    )
    manager.enforce_time_exits(now=_NOW)
    assert counts(journal)["paper_fills"] == 0
    assert sum(op == "close" for op, _ in fake.call_log) == 1


def orch(journal):
    return SimpleNamespace(
        journal=journal, settings=make_settings(ALPHAOS_DB_PATH=journal.db_path)
    )


def start_pass(journal):
    parent = recover_interrupted_monitor(journal, allow_current_job=True)
    st = timeutils.stamp()
    with ledger_ops.atomic(journal) as tx:
        rowid = tx.insert(
            "scheduler_runs",
            {
                "scheduler_run_id": "test-pass",
                "run_type": "monitor",
                "trigger_source": "scheduler",
                "started_at_utc": st.utc,
                "started_at_sgt": st.local_sgt,
                "status": "started",
            },
        )
        record_started(
            tx,
            "scheduler_runs",
            tx.one("SELECT * FROM scheduler_runs WHERE id=?", (rowid,)),
            parent_job_id=parent,
        )


def die_with_started_rows(path):
    journal = JournalStore(path)
    alerts.send_alert = lambda *a, **k: True

    def body(*_):
        start_pass(journal)
        os._exit(71)

    _JOB_FUNCS["monitor"] = body
    JobRunner(orch(journal), role="monitor").run_job(
        "monitor", lock_key="dead", allow_monitor_recovery=False
    )


def test_real_process_death_recovers_both_rows_without_age_delay(tmp_path, monkeypatch):
    path = str(tmp_path / "ledger.db")
    JournalStore(path).close()
    child = multiprocessing.get_context("spawn").Process(
        target=die_with_started_rows, args=(path,)
    )
    child.start()
    child.join(10)
    try:
        assert child.exitcode == 71
    finally:
        if child.is_alive():
            child.kill()
            child.join()
    journal = JournalStore(path)
    assert journal.one("SELECT status FROM job_runs")["status"] == "started"
    assert journal.one("SELECT status FROM scheduler_runs")["status"] == "started"
    monkeypatch.setitem(_JOB_FUNCS, "monitor", lambda *_: {"status": "completed"})
    result = JobRunner(orch(journal), role="monitor").run_job(
        "monitor", lock_key="fresh", allow_monitor_recovery=False
    )
    assert result["status"] == "completed"
    assert [
        r["status"] for r in journal.query("SELECT status FROM job_runs ORDER BY id")
    ] == ["failed", "completed"]
    assert journal.one("SELECT status FROM scheduler_runs")["status"] == "failed"
    assert (
        len(
            journal.query(
                "SELECT * FROM system_events WHERE category='monitor_interrupted_recovery'"
            )
        )
        == 2
    )


def test_reentrant_current_epoch_cannot_recover_started_job_or_pass(
    tmp_path, monkeypatch
):
    journal = JournalStore(str(tmp_path / "ledger.db"))
    runner = JobRunner(orch(journal))
    nested = []

    def body(*_):
        start_pass(journal)
        nested.append(runner.run_job("monitor", lock_key="nested"))
        assert journal.one("SELECT status FROM job_runs")["status"] == "started"
        assert journal.one("SELECT status FROM scheduler_runs")["status"] == "started"
        return {"status": "completed"}

    monkeypatch.setitem(_JOB_FUNCS, "monitor", body)
    assert runner.run_job("monitor", lock_key="first")["status"] == "completed"
    assert nested[0]["status"] == "blocked"


def test_job_and_pass_recovery_is_one_atomic_transaction(tmp_path, monkeypatch):
    journal = JournalStore(str(tmp_path / "ledger.db"))

    def fail(*_):
        start_pass(journal)
        raise SystemExit("gone")

    monkeypatch.setitem(_JOB_FUNCS, "monitor", fail)
    with pytest.raises(SystemExit):
        JobRunner(orch(journal)).run_job("monitor")
    before = {
        t: journal.query(f"SELECT * FROM {t}")
        for t in ("job_runs", "scheduler_runs", "system_events")
    }
    log = JournalStore.log_system_event
    calls = []

    def fail_second(self, severity, category, *args, **kw):
        if category == "monitor_interrupted_recovery":
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("second row fault")
        return log(self, severity, category, *args, **kw)

    monkeypatch.setattr(JournalStore, "log_system_event", fail_second)
    with monitor_ownership(journal) as acquired:
        assert acquired
        with pytest.raises(RuntimeError):
            recover_interrupted_monitor(journal)
    assert {t: journal.query(f"SELECT * FROM {t}") for t in before} == before


def test_replaced_lock_inode_never_authorizes_old_owner(tmp_path):
    journal = JournalStore(str(tmp_path / "ledger.db"))
    with monitor_ownership(journal) as acquired:
        assert acquired and current_ownership(journal)
        os.unlink(journal.db_path + ".monitor.lock")
        with open(journal.db_path + ".monitor.lock", "w"):
            pass
        with pytest.raises(RuntimeError, match="inode"):
            current_ownership(journal)


def test_old_unreceipted_pass_blocks_readiness_after_newer_success(tmp_path):
    from datetime import timedelta

    journal = JournalStore(str(tmp_path / "ledger.db"))
    now = timeutils.now_utc()
    journal.insert(
        "scheduler_runs",
        {
            "scheduler_run_id": "legacy-unknown",
            "run_type": "monitor",
            "status": "started",
            "started_at_utc": (now - timedelta(hours=1)).isoformat(),
        },
    )
    journal.insert(
        "job_runs",
        {
            "job_run_id": "newer",
            "job_type": "monitor",
            "lock_key": "newer",
            "status": "completed",
            "started_at_utc": now.isoformat(),
            "finished_at_utc": now.isoformat(),
            "started_at_sgt": timeutils.stamp(now).local_sgt,
        },
    )
    health = monitor_health(journal, make_settings(), now)
    assert not health["entry_ready"] and health["unfinished_monitor_passes"] == 1


def test_scheduled_recovery_must_recheck_failure_fuse(tmp_path, monkeypatch):
    from alphaos.scheduler import cadence

    journal = JournalStore(str(tmp_path / "ledger.db"))
    runner = JobRunner(orch(journal), role="monitor")

    def die(*_):
        raise SystemExit("process exit after job claim")

    monkeypatch.setitem(_JOB_FUNCS, "monitor", die)
    with pytest.raises(SystemExit):
        runner.run_job("monitor", lock_key="interrupted", allow_monitor_recovery=False)
    now = timeutils.stamp()
    for index in range(2):
        journal.insert(
            "job_runs",
            {
                "job_run_id": f"failed-{index}",
                "job_type": "monitor",
                "lock_key": f"failed-{index}",
                "status": "failed",
                "started_at_utc": now.utc,
                "started_at_sgt": now.local_sgt,
                "finished_at_utc": now.utc,
            },
        )
    calls = []
    monkeypatch.setitem(
        _JOB_FUNCS, "monitor", lambda *_: calls.append(True) or {"status": "completed"}
    )
    monkeypatch.setattr(cadence, "is_due", lambda *a, **kw: (True, "test"))
    assert runner.run_due_jobs()[0]["status"] == "fused"
    assert not calls and monitor_health(journal, runner.orch.settings)["fused"]
