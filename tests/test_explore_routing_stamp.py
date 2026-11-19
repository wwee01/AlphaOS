"""Routing event time is atomic with the draw and distinct from persistence."""

from datetime import datetime, timezone
import json
import random
import sqlite3

import pytest

from alphaos import explore_policy as ep
from alphaos.journal.journal_store import JournalStore
from alphaos.util import timeutils
from test_explore_policy import scan, settings


START = "2026-11-18T20:45:00+00:00"
BEFORE = datetime(2026, 11, 18, 20, 59, 58, 123456, tzinfo=timezone.utc)
AFTER = datetime(2026, 11, 18, 21, 3, tzinfo=timezone.utc)


def saved(journal, sid="s"):
    return journal.one("SELECT * FROM scan_batches WHERE scan_batch_id=?", (sid,))


def no_clock():
    pytest.fail("a persisted draw must not take a new routing stamp")


class ConnectionHook:
    def __init__(self, connection, *, execute=None, commit=None):
        self.connection = connection
        self.execute_hook = execute
        self.commit_hook = commit

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, params=()):
        if self.execute_hook and sql.startswith("UPDATE scan_batches SET explore_window_key"):
            self.execute_hook(sql, params)
        return self.connection.execute(sql, params)

    def commit(self):
        if self.commit_hook:
            self.commit_hook()
        return self.connection.commit()


@pytest.mark.parametrize("start,pool", [(START, ["z", "a"]), (START, []),
                                       ("2026-11-18T16:00:00+00:00", ["off"])])
def test_owner_empty_and_offwindow_stamp_in_same_draw_update(journal, monkeypatch, start, pool):
    scan(journal, "s")
    monkeypatch.setattr(timeutils, "now_utc", lambda: BEFORE)
    updates = []
    journal.conn = ConnectionHook(journal.conn, execute=lambda sql, args: updates.append((sql, args)))
    chosen = ep.freeze_draw(journal, settings(), "s", start, pool)
    row = saved(journal)
    draw = json.loads(row["explore_draw_json"])
    assert row["explore_routed_at_utc"] == BEFORE.isoformat(timespec="microseconds")
    assert len(updates) == 1 and "explore_routed_at_utc=?" in updates[0][0]
    assert updates[0][1][2] == row["explore_routed_at_utc"]
    assert draw["eligible_ids"] == sorted(pool)
    assert chosen == (random.Random(draw["seed"]).choice(sorted(pool)) if draw["window_key"] and pool else None)


def test_clock_sampled_after_seed_pool_and_choice(journal, monkeypatch):
    scan(journal, "s")
    choices = []
    original = random.Random

    class RecordingRandom(original):
        def choice(self, values):
            selected = super().choice(values)
            choices.append((list(values), selected))
            return selected

    def clock():
        assert len(choices) == 1 and choices[0][0] == ["a", "z"]
        return BEFORE

    monkeypatch.setattr(ep.random, "Random", RecordingRandom)
    monkeypatch.setattr(timeutils, "now_utc", clock)
    assert ep.freeze_draw(journal, settings(), "s", START, ["z", "a"]) == choices[0][1]


def test_nonowner_copies_draw_but_stamps_its_own_copy_event(journal, monkeypatch):
    scan(journal, "owner")
    scan(journal, "copy")
    stamps = iter([BEFORE, AFTER, AFTER.replace(microsecond=77)])
    monkeypatch.setattr(timeutils, "now_utc", lambda: next(stamps))
    ep.freeze_draw(journal, settings(), "owner", START, ["winner"])
    assert ep.freeze_draw(journal, settings(), "copy", START, ["other"]) is None
    owner, copy = saved(journal, "owner"), saved(journal, "copy")
    assert owner["explore_draw_json"] == copy["explore_draw_json"]
    assert owner["explore_routed_at_utc"] == BEFORE.isoformat(timespec="microseconds")
    assert copy["explore_routed_at_utc"] == "2026-11-18T21:03:00.000077+00:00"
    assert copy["explore_window_key"] is None
    monkeypatch.setattr(timeutils, "now_utc", no_clock)
    assert ep.freeze_draw(journal, settings(), "copy", START, ["changed"]) is None
    assert saved(journal, "copy") == copy


@pytest.mark.parametrize("legacy", [False, True])
def test_reentry_never_overwrites_or_backstamps(journal, monkeypatch, legacy):
    scan(journal, "s")
    monkeypatch.setattr(timeutils, "now_utc", lambda: BEFORE)
    chosen = ep.freeze_draw(journal, settings(), "s", START, ["first"])
    if legacy:
        journal.conn.execute("UPDATE scan_batches SET explore_routed_at_utc=NULL WHERE scan_batch_id='s'")
        journal.conn.commit()
    before = list(journal.conn.iterdump())
    changes = journal.conn.total_changes
    monkeypatch.setattr(timeutils, "now_utc", no_clock)
    assert ep.freeze_draw(journal, settings(), "s", START, ["new"]) == chosen
    assert list(journal.conn.iterdump()) == before
    assert journal.conn.total_changes == changes


def test_late_update_and_commit_keep_prewrite_event_time(journal, monkeypatch):
    scan(journal, "s")
    current = [BEFORE]
    monkeypatch.setattr(timeutils, "now_utc", lambda: current[0])

    def delayed(sql, args):
        # Model waiting for the write lock across the close; do not sleep.
        current[0] = AFTER
        assert args[2] == BEFORE.isoformat(timespec="microseconds")

    journal.conn = ConnectionHook(journal.conn, execute=delayed)
    ep.freeze_draw(journal, settings(), "s", START, ["first"])
    assert current[0] == AFTER
    assert saved(journal)["explore_routed_at_utc"] == BEFORE.isoformat(timespec="microseconds")


@pytest.mark.parametrize("phase", ["execute", "before_commit", "after_commit"])
def test_failed_write_or_commit_ack_has_durable_reentry_semantics(tmp_path, monkeypatch, phase):
    class Crash(BaseException):
        pass

    path = tmp_path / "crash.db"
    journal = JournalStore(str(path))
    scan(journal, "s")
    raw = journal.conn
    monkeypatch.setattr(timeutils, "now_utc", lambda: BEFORE)

    def fail(*args):
        if phase == "after_commit":
            raw.commit()
        raise Crash

    journal.conn = ConnectionHook(raw, execute=fail if phase == "execute" else None,
                                  commit=fail if phase != "execute" else None)
    with pytest.raises(Crash):
        ep.freeze_draw(journal, settings(), "s", START, ["first"])
    journal.conn = raw
    journal.close()
    reopened = JournalStore(str(path))
    try:
        if phase == "after_commit":
            monkeypatch.setattr(timeutils, "now_utc", no_clock)
            assert ep.freeze_draw(reopened, settings(), "s", START, ["new"]) == "first"
            assert saved(reopened)["explore_routed_at_utc"] == BEFORE.isoformat(timespec="microseconds")
        else:
            assert saved(reopened)["explore_draw_json"] is None
            assert saved(reopened)["explore_routed_at_utc"] is None
            monkeypatch.setattr(timeutils, "now_utc", lambda: AFTER)
            assert ep.freeze_draw(reopened, settings(), "s", START, ["new"]) == "new"
            assert saved(reopened)["explore_routed_at_utc"] == "2026-11-18T21:03:00.000000+00:00"
    finally:
        reopened.close()


def test_same_scan_write_race_preserves_first_durable_pair(tmp_path, monkeypatch):
    path = tmp_path / "race.db"
    first, second = JournalStore(str(path)), JournalStore(str(path))
    try:
        scan(first, "s")
        stamps = iter([BEFORE, AFTER])
        monkeypatch.setattr(timeutils, "now_utc", lambda: next(stamps))

        def race(sql, args):
            assert ep.freeze_draw(second, settings(), "s", START, ["winner"]) == "winner"

        first.conn = ConnectionHook(first.conn, execute=race)
        assert ep.freeze_draw(first, settings(), "s", START, ["loser"]) == "winner"
        assert saved(first)["explore_routed_at_utc"] == "2026-11-18T21:03:00.000000+00:00"
        assert json.loads(saved(first)["explore_draw_json"])["eligible_ids"] == ["winner"]
    finally:
        first.close()
        second.close()


def test_naive_clock_refuses_without_persisting(journal, monkeypatch):
    scan(journal, "s")
    before = list(journal.conn.iterdump())
    monkeypatch.setattr(timeutils, "now_utc", lambda: BEFORE.replace(tzinfo=None))
    with pytest.raises(ValueError, match="aware clock"):
        ep.freeze_draw(journal, settings(), "s", START, ["x"])
    assert list(journal.conn.iterdump()) == before


def test_stamp_without_draw_is_not_reused_for_new_membership(journal):
    scan(journal, "s")
    journal.conn.execute("UPDATE scan_batches SET explore_routed_at_utc=?", (BEFORE.isoformat(),))
    journal.conn.commit()
    before = list(journal.conn.iterdump())
    with pytest.raises(ValueError, match="stamp exists without its draw"):
        ep.freeze_draw(journal, settings(), "s", START, ["x"])
    assert list(journal.conn.iterdump()) == before


def test_unrelated_sql_integrity_failure_is_not_swallowed(journal, monkeypatch):
    scan(journal, "s")
    monkeypatch.setattr(timeutils, "now_utc", lambda: BEFORE)

    def fail(sql, args):
        raise sqlite3.IntegrityError("synthetic unrelated constraint")

    journal.conn = ConnectionHook(journal.conn, execute=fail)
    with pytest.raises(sqlite3.IntegrityError, match="unrelated constraint"):
        ep.freeze_draw(journal, settings(), "s", START, ["x"])
    assert saved(journal)["explore_routed_at_utc"] is None
    assert saved(journal)["explore_draw_json"] is None
