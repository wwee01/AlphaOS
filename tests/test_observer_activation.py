"""Temporary plists/pins and fake launchctl; never read keys or activate services."""
import json
import plistlib
from pathlib import Path

import pytest

from alphaos.monitoring.activation import Layout, LABEL, activate, rollback, check_review, file_hash
from alphaos.monitoring.broker_reads import ObservationRefused
from alphaos.monitoring.poll import PollPaths

ID = '11111111-1111-4111-8111-111111111111'


class Service:
    def __init__(self, installed):
        self.installed = installed
        self.arguments = plistlib.loads(installed.read_bytes())['ProgramArguments']
        self.running = False
        self.calls = []
        self.fail_bootstrap_once = False

    def inspect(self):
        return {'arguments': self.arguments, 'running': self.running} if self.arguments else None

    def bootout(self):
        self.calls.append(('bootout', LABEL))
        self.arguments = None

    def bootstrap(self, path):
        self.calls.append(('bootstrap', LABEL))
        if self.fail_bootstrap_once:
            self.fail_bootstrap_once = False
            raise RuntimeError('fixture only')
        self.arguments = plistlib.loads(path.read_bytes())['ProgramArguments']


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'data').mkdir()
    (repo / 'source.py').write_text('fixture source')
    installed = tmp_path / 'heartbeat.plist'
    original = {'Label': LABEL, 'StartInterval': 300, 'RunAtLoad': True, 'WorkingDirectory': str(repo),
                'EnvironmentVariables': {'HOME': str(tmp_path)},
                'ProgramArguments': [str(repo / '.venv/bin/python'), '-m', 'alphaos.scheduler.worker', '--role', 'health']}
    installed.write_bytes(plistlib.dumps(original))
    paths = PollPaths(repo / 'data/paper-observer-account.json', repo / 'data/alphaos.paper-observer.json', tmp_path / 'reports')
    candidate = repo / 'candidate.plist'
    proposal = {**original, 'ProgramArguments': [str(repo / '.venv/bin/python'), '-m', 'alphaos.monitoring.heartbeat',
        '--pin-path', str(paths.pin), '--state-path', str(paths.state), '--report-directory', str(paths.reports)]}
    candidate.write_bytes(plistlib.dumps(proposal))
    layout = Layout(repo, installed, candidate, paths, tmp_path / 'activation.json')
    review = {'version': 1, 'repository': str(repo), 'head': 'fixture-head',
              'source_sha256': {'source.py': file_hash(repo / 'source.py'), 'candidate.plist': file_hash(candidate)},
              'installed_heartbeat_sha256': file_hash(installed)}
    monkeypatch.setattr('alphaos.monitoring.activation.subprocess.check_output', lambda command, **kw: 'fixture-head\n' if command[1] == 'rev-parse' else '')
    pin = {'version': 1, 'account_id': ID, 'paper_host': 'https://paper-api.alpaca.markets',
           'identity_method': 'independent_paper_account_number', 'identity_confirmed_at_utc': '2026-10-14T13:20:00Z'}
    return layout, review, Service(installed), lambda: pin


def good_poll(paths):
    return {'broker_verified': True, 'ok': True, 'finding_codes': []}, 0


def test_activation_changes_only_heartbeat_and_creates_private_pin_backup(fixture):
    layout, review, service, identity = fixture
    result = activate(layout, review, service, identity=identity, poll=good_poll)
    assert result['status'] == 'heartbeat_observer_registered' and ID not in json.dumps(result)
    assert service.calls == [('bootout', LABEL), ('bootstrap', LABEL)]
    assert layout.paths.pin.stat().st_mode & 0o777 == 0o600
    assert layout.paths.reports.stat().st_mode & 0o777 == 0o700
    receipt = json.loads(layout.receipt.read_text())
    assert receipt['status'] == 'registered' and Path(receipt['original_backup']).stat().st_mode & 0o777 == 0o600
    assert plistlib.loads(layout.installed.read_bytes())['StartInterval'] == 300


def test_read_only_review_does_not_call_identity_or_change_files(fixture):
    layout, review, service, _ = fixture
    old = layout.installed.read_bytes()
    check_review(layout, review, service)
    assert layout.installed.read_bytes() == old and not layout.paths.pin.exists() and service.calls == []


def test_changed_source_refuses_before_credentials(fixture):
    layout, review, service, _ = fixture
    (layout.repository / 'source.py').write_text('operator edit')
    with pytest.raises(ObservationRefused, match='reviewed_source_changed'):
        activate(layout, review, service, identity=lambda: pytest.fail('credentials accessed'), poll=good_poll)
    assert not layout.paths.pin.exists() and service.calls == []


def test_existing_pin_preserved_no_unconditional_overwrite(fixture):
    layout, review, service, _ = fixture
    layout.paths.pin.write_text('operator file')
    with pytest.raises(ObservationRefused, match='activation_metadata_exists'):
        activate(layout, review, service, identity=lambda: pytest.fail('credentials accessed'), poll=good_poll)
    assert layout.paths.pin.read_text() == 'operator file' and service.calls == []


def test_busy_heartbeat_refuses_without_killing_it(fixture):
    layout, review, service, _ = fixture
    service.running = True
    with pytest.raises(ObservationRefused, match='not_idle'):
        activate(layout, review, service, identity=lambda: pytest.fail('credentials accessed'), poll=good_poll)
    assert service.calls == []


def test_heartbeat_starting_during_poll_prevents_reload(fixture):
    layout, review, service, identity = fixture
    def poll(paths):
        service.running = True
        return good_poll(paths)
    with pytest.raises(ObservationRefused, match='not_idle'):
        activate(layout, review, service, identity=identity, poll=poll)
    assert service.calls == [] and layout.paths.pin.exists()


def test_failed_broker_verification_never_reloads_service(fixture):
    layout, review, service, identity = fixture
    with pytest.raises(ObservationRefused, match='initial_broker_observation_incomplete'):
        activate(layout, review, service, identity=identity, poll=lambda paths: ({'broker_verified': False}, 1))
    assert service.calls == [] and layout.paths.pin.exists()


def test_candidate_cannot_change_existing_environment_or_cadence(fixture):
    layout, review, service, _ = fixture
    candidate = plistlib.loads(layout.candidate.read_bytes())
    candidate['StartInterval'] = 600
    layout.candidate.write_bytes(plistlib.dumps(candidate))
    review['source_sha256']['candidate.plist'] = file_hash(layout.candidate)
    with pytest.raises(ObservationRefused, match='more_than_arguments'):
        check_review(layout, review, service)
    assert service.calls == []


def test_bootstrap_failure_restores_original_health_and_preserves_evidence(fixture):
    layout, review, service, identity = fixture
    old = layout.installed.read_bytes()
    service.fail_bootstrap_once = True
    with pytest.raises(ObservationRefused, match='original_health_restored'):
        activate(layout, review, service, identity=identity, poll=good_poll)
    assert layout.installed.read_bytes() == old and service.arguments[-1] == 'health'
    assert layout.paths.pin.exists() and json.loads(layout.receipt.read_text())['original_health_restored']


def test_rollback_restores_only_original_heartbeat_and_keeps_pin(fixture):
    layout, review, service, identity = fixture
    old = layout.installed.read_bytes()
    activate(layout, review, service, identity=identity, poll=good_poll)
    result = rollback(layout, service)
    assert result['status'] == 'original_broker_free_heartbeat_restored'
    assert layout.installed.read_bytes() == old and layout.paths.pin.exists()
    assert all(label == LABEL for action, label in service.calls)


def test_explicit_resume_reuses_confirmed_pin_after_restored_bootstrap_failure(fixture):
    layout, review, service, identity = fixture
    service.fail_bootstrap_once = True
    with pytest.raises(ObservationRefused):
        activate(layout, review, service, identity=identity, poll=good_poll)
    before = layout.paths.pin.read_bytes()
    result = activate(layout, review, service, identity=lambda: pytest.fail('pin re-created'), poll=good_poll, resume=True)
    assert result['status'] == 'heartbeat_observer_registered' and layout.paths.pin.read_bytes() == before
