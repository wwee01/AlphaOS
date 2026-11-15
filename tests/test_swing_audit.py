"""Isolated audit-only tests. Synthetic clocks/receipts, no providers or keys."""

from copy import deepcopy
from datetime import datetime, timezone, timedelta
import gzip
import json
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import socket
import urllib.request

import pytest

from alphaos.research import swing_audit as audit, swing_session_evidence as sessions
from alphaos.research import data_freshness_trial as trial, swing_data_policy as v2
from alphaos.research import swing_audit_worker as hook

ROOT = Path(__file__).resolve().parents[1]


def no_call(*args, **kwargs):
    raise AssertionError("Unexpected network or execution call")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", no_call)
    monkeypatch.setattr(urllib.request, "urlopen", no_call)


@pytest.fixture
def protocol():
    return trial.load_protocol(
        ROOT / "docs/research/three-session-freshness-protocol-20261014.json"
    )


def receipt(day="2026-10-20", written="13:38:00", close="16:00"):
    return {
        "written_at_utc": day + "T" + written + "Z",
        "error": None,
        "version": "paper_observer_v1",
        "broker_verified": True,
        "observed_at_utc": day + "T13:37:55Z",
        "completed_at_utc": day + "T13:37:58Z",
        "market_calendar": {
            "date": day,
            "open": "09:30",
            "close": close,
            "source": "broker_calendar",
        },
        "report_sha256": "a" * 64,
        "receipts": [
            {
                "path": "/v2/calendar",
                "http_status": 200,
                "params": {"start": day, "end": day},
                "body_sha256": "b" * 64,
                "request_at_utc": day + "T13:37:56Z",
                "received_at_utc": day + "T13:37:57Z",
            }
        ],
    }


@pytest.fixture
def capture():
    p = ROOT / "tests/fixtures/swing_freshness_forward_20261015_16.json.gz"
    c = json.loads(gzip.decompress(p.read_bytes()))["captures"][0]
    # Synthetic date shift only; never credited as a natural observation.
    return json.loads(json.dumps(c).replace("2026-10-15", "2026-10-20"))


@pytest.mark.parametrize(
    "age,expected", [(900, True), (900.000001, False), (-1, False)]
)
def test_calendar_lease_and_no_future_receipt(age, expected):
    r = receipt()
    at = datetime(2026, 10, 20, 13, 37, 57, tzinfo=timezone.utc) + timedelta(
        seconds=age
    )
    if age >= 0:
        r["written_at_utc"] = r["completed_at_utc"] = "2026-10-20T13:37:57Z"
    bounds, status = sessions.bounds_at([r], "2026-10-20", at.isoformat())
    assert (bounds is not None) is expected


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "newest_failure",
        "wrong_date",
        "wrong_params",
        "wrong_source",
        "duplicate",
        "naive",
        "bad_hash",
        "closed",
        "bad_close",
        "future_completion",
    ],
)
def test_calendar_fail_closed(kind):
    r = receipt()
    records = [r]
    if kind == "missing":
        records = []
    elif kind == "newest_failure":
        later = deepcopy(r)
        later["written_at_utc"] = "2026-10-20T13:39:00Z"
        later["broker_verified"] = False
        records.append(later)
    elif kind == "wrong_date":
        r["market_calendar"]["date"] = "2026-10-19"
    elif kind == "wrong_params":
        r["receipts"][0]["params"]["start"] = "2026-10-19"
    elif kind == "wrong_source":
        r["market_calendar"]["source"] = "local_calendar_full_closures_only"
    elif kind == "duplicate":
        r["receipts"].append(deepcopy(r["receipts"][0]))
    elif kind == "naive":
        r["receipts"][0]["received_at_utc"] = "2026-10-20T13:37:57"
    elif kind == "bad_hash":
        r["report_sha256"] = "unknown"
    elif kind == "closed":
        r["market_calendar"]["open"] = r["market_calendar"]["close"] = None
    elif kind == "bad_close":
        r["market_calendar"]["close"] = "25:00"
    else:
        r["completed_at_utc"] = "2026-10-20T13:50:00Z"
    b, reason = sessions.bounds_at(records, "2026-10-20", "2026-10-20T13:40:00Z")
    assert b is None and reason != "verified"
    if kind == "closed":
        assert reason == "verified_closed_day"


def test_source_boundary_dst_and_close_exclusive():
    r = receipt("2026-11-27", written="17:58:00", close="13:00")
    r["observed_at_utc"] = "2026-11-27T17:57:55Z"
    r["completed_at_utc"] = "2026-11-27T17:57:58Z"
    r["receipts"][0].update(
        request_at_utc="2026-11-27T17:57:56Z", received_at_utc="2026-11-27T17:57:57Z"
    )
    b, status = sessions.bounds_at([r], "2026-11-27", "2026-11-27T17:59:59Z")
    assert b is not None
    assert status == "verified" and b["close_at_utc"] == "2026-11-27T18:00:00+00:00"
    before = v2._stamp("2026-11-27T17:59:59.999999999Z")
    close = v2._stamp("2026-11-27T18:00:00Z")
    assert before is not None and close is not None
    assert v2._within_session(before, b)
    assert not v2._within_session(close, b)


def test_catalog_is_private_and_allowlisted(tmp_path):
    directory = tmp_path / "observer"
    directory.mkdir(mode=0o700)
    r = receipt()
    r["broker_receipts"] = r.pop("receipts")
    r["account"] = {"id": "SECRET_SENTINEL"}
    path = directory / "paper-observer-poll-20261020T133800000000Z-1.json"
    path.write_text(json.dumps(r))
    path.chmod(0o600)
    catalog = sessions.load_catalog(directory, "2026-10-20")
    assert len(catalog) == 1 and "SECRET_SENTINEL" not in json.dumps(catalog)
    path.chmod(0o644)
    assert (
        sessions.load_catalog(directory, "2026-10-20")[0]["error"]
        == "observer_report_invalid"
    )


def test_future_capture_has_same_price_valid_counts_and_no_execution(capture, protocol):
    before = deepcopy(capture)
    result = audit.evaluate(capture, protocol, [receipt()], "synthetic-v3")
    assert (
        result["research_eligible"] == 435 and result["paired_baseline_eligible"] == 415
    )
    assert result["status"] == "completed" and result["integrity_ok"]
    assert result["execution_authority"] is False and result["hold10_enrolled"] is False
    assert capture == before
    assert not {"candidate_id", "proposal_id", "order_id"} & result.keys()


@pytest.mark.parametrize(
    "kind",
    [
        "stale_trade",
        "fallback",
        "bad_quote",
        "bad_price",
        "wrong_feed",
        "mock",
        "binding",
        "duplicate",
    ],
)
def test_v3_never_drops_non_session_rejections(capture, protocol, kind):
    o = capture["chunks"][0]["observations"][0]
    s = capture["snapshots"][o["snapshot_id"]]
    p = o["source_provenance"]
    if kind == "stale_trade":
        p["parts"]["latestTrade"]["timestamp"] = p[
            "normalized_last_price_timestamp"
        ] = "2026-10-20T13:30:00Z"
    elif kind == "fallback":
        p["normalized_last_price_source"] = "dailyBar"
    elif kind == "bad_quote":
        p["parts"]["latestQuote"]["timestamp"] = None
    elif kind == "bad_price":
        s["last_price"] = 0
    elif kind == "wrong_feed":
        s["feed"] = "sip"
    elif kind == "mock":
        s["is_mock"] = 1
    elif kind == "binding":
        p["raw_latest_trade_price"] = 101
    else:
        capture["chunks"][0]["observations"][-1] = deepcopy(o)
    result = audit.evaluate(capture, protocol, [receipt()], "synthetic-v3")
    assert result["research_eligible"] < 435
    assert result["denominator"] == len(result["rows"]) == 500


def test_missing_calendar_blocks_capture_and_keeps_all_names(capture, protocol):
    result = audit.evaluate(capture, protocol, [], "synthetic-v3")
    assert result["research_eligible"] == 0 and result["status"] == "blocked_calendar"
    assert result["denominator"] == 500


def database(path, capture):
    c = sqlite3.connect(path)
    c.executescript("""CREATE TABLE job_runs(id,status,scheduler_run_id,result_summary_json,started_at_utc,job_type,trigger_source,lock_key);
      CREATE TABLE scan_batches(id,scan_batch_id,scheduler_run_id,started_at_utc,completed_at_utc,status);
      CREATE TABLE system_events(id,category,created_at_utc,detail_json);""")
    columns = trial.SNAPSHOT_COLUMNS.split(",")
    c.execute("CREATE TABLE price_snapshots(" + trial.SNAPSHOT_COLUMNS + ")")
    for i, trigger, scan in [
        (1, "cli", "manual"),
        (2, "scheduler", capture["scan_id"]),
        (3, "scheduler", "better_retry"),
    ]:
        c.execute(
            "INSERT INTO job_runs VALUES(?,?,?,?,?,?,?,?)",
            (
                i,
                "completed",
                str(i),
                json.dumps({"scan_summary": {"scan_batch_id": scan}}),
                capture["started_at_utc"],
                "scan",
                trigger,
                capture["lock_key"],
            ),
        )
        c.execute(
            "INSERT INTO scan_batches VALUES(?,?,?,?,?,?)",
            (
                i,
                scan,
                str(i),
                capture["started_at_utc"],
                capture["completed_at_utc"],
                "completed",
            ),
        )
    for i, ch in enumerate(capture["chunks"]):
        c.execute(
            "INSERT INTO system_events VALUES(?,?,?,?)",
            (i, "shadow_collection", capture["completed_at_utc"], json.dumps(ch)),
        )
    for s in capture["snapshots"].values():
        c.execute(
            "INSERT INTO price_snapshots VALUES("
            + ",".join("?" for _ in columns)
            + ")",
            tuple(s.get(k) for k in columns),
        )
    c.commit()
    c.close()


def configuration(tmp_path):
    observer = tmp_path / "observer"
    observer.mkdir(mode=0o700)
    output = tmp_path / "audit"
    output.mkdir(mode=0o700)
    cfg = {
        "version": audit.ACTIVATION_VERSION,
        "enabled": True,
        "policy_id": audit.POLICY_ID,
        "cohort_id": "synthetic-v3",
        "start_at_utc": "2026-10-20T13:30:00+00:00",
        "end_at_utc": "2026-10-20T20:45:00+00:00",
        "dates": ["2026-10-20"],
        "source_sha256": audit.source_identity(ROOT),
        "observer_directory": str(observer),
        "audit_directory": str(output),
        "calendar_lease_seconds": 900,
        "hold10_reference_revision": "cd4eadbe16d83fb7e86d3f09878523c875196fe2",
    }
    p = tmp_path / "swing-audit-activation.json"
    p.write_text(json.dumps(cfg))
    p.chmod(0o600)
    return p, cfg


def test_readonly_first_capture_selection_and_activation_start(
    tmp_path, capture, protocol
):
    db = tmp_path / "source.db"
    database(db, capture)
    p, cfg = configuration(tmp_path)
    before = db.read_bytes()
    rows = audit.read_captures(db, cfg, protocol)
    assert len(rows) == 1 and rows[0]["selected_job_id"] == 2
    assert db.read_bytes() == before
    cfg["start_at_utc"] = "2026-10-20T14:00:00+00:00"
    assert not audit.read_captures(db, cfg, protocol)


def test_run_dedup_persists_blocked_status_and_rejects_tampering(
    tmp_path, capture, monkeypatch
):
    db = tmp_path / "source.db"
    database(db, capture)
    p, cfg = configuration(tmp_path)
    # No calendar evidence: this is durably blocked, not healthy inactivity.
    now = datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc)
    first = audit.run(db, p, now=now)
    assert first["status"] == "blocked"
    files = list(Path(cfg["audit_directory"]).glob("capture-*.json"))
    assert len(files) == 1
    old = files[0].read_bytes()
    second = audit.run(db, p, now=now + timedelta(seconds=1))
    assert (
        second["status"] == "blocked"
        and second["new_captures"] == 0
        and files[0].read_bytes() == old
    )
    data = json.loads(old)
    data["research_eligible"] = 500
    files[0].write_text(json.dumps(data))
    with pytest.raises(ValueError, match="identity_mismatch"):
        audit.run(db, p, now=now + timedelta(seconds=2))


def test_disable_expiry_source_pin_and_missing_window(tmp_path, capture):
    p, cfg = configuration(tmp_path)
    db = tmp_path / "source.db"
    database(db, capture)
    assert (
        audit.run(db, p, now=datetime(2026, 10, 21, tzinfo=timezone.utc))["status"]
        == "outside_bounded_cohort"
    )
    cfg["enabled"] = False
    p.write_text(json.dumps(cfg))
    assert (
        audit.run(db, p, now=datetime(2026, 10, 20, 14, tzinfo=timezone.utc))["status"]
        == "disabled"
    )
    cfg["enabled"] = True
    cfg["source_sha256"]["alphaos/research/swing_audit.py"] = "0" * 64
    p.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="pin_mismatch"):
        audit.run(db, p, now=datetime(2026, 10, 20, 14, tzinfo=timezone.utc))


def test_atomic_exclusive_audits(tmp_path):
    tmp_path.chmod(0o700)
    p, created = audit.exclusive_json(tmp_path, "report.json", {"ok": True})
    assert created
    assert audit.exclusive_json(tmp_path, "report.json", {"ok": True}) == (p, False)
    with pytest.raises(ValueError, match="conflict"):
        audit.exclusive_json(tmp_path, "report.json", {"ok": False})
    assert json.loads(p.read_bytes()) == {"ok": True}


def test_worker_inactive_reserve_and_commands(tmp_path, monkeypatch):
    orch = SimpleNamespace(
        settings=SimpleNamespace(db_path=str(tmp_path / "db")), journal=object()
    )
    assert hook.maybe_run(orch) is None
    p = tmp_path / "swing-audit-activation.json"
    p.write_text("{}")
    monkeypatch.setattr(hook, "admission", lambda *x: {"allowed": False})
    assert hook.maybe_run(orch)["status"] == "deferred_scan_reserve"
    monkeypatch.setattr(hook, "admission", lambda *x: {"allowed": True})

    def runner(command):
        assert "alphaos.research.swing_audit" in command
        assert "--activation" in command and "--db" in command
        return {"status": "fixture_only"}

    assert hook.maybe_run(orch, runner=runner)["status"] == "fixture_only"


def test_worker_child_drops_parent_secrets(tmp_path, monkeypatch):
    (tmp_path / "swing-audit-activation.json").write_text("{}")
    orch = SimpleNamespace(
        settings=SimpleNamespace(db_path=str(tmp_path / "db")), journal=object()
    )
    monkeypatch.setattr(hook, "admission", lambda *x: {"allowed": True})
    monkeypatch.setenv("FAKE_API_KEY", "SECRET_SENTINEL")

    class Child:
        def __init__(self, *a, **k):
            assert "FAKE_API_KEY" not in k["env"] and set(k["env"]) == {
                "PATH",
                "PYTHONDONTWRITEBYTECODE",
            }

        def communicate(self, timeout):
            return b'{"status":"waiting"}', None

    monkeypatch.setattr(hook.subprocess, "Popen", Child)
    assert hook.maybe_run(orch)["status"] == "waiting"


def test_existing_research_hook_never_runs_for_monitor(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from alphaos.scheduler import worker
    from alphaos import orchestrator

    settings = SimpleNamespace(db_path=str(tmp_path / "source.db"), monitor_deadman_enabled=False)

    @contextmanager
    def owned(*args):
        yield True

    class Orch:
        def __init__(self, settings):
            self.settings, self.journal = settings, object()

        def close(self):
            pass

    monkeypatch.setattr(worker, "worker_ownership", owned)
    monkeypatch.setattr(orchestrator, "Orchestrator", Orch)
    monkeypatch.setattr(
        worker, "JobRunner", lambda *a, **k: SimpleNamespace(run_due_jobs=lambda: [])
    )
    monkeypatch.setattr(
        worker, "monitor_health", lambda *a: {"ok": True, "entry_ready": True}
    )
    calls = []

    def blocked_audit(orch):
        calls.append(True)
        return {"status": "blocked"}

    monkeypatch.setattr(hook, "maybe_run", blocked_audit)
    result, code = worker.run_role("monitor", settings)
    assert code == 0 and not calls and "swing_availability_audit" not in result
    result, code = worker.run_role("research", settings)
    assert code == 0 and calls == [True]
    assert result["swing_availability_audit"]["status"] == "blocked"


def test_timeout_signals_only_own_child_group(tmp_path, monkeypatch):
    (tmp_path / "swing-audit-activation.json").write_text("{}")
    orch = SimpleNamespace(
        settings=SimpleNamespace(db_path=str(tmp_path / "db")), journal=object()
    )
    monkeypatch.setattr(hook, "admission", lambda *a: {"allowed": True})
    signals = []

    class Child:
        pid = 424242
        calls = 0

        def __init__(self, *a, **k):
            assert k["start_new_session"] is True

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                assert timeout == 15
                raise hook.subprocess.TimeoutExpired("fixture", 15)
            return b"", None

    monkeypatch.setattr(hook.subprocess, "Popen", Child)
    monkeypatch.setattr(hook.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    assert hook.maybe_run(orch)["status"] == "timed_out"
    assert signals == [(424242, hook.signal.SIGKILL)]


def test_capture_source_and_legacy_files_are_not_mutated(tmp_path, capture, protocol):
    path = tmp_path / "source.db"
    database(path, capture)
    before = path.read_bytes()
    cfg_path, cfg = configuration(tmp_path)
    audit.run(path, cfg_path, now=datetime(2026, 10, 20, 14, tzinfo=timezone.utc))
    assert path.read_bytes() == before
    # Audit read path contains no SQL mutation operations or JournalStore init.
    source = Path(audit.__file__).read_text()
    assert "mode=ro" in source and "PRAGMA query_only=ON" in source
    assert not any(
        x in source
        for x in ("INSERT INTO", "UPDATE universe_days", "JournalStore", "Orchestrator")
    )


def test_conflicting_same_instant_calendar_receipts_fail_closed():
    first = receipt()
    second = deepcopy(first)
    second["market_calendar"]["close"] = "13:00"
    bounds, status = sessions.bounds_at(
        [first, second], "2026-10-20", "2026-10-20T13:40:00Z"
    )
    assert bounds is None and status == "calendar_conflicting_latest"


def test_schedule_expectation_distinguishes_closed_from_missing(
    tmp_path, capture, monkeypatch
):
    db = tmp_path / "source.db"
    database(db, capture)
    config, cfg = configuration(tmp_path)
    monkeypatch.setattr(audit, "read_captures", lambda *args: [])
    r = receipt()
    # Late expected window, with a contemporaneous, explicit early close.
    r["market_calendar"]["close"] = "13:00"
    for key in ("written_at_utc", "observed_at_utc", "completed_at_utc"):
        r[key] = "2026-10-20T19:59:00Z"
    r["receipts"][0].update(
        request_at_utc="2026-10-20T19:59:00Z", received_at_utc="2026-10-20T19:59:00Z"
    )
    monkeypatch.setattr(sessions, "load_catalog", lambda *args: [r])
    result = audit.run(
        db, config, now=datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc)
    )
    run = json.loads(Path(result["run_receipt"]).read_bytes())
    assert run["windows"][-1]["status"] == "expected_no_session"
    monkeypatch.setattr(sessions, "load_catalog", lambda *args: [])
    result = audit.run(
        db, config, now=datetime(2026, 10, 20, 20, 31, tzinfo=timezone.utc)
    )
    run = json.loads(Path(result["run_receipt"]).read_bytes())
    assert run["windows"][-1]["status"] == "missing_capture_calendar_unverified"
    assert result["status"] == "missing_capture"


@pytest.mark.parametrize("delay_ns", [1, 60_000_000_000])
def test_report_file_written_after_assessment_cannot_be_backfilled(tmp_path, delay_ns):
    import os

    directory = tmp_path / "observer"
    directory.mkdir(mode=0o700)
    r = receipt()
    r["broker_receipts"] = r.pop("receipts")
    p = directory / "paper-observer-poll-20261020T133800000000Z-1.json"
    p.write_text(json.dumps(r))
    p.chmod(0o600)
    later = (
        int(datetime(2026, 10, 20, 13, 40, tzinfo=timezone.utc).timestamp())
        * 1_000_000_000
        + delay_ns
    )
    os.utime(p, ns=(later, later))
    catalog = sessions.load_catalog(directory, "2026-10-20")
    bounds, reason = sessions.bounds_at(catalog, "2026-10-20", "2026-10-20T13:40:00Z")
    assert bounds is None and reason == "calendar_missing_at_assessment"
