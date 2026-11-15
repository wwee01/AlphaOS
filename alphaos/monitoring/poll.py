"""One isolated scheduled observer poll; alerts are structurally off here."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import stat
from datetime import timedelta
from uuid import UUID

from alphaos.config.settings import PAPER_BASE_URL, load_settings
from alphaos.journal.journal_store import JournalStore
from alphaos.monitoring.broker_reads import PaperReads, ObservationRefused, paper_guard
from alphaos.monitoring.incidents import record_transitions
from alphaos.monitoring.observer import ledger_snapshot, observe
from alphaos.util import timeutils


@dataclass(frozen=True)
class PollPaths:
    pin: Path
    state: Path
    reports: Path

    @property
    def lock(self):
        return Path(str(self.state) + '.poll.lock')

    def validate(self):
        if (self.pin.name != 'paper-observer-account.json'
                or not self.state.name.endswith('.paper-observer.json')
                or self.pin.resolve() == self.state.resolve()
                or any(p.is_symlink() for p in (self.pin, self.state, self.reports))):
            raise ObservationRefused('invalid_observer_paths')


def read_private_json(path, max_bytes=2_000_000):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd) as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > max_bytes):
            raise ObservationRefused('private_regular_owner_file_required')
        result = json.load(source)
    if not isinstance(result, dict):
        raise ObservationRefused('invalid_private_json')
    return result


def read_pin(path):
    value = read_private_json(path, max_bytes=4096)
    if (value.get('version') != 1 or value.get('paper_host') != PAPER_BASE_URL
            or value.get('identity_method') != 'independent_paper_account_number'
            or not timeutils.parse_iso(value.get('identity_confirmed_at_utc'))):
        raise ObservationRefused('invalid_account_pin')
    account_id = value.get('account_id')
    if not isinstance(account_id, str):
        raise ObservationRefused('invalid_account_pin')
    UUID(account_id)
    return account_id


def write_report(directory, report):
    info = directory.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ObservationRefused('private_report_directory_required')
    name = f'paper-observer-poll-{timeutils.now_utc():%Y%m%dT%H%M%S%fZ}-{os.getpid()}.json'
    path = directory / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(report, output, sort_keys=True, allow_nan=False)
        output.write('\n')
    # Retention only touches our own regular report files, never other logs.
    cutoff = (timeutils.now_utc() - timedelta(days=30)).timestamp()
    for old in directory.glob('paper-observer-poll-*.json'):
        details = old.lstat()
        if (stat.S_ISREG(details.st_mode) and details.st_uid == os.getuid()
                and details.st_mtime < cutoff):
            old.unlink()
    return path


def run_poll(paths, settings_factory=load_settings, broker_factory=PaperReads,
             journal_factory=JournalStore):
    paths.validate()
    fd = os.open(paths.lock, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    broker = None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status': 'skipped', 'reason': 'observer_poll_owner_busy', 'ok': False}, 0
        # No credentials are loaded and no broker is built if pin validation fails.
        account_id = read_pin(paths.pin)
        settings = settings_factory()
        paper_guard(settings)
        now = timeutils.now_utc()
        journal = journal_factory(settings.db_path, read_only=True)
        try:
            local = ledger_snapshot(journal, settings, now)
        finally:
            journal.close()
        broker = broker_factory(settings, account_id)
        report = observe(local, settings, broker, now)
        report['completed_at_utc'] = timeutils.to_iso(timeutils.now_utc())
        # Reports must be writable before declaring a durable completed poll.
        report_path = write_report(paths.reports, report)
        transition = record_transitions(report, paths.state, settings, notify=False)
        summary = {'status': report['status'], 'ok': report['ok'] and not transition['active_count'],
                   'broker_verified': report['broker_verified'], 'completed_at_utc': report['completed_at_utc'],
                   'finding_codes': sorted({f['code'] for f in report['findings']}),
                   'unresolved_incidents': transition['active_count'], 'report_path': str(report_path),
                   'alerts_enabled': False}
        return summary, 0 if summary['ok'] else 1
    finally:
        if broker is not None:
            broker.close()
        os.close(fd)


def add_path_arguments(parser):
    parser.add_argument('--pin-path', required=True)
    parser.add_argument('--state-path', required=True)
    parser.add_argument('--report-directory', required=True)


def paths_from_args(args):
    return PollPaths(Path(args.pin_path), Path(args.state_path), Path(args.report_directory))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    add_path_arguments(parser)
    args = parser.parse_args(argv)
    try:
        result, code = run_poll(paths_from_args(args))
    except Exception as exc:
        result, code = {'status': 'observation_failed', 'ok': False, 'error_type': type(exc).__name__}, 2
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
