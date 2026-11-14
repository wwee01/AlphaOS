"""Heartbeat first, observer child second. No broker/credentials in this parent.

This opt-in wrapper is inert until its candidate LaunchAgent is activated.
It never changes execution-monitor readiness or creates scheduler job rows.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys

from alphaos.monitoring.poll import add_path_arguments, paths_from_args, read_private_json
from alphaos.util import timeutils

OBSERVER_STALE_SECONDS = 15 * 60


def run_child(command, timeout):
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             start_new_session=True,
                             env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    try:
        output, _ = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Kill only this wrapper's isolated child process group, never launchd
        # services or unrelated workers. communicate reaps it before return.
        os.killpg(child.pid, signal.SIGKILL)
        child.communicate()
        return {'status': 'timed_out', 'ok': False}, 2
    except BaseException:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
        child.communicate()
        raise
    if len(output) > 2_000_000:
        return {'status': 'invalid_child_output', 'ok': False}, 2
    try:
        value = json.loads(output)
        if not isinstance(value, dict):
            raise ValueError('child_object_required')
    except (ValueError, UnicodeError):
        return {'status': 'invalid_child_output', 'ok': False}, 2
    return value, child.returncode


def observer_freshness(paths, now):
    try:
        state = read_private_json(paths.state)
        age = timeutils.age_seconds(state.get('last_complete_broker_poll_at_utc'), now)
        if age is None or age < 0:
            return {'ok': False, 'code': 'observer_completion_unverified'}
        return {'ok': age <= OBSERVER_STALE_SECONDS,
                'code': 'observer_fresh' if age <= OBSERVER_STALE_SECONDS else 'observer_stale',
                'age_seconds': age, 'stale_after_seconds': OBSERVER_STALE_SECONDS}
    except Exception as exc:
        return {'ok': False, 'code': 'observer_state_unavailable', 'error_type': type(exc).__name__}


def run_once(paths, runner=run_child, now=None):
    # Always run the original broker-free health command first, even if the
    # observer paths/pin/state are absent, malformed, busy or failed.
    try:
        health, health_code = runner([sys.executable, '-m', 'alphaos.scheduler.worker', '--role', 'health'], 30)
    except Exception as exc:
        health, health_code = {'status': 'failed', 'error_type': type(exc).__name__}, 2
    health_ok = health_code == 0 and health.get('ready') is True
    health_detail = health.get('monitor_health') or {}
    health_summary = {'ok': health_ok, 'exit_code': health_code,
                     'issue_codes': health_detail.get('issue_codes', []),
                     'last_success_at_utc': health_detail.get('last_success_at_utc'),
                     'notification_status': (health_detail.get('notification') or {}).get('status')}
    # Flush the original health outcome before a possibly slow broker poll.
    print(json.dumps({'role': 'health', **health_summary}), flush=True)
    try:
        paths.validate()
        observer, observer_code = runner([sys.executable, '-m', 'alphaos.monitoring.poll',
            '--pin-path', str(paths.pin), '--state-path', str(paths.state),
            '--report-directory', str(paths.reports)], 75)
    except Exception as exc:
        observer, observer_code = {'status': 'failed', 'ok': False, 'error_type': type(exc).__name__}, 2
    freshness = observer_freshness(paths, now or timeutils.now_utc())
    observer_ok = (observer_code == 0 and (observer.get('ok') is True
                   or observer.get('reason') == 'observer_poll_owner_busy') and freshness['ok'])
    # Whitelist child output, so pin IDs/provider text can't enter service logs.
    public = {k: observer[k] for k in ('status', 'reason', 'ok', 'broker_verified',
              'finding_codes', 'unresolved_incidents', 'completed_at_utc', 'error_type') if k in observer}
    result = {'role': 'health_with_paper_observer', 'ok': health_ok and observer_ok,
              'original_health': health_summary,
              'observer': public, 'observer_exit_code': observer_code,
              'observer_freshness': freshness, 'observer_alerts_enabled': False}
    return result, 0 if result['ok'] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    add_path_arguments(parser)
    args = parser.parse_args(argv)
    def stop(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, stop)
    result, code = run_once(paths_from_args(args))
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
