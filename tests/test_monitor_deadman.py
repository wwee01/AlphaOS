"""Deadman transport is always replaced; no provider or external request."""
import copy
import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

from alphaos.lineage.config_snapshot import build_config_hashes, settings_dict
from alphaos.scheduler import monitor_deadman as dm
from alphaos.util.alerts import _sanitize
from conftest import make_settings

URL = 'https://deadman.invalid/private-token?secret=example'
HEALTH = {'ok': True, 'entry_ready': True}
COMPLETED = [{'job_type': 'monitor', 'status': 'completed', 'completed_with_incidents': False}]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(dm.urllib.request, 'build_opener', lambda *a: pytest.fail('unexpected transport'))


def configured(**overrides):
    return make_settings(ALPHAOS_MODE='paper', MONITOR_DEADMAN_ENABLED='true',
                         MONITOR_DEADMAN_URL=URL, **overrides)


def call(settings=None, **overrides):
    args = {'role': 'monitor', 'results': copy.deepcopy(COMPLETED), 'health': dict(HEALTH)}
    args.update(overrides)
    return dm.ping_monitor_success(settings or configured(), **args)


def test_default_off_and_url_alone_are_inert():
    assert make_settings().monitor_deadman_enabled is False
    assert make_settings().monitor_deadman_url == ''
    assert call(make_settings()) == {'status': 'disabled'}
    assert call(make_settings(MONITOR_DEADMAN_URL=URL)) == {'status': 'disabled'}


@pytest.mark.parametrize('role', ['research', 'health', 'readiness', 'preflight', 'all'])
def test_only_monitor_role_can_ping(role):
    assert call(role=role)['status'] == 'skipped'


@pytest.mark.parametrize('overrides', [{'ALPHAOS_MODE': 'mock'}, {'OFFLINE_MODE': 'true'}, {'RUN_MODE': 'mock'}])
def test_mock_and_offline_never_ping(overrides):
    env = {'ALPHAOS_MODE': 'paper', 'MONITOR_DEADMAN_ENABLED': 'true', 'MONITOR_DEADMAN_URL': URL}
    env.update(overrides)
    assert call(make_settings(**env))['status'] == 'skipped'


@pytest.mark.parametrize('results,health', [
    ([], HEALTH),
    *[([{'job_type': 'monitor', 'status': status}], HEALTH)
      for status in ['not_due', 'skipped', 'failed', 'fused', 'blocked', 'started']],
    ([{'job_type': 'scan', 'status': 'completed'}], HEALTH),
    ([{**COMPLETED[0], 'completed_with_incidents': True}], HEALTH),
    ([{**COMPLETED[0], 'protection_blocking': True}], HEALTH),
    (COMPLETED, {'ok': False, 'entry_ready': False}),
    (COMPLETED, {'ok': True, 'entry_ready': False}),
    (COMPLETED, {'ok': True}),
    (COMPLETED + [{'status': 'failed', 'job_type': 'monitor'}], HEALTH),
])
def test_no_ping_without_current_healthy_completed_supervision(results, health):
    before = copy.deepcopy((results, health))
    assert call(results=results, health=health)['status'] == 'skipped'
    assert (results, health) == before


@pytest.mark.parametrize('url,reason', [('', 'missing_url'), ('http://deadman.invalid/token', 'invalid_url'),
    ('https://user:password@deadman.invalid/token', 'invalid_url'),
    ('https://deadman.invalid/token#secret', 'invalid_url'),
    ('https://deadman.invalid/token\nheader', 'invalid_url')])
def test_bad_configuration_is_sanitized_without_transport(url, reason):
    settings = make_settings(ALPHAOS_MODE='paper', MONITOR_DEADMAN_ENABLED='true', MONITOR_DEADMAN_URL=url)
    assert call(settings) == {'status': 'failed', 'reason': reason}


def fake_transport(monkeypatch, status=204, error=None):
    calls = []
    class Response:
        def __enter__(self):
            return SimpleNamespace(status=status)
        def __exit__(self, *a):
            calls.append('closed')
    class Opener:
        def open(self, request, *, timeout):
            calls.append((request.full_url, request.method, request.data, timeout))
            if error is not None:
                raise error
            return Response()
    def build(*handlers):
        assert any(isinstance(h, dm._NoRedirect) for h in handlers)
        assert any(isinstance(h, dm.urllib.request.ProxyHandler) and h.proxies == {} for h in handlers)
        return Opener()
    monkeypatch.setattr(dm.urllib.request, 'build_opener', build)
    return calls


@pytest.mark.parametrize('status', [200, 204, 299, 302, 500])
def test_single_bounded_get_no_body_read_and_closed_response(monkeypatch, status):
    calls = fake_transport(monkeypatch, status)
    result = call()
    assert result == {'status': 'sent' if status < 300 else 'failed', 'http_status': status}
    assert calls == [(URL, 'GET', None, 5), 'closed']
    assert URL not in json.dumps(result)


def test_redirects_never_follow_secret_url():
    assert dm._NoRedirect().redirect_request(None, None, 302, 'redirect', {}, URL) is None


@pytest.mark.parametrize('error', [TimeoutError(URL), OSError(URL), ValueError(URL)])
def test_transport_failure_is_visible_sanitized_and_not_retried(monkeypatch, capsys, error):
    calls = fake_transport(monkeypatch, error=error)
    result = call()
    assert result == {'status': 'failed', 'reason': 'transport_error'}
    assert len(calls) == 1
    assert URL not in json.dumps(result) + capsys.readouterr().out


def test_http_error_body_is_closed_not_logged(monkeypatch):
    body = io.BytesIO(URL.encode())
    calls = fake_transport(monkeypatch, error=urllib.error.HTTPError(URL, 503, URL, {}, body))
    assert call() == {'status': 'failed', 'http_status': 503}
    assert body.closed and len(calls) == 1


def test_url_excluded_from_settings_repr_lineage_and_alerts():
    settings = configured()
    alternate = make_settings(ALPHAOS_MODE='paper', MONITOR_DEADMAN_ENABLED='true',
                              MONITOR_DEADMAN_URL='https://other.invalid/another-secret')
    assert URL not in repr(settings)
    assert 'monitor_deadman_url' not in settings_dict(settings)
    assert build_config_hashes(settings) == build_config_hashes(alternate)
    assert URL not in _sanitize('request failed: ' + URL, settings)
