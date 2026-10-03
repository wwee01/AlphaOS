"""Actual dispatcher/outcome writer with a fake clock and no network."""

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.scheduler import cadence, job_runner
from alphaos.scheduler.job_runner import JobRunner
from alphaos.util import alerts, timeutils
from conftest import make_settings
from test_outcome_processing_fairness import seed


@pytest.fixture
def window_case(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=datetime(2026, 10, 2, 19, 44, 4, tzinfo=timezone.utc))
    monkeypatch.setattr(timeutils, "now_utc", lambda: clock.now)
    monkeypatch.setattr(alerts, "send_alert", lambda *a, **k: True)
    j = JournalStore(str(tmp_path / "ledger.db"))
    s = make_settings(
        ALPHAOS_DB_PATH=j.db_path,
        SCHEDULER_SCAN_WINDOWS="15:45-16:00",
        UNATTENDED_APPROVE_WINDOWS="15:45-16:00",
        BASELINE_ENABLED=False,
        ATTRIBUTION_ENABLED=False,
    )
    orch = SimpleNamespace(journal=j, settings=s)
    orch.outcomes_update = lambda **kw: Orchestrator.outcomes_update(orch, **kw)
    state = SimpleNamespace(
        clock=clock, j=j, s=s, orch=orch, calls=[], duration=1009, scans=[]
    )

    def monitor():
        stamp = timeutils.stamp()
        j.insert(
            "job_runs",
            {
                "job_run_id": "monitor-" + stamp.utc,
                "job_type": "monitor",
                "lock_key": "monitor-" + stamp.utc,
                "status": "completed",
                "trigger_source": "scheduler",
                "started_at_utc": stamp.utc,
                "started_at_sgt": stamp.local_sgt,
                "finished_at_utc": stamp.utc,
                "finished_at_sgt": stamp.local_sgt,
                "result_summary_json": json.dumps(
                    {"worker_role": "monitor", "monitor_result": {"errors": []}}
                ),
            },
        )

    state.monitor = monitor
    monitor()

    class Provider:
        def get_daily_bars(self, symbol, start, end):
            assert not j.conn.in_transaction
            state.calls.append(symbol)
            clock.now += timedelta(seconds=state.duration)
            monitor()
            return []

    monkeypatch.setattr(
        "alphaos.data.providers.alpaca_bars.make_bars_provider", lambda *a: Provider()
    )
    real_due = cadence.is_due
    monkeypatch.setattr(
        cadence,
        "is_due",
        lambda kind, *a, **kw: (
            real_due(kind, *a, **kw)
            if kind in ("scan", "outcomes_update")
            else (False, "isolated unrelated job")
        ),
    )
    monkeypatch.setitem(
        job_runner._JOB_FUNCS,
        "scan",
        lambda *a: state.scans.append(clock.now) or {"status": "completed"},
    )
    yield state
    j.close()


def test_admission_prevents_actual_one_row_job_consuming_entire_close_window(
    window_case,
):
    d = window_case
    seed(d.j, "SLOW", decision="2026-09-01T15:00:00Z")
    results = JobRunner(d.orch, role="research").run_due_jobs()
    assert d.calls == []
    assert d.j.count_rows("job_runs", "job_type='outcomes_update'") == 0
    assert any(
        r["job_type"] == "outcomes_update" and r["status"] == "deferred"
        for r in results
    )
    d.clock.now = datetime(2026, 10, 2, 19, 45, tzinfo=timezone.utc)
    d.monitor()
    JobRunner(d.orch, role="research").run_due_jobs()
    assert d.scans[0].minute == 45


def test_far_started_work_yields_before_claiming_more_rows_and_scan_still_runs(
    window_case,
):
    d = window_case
    d.clock.now = datetime(2026, 10, 2, 19, 0, tzinfo=timezone.utc)
    d.monitor()
    d.duration = 120
    for i in range(40):
        seed(d.j, f"ROW{i}", decision="2026-09-01T15:00:00Z")
    results = JobRunner(d.orch, role="research").run_due_jobs()
    assert any(
        r["job_type"] == "outcomes_update" and r["status"] == "yielded" for r in results
    )
    assert len(d.calls) == 20
    cursor = d.j.one(
        "SELECT * FROM outcome_processing_cursors WHERE family='forward_replay_v1'"
    )
    assert cursor["last_row_id"] == 20
    assert (
        d.j.scalar(
            "SELECT COUNT(*) FROM candidate_outcomes WHERE outcome_status='pending'"
        )
        == 20
    )
    d.clock.now = datetime(2026, 10, 2, 19, 45, tzinfo=timezone.utc)
    d.monitor()
    JobRunner(d.orch, role="research").run_due_jobs()
    assert d.scans[0].minute == 45


def test_manual_body_overrun_never_manufactures_late_scan(window_case):
    d = window_case
    seed(d.j, "SLOW", decision="2026-09-01T15:00:00Z")
    JobRunner(d.orch, role="all").run_due_jobs()
    assert d.calls == ["SLOW"] and d.scans == []
    assert d.clock.now.hour == 20


@pytest.mark.parametrize("status", ["started", "failed", "skipped", "yielded"])
def test_unserved_window_defers_every_background_kind(window_case, status):
    from alphaos.scheduler.research_budget import admission

    d = window_case
    st = timeutils.stamp()
    d.j.insert(
        "job_runs",
        {
            "job_run_id": "scan-attempt",
            "job_type": "scan",
            "lock_key": "scan:2026-10-02T15:45",
            "status": status,
            "started_at_utc": st.utc,
            "started_at_sgt": st.local_sgt,
        },
    )
    d.clock.now += timedelta(minutes=2)
    for kind in job_runner._JOB_FUNCS:
        if kind not in ("scan", "monitor"):
            assert not admission(d.j, d.s, kind)["allowed"]


def test_completed_window_allows_background_and_no_claim_for_deferral(window_case):
    from alphaos.scheduler.research_budget import admission

    d = window_case
    d.clock.now += timedelta(minutes=2)
    st = timeutils.stamp()
    d.j.insert(
        "job_runs",
        {
            "job_run_id": "served",
            "job_type": "scan",
            "lock_key": "scan:2026-10-02T15:45",
            "status": "completed",
            "started_at_utc": st.utc,
            "started_at_sgt": st.local_sgt,
            "finished_at_utc": st.utc,
        },
    )
    assert admission(d.j, d.s, "outcomes_update")["allowed"]


def test_history_extends_admission_but_never_calls_it_hard_deadline(window_case):
    from alphaos.scheduler.research_budget import admission

    d = window_case
    d.clock.now = datetime(2026, 10, 2, 19, 25, tzinfo=timezone.utc)
    d.j.insert(
        "job_runs",
        {
            "job_run_id": "long-past",
            "job_type": "outcomes_update",
            "lock_key": "old",
            "status": "completed",
            "duration_ms": 1_139_690,
            "started_at_utc": "2026-10-01T18:00:00+00:00",
            "started_at_sgt": "2026-10-02T02:00:00+08:00",
        },
    )
    proof = admission(d.j, d.s, "outcomes_update")
    assert proof["admission_seconds"] == 1199.69
    assert proof["allowed"]  # 20min remaining, envelope just under20min.
    d.clock.now += timedelta(seconds=1)
    assert not admission(d.j, d.s, "outcomes_update")["allowed"]


def test_sticky_budget_remains_yielded_after_single_call_overruns_whole_window(
    window_case,
):
    from alphaos.scheduler.research_budget import ScanWindowBudget

    d = window_case
    d.clock.now = datetime(2026, 10, 2, 19, 0, tzinfo=timezone.utc)
    budget = ScanWindowBudget(d.j, d.s)
    assert not budget()
    d.clock.now = datetime(2026, 10, 2, 20, 1, tzinfo=timezone.utc)
    assert (
        budget() and budget.as_dict()["yield_before_utc"] == "2026-10-02T19:40:00+00:00"
    )
    assert not cadence.is_due("scan", d.s, d.j, d.clock.now)[0]


def test_clock_regression_yields_instead_of_extending_budget(window_case):
    from alphaos.scheduler.research_budget import ScanWindowBudget

    d = window_case
    d.clock.now = datetime(2026, 10, 2, 19, 0, tzinfo=timezone.utc)
    budget = ScanWindowBudget(d.j, d.s)
    d.clock.now -= timedelta(seconds=1)
    assert budget()
