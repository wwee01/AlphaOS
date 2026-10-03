"""Manual recovery state machine uses copied/scratch ledgers and no provider calls."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from alphaos import shadow_marker
from alphaos.journal.journal_store import JournalStore
from alphaos.safety import KillSwitch, ShadowLabelSuspendSwitch
from alphaos.scheduler import shadow_recovery as recovery
from alphaos.scheduler.ownership import worker_ownership
from alphaos.util import timeutils
from conftest import make_settings


@pytest.fixture
def case(tmp_path, monkeypatch):
    clock = [datetime(2026, 10, 3, 6, tzinfo=timezone.utc)]
    monkeypatch.setattr(timeutils, "now_utc", lambda: clock[0])
    settings = make_settings(
        SHADOW_LABELLING_ENABLED=True,
        SHADOW_TIER_ENABLED=True,
        ALPHAOS_DB_PATH=str(tmp_path / "ledger.db"),
    )
    journal = JournalStore(settings.db_path)
    for i in range(10):
        journal.insert(
            "universe_days",
            {
                "universe_day_id": f"u{i}",
                "market_date": "2026-10-02",
                "symbol": f"S{i}",
                "tier": "watchlist",
                "freshness_status": "usable",
            },
        )
    switch = ShadowLabelSuspendSwitch(str(tmp_path / "suspend"))
    switch.engage("original historical condition")
    kill = KillSwitch(str(tmp_path / "kill"))
    canary = {
        "version": "test_canary",
        "ok": True,
        "reasons": [],
        "run_id": "real-shaped-fixture",
        "evidence_sha256": "a" * 64,
        "limitations": [],
    }
    monkeypatch.setattr(recovery, "_canary", lambda *a: dict(canary))
    monkeypatch.setattr(recovery, "_source_identity", lambda: {"test": "b" * 64})
    journal.close()
    with recovery.existing_journal(settings.db_path, writable=True) as journal:
        yield journal, settings, switch, kill, clock, canary


def view(case):
    j, s, sw, k, *_ = case
    return recovery.preview(j, s, suspend_switch=sw, kill_switch=k)


def apply(case, proof=None):
    j, s, sw, k, *_ = case
    proof = proof or view(case)
    return recovery.apply_recovery(
        j,
        s,
        proof,
        expected_preview_sha256=proof["preview_sha256"],
        note="Fresh reviewed recovery conditions.",
        suspend_switch=sw,
        kill_switch=k,
    )


def events(j):
    return j.query(
        "SELECT * FROM system_events WHERE category='shadow_recovery' ORDER BY id"
    )


def test_preview_no_writes_or_new_locks(case, tmp_path):
    j, _, sw, *_ = case
    before = sorted(p.name for p in tmp_path.iterdir())
    changes = j.conn.total_changes
    proof = view(case)
    assert proof["eligible"] is True and proof["action"] == "release"
    assert j.conn.total_changes == changes and events(j) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert sw.is_engaged()


def test_positive_release_is_audited_and_does_not_touch_kill(case):
    j, _, sw, k, *_ = case
    original = shadow_marker.fingerprint(sw.path)[0]
    result = apply(case)
    assert result["status"] == "released" and not sw.is_engaged() and not k.is_engaged()
    rows = [json.loads(r["detail_json"]) for r in events(j)]
    assert [r["phase"] for r in rows] == ["prepared", "completed"]
    assert rows[0]["marker"] == original
    assert rows[1]["prepared_sha256"] == recovery.digest(rows[0])
    assert j.scalar("SELECT COUNT(*) FROM universe_days") == 10


def test_current_failing_coverage_never_releases(case):
    j, _, sw, *_ = case
    j.conn.execute("UPDATE universe_days SET freshness_status='stale' WHERE id<=3")
    j.conn.commit()
    proof = view(case)
    assert proof["eligible"] is False and not proof["checks"]["coverage_ok"]
    with pytest.raises(recovery.RecoveryRefused):
        apply(case, proof)
    assert sw.is_engaged() and events(j) == []


@pytest.mark.parametrize("change", ["canary", "kill", "marker", "config", "coverage"])
def test_changed_preview_inputs_refuse_without_audit(case, change):
    j, s, sw, k, _, canary = case
    proof = view(case)
    if change == "canary":
        canary["evidence_sha256"] = "c" * 64
    if change == "kill":
        k.engage("stop")
    if change == "marker":
        sw.engage("new trigger")
    if change == "config":
        case = (
            j,
            make_settings(
                SHADOW_LABELLING_ENABLED=True,
                SHADOW_TIER_ENABLED=True,
                MAX_OPEN_POSITIONS=9,
                ALPHAOS_DB_PATH=j.db_path,
            ),
            sw,
            k,
            case[4],
            canary,
        )
    if change == "coverage":
        j.conn.execute("UPDATE universe_days SET freshness_status='stale' WHERE id=1")
        j.conn.commit()
    with pytest.raises(recovery.RecoveryRefused):
        apply(case, proof)
    assert sw.is_engaged() and events(j) == []


@pytest.mark.parametrize("phase", ["prepared", "completed"])
def test_audit_failure_stays_suspended_and_can_resume(case, monkeypatch, phase):
    j, _, sw, *_ = case
    original = recovery._audit

    def fail(journal, p, value):
        if p == phase:
            raise sqlite3.OperationalError("offline audit failure")
        return original(journal, p, value)

    with monkeypatch.context() as m:
        m.setattr(recovery, "_audit", fail)
        with pytest.raises(sqlite3.OperationalError):
            apply(case)
    assert sw.is_engaged()
    if phase == "completed":
        assert shadow_marker.fingerprint(sw.path)[0] is None
        assert (
            shadow_marker.fingerprint(shadow_marker.pending_path(sw.path))[0]
            is not None
        )
        assert view(case)["action"] == "resume"
    assert apply(case)["status"] == "released"


def test_restart_pending_then_new_marker_refuses_both(case, monkeypatch):
    j, _, sw, *_ = case
    original = recovery._audit

    def fail(journal, phase, value):
        if phase == "completed":
            raise RuntimeError("interrupted")
        return original(journal, phase, value)

    with monkeypatch.context() as m:
        m.setattr(recovery, "_audit", fail)
        with pytest.raises(RuntimeError):
            apply(case)
    sw.engage("new trigger after interruption")
    before = shadow_marker.fingerprint(sw.path)[0]
    pending = shadow_marker.fingerprint(shadow_marker.pending_path(sw.path))[0]
    with pytest.raises(recovery.RecoveryRefused, match="new suspension"):
        view(case)
    assert shadow_marker.fingerprint(sw.path)[0] == before
    assert shadow_marker.fingerprint(shadow_marker.pending_path(sw.path))[0] == pending


def test_same_thread_research_callback_cannot_apply(case):
    j, _, sw, *_ = case
    proof = view(case)
    with worker_ownership(j, "research") as acquired:
        assert acquired
        with pytest.raises(recovery.RecoveryRefused, match="nested"):
            apply(case, proof)
    assert sw.is_engaged() and not events(j)


def test_canary_change_after_completed_commit_keeps_pending(case, monkeypatch):
    j, _, sw, _, _, canary = case
    original = recovery._audit

    def change(journal, phase, value):
        result = original(journal, phase, value)
        if phase == "completed":
            canary["ok"] = False
        return result

    monkeypatch.setattr(recovery, "_audit", change)
    with pytest.raises(recovery.RecoveryRefused, match="before final release"):
        apply(case)
    assert sw.is_engaged() and len(events(j)) == 2
    assert shadow_marker.fingerprint(shadow_marker.pending_path(sw.path))[0] is not None


def test_same_thread_engage_poison_survives_caught_error(case, monkeypatch):
    j, _, sw, *_ = case
    original = recovery._audit

    def nested(journal, phase, value):
        if phase == "completed":
            with pytest.raises(shadow_marker.MarkerError):
                sw.engage("new nested trigger")
        return original(journal, phase, value)

    monkeypatch.setattr(recovery, "_audit", nested)
    with pytest.raises(shadow_marker.MarkerError, match="scope aborted"):
        apply(case)
    assert sw.is_engaged()


def test_busy_timeout_restored_on_refusal(case):
    j, _, sw, *_ = case
    j.conn.execute("PRAGMA busy_timeout=1234")
    proof = view(case)
    sw.engage("new trigger")
    with pytest.raises(recovery.RecoveryRefused):
        apply(case, proof)
    assert j.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1234


def test_preview_expired_tampered_or_repeated_cannot_clear(case):
    j, _, sw, _, clock, _ = case
    proof = view(case)
    clock[0] += timedelta(seconds=301)
    with pytest.raises(recovery.RecoveryRefused, match="expired"):
        apply(case, proof)
    proof = view(case)
    proof["eligible"] = False
    with pytest.raises(recovery.RecoveryRefused, match="hash"):
        apply(case, proof)
    assert sw.is_engaged() and not events(j)
    proof = view(case)
    apply(case, proof)
    with pytest.raises(recovery.RecoveryRefused):
        apply(case, proof)
    assert len(events(j)) == 2


@pytest.mark.parametrize(
    "cap",
    [
        "shadow_ai_cap_calls_per_day",
        "shadow_ai_cap_calls_per_30d",
        "scheduler_ai_cost_cap_calls_per_30d",
    ],
)
def test_actual_new_label_consumes_remaining_budget(case, cap):
    j, s, sw, k, clock, canary = case
    planned = s.shadow_label_top_k + max(
        1, round(s.shadow_explore_fraction * s.shadow_label_top_k)
    )
    s = replace(s, **{cap: planned})
    case = j, s, sw, k, clock, canary
    proof = view(case)
    assert proof["eligible"]
    j.insert(
        "candidate_labels",
        {
            "label_id": "after-preview",
            "candidate_id": "c1",
            "symbol": "AAA",
            "shadow_tier": 1,
            "is_mock": 0,
            "label_decision": "watch",
        },
    )
    assert not view(case)["checks"]["budget_ok"]
    with pytest.raises(recovery.RecoveryRefused):
        apply(case, proof)
    assert sw.is_engaged() and not events(j)


@pytest.mark.parametrize("stamp", ["2026-10-03", "2026-10-03T06:00:00", "invalid"])
def test_resealed_preview_requires_explicit_timezone(case, stamp):
    proof = view(case)
    proof.pop("preview_sha256")
    proof["observed_at_utc"] = stamp
    proof["preview_sha256"] = recovery.digest(proof)
    with pytest.raises(recovery.RecoveryRefused, match="timestamp"):
        apply(case, proof)
    assert not events(case[0]) and case[2].is_engaged()


def test_resume_uses_reopened_existing_database_without_startup(case, monkeypatch):
    j, s, sw, k, *_ = case
    original = recovery._audit

    def interrupt(journal, phase, value):
        if phase == "completed":
            raise RuntimeError("simulated interruption")
        return original(journal, phase, value)

    with monkeypatch.context() as m:
        m.setattr(recovery, "_audit", interrupt)
        with pytest.raises(RuntimeError):
            apply(case)
    schema = j.query("SELECT * FROM sqlite_master ORDER BY name")
    with recovery.existing_journal(j.db_path, writable=True) as reopened:
        fresh = recovery.preview(reopened, s, suspend_switch=sw, kill_switch=k)
        result = recovery.apply_recovery(
            reopened,
            s,
            fresh,
            expected_preview_sha256=fresh["preview_sha256"],
            note="Resume verified scratch state.",
            suspend_switch=sw,
            kill_switch=k,
        )
        assert result["status"] == "released"
    assert not sw.is_engaged() and len(events(j)) == 2
    assert j.query("SELECT * FROM sqlite_master ORDER BY name") == schema


def test_cli_preview_apply_roundtrip_no_orchestrator_or_schema_writes(
    case, monkeypatch, capsys, tmp_path
):
    from alphaos import __main__ as cli

    j, s, sw, k, *_ = case
    monkeypatch.setattr(cli, "load_settings", lambda: s)

    def forbidden(*a, **kw):
        raise AssertionError("Orchestrator must not be created")

    monkeypatch.setattr(cli, "Orchestrator", forbidden)
    monkeypatch.setattr(recovery, "ShadowLabelSuspendSwitch", lambda: sw)
    monkeypatch.setattr(recovery, "KillSwitch", lambda: k)
    schema = j.query("SELECT * FROM sqlite_master ORDER BY name")
    files = sorted(p.name for p in tmp_path.iterdir())
    assert cli.main(["shadow_recovery"]) == 0
    raw = capsys.readouterr().out
    proof = json.loads(raw)
    assert len(raw.encode()) < shadow_marker.MAX_BYTES
    assert proof["eligible"] and not events(j)
    assert sorted(p.name for p in tmp_path.iterdir()) == files
    saved = tmp_path / "preview.json"
    saved.write_text(raw)
    assert (
        cli.main(
            [
                "shadow_recovery",
                "--apply",
                "--preview-file",
                str(saved),
                "--expected-preview-sha256",
                proof["preview_sha256"],
                "--note",
                "Explicit scratch recovery.",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "released"
    assert j.query("SELECT * FROM sqlite_master ORDER BY name") == schema
    assert len(events(j)) == 2


def test_cli_help_and_no_corpus_override(capsys):
    from alphaos import __main__ as cli

    with pytest.raises(SystemExit) as help_exit:
        cli.build_parser().parse_args(["shadow_recovery", "--help"])
    assert help_exit.value.code == 0
    text = capsys.readouterr().out
    assert "--apply" in text and "--expected-preview-sha256" in text
    assert "--corpus-dir" not in text
    with pytest.raises(SystemExit) as invalid:
        cli.build_parser().parse_args(["shadow_recovery", "--corpus-dir", "old-corpus"])
    assert invalid.value.code == 2


def test_final_pending_sync_failure_restores_barrier(case, monkeypatch):
    _, _, sw, *_ = case
    original = shadow_marker.sync_parent
    pending = shadow_marker.pending_path(sw.path)
    failed = []

    def sync(path):
        if (
            shadow_marker.canonical(path) == pending
            and not pending.exists()
            and not failed
        ):
            failed.append(True)
            raise OSError("simulated directory sync failure")
        return original(path)

    with monkeypatch.context() as m:
        m.setattr(shadow_marker, "sync_parent", sync)
        with pytest.raises(OSError):
            apply(case)
    assert failed and sw.is_engaged() and pending.exists()
    assert apply(case)["status"] == "released"


def test_unbound_existing_journal_cannot_infer_old_connection_identity(case):
    j, s, sw, k, *_ = case
    unbound = JournalStore(j.db_path)
    try:
        with pytest.raises(recovery.RecoveryRefused, match="identity-bound"):
            recovery.preview(unbound, s, suspend_switch=sw, kill_switch=k)
    finally:
        unbound.close()
    assert sw.is_engaged() and not events(j)


def test_ledger_replacement_before_preview_refuses_old_connection(case, tmp_path):
    j, s, sw, k, *_ = case
    from pathlib import Path

    old = Path(j.db_path)
    old.rename(tmp_path / "old-ledger.db")
    replacement = JournalStore(j.db_path)
    replacement.close()
    with pytest.raises(recovery.RecoveryRefused, match="identity changed"):
        recovery.preview(j, s, suspend_switch=sw, kill_switch=k)
    assert sw.is_engaged()


def test_completed_audit_commit_then_replaced_ledger_keeps_pending(case, tmp_path):
    from pathlib import Path

    j, _, sw, *_ = case
    connection = j.conn

    class ReplaceAfterCommit:
        def __init__(self):
            self.n = 0

        def __getattr__(self, name):
            return getattr(connection, name)

        def commit(self):
            connection.commit()
            self.n += 1
            if self.n == 2:
                Path(j.db_path).rename(tmp_path / "previous-ledger.db")
                # A distinct filesystem inode must never inherit the old open connection's proof.
                Path(j.db_path).write_bytes(b"replacement")

    j.conn = ReplaceAfterCommit()
    try:
        with pytest.raises(recovery.RecoveryRefused, match="identity changed"):
            apply(case)
    finally:
        j.conn = connection
    assert sw.is_engaged()
    assert shadow_marker.pending_path(sw.path).exists()
    assert len(events(j)) == 2


@pytest.mark.parametrize("field", ["created_at_utc", "observed_at_utc"])
def test_resealed_prepared_receipt_naive_timestamp_refused(case, monkeypatch, field):
    j, _, sw, *_ = case
    original = recovery._audit

    def stop(journal, phase, value):
        if phase == "completed":
            raise RuntimeError("interrupt")
        return original(journal, phase, value)

    with monkeypatch.context() as m:
        m.setattr(recovery, "_audit", stop)
        with pytest.raises(RuntimeError):
            apply(case)
    pending_path = shadow_marker.pending_path(sw.path)
    pending = json.loads(pending_path.read_text())
    row = events(j)[0]
    value = json.loads(row["detail_json"])
    if field == "created_at_utc":
        j.conn.execute(
            "UPDATE system_events SET created_at_utc=? WHERE id=?",
            ("2026-10-03T06:00:00", row["id"]),
        )
    else:
        value["observed_at_utc"] = "2026-10-03T06:00:00"
        pending["prepared_sha256"] = recovery.digest(value)
        pending_path.write_text(json.dumps(pending))
        j.conn.execute(
            "UPDATE system_events SET detail_json=? WHERE id=?",
            (json.dumps(value), row["id"]),
        )
    j.conn.commit()
    with pytest.raises(recovery.RecoveryRefused, match="timestamp"):
        view(case)
    assert sw.is_engaged() and pending_path.exists()


def test_research_lock_replaced_during_final_checks_retains_pending(case, monkeypatch):
    from pathlib import Path

    j, _, sw, *_ = case
    original = recovery.recovery_checks
    count = []
    proof = view(case)

    def change(*args, **kwargs):
        result = original(*args, **kwargs)
        count.append(True)
        if len(count) == 3:
            lock = Path(j.db_path + ".research.lock")
            lock.rename(lock.with_suffix(".old"))
            lock.write_text("replacement")
        return result

    monkeypatch.setattr(recovery, "recovery_checks", change)
    with pytest.raises(RuntimeError, match="inode changed"):
        apply(case, proof)
    assert sw.is_engaged() and shadow_marker.pending_path(sw.path).exists()
