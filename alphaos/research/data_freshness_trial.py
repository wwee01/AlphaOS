"""Frozen three-session offline availability comparison; no runtime wiring.

Reads retained scheduled scans only. Does not load .env, construct a provider,
use AI/brokers, write the ledger, modify a universe, or consume formal tests.
"""

from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
import math
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from alphaos.data.freshness_guard import FreshnessGuard
from alphaos.util import timeutils

PROTOCOL_SHA256 = "3ee3ee14f295156b76850f30563a86e844a4e7a6b2d7944f69a2cdc107f4f795"
VERSION = "three_session_freshness_runner_v1"
SNAPSHOT_COLUMNS = "snapshot_id,symbol,provider,feed,is_mock,last_price,bar_close,quote_timestamp,bar_timestamp,source_timestamp,received_at,market_session,freshness_status,is_usable"


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def load_protocol(path):
    value = json.loads(Path(path).read_text())
    body = {k: v for k, v in value.items() if k != "protocol_sha256"}
    if (
        sha256(canonical(body)).hexdigest() != PROTOCOL_SHA256
        or value.get("protocol_sha256") != PROTOCOL_SHA256
    ):
        raise ValueError("frozen_protocol_hash_mismatch")
    symbols = value["symbols"]
    frozen = timeutils.parse_iso(value["frozen_at_utc"])
    forward_start = timeutils.parse_iso("2026-10-15T13:35:00Z")
    assert forward_start is not None
    if (
        len(symbols) != 500
        or len(set(symbols)) != 500
        or frozen is None
        or frozen >= forward_start
    ):
        raise ValueError("invalid_frozen_protocol")
    return value


def numeric(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def same(a, b):
    a, b = numeric(a), numeric(b)
    return (
        a is not None
        and b is not None
        and math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-8)
    )


def price_validity(observation, snapshot, protocol):
    source = observation.get("source_provenance") or {}
    age = timeutils.age_seconds(
        (source.get("parts", {}).get("latestTrade") or {}).get("timestamp") or "",
        timeutils.parse_iso(observation["assessed_at_utc"]),
    )
    last = numeric(snapshot.get("last_price"))
    # Legacy timestamps give a useful descriptive screen, not a complete
    # proof that _map selected the trade rather than its daily-close fallback.
    proxy = bool(
        last is not None
        and last > 0
        and age is not None
        and -protocol["future_timestamp_tolerance_seconds"]
        <= age
        <= protocol["last_price_age_seconds"]
    )
    if source.get("price_evidence_version") != protocol["price_evidence_version"]:
        return {
            "valid": False,
            "binding_complete": False,
            "reason": "legacy_price_binding_unavailable",
            "legacy_age_proxy": proxy,
        }
    origin = source.get("normalized_last_price_source")
    if origin != "latestTrade":
        return {
            "valid": False,
            "binding_complete": True,
            "reason": "fallback_or_missing_price",
            "legacy_age_proxy": proxy,
        }
    timestamp = source.get("normalized_last_price_timestamp")
    expected_ts = (source.get("parts", {}).get("latestTrade") or {}).get("timestamp")
    if (
        timestamp != expected_ts
        or not same(last, source.get("normalized_last_price"))
        or not same(last, source.get("raw_latest_trade_price"))
    ):
        return {
            "valid": False,
            "binding_complete": False,
            "reason": "price_binding_mismatch",
            "legacy_age_proxy": proxy,
        }
    return {
        "valid": proxy,
        "binding_complete": True,
        "reason": "valid" if proxy else "stale_missing_or_invalid_price",
        "legacy_age_proxy": proxy,
    }


def guard(quote_cap, bar_cap):
    return FreshnessGuard.from_settings(
        SimpleNamespace(
            max_quote_age_seconds_rth=quote_cap,
            max_bar_age_seconds_rth=bar_cap,
            max_quote_age_seconds_premarket=quote_cap,
            max_bar_age_seconds_premarket=bar_cap,
            max_price_drift_bps_since_proposal=0,
        )
    )


def evaluate_capture(observations, snapshots, protocol):
    expected = set(protocol["symbols"])
    counts = Counter(o.get("symbol") for o in observations)
    structural_errors = []
    if (
        len(observations) != 500
        or set(counts) != expected
        or any(n != 1 for n in counts.values())
    ):
        structural_errors.append("fixed_denominator_or_membership_mismatch")
    baseline = guard(
        protocol["baseline_quote_age_seconds"], protocol["baseline_bar_age_seconds"]
    )
    comparison = guard(
        protocol["comparison_quote_age_seconds"], protocol["comparison_bar_age_seconds"]
    )
    metrics = Counter(
        {
            "raw_baseline": 0,
            "raw_comparison": 0,
            "safe_baseline": 0,
            "safe_comparison": 0,
            "legacy_proxy_baseline": 0,
            "legacy_proxy_comparison": 0,
            "raw_additions_invalid_price": 0,
            "raw_additions_unverified_price": 0,
            "eligible_price_binding_missing": 0,
            "baseline_verdict_mismatches": 0,
            "accepted_invalid_prices": 0,
        }
    )
    reasons: Counter[str] = Counter()
    rows = []
    for o in observations:
        symbol = o.get("symbol")
        snapshot = snapshots.get(o.get("snapshot_id"))
        source = o.get("source_provenance") or {}
        if (
            not snapshot
            or snapshot["symbol"] != symbol
            or snapshot["provider"] != protocol["provider"]
            or snapshot["feed"] != protocol["feed"]
            or snapshot["is_mock"]
        ):
            structural_errors.append("snapshot_identity_source_or_mock_mismatch")
            continue
        if source and source.get("requested_symbol") != symbol:
            structural_errors.append("response_identity_mismatch")
            continue
        assessment = timeutils.parse_iso(o.get("assessed_at_utc"))
        if assessment is None or snapshot.get("market_session") != "regular":
            structural_errors.append("assessment_clock_or_session_invalid")
            continue
        raw_base = baseline.assess(snapshot, now=assessment).is_usable
        raw_alt = comparison.assess(snapshot, now=assessment).is_usable
        if raw_base != (o.get("freshness_status") == "usable") or raw_base != bool(
            snapshot.get("is_usable")
        ):
            metrics["baseline_verdict_mismatches"] += 1
        # A missing/duplicate response is a data failure, never silently
        # excluded from the 500-symbol denominator or borrowed from a peer.
        matched = o.get("response_status") == "matched"
        raw_base, raw_alt = bool(raw_base and matched), bool(raw_alt and matched)
        price = price_validity(o, snapshot, protocol)
        safe_base, safe_alt = raw_base and price["valid"], raw_alt and price["valid"]
        metrics["raw_baseline"] += raw_base
        metrics["raw_comparison"] += raw_alt
        metrics["safe_baseline"] += safe_base
        metrics["safe_comparison"] += safe_alt
        metrics["legacy_proxy_baseline"] += raw_base and price["legacy_age_proxy"]
        metrics["legacy_proxy_comparison"] += raw_alt and price["legacy_age_proxy"]
        metrics["eligible_price_binding_missing"] += (
            raw_alt and not price["binding_complete"]
        )
        metrics["raw_additions_invalid_price"] += (
            raw_alt
            and not raw_base
            and price["binding_complete"]
            and not price["valid"]
        )
        metrics["raw_additions_unverified_price"] += (
            raw_alt and not raw_base and not price["binding_complete"]
        )
        metrics["accepted_invalid_prices"] += safe_alt and not price["valid"]
        reasons[price["reason"]] += 1
        rows.append(
            {
                "symbol": symbol,
                "snapshot_id": o["snapshot_id"],
                "assessment_at_utc": o["assessed_at_utc"],
                "raw_baseline": raw_base,
                "raw_comparison": raw_alt,
                "safe_baseline": safe_base,
                "safe_comparison": safe_alt,
                "price_reason": price["reason"],
            }
        )
    integrity = not structural_errors and not metrics["baseline_verdict_mismatches"]
    return {
        "denominator": 500,
        "observations": len(observations),
        "integrity_ok": integrity,
        "structural_errors": sorted(set(structural_errors)),
        "metrics": dict(metrics),
        "price_reasons": dict(reasons),
        "safe_gain_symbols": metrics["safe_comparison"] - metrics["safe_baseline"],
        "raw_gain_symbols": metrics["raw_comparison"] - metrics["raw_baseline"],
        "rows_sha256": sha256(canonical(rows)).hexdigest(),
        "rows": rows,
    }


def read_captures(database, protocol, as_of):
    uri = Path(database).resolve(strict=True).as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    captures = []
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        for date in protocol["dates"]:
            for window in protocol["windows_et"]:
                start = window.split("-")[0]
                key = f"scan:{date}T{start}"
                jobs = connection.execute(
                    "SELECT id,status,scheduler_run_id,result_summary_json FROM job_runs WHERE job_type='scan' AND trigger_source='scheduler' AND lock_key=? AND started_at_utc<=? ORDER BY id",
                    (key, timeutils.to_iso(as_of)),
                ).fetchall()
                batch = None
                selected_job = None
                for job in jobs:
                    summary = json.loads(job["result_summary_json"] or "{}")
                    scan_id = (summary.get("scan_summary") or {}).get("scan_batch_id")
                    if scan_id:
                        candidate = connection.execute(
                            "SELECT * FROM scan_batches WHERE scan_batch_id=?",
                            (scan_id,),
                        ).fetchone()
                    elif job["scheduler_run_id"]:
                        candidate = connection.execute(
                            "SELECT * FROM scan_batches WHERE scheduler_run_id=? ORDER BY id LIMIT 1",
                            (job["scheduler_run_id"],),
                        ).fetchone()
                    else:
                        candidate = None
                    if candidate:
                        batch, selected_job = candidate, job
                        break  # first actual source-capture attempt; no best retry
                entry = {
                    "date": date,
                    "window_et": window,
                    "role": "retrospective"
                    if date in protocol["retrospective_dates"]
                    else "forward",
                    "capture_present": batch is not None,
                }
                if batch is None:
                    captures.append(entry)
                    continue
                assert selected_job is not None
                chunks = []
                for row in connection.execute(
                    "SELECT detail_json FROM system_events WHERE category='shadow_collection' AND created_at_utc>=? AND created_at_utc<=? ORDER BY id",
                    (batch["started_at_utc"], timeutils.to_iso(as_of)),
                ):
                    detail = json.loads(row["detail_json"] or "{}")
                    if detail.get("scan_id") == batch["scan_batch_id"]:
                        chunks.append(detail)
                observations = [
                    o for chunk in chunks for o in chunk.get("observations", [])
                ]
                snapshots = {}
                for o in observations:
                    snapshot = connection.execute(
                        f"SELECT {SNAPSHOT_COLUMNS} FROM price_snapshots WHERE snapshot_id=?",
                        (o.get("snapshot_id"),),
                    ).fetchone()
                    if snapshot:
                        snapshots[o["snapshot_id"]] = dict(snapshot)
                assessment_dates = set()
                for observation in observations:
                    parsed = timeutils.parse_iso(observation.get("assessed_at_utc"))
                    if parsed is not None:
                        assessment_dates.add(timeutils.to_et(parsed).date().isoformat())
                result = evaluate_capture(observations, snapshots, protocol)
                if (
                    assessment_dates != {date}
                    or len(chunks) != 5
                    or sorted(c.get("chunk_index") for c in chunks) != list(range(5))
                    or any(
                        c.get("scan_window") != window
                        or c.get("universe_file_version")
                        != protocol["universe_version"]
                        for c in chunks
                    )
                ):
                    result["integrity_ok"] = False
                    result["structural_errors"].append(
                        "chunk_date_window_or_version_mismatch"
                    )
                entry.update(
                    scan_id=batch["scan_batch_id"],
                    selected_job_id=selected_job["id"],
                    job_status=selected_job["status"],
                    started_at_utc=batch["started_at_utc"],
                    completed_at_utc=batch["completed_at_utc"],
                    result=result,
                )
                captures.append(entry)
    finally:
        connection.rollback()
        connection.close()
    return captures


def decide(captures, protocol, as_of, final=False):
    cutoff = timeutils.parse_iso(protocol["final_not_before_utc"])
    if cutoff is None:
        raise ValueError("invalid_frozen_cutoff")
    if final and as_of < cutoff:
        raise ValueError("final_requested_before_frozen_cutoff")
    forward = [c for c in captures if c["role"] == "forward"]
    missing = [
        f"{c['date']}:{c['window_et']}" for c in forward if not c["capture_present"]
    ]
    integrity = [
        c for c in forward if c["capture_present"] and not c["result"]["integrity_ok"]
    ]
    binding = [
        c
        for c in forward
        if c["capture_present"]
        and c["result"]["metrics"]["eligible_price_binding_missing"]
    ]
    if missing or integrity or binding:
        return {
            "outcome": "inconclusive" if final else "pending_collection",
            "missing_forward_windows": missing,
            "integrity_problem_windows": len(integrity),
            "price_binding_problem_windows": len(binding),
            "final": final,
        }
    if not final:
        return {"outcome": "pending_final_cutoff", "final": False}
    rules = protocol["decision_rules"]
    openings = [c for c in forward if c["window_et"] == protocol["windows_et"][0]]
    enough = len(forward) == rules["required_forward_windows"] and len(openings) == 2
    if not enough:
        return {
            "outcome": "inconclusive",
            "reason": "forward_window_count_mismatch",
            "final": True,
        }
    checks = [
        {
            "date": c["date"],
            "safe_comparison": c["result"]["metrics"]["safe_comparison"],
            "safe_baseline": c["result"]["metrics"]["safe_baseline"],
            "gain": c["result"]["safe_gain_symbols"],
            "pass": c["result"]["metrics"]["safe_comparison"]
            >= rules["opening_safe_comparison_min_usable_symbols_each_forward_day"]
            and c["result"]["safe_gain_symbols"]
            >= rules["opening_safe_gain_min_symbols_each_forward_day"],
        }
        for c in openings
    ]
    invalid = sum(c["result"]["metrics"]["accepted_invalid_prices"] for c in forward)
    raw_risk = sum(
        c["result"]["metrics"]["raw_additions_invalid_price"] for c in forward
    )
    return {
        "outcome": "pass"
        if all(c["pass"] for c in checks) and invalid == 0
        else "fail",
        "opening_checks": checks,
        "accepted_invalid_prices": invalid,
        "raw_extra_observations_with_invalid_prices": raw_risk,
        "direct_env_bar_change": "not_supported"
        if raw_risk
        else "no_stale_addition_observed_in_small_sample",
        "final": True,
        "meaning": "availability pilot only; not profitability or permission to change baseline/release suspension",
    }


def run(database, protocol_path, as_of=None, final=False):
    protocol = load_protocol(protocol_path)
    as_of = as_of or timeutils.now_utc()
    captures = read_captures(database, protocol, as_of)
    return {
        "version": VERSION,
        "protocol_sha256": protocol["protocol_sha256"],
        "observed_at_utc": timeutils.to_iso(as_of),
        "decision": decide(captures, protocol, as_of, final),
        "captures": captures,
        "formal_evaluation_consumed": False,
        "limitations": [
            "October14 is retrospective; only two forward opening samples.",
            "Same-symbol/date/window observations are correlated, not independent trials.",
            "Legacy price-timestamp screens are descriptive when normalized source binding is absent.",
            "No P&L, fill, slippage, strategy profitability or live execution inference.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/alphaos.db")
    parser.add_argument("--protocol", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run(args.db, args.protocol, final=args.final)
        Path(args.output).write_text(
            json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
        )
        print(
            json.dumps(
                {"decision": report["decision"], "report_path": args.output},
                sort_keys=True,
            )
        )
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"status": "offline_trial_failed", "error_type": type(exc).__name__}
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
