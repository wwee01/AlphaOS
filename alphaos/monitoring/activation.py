"""User-only reviewed activation/rollback. Import and --check never load keys.

Activation is intentionally interactive. It confirms account identity, writes
only approved observer metadata, and replaces only the heartbeat definition.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import getpass
from hashlib import sha256
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import tempfile
from uuid import UUID
import warnings

from alphaos.monitoring.poll import PollPaths, read_private_json, read_pin, run_poll
from alphaos.monitoring.broker_reads import ObservationRefused
from alphaos.util import timeutils

LABEL = 'com.ck.alphaos.heartbeat'


def file_hash(path):
    return sha256(path.read_bytes()).hexdigest()


def private_create(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(value, output, sort_keys=True)
        output.write('\n')


def replace_plist(path, content, mode):
    fd, temporary = tempfile.mkstemp(prefix='.heartbeat-observer-', dir=path.parent)
    try:
        os.fchmod(fd, mode)  # preserve the existing operator plist's mode
        with os.fdopen(fd, 'wb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def update_receipt(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.observer-activation-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(value, output, sort_keys=True)
            output.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@dataclass(frozen=True)
class Layout:
    repository: Path
    installed: Path
    candidate: Path
    paths: PollPaths
    receipt: Path


class HeartbeatService:
    """Fixed-label launchctl only: never monitor/research/backup operations."""
    def __init__(self):
        self.domain = f'gui/{os.getuid()}'
        self.target = f'{self.domain}/{LABEL}'

    def inspect(self):
        result = subprocess.run(['launchctl', 'print', self.target], capture_output=True, text=True)
        if result.returncode:
            return None
        match = re.search(r'arguments = \{(.*?)\n\s*\}', result.stdout, re.S)
        state = re.search(r'^\s*state = (\S+)', result.stdout, re.M)
        return {'arguments': [line.strip() for line in match.group(1).splitlines() if line.strip()] if match else [],
                'running': bool(state and state.group(1) == 'running')}

    def bootout(self):
        result = subprocess.run(['launchctl', 'bootout', self.target], capture_output=True)
        if result.returncode:
            raise ObservationRefused('heartbeat_bootout_failed')

    def bootstrap(self, path):
        result = subprocess.run(['launchctl', 'bootstrap', self.domain, str(path)], capture_output=True)
        if result.returncode:
            raise ObservationRefused('heartbeat_bootstrap_failed')


def check_review(layout, review, service):
    layout.paths.validate()
    if layout.installed.is_symlink() or layout.candidate.is_symlink():
        raise ObservationRefused('service_definition_symlink_refused')
    if review.get('version') != 1 or review.get('repository') != str(layout.repository):
        raise ObservationRefused('invalid_review_manifest')
    current_head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=layout.repository, text=True).strip()
    dirty = subprocess.check_output(['git', 'diff', '--name-only', 'HEAD'], cwd=layout.repository, text=True).strip()
    if current_head != review['head'] or dirty:
        raise ObservationRefused('reviewed_repository_changed')
    for relative, expected in review['source_sha256'].items():
        if Path(relative).is_absolute() or '..' in Path(relative).parts or file_hash(layout.repository / relative) != expected:
            raise ObservationRefused('reviewed_source_changed')
    if file_hash(layout.installed) != review['installed_heartbeat_sha256']:
        raise ObservationRefused('installed_heartbeat_changed')
    previous = plistlib.loads(layout.installed.read_bytes())
    candidate = plistlib.loads(layout.candidate.read_bytes())
    expected = [str(layout.repository / '.venv/bin/python'), '-m', 'alphaos.scheduler.worker', '--role', 'health']
    if previous.get('Label') != LABEL or previous.get('ProgramArguments') != expected:
        raise ObservationRefused('original_health_definition_unexpected')
    if {k: v for k, v in candidate.items() if k != 'ProgramArguments'} != {k: v for k, v in previous.items() if k != 'ProgramArguments'}:
        raise ObservationRefused('candidate_changes_more_than_arguments')
    expected_candidate = [expected[0], '-m', 'alphaos.monitoring.heartbeat',
        '--pin-path', str(layout.paths.pin), '--state-path', str(layout.paths.state),
        '--report-directory', str(layout.paths.reports)]
    if candidate.get('ProgramArguments') != expected_candidate:
        raise ObservationRefused('candidate_observer_arguments_unexpected')
    registered = service.inspect()
    if not registered or registered['arguments'] != expected or registered['running']:
        raise ObservationRefused('heartbeat_not_idle_verified_health_role')
    return previous, candidate


def confirm_identity():
    # These credential-bearing imports and the GET execute only in --activate,
    # after the user's explicit interactive acknowledgement.
    from alphaos.config.settings import load_settings, PAPER_BASE_URL
    from alphaos.monitoring.broker_reads import PaperReads, paper_guard
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        number = getpass.getpass('Independently confirmed PAPER account number (hidden): ').strip()
    if not number:
        raise ObservationRefused('independent_account_number_required')
    settings = load_settings()
    paper_guard(settings)
    broker = PaperReads(settings, 'identity_confirmation_pending')
    try:
        account = broker._get('/v2/account')
        if (not isinstance(account, dict) or account.get('account_number') != number
                or account.get('status') != 'ACTIVE' or not isinstance(account.get('id'), str)):
            raise ObservationRefused('paper_account_identity_mismatch')
        UUID(account['id'])
        return {'version': 1, 'account_id': account['id'], 'paper_host': PAPER_BASE_URL,
                'identity_method': 'independent_paper_account_number',
                'identity_confirmed_at_utc': timeutils.to_iso(timeutils.now_utc())}
    finally:
        broker.close()


def activate(layout, review, service, identity=confirm_identity, poll=run_poll, resume=False):
    check_review(layout, review, service)
    if resume:
        read_pin(layout.paths.pin)  # explicit reuse, never replace an existing pin
    else:
        if layout.paths.pin.exists() or layout.paths.pin.is_symlink() or layout.receipt.exists():
            raise ObservationRefused('activation_metadata_exists_use_reviewed_resume')
        pin = identity()
        private_create(layout.paths.pin, pin)
    layout.paths.reports.mkdir(mode=0o700, exist_ok=True)
    result, _ = poll(layout.paths)
    if result.get('broker_verified') is not True:
        raise ObservationRefused('initial_broker_observation_incomplete_pin_retained')
    # Inspect again after authenticated reads: never replace a heartbeat that
    # became active or an operator-edited definition during identity checking.
    check_review(layout, review, service)
    old = layout.installed.read_bytes()
    mode = layout.installed.stat().st_mode & 0o777
    backup = layout.receipt.with_suffix('.original.plist')
    if layout.receipt.exists():
        prior = read_private_json(layout.receipt, max_bytes=16384)
        if (not resume or prior.get('status') not in {'prepared', 'activation_failed'}
                or prior.get('original_sha256') != sha256(old).hexdigest()
                or prior.get('candidate_sha256') != file_hash(layout.candidate)
                or file_hash(backup) != sha256(old).hexdigest()):
            raise ObservationRefused('resume_receipt_requires_review')
    else:
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(old)
    receipt = {'version': 1, 'status': 'prepared', 'original_backup': str(backup),
               'original_sha256': sha256(old).hexdigest(), 'original_mode': mode,
               'candidate_sha256': file_hash(layout.candidate), 'pin_saved': True,
               'observer_alerts_enabled': False, 'initial_finding_codes': result.get('finding_codes', [])}
    if layout.receipt.exists():
        update_receipt(layout.receipt, receipt)
    else:
        private_create(layout.receipt, receipt)
    stopped = False
    try:
        service.bootout()
        stopped = True
        replace_plist(layout.installed, layout.candidate.read_bytes(), mode)
        service.bootstrap(layout.installed)
        registered = service.inspect()
        candidate_args = plistlib.loads(layout.candidate.read_bytes())['ProgramArguments']
        if not registered or registered['arguments'] != candidate_args:
            raise ObservationRefused('observer_heartbeat_registration_unverified')
    except Exception:
        restored = False
        if stopped:
            # Only our fixed heartbeat label. Retain pin/reports for review;
            # no credentials are deleted/rotated and other services untouched.
            try:
                if service.inspect():
                    service.bootout()
                replace_plist(layout.installed, old, mode)
                service.bootstrap(layout.installed)
                actual = service.inspect()
                restored = bool(actual and actual['arguments'] == plistlib.loads(old)['ProgramArguments'])
            except Exception:
                restored = False
        receipt.update(status='activation_failed', original_health_restored=restored)
        update_receipt(layout.receipt, receipt)
        raise ObservationRefused('activation_failed_original_health_restored' if restored
                                 else 'activation_failed_service_requires_review') from None
    receipt['status'] = 'registered'
    update_receipt(layout.receipt, receipt)
    return {'status': 'heartbeat_observer_registered', 'pin_saved': True,
            'observer_alerts_enabled': False, 'receipt_path': str(layout.receipt),
            'initial_finding_codes': result.get('finding_codes', []),
            'note': 'registration is not proof of subsequent healthy cadence'}


def rollback(layout, service):
    receipt = read_private_json(layout.receipt, max_bytes=16384)
    backup = Path(receipt['original_backup'])
    if file_hash(layout.installed) != receipt['candidate_sha256'] or file_hash(backup) != receipt['original_sha256']:
        raise ObservationRefused('rollback_definition_changed_review_required')
    registered = service.inspect()
    if registered and registered['running']:
        raise ObservationRefused('heartbeat_busy_retry_after_completion')
    if registered:
        service.bootout()
    replace_plist(layout.installed, backup.read_bytes(), receipt['original_mode'])
    service.bootstrap(layout.installed)
    return {'status': 'original_broker_free_heartbeat_restored', 'pin_and_evidence_preserved': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--activate', action='store_true')
    mode.add_argument('--resume', action='store_true')
    mode.add_argument('--rollback', action='store_true')
    parser.add_argument('--review', required=True)
    args = parser.parse_args(argv)
    try:
        review = json.loads(Path(args.review).read_text())
        repository = Path('/Users/ck/Documents/Claude Playground/AlphaOS')
        layout = Layout(repository, Path.home() / 'Library/LaunchAgents/com.ck.alphaos.heartbeat.plist',
            repository / 'deploy/com.ck.alphaos.heartbeat-observer.plist',
            PollPaths(repository / 'data/paper-observer-account.json', repository / 'data/alphaos.paper-observer.json',
                      Path.home() / 'Library/Logs/alphaos/paper-observer'),
            Path(args.review).with_name('observer-heartbeat-activation.json'))
        service = HeartbeatService()
        if args.check:
            check_review(layout, review, service)
            result = {'status': 'review_matches_idle_heartbeat', 'keys_loaded': False, 'service_changes': False}
        else:
            if not os.isatty(0):
                raise ObservationRefused('interactive_user_terminal_required')
            print('Only com.ck.alphaos.heartbeat changes. Observer activation reuses existing paper credentials every five minutes; alerts off.')
            # The explicitly invoked --activate/--rollback command is the
            # user's action; do not ask for a second approval after authorization.
            result = activate(layout, review, service, resume=args.resume) if args.activate or args.resume else rollback(layout, service)
        print(json.dumps(result, sort_keys=True))
        return 0
    except ObservationRefused as exc:
        print(json.dumps({'status': 'refused', 'reason': str(exc), 'review_state_before_retry': True}))
        return 2
    except Exception as exc:
        print(json.dumps({'status': 'activation_failed', 'error_type': type(exc).__name__,
                          'review_state_before_retry': True}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
