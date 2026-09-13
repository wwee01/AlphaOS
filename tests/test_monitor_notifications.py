"""Transition delivery, failures and real-process notification deduplication."""
from __future__ import annotations

import json
import multiprocessing
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from alphaos.journal.journal_store import JournalStore
from alphaos.scheduler.job_runner import JobRunner
from alphaos.scheduler.monitor_notifications import notify_monitor_transition
from alphaos.scheduler.worker import run_role
from alphaos.util import alerts
from conftest import make_settings


def _unhealthy(code='monitor_stale', **extra):
    return {'ok': False, 'fused': code == 'monitor_fused', 'issue_codes': [code],
            'detail': 'monitor stale 35 minutes ago', 'protection_issues': [], **extra}


def _handle(tmp_path):
    return SimpleNamespace(db_path=str(tmp_path/'ledger.db'))


def _state_path(handle):
    return Path(handle.db_path + '.monitor-notifications.json')


def test_age_and_timestamps_never_repeat_a_delivered_alert(tmp_path, monkeypatch):
    h = _handle(tmp_path)
    calls = []
    monkeypatch.setattr(alerts, 'send_alert', lambda *a, **k: calls.append(k) or True)
    for n in range(12):
        health = _unhealthy(detail=f'monitor stale {35+n*5} minutes ago',
                            last_success_at_utc=f'irrelevant-time-{n}')
        result = notify_monitor_transition(h, make_settings(), health)
        assert result['status'] == ('sent' if n == 0 else 'unchanged')
    assert len(calls) == 1
    assert stat.S_IMODE(_state_path(h).stat().st_mode) == 0o600
    assert stat.S_IMODE(Path(str(_state_path(h))+'.lock').stat().st_mode) == 0o600


def test_recovery_resets_episode_and_relapse_notifies(tmp_path, monkeypatch):
    h, calls = _handle(tmp_path), []
    monkeypatch.setattr(alerts, 'send_alert', lambda *a, **k: calls.append(k) or True)
    notify_monitor_transition(h, make_settings(), _unhealthy())
    recovered = notify_monitor_transition(h, make_settings(), {'ok': True})
    assert recovered['status'] == 'recovered'
    assert not json.loads(_state_path(h).read_text())['active']
    notify_monitor_transition(h, make_settings(), _unhealthy())
    assert len(calls) == 2


def test_escalation_and_new_fuse_notify_once_each(tmp_path, monkeypatch):
    h, calls = _handle(tmp_path), []
    monkeypatch.setattr(alerts, 'send_alert', lambda *a, **k: calls.append(k) or True)
    for code in ['monitor_stale', 'monitor_stale', 'monitor_failed', 'monitor_failed',
                 'monitor_fused', 'monitor_fused']:
        notify_monitor_transition(h, make_settings(), _unhealthy(code))
    assert len(calls) == 3


def test_new_protection_issue_not_per_pass_record_notifies(tmp_path, monkeypatch):
    h, calls = _handle(tmp_path), []
    monkeypatch.setattr(alerts, 'send_alert', lambda *a, **k: calls.append(k) or True)
    for row_id in range(5):
        notify_monitor_transition(h, make_settings(), _unhealthy('protection_blocking',
            protection_blocking=True, protection_issues=['position-a:unprotected'], check_id=str(row_id)))
    assert len(calls) == 1
    notify_monitor_transition(h, make_settings(), _unhealthy('protection_blocking',
        protection_blocking=True, protection_issues=['position-a:unprotected', 'position-b:unverifiable']))
    assert len(calls) == 2


@pytest.mark.parametrize('first', [False, RuntimeError('delivery interrupted')])
def test_failed_delivery_is_pending_and_retried(tmp_path, monkeypatch, first):
    h, calls = _handle(tmp_path), []
    def send(*a, **k):
        calls.append(k)
        if len(calls) == 1:
            if isinstance(first, Exception):
                raise first
            return first
        return True
    monkeypatch.setattr(alerts, 'send_alert', send)
    result = notify_monitor_transition(h, make_settings(), _unhealthy())
    assert result['status'] == 'delivery_failed' and result['delivery_pending']
    assert json.loads(_state_path(h).read_text())['pending']
    assert notify_monitor_transition(h, make_settings(), _unhealthy())['reason'] == 'retry'
    assert not json.loads(_state_path(h).read_text())['pending']
    assert notify_monitor_transition(h, make_settings(), _unhealthy())['status'] == 'unchanged'
    assert len(calls) == 2


@pytest.mark.parametrize('contents', ['{broken', '{"version":1,"active":true,"pending":false}', 'null'])
def test_malformed_state_is_disclosed_and_never_assumes_delivery(tmp_path, monkeypatch, contents):
    h, calls = _handle(tmp_path), []
    _state_path(h).write_text(contents)
    monkeypatch.setattr(alerts, 'send_alert', lambda *a, **k: calls.append(k) or True)
    result = notify_monitor_transition(h, make_settings(), _unhealthy())
    assert result['state_warning'] == 'invalid_notification_state_reset'
    assert result['status'] == 'sent' and len(calls) == 1
    assert json.loads(_state_path(h).read_text())['last_delivery_ok']


def test_unwritable_state_is_reported_and_not_falsely_delivered(tmp_path, monkeypatch):
    h = SimpleNamespace(db_path=str(tmp_path/'missing-parent'/'ledger.db'))
    monkeypatch.setattr(alerts, 'send_alert', lambda *a, **k: pytest.fail('cannot save delivery intent'))
    result = notify_monitor_transition(h, make_settings(), _unhealthy())
    assert result['status'] == 'notification_state_unavailable'
    assert result['delivery_pending'] and result['state_error'] == 'FileNotFoundError'


def test_worker_and_legacy_heartbeat_share_latch_without_ledger_writes(tmp_path, monkeypatch):
    db = str(tmp_path/'ledger.db')
    j = JournalStore(db)
    j.close()
    before = Path(db).read_bytes()
    settings, calls = make_settings(ALPHAOS_DB_PATH=db), []
    monkeypatch.setattr(alerts, 'send_alert', lambda *a, **k: calls.append(k) or True)
    response, code = run_role('health', settings)
    assert code == 1 and response['monitor_health']['notification']['status'] == 'sent'
    ro = JournalStore(db, read_only=True)
    try:
        response = JobRunner(SimpleNamespace(journal=ro, settings=settings)).heartbeat_check()
        assert response['notification']['status'] == 'unchanged'
    finally:
        ro.close()
    assert len(calls) == 1
    assert Path(db).read_bytes() == before


def _concurrent_alert(db, started, release, output, call_log):
    handle = SimpleNamespace(db_path=db)
    def send(*a, **k):
        with open(call_log, 'a') as dest:
            dest.write('sent\n')
        started.set()
        release.wait(8)
        return True
    with patch.object(alerts, 'send_alert', side_effect=send):
        output.put(notify_monitor_transition(handle, make_settings(), _unhealthy()))


def test_concurrent_processes_cannot_duplicate_alert_delivery(tmp_path):
    ctx = multiprocessing.get_context('spawn')
    started, release, output = ctx.Event(), ctx.Event(), ctx.Queue()
    call_log = str(tmp_path/'send.log')
    args = (str(tmp_path/'ledger.db'), started, release, output, call_log)
    first = ctx.Process(target=_concurrent_alert, args=args)
    second = ctx.Process(target=_concurrent_alert, args=args)
    first.start()
    try:
        assert started.wait(8)
        second.start()
        result = output.get(timeout=8)
        assert result['status'] == 'notification_owner_busy'
        release.set()
        assert output.get(timeout=8)['status'] == 'sent'
    finally:
        release.set()
        for p in (first, second):
            if p.pid is not None:
                p.join(timeout=8)
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=3)
    assert first.exitcode == second.exitcode == 0
    assert Path(call_log).read_text().splitlines() == ['sent']
