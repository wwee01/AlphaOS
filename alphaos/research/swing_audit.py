"""Availability-only prospective audit. No settings, providers, AI or orders."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from alphaos.research import data_freshness_trial as trial
from alphaos.research import swing_data_policy as v2
from alphaos.research import swing_session_evidence as sessions

POLICY_ID = "swing_research_freshness_v3"
ACTIVATION_VERSION = "swing_audit_activation_v1"
MAX_CAPTURES = 3
SOURCE_FILES = (
    "alphaos/research/swing_audit.py",
    "alphaos/research/swing_session_evidence.py",
    "alphaos/research/swing_audit_worker.py",
    "alphaos/research/swing_data_policy.py",
    "alphaos/research/data_freshness_trial.py",
    "alphaos/data/freshness_guard.py",
    "alphaos/util/timeutils.py",
    "alphaos/util/market_calendar.py",
    "alphaos/constants.py",
    "alphaos/scheduler/worker.py",
    "docs/research/three-session-freshness-protocol-20261014.json",
)
SESSION_REASONS = {"session_boundary_unverified", "capture_outside_verified_session"}


def source_identity(root: Path) -> dict:
    return {p: sha256((root / p).read_bytes()).hexdigest() for p in SOURCE_FILES}


def activation(path: Path, root: Path) -> dict:
    value = json.loads(sessions.private_bytes(path, 65536))
    required = {
        "version",
        "enabled",
        "policy_id",
        "cohort_id",
        "start_at_utc",
        "end_at_utc",
        "dates",
        "source_sha256",
        "observer_directory",
        "audit_directory",
        "calendar_lease_seconds",
        "hold10_reference_revision",
    }
    if (
        set(value) != required
        or value["version"] != ACTIVATION_VERSION
        or value["policy_id"] != POLICY_ID
        or type(value["enabled"]) is not bool
        or value["calendar_lease_seconds"] != 900
        or not v2._token(value["cohort_id"])
        or value["source_sha256"] != source_identity(root)
    ):
        raise ValueError("activation_or_source_pin_mismatch")
    start, end = v2._stamp(value["start_at_utc"]), v2._stamp(value["end_at_utc"])
    dates = value["dates"]
    if (
        not start
        or not end
        or not 0 < end[1] - start[1] <= 7 * 86400
        or not isinstance(dates, list)
        or not 1 <= len(dates) <= 5
        or dates != sorted(set(dates))
    ):
        raise ValueError("invalid_bounded_cohort")
    for day in dates:
        parsed = datetime.strptime(day, "%Y-%m-%d")
        if not start[0].date() <= parsed.date() <= end[0].date():
            raise ValueError("cohort_date_outside_activation")
    sessions.private_directory(Path(value["observer_directory"]))
    sessions.private_directory(Path(value["audit_directory"]))
    if Path(value["audit_directory"]).resolve() in (
        root.resolve(),
        (root / "data").resolve(),
    ):
        raise ValueError("separate_audit_directory_required")
    return value


def evaluate(capture: dict, protocol: dict, catalog: list[dict], cohort: str) -> dict:
    """Reuse every v2 data/integrity check; replace only its pinned-day guard."""
    result = v2.evaluate_capture(capture, protocol)
    errors = [x for x in result["structural_errors"] if x not in SESSION_REASONS]
    source_observations = {
        o.get("symbol"): o for c in capture["chunks"] for o in c["observations"]
    }
    evidence = {}
    for row in result["rows"]:
        o = source_observations.get(row["symbol"], {"symbol": row["symbol"]})
        s = capture["snapshots"].get(o.get("snapshot_id"), {})
        fresh = v2.assess_observation(
            o, s, date=capture["date"], window_et=capture["window_et"]
        )
        reasons = [
            x
            for x in fresh["reasons"]
            if x != "session_boundary_unverified"
            and not x.startswith("outside_verified_session:")
        ]
        bounds, status = sessions.bounds_at(
            catalog, capture["date"], o.get("assessed_at_utc", "")
        )
        if bounds is None:
            reasons.append("calendar:" + status)
        else:
            key = sha256(trial.canonical(bounds)).hexdigest()
            evidence[key] = bounds
            for name in ("assessment", "quote", "minute_bar", "trade"):
                stamp = v2._stamp(fresh["timestamps"][name])
                if stamp is not None and not v2._within_session(stamp, bounds):
                    reasons.append("outside_verified_session:" + name)
            fresh["session_evidence_sha256"] = key
        fresh["calendar_status"] = status
        fresh["reasons"] = sorted(
            set(reasons + (["capture_integrity_failure"] if errors else []))
        )
        fresh["research_eligible"] = not fresh["reasons"]
        age = fresh["age_seconds"]["minute_bar"]
        fresh["paired_baseline_eligible"] = (
            fresh["research_eligible"] and age is not None and age <= 180
        )
        row.clear()
        row.update(fresh)
    start = v2._stamp(capture["started_at_utc"])
    # Admission is anchored to assessments; start additionally must be within
    # the same verified session, without requiring a receipt before scan start.
    if (
        start
        and evidence
        and not all(v2._within_session(start, b) for b in evidence.values())
    ):
        errors.append("capture_outside_verified_session")
        for row in result["rows"]:
            row["research_eligible"] = row["paired_baseline_eligible"] = False
            row["reasons"] = sorted(set(row["reasons"] + ["capture_integrity_failure"]))
    result.update(
        policy_id=POLICY_ID,
        cohort_id=cohort,
        session_evidence=evidence,
        structural_errors=sorted(set(errors)),
        integrity_ok=not errors,
        research_eligible=sum(r["research_eligible"] for r in result["rows"]),
        paired_baseline_eligible=sum(
            r["paired_baseline_eligible"] for r in result["rows"]
        ),
    )
    result["availability_only"] = True
    result["hold10_enrolled"] = False
    result["execution_authority"] = False
    result["calendar_lease_seconds"] = 900
    statuses = {r["calendar_status"] for r in result["rows"]}
    result["status"] = (
        "blocked_integrity"
        if errors
        else "expected_no_session"
        if statuses == {"verified_closed_day"}
        else "blocked_calendar"
        if statuses != {"verified"}
        else "expected_no_session"
        if all(
            "outside_verified_session:assessment" in r["reasons"]
            for r in result["rows"]
        )
        else "completed_zero_eligible"
        if not result["research_eligible"]
        else "completed"
    )
    return result


def read_captures(database: Path, cfg: dict, protocol: dict) -> list[dict]:
    """A single bounded SQLite read snapshot, first actual scheduled captures."""
    v2._valid_protocol(protocol)
    connection = sqlite3.connect(
        database.resolve(strict=True).as_uri() + "?mode=ro", uri=True, timeout=0.25
    )
    connection.row_factory = sqlite3.Row
    deadline = time.monotonic() + 6
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    output = []
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        for date in cfg["dates"]:
            for window in protocol["windows_et"]:
                key = f"scan:{date}T{window.split('-')[0]}"
                jobs = connection.execute(
                    "SELECT id,status,scheduler_run_id,result_summary_json,started_at_utc FROM job_runs "
                    "WHERE job_type='scan' AND trigger_source='scheduler' AND lock_key=? ORDER BY id LIMIT 20",
                    (key,),
                ).fetchall()
                if len(jobs) == 20:
                    raise ValueError("ambiguous_job_limit")
                for job in jobs:
                    # A cohort cannot adopt a pre-activation attempt or a later
                    # favorable retry of that same window.
                    job_start = v2._stamp(job["started_at_utc"])
                    cohort_start = v2._stamp(cfg["start_at_utc"])
                    assert cohort_start is not None
                    if job_start is None or job_start[1] < cohort_start[1]:
                        break
                    summary = json.loads(job["result_summary_json"] or "{}")
                    scan = v2._mapping(summary.get("scan_summary")).get("scan_batch_id")
                    batch = (
                        connection.execute(
                            "SELECT * FROM scan_batches WHERE scan_batch_id=?", (scan,)
                        ).fetchone()
                        if scan
                        else connection.execute(
                            "SELECT * FROM scan_batches WHERE scheduler_run_id=? ORDER BY id LIMIT 1",
                            (job["scheduler_run_id"],),
                        ).fetchone()
                    )
                    if batch is None:
                        continue
                    if (
                        job["status"] not in ("completed", "failed")
                        or batch["status"] != "completed"
                    ):
                        break
                    chunks = []
                    events = connection.execute(
                        "SELECT detail_json FROM system_events WHERE category='shadow_collection' "
                        "AND created_at_utc>=? AND created_at_utc<=? ORDER BY id LIMIT 100",
                        (batch["started_at_utc"], batch["completed_at_utc"]),
                    ).fetchall()
                    if len(events) == 100:
                        raise ValueError("shadow_event_limit")
                    for event in events:
                        detail = json.loads(event[0])
                        if detail.get("scan_id") == batch["scan_batch_id"]:
                            chunks.append(detail)
                    ids = [
                        o.get("snapshot_id")
                        for c in chunks
                        for o in c.get("observations", [])
                    ]
                    if len(ids) > 500:
                        raise ValueError("capture_row_limit")
                    snapshots = {}
                    if ids:
                        for s in connection.execute(
                            "SELECT "
                            + trial.SNAPSHOT_COLUMNS
                            + " FROM price_snapshots WHERE snapshot_id IN ("
                            + ",".join("?" for _ in ids)
                            + ")",
                            ids,
                        ):
                            snapshots[s["snapshot_id"]] = dict(s)
                    output.append(
                        {
                            "date": date,
                            "window_et": window,
                            "scan_id": batch["scan_batch_id"],
                            "selected_job_id": job["id"],
                            "trigger_source": "scheduler",
                            "lock_key": key,
                            "started_at_utc": batch["started_at_utc"],
                            "completed_at_utc": batch["completed_at_utc"],
                            "chunks": chunks,
                            "snapshots": snapshots,
                        }
                    )
                    break
        return output
    finally:
        connection.rollback()
        connection.close()


def exclusive_json(directory: Path, name: str, value: dict) -> tuple[Path, bool]:
    sessions.private_directory(directory)
    raw = trial.canonical(value) + b"\n"
    temporary = directory / (".pending-" + uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        target = directory / name
        try:
            os.link(temporary, target, follow_symlinks=False)
            return target, True
        except FileExistsError:
            if sessions.private_bytes(target, 8_000_000) != raw:
                raise ValueError("existing_audit_content_conflict")
            return target, False
    finally:
        temporary.unlink()


def run(database: Path, config_path: Path, *, now=None) -> dict:
    root = Path(__file__).resolve().parents[2]
    cfg = activation(config_path, root)
    now = now or datetime.now(timezone.utc)
    if not cfg["enabled"]:
        return {"status": "disabled"}
    start_stamp, end_stamp = (
        v2._stamp(cfg["start_at_utc"]),
        v2._stamp(cfg["end_at_utc"]),
    )
    assert start_stamp is not None and end_stamp is not None
    if now < start_stamp[0] or now > end_stamp[0]:
        return {"status": "outside_bounded_cohort"}
    directory = Path(cfg["audit_directory"])
    protocol = trial.load_protocol(
        root / "docs/research/three-session-freshness-protocol-20261014.json"
    )
    captures = read_captures(database, cfg, protocol)
    pending = []
    results = []
    for c in captures:
        if not v2._token(c["scan_id"]):
            raise ValueError("invalid_scan_identity")
        prefix = f"capture-{POLICY_ID}-{cfg['cohort_id']}-{c['scan_id']}"
        prior_paths = list(directory.glob(prefix + "-*.json"))
        if len(prior_paths) > 1:
            raise ValueError("ambiguous_saved_capture")
        if prior_paths:
            raw = sessions.private_bytes(prior_paths[0], 8_000_000)
            prior = json.loads(raw)
            if (
                sha256(raw).hexdigest() not in prior_paths[0].name
                or prior.get("policy_id") != POLICY_ID
                or prior.get("cohort_id") != cfg["cohort_id"]
                or prior.get("scan_id") != c["scan_id"]
                or prior.get("source_sha256") != cfg["source_sha256"]
            ):
                raise ValueError("existing_capture_identity_mismatch")
            results.append(
                {
                    "scan_id": c["scan_id"],
                    "date": c["date"],
                    "window": c["window_et"],
                    "status": prior["status"],
                    "eligible": prior["research_eligible"],
                    "created": False,
                    "path": str(prior_paths[0]),
                }
            )
        else:
            pending.append((c, prefix))
    # At most three new captures per invocation; oldest first. All requested
    # windows stay visible in the run receipt, even when backlog is present.
    catalogs = {}
    for capture, prefix in pending[:MAX_CAPTURES]:
        day = capture["date"]
        if day not in catalogs:
            catalogs[day] = sessions.load_catalog(Path(cfg["observer_directory"]), day)
        result = evaluate(capture, protocol, catalogs[day], cfg["cohort_id"])
        result["source_sha256"] = cfg["source_sha256"]
        result["activation_sha256"] = sha256(trial.canonical(cfg)).hexdigest()
        result["hold10_reference_revision"] = cfg["hold10_reference_revision"]
        result["identity_limit"] = (
            "Selected disk hashes only; no HOLD10 expected-runtime agreement or enrollment."
        )
        payload_hash = sha256(trial.canonical(result) + b"\n").hexdigest()
        path, created = exclusive_json(
            directory, prefix + "-" + payload_hash + ".json", result
        )
        results.append(
            {
                "scan_id": capture["scan_id"],
                "date": day,
                "window": capture["window_et"],
                "status": result["status"],
                "eligible": result["research_eligible"],
                "created": created,
                "path": str(path),
            }
        )
    if cfg["source_sha256"] != source_identity(root):
        raise ValueError("source_changed_during_audit")
    served = {(c["date"], c["window_et"]) for c in captures}
    windows = []
    for day in cfg["dates"]:
        for window in protocol["windows_et"]:
            start, end = [
                datetime.strptime(day + "T" + x, "%Y-%m-%dT%H:%M").replace(
                    tzinfo=ZoneInfo("America/New_York")
                )
                for x in window.split("-")
            ]
            if (day, window) in served:
                status = "captured"
            elif now < start.astimezone(timezone.utc):
                status = "not_yet_due"
            else:
                deadline = end.astimezone(timezone.utc) + timedelta(minutes=30)
                if day not in catalogs:
                    catalogs[day] = sessions.load_catalog(
                        Path(cfg["observer_directory"]), day
                    )
                expectation, reason = sessions.bounds_at(
                    catalogs[day], day, min(now, deadline).isoformat()
                )
                boundary = (
                    v2._stamp(expectation["close_at_utc"]) if expectation else None
                )
                after_close = (
                    boundary is not None
                    and start.astimezone(timezone.utc) >= boundary[0]
                )
                if reason == "verified_closed_day" or after_close:
                    status = "expected_no_session"
                elif now < deadline:
                    status = "pending_capture"
                else:
                    status = (
                        "missing_capture"
                        if expectation
                        else "missing_capture_calendar_unverified"
                    )
            windows.append({"date": day, "window": window, "status": status})
    blocked = any(x["status"].startswith("blocked") for x in results)
    status = (
        "blocked"
        if blocked
        else "backlog"
        if len(pending) > MAX_CAPTURES
        else "missing_capture"
        if any(w["status"].startswith("missing_capture") for w in windows)
        else "recorded"
        if results
        else "waiting_or_already_recorded"
    )
    receipt = {
        "version": "swing_audit_run_v1",
        "policy_id": POLICY_ID,
        "cohort_id": cfg["cohort_id"],
        "observed_at_utc": now.isoformat(),
        "status": status,
        "activation_sha256": sha256(trial.canonical(cfg)).hexdigest(),
        "captures": results,
        "windows": windows,
        "broker_calls": 0,
        "ai_calls": 0,
        "hold10_enrolled": False,
        "notifications_enabled": False,
    }
    path, _ = exclusive_json(
        directory,
        "run-" + now.strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex + ".json",
        receipt,
    )
    return {
        "status": status,
        "run_receipt": str(path),
        "new_captures": sum(r["created"] for r in results),
        "cohort_id": cfg["cohort_id"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--activation", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = run(args.db, args.activation)
    except Exception as exc:
        result = {"status": "blocked", "error_type": type(exc).__name__}
    print(json.dumps(result, sort_keys=True))
    return 1 if result["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
