"""Offline public-key and durable JobRunner regression for the repeated ET hour."""
from datetime import datetime, timedelta, timezone
import socket
from types import SimpleNamespace

import pytest

from alphaos.journal.journal_store import JournalStore
from alphaos.scheduler import cadence
from alphaos.scheduler import job_runner
from alphaos.scheduler.job_runner import JobRunner
from alphaos.util import timeutils
from conftest import make_settings


def utc(hour, minute=7, *, day=1, month=11):
    return datetime(2026, month, day, hour, minute, tzinfo=timezone.utc)


def settings(interval=15):
    return SimpleNamespace(scheduler_monitor_interval_minutes=interval,
                           scheduler_outcomes_interval_minutes=interval,
                           scheduler_scan_windows="01:00-02:00,09:35-09:50")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("DST tests must not call any network")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


@pytest.mark.parametrize("minute,bucket", [(7,"01:00"),(22,"01:15"),(37,"01:30"),(52,"01:45")])
def test_public_monitor_keys_disambiguate_fixed_offset_fold(minute, bucket):
    first, second = utc(5,minute), utc(6,minute)
    parsed = cadence.market_now_et(second)
    # The public path loses fold in its ISO round trip: do not test only a
    # hand-built ZoneInfo datetime where fold already happens to be present.
    assert parsed.fold == 0 and timeutils.to_et(parsed).fold == 1
    legacy = f"monitor:2026-11-01T{bucket}"
    assert cadence.default_lock_key("monitor",settings(),first) == legacy
    assert cadence.default_lock_key("monitor",settings(),second) == legacy + ":fold1"


@pytest.mark.parametrize("job", ["monitor","outcomes_update"])
@pytest.mark.parametrize("interval", [1,7,15,30,60,90,120])
def test_shared_interval_keys_and_elapsed_gate(job, interval, journal):
    first, second = utc(5,37), utc(6,37)
    cfg = settings(interval)
    old_key = cadence.default_lock_key(job,cfg,first)
    new_key = cadence.default_lock_key(job,cfg,second)
    assert new_key == old_key + ":fold1"
    finish = second-timedelta(minutes=interval)+timedelta(seconds=1)
    journal.insert("job_runs", {"job_run_id":"prior", "job_type":job,"lock_key":"unrelated",
        "started_at_utc":(finish-timedelta(seconds=3)).isoformat(),
        "started_at_sgt":timeutils.stamp(finish).local_sgt,"finished_at_utc":finish.isoformat(),
        "status":"completed"})
    assert cadence.is_due(job,cfg,journal,second)[0] is False
    assert cadence.is_due(job,cfg,journal,second+timedelta(seconds=1))[0] is True


@pytest.mark.parametrize("instant,expected", [
    (utc(4,37),"2026-11-01T00:30"), (utc(5,37),"2026-11-01T01:30"),
    (utc(7,37),"2026-11-01T02:30"), (utc(6,37,day=2),"2026-11-02T01:30"),
    (utc(6,37,day=8,month=3),"2026-03-08T01:30"),
    (utc(7,37,day=8,month=3),"2026-03-08T03:30"),
])
def test_normal_first_fold_and_spring_keys_remain_exact(instant, expected):
    assert cadence.default_lock_key("monitor",settings(),instant) == "monitor:"+expected
    assert cadence.default_lock_key("outcomes_update",settings(),instant) == "outcomes_update:"+expected


@pytest.mark.parametrize("job", ["scan","shadow_label","daily_digest","benchmark_spine","preflight"])
def test_daily_and_window_keys_do_not_change_in_repeated_hour(job):
    assert cadence.default_lock_key(job,settings(),utc(5,37)) == cadence.default_lock_key(job,settings(),utc(6,37))
    assert ":fold1" not in cadence.default_lock_key(job,settings(),utc(6,37))


def test_durable_public_job_runs_once_per_fold_and_restart(tmp_path, monkeypatch):
    path = tmp_path/"ledger.sqlite"
    cfg = make_settings(ALPHAOS_DB_PATH=str(path))
    invoked = []
    def harmless_monitor(orch, runner):
        invoked.append(timeutils.now_utc())
        return {"status":"completed","protection_blocking":False}
    monkeypatch.setitem(job_runner._JOB_FUNCS, "monitor", harmless_monitor)
    first, second = utc(5,7), utc(6,7)
    for instant, expected_count in [(first,1),(second,2),(second+timedelta(minutes=3),2)]:
        monkeypatch.setattr(timeutils,"now_utc",lambda:instant)
        journal = JournalStore(str(path))
        try:
            runner = JobRunner(SimpleNamespace(journal=journal,settings=cfg),role="monitor")
            result = runner.run_job("monitor")
            if expected_count > len(invoked)-int(result["status"]=="completed"):
                assert result["status"] == "completed"
            else:
                assert result["status"] == "skipped" and result["reason"] == "duplicate_lock"
            assert len(invoked) == expected_count
            assert journal.count_rows("job_runs") == expected_count
            assert journal.count_rows("system_events","category=?",("monitor_ownership_v1",)) == expected_count
            for table in ["paper_orders","paper_fills","positions","exits"]:
                assert journal.count_rows(table) == 0
        finally:
            journal.close()


def test_existing_normal_key_still_deduplicates_without_historical_replay(tmp_path, monkeypatch):
    path = tmp_path/"ledger.sqlite"
    journal = JournalStore(str(path))
    now = utc(8,7)
    cfg = make_settings(ALPHAOS_DB_PATH=str(path))
    key = cadence.default_lock_key("monitor",cfg,now)
    journal.insert("job_runs",{"job_run_id":"already_completed","job_type":"monitor","lock_key":key,
        "started_at_utc":now.isoformat(),"started_at_sgt":timeutils.stamp(now).local_sgt,
        "finished_at_utc":now.isoformat(),"status":"completed"})
    monkeypatch.setattr(timeutils,"now_utc",lambda:now+timedelta(minutes=3))
    monkeypatch.setitem(job_runner._JOB_FUNCS,"monitor",lambda *a:pytest.fail("historical job replayed"))
    try:
        result=JobRunner(SimpleNamespace(journal=journal,settings=cfg),role="monitor").run_job("monitor")
        assert result["status"]=="skipped" and result["reason"]=="duplicate_lock"
        assert journal.count_rows("job_runs")==1
        assert cadence.is_due("monitor",cfg,journal,timeutils.now_utc())[0] is False
    finally:
        journal.close()
