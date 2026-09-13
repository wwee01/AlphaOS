"""Exercise the actual installer with disposable git/plists and fake launchctl.

No real launchctl, AlphaOS worker, broker or trading ledger is invoked.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

import pytest


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS deployment tools")
SOURCE = Path(__file__).resolve().parents[1] / "deploy"

FAKE_LAUNCHCTL = r'''
import json, os, plistlib, sys
from pathlib import Path
p = Path(os.environ['INSTALL_TEST_STATE'])
s = json.loads(p.read_text())
a = sys.argv[1:]
s['calls'].append(a)
role = a[-1].split('.')[-1]
if a[0] == 'bootstrap':
    role = Path(a[-1]).stem.split('.')[-1]
fail = s.get('fail', {}).get(a[0] + ':' + role, 0)
if fail:
    s['fail'][a[0] + ':' + role] -= 1
    p.write_text(json.dumps(s)); sys.exit(5)
if a[0] == 'print-disabled':
    for k, v in s['disabled'].items():
        print('"com.ck.alphaos.' + k + '" => ' + str(v).lower())
elif a[0] == 'print':
    data = s['loaded'].get(role)
    if data is None:
        p.write_text(json.dumps(s)); sys.exit(113)
    print('state = ' + ('running' if role in s.get('running', []) else 'not running'))
    if role in s.get('running', []): print('pid = 12345')
    print('program = ' + data['ProgramArguments'][0])
    print('working directory = ' + data['WorkingDirectory'])
    print('arguments = {')
    for arg in data['ProgramArguments']: print('\t' + arg)
    print('}')
elif a[0] in ('disable', 'enable'):
    s['disabled'][role] = a[0] == 'disable'
    if a[0] == 'disable' and role == 'monitor' and s.get('race_running'):
        s['running'] = ['scheduler']
elif a[0] == 'bootout':
    s['loaded'].pop(role, None)
elif a[0] == 'bootstrap':
    if role in s['loaded']:
        p.write_text(json.dumps(s)); sys.exit(5)
    s['loaded'][role] = plistlib.loads(Path(a[-1]).read_bytes())
else:
    raise AssertionError(a)
p.write_text(json.dumps(s))
'''

FAKE_WORKER = r'''
import json, os, sys
from pathlib import Path
p = Path(os.environ['INSTALL_TEST_STATE'])
s = json.loads(p.read_text())
role = sys.argv[-1]
s['worker_calls'].append(role)
if role == 'preflight':
    s['preflights'] = s.get('preflights', 0) + 1
    if s.get('change_env_on_second_preflight') and s['preflights'] == 2:
        Path('.env').write_text('CHANGED=yes\n')
    code = s.get('preflight_code', 0)
elif role == 'readiness':
    if s.get('change_env_on_readiness'):
        Path('.env').write_text('CHANGED=yes\n')
    code = 0 if s['ready'] else 1
else:
    raise AssertionError('Installer must not execute monitor/research/health')
p.write_text(json.dumps(s))
sys.exit(code)
'''


class Installer:
    def __init__(self, tmp_path):
        self.root = (tmp_path / "project").resolve()
        self.root.mkdir()
        self.deploy = self.root / "deploy"
        self.deploy.mkdir()
        self.home = tmp_path / "operator"
        self.dest = self.home / "Library/LaunchAgents"
        self.dest.mkdir(parents=True)
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.state_path = tmp_path / "services.json"
        self.write_state({"loaded": {}, "disabled": {}, "calls": [], "worker_calls": [], "ready": True})
        self.executable(self.bin / "launchctl", FAKE_LAUNCHCTL)
        self.executable(self.bin / "worker", FAKE_WORKER)
        self.executable(self.bin / "sleep", "pass\n")
        for name in ("install_monitor_isolation.sh", "install_launchagent.sh"):
            shutil.copy2(SOURCE / name, self.deploy / name)
        for role, worker_role in (("scheduler", "research"), ("monitor", "monitor"), ("heartbeat", "health")):
            data = {
                "Label": f"com.ck.alphaos.{role}",
                "ProgramArguments": [str(self.bin / "worker"), "-m", "alphaos.scheduler.worker", "--role", worker_role],
                "WorkingDirectory": str(self.root),
                "RunAtLoad": True,
                "StartInterval": 300,
            }
            (self.deploy / f"com.ck.alphaos.{role}.plist").write_bytes(plistlib.dumps(data))
        (self.root / ".env").write_text("ALPHAOS_MODE=paper\n")
        (self.root / ".gitignore").write_text(".env\n")
        self.git("init", "-q")
        self.git("add", ".")
        self.git("-c", "user.name=Installer Test", "-c", "user.email=offline@example.invalid", "commit", "-qm", "reviewed fixture")
        self.head = self.git("rev-parse", "HEAD").strip()
        self.env_hash = hashlib.sha256((self.root / ".env").read_bytes()).hexdigest()
        self.env = {**os.environ, "HOME": str(self.home), "PATH": str(self.bin) + os.pathsep + os.environ["PATH"], "INSTALL_TEST_STATE": str(self.state_path)}

    def executable(self, path, body):
        path.write_text(f"#!{Path(sys.executable).resolve()}\n" + body)
        path.chmod(0o755)

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, text=True)

    def state(self):
        return json.loads(self.state_path.read_text())

    def write_state(self, state):
        self.state_path.write_text(json.dumps(state))

    def alter(self, **kwargs):
        self.write_state({**self.state(), **kwargs})

    def run(self, mode="--activate", identity=True, **kwargs):
        args = ["/bin/bash", str(self.deploy / "install_monitor_isolation.sh"), mode]
        if identity:
            args += ["--expected-head", self.head, "--expected-env-sha256", self.env_hash]
        return subprocess.run(args, cwd=self.root, env=self.env, text=True, capture_output=True, timeout=15, **kwargs)

    def bootstrap_roles(self):
        return [Path(c[-1]).stem.split('.')[-1] for c in self.state()['calls'] if c[0] == 'bootstrap']

    def mutations(self):
        return [c for c in self.state()['calls'] if c[0] in {'enable', 'disable', 'bootout', 'bootstrap'}]

    def old_services(self):
        loaded = {}
        for role in ('scheduler', 'monitor', 'heartbeat'):
            data = plistlib.loads((self.deploy / f'com.ck.alphaos.{role}.plist').read_bytes())
            if role == 'scheduler':
                data['ProgramArguments'] = [str(self.bin / 'worker'), '-m', 'alphaos', 'scheduler', 'run']
            (self.dest / f'com.ck.alphaos.{role}.plist').write_bytes(plistlib.dumps(data))
            loaded[role] = data
        self.alter(loaded=loaded, disabled={'scheduler': True, 'monitor': False})


@pytest.fixture
def installer(tmp_path):
    return Installer(tmp_path)


def test_stage_needs_no_activation_identity_or_runtime(installer):
    (installer.bin / 'worker').unlink()
    result = installer.run('--stage', identity=False)
    assert result.returncode == 0, result.stderr
    assert not installer.mutations()
    assert len(list((installer.dest / '.alphaos-monitor-stage').glob('*.plist'))) == 3


@pytest.mark.parametrize('mismatch', ['missing', 'head', 'env', 'tracked'])
def test_refuses_unreviewed_activation_without_service_changes(installer, mismatch):
    if mismatch == 'head':
        installer.head = '0' * 40
    if mismatch == 'env':
        installer.env_hash = '0' * 64
    if mismatch == 'tracked':
        (installer.root / '.gitignore').write_text('.env\nchanged\n')
    result = installer.run(identity=mismatch != 'missing')
    assert result.returncode != 0
    assert not installer.mutations()


def test_activation_starts_heartbeat_then_monitor_then_research_and_preserves_originals(installer):
    installer.old_services()
    originals = {p.name: p.read_bytes() for p in installer.dest.glob('*.plist')}
    result = installer.run()
    assert result.returncode == 0, result.stderr
    assert installer.bootstrap_roles() == ['heartbeat', 'monitor', 'scheduler']
    assert installer.state()['disabled']['scheduler'] is False
    backups = list((installer.dest / '.alphaos-monitor-backups').iterdir())
    assert len(backups) == 1
    for name, contents in originals.items():
        assert (backups[0] / name).read_bytes() == contents
        assert hashlib.sha256(contents).hexdigest() in (backups[0] / 'SHA256SUMS').read_text()
    assert 'scheduler_disabled=true' in (backups[0] / 'previous-state.txt').read_text()


def test_not_due_leaves_monitoring_loaded_and_resume_never_restarts_it(installer):
    installer.alter(ready=False)
    result = installer.run()
    assert result.returncode != 0
    assert 'monitor=loaded_verified' in result.stderr
    assert set(installer.state()['loaded']) == {'monitor', 'heartbeat'}
    assert installer.state()['disabled']['scheduler'] is True
    assert installer.bootstrap_roles() == ['heartbeat', 'monitor']
    assert 'monitor' not in installer.state()['worker_calls']
    installer.alter(ready=True, calls=[])
    result = installer.run('--resume')
    assert result.returncode == 0, result.stderr
    assert installer.bootstrap_roles() == ['scheduler']
    assert not any(c[0] == 'bootout' for c in installer.state()['calls'])


def test_failed_monitor_bootstrap_recovers_actual_registration_but_keeps_research_stopped(installer):
    installer.alter(fail={'bootstrap:monitor': 1})
    result = installer.run()
    assert result.returncode != 0
    assert installer.bootstrap_roles() == ['heartbeat', 'monitor', 'monitor']
    assert set(installer.state()['loaded']) == {'monitor', 'heartbeat'}
    assert installer.state()['disabled']['scheduler'] is True
    assert 'monitor=loaded_verified' in result.stderr


def test_persistent_monitor_failure_reports_unavailable_not_merely_enabled(installer):
    installer.alter(fail={'bootstrap:monitor': 2})
    result = installer.run()
    assert result.returncode != 0
    assert set(installer.state()['loaded']) == {'heartbeat'}
    assert 'monitor=UNAVAILABLE' in result.stderr
    assert installer.state()['disabled']['scheduler'] is True


def test_readiness_failure_resume_keeps_existing_monitor_and_research_stopped(installer):
    installer.alter(ready=False)
    installer.run()
    installer.alter(calls=[])
    result = installer.run('--resume')
    assert result.returncode != 0
    assert installer.bootstrap_roles() == []
    assert not any(c[0] == 'bootout' for c in installer.state()['calls'])
    assert installer.state()['disabled']['scheduler'] is True


def test_post_disable_refusal_restores_prior_enabled_states_without_bootout(installer):
    installer.old_services()
    installer.alter(race_running=True)
    result = installer.run()
    assert result.returncode != 0
    assert installer.state()['disabled'] == {'scheduler': True, 'monitor': False}
    assert not any(c[0] == 'bootout' for c in installer.state()['calls'])
    assert 'previous enabled states restored' in result.stderr


def test_changed_env_at_activation_boundary_refuses_and_restores_state(installer):
    installer.old_services()
    installer.alter(change_env_on_second_preflight=True)
    result = installer.run()
    assert result.returncode != 0
    assert installer.state()['disabled'] == {'scheduler': True, 'monitor': False}
    assert not any(c[0] == 'bootout' for c in installer.state()['calls'])


def test_old_combined_bootout_failure_does_not_bootstrap_duplicate_monitor(installer):
    installer.old_services()
    installer.alter(fail={'bootout:scheduler': 1})
    result = installer.run()
    assert result.returncode != 0
    assert installer.bootstrap_roles() == []
    assert 'could overlap its monitor' in result.stderr


def test_resume_rejects_actual_registered_wrong_role(installer):
    installer.alter(ready=False)
    installer.run()
    state = installer.state()
    state['loaded']['monitor']['ProgramArguments'][-1] = 'research'
    state['calls'] = []
    installer.write_state(state)
    result = installer.run('--resume')
    assert result.returncode != 0
    assert 'loaded definition is unverified' in result.stderr
    assert installer.bootstrap_roles() == []
    assert installer.state()['disabled']['scheduler'] is True


def test_failed_heartbeat_bootstrap_recovers_alerts_before_monitor(installer):
    installer.alter(fail={'bootstrap:heartbeat': 1})
    result = installer.run()
    assert result.returncode != 0
    assert installer.bootstrap_roles() == ['heartbeat', 'heartbeat', 'monitor']
    assert set(installer.state()['loaded']) == {'monitor', 'heartbeat'}
    assert installer.state()['disabled']['scheduler'] is True


def test_failed_research_bootstrap_keeps_independent_monitoring_loaded(installer):
    installer.alter(fail={'bootstrap:scheduler': 1})
    result = installer.run()
    assert result.returncode != 0
    assert installer.bootstrap_roles() == ['heartbeat', 'monitor', 'scheduler']
    assert set(installer.state()['loaded']) == {'monitor', 'heartbeat'}
    assert installer.state()['disabled']['scheduler'] is True
    assert 'monitor=loaded_verified' in result.stderr


def test_resume_recovers_absent_monitor_without_restarting_heartbeat(installer):
    installer.alter(ready=False)
    installer.run()
    state = installer.state()
    state['loaded'].pop('monitor')
    state['calls'] = []
    state['ready'] = True
    installer.write_state(state)
    result = installer.run('--resume')
    assert result.returncode == 0, result.stderr
    assert installer.bootstrap_roles() == ['monitor', 'scheduler']
    assert not any(c[0] == 'bootout' for c in installer.state()['calls'])


def test_environment_changed_during_readiness_never_starts_research(installer):
    installer.alter(change_env_on_readiness=True)
    result = installer.run()
    assert result.returncode != 0
    assert installer.bootstrap_roles() == ['heartbeat', 'monitor']
    assert installer.state()['disabled']['scheduler'] is True
    assert 'monitor=loaded_unverified' in result.stderr
    assert 'CHANGED=yes' not in result.stderr
