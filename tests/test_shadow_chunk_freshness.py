"""Shadow collection keeps decision-time freshness without waiting on later HTTP chunks."""
from datetime import datetime, timedelta, timezone
import json

from alphaos.data.providers.alpaca_data import AlpacaDataProvider
from alphaos.scanner.candidate_scanner import CandidateScanner
from alphaos.util import timeutils
from conftest import make_settings


def test_first_chunk_is_assessed_before_later_network_waits(journal, monkeypatch):
    clock = [datetime(2026, 9, 22, 13, 36, tzinfo=timezone.utc)]
    monkeypatch.setattr(timeutils, "now_utc", lambda: clock[0])
    settings = make_settings(ALPACA_API_KEY="offline-key", ALPACA_SECRET_KEY="offline-secret")
    scanner = CandidateScanner(settings, journal)
    scanner.market.provider = AlpacaDataProvider(settings, journal)
    scanner.market.use_mock = False
    monkeypatch.setattr(scanner, "_maybe_candidate", lambda *a, **k: None)
    calls = []
    assessed_before_request = []

    def urlopen(request, timeout):
        assessed_before_request.append(journal.scalar("SELECT COUNT(*) FROM price_snapshots"))
        symbols = request.full_url.split("symbols=")[1].split("&")[0].split(",")
        calls.append(symbols)

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                clock[0] += timedelta(seconds=2)
                lag = 55 if len(calls) == 1 else 1
                source = {
                    "latestQuote": {"bp": 99, "ap": 101, "t": (clock[0] - timedelta(seconds=lag)).isoformat()},
                    "latestTrade": {"p": 100, "t": clock[0].isoformat()},
                    "minuteBar": {"t": (clock[0] - timedelta(seconds=90)).isoformat(), "n": 3, "v": 300},
                    "dailyBar": {"t": "2026-09-22T04:00:00Z", "v": 100000},
                    "prevDailyBar": {"v": 1000000},
                }
                return json.dumps({s: source for s in symbols}).encode()

        return Response()

    monkeypatch.setattr("alphaos.data.providers.alpaca_data.urllib.request.urlopen", urlopen)
    symbols = [f"S{i}" for i in range(500)]
    result = scanner.scan_shadow_tier(symbols)
    assert result.snapshots == 500
    assert result.blocked_stale == 0
    assert len(calls) == 5 and [s for chunk in calls for s in chunk] == symbols
    assert assessed_before_request == [0, 100, 200, 300, 400]
    assert all(row["is_usable"] for row in journal.query("SELECT * FROM price_snapshots"))


def _scanner_at(journal, monkeypatch):
    clock = [datetime(2026, 9, 22, 13, 36, tzinfo=timezone.utc)]
    monkeypatch.setattr(timeutils, "now_utc", lambda: clock[0])
    scanner = CandidateScanner(make_settings(), journal)
    monkeypatch.setattr(scanner, "_maybe_candidate", lambda *a, **k: None)
    return scanner, clock


def _fresh(symbol, now, *, quote_age=1, bar_age=90):
    return {"symbol": symbol, "provider": "alpaca", "feed": "iex", "is_mock": False,
            "last_price": 100, "bid": 99.9, "ask": 100.1, "dollar_volume": 1000000,
            "received_at": now.isoformat(), "market_session": "regular",
            "quote_timestamp": (now - timedelta(seconds=quote_age)).isoformat(),
            "bar_timestamp": (now - timedelta(seconds=bar_age)).isoformat()}


def test_shuffled_responses_keep_actual_symbol_identity(journal, monkeypatch):
    scanner, clock = _scanner_at(journal, monkeypatch)
    rows = [_fresh("A", clock[0]), _fresh("B", clock[0], quote_age=61)]
    monkeypatch.setattr(scanner.market, "get_snapshots", lambda symbols: list(reversed(rows)))
    result = scanner.scan_shadow_tier(["A", "B"])
    assert result.per_symbol["A"]["freshness_status"] == "usable"
    assert result.per_symbol["B"]["freshness_status"] == "stale"
    assert journal.query("SELECT symbol,block_reason FROM price_snapshots ORDER BY id") == [
        {"symbol": "A", "block_reason": None}, {"symbol": "B", "block_reason": "STALE_QUOTE"}]


def test_missing_duplicate_and_foreign_responses_stay_in_denominator(journal, monkeypatch):
    scanner, clock = _scanner_at(journal, monkeypatch)
    rows = [_fresh("A", clock[0]), _fresh("A", clock[0]), _fresh("FOREIGN", clock[0])]
    monkeypatch.setattr(scanner.market, "get_snapshots", lambda symbols: rows)
    result = scanner.scan_shadow_tier(["A", "B"])
    assert result.snapshots == result.blocked_stale == 2
    assert set(result.per_symbol) == {"A", "B"}
    assert journal.scalar("SELECT COUNT(*) FROM price_snapshots WHERE is_usable=1") == 0
    details = json.loads(journal.query("SELECT detail_json FROM system_events WHERE category='shadow_collection'")[0]["detail_json"])
    assert [r["response_status"] for r in details["observations"]] == ["duplicate", "missing"]


def test_actual_processing_expiry_is_not_backdated_to_receipt(journal, monkeypatch):
    scanner, clock = _scanner_at(journal, monkeypatch)
    rows = [_fresh("A", clock[0]), _fresh("B", clock[0], quote_age=59)]
    monkeypatch.setattr(scanner.market, "get_snapshots", lambda symbols: rows)

    def slow_candidate(*args, **kwargs):
        clock[0] += timedelta(seconds=2)
        return None

    monkeypatch.setattr(scanner, "_maybe_candidate", slow_candidate)
    result = scanner.scan_shadow_tier(["A", "B"])
    assert result.blocked_stale == 1
    bad = journal.query("SELECT * FROM price_snapshots WHERE symbol='B'")[0]
    assert bad["quote_age_seconds"] == 61 and bad["block_reason"] == "STALE_QUOTE"


def test_old_source_bar_and_future_quote_remain_blocked(journal, monkeypatch):
    scanner, clock = _scanner_at(journal, monkeypatch)
    rows = [_fresh("OLD", clock[0], bar_age=181), _fresh("FUTURE", clock[0], quote_age=-6)]
    monkeypatch.setattr(scanner.market, "get_snapshots", lambda symbols: rows)
    result = scanner.scan_shadow_tier(["OLD", "FUTURE"])
    assert result.blocked_stale == 2
    assert [r["block_reason"] for r in journal.query("SELECT * FROM price_snapshots ORDER BY id")] == [
        "STALE_BAR", "UNVERIFIABLE_DATA"]


def test_empty_or_repeated_membership_refused_without_requests(journal, monkeypatch):
    import pytest
    scanner, _ = _scanner_at(journal, monkeypatch)
    calls = []
    monkeypatch.setattr(scanner.market, "get_snapshots", lambda symbols: calls.append(symbols))
    for symbols in ([], ["A", "A"]):
        with pytest.raises(ValueError, match="nonempty and unique"):
            scanner.scan_shadow_tier(symbols)
    assert calls == [] and journal.scalar("SELECT COUNT(*) FROM price_snapshots") == 0


def test_same_request_ceiling_for_partial_final_chunk(journal, monkeypatch):
    scanner, clock = _scanner_at(journal, monkeypatch)
    calls = []

    def fetch(symbols):
        calls.append(symbols)
        return [_fresh(s, clock[0]) for s in symbols]

    monkeypatch.setattr(scanner.market, "get_snapshots", fetch)
    symbols = [f"S{i}" for i in range(201)]
    result = scanner.scan_shadow_tier(symbols)
    assert list(map(len, calls)) == [100, 100, 1]
    assert [s for part in calls for s in part] == symbols
    assert result.snapshots == 201 and result.blocked_stale == 0


def test_source_provenance_is_diagnostic_only_and_retains_fallback(journal, monkeypatch):
    scanner, clock = _scanner_at(journal, monkeypatch)
    provider = AlpacaDataProvider(make_settings(ALPACA_API_KEY="offline", ALPACA_SECRET_KEY="offline"))
    payload = {"latestQuote": {"bp": 99, "ap": 101, "t": clock[0].isoformat()},
               "latestTrade": {"p": 100, "t": "2026-09-22T13:35:50Z"},
               "dailyBar": {"t": "2026-09-22T04:00:00Z", "v": 5000},
               "unretained_field": "not part of diagnostics"}
    row = provider._map("A", payload, clock[0].isoformat(),
                        requested="2026-09-22T13:35:59Z", response_sha256="a" * 64)
    monkeypatch.setattr(scanner.market, "get_snapshots", lambda symbols: [row])
    scanner.scan_shadow_tier(["A"])
    details = json.loads(journal.query("SELECT detail_json FROM system_events WHERE category='shadow_collection'")[0]["detail_json"])
    obs = details["observations"][0]
    source = obs["source_provenance"]
    assert obs["block_reason"] == "STALE_BAR"
    assert source["normalized_bar_source"] == "dailyBar"
    assert source["parts"]["minuteBar"] == {"present": False, "timestamp": None}
    assert source["parts"]["latestTrade"]["timestamp"] == "2026-09-22T13:35:50Z"
    assert source["requested_at_utc"] == "2026-09-22T13:35:59Z"
    assert source["response_sha256"] == "a" * 64
    assert "unretained_field" not in json.dumps(details)
    assert "_source_provenance" not in row
    # Existing single-symbol/core mapping has no new decision-facing field.
    assert "_source_provenance" not in provider._map("A", payload, clock[0].isoformat())


def test_same_old_minute_timestamp_is_never_replaced_by_fresher_daily_bar(journal, monkeypatch):
    scanner, clock = _scanner_at(journal, monkeypatch)
    provider = AlpacaDataProvider(make_settings())
    row = provider._map("A", {"latestQuote": {"t": clock[0].isoformat()},
        "minuteBar": {"t": "2026-09-21T19:59:00Z", "v": 100, "n": 1},
        "dailyBar": {"t": clock[0].isoformat()}}, clock[0].isoformat(), requested=clock[0].isoformat())
    monkeypatch.setattr(scanner.market, "get_snapshots", lambda symbols: [row])
    result = scanner.scan_shadow_tier(["A"])
    assert result.blocked_stale == 1
    snapshot = journal.query("SELECT * FROM price_snapshots")[0]
    assert snapshot["bar_timestamp"] == "2026-09-21T19:59:00Z"
    detail = json.loads(journal.query("SELECT detail_json FROM system_events WHERE category='shadow_collection'")[0]["detail_json"])
    assert detail["observations"][0]["source_provenance"]["normalized_bar_source"] == "minuteBar"


def test_transport_error_and_absent_symbols_do_not_reduce_requested_count(journal, monkeypatch):
    import urllib.error
    scanner, clock = _scanner_at(journal, monkeypatch)
    scanner.market.provider = AlpacaDataProvider(make_settings(ALPACA_API_KEY="offline", ALPACA_SECRET_KEY="offline"))
    scanner.market.use_mock = False
    calls = []

    def fetch(request, timeout):
        symbols = request.full_url.split("symbols=")[1].split("&")[0].split(",")
        calls.append(symbols)
        if len(calls) == 2:
            raise urllib.error.URLError("deliberate test failure")

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{}'  # successful transport, entire requested batch absent

        return Response()

    monkeypatch.setattr("alphaos.data.providers.alpaca_data.urllib.request.urlopen", fetch)
    result = scanner.scan_shadow_tier([f"S{i}" for i in range(201)])
    assert len(calls) == 3 and list(map(len, calls)) == [100, 100, 1]
    assert result.snapshots == result.blocked_stale == 201
    events = [json.loads(r["detail_json"]) for r in journal.query("SELECT detail_json FROM system_events WHERE category='shadow_collection' ORDER BY id")]
    first = events[0]["observations"][0]["source_provenance"]
    second = events[1]["observations"][0]["source_provenance"]
    assert first["transport_status"] == "received" and first["response_present"] is False
    assert len(first["response_sha256"]) == 64
    assert second["transport_status"] == "request_failed" and second["error_type"] == "URLError"
    assert second["response_sha256"] is None
    assert all(x["freshness_counts"] == {"missing": x["requested"]} for x in events)


def test_successful_provenance_never_enters_candidate_inputs(journal, monkeypatch):
    scanner, clock = _scanner_at(journal, monkeypatch)
    row = _fresh("A", clock[0])
    row["_source_provenance"] = {"version": "diagnostic-test"}
    seen = []
    monkeypatch.setattr(scanner.market, "get_snapshots", lambda symbols: [row])

    def candidate(*args, **kwargs):
        seen.append(dict(args[2]))
        return None

    monkeypatch.setattr(scanner, "_maybe_candidate", candidate)
    scanner.scan_shadow_tier(["A"])
    assert len(seen) == 1 and "_source_provenance" not in seen[0]
