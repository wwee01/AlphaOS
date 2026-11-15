"""Offline, evidence-only swing policy. No settings, providers or runtime wiring.

Per-capture assessment is pure. The explicit replay reader opens SQLite read-only;
the audit writer creates new private, content-addressed files without replacement.
Neither research eligibility nor its paired baseline grants trading authority.
"""

from __future__ import annotations

import argparse
import calendar
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from alphaos.research import data_freshness_trial as trial
from alphaos.util import timeutils

POLICY_ID = "swing_research_freshness_v2"
AUDIT_VERSION = "swing_research_audit_v2"
CONTRACT_SHA256 = "c32965e9398b584e1a83b60689f723d1f48fe4490107b4e7c370e3ad8c49a8de"
REFERENCE_SHA256 = "00debb672f0f76525dd74d3314e4e774d881608f1be40d92ce63739b9b6a7df0"
# Allowlisted calendar attestations extracted from already-retained private
# observer reports. No new request, computed future calendar, or default hours.
_SESSION_EVIDENCE = {'2026-10-14': {'close': '16:00',
                'date': '2026-10-14',
                'open': '09:30',
                'source': 'broker_calendar',
                'report_sha256': '1318527091749040ccb42972d7b35676718393f6cba75783d7d32bb6c7f6a253',
                'calendar_response_sha256': 'ca23c613aaa87532d03b5d0fa858b710f009f4901e3b3b278953ce6295b3c5d1',
                'requested_at_utc': '2026-10-14T14:13:57.196576+00:00',
                'received_at_utc': '2026-10-14T14:13:57.429755+00:00'},
 '2026-10-15': {'close': '16:00',
                'date': '2026-10-15',
                'open': '09:30',
                'source': 'broker_calendar',
                'report_sha256': 'b3ba17d7516585c5e4ed2088c73bfd5c0e5c2d3cabb93fe77add00a4743483a2',
                'calendar_response_sha256': '73d8214909ad04c978ec944235ad6c52c3d2bae4cb29af921bd8d267264248e9',
                'requested_at_utc': '2026-10-15T04:01:16.114218+00:00',
                'received_at_utc': '2026-10-15T04:01:16.343993+00:00'},
 '2026-10-16': {'close': '16:00',
                'date': '2026-10-16',
                'open': '09:30',
                'source': 'broker_calendar',
                'report_sha256': '9f33ca2bc839098652ab35d2868cac6d020c8fab3c19fb79d565c8696fb58de2',
                'calendar_response_sha256': 'b8d62abef8d702851af447464e973d10d9dd8da4ecc1147576627880d648a7f0',
                'requested_at_utc': '2026-10-16T04:04:47.158235+00:00',
                'received_at_utc': '2026-10-16T04:04:47.386598+00:00'}}
SESSION_EVIDENCE_SHA256 = sha256(trial.canonical(_SESSION_EVIDENCE)).hexdigest()

MANIFEST = {
    "policy_id": POLICY_ID, "contract_sha256": CONTRACT_SHA256,
    "provider": "alpaca", "feed": "iex", "universe_version": 2,
    "universe_file_sha256": "bb814b20e3b4806125397244481cdb33eb2b37ef23fdd7143e4c5769db694433",
    "quote_seconds": 60, "minute_bar_seconds": 300, "trade_seconds": 180,
    "paired_baseline_bar_seconds": 180, "future_tolerance_seconds": 5,
    "price_evidence_version": "normalized_last_price_v1",
    "calendar": "retained_explicit_session_bounds_fail_closed",
    "session_evidence_sha256": SESSION_EVIDENCE_SHA256,
    "supersedes_policy_id": "swing_research_freshness_v1",
}
POLICY_SHA256 = sha256(trial.canonical(MANIFEST)).hexdigest()
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.(\d{1,9}))?(?:Z|[+-]\d{2}:\d{2})")
_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_HASH = re.compile(r"[a-f0-9]{64}")


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _token(value: Any) -> str | None:
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else None


def _stamp(value: Any) -> tuple[datetime, Decimal] | None:
    """Explicit offsets only; retain nanoseconds for inclusive age boundaries."""
    match = _STAMP.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        whole = calendar.timegm(at.replace(microsecond=0).utctimetuple())
        return at, Decimal(whole) + Decimal("0." + (match[1] or "0"))
    except (ValueError, OverflowError):
        return None


def _time(value: Any) -> str | None:
    return value if _stamp(value) is not None else None


def _number(value: Any) -> float | None:
    try:
        return None if isinstance(value, bool) else trial.numeric(value)
    except OverflowError:
        return None


def _valid_protocol(protocol: dict) -> None:
    body = {k: v for k, v in protocol.items() if k != "protocol_sha256"}
    if (sha256(trial.canonical(body)).hexdigest() != trial.PROTOCOL_SHA256
            or protocol.get("protocol_sha256") != trial.PROTOCOL_SHA256):
        raise ValueError("frozen_protocol_mismatch")


def _in_window(at: datetime, date: str, window: str) -> bool:
    et = timeutils.to_et(at)
    start, end = window.split("-")
    return et.date().isoformat() == date and start <= et.strftime("%H:%M") < end


def session_bounds(date: str) -> dict | None:
    """Only pinned, date-bound broker-calendar attestations certify hours.

    The shared holiday/session calendar remains an additional rejection gate;
    its default 16:00 close is never positive proof of session membership.
    Unknown/malformed boundaries fail closed. No filesystem/network access.
    """
    record = _mapping(_SESSION_EVIDENCE.get(date))
    if (record.get("date") != date or record.get("source") != "broker_calendar"
            or any(not isinstance(record.get(k), str) or not _HASH.fullmatch(record[k])
                   for k in ("report_sha256", "calendar_response_sha256"))):
        return None
    request, receipt = _stamp(record.get("requested_at_utc")), _stamp(record.get("received_at_utc"))
    if not request or not receipt or request[1] > receipt[1]:
        return None
    try:
        def bound(key):
            value = record.get(key)
            if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value):
                raise ValueError("invalid session bound")
            return datetime.strptime(date + "T" + value, "%Y-%m-%dT%H:%M").replace(
                tzinfo=ZoneInfo("America/New_York"))
        opening, closing = bound("open"), bound("close")
        if (record["open"] != "09:30" or not "09:30" < record["close"] <= "16:00"
                or timeutils.market_session(opening).value != "regular"):
            return None
    except (ValueError, TypeError):
        return None
    return {**record, "open_at_utc": opening.astimezone(timezone.utc).isoformat(),
            "close_at_utc": closing.astimezone(timezone.utc).isoformat(),
            "evidence_sha256": sha256(trial.canonical(record)).hexdigest()}


def _within_session(stamp: tuple[datetime, Decimal], bounds: dict) -> bool:
    opening, closing = _stamp(bounds["open_at_utc"]), _stamp(bounds["close_at_utc"])
    assert opening is not None and closing is not None
    return opening[1] <= stamp[1] < closing[1]


def assess_observation(observation: dict, snapshot: dict, *, date: str,
                       window_et: str) -> dict:
    """Provisional row check; evaluate_capture also enforces capture lineage.

    Output is allowlisted evidence, never a candidate or final capture verdict.
    """
    o, s = _mapping(observation), _mapping(snapshot)
    p = _mapping(o.get("source_provenance"))
    parts = _mapping(p.get("parts"))
    reasons = []
    symbol, sid = _token(o.get("symbol")), _token(o.get("snapshot_id"))
    if (not symbol or not sid or s.get("symbol") != symbol
            or s.get("snapshot_id") != sid or p.get("requested_symbol") != symbol):
        reasons.append("symbol_or_snapshot_binding")
    if (s.get("provider") != "alpaca" or s.get("feed") != "iex"
            or s.get("is_mock") not in (False, 0)
            or p.get("provider") != "alpaca" or p.get("feed") != "iex"
            or p.get("version") != "alpaca_snapshot_sources_v1"
            or p.get("response_present") is not True
            or p.get("transport_status") != "received"):
        reasons.append("source_or_transport")
    response_hash = p.get("response_sha256")
    if not isinstance(response_hash, str) or not _HASH.fullmatch(response_hash):
        response_hash = None
        reasons.append("missing_response_hash")
    if o.get("response_status") != "matched":
        reasons.append("unmatched_response")

    timestamps = {
        "assessment": o.get("assessed_at_utc"), "requested": p.get("requested_at_utc"),
        "received": p.get("received_at_utc"), "snapshot_received": s.get("received_at"),
        "observation_received": o.get("received_at_utc"),
        "quote": _mapping(parts.get("latestQuote")).get("timestamp"),
        "minute_bar": _mapping(parts.get("minuteBar")).get("timestamp"),
        "trade": _mapping(parts.get("latestTrade")).get("timestamp"),
        "normalized_price": p.get("normalized_last_price_timestamp"),
        "snapshot_quote": s.get("quote_timestamp"),
        "snapshot_bar": s.get("bar_timestamp"),
        "snapshot_source": s.get("source_timestamp"),
    }
    parsed = {k: _stamp(v) for k, v in timestamps.items()}
    for key, value in parsed.items():
        if value is None:
            reasons.append("invalid_timestamp:" + key)
    assessment = parsed["assessment"]
    bounds = session_bounds(date)
    if bounds is None:
        reasons.append("session_boundary_unverified")
    else:
        for key in ("assessment", "quote", "minute_bar", "trade"):
            component = parsed[key]
            if component is not None and not _within_session(component, bounds):
                reasons.append("outside_verified_session:" + key)
    if assessment is not None:
        if (s.get("market_session") != "regular"
                or timeutils.market_session(assessment[0]).value != "regular"
                or not _in_window(assessment[0], date, window_et)):
            reasons.append("session_date_or_window")
    if all(parsed[k] is not None for k in ("requested", "received", "assessment")):
        request, receipt = parsed["requested"], parsed["received"]
        assert request is not None and receipt is not None and assessment is not None
        if not request[1] <= receipt[1] <= assessment[1]:
            reasons.append("request_receipt_assessment_order")
    for key in ("snapshot_received", "observation_received"):
        if parsed[key] is not None and parsed[key] != parsed["received"]:
            reasons.append("receipt_binding:" + key)
    for name, field, part in (("quote", "quote_timestamp", "latestQuote"),
                              ("minute_bar", "bar_timestamp", "minuteBar")):
        if (_mapping(parts.get(part)).get("present") is not True
                or timestamps[name] != s.get(field)):
            reasons.append("component_binding:" + name)
    if s.get("source_timestamp") != s.get("quote_timestamp"):
        reasons.append("quote_source_binding")
    if p.get("normalized_bar_source") != "minuteBar":
        reasons.append("minute_bar_required")
    if _mapping(parts.get("latestTrade")).get("present") is not True:
        reasons.append("trade_component_required")

    ages: dict[str, float | None] = {}
    exact_ages: dict[str, Decimal | None] = {}
    for key in ("quote", "minute_bar", "trade"):
        component = parsed[key]
        age = assessment[1] - component[1] if assessment and component else None
        exact_ages[key] = age
        ages[key] = float(age) if age is not None else None
        if age is not None and age < -5:
            reasons.append("future_timestamp:" + key)
    for key, cap in (("quote", 60), ("trade", 180)):
        age = exact_ages[key]
        if age is not None and age > cap:
            reasons.append("stale:" + key)

    values = {"persisted": _number(s.get("last_price")),
              "normalized": _number(p.get("normalized_last_price")),
              "raw_trade": _number(p.get("raw_latest_trade_price"))}
    if any(v is None or v <= 0 for v in values.values()):
        reasons.append("invalid_price_value")
    # Reuse frozen binding logic after validating nested types and strict clocks.
    price = trial.price_validity(
        {"assessed_at_utc": _time(timestamps["assessment"]) or "1970-01-01T00:00:00Z",
         "source_provenance": {
             "parts": {"latestTrade": {"timestamp": _time(timestamps["trade"])}},
             "price_evidence_version": p.get("price_evidence_version"),
             "normalized_last_price_source": p.get("normalized_last_price_source"),
             "normalized_last_price_timestamp": p.get("normalized_last_price_timestamp"),
             "normalized_last_price": values["normalized"],
             "raw_latest_trade_price": values["raw_trade"],
         }}, {"last_price": values["persisted"]}, {"future_timestamp_tolerance_seconds": 5,
                                                   "last_price_age_seconds": 180,
                                                   "price_evidence_version": "normalized_last_price_v1"})
    if not price["valid"]:
        reasons.append("price:" + price["reason"])

    baseline = None
    if assessment and parsed["snapshot_quote"] and parsed["snapshot_bar"]:
        original = trial.guard(60, 180).assess(s, now=assessment[0])
        baseline = original.is_usable
        if (s.get("is_usable") not in (False, True, 0, 1)
                or baseline != bool(s.get("is_usable"))
                or original.freshness_status != s.get("freshness_status")
                or original.freshness_status != o.get("freshness_status")):
            reasons.append("recorded_baseline_mismatch")
    bar_age = exact_ages["minute_bar"]
    paired_baseline = not reasons and bar_age is not None and bar_age <= 180
    if bar_age is not None and bar_age > 300:
        reasons.append("stale:minute_bar")
    return {
        "symbol": symbol, "snapshot_id": sid, "provider": "alpaca", "feed": "iex",
        "response_sha256": response_hash,
        "session_evidence_sha256": bounds["evidence_sha256"] if bounds else None,
        "timestamps": {k: _time(v) for k, v in timestamps.items()},
        "age_seconds": ages,
        "future_skew_tolerated": {k: v is not None and -5 <= v < 0
                                   for k, v in exact_ages.items()},
        "price_source": "latestTrade" if p.get("normalized_last_price_source") == "latestTrade" else "unverified_or_fallback",
        "price_values": values, "price_binding_complete": price["binding_complete"],
        "recorded_baseline_usable": s.get("is_usable") in (True, 1),
        "baseline_freshness_usable": baseline,
        "paired_baseline_eligible": paired_baseline,
        "research_eligible": not reasons, "reasons": sorted(set(reasons)),
    }


def evaluate_capture(capture: dict, protocol: dict) -> dict:
    """Assess all 500 names; any capture-integrity failure blocks admissions."""
    _valid_protocol(protocol)
    expected = protocol["symbols"]
    date, window = capture.get("date"), capture.get("window_et")
    if (not isinstance(date, str) or not isinstance(window, str)
            or window not in protocol["windows_et"]):
        raise ValueError("invalid_capture_date_or_window")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("invalid_capture_date") from exc
    errors = []
    if (capture.get("trigger_source") != "scheduler"
            or capture.get("lock_key") != f"scan:{date}T{window.split('-')[0]}"
            or not _token(capture.get("scan_id"))
            or type(capture.get("selected_job_id")) is not int
            or capture["selected_job_id"] <= 0):
        errors.append("scheduled_capture_identity")
    started, completed = (_stamp(capture.get(k)) for k in ("started_at_utc", "completed_at_utc"))
    if (not started or not completed or started[1] > completed[1]
            or not _in_window(started[0], date, window)):
        errors.append("capture_clock")
    bounds = session_bounds(date)
    if bounds is None:
        errors.append("session_boundary_unverified")
    elif started is not None and not _within_session(started, bounds):
        errors.append("capture_outside_verified_session")
    chunks = capture.get("chunks")
    if not isinstance(chunks, list):
        chunks = []
    if len(chunks) != 5 or any(not isinstance(c, dict) for c in chunks):
        errors.append("missing_or_invalid_chunks")
    chunks = [c for c in chunks if isinstance(c, dict)]
    if (any(type(c.get("chunk_index")) is not int for c in chunks)
            or Counter(str(c.get("chunk_index")) for c in chunks) != Counter(map(str, range(5)))):
        errors.append("chunk_indices")
    observations = []
    lineage_ids = set()
    lineage_records = set()
    for chunk in chunks:
        obs = chunk.get("observations")
        if (not isinstance(obs, list) or len(obs) != 100
                or any(not isinstance(o, dict) for o in obs)):
            errors.append("chunk_observation_count_or_shape")
            obs = [o for o in (obs if isinstance(obs, list) else []) if isinstance(o, dict)]
        observations.extend(obs)
        lineage = _mapping(chunk.get("lineage_capture"))
        line_start, line_end = (_stamp(lineage.get(k)) for k in ("started_at_utc", "completed_at_utc"))
        lid = _token(lineage.get("lineage_id"))
        lineage_ids.add(lid)
        lineage_records.add((lid, _time(lineage.get("started_at_utc")),
                             _time(lineage.get("completed_at_utc"))))
        if (chunk.get("version") != "shadow_chunk_assessment_v1"
                or chunk.get("scan_id") != capture.get("scan_id")
                or chunk.get("scan_window") != window
                or chunk.get("universe_file_version") != 2 or chunk.get("requested") != 100
                or not lid or lineage.get("version") != "shadow_scan_lineage_v1"
                or lineage.get("status") != "recorded" or lineage.get("scope") != "single_shadow_scan"
                or not line_start or not line_end or not started
                or not started[1] <= line_start[1] <= line_end[1]):
            errors.append("chunk_or_lineage_binding")
        chunk_receipts = set()
        for o in obs:
            p = _mapping(o.get("source_provenance"))
            chunk_receipts.add(tuple(p.get(k) if isinstance(p.get(k), str) else None
                                     for k in ("response_sha256", "requested_at_utc", "received_at_utc")))
            request, assessment = _stamp(p.get("requested_at_utc")), _stamp(o.get("assessed_at_utc"))
            if (not request or not assessment or not started or not completed or not line_end
                    or not started[1] <= line_end[1] <= request[1] <= assessment[1] <= completed[1]):
                errors.append("capture_or_lineage_clock_binding")
        if len(chunk_receipts) != 1:
            errors.append("chunk_response_binding")
    if len(lineage_ids) != 1 or len(lineage_records) != 1:
        errors.append("scan_lineage_identity")
    counts = Counter(_token(o.get("symbol")) for o in observations)
    if set(counts) != set(expected) or any(n != 1 for n in counts.values()):
        errors.append("fixed_universe_membership")
    snapshot_counts = Counter(_token(o.get("snapshot_id")) for o in observations)
    if None in snapshot_counts or any(n != 1 for n in snapshot_counts.values()):
        errors.append("snapshot_identity_duplicates")
    snapshots = _mapping(capture.get("snapshots"))
    by_symbol = {o.get("symbol"): o for o in observations if _token(o.get("symbol"))}
    rows = []
    for symbol in expected:
        o = by_symbol.get(symbol, {"symbol": symbol})
        sid = _token(o.get("snapshot_id"))
        snapshot = _mapping(snapshots.get(sid))
        row = assess_observation(o, snapshot, date=date, window_et=window)
        if errors:
            row["research_eligible"] = row["paired_baseline_eligible"] = False
            row["reasons"] = sorted(set(row["reasons"] + ["capture_integrity_failure"]))
        rows.append(row)
    return {
        "date": date, "window_et": window, "scan_id": _token(capture.get("scan_id")),
        "selected_job_id": capture.get("selected_job_id") if type(capture.get("selected_job_id")) is int else None,
        "selection": "first_scheduled_source_capture",
        "universe_version": 2, "lineage_ids": sorted(x for x in lineage_ids if x),
        "session_evidence": bounds,
        "started_at_utc": _time(capture.get("started_at_utc")),
        "completed_at_utc": _time(capture.get("completed_at_utc")),
        "integrity_ok": not errors, "structural_errors": sorted(set(errors)),
        "denominator": 500, "received_observations": len(observations),
        "missing_symbols": sum(s not in counts for s in expected),
        "duplicate_observations": sum(max(0, n - 1) for n in counts.values()),
        "research_eligible": sum(r["research_eligible"] for r in rows),
        "paired_baseline_eligible": sum(r["paired_baseline_eligible"] for r in rows),
        "rows": rows,
    }


def read_frozen_captures(database: Path, protocol: dict, as_of: str) -> list[dict]:
    """Existing ledger only, fixed trial dates, first capture, no schema writes."""
    _valid_protocol(protocol)
    if _stamp(as_of) is None:
        raise ValueError("invalid_replay_cutoff")
    connection = sqlite3.connect(database.resolve(strict=True).as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    captures = []
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        for date in protocol["dates"]:
            for window in protocol["windows_et"]:
                key = f"scan:{date}T{window.split('-')[0]}"
                jobs = connection.execute(
                    "SELECT id,status,scheduler_run_id,result_summary_json,trigger_source,lock_key "
                    "FROM job_runs WHERE job_type='scan' AND trigger_source='scheduler' "
                    "AND lock_key=? AND started_at_utc<=? ORDER BY id", (key, as_of)).fetchall()
                batch = job = None
                for candidate_job in jobs:
                    summary = json.loads(candidate_job["result_summary_json"] or "{}")
                    scan_id = _mapping(summary.get("scan_summary")).get("scan_batch_id")
                    if scan_id:
                        candidate = connection.execute("SELECT * FROM scan_batches WHERE scan_batch_id=?", (scan_id,)).fetchone()
                    else:
                        candidate = connection.execute("SELECT * FROM scan_batches WHERE scheduler_run_id=? ORDER BY id LIMIT 1", (candidate_job["scheduler_run_id"],)).fetchone()
                    if candidate is not None:
                        batch, job = candidate, candidate_job
                        break
                if batch is None or job is None:
                    raise ValueError("missing_frozen_capture:" + date + ":" + window)
                chunks = []
                for event in connection.execute(
                        "SELECT detail_json FROM system_events WHERE category='shadow_collection' "
                        "AND created_at_utc>=? AND created_at_utc<=? ORDER BY id", (batch["started_at_utc"], as_of)):
                    chunk = json.loads(event[0] or "{}")
                    if chunk.get("scan_id") == batch["scan_batch_id"]:
                        chunks.append(chunk)
                snapshots = {}
                for chunk in chunks:
                    for o in chunk.get("observations", []):
                        row = connection.execute(f"SELECT {trial.SNAPSHOT_COLUMNS} FROM price_snapshots WHERE snapshot_id=?", (o.get("snapshot_id"),)).fetchone()
                        if row is not None:
                            snapshots[o["snapshot_id"]] = dict(row)
                captures.append({"date": date, "window_et": window, "scan_id": batch["scan_batch_id"],
                                 "selected_job_id": job["id"], "trigger_source": job["trigger_source"],
                                 "lock_key": job["lock_key"], "started_at_utc": batch["started_at_utc"],
                                 "completed_at_utc": batch["completed_at_utc"],
                                 "chunks": chunks, "snapshots": snapshots})
    finally:
        connection.rollback()
        connection.close()
    return captures


def code_identity() -> dict:
    """Hash the complete local policy dependency chain without importing runtime."""
    root = Path(__file__).resolve().parents[1]
    files = ("research/swing_data_policy.py", "research/data_freshness_trial.py",
             "data/freshness_guard.py", "util/timeutils.py", "util/market_calendar.py", "constants.py")
    return {"alphaos/" + name: sha256((root / name).read_bytes()).hexdigest() for name in files}


def build_report(captures: list[dict], protocol: dict) -> dict:
    return {
        "audit_version": AUDIT_VERSION, "policy": dict(MANIFEST), "policy_sha256": POLICY_SHA256,
        "protocol_sha256": trial.PROTOCOL_SHA256, "code_identity": code_identity(),
        "scope": "offline_research_availability_only", "runtime_integration": False,
        "warnings": ["Data eligibility grants no tradeability, candidate, order or labelling authority.",
                     "Existing liquidity/spread/risk gates and size/liquidity warning still apply.",
                     "Paper results are not live-equivalent; no profitability inference.",
                     "Only pinned retained calendar dates have verified hours; unknown session boundaries fail closed.",
                     "Response hashes are trusted retained lineage attestations, not independently reconstructed body hashes."],
        "captures": [evaluate_capture(c, protocol) for c in captures],
    }


def append_audit(captures: list[dict], protocol: dict, directory: Path) -> Path:
    """One new file per report; duplicates/collisions fail without replacement.

    Link a fully fsynced private temporary file into place atomically. This is
    application-level append-only storage, not a filesystem immutability claim.
    Unknown input fields never enter build_report's allowlisted audit schema.
    """
    payload = trial.canonical(build_report(captures, protocol)) + b"\n"
    directory.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = ".pending-" + uuid4().hex
    name = POLICY_ID + "-" + sha256(payload).hexdigest() + ".json"
    created = False
    try:
        out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        created = True
        with os.fdopen(out, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        os.fsync(fd)
    finally:
        try:
            if created:
                os.unlink(temporary, dir_fd=fd)
        finally:
            os.close(fd)
    return directory / name


def replay_frozen(database: Path, protocol_path: Path, reference_path: Path) -> tuple[list[dict], dict, dict]:
    """Recheck completed evidence; this does not invoke formal evaluation."""
    raw = reference_path.read_bytes()
    if sha256(raw).hexdigest() != REFERENCE_SHA256:
        raise ValueError("frozen_final_reference_mismatch")
    reference = json.loads(raw)
    protocol = trial.load_protocol(protocol_path)
    captures = read_frozen_captures(database, protocol, reference["observed_at_utc"])
    strict = [evaluate_capture(c, protocol) for c in captures]
    comparisons = []
    raw_invalid = 0
    for capture, result, prior in zip(captures, strict, reference["captures"], strict=True):
        if capture["date"] not in protocol["forward_dates"]:
            continue
        obs = [o for chunk in capture["chunks"] for o in chunk["observations"]]
        frozen = trial.evaluate_capture(obs, capture["snapshots"], protocol)
        raw_invalid += frozen["metrics"]["raw_additions_invalid_price"]
        comparisons.append({
            "date": capture["date"], "window_et": capture["window_et"],
            "capture_identity_matches": (capture["scan_id"] == prior["scan_id"]
                                         and capture["selected_job_id"] == prior["selected_job_id"]),
            "frozen_rows_hash_matches": frozen["rows_sha256"] == prior["result"]["rows_sha256"],
            "frozen_research_eligible": frozen["metrics"]["safe_comparison"],
            "strict_research_eligible": result["research_eligible"],
            "strict_paired_baseline_eligible": result["paired_baseline_eligible"],
            "frozen_paired_baseline_eligible": frozen["metrics"]["safe_baseline"],
            "strict_integrity_ok": result["integrity_ok"],
        })
    matches = (len(comparisons) == 6
               and raw_invalid == reference["decision"]["raw_extra_observations_with_invalid_prices"]
               and all(
        r["capture_identity_matches"] and r["frozen_rows_hash_matches"] and r["strict_integrity_ok"]
        and r["frozen_research_eligible"] == r["strict_research_eligible"]
        and r["frozen_paired_baseline_eligible"] == r["strict_paired_baseline_eligible"]
        for r in comparisons))
    return captures, protocol, {"reference_sha256": REFERENCE_SHA256, "forward_windows": comparisons,
                                "raw_additions_invalid_price": raw_invalid,
                                "strict_contract_reproduces_frozen_counts": matches}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--audit-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        captures, protocol, comparison = replay_frozen(args.db, args.protocol, args.reference)
        path = append_audit(captures, protocol, args.audit_dir)
        print(json.dumps({"audit_path": str(path), "comparison": comparison}, sort_keys=True))
        return 0 if comparison["strict_contract_reproduces_frozen_counts"] else 2
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"status": "offline_replay_failed", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
