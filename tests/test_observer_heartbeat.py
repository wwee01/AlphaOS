"""Offline subprocess/lock/identity fixtures; no service or broker activation."""
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from conftest import make_settings
from test_paper_observer import local, Broker, Session, settings, trade, codes
from alphaos.monitoring.broker_reads import PaperReads, BrokerReadError, ObservationRefused
from alphaos.monitoring.heartbeat import run_once, run_child, observer_freshness
from alphaos.monitoring.poll import PollPaths, read_pin, run_poll, write_report
from alphaos.monitoring.observer import observe
from alphaos.util import timeutils

NOW = datetime(2026, 10, 14, 13, 20, tzinfo=timezone.utc)
ID = '11111111-1111-4111-8111-111111111111'


@pytest.fixture
def paths(tmp_path):
    reports = tmp_path / 'reports'
    reports.mkdir(mode=0o700)
    return PollPaths(tmp_path / 'paper-observer-account.json', tmp_path / 'alphaos.paper-observer.json', reports)


def private_write(path, value):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(value, output)


def pin(paths):
    private_write(paths.pin, {'version': 1, 'account_id': ID, 'paper_host': 'https://paper-api.alpaca.markets',
        'identity_method': 'independent_paper_account_number', 'identity_confirmed_at_utc': NOW.isoformat()})


def fresh(paths, age=0):
    private_write(paths.state, {'version': 1, 'active': {}, 'pending': [],
                              'last_complete_broker_poll_at_utc': (NOW - timedelta(seconds=age)).isoformat()})


def test_get_budget_limits_attempts_before_transport():
    session = Session([(200, {'id': 'account', 'status': 'ACTIVE'}), (200, [])])
    broker = PaperReads(settings(), 'account', session=session, max_requests=2)
    broker.account()
    assert broker.positions() == []
    with pytest.raises(BrokerReadError, match='poll_request_budget_exceeded'):
        broker.open_orders()
    assert len(session.calls) == 2 and broker.requests_sent == 2


@pytest.mark.parametrize('budget', [0, 51, True])
def test_read_budget_cannot_be_widened(budget):
    with pytest.raises(ObservationRefused):
        PaperReads(settings(), 'account', session=object(), max_requests=budget)


def test_pin_requires_private_file_and_paper_identity(paths):
    pin(paths)
    assert read_pin(paths.pin) == ID
    paths.pin.chmod(0o644)  # fixture only; production never chmods existing files
    with pytest.raises(ObservationRefused):
        read_pin(paths.pin)
    paths.pin.unlink()
    paths.pin.symlink_to(paths.state)
    with pytest.raises(OSError):
        read_pin(paths.pin)


def test_missing_pin_does_not_load_credentials_or_construct_broker(paths):
    calls = []
    with pytest.raises(FileNotFoundError):
        run_poll(paths, settings_factory=lambda: calls.append('settings'), broker_factory=lambda *a: calls.append('broker'))
    assert calls == []


def test_lock_overlap_skips_before_pin_credentials_or_broker(paths):
    fd = os.open(paths.lock, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result, code = run_poll(paths, settings_factory=lambda: pytest.fail('loaded credentials'))
        assert code == 0 and result['reason'] == 'observer_poll_owner_busy'
    finally:
        os.close(fd)


def test_poll_records_private_report_state_without_alerts_or_uuid(paths, monkeypatch):
    pin(paths)
    snapshot = local()
    snapshot['scan'] = None
    monkeypatch.setattr('alphaos.monitoring.poll.ledger_snapshot', lambda *a: snapshot)
    monkeypatch.setattr(timeutils, 'now_utc', lambda: NOW)
    monkeypatch.setattr('alphaos.monitoring.incidents.alerts.send_alert', lambda *a, **kw: pytest.fail('alert sent'))
    broker = Broker()
    broker.close = lambda: None
    result, code = run_poll(paths, settings_factory=settings,
        broker_factory=lambda s, account_id: broker,
        journal_factory=lambda *a, **kw: SimpleNamespace(close=lambda: None))
    assert code == 0 and result['alerts_enabled'] is False
    state = json.loads(paths.state.read_text())
    assert state['last_complete_broker_poll_at_utc'] == NOW.isoformat()
    report = Path(result['report_path'])
    assert report.stat().st_mode & 0o777 == 0o600 and paths.state.stat().st_mode & 0o777 == 0o600
    assert ID not in json.dumps(result) + report.read_text() + paths.state.read_text()


def test_original_health_always_runs_first_even_if_observer_fails(paths, capsys):
    fresh(paths)
    calls = []
    def runner(command, timeout):
        calls.append((command, timeout))
        if command[-1] == 'health':
            return {'ready': True}, 0
        raise RuntimeError('secret must not be logged')
    result, code = run_once(paths, runner=runner, now=NOW)
    assert calls[0][0][-2:] == ['--role', 'health'] and calls[0][1] == 30
    assert calls[1][0][2] == 'alphaos.monitoring.poll' and calls[1][1] == 75
    assert result['original_health']['ok'] and result['ok'] and code == 0
    assert not result['observer_ok'] and not result['observer_affects_exit_code']
    assert 'secret' not in json.dumps(result) + capsys.readouterr().out


def test_unhealthy_original_health_cannot_be_masked_by_good_poll(paths):
    fresh(paths)
    def runner(command, timeout):
        return ({'ready': False}, 1) if command[-1] == 'health' else ({'ok': True}, 0)
    result, code = run_once(paths, runner=runner, now=NOW)
    assert not result['ok'] and code == 1


@pytest.mark.parametrize('age,ok', [(900, True), (901, False), (-1, False)])
def test_observer_completion_expiry_and_future_rejection(paths, age, ok):
    fresh(paths, age)
    assert observer_freshness(paths, NOW)['ok'] is ok


def test_missing_observer_state_does_not_fail_healthy_heartbeat(paths):
    def runner(command, timeout):
        return ({'ready': True}, 0) if command[-1] == 'health' else ({'ok': True}, 0)
    result, code = run_once(paths, runner=runner, now=NOW)
    assert code == 0 and not result['observer_ok']
    assert result['observer_freshness']['code'] == 'observer_state_unavailable'


def test_busy_observer_freshness_remains_visible_without_affecting_exit(paths):
    fresh(paths)
    def runner(command, timeout):
        return ({'ready': True}, 0) if command[-1] == 'health' else ({'reason': 'observer_poll_owner_busy'}, 0)
    assert run_once(paths, runner=runner, now=NOW)[1] == 0
    result, code = run_once(paths, runner=runner, now=NOW + timedelta(minutes=16))
    assert code == 0 and not result['observer_ok']
    assert result['observer_freshness']['code'] == 'observer_stale'


def test_report_retention_preserves_other_files(paths, monkeypatch):
    monkeypatch.setattr(timeutils, 'now_utc', lambda: NOW)
    old = paths.reports / 'paper-observer-poll-old.json'
    other = paths.reports / 'operator-evidence.json'
    old.write_text('{}')
    other.write_text('{}')
    stamp = (NOW - timedelta(days=31)).timestamp()
    os.utime(old, (stamp, stamp))
    os.utime(other, (stamp, stamp))
    write_report(paths.reports, {'ok': True})
    assert not old.exists() and other.exists()


def test_subprocess_timeout_isolated_process_group_and_reaping(monkeypatch):
    calls = []
    class Child:
        pid = 999999
        attempts = 0
        def communicate(self, timeout=None):
            self.attempts += 1
            if self.attempts == 1:
                raise subprocess.TimeoutExpired('fixture', timeout)
            return b'', b''
    child = Child()
    monkeypatch.setattr(subprocess, 'Popen', lambda command, **kw: calls.append(kw) or child)
    monkeypatch.setattr(os, 'killpg', lambda pid, sig: calls.append((pid, sig)))
    result, code = run_child(['fixture'], 75)
    assert result['status'] == 'timed_out' and code == 2
    assert calls[0]['start_new_session'] is True and calls[1][0] == 999999 and child.attempts == 2


def test_real_subprocess_contract_is_broker_free():
    result, code = run_child([sys.executable, '-c', 'print("{\\"ready\\":true}")'], 2)
    assert code == 0 and result['ready'] is True


def test_complete_domain_incident_advances_observation_clock_but_failure_does_not(paths, monkeypatch):
    from alphaos.monitoring.incidents import record_transitions
    s = make_settings()
    report = {'scope': {'order_ids': []}, 'observed_at_utc': NOW.isoformat(),
              'completed_at_utc': NOW.isoformat(), 'broker_verified': True, 'ledger_source_sha256': 'fixture',
              'findings': [{'key': 'scan_data_coverage:shadow_tier', 'code': 'scan_data_coverage',
                            'entity': 'shadow_tier', 'severity': 'warning'}]}
    record_transitions(report, paths.state, s)
    report.update(broker_verified=False, completed_at_utc=(NOW + timedelta(minutes=5)).isoformat())
    record_transitions(report, paths.state, s)
    assert json.loads(paths.state.read_text())['last_complete_broker_poll_at_utc'] == NOW.isoformat()


def test_held_stop_requires_existing_activated_parent_and_working_target_contract():
    data, broker = trade()
    parent = broker.orders['b']
    parent['order_class'] = 'bracket'
    parent['legs'][0].update(status='held', order_type='stop')
    parent['legs'].append({'broker_order_id': 'target', 'symbol': 'AAPL', 'side': 'sell', 'qty': 10,
        'filled_qty': 0, 'status': 'new', 'role': 'take_profit', 'order_type': 'limit', 'limit_price': 110})
    assert 'missing_protective_order' not in codes(observe(data, settings(), broker, data_now()))
    parent['status'] = 'partially_filled'
    assert 'missing_protective_order' in codes(observe(data, settings(), broker, data_now()))


def data_now():
    return datetime(2026, 10, 13, 14, 0, tzinfo=timezone.utc)
