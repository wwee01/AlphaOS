"""Real process/SQLite ownership and monitor-readiness regressions, no broker."""
from __future__ import annotations

import multiprocessing
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from alphaos.journal.journal_store import JournalStore
from alphaos.scheduler import cadence
from alphaos.scheduler.job_runner import JobRunner, _JOB_FUNCS
from alphaos.scheduler.monitor_health import monitor_health
from alphaos.scheduler.ownership import monitor_ownership, worker_ownership
from alphaos.scheduler.worker import run_role
from alphaos.util import alerts, timeutils
from conftest import make_settings

NOW = datetime(2026, 9, 13, 14, 0, tzinfo=timezone.utc)  # Sunday: still expected


def _row(journal, state, *, kind='monitor', when=NOW, summary=None):
    import json
    import uuid
    journal.insert('job_runs', {
        'job_run_id': str(uuid.uuid4()), 'job_type': kind,
        'lock_key': str(uuid.uuid4()), 'trigger_source': 'scheduler',
        'started_at_utc': when.isoformat(), 'started_at_sgt': timeutils.stamp(when).local_sgt,
        'finished_at_utc': when.isoformat() if state != 'started' else None,
        'status': state, 'result_summary_json': json.dumps(summary or {}),
    })


def _orch(journal):
    return SimpleNamespace(journal=journal, settings=make_settings(ALPHAOS_DB_PATH=journal.db_path),
                           close=journal.close)


def test_fused_monitor_not_masked_by_healthy_research(journal, monkeypatch):
    for _ in range(3):
        _row(journal, 'failed')
    _row(journal, 'completed', kind='outcomes_update')
    calls = []
    monkeypatch.setattr(alerts, 'send_alert', lambda *a, **k: True)
    monkeypatch.setattr(timeutils, 'now_utc', lambda: NOW)
    monkeypatch.setattr(cadence, 'is_due', lambda *a: (True, 'test'))
    monkeypatch.setitem(_JOB_FUNCS, 'monitor', lambda *a: calls.append('monitor') or {'status': 'completed'})
    result = JobRunner(_orch(journal), role='monitor').run_due_jobs()
    health = JobRunner(_orch(journal)).heartbeat_check(now=NOW)
    assert result[0]['status'] == 'fused'
    assert calls == []
    assert health['ok'] is False and health['fused'] is True
    assert health['last_success_at_utc'] is None
    assert journal.count_rows('job_runs', "job_type='monitor'") == 3


def test_latest_failure_blocks_even_with_recent_monitor_success(journal):
    _row(journal, 'completed', when=NOW-timedelta(minutes=5))
    _row(journal, 'failed')
    assert not monitor_health(journal, make_settings(), NOW)['ok']


def test_weekend_missing_monitor_unhealthy_and_success_healthy(journal):
    assert not monitor_health(journal, make_settings(), NOW)['ok']
    _row(journal, 'completed')
    health = monitor_health(journal, make_settings(), NOW)
    assert health['ok'] and health['entry_ready'] and not health['market_hours']


@pytest.mark.parametrize('summary', [
    {'monitor_result': {'errors': ['broker read uncertain']}},
    {'monitor_result': {'time_exits_errors': ['quantity unresolved']}},
    {'protection_blocking': True},
])
def test_normal_return_with_execution_error_is_unhealthy(journal, summary):
    _row(journal, 'completed', summary=summary)
    assert not monitor_health(journal, make_settings(), NOW)['ok']


def test_new_scheduled_scans_block_but_outcomes_continue(journal, monkeypatch):
    calls = []
    monkeypatch.setattr(timeutils, 'now_utc', lambda: NOW)
    monkeypatch.setattr(cadence, 'is_due', lambda kind, *a: (kind in ('scan', 'outcomes_update'), 'test'))
    for kind in ('scan', 'monitor', 'outcomes_update'):
        monkeypatch.setitem(_JOB_FUNCS, kind, lambda *a, k=kind: calls.append(k) or {'status': 'completed'})
    results = JobRunner(_orch(journal), role='research').run_due_jobs()
    assert calls == ['outcomes_update']
    assert results[0]['reason'] == 'monitor_not_ready'
    assert not any(r['job_type'] == 'monitor' for r in results)


def test_orphan_monitor_not_reaped_or_retried_by_scheduled_roles(journal, monkeypatch):
    _row(journal, 'started', when=NOW-timedelta(hours=3))
    monkeypatch.setattr(timeutils, 'now_utc', lambda: NOW)
    monkeypatch.setattr(cadence, 'is_due', lambda kind, *a: (kind == 'monitor', 'test'))
    for role in ('research', 'monitor'):
        JobRunner(_orch(journal), role=role).run_due_jobs()
    assert journal.one("SELECT status FROM job_runs")['status'] == 'started'
    assert not monitor_health(journal, make_settings(), NOW)['ok']


def test_file_lock_thread_exclusion_and_same_thread_reentrancy(tmp_path):
    handle = SimpleNamespace(db_path=str(tmp_path/'ledger.db'))
    results = []
    with monitor_ownership(handle) as first:
        with monitor_ownership(handle) as nested:
            assert first and nested
            def other():
                with monitor_ownership(handle) as acquired:
                    results.append(acquired)
            t = threading.Thread(target=other)
            t.start()
            t.join(timeout=3)
            assert not t.is_alive()
    assert results == [False]
    with monitor_ownership(handle) as released:
        assert released


def _process_role(db_path, role, started, release, output):
    journal = JournalStore(db_path)
    orch = _orch(journal)
    def slow_scan(*args):
        started.set()
        if not release.wait(8):
            raise RuntimeError('test did not release slow scan')
        return {'status': 'completed'}
    def monitor(*args):
        return {'status': 'completed'}
    with (
        patch('alphaos.orchestrator.Orchestrator', return_value=orch),
        patch.object(cadence, 'is_due', side_effect=lambda kind, *a: (kind in ('scan', 'monitor'), 'test')),
        patch.dict(_JOB_FUNCS, {'scan': slow_scan, 'monitor': monitor}),
        patch.object(alerts, 'send_alert', return_value=True),
    ):
        output.put(run_role(role, orch.settings))


def test_slow_research_process_does_not_delay_monitor(tmp_path):
    db = str(tmp_path/'ledger.db')
    j = JournalStore(db)
    _row(j, 'completed', when=timeutils.now_utc())
    j.close()
    ctx = multiprocessing.get_context('spawn')
    started, release, output = ctx.Event(), ctx.Event(), ctx.Queue()
    research = ctx.Process(target=_process_role, args=(db, 'research', started, release, output))
    monitor = ctx.Process(target=_process_role, args=(db, 'monitor', started, release, output))
    research.start()
    try:
        assert started.wait(8), 'research did not enter slow job'
        monitor.start()
        result, code = output.get(timeout=8)
        assert result['role'] == 'monitor' and code == 0
        assert result['results'][0]['status'] == 'completed'
        assert research.is_alive(), 'research must still be blocked while monitor completes'
    finally:
        release.set()
        for p in (monitor, research):
            if p.pid is not None:
                p.join(timeout=8)
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=3)
    assert monitor.exitcode == research.exitcode == 0


def _try_monitor(db_path, output):
    j = JournalStore(db_path)
    try:
        runner = JobRunner(_orch(j))
        with patch.dict(_JOB_FUNCS, {'monitor': lambda *a: {'status': 'completed'}}):
            output.put(runner.run_job('monitor', 'monitor:different-interval'))
    finally:
        j.close()


def test_second_process_cannot_run_or_reap_active_monitor_across_intervals(tmp_path):
    db = str(tmp_path/'ledger.db')
    j = JournalStore(db)
    _row(j, 'started', when=timeutils.now_utc()-timedelta(hours=3))
    ctx = multiprocessing.get_context('spawn')
    output = ctx.Queue()
    with monitor_ownership(j) as acquired:
        assert acquired
        p = ctx.Process(target=_try_monitor, args=(db, output))
        p.start()
        try:
            result = output.get(timeout=8)
            assert result['reason'] == 'monitor_owner_busy'
            assert j.one('SELECT status FROM job_runs')['status'] == 'started'
        finally:
            p.join(timeout=8)
            if p.is_alive():
                p.terminate()
                p.join(timeout=3)
    assert p.exitcode == 0
    j.close()


def test_health_worker_is_read_only_and_does_not_construct_broker(tmp_path, monkeypatch):
    db = str(tmp_path/'ledger.db')
    j = JournalStore(db)
    _row(j, 'completed', when=timeutils.now_utc())
    j.close()
    before = (tmp_path/'ledger.db').read_bytes()
    monkeypatch.setattr('alphaos.orchestrator.Orchestrator', lambda *a, **k: pytest.fail('broker constructor'))
    result, code = run_role('health', make_settings(ALPHAOS_DB_PATH=db))
    assert code == 0 and result['monitor_health']['ok']
    assert (tmp_path/'ledger.db').read_bytes() == before
    assert not (tmp_path/'ledger.db.monitor.lock').exists()


def test_research_and_monitor_ownership_are_independent(tmp_path):
    handle = SimpleNamespace(db_path=str(tmp_path/'ledger.db'))
    with worker_ownership(handle, 'research') as research:
        with worker_ownership(handle, 'monitor') as monitor:
            assert research and monitor


def test_four_domain_incident_passes_keep_monitor_running_but_block_entries(journal, monkeypatch):
    from alphaos.scheduler.jobs import run_monitor_job
    orch = _orch(journal)
    calls = []
    def reconcile(**kwargs):
        calls.append('reconcile')
        return {'errors': ['protective cancellation still pending'],
                'time_exits_errors': ['working close requires reconciliation']}
    orch.run_monitor_once = reconcile
    monkeypatch.setitem(_JOB_FUNCS, 'monitor', run_monitor_job)
    monkeypatch.setattr(alerts, 'send_alert', lambda *a, **k: True)
    monkeypatch.setattr(cadence, 'is_due', lambda kind, *a: (kind == 'monitor', 'test'))
    now = NOW
    for i in range(4):
        now = NOW + timedelta(minutes=i*15)
        monkeypatch.setattr(timeutils, 'now_utc', lambda now=now: now)
        results = JobRunner(orch, role='monitor').run_due_jobs()
        assert results[0]['status'] == 'completed'
        assert results[0]['completed_with_incidents']
    health = monitor_health(journal, orch.settings, now)
    assert len(calls) == 4 and not health['fused']
    assert not health['ok'] and not health['entry_ready']


def test_readiness_requires_completed_dedicated_worker_pass(tmp_path):
    db = str(tmp_path/'ledger.db')
    j = JournalStore(db)
    _row(j, 'completed', when=timeutils.now_utc())
    j.close()
    _, code = run_role('readiness', make_settings(ALPHAOS_DB_PATH=db))
    assert code == 1
    j = JournalStore(db)
    _row(j, 'completed', when=timeutils.now_utc(), summary={'worker_role': 'monitor'})
    j.close()
    result, code = run_role('readiness', make_settings(ALPHAOS_DB_PATH=db))
    assert code == 0 and result['ready']


def test_preflight_refuses_unfinished_manual_monitor_pass(tmp_path):
    db = str(tmp_path/'ledger.db')
    j = JournalStore(db)
    j.insert('scheduler_runs', {'scheduler_run_id': 'legacy-pass', 'run_type': 'monitor',
                              'trigger_source': 'manual_cli', 'status': 'started'})
    j.close()
    result, code = run_role('preflight', make_settings(ALPHAOS_DB_PATH=db))
    assert code == 1 and not result['ready']
    assert result['active_monitor_passes'][0]['scheduler_run_id'] == 'legacy-pass'



def test_manual_monitor_uses_same_owner_without_startup_or_broker_calls(tmp_path):
    from alphaos.orchestrator import Orchestrator
    j = JournalStore(str(tmp_path/'ledger.db'))
    orch = Orchestrator.__new__(Orchestrator)
    orch.journal = j
    orch._run_monitor_owned = lambda *a: pytest.fail('another owner must prevent monitor body')
    entered, release = threading.Event(), threading.Event()
    def hold():
        with monitor_ownership(j) as acquired:
            assert acquired
            entered.set()
            release.wait(3)
    t = threading.Thread(target=hold)
    t.start()
    try:
        assert entered.wait(3)
        result = orch.run_monitor_once()
        assert result['status'] == 'skipped' and result['reason'] == 'monitor_owner_busy'
        assert j.count_rows('scheduler_runs') == 0
    finally:
        release.set()
        t.join(timeout=3)
        j.close()


def test_launchd_plists_are_valid_and_roles_are_separate():
    import plistlib
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for service, role in [('scheduler', 'research'), ('monitor', 'monitor'), ('heartbeat', 'health')]:
        config = plistlib.loads((root/'deploy'/f'com.ck.alphaos.{service}.plist').read_bytes())
        assert config['ProgramArguments'][1:] == ['-m', 'alphaos.scheduler.worker', '--role', role]
        assert config['StartInterval'] == 300


def test_recent_unfinished_manual_monitor_blocks_entry_readiness(journal):
    _row(journal, 'completed', when=NOW-timedelta(minutes=5))
    journal.insert('scheduler_runs', {'scheduler_run_id': 'manual-inflight', 'run_type': 'monitor',
                                    'status': 'started', 'started_at_utc': NOW.isoformat()})
    health = monitor_health(journal, make_settings(), NOW)
    assert health['ok'] and not health['entry_ready']
    assert health['unfinished_monitor_passes'] == 1
