"""Actual scheduler/SQLite regressions for research work crossing scan windows.

Only job bodies are fakes. Scan cadence, fuse, monitor-health and DB claim paths
are real; the advancing clock reproduces the observed September23 omission.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from alphaos.journal.journal_store import JournalStore
from alphaos.scheduler import cadence
from alphaos.scheduler.job_runner import JobRunner, _JOB_FUNCS
from alphaos.util import alerts, timeutils
from conftest import make_settings

START = datetime(2026, 9, 23, 13, 34, 4, tzinfo=timezone.utc)


@pytest.fixture
def dispatch(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=START)
    monkeypatch.setattr(timeutils, 'now_utc', lambda: clock.now)
    monkeypatch.setattr(alerts, 'send_alert', lambda *a, **k: True)
    journal = JournalStore(str(tmp_path / 'ledger.db'))
    settings = make_settings(ALPHAOS_DB_PATH=journal.db_path,
        SCHEDULER_SCAN_WINDOWS='09:35-09:50,11:30-12:00,15:45-16:00',
        UNATTENDED_APPROVE_WINDOWS='09:35-09:50,11:30-12:00,15:45-16:00')
    orch = SimpleNamespace(journal=journal, settings=settings)
    state = SimpleNamespace(clock=clock, journal=journal, orch=orch, calls=[],
                            after_outcomes=None, finish=START.replace(minute=46, second=19),
                            fail_outcomes=False, scan_failure=False, extra_jobs=set())
    real_due = cadence.is_due

    def due(kind, *args, **kwargs):
        if kind in ('scan', 'outcomes_update'):
            return real_due(kind, *args, **kwargs)
        return kind in state.extra_jobs, 'isolated other job'

    monkeypatch.setattr(cadence, 'is_due', due)

    def monitor_receipt(healthy=True):
        stamp = timeutils.stamp()
        journal.insert('job_runs', {
            'job_run_id': 'monitor-' + stamp.utc, 'job_type': 'monitor',
            'lock_key': 'monitor-' + stamp.utc, 'trigger_source': 'scheduler',
            'status': 'completed', 'started_at_utc': stamp.utc, 'started_at_sgt': stamp.local_sgt,
            'finished_at_utc': stamp.utc, 'finished_at_sgt': stamp.local_sgt,
            'result_summary_json': json.dumps({'worker_role': 'monitor',
                'monitor_result': {'errors': [] if healthy else ['broker read unavailable']}}),
        })

    state.monitor_receipt = monitor_receipt
    monitor_receipt()

    def outcomes(*args):
        state.calls.append(('outcomes_update', clock.now))
        clock.now = state.finish
        if state.after_outcomes:
            state.after_outcomes()
        if state.fail_outcomes:
            raise RuntimeError('offline outcomes failure after advancing clock')
        return {'status': 'completed'}

    def scan(*args):
        state.calls.append(('scan', clock.now))
        if state.scan_failure:
            raise RuntimeError('offline scan failure')
        return {'status': 'completed'}

    monkeypatch.setitem(_JOB_FUNCS, 'outcomes_update', outcomes)
    monkeypatch.setitem(_JOB_FUNCS, 'scan', scan)
    for kind in ('monitor', 'daily_digest', 'benchmark_spine'):
        monkeypatch.setitem(_JOB_FUNCS, kind,
                            lambda *args, k=kind: state.calls.append((k, clock.now)) or {'status': 'completed'})
    yield state
    journal.close()


def test_learning_finishes_inside_window_runs_scan_before_return(dispatch):
    """Fails on058dedf: actual run_due_jobs never revisits SCAN at09:46."""
    d = dispatch
    results = JobRunner(d.orch, role='research').run_due_jobs()
    assert [kind for kind, _ in d.calls] == ['outcomes_update', 'scan']
    row = d.journal.one("SELECT * FROM job_runs WHERE job_type='scan'")
    assert row['lock_key'] == 'scan:2026-09-23T09:35'
    assert row['started_at_utc'] == d.finish.isoformat()
    assert row['status'] == 'completed'
    assert any(r['job_type'] == 'scan' and r['status'] == 'completed' for r in results)


@pytest.mark.parametrize('finish', [START.replace(minute=34, second=30),
                                   START.replace(minute=50, second=0),
                                   START.replace(minute=51, second=0)])
def test_no_window_or_whole_window_overrun_never_catches_up(dispatch, finish):
    d = dispatch
    d.finish = finish
    d.after_outcomes = d.monitor_receipt
    JobRunner(d.orch, role='research').run_due_jobs()
    assert [kind for kind, _ in d.calls] == ['outcomes_update']
    assert d.journal.count_rows('job_runs', "job_type='scan'") == 0


def test_outcomes_exception_still_rechecks_and_preserves_failure(dispatch):
    d = dispatch
    d.fail_outcomes = True
    results = JobRunner(d.orch, role='research').run_due_jobs()
    assert [kind for kind, _ in d.calls] == ['outcomes_update', 'scan']
    assert d.journal.one("SELECT status,error FROM job_runs WHERE job_type='outcomes_update'") == {
        'status': 'failed', 'error': 'offline outcomes failure after advancing clock'}
    assert any(r['job_type'] == 'outcomes_update' and r['status'] == 'failed' for r in results)


def test_fresh_monitor_failure_after_long_work_blocks_scan(dispatch):
    d = dispatch
    d.after_outcomes = lambda: d.monitor_receipt(healthy=False)
    results = JobRunner(d.orch, role='research').run_due_jobs()
    assert [kind for kind, _ in d.calls] == ['outcomes_update']
    assert results[0]['status'] == 'not_due'  # healthy before learning
    assert any(r.get('reason') == 'monitor_not_ready' for r in results[1:])
    assert d.journal.count_rows('job_runs', "job_type='scan'") == 0


def test_prior_monitor_can_become_stale_while_learning_runs(dispatch):
    d = dispatch
    # Next authorized window reached, but no fresh monitoring receipt follows.
    d.finish = START.replace(hour=15, minute=35)
    results = JobRunner(d.orch, role='research').run_due_jobs()
    assert [kind for kind, _ in d.calls] == ['outcomes_update']
    assert any(r.get('reason') == 'monitor_not_ready' for r in results)


def test_unhealthy_initial_monitor_can_recover_before_new_window(dispatch):
    d = dispatch
    d.clock.now += timedelta(seconds=1)
    d.monitor_receipt(healthy=False)
    d.after_outcomes = d.monitor_receipt
    results = JobRunner(d.orch, role='research').run_due_jobs()
    assert results[0]['reason'] == 'monitor_not_ready'
    assert [kind for kind, _ in d.calls] == ['outcomes_update', 'scan']


def seed_scan(d, status, *, key=None):
    from uuid import uuid4
    stamp = timeutils.stamp()
    row = {'job_run_id': uuid4().hex, 'job_type': 'scan',
           'lock_key': key or uuid4().hex, 'trigger_source': 'scheduler',
           'status': status, 'started_at_utc': stamp.utc, 'started_at_sgt': stamp.local_sgt}
    if status != 'started':
        row.update(finished_at_utc=stamp.utc, finished_at_sgt=stamp.local_sgt)
    d.journal.insert('job_runs', row)


@pytest.mark.parametrize('status', ['started', 'completed'])
def test_existing_window_claim_survives_recheck(dispatch, status):
    d = dispatch
    seed_scan(d, status, key='scan:2026-09-23T09:35')
    JobRunner(d.orch, role='research').run_due_jobs()
    assert [kind for kind, _ in d.calls] == ['outcomes_update']
    assert d.journal.count_rows('job_runs', "job_type='scan'") == 1


def test_restart_preserves_successful_window_claim(dispatch):
    d = dispatch
    JobRunner(d.orch, role='research').run_due_jobs()
    path = d.journal.db_path
    d.journal.close()
    reopened = JournalStore(path)
    try:
        d.orch.journal = reopened
        d.calls.clear()
        JobRunner(d.orch, role='research').run_due_jobs()
        assert d.calls == []
        assert reopened.count_rows('job_runs', "job_type='scan'") == 1
    finally:
        reopened.close()


def test_scan_fuse_not_cleared_or_bypassed_by_recheck(dispatch):
    d = dispatch
    for _ in range(3):
        seed_scan(d, 'failed')
    results = JobRunner(d.orch, role='research').run_due_jobs()
    assert [kind for kind, _ in d.calls] == ['outcomes_update']
    assert any(r['job_type'] == 'scan' and r['status'] == 'fused' for r in results)
    assert d.journal.count_rows('job_runs', "job_type='scan'") == 3
    assert d.journal.count_rows('system_events', "category='scheduler_fused'") == 1


@pytest.mark.parametrize('result', ['failed', 'skipped'])
@pytest.mark.parametrize('initially_due', [False, True])
def test_failed_or_skipped_scan_attempts_once_per_pass_then_normal_next_tick_retry(
        dispatch, monkeypatch, result, initially_due):
    d = dispatch
    if initially_due:
        d.clock.now = START.replace(minute=36)
    def scan(*args):
        d.calls.append(('scan', d.clock.now))
        if result == 'failed':
            raise RuntimeError('scan fails after attempted work')
        return {'status': 'skipped', 'reason': 'existing cost guard refused'}
    monkeypatch.setitem(_JOB_FUNCS, 'scan', scan)
    d.extra_jobs = {'daily_digest', 'benchmark_spine'}
    JobRunner(d.orch, role='research').run_due_jobs()
    assert [kind for kind, _ in d.calls].count('scan') == 1
    assert {'outcomes_update', 'daily_digest', 'benchmark_spine'} <= {kind for kind, _ in d.calls}
    # Failed/skipped rows preserve the existing next-tick retry semantics.
    d.calls.clear()
    JobRunner(d.orch, role='research').run_due_jobs()
    assert [kind for kind, _ in d.calls].count('scan') == 1
    assert d.journal.count_rows('job_runs', "job_type='scan'") == 2


def test_distinct_later_window_may_run_without_starving_remaining_work(dispatch, monkeypatch):
    d = dispatch
    d.extra_jobs = {'daily_digest', 'benchmark_spine'}
    def later_work(*args):
        d.calls.append(('daily_digest', d.clock.now))
        d.clock.now = START.replace(hour=15, minute=35)
        d.monitor_receipt()
        return {'status': 'completed'}
    monkeypatch.setitem(_JOB_FUNCS, 'daily_digest', later_work)
    JobRunner(d.orch, role='research').run_due_jobs()
    assert [kind for kind, _ in d.calls] == ['outcomes_update', 'scan', 'daily_digest', 'scan', 'benchmark_spine']
    assert [r['lock_key'] for r in d.journal.query("SELECT lock_key FROM job_runs WHERE job_type='scan' ORDER BY id")] == [
        'scan:2026-09-23T09:35', 'scan:2026-09-23T11:30']


def test_claim_race_winner_is_not_dispatched_twice(dispatch, monkeypatch):
    d = dispatch
    runner = JobRunner(d.orch, role='research')
    original_acquire = runner.acquire
    def raced(kind, lock):
        if kind == 'scan':
            seed_scan(d, 'started', key=lock)  # another owner wins before INSERT
        return original_acquire(kind, lock)
    monkeypatch.setattr(runner, 'acquire', raced)
    results = runner.run_due_jobs()
    assert [kind for kind, _ in d.calls] == ['outcomes_update']
    assert any(r.get('reason') == 'duplicate_lock' for r in results)
    assert d.journal.count_rows('job_runs', "job_type='scan'") == 1


def test_monitor_and_legacy_all_roles_do_not_gain_research_rechecks(dispatch):
    d = dispatch
    d.extra_jobs = {'monitor'}
    JobRunner(d.orch, role='monitor').run_due_jobs()
    assert [kind for kind, _ in d.calls] == ['monitor']
    d.calls.clear()
    JobRunner(d.orch, role='all').run_due_jobs()
    assert [kind for kind, _ in d.calls] == ['outcomes_update']


def test_window_is_rechecked_after_gate_clock_advance(dispatch, monkeypatch):
    d = dispatch
    real_fused = cadence.is_fused
    def fuse(kind, *args):
        if kind == 'scan':
            d.clock.now = START.replace(minute=50, second=0)
        return real_fused(kind, *args)
    monkeypatch.setattr(cadence, 'is_fused', fuse)
    JobRunner(d.orch, role='research').run_due_jobs()
    assert [kind for kind, _ in d.calls] == ['outcomes_update']
    assert d.journal.count_rows('job_runs', "job_type='scan'") == 0
