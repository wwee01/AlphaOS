"""Shadow-only lineage work is scoped before data receipt; freshness stays current."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from alphaos import lineage
from alphaos.lineage.builder import get_or_create_lineage_id
from alphaos.lineage.git_info import GitInfo
from alphaos.scanner.candidate_scanner import CandidateScanner
from alphaos.util import timeutils
from conftest import make_settings


@pytest.fixture
def harness(journal, monkeypatch):
    clock = [datetime(2026, 10, 2, 13, 36, tzinfo=timezone.utc)]
    monkeypatch.setattr(timeutils, "now_utc", lambda: clock[0])
    scanner = CandidateScanner(make_settings(), journal)
    captures, requests = [], []
    next_id = ["lineage_one"]
    def capture(j, settings):
        captures.append({"clock": clock[0], "journal": j, "settings": settings})
        return next_id[0]
    monkeypatch.setattr(lineage, "get_or_create_lineage_id", capture)
    def snapshot(symbol):
        return {"symbol": symbol, "provider": "alpaca", "feed": "iex", "is_mock": False,
                "last_price": 103, "prev_close": 100, "change_pct": .03, "rel_volume": 1,
                "volume": 1_000_000, "dollar_volume": 103_000_000,
                "bid": 102.99, "ask": 103.01, "spread_pct": .0002,
                "bar_open": 101, "bar_high": 104, "bar_low": 100, "bar_close": 103,
                "received_at": clock[0].isoformat(), "market_session": "regular",
                "quote_timestamp": (clock[0]-timedelta(seconds=1)).isoformat(),
                "bar_timestamp": (clock[0]-timedelta(seconds=90)).isoformat()}
    def fetch(symbols):
        requests.append({"symbols": list(symbols), "captures": len(captures), "clock": clock[0]})
        return [snapshot(s) for s in symbols]
    monkeypatch.setattr(scanner.market, "get_snapshots", fetch)
    monkeypatch.setattr(scanner.market, "get_snapshot", snapshot)
    return scanner, clock, captures, requests, next_id, snapshot


def details(journal):
    return [json.loads(r["detail_json"]) for r in journal.query(
        "SELECT detail_json FROM system_events WHERE category='shadow_collection' ORDER BY id")]


def test_one_capture_precedes_every_chunk_with_same_scoped_receipt(harness, journal):
    scanner, clock, captures, requests, _, _ = harness
    result = scanner.scan_shadow_tier([f"S{i}" for i in range(201)], scan_batch_id="batch_scope")
    assert result.snapshots == len(result.candidates) == 201
    assert len(captures) == 1
    assert [len(r["symbols"]) for r in requests] == [100, 100, 1]
    assert all(r["captures"] == 1 for r in requests)
    receipts = [d["lineage_capture"] for d in details(journal)]
    assert receipts == [receipts[0]]*3
    assert receipts[0] == {
        "version": "shadow_scan_lineage_v1", "scope": "single_shadow_scan",
        "lineage_id": "lineage_one", "status": "recorded",
        "started_at_utc": clock[0].isoformat(), "completed_at_utc": clock[0].isoformat(),
    }
    assert {r["lineage_id"] for r in journal.query("SELECT * FROM candidates")} == {"lineage_one"}


def test_candidates_and_stale_rejects_share_capture_without_changing_freshness(harness, journal, monkeypatch):
    scanner, clock, captures, _, _, snapshot = harness
    rows = [snapshot("GOOD"), snapshot("OLD")]
    rows[1]["bar_timestamp"] = (clock[0]-timedelta(seconds=181)).isoformat()
    monkeypatch.setattr(scanner.market, "get_snapshots", lambda s: rows)
    result = scanner.scan_shadow_tier(["GOOD", "OLD"])
    assert len(captures) == 1
    assert len(result.candidates) == 1 and result.blocked_stale == 1
    assert journal.one("SELECT lineage_id FROM candidates")["lineage_id"] == "lineage_one"
    rejected = journal.one("SELECT * FROM rejected_candidates")
    assert rejected["lineage_id"] == "lineage_one" and rejected["reason_code"] == "STALE_BAR"
    assert journal.one("SELECT bar_age_seconds FROM price_snapshots WHERE symbol='OLD'")["bar_age_seconds"] == 181


def test_unavailable_capture_is_reused_as_missing_not_retried(harness, journal):
    scanner, _, captures, _, next_id, _ = harness
    next_id[0] = None
    result = scanner.scan_shadow_tier(["A", "B"])
    assert len(captures) == 1 and len(result.candidates) == 2
    assert journal.scalar("SELECT COUNT(*) FROM candidates WHERE lineage_id IS NULL") == 2
    assert details(journal)[0]["lineage_capture"]["status"] == "unavailable"


def test_next_scan_captures_new_identity_and_settings(harness, journal):
    scanner, _, captures, _, next_id, _ = harness
    scanner.scan_shadow_tier(["A"], scan_batch_id="first")
    next_id[0] = "lineage_two"
    scanner.settings = make_settings(MAX_OPEN_POSITIONS=7)
    scanner.scan_shadow_tier(["B"], scan_batch_id="second")
    assert len(captures) == 2 and captures[1]["settings"].max_open_positions == 7
    assert [r["lineage_id"] for r in journal.query("SELECT * FROM candidates ORDER BY id")] == ["lineage_one", "lineage_two"]
    assert [d["lineage_capture"]["lineage_id"] for d in details(journal)] == ["lineage_one", "lineage_two"]


def test_failed_scan_does_not_leak_capture_into_next_scan(harness, monkeypatch):
    scanner, _, captures, _, next_id, snapshot = harness
    with monkeypatch.context() as m:
        m.setattr(scanner.market, "get_snapshots", lambda s: (_ for _ in ()).throw(RuntimeError("offline")))
        with pytest.raises(RuntimeError, match="offline"):
            scanner.scan_shadow_tier(["A"])
    next_id[0] = "lineage_next"
    result = scanner.scan_shadow_tier(["B", "C"])
    assert len(captures) == 2
    assert {c["lineage_id"] for c in result.candidates} == {"lineage_next"}


def test_slow_capture_is_before_request_and_not_backdated(harness, monkeypatch, journal):
    scanner, clock, captures, requests, _, _ = harness
    initial = clock[0]
    def slow_capture(*args):
        captures.append({})
        clock[0] += timedelta(seconds=100)
        return "slow_lineage"
    monkeypatch.setattr(lineage, "get_or_create_lineage_id", slow_capture)
    result = scanner.scan_shadow_tier(["A", "B"])
    assert len(captures) == 1 and len(result.candidates) == 2
    assert requests[0]["clock"] == initial+timedelta(seconds=100)
    receipt = details(journal)[0]["lineage_capture"]
    assert receipt["started_at_utc"] == initial.isoformat()
    assert receipt["completed_at_utc"] == (initial+timedelta(seconds=100)).isoformat()
    assert all(o["assessed_at_utc"] == clock[0].isoformat() for o in details(journal)[0]["observations"])


def test_core_and_direct_callers_still_capture_per_row(harness, journal):
    scanner, _, captures, _, _, _ = harness
    result = scanner.scan(["A", "B"])
    assert len(captures) == 2 and len(result.candidates) == 2
    scanner._reject(None, "C", "external", "TEST", "test", None)
    scanner._reject(None, "D", "external", "TEST", "test", None)
    assert len(captures) == 4
    assert details(journal) == []


def test_real_builder_recaptures_changed_git_and_configuration(harness, journal, monkeypatch):
    scanner, _, _, _, _, _ = harness
    monkeypatch.setattr(lineage, "get_or_create_lineage_id", get_or_create_lineage_id)
    from alphaos.lineage import builder
    identity = [GitInfo("a"*40, "first", False)]
    reads = []
    def git_info():
        reads.append(identity[0])
        return identity[0]
    monkeypatch.setattr(builder, "get_git_info", git_info)
    scanner.scan_shadow_tier(["A", "B"], scan_batch_id="first")
    identity[0] = GitInfo("b"*40, "second", False)
    scanner.settings = make_settings(MAX_OPEN_POSITIONS=7)
    scanner.scan_shadow_tier(["C", "D"], scan_batch_id="second")
    rows = journal.query("SELECT * FROM lineage_snapshots ORDER BY id")
    assert len(reads) == len(rows) == 2
    assert [r["git_commit_sha"] for r in rows] == ["a"*40, "b"*40]
    assert rows[0]["config_hash"] != rows[1]["config_hash"]
    assert rows[0]["risk_config_hash"] != rows[1]["risk_config_hash"]
    assert [r["lineage_id"] for r in journal.query("SELECT * FROM candidates ORDER BY id")] == [
        rows[0]["lineage_id"], rows[0]["lineage_id"], rows[1]["lineage_id"], rows[1]["lineage_id"]]


@pytest.mark.parametrize("symbols", [[], ["A", "A"]])
def test_invalid_membership_does_not_capture_or_request(harness, symbols):
    scanner, _, captures, requests, _, _ = harness
    with pytest.raises(ValueError, match="nonempty and unique"):
        scanner.scan_shadow_tier(symbols)
    assert captures == requests == []
