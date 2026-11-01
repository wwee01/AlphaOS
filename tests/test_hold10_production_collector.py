"""Synthetic actual scheduler -> hooks -> pinned snapshot -> existing collector."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import socket
from zoneinfo import ZoneInfo

import pytest

from alphaos.research import hold10_production_collector as bridge
from alphaos.research import hold10_expected_runtime as er
from alphaos.research import hold10_collection_audit as audit
from alphaos.research import hold10_designated_registry as designated
from alphaos.research import hold10_policy_binding as policy
from alphaos.research import hold10_prospective as v1
from alphaos.research.runtime_identity import observe_runtime
from alphaos.scheduler import jobs
from runtime_identity_fixture import synthetic_transport_scan
from test_hold10_policy_binding import fixture
from test_hold10_runtime_collection_path import MemorySink


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("network forbidden")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.delenv(bridge.CONFIG_ENV, raising=False)
    monkeypatch.delenv(bridge.HASH_ENV, raising=False)


def _market(o, monkeypatch):
    old = o.market.get_snapshot
    def get(symbol):
        row = old(symbol)
        row.update(last_price=100., prev_close=95., bid=99.99, ask=100.01,
            spread=.02, spread_pct=.0002, change_pct=.05, rel_volume=2.,
            volume=2000000, avg_volume=1000000, dollar_volume=200000000.,
            bar_open=98., bar_high=101., bar_low=97., bar_close=100., is_mock=False)
        return row
    monkeypatch.setattr(o.market, "get_snapshot", get)
    monkeypatch.setattr(o.market, "get_snapshots", lambda symbols: [get(s) for s in symbols])


def configured_producer(tmp_path, monkeypatch):
    import alphaos.orchestrator as om
    import alphaos.scanner.candidate_scanner as sm
    monkeypatch.setattr(om, "DEFAULT_UNIVERSE", ["AAPL", "MSFT"])
    monkeypatch.setattr(sm, "DEFAULT_UNIVERSE", ["AAPL", "MSFT"])
    root, _, q, _, clock = fixture(tmp_path, monkeypatch)
    code = Path(__file__).resolve().parents[1]
    monkeypatch.chdir(code)
    warm, _, _ = synthetic_transport_scan(monkeypatch, MemorySink(), prompt_version="v4",
        before_scan=lambda o: _market(o, monkeypatch))
    evaluation = warm.journal.query("SELECT * FROM openai_evaluations")[0]
    classifier = warm.journal.query("SELECT * FROM candidate_labels")[0]
    reference = warm.journal.query("SELECT * FROM shadow_baseline_decisions WHERE rule_version='threshold_v1_hold10'")[0]
    q["protocol"]["policies"]["tested"]["identity"] = {
        k: evaluation[k] for k in q["protocol"]["policies"]["tested"]["identity"]}
    q["protocol"]["policies"]["reference"]["setup_card_id"] = reference["setup_card_id"]
    q["combined_policy"]["classifier_identity"] = {k: classifier[k] for k in q["combined_policy"]["classifier_identity"]}
    q["combined_policy"]["configuration"] = {k: getattr(warm.settings, k) for k in q["combined_policy"]["configuration"]}
    profile = er.runtime_profile(observe_runtime(warm))
    warm.close()
    holder = {}
    class Ready(BaseException):
        pass
    def ready(o):
        _market(o, monkeypatch)
        holder["orch"] = o
        raise Ready()
    with pytest.raises(Ready):
        synthetic_transport_scan(monkeypatch, None, db_path=str(root/"producer.sqlite"),
            prompt_version="v4", before_scan=ready)
    orch = holder["orch"]
    monkeypatch.setattr(orch.kill_switch, "is_engaged", lambda: False)
    now = datetime.now(timezone.utc)
    start = now.astimezone(ZoneInfo("America/New_York")).replace(hour=0, minute=0, second=0, microsecond=0)
    q.update(start_at_utc=start.isoformat(), end_at_utc=(start+timedelta(days=28)).isoformat(),
        close_at_utc=(start+timedelta(days=50)).isoformat())
    clock[0] = start-timedelta(days=1)
    binding = policy.create_policy_binding(q, expected_contract_sha256=v1.sha(q),
        code_root=code, configuration=q["combined_policy"]["configuration"])
    environment = er.interpreter_identity()
    manifest = er.create_manifest(q, binding, reviewed_profile=profile,
        expected_profile_sha256=v1.sha(profile), reviewed_environment=environment,
        expected_environment_sha256=v1.sha(environment), expected_contract_sha256=v1.sha(q),
        expected_binding_sha256=v1.sha(binding))
    api = designated.DesignatedHold10(root, dry_run=True)
    designation = api.register(q, binding, expected_contract_sha256=v1.sha(q), expected_binding_sha256=v1.sha(binding))
    pin = designation["designation_sha256"]
    identity = audit.CollectionAuditJournal.create(root/"audit.sqlite", designation_sha256=pin, enabled=True)
    journal = audit.CollectionAuditJournal(root/"audit.sqlite", expected_identity_sha256=identity["identity_sha256"], enabled=True)
    frozen = er.freeze_manifest(api, journal, manifest, expected_manifest_sha256=v1.sha(manifest), expected_designation_sha256=pin)
    (root/bridge.SNAPSHOT_DIRECTORY).mkdir()
    (root/bridge.SNAPSHOT_DIRECTORY/".allocation.lock").touch(mode=0o600)
    info = (root/"producer.sqlite").stat()
    config = {"version": bridge.VERSION, "workspace": str(root), "designation_sha256": pin,
        "audit_path": str(journal.path), "audit_identity_sha256": identity["identity_sha256"],
        "manifest_receipt_sha256": frozen["receipt_sha256"], "source_database": str(root/"producer.sqlite"),
        "source_device": info.st_dev, "source_inode": info.st_ino, "snapshot_byte_budget": 64*1024*1024,
        "snapshot_file_byte_limit": 16*1024*1024,
        "closure_not_before_utc": (start+timedelta(days=48)).isoformat()}
    path = root/"collector.json"
    path.write_text(json.dumps(config, sort_keys=True))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setenv(bridge.CONFIG_ENV, str(path))
    monkeypatch.setenv(bridge.HASH_ENV, digest)
    monkeypatch.setattr(v1, "_now", lambda: datetime.now(timezone.utc))
    return orch, api, journal, path, digest, config


def test_actual_job_collection_reopen_and_exact_resume(tmp_path, monkeypatch):
    orch, api, journal, path, digest, config = configured_producer(tmp_path, monkeypatch)
    try:
        result = jobs.run_scan_job(orch, None)
        assert result["status"] == "completed"
        collection = result["prospective_collection"]
        assert collection["status"] == "captured", collection
        assert collection["expected_runtime_agreement"] is True
        assert orch.runtime_identity_sink is None
        state = api.read(expected_designation_sha256=config["designation_sha256"])["state"]
        assert state["full_candidate_count"] == 2
        before = journal.read()
        assert sum(e["kind"] == "runtime_identity" for e in before["events"]) == 4
        request = next((path.parent/bridge.SNAPSHOT_DIRECTORY).glob("*.request.json"))
        assert bridge.resume(path, digest, request)["status"] == "already_recorded"
        assert journal.read() == before
        assert state == api.read(expected_designation_sha256=config["designation_sha256"])["state"]
    finally:
        orch.close()


def test_default_off_scheduler_has_no_collector_import_io_or_sink(monkeypatch):
    import builtins
    from types import SimpleNamespace
    original_import = builtins.__import__
    def checked_import(name, *args, **kwargs):
        assert "hold10_production_collector" not in name
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", checked_import)
    def no_io(*args, **kwargs):
        raise AssertionError("disabled collector performed file IO")
    monkeypatch.setattr(Path, "read_bytes", no_io)
    monkeypatch.setattr(bridge.os, "open", no_io)
    summary = SimpleNamespace(scan_batch_id="scan_fixture", as_dict=lambda: {"candidates": 2})
    orch = SimpleNamespace(kill_switch=SimpleNamespace(is_engaged=lambda: False),
        settings=None, journal=None, run_scan_once=lambda **kwargs: summary)
    monkeypatch.setattr(jobs.cost_guard, "check_scan_budget", lambda *args: (True, ""))
    monkeypatch.setattr(jobs, "_alert_new_pending_approvals", lambda *args: None)
    result = jobs.run_scan_job(orch, None)
    assert result == {"status": "completed", "kill_switch_engaged": False,
        "cost_cap_exceeded": False, "scan_summary": {"candidates": 2}}
    assert not hasattr(orch, "runtime_identity_sink")


def test_config_mutation_during_encoding_rolls_back(tmp_path, monkeypatch):
    from alphaos.research import hold10_prospective_compact as compact
    orch, api, journal, path, _, config = configured_producer(tmp_path, monkeypatch)
    original = compact.pack_observation
    def changed(value):
        packed = original(value)
        path.write_text("{}")
        return packed
    monkeypatch.setattr(compact, "pack_observation", changed)
    try:
        result = jobs.run_scan_job(orch, None)
        assert result["status"] == "completed"
        assert not result["prospective_collection"]["expected_runtime_agreement"]
        assert not api.read(expected_designation_sha256=config["designation_sha256"])["state"]["captures"]
        assert any(e["kind"] == "collection_prepared" for e in journal.read()["events"])
    finally:
        orch.close()


def _lifecycle_clock(monkeypatch, at):
    # Both original clock boundaries must move together in this explicitly
    # synthetic fixture; changing only v1 makes the enrollment guard correctly
    # reject its future as_of timestamp against the unshifted wall clock.
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return at.astimezone(tz) if tz else at.replace(tzinfo=None)
    monkeypatch.setattr(v1, "_now", lambda: at)
    monkeypatch.setattr(v1.e, "datetime", Clock)


def test_enrollment_end_no_new_hooks_then_one_reviewed_closure(tmp_path, monkeypatch):
    orch, api, journal, path, digest, config = configured_producer(tmp_path, monkeypatch)
    try:
        first = jobs.run_scan_job(orch, None)
        assert first["prospective_collection"]["status"] == "captured"
        count = len(journal.read()["events"])
        before_files = sorted(p.name for p in (path.parent/bridge.SNAPSHOT_DIRECTORY).iterdir())
        with pytest.raises(ValueError, match="reviewed_closure_boundary_not_reached"):
            bridge.closure_checkpoint(path, digest)
        assert sorted(p.name for p in (path.parent/bridge.SNAPSHOT_DIRECTORY).iterdir()) == before_files
        state = api.read(expected_designation_sha256=config["designation_sha256"])["state"]
        end = v1.c._dt(state["registration"]["payload"]["contract"]["end_at_utc"])
        _lifecycle_clock(monkeypatch, end)
        second = jobs.run_scan_job(orch, None)
        assert second["status"] == "completed"
        assert second["prospective_collection"]["reason"] == "enrollment_closed_checkpoint_required"
        assert len(journal.read()["events"]) == count
        assert sorted(p.name for p in (path.parent/bridge.SNAPSHOT_DIRECTORY).iterdir()) == before_files
        _lifecycle_clock(monkeypatch, v1.c._dt(config["closure_not_before_utc"]))
        closure = bridge.closure_checkpoint(path, digest)
        assert closure["status"] == "refused_expected_runtime_agreement"
        # Fixture's second actual producer scan used real current timestamps
        # despite synthetic lifecycle clock, so it is retained but lacks runtime.
        # Never clip it out to turn this closure into a success.
        final = journal.read()
        prepared = [e for e in final["events"] if e["kind"] == "collection_prepared"][-1]["payload"]
        assert prepared["policy_audit"]["core_candidate_count"] == 4
        again = bridge.closure_checkpoint(path, digest)
        assert again["status"] == "already_recorded" and not again["expected_runtime_agreement"]
        assert journal.read() == final
    finally:
        orch.close()


def test_closure_retains_terminal_scan_job_metadata(tmp_path, monkeypatch):
    orch, _, journal, path, digest, config = configured_producer(tmp_path, monkeypatch)
    try:
        from alphaos.scheduler.job_runner import JobRunner
        result = JobRunner(orch, role="research").run_job("scan", "scan:synthetic-bridge")
        assert result["prospective_collection"]["status"] == "captured"
        import sqlite3
        first_snapshot = next((path.parent/bridge.SNAPSHOT_DIRECTORY).glob("scan_*.sqlite"))
        with sqlite3.connect(first_snapshot.as_uri()+"?mode=ro&immutable=1", uri=True) as db:
            assert db.execute("SELECT status,finished_at_utc FROM job_runs WHERE lock_key='scan:synthetic-bridge'").fetchone() == ("started", None)
        assert orch.journal.one("SELECT status FROM job_runs WHERE lock_key='scan:synthetic-bridge'")["status"] == "completed"
        _lifecycle_clock(monkeypatch, v1.c._dt(config["closure_not_before_utc"]))
        closure = bridge.closure_checkpoint(path, digest)
        assert closure["status"] == "captured"
        import sqlite3
        snapshot = next((path.parent/bridge.SNAPSHOT_DIRECTORY).glob("closure_*.sqlite"))
        with sqlite3.connect(snapshot.as_uri()+"?mode=ro&immutable=1", uri=True) as db:
            assert db.execute("SELECT status FROM job_runs WHERE lock_key='scan:synthetic-bridge'").fetchone()[0] == "completed"
        before = journal.read()
        assert bridge.closure_checkpoint(path, digest)["status"] == "already_recorded"
        assert journal.read() == before
    finally:
        orch.close()



@pytest.mark.parametrize("case", ["missing_file", "wrong_hash", "missing_manifest", "wrong_designation", "closed"])
def test_invalid_configuration_never_claims_agreement_or_blocks_scan(tmp_path, monkeypatch, case):
    orch, api, journal, path, digest, config = configured_producer(tmp_path, monkeypatch)
    if case == "missing_file":
        monkeypatch.setenv(bridge.CONFIG_ENV, str(path.parent/"absent.json"))
    elif case == "wrong_hash":
        monkeypatch.setenv(bridge.HASH_ENV, "0"*64)
    elif case in ("missing_manifest", "wrong_designation"):
        config["manifest_receipt_sha256" if case == "missing_manifest" else "designation_sha256"] = "0"*64
        path.write_text(json.dumps(config))
        monkeypatch.setenv(bridge.HASH_ENV, hashlib.sha256(path.read_bytes()).hexdigest())
    else:
        api.cancel(expected_designation_sha256=config["designation_sha256"], expected_previous_capture_sha256=None)
    try:
        before = journal.read()
        result = jobs.run_scan_job(orch, None)
        assert result["status"] == "completed"
        assert result["prospective_collection"]["status"] == "blocked"
        assert result["prospective_collection"]["expected_runtime_agreement"] is False
        assert str(path) not in json.dumps(result["prospective_collection"])
        assert journal.read() == before
        assert orch.runtime_identity_sink is None
    finally:
        orch.close()


def test_kill_skip_is_zero_touch_even_with_bad_config(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setenv(bridge.CONFIG_ENV, "/MUST-NOT-READ")
    orch = SimpleNamespace(kill_switch=SimpleNamespace(is_engaged=lambda: True))
    result = jobs.run_scan_job(orch, None)
    assert result["status"] == "skipped" and "prospective_collection" not in result


def test_degraded_scan_retains_incomplete_population(tmp_path, monkeypatch):
    orch, api, journal, path, digest, config = configured_producer(tmp_path, monkeypatch)
    monkeypatch.setattr(jobs.cost_guard, "check_scan_budget", lambda *args: (False, "synthetic budget"))
    try:
        result = jobs.run_scan_job(orch, None)
        assert result["status"] == "completed" and result["degraded"] is True
        assert result["prospective_collection"]["status"] == "refused_expected_runtime_agreement"
        prepared = [e for e in journal.read()["events"] if e["kind"] == "collection_prepared"][-1]["payload"]
        assert prepared["policy_audit"]["core_candidate_count"] == 2
        assert prepared["expected_runtime_audit"]["complete_count"] == 0
        assert not api.read(expected_designation_sha256=config["designation_sha256"])["state"]["captures"]
    finally:
        orch.close()


def test_scan_exception_propagates_and_sink_restores(tmp_path, monkeypatch):
    orch, _, journal, _, _, _ = configured_producer(tmp_path, monkeypatch)
    def broken(*args, **kwargs):
        raise RuntimeError("synthetic scan failure")
    monkeypatch.setattr(orch.scanner, "scan", broken)
    try:
        with pytest.raises(RuntimeError, match="synthetic scan failure"):
            jobs.run_scan_job(orch, None)
        assert orch.runtime_identity_sink is None
        assert journal.read()["events"]
    finally:
        orch.close()


def test_config_changed_during_scan_refuses_collection(tmp_path, monkeypatch):
    orch, api, _, path, _, config = configured_producer(tmp_path, monkeypatch)
    original = orch.run_scan_once
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        path.write_text("{}")
        return result
    monkeypatch.setattr(orch, "run_scan_once", changed)
    try:
        result = jobs.run_scan_job(orch, None)
        assert result["status"] == "completed"
        assert result["prospective_collection"]["status"] == "blocked"
        assert not result["prospective_collection"]["expected_runtime_agreement"]
        assert not api.read(expected_designation_sha256=config["designation_sha256"])["state"]["captures"]
        assert orch.runtime_identity_sink is None
    finally:
        orch.close()



@pytest.mark.parametrize("case", ["normal", "degraded", "missing", "foreign", "old", "failed", "before_job", "reused"])
def test_runner_links_only_current_exact_completed_scan(orchestrator, monkeypatch, case):
    from alphaos.scheduler import job_runner as jr
    orch = orchestrator
    original = jr._JOB_FUNCS["scan"]
    if case == "degraded":
        monkeypatch.setattr(jobs.cost_guard, "check_scan_budget", lambda *args: (False, "synthetic budget"))
    prior = original(orch, None) if case == "old" else None
    historical = orch.journal.query("SELECT * FROM job_runs ORDER BY id")
    def work(o, r):
        result = prior or original(o, r)
        if case == "missing":
            result["scan_summary"] = None
        elif case == "foreign":
            result["scan_summary"]["scheduler_run_id"] = "schr_foreign"
        elif case == "failed":
            result["status"] = "failed"
        elif case == "before_job":
            o.journal.conn.execute("UPDATE scan_batches SET started_at_utc='2000-01-01T00:00:00Z' WHERE scan_batch_id=?",
                (result["scan_summary"]["scan_batch_id"],))
            o.journal.conn.commit()
        elif case == "reused":
            o.journal.insert("job_runs", {"job_run_id": "other", "job_type": "scan", "status": "completed",
                "started_at_utc": "2000-01-01T00:00:00Z", "started_at_sgt": "2000-01-01T08:00:00+08:00",
                "scheduler_run_id": result["scan_summary"]["scheduler_run_id"]})
        return result
    monkeypatch.setitem(jr._JOB_FUNCS, "scan", work)
    runner = jr.JobRunner(orch, role="research")
    result = runner.run_job("scan", "metadata-current")
    assert result["status"] == ("failed" if case == "failed" else "completed")
    row = orch.journal.one("SELECT * FROM job_runs WHERE lock_key='metadata-current'")
    if case in ("normal", "degraded"):
        assert row["scheduler_run_id"] == result["scan_summary"]["scheduler_run_id"]
        assert runner.run_job("scan", "metadata-current")["reason"] == "duplicate_lock"
        assert row == orch.journal.one("SELECT * FROM job_runs WHERE id=?", (row["id"],))
    else:
        assert row["scheduler_run_id"] is None
    assert all(orch.journal.one("SELECT * FROM job_runs WHERE id=?", (x["id"],)) == x for x in historical)


def test_non_scan_terminal_preserves_callback_link_and_open_transaction(orchestrator, monkeypatch):
    from alphaos.scheduler import job_runner as jr
    def work(o, r):
        o.journal.conn.execute("UPDATE job_runs SET scheduler_run_id='existing-callback-link' WHERE status='started'")
        return {"status": "completed"}
    monkeypatch.setitem(jr._JOB_FUNCS, "atr_update", work)
    assert jr.JobRunner(orchestrator).run_job("atr_update", "preserve-nonscan")["status"] == "completed"
    row = orchestrator.journal.one("SELECT * FROM job_runs WHERE lock_key='preserve-nonscan'")
    assert row["scheduler_run_id"] == "existing-callback-link"
    assert not orchestrator.journal.conn.in_transaction


def test_scan_terminal_preserves_open_callback_transaction(orchestrator, monkeypatch):
    from alphaos.scheduler import job_runner as jr
    def work(o, r):
        o.journal.conn.execute("UPDATE job_runs SET trigger_source='test-marker' WHERE status='started'")
        return {"status": "completed", "scan_summary": None}
    monkeypatch.setitem(jr._JOB_FUNCS, "scan", work)
    assert jr.JobRunner(orchestrator).run_job("scan", "preserve-open-scan")["status"] == "completed"
    row = orchestrator.journal.one("SELECT * FROM job_runs WHERE lock_key='preserve-open-scan'")
    assert row["trigger_source"] == "test-marker" and row["scheduler_run_id"] is None
    assert not orchestrator.journal.conn.in_transaction
