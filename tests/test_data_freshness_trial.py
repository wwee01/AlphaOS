"""Offline normalized receipt fixtures; no provider, keys, orders or evaluation."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from conftest import make_settings
from alphaos.data.providers.alpaca_data import AlpacaDataProvider
from alphaos.research.data_freshness_trial import (
    load_protocol,
    evaluate_capture,
    decide,
    run,
    guard,
)

PROTOCOL = (
    Path(__file__).resolve().parents[1]
    / "docs/research/three-session-freshness-protocol-20261014.json"
)
FINAL = datetime(2026, 10, 16, 20, 31, tzinfo=timezone.utc)


@pytest.fixture
def protocol():
    return load_protocol(PROTOCOL)


def observation(
    symbol,
    index,
    *,
    day=15,
    bar_age=120,
    trade_age=30,
    price_source="latestTrade",
    binding=True,
):
    now = datetime(2026, 10, day, 13, 35, 10, tzinfo=timezone.utc)
    q, b, t = [
        (now - timedelta(seconds=age)).isoformat() for age in [10, bar_age, trade_age]
    ]
    sid = f"snapshot-{day}-{index}"
    snapshot = {
        "snapshot_id": sid,
        "symbol": symbol,
        "provider": "alpaca",
        "feed": "iex",
        "is_mock": 0,
        "last_price": 100.0,
        "bar_close": 100.0,
        "quote_timestamp": q,
        "bar_timestamp": b,
        "source_timestamp": q,
        "received_at": now.isoformat(),
        "market_session": "regular",
    }
    usable = guard(60, 180).assess(snapshot, now=now).is_usable
    snapshot.update(
        freshness_status="usable" if usable else "stale", is_usable=int(usable)
    )
    source = {
        "requested_symbol": symbol,
        "parts": {"latestTrade": {"present": True, "timestamp": t}},
    }
    if binding:
        source.update(
            price_evidence_version="normalized_last_price_v1",
            normalized_last_price_source=price_source,
            normalized_last_price=100.0,
            normalized_last_price_timestamp=t,
            raw_latest_trade_price=100.0,
        )
    obs = {
        "symbol": symbol,
        "snapshot_id": sid,
        "response_status": "matched",
        "assessed_at_utc": now.isoformat(),
        "freshness_status": snapshot["freshness_status"],
        "source_provenance": source,
    }
    return obs, snapshot


def dataset(protocol, **kwargs):
    rows = [
        observation(symbol, i, **kwargs) for i, symbol in enumerate(protocol["symbols"])
    ]
    return [o for o, s in rows], {s["snapshot_id"]: s for o, s in rows}


def test_protocol_hash_is_frozen_and_tamper_rejected(protocol, tmp_path):
    value = deepcopy(protocol)
    value["comparison_bar_age_seconds"] = 600
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="frozen_protocol_hash_mismatch"):
        load_protocol(path)


def test_paired_baseline_reproduction_and_bar300_with_fresh_price(protocol):
    obs, snaps = dataset(protocol, bar_age=240)
    result = evaluate_capture(obs, snaps, protocol)
    assert result["integrity_ok"] and result["metrics"]["raw_baseline"] == 0
    assert (
        result["metrics"]["safe_comparison"] == 500
        and result["safe_gain_symbols"] == 500
    )


def test_old_last_trade_excluded_even_with_fresh_quote_and_bar300(protocol):
    obs, snaps = dataset(protocol, bar_age=240, trade_age=200)
    result = evaluate_capture(obs, snaps, protocol)
    assert (
        result["metrics"]["raw_comparison"] == 500
        and result["metrics"]["safe_comparison"] == 0
    )
    assert result["metrics"]["raw_additions_invalid_price"] == 500


def test_daily_fallback_price_excluded(protocol):
    obs, snaps = dataset(protocol, price_source="dailyBar")
    assert evaluate_capture(obs, snaps, protocol)["metrics"]["safe_comparison"] == 0


def test_legacy_trade_timestamp_is_only_proxy_not_confirmed_price(protocol):
    obs, snaps = dataset(protocol, bar_age=240, binding=False)
    r = evaluate_capture(obs, snaps, protocol)
    assert r["metrics"]["legacy_proxy_comparison"] == 500
    assert (
        r["metrics"]["safe_comparison"] == 0
        and r["metrics"]["eligible_price_binding_missing"] == 500
    )


def test_corrupt_price_binding_is_not_accepted(protocol):
    obs, snaps = dataset(protocol)
    obs[0]["source_provenance"]["raw_latest_trade_price"] = 101
    r = evaluate_capture(obs, snaps, protocol)
    assert (
        r["metrics"]["safe_comparison"] == 499
        and r["metrics"]["eligible_price_binding_missing"] == 1
    )


@pytest.mark.parametrize(
    "kind", ["duplicate", "missing", "unknown", "source", "mock", "verdict"]
)
def test_denominator_identity_source_and_recorded_baseline_guards(protocol, kind):
    obs, snaps = dataset(protocol)
    if kind == "duplicate":
        obs[-1] = deepcopy(obs[0])
    elif kind == "missing":
        obs.pop()
    elif kind == "unknown":
        obs[-1]["symbol"] = "NOT_IN_FROZEN_UNIVERSE"
    elif kind == "source":
        snaps[obs[0]["snapshot_id"]]["feed"] = "sip"
    elif kind == "mock":
        snaps[obs[0]["snapshot_id"]]["is_mock"] = 1
    else:
        obs[0]["freshness_status"] = "stale"
    assert not evaluate_capture(obs, snaps, protocol)["integrity_ok"]


def captures(protocol, *, safe=420, gain=20):
    rows = []
    for day in protocol["dates"]:
        for window in protocol["windows_et"]:
            rows.append(
                {
                    "date": day,
                    "window_et": window,
                    "role": "retrospective" if day == "2026-10-14" else "forward",
                    "capture_present": True,
                    "result": {
                        "integrity_ok": True,
                        "metrics": {
                            "safe_comparison": safe,
                            "safe_baseline": safe - gain,
                            "accepted_invalid_prices": 0,
                            "eligible_price_binding_missing": 0,
                            "raw_additions_invalid_price": 1,
                        },
                        "safe_gain_symbols": gain,
                    },
                }
            )
    return rows


def test_pass_is_availability_only_and_raw_env_change_can_still_be_unsafe(protocol):
    result = decide(captures(protocol), protocol, FINAL, final=True)
    assert (
        result["outcome"] == "pass"
        and result["direct_env_bar_change"] == "not_supported"
    )


@pytest.mark.parametrize("safe,gain", [(399, 20), (420, 14)])
def test_complete_sample_fails_if_practical_criteria_missed(protocol, safe, gain):
    assert (
        decide(captures(protocol, safe=safe, gain=gain), protocol, FINAL, final=True)[
            "outcome"
        ]
        == "fail"
    )


def test_missing_forward_capture_or_price_binding_is_inconclusive(protocol):
    rows = captures(protocol)
    rows[-1]["capture_present"] = False
    assert decide(rows, protocol, FINAL, final=True)["outcome"] == "inconclusive"
    rows = captures(protocol)
    rows[-1]["result"]["metrics"]["eligible_price_binding_missing"] = 1
    assert decide(rows, protocol, FINAL, final=True)["outcome"] == "inconclusive"


def test_today_is_retrospective_and_finalization_has_fixed_cutoff(protocol):
    rows = captures(protocol)
    rows[0]["result"]["integrity_ok"] = False
    assert decide(rows, protocol, FINAL, final=True)["outcome"] == "pass"
    assert (
        decide(rows, protocol, FINAL, final=False)["outcome"] == "pending_final_cutoff"
    )
    with pytest.raises(ValueError, match="before_frozen_cutoff"):
        decide(rows, protocol, FINAL - timedelta(minutes=2), final=True)


def test_diagnostic_price_binding_changes_no_normalized_decision_field():
    provider = AlpacaDataProvider(make_settings())
    now = "2026-10-15T13:35:10Z"
    payload = {
        "latestQuote": {"bp": 99, "ap": 101, "t": now},
        "latestTrade": {"p": 100.0, "t": now},
        "minuteBar": {"t": now, "v": 1},
        "dailyBar": {"c": 90, "t": "2026-10-15T04:00:00Z"},
    }
    regular = provider._map("A", payload, now)
    diagnostic = provider._map(
        "A", payload, now, requested=now, response_sha256="a" * 64
    )
    assert regular == {k: v for k, v in diagnostic.items() if k != "_source_provenance"}
    source = diagnostic["_source_provenance"]
    assert (
        source["normalized_last_price_source"] == "latestTrade"
        and source["normalized_last_price"] == 100
    )
    del payload["latestTrade"]["p"]
    fallback = provider._map("A", payload, now, requested=now, response_sha256="b" * 64)
    assert fallback["_source_provenance"]["normalized_last_price_source"] == "dailyBar"
    assert fallback["last_price"] == 90


def test_runner_reads_existing_ledger_without_write_and_does_not_use_manual_captures(
    protocol, tmp_path
):
    db = tmp_path / "ledger.db"
    c = sqlite3.connect(db)
    c.executescript("""CREATE TABLE job_runs(id INTEGER,job_type TEXT,trigger_source TEXT,lock_key TEXT,started_at_utc TEXT,status TEXT,scheduler_run_id TEXT,result_summary_json TEXT);
        CREATE TABLE scan_batches(id INTEGER,scan_batch_id TEXT,scheduler_run_id TEXT,started_at_utc TEXT,completed_at_utc TEXT);
        CREATE TABLE system_events(id INTEGER,category TEXT,created_at_utc TEXT,detail_json TEXT);
        CREATE TABLE price_snapshots(snapshot_id TEXT,symbol TEXT,provider TEXT,feed TEXT,is_mock INTEGER,last_price REAL,bar_close REAL,quote_timestamp TEXT,bar_timestamp TEXT,source_timestamp TEXT,received_at TEXT,market_session TEXT,freshness_status TEXT,is_usable INTEGER);""")
    obs, snaps = dataset(protocol, day=14, bar_age=240)
    stamp = "2026-10-14T13:35:00Z"
    summary = json.dumps({"scan_summary": {"scan_batch_id": "scan"}})
    c.execute(
        "INSERT INTO job_runs VALUES(1,'scan','scheduler','scan:2026-10-14T09:35',?,'completed','run',?)",
        (stamp, summary),
    )
    c.execute(
        "INSERT INTO scan_batches VALUES(1,?,?,?,?)",
        ("scan", "run", stamp, "2026-10-14T13:36:00Z"),
    )
    columns = list(next(iter(snaps.values())))
    for snap in snaps.values():
        c.execute(
            "INSERT INTO price_snapshots ("
            + ",".join(columns)
            + ") VALUES("
            + ",".join("?" for _ in columns)
            + ")",
            tuple(snap[k] for k in columns),
        )
    for ix in range(5):
        d = {
            "scan_id": "scan",
            "chunk_index": ix,
            "scan_window": "09:35-09:50",
            "universe_file_version": protocol["universe_version"],
            "observations": obs[ix * 100 : (ix + 1) * 100],
        }
        c.execute(
            "INSERT INTO system_events VALUES(?,?,?,?)",
            (ix, "shadow_collection", "2026-10-14T13:35:20Z", json.dumps(d)),
        )
    c.commit()
    c.close()
    before = db.read_bytes()
    r = run(db, PROTOCOL, as_of=FINAL)
    assert r["captures"][0]["result"]["metrics"]["safe_comparison"] == 500
    assert (
        r["decision"]["outcome"] == "pending_collection" and db.read_bytes() == before
    )
    c = sqlite3.connect(db)
    c.execute("UPDATE job_runs SET trigger_source='cli'")
    c.commit()
    c.close()
    assert not run(db, PROTOCOL, as_of=FINAL)["captures"][0]["capture_present"]
