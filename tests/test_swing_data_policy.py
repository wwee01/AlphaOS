"""Offline contract, evidence-replay, audit and runtime-isolation tests."""

import ast
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import gzip
from hashlib import sha256
import json
from pathlib import Path
import socket
import sqlite3
import stat
import urllib.request

import pytest

from alphaos.data.freshness_guard import FreshnessGuard
from alphaos.research import data_freshness_trial as trial
from alphaos.research import swing_data_policy as policy

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "docs/research/three-session-freshness-protocol-20261014.json"
FIXTURE = ROOT / "tests/fixtures/swing_freshness_forward_20261015_16.json.gz"
FIXTURE_SHA256 = "ae61859401e53a05b721a3cdb16ffd0b43741b630134a65b6bd5f99a11d1fd2c"
NOW = datetime(2026, 10, 15, 13, 40, tzinfo=timezone.utc)


def blocked(*args, **kwargs):
    raise AssertionError("Unexpected network/runtime/database operation")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(urllib.request, "urlopen", blocked)


@pytest.fixture
def protocol():
    return trial.load_protocol(PROTOCOL)


@pytest.fixture
def retained():
    assert sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256
    return json.loads(gzip.decompress(FIXTURE.read_bytes()))


def receipt(*, quote_age=10, bar_age=240, trade_age=30, now=NOW):
    q, b, t = [
        (now - timedelta(seconds=age)).isoformat()
        for age in (quote_age, bar_age, trade_age)
    ]
    request, receive = [(now - timedelta(seconds=n)).isoformat() for n in (2, 1)]
    s = {
        "snapshot_id": "snap_fixture",
        "symbol": "AA",
        "provider": "alpaca",
        "feed": "iex",
        "is_mock": 0,
        "last_price": 100,
        "quote_timestamp": q,
        "bar_timestamp": b,
        "source_timestamp": q,
        "received_at": receive,
        "market_session": "regular",
    }
    original = trial.guard(60, 180).assess(s, now=now)
    s.update(
        is_usable=int(original.is_usable), freshness_status=original.freshness_status
    )
    p = {
        "version": "alpaca_snapshot_sources_v1",
        "provider": "alpaca",
        "feed": "iex",
        "requested_symbol": "AA",
        "response_present": True,
        "transport_status": "received",
        "response_sha256": "a" * 64,
        "requested_at_utc": request,
        "received_at_utc": receive,
        "parts": {
            k: {"present": True, "timestamp": v}
            for k, v in (("latestQuote", q), ("minuteBar", b), ("latestTrade", t))
        },
        "normalized_bar_source": "minuteBar",
        "price_evidence_version": "normalized_last_price_v1",
        "normalized_last_price_source": "latestTrade",
        "normalized_last_price": 100,
        "normalized_last_price_timestamp": t,
        "raw_latest_trade_price": 100,
    }
    o = {
        "symbol": "AA",
        "snapshot_id": "snap_fixture",
        "response_status": "matched",
        "assessed_at_utc": now.isoformat(),
        "received_at_utc": receive,
        "freshness_status": original.freshness_status,
        "source_provenance": p,
    }
    return o, s


def assess(o, s, *, date="2026-10-15", window="09:35-09:50"):
    return policy.assess_observation(o, s, date=date, window_et=window)


def restamp(o, s, part, stamp):
    p = o["source_provenance"]
    p["parts"][part]["timestamp"] = stamp
    if part == "latestQuote":
        s["quote_timestamp"] = s["source_timestamp"] = stamp
    elif part == "minuteBar":
        s["bar_timestamp"] = stamp
    else:
        p["normalized_last_price_timestamp"] = stamp
    original = trial.guard(60, 180).assess(
        s, now=datetime.fromisoformat(o["assessed_at_utc"])
    )
    s["is_usable"] = int(original.is_usable)
    s["freshness_status"] = o["freshness_status"] = original.freshness_status


def test_research_admission_keeps_baseline_and_input_unchanged(monkeypatch):
    o, s = receipt()
    before = deepcopy((o, s))
    monkeypatch.setattr(sqlite3, "connect", blocked)
    result = assess(o, s)
    assert result["research_eligible"]
    assert result["baseline_freshness_usable"] is False
    assert not result["paired_baseline_eligible"]
    assert FreshnessGuard().assess(s, now=NOW).is_usable is False
    assert (o, s) == before
    assert not {"candidate_id", "proposal_id", "order_id", "approved"} & result.keys()


@pytest.mark.parametrize(
    "field,cap", [("quote_age", 60), ("bar_age", 300), ("trade_age", 180)]
)
@pytest.mark.parametrize("extra,expected", [(0, True), (0.000001, False)])
def test_inclusive_age_limits(field, cap, extra, expected):
    assert assess(*receipt(**{field: cap + extra}))["research_eligible"] is expected


@pytest.mark.parametrize("field", ["quote_age", "bar_age", "trade_age"])
@pytest.mark.parametrize("age,expected", [(-5, True), (-5.000001, False)])
def test_future_clock_tolerance(field, age, expected):
    result = assess(*receipt(**{field: age}))
    assert result["research_eligible"] is expected
    if expected:
        assert any(result["future_skew_tolerated"].values())
        assert age in result["age_seconds"].values()


@pytest.mark.parametrize(
    "part,stamp,expected",
    [
        ("latestQuote", "2026-10-15T13:39:00.000000001Z", True),
        ("latestQuote", "2026-10-15T13:38:59.999999999Z", False),
        ("minuteBar", "2026-10-15T13:35:00.000000001Z", True),
        ("minuteBar", "2026-10-15T13:34:59.999999999Z", False),
        ("latestTrade", "2026-10-15T13:37:00.000000001Z", True),
        ("latestTrade", "2026-10-15T13:36:59.999999999Z", False),
        ("latestTrade", "2026-10-15T13:40:05.000000001Z", False),
    ],
)
def test_nanosecond_boundaries_are_not_rounded_into_acceptance(part, stamp, expected):
    o, s = receipt()
    restamp(o, s, part, stamp)
    assert assess(o, s)["research_eligible"] is expected


@pytest.mark.parametrize(
    "value", [None, "", "bad", "2026-10-15T13:40:00", "2026-99-15T13:40:00Z", {}, True]
)
@pytest.mark.parametrize(
    "target", ["assessment", "request", "receipt", "quote", "bar", "trade"]
)
def test_missing_naive_invalid_timestamps_fail_closed(target, value):
    o, s = receipt()
    p = o["source_provenance"]
    if target == "assessment":
        o["assessed_at_utc"] = value
    elif target == "request":
        p["requested_at_utc"] = value
    elif target == "receipt":
        p["received_at_utc"] = value
    else:
        part = {"quote": "latestQuote", "bar": "minuteBar", "trade": "latestTrade"}[
            target
        ]
        p["parts"][part]["timestamp"] = value
    assert not assess(o, s)["research_eligible"]


@pytest.mark.parametrize(
    "field", ["last_price", "normalized_last_price", "raw_latest_trade_price"]
)
@pytest.mark.parametrize(
    "value",
    [
        None,
        0,
        -1,
        float("nan"),
        float("inf"),
        float("-inf"),
        True,
        "SECRET_SENTINEL",
        10**1000,
    ],
)
def test_invalid_prices_fail_closed_and_serialize_safely(field, value):
    o, s = receipt()
    (s if field == "last_price" else o["source_provenance"])[field] = value
    result = assess(o, s)
    assert not result["research_eligible"]
    assert "SECRET_SENTINEL" not in json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(
    "kind",
    [
        "dailyBar",
        "quoteMid",
        "legacy",
        "binding_time",
        "binding_price",
        "missing_part",
        "daily_bar",
        "quote_time",
        "bar_time",
        "receipt_time",
        "source_time",
        "response_hash",
        "requested_symbol",
        "snapshot_id",
        "snapshot_symbol",
        "feed",
        "source_feed",
        "provider",
        "mock",
        "response",
        "baseline",
        "request_order",
        "assessment_order",
    ],
)
def test_binding_identity_and_clock_rejections(kind):
    o, s = receipt()
    p = o["source_provenance"]
    if kind in ("dailyBar", "quoteMid"):
        p["normalized_last_price_source"] = kind
    elif kind == "legacy":
        p.pop("price_evidence_version")
    elif kind == "binding_time":
        p["normalized_last_price_timestamp"] = NOW.isoformat()
    elif kind == "binding_price":
        p["raw_latest_trade_price"] = 101
    elif kind == "missing_part":
        p["parts"]["latestTrade"]["present"] = False
    elif kind == "daily_bar":
        p["normalized_bar_source"] = "dailyBar"
    elif kind in ("quote_time", "bar_time", "source_time", "receipt_time"):
        s[
            {
                "quote_time": "quote_timestamp",
                "bar_time": "bar_timestamp",
                "source_time": "source_timestamp",
                "receipt_time": "received_at",
            }[kind]
        ] = NOW.isoformat()
    elif kind == "response_hash":
        p["response_sha256"] = None
    elif kind == "requested_symbol":
        p[kind] = "OTHER"
    elif kind in ("snapshot_id", "snapshot_symbol"):
        s["snapshot_id" if kind == "snapshot_id" else "symbol"] = "OTHER"
    elif kind == "source_feed":
        p["feed"] = "sip"
    elif kind in ("feed", "provider"):
        s[kind] = "other"
    elif kind == "mock":
        s["is_mock"] = 1
    elif kind == "response":
        o["response_status"] = "duplicate"
    elif kind == "baseline":
        s["is_usable"] = 1
    elif kind == "request_order":
        p["requested_at_utc"] = NOW.isoformat()
    else:
        p["received_at_utc"] = (NOW + timedelta(seconds=1)).isoformat()
    assert not assess(o, s)["research_eligible"]


@pytest.mark.parametrize(
    "now,date,window",
    [
        (
            datetime(2026, 10, 17, 13, 40, tzinfo=timezone.utc),
            "2026-10-17",
            "09:35-09:50",
        ),
        (
            datetime(2026, 12, 25, 14, 40, tzinfo=timezone.utc),
            "2026-12-25",
            "09:35-09:50",
        ),
        (
            datetime(2026, 10, 15, 12, 40, tzinfo=timezone.utc),
            "2026-10-15",
            "09:35-09:50",
        ),
        (NOW, "2026-10-16", "09:35-09:50"),
        (NOW, "2026-10-15", "11:30-12:00"),
    ],
)
def test_calendar_and_capture_window(now, date, window):
    assert not assess(*receipt(now=now), date=date, window=window)["research_eligible"]


def test_retained_forward_reproduction_and_rejected_additions(protocol, retained):
    assert retained["protocol_sha256"] == trial.PROTOCOL_SHA256
    assert retained["reference_sha256"] == policy.REFERENCE_SHA256
    expected = [435, 446, 490, 418, 448, 491]
    baseline = [415, 434, 483, 395, 422, 481]
    rejected = 0
    assert (
        sum(
            len(chunk["observations"])
            for c in retained["captures"]
            for chunk in c["chunks"]
        )
        == 3000
    )
    for capture, count, base in zip(
        retained["captures"], expected, baseline, strict=True
    ):
        strict = policy.evaluate_capture(capture, protocol)
        assert strict["integrity_ok"] and strict["denominator"] == 500
        assert strict["research_eligible"] == count
        assert strict["paired_baseline_eligible"] == base
        obs = [o for ch in capture["chunks"] for o in ch["observations"]]
        frozen = trial.evaluate_capture(obs, capture["snapshots"], protocol)
        assert frozen["metrics"]["safe_comparison"] == count
        rejected += frozen["metrics"]["raw_additions_invalid_price"]
        for row in strict["rows"]:
            if row["research_eligible"]:
                assert row["price_binding_complete"]
                assert -5 <= row["age_seconds"]["trade"] <= 180
    assert rejected == 75


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "duplicate",
        "unknown",
        "snapshot_duplicate",
        "chunk_missing",
        "chunk_duplicate",
        "chunk_version",
        "universe_version",
        "wrong_job",
        "wrong_lock",
        "lineage",
        "lineage_clock",
        "chunk_hash",
        "capture_clock",
        "chunk_index_type",
        "lineage_inconsistent",
    ],
)
def test_capture_integrity_keeps_fixed_denominator_and_blocks_admissions(
    protocol, retained, kind
):
    c = retained["captures"][0]
    ch = c["chunks"][0]
    o = ch["observations"][0]
    if kind == "missing":
        ch["observations"].pop()
    elif kind == "duplicate":
        ch["observations"][-1] = deepcopy(o)
    elif kind == "unknown":
        o["symbol"] = "UNKNOWN_SYMBOL"
    elif kind == "snapshot_duplicate":
        ch["observations"][1]["snapshot_id"] = o["snapshot_id"]
    elif kind == "chunk_missing":
        c["chunks"].pop()
    elif kind == "chunk_duplicate":
        c["chunks"][1]["chunk_index"] = 0
    elif kind in ("chunk_version", "universe_version"):
        ch["version" if kind == "chunk_version" else "universe_file_version"] = "wrong"
    elif kind == "wrong_job":
        c["trigger_source"] = "cli"
    elif kind == "wrong_lock":
        c["lock_key"] = "scan:wrong"
    elif kind == "lineage":
        ch["lineage_capture"]["lineage_id"] = "other"
    elif kind == "lineage_clock":
        ch["lineage_capture"]["completed_at_utc"] = "2026-10-15T13:49:00Z"
    elif kind == "chunk_index_type":
        ch["chunk_index"] = "0"
    elif kind == "lineage_inconsistent":
        ch["lineage_capture"]["completed_at_utc"] = ch["lineage_capture"][
            "started_at_utc"
        ]
    elif kind == "chunk_hash":
        o["source_provenance"]["response_sha256"] = "b" * 64
    else:
        c["completed_at_utc"] = c["started_at_utc"]
    result = policy.evaluate_capture(c, protocol)
    assert not result["integrity_ok"]
    assert result["denominator"] == len(result["rows"]) == 500
    assert result["research_eligible"] == result["paired_baseline_eligible"] == 0


def test_unknown_headers_never_enter_audit_and_writes_are_exclusive(
    protocol, retained, tmp_path
):
    c = retained["captures"][0]
    c["authorization"] = "SECRET_SENTINEL"
    c["chunks"][0]["observations"][0]["source_provenance"]["headers"] = {
        "key": "SECRET_SENTINEL"
    }
    directory = tmp_path / "audit"
    path = policy.append_audit([c], protocol, directory)
    before = path.read_bytes()
    assert "SECRET_SENTINEL" not in before.decode()
    report = json.loads(before)
    assert report["policy_sha256"] == policy.POLICY_SHA256
    assert report["runtime_integration"] is False
    assert (
        report["code_identity"]["alphaos/research/swing_data_policy.py"]
        == sha256(Path(policy.__file__).read_bytes()).hexdigest()
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    with pytest.raises(FileExistsError):
        policy.append_audit([c], protocol, directory)
    assert path.read_bytes() == before
    assert list(directory.iterdir()) == [path]


def test_concurrent_duplicate_audits_never_overwrite(protocol, retained, tmp_path):
    def write():
        try:
            return policy.append_audit(
                retained["captures"][:1], protocol, tmp_path / "audit"
            )
        except FileExistsError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: write(), range(2)))
    assert sum(r is not None for r in results) == 1
    files = list((tmp_path / "audit").iterdir())
    assert len(files) == 1
    assert json.loads(files[0].read_bytes())["audit_version"] == policy.AUDIT_VERSION


def test_failed_audit_and_symlink_directory_do_not_publish_partial_reports(
    protocol, retained, tmp_path, monkeypatch
):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError):
        policy.append_audit(retained["captures"][:1], protocol, link)

    def fail_link(*args, **kwargs):
        raise OSError("fixture failure")

    monkeypatch.setattr(policy.os, "link", fail_link)
    with pytest.raises(OSError):
        policy.append_audit(retained["captures"][:1], protocol, target)
    assert list(target.iterdir()) == []


def test_report_is_pure_of_network_database_and_settings(
    protocol, retained, monkeypatch
):
    monkeypatch.setattr(sqlite3, "connect", blocked)
    monkeypatch.setattr(policy.timeutils, "now_utc", blocked)
    report = policy.build_report(retained["captures"][:1], protocol)
    assert report["captures"][0]["research_eligible"] == 435
    o, s = receipt()
    o["assessed_at_utc"] = None
    assert not assess(o, s)["research_eligible"]


def test_policy_contract_and_production_import_isolation(protocol):
    contract = ROOT / "docs/research/swing-research-freshness-policy-v1-20261018.md"
    assert sha256(contract.read_bytes()).hexdigest() == policy.CONTRACT_SHA256
    broken = deepcopy(protocol)
    broken["symbols"][0] = "OTHER"
    with pytest.raises(ValueError, match="frozen_protocol_mismatch"):
        policy.evaluate_capture({}, broken)
    allowed = {"alphaos.research", "alphaos.util"}
    tree = ast.parse(Path(policy.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "alphaos"
        ):
            assert node.module in allowed
            assert {a.name for a in node.names} <= {"data_freshness_trial", "timeutils"}
    for path in (ROOT / "alphaos").rglob("*.py"):
        permitted_audit_adapters = {
            "research/swing_audit.py",
            "research/swing_session_evidence.py",
        }
        relative = path.relative_to(ROOT / "alphaos").as_posix()
        if (
            path.resolve() != Path(policy.__file__).resolve()
            and relative not in permitted_audit_adapters
        ):
            assert "swing_data_policy" not in path.read_text(), path


def test_readonly_reader_keeps_first_capture_even_if_incomplete(
    protocol, tmp_path, monkeypatch
):
    p = tmp_path / "ledger.db"
    conn = sqlite3.connect(p)
    conn.executescript("""
        CREATE TABLE job_runs(id INTEGER,status TEXT,scheduler_run_id TEXT,result_summary_json TEXT,
          trigger_source TEXT,lock_key TEXT,job_type TEXT,started_at_utc TEXT);
        CREATE TABLE scan_batches(id INTEGER,scan_batch_id TEXT,scheduler_run_id TEXT,
          started_at_utc TEXT,completed_at_utc TEXT);
        CREATE TABLE system_events(id INTEGER,category TEXT,created_at_utc TEXT,detail_json TEXT);
    """)
    conn.execute("CREATE TABLE price_snapshots(" + trial.SNAPSHOT_COLUMNS + ")")
    expected = []
    ix = 0
    for day in protocol["dates"]:
        for window in protocol["windows_et"]:
            start = day + "T13:35:00Z"
            lock = f"scan:{day}T{window.split('-')[0]}"
            for kind, trigger in (
                ("manual", "cli"),
                ("first", "scheduler"),
                ("retry", "scheduler"),
            ):
                ix += 1
                scan = f"scan_{ix}_{kind}"
                if kind == "first":
                    expected.append(scan)
                conn.execute(
                    "INSERT INTO job_runs VALUES(?,?,?,?,?,?,?,?)",
                    (
                        ix,
                        "completed",
                        str(ix),
                        json.dumps({"scan_summary": {"scan_batch_id": scan}}),
                        trigger,
                        lock,
                        "scan",
                        start,
                    ),
                )
                conn.execute(
                    "INSERT INTO scan_batches VALUES(?,?,?,?,?)",
                    (ix, scan, str(ix), start, start),
                )
                # First actual capture is deliberately incomplete; retry must
                # not substitute. Reader returns it; policy later blocks it.
                detail = {"scan_id": scan, "observations": []}
                conn.execute(
                    "INSERT INTO system_events VALUES(?,?,?,?)",
                    (ix, "shadow_collection", start, json.dumps(detail)),
                )
    conn.commit()
    conn.close()
    before = p.read_bytes()
    real_connect = sqlite3.connect
    traces: list[str] = []

    def readonly(*args, **kwargs):
        assert "?mode=ro" in args[0]
        db = real_connect(*args, **kwargs)
        db.set_trace_callback(traces.append)
        return db

    monkeypatch.setattr(policy.sqlite3, "connect", readonly)
    captures = policy.read_frozen_captures(p, protocol, "2026-10-16T20:33:45Z")
    assert [c["scan_id"] for c in captures] == expected
    assert p.read_bytes() == before
    assert all(
        not q.startswith(("INSERT", "UPDATE", "DELETE", "CREATE")) for q in traces
    )
    assert all(
        policy.evaluate_capture(c, protocol)["research_eligible"] == 0 for c in captures
    )


def test_tampered_reference_rejected_before_database_access(tmp_path, monkeypatch):
    reference = tmp_path / "reference.json"
    reference.write_text("{}")
    monkeypatch.setattr(sqlite3, "connect", blocked)
    with pytest.raises(ValueError, match="frozen_final_reference_mismatch"):
        policy.replay_frozen(tmp_path / "missing.db", PROTOCOL, reference)


def test_new_capture_appends_without_changing_prior_audit(protocol, retained, tmp_path):
    first = policy.append_audit(retained["captures"][:1], protocol, tmp_path)
    before = first.read_bytes()
    second = policy.append_audit(retained["captures"][1:2], protocol, tmp_path)
    assert first != second and first.read_bytes() == before
    assert len(list(tmp_path.iterdir())) == 2


@pytest.mark.parametrize("delta,expected", [(1e-9, True), (1e-5, False)])
def test_price_serialization_tolerance_is_explicit(delta, expected):
    o, s = receipt()
    o["source_provenance"]["raw_latest_trade_price"] += delta
    assert assess(o, s)["research_eligible"] is expected


@pytest.mark.parametrize(
    "day,instant",
    [
        ("2026-11-27", datetime(2026, 11, 27, 20, 46, tzinfo=timezone.utc)),
        ("2026-10-19", datetime(2026, 10, 19, 13, 40, tzinfo=timezone.utc)),
    ],
)
def test_unknown_boundaries_fail_closed_despite_shared_regular_label(day, instant):
    assert policy.timeutils.market_session(instant).value == "regular"
    window = "15:45-16:00" if day == "2026-11-27" else "09:35-09:50"
    result = assess(*receipt(now=instant), date=day, window=window)
    assert not result["research_eligible"]
    assert "session_boundary_unverified" in result["reasons"]


@pytest.fixture
def early_close_evidence(monkeypatch):
    # TEST FIXTURE ONLY: no real November calendar request or new observation.
    record = {
        "date": "2026-11-27",
        "open": "09:30",
        "close": "13:00",
        "source": "broker_calendar",
        "report_sha256": "a" * 64,
        "calendar_response_sha256": "b" * 64,
        "requested_at_utc": "2026-11-27T12:00:00Z",
        "received_at_utc": "2026-11-27T12:00:01Z",
    }
    monkeypatch.setattr(policy, "_SESSION_EVIDENCE", {"2026-11-27": record})
    return record


@pytest.mark.parametrize(
    "instant,expected",
    [
        (datetime(2026, 11, 27, 17, 59, 59, 999999, tzinfo=timezone.utc), True),
        (datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc), False),
        (datetime(2026, 11, 27, 18, 0, 0, 1, tzinfo=timezone.utc), False),
        (datetime(2026, 11, 27, 20, 46, tzinfo=timezone.utc), False),
    ],
)
def test_verified_early_close_is_exclusive_and_dst_aware(
    early_close_evidence, instant, expected
):
    # The shared production calendar calls all these instants regular.
    assert policy.timeutils.market_session(instant).value == "regular"
    window = "15:45-16:00" if instant.hour == 20 else "12:55-13:05"
    result = assess(*receipt(now=instant), date="2026-11-27", window=window)
    assert result["research_eligible"] is expected
    if not expected:
        assert "outside_verified_session:assessment" in result["reasons"]
    bounds = policy.session_bounds("2026-11-27")
    assert bounds is not None
    assert bounds["close_at_utc"] == "2026-11-27T18:00:00+00:00"


@pytest.mark.parametrize("component", ["latestQuote", "minuteBar", "latestTrade"])
def test_future_skew_cannot_cross_verified_close(early_close_evidence, component):
    instant = datetime(2026, 11, 27, 17, 59, 58, tzinfo=timezone.utc)
    o, s = receipt(now=instant)
    restamp(o, s, component, "2026-11-27T18:00:01Z")
    result = assess(o, s, date="2026-11-27", window="12:55-13:05")
    assert -3 in result["age_seconds"].values()
    assert not result["research_eligible"]
    assert any(r.startswith("outside_verified_session:") for r in result["reasons"])


@pytest.mark.parametrize(
    "change,value",
    [
        ("close", None),
        ("close", "25:00"),
        ("close", "16:01"),
        ("close", "09:30"),
        ("open", "9:30"),
        ("source", "local_calendar_full_closures_only"),
        ("date", "2026-11-26"),
        ("calendar_response_sha256", None),
        ("received_at_utc", "2026-11-27T11:59:59Z"),
    ],
)
def test_unverified_or_malformed_explicit_bounds_fail_closed(
    early_close_evidence, change, value
):
    early_close_evidence[change] = value
    assert policy.session_bounds("2026-11-27") is None
    instant = datetime(2026, 11, 27, 17, 40, tzinfo=timezone.utc)
    result = assess(*receipt(now=instant), date="2026-11-27", window="12:35-12:50")
    assert not result["research_eligible"]
    assert "session_boundary_unverified" in result["reasons"]


def test_source_before_verified_open_is_rejected(early_close_evidence):
    instant = datetime(2026, 11, 27, 14, 30, tzinfo=timezone.utc)
    o, s = receipt(now=instant, quote_age=0, trade_age=0, bar_age=120)
    result = assess(o, s, date="2026-11-27", window="09:30-09:35")
    assert not result["research_eligible"]
    assert "outside_verified_session:minute_bar" in result["reasons"]


def test_calendar_and_price_hashes_are_trusted_lineage_not_reconstructed_bodies(
    protocol, retained
):
    c = retained["captures"][0]
    for o in c["chunks"][0]["observations"]:
        o["source_provenance"]["response_sha256"] = "b" * 64
    # No raw HTTP body exists in this input, so a syntactically valid, internally
    # consistent attestation cannot be independently rehashed or authenticated.
    result = policy.evaluate_capture(c, protocol)
    assert result["research_eligible"] == 435
    report = policy.build_report([c], protocol)
    assert any("trusted retained lineage" in warning for warning in report["warnings"])
    assert result["session_evidence"]["source"] == "broker_calendar"
    assert result["session_evidence"]["report_sha256"]
