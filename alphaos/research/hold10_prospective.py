"""Inactive, manually invoked prospective-timing registry; never a live hook.

The two new tables belong in a NEW isolated scratch database. Existing saved
batch exports are retained verbatim; no outcome, price, broker or evaluator is
read. A timely local receipt is not proof of authentic policy execution.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from alphaos.research import hold10_collection as c
from alphaos.research import hold10_enrollment as e
from alphaos.research import hold10_receipts as h
from alphaos.research.hold10_analysis import FLOORS
from alphaos.stats.frozen_policy_inference import canonical_sha256 as sha
from alphaos.util import market_calendar as calendar

VERSION = "hold10_prospective_boundary_dry_run_v1"
FAMILY = "HOLD10:prereg_35bbce762e7f:ai_final_vs_threshold_v1_hold10"
ET = ZoneInfo("America/New_York")
FLAGS = {
    "mode": "inactive_manual_dry_run",
    "activated": False,
    "formal_allowed": False,
    "promotion_allowed": False,
    "prospective_execution_certified": False,
}
TABLES = (
    "research_hold10_prospective_registrations",
    "research_hold10_prospective_captures",
)
DDL = (
    f"CREATE TABLE {TABLES[0]} (registration_id TEXT PRIMARY KEY, family_id TEXT NOT NULL UNIQUE, {h._COMMON})",
    f"CREATE TABLE {TABLES[1]} (registration_id TEXT NOT NULL REFERENCES {TABLES[0]}(registration_id), sequence INTEGER NOT NULL CHECK(sequence>0), previous_receipt_sha256 TEXT, {h._COMMON}, PRIMARY KEY(registration_id,sequence))",
) + tuple(
    f"CREATE TRIGGER {table}_immutable_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'immutable prospective boundary'); END"
    for table in TABLES
    for action in ("UPDATE", "DELETE")
)
POLICY_FILES = {
    "alphaos/orchestrator.py",
    "alphaos/baseline/rules.py",
    "alphaos/research/watch_advisory.py",
    "alphaos/ai/playbook_classifier.py",
    "alphaos/ai/label_validation.py",
    "alphaos/ai/prompt_templates.py",
}
MAX_CAPTURES = 500
MAX_HISTORY_BYTES = 128 * 1024 * 1024
CANCELLATION_BYTES = 1024


def _now():
    return datetime.now(timezone.utc)


def _clock():
    now = _now()
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("aware_actual_clock_required")
    return now.astimezone(timezone.utc)


def _copy(value):
    return c._copy(value)


def _exact(obj, keys, code):
    if not isinstance(obj, dict) or set(obj) != set(keys):
        raise ValueError(code)


def _calendar_sha():
    return hashlib.sha256(Path(calendar.__file__).read_bytes()).hexdigest()


def _open_after(value):
    day = calendar.nth_trading_day_after(c._dt(value).astimezone(ET).date(), 1)
    return datetime.combine(day, time(9, 30), ET).astimezone(timezone.utc)


def _contract(value):
    q = _copy(value)
    _exact(
        q,
        {
            "version",
            "registration_id",
            "family_id",
            "protocol",
            "combined_policy",
            "start_at_utc",
            "end_at_utc",
            "close_at_utc",
            "schedule",
            "stopping_rule",
            "analysis_mode",
            "original_constraints",
            *FLAGS,
        },
        "exact_prospective_contract_required",
    )
    if q["version"] != VERSION or any(q[k] != v for k, v in FLAGS.items()):
        raise ValueError("inactive_manual_contract_required")
    if (
        not h._text(q["registration_id"], "registration_id").startswith(
            "hold10-prospective-draft:"
        )
        or q["family_id"] != FAMILY
    ):
        raise ValueError("fixed_family_and_new_namespace_required")
    if (
        q["stopping_rule"] != "one_fixed_deadline_no_extension"
        or q["analysis_mode"] != "disabled_pending_method_validation"
        or q["original_constraints"] != FLOORS
    ):
        raise ValueError("original_floors_fixed_stop_and_disabled_analysis_required")
    start, end, close = (
        c._dt(q[k]) for k in ("start_at_utc", "end_at_utc", "close_at_utc")
    )
    if (
        start.astimezone(ET).timetz().replace(tzinfo=None) != time()
        or end.astimezone(ET).timetz().replace(tzinfo=None) != time()
        or not 28
        <= (end.astimezone(ET).date() - start.astimezone(ET).date()).days
        <= 120
    ):
        raise ValueError("full_ET_dates_28_to_120_days_required")
    last_day = end.astimezone(ET).date() - timedelta(days=1)
    last_maturity = datetime.combine(
        calendar.nth_trading_day_after(last_day, 10), time(16), ET
    )
    if not last_maturity <= close <= end + timedelta(days=45):
        raise ValueError("fixed_followup_deadline_must_cover_ten_sessions")
    p = q["protocol"]
    _exact(
        p,
        {
            "version",
            "protocol_id",
            "status",
            "predecessor_id",
            "horizon_sessions",
            "direction",
            "effect_target",
            "statistic",
            "policies",
            "risk_contract",
            "source_revision",
            "measurement_method",
            "population",
            "enrollment_adapter",
        },
        "exact_protocol_fields_required",
    )
    h._protocol_contract(p)
    if p["population"] != "final_proposed":
        raise ValueError("fixed_final_proposed_population_required")
    _exact(
        p["policies"]["tested"],
        {"source", "decision_meaning", "identity"},
        "exact_tested_policy_required",
    )
    _exact(
        p["policies"]["reference"],
        {"rule_version", "setup_card_id"},
        "exact_reference_policy_required",
    )
    _exact(
        p["risk_contract"],
        {"R_basis", "risk_budget", "policy_version", "currency"},
        "exact_risk_contract_required",
    )
    request = {
        "cohort_id": "contract-check",
        "scan_batch_id": "contract-check",
        "as_of_utc": _clock().isoformat(),
        "source": {"kind": "archived_copy", "sha256": "0" * 64},
    }
    e._contract(p, request, sha(p), sha(request))
    policy = q["combined_policy"]
    _exact(
        policy,
        {
            "source_revision",
            "files",
            "configuration",
            "classifier_identity",
            "nonsecret_settings_sha256",
            "execution_identity",
        },
        "exact_combined_policy_required",
    )
    revision = policy["source_revision"]
    if (
        not isinstance(revision, str)
        or len(revision) != 40
        or any(x not in "0123456789abcdef" for x in revision)
        or revision != p["source_revision"]
    ):
        raise ValueError("exact_combined_source_revision_required")
    _exact(policy["files"], POLICY_FILES, "exact_combined_source_files_required")
    for digest in policy["files"].values():
        h._sha(digest, "policy_file")
    h._sha(policy["nonsecret_settings_sha256"], "nonsecret_settings")
    _exact(
        policy["classifier_identity"],
        {"model", "label_version", "label_source"},
        "exact_classifier_identity_required",
    )
    for value in policy["classifier_identity"].values():
        h._text(value, "classifier_identity")
    config = policy["configuration"]
    _exact(
        config,
        {
            "labeller_decision_override_enabled",
            "label_min_confidence_to_propose",
            "min_reward_risk",
        },
        "exact_combination_settings_required",
    )
    if (
        type(config["labeller_decision_override_enabled"]) is not bool
        or not h.finite(config["label_min_confidence_to_propose"])
        or not 0 <= config["label_min_confidence_to_propose"] <= 1
        or not h.finite(config["min_reward_risk"])
        or config["min_reward_risk"] <= 0
        or policy["execution_identity"]
        != "declared_source_and_settings_not_runtime_attestation"
    ):
        raise ValueError("declared_combined_policy_configuration_required")
    schedule = q["schedule"]
    _exact(
        schedule,
        {"calendar_sha256", "windows_et", "scope"},
        "exact_schedule_rule_required",
    )
    if (
        schedule["calendar_sha256"] != _calendar_sha()
        or schedule["scope"] != "full_closure_calendar_all_retained_core_batches"
    ):
        raise ValueError("frozen_full_closure_calendar_required")
    windows = schedule["windows_et"]
    if not isinstance(windows, list) or not 1 <= len(windows) <= 6:
        raise ValueError("bounded_daily_windows_required")
    previous = None
    for window in windows:
        if not isinstance(window, list) or len(window) != 2:
            raise ValueError("ordered_exact_daily_windows_required")
        a, b = window
        if any(
            not isinstance(x, str)
            or len(x) != 5
            or time.fromisoformat(x).isoformat(timespec="minutes") != x
            for x in window
        ):
            raise ValueError("HH_MM_daily_windows_required")
        if not "09:30" <= a < b <= "16:00" or previous is not None and a < previous:
            raise ValueError("nonoverlapping_regular_session_windows_required")
        previous = b
    return q


def schedule_slots(contract):
    """Derive all declared slots, never take a caller-selected slot subset."""
    q = _contract(contract)
    day, end = (
        c._dt(q[k]).astimezone(ET).date() for k in ("start_at_utc", "end_at_utc")
    )
    slots = []
    while day < end:
        if calendar.is_trading_day(day):
            for start, finish in q["schedule"]["windows_et"]:
                slots.append(
                    {
                        "lock_key": f"scan:{day.isoformat()}T{start}",
                        "start_at_utc": datetime.combine(
                            day, time.fromisoformat(start), ET
                        )
                        .astimezone(timezone.utc)
                        .isoformat(),
                        "end_at_utc": datetime.combine(
                            day, time.fromisoformat(finish), ET
                        )
                        .astimezone(timezone.utc)
                        .isoformat(),
                    }
                )
        day += timedelta(days=1)
    return slots


@lru_cache(maxsize=1)
def _expected_schema():
    db = sqlite3.connect(":memory:")
    try:
        for sql in DDL:
            db.execute(sql)
        return _schema(db)
    finally:
        db.close()


def _schema(db):
    return tuple(
        tuple(row)
        for row in db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        )
    )


def _verify(db):
    if _schema(db) != _expected_schema():
        raise ValueError("dedicated_registry_schema_or_immutability_mismatch")


def initialize_prospective_registry(connection, *, purpose):
    if purpose != "inactive_manual_prospective_dry_run":
        raise ValueError("explicit_inactive_scratch_initialization_required")
    with h._atomic(connection):
        if _schema(connection):
            _verify(connection)
            return
        for sql in DDL:
            connection.execute(sql)
        _verify(connection)


@contextmanager
def _read_snapshot(db):
    if db.in_transaction:
        raise ValueError("registry_requires_own_transaction")
    db.execute("BEGIN")
    try:
        yield
    finally:
        db.rollback()


def _records(db, table, rid):
    order = "sequence" if table == TABLES[1] else "registration_id"
    cursor = db.execute(
        f"SELECT * FROM {table} WHERE registration_id=? ORDER BY {order}", (rid,)
    )
    result = []
    total_bytes = 0
    for row in cursor:
        raw = dict(zip([x[0] for x in cursor.description], row))
        total_bytes += len(raw["payload_json"].encode())
        payload = _copy(json.loads(raw.pop("payload_json")))
        allowance = CANCELLATION_BYTES if payload.get("kind") == "cancel" else 0
        if total_bytes > MAX_HISTORY_BYTES + allowance:
            raise ValueError("capture_history_byte_cap_exceeded")
        h._pin(payload, raw["payload_sha256"], "saved_payload")
        digest = raw.pop("receipt_sha256")
        h._pin(raw, digest, "saved_receipt")
        if raw["mode"] != "dry_run" or c._dt(raw["recorded_at_utc"]) > _clock():
            raise ValueError("invalid_saved_receipt_mode_or_future_clock")
        result.append({**raw, "receipt_sha256": digest, "payload": payload})
        if len(result) > MAX_CAPTURES + int(payload.get("kind") == "cancel"):
            raise ValueError("bounded_capture_history_exceeded")
    return result


@contextmanager
def _source_only(db):
    tables = {*e.FIELDS, "job_runs", "candidate_labels"}
    for table in tables:
        row = db.execute(
            "SELECT type,sql FROM sqlite_master WHERE name=?", (table,)
        ).fetchone()
        if row is None or row[0] != "table" or "CREATE VIRTUAL TABLE" in row[1].upper():
            raise ValueError("plain_saved_input_table_required")

    def authorize(action, arg1, arg2, database, origin):
        if action == sqlite3.SQLITE_READ:
            return sqlite3.SQLITE_OK if arg1 in tables else sqlite3.SQLITE_DENY
        return (
            sqlite3.SQLITE_OK
            if action
            in {
                sqlite3.SQLITE_SELECT,
                sqlite3.SQLITE_FUNCTION,
                sqlite3.SQLITE_TRANSACTION,
            }
            else sqlite3.SQLITE_DENY
        )

    db.set_authorizer(authorize)
    try:
        yield
    finally:
        db.set_authorizer(None)


def _label_rows(db):
    fields = "label_id,candidate_id,packet_id,scan_batch_id,symbol,model,label_version,label_source,validation_status,is_mock,created_at_utc"
    cursor = db.execute(
        f"SELECT {fields} FROM candidate_labels ORDER BY rowid LIMIT ?",
        (c.MAX_INPUT_ROWS + 1,),
    )
    rows = [dict(zip([x[0] for x in cursor.description], row)) for row in cursor]
    if len(rows) > c.MAX_INPUT_ROWS:
        raise ValueError("source_label_bound_exceeded")
    return rows


def _observe(snapshot, registration, at):
    if not isinstance(snapshot, c.SavedLedger):
        raise ValueError("immutable_retained_copy_required")
    snapshot.verify()
    q = registration["payload"]["contract"]
    start, end = (c._dt(q[k]) for k in ("start_at_utc", "end_at_utc"))
    with _source_only(snapshot.conn), e._snapshot(snapshot.conn):
        batches = c._rows(snapshot.conn, "scan_batches")
        candidates = c._rows(snapshot.conn, "candidates", e.FIELDS["candidates"])
        if len({b["scan_batch_id"] for b in batches}) != len(batches) or len(
            {x["candidate_id"] for x in candidates}
        ) != len(candidates):
            raise ValueError("duplicate_retained_source_identity")
        selected = [b for b in batches if start <= c._dt(b["started_at_utc"]) < end]
        if len(selected) > 1000:
            raise ValueError("batch_census_bound_exceeded")
        ids = {b["scan_batch_id"] for b in selected}
        all_ids = {b["scan_batch_id"] for b in batches}
        linked = [x for x in candidates if x["scan_batch_id"] in ids]
        orphan = [
            x
            for x in candidates
            if start <= c._dt(x["created_at_utc"]) < end
            and x["scan_batch_id"] not in all_ids
        ]
        inventory = {
            "batches": selected,
            "linked_candidates": linked,
            "orphan_candidates": orphan,
        }
        # Freeze the full census before any policy output query.
        inventory = _copy(inventory)
        labels = _label_rows(snapshot.conn)
        dispositions = []
        for batch in selected:
            bid = batch["scan_batch_id"]
            rows = [x for x in linked if x["scan_batch_id"] == bid]
            d = {
                "scan_batch_id": bid,
                "batch_input_sha256": sha({"batch": batch, "candidates": rows}),
                "core_candidate_ids": [
                    x["candidate_id"] for x in rows if x["shadow_tier"] in (None, 0)
                ],
                "deadline_utc": _open_after(batch["started_at_utc"]).isoformat(),
                "status": "pending",
                "export": None,
            }
            if any(x["shadow_tier"] not in (None, 0, 1) for x in rows):
                d["status"] = "invalid_population"
            elif (
                c._dt(batch["started_at_utc"]) > at
                or c._dt(batch["created_at_utc"]) > at
                or any(c._dt(x["created_at_utc"]) > at for x in rows)
            ):
                d["status"] = "future_source_rows"
            elif any(not start <= c._dt(x["created_at_utc"]) < end for x in rows):
                d["status"] = "candidate_outside_registered_interval"
            elif batch["status"] not in {"started", "running", "completed"}:
                d["status"] = "failed_batch"
            elif batch["status"] == "completed":
                request = {
                    "cohort_id": q["registration_id"] + ":" + bid,
                    "scan_batch_id": bid,
                    "as_of_utc": at.isoformat(),
                    "source": {
                        "kind": "archived_copy",
                        "sha256": snapshot.expected_sha256,
                    },
                }
                try:
                    export = e.build_saved_hold10_manifest(
                        snapshot.conn,
                        protocol=q["protocol"],
                        expected_protocol_sha256=sha(q["protocol"]),
                        request=request,
                        expected_request_sha256=sha(request),
                    )
                    d["export"] = export
                    export["combined_classifier_rows"] = [
                        row
                        for row in labels
                        if row["candidate_id"] in d["core_candidate_ids"]
                    ]
                    d["status"] = (
                        "timely_input_freeze"
                        if _complete_inputs(export, q, bid)
                        else "timely_incomplete_input_freeze"
                    )
                except (ValueError, TypeError, KeyError):
                    d["status"] = "invalid_completed_inputs"
                    d["export"] = None
            # Read completed inputs even on later observations so a genuine
            # mutation stays detectable. Time passage alone changes no inputs.
            if at >= c._dt(d["deadline_utc"]) and d["status"] in {
                "pending",
                "timely_input_freeze",
                "timely_incomplete_input_freeze",
            }:
                d["status"] = "late_first_observation"
            dispositions.append(d)
        jobs = c._rows(snapshot.conn, "job_runs")
        slots = [
            c._slot_evidence(slot, jobs, selected, at)
            for slot in registration["payload"]["schedule_slots"]
        ]
    snapshot.verify()
    return {
        "kind": "capture",
        "source_sha256": snapshot.expected_sha256,
        "observed_at_utc": at.isoformat(),
        "inventory": inventory,
        "inventory_sha256": sha(inventory),
        "dispositions": dispositions,
        "schedule_slots": slots,
    }


def _complete_inputs(export, q, bid):
    if any(
        x["issues"] or (x["membership_state"] == "selected" and x["missing_arms"])
        for x in export["diagnostics"]
    ):
        return False
    config = q["combined_policy"]["configuration"]
    for op in export["manifest"]["opportunities"]:
        adj = op["saved_decision_sources"]["decision_adjustments"]
        if len(adj) != 1:
            return False
        diagnostic = json.loads(adj[0]["evidence_json"]).get(
            "watch_advisory_diagnostic", {}
        )
        if not isinstance(diagnostic, dict):
            return False
        labels = [
            row
            for row in export["combined_classifier_rows"]
            if row["candidate_id"] == op["opportunity_id"]
        ]
        if (
            len(labels) != 1
            or labels[0]["label_id"] != diagnostic.get("label_id")
            or labels[0]["packet_id"] != adj[0]["packet_id"]
            or labels[0]["scan_batch_id"] != bid
            or labels[0]["symbol"] != adj[0]["symbol"]
            or labels[0]["validation_status"] != "passed"
            or labels[0]["is_mock"] != 0
            or not c._dt(op["input"]["candidate"]["created_at_utc"])
            <= c._dt(labels[0]["created_at_utc"])
            <= c._dt(adj[0]["created_at_utc"])
            or any(
                labels[0][key] != value
                for key, value in q["combined_policy"]["classifier_identity"].items()
            )
            or diagnostic.get("confidence_floor")
            != config["label_min_confidence_to_propose"]
            or diagnostic.get("min_reward_risk") != config["min_reward_risk"]
            or diagnostic.get("current_decision") != adj[0]["final_decision"]
            or diagnostic.get("is_mock") is not False
        ):
            return False
    return True


def _check_export(export, q, batch, rows, observation):
    manifest = export["manifest"]
    h._pin(manifest, export["manifest_sha256"], "manifest")
    core = [row for row in rows if row["shadow_tier"] in (None, 0)]
    if (
        manifest["all_opportunity_ids"] != [row["candidate_id"] for row in core]
        or len(manifest["opportunities"]) != len(core)
        or len(export["diagnostics"]) != len(core)
        or manifest["protocol_sha256"] != sha(q["protocol"])
        or manifest["cohort_id"] != q["registration_id"] + ":" + batch["scan_batch_id"]
        or manifest["source"]["sha256"] != observation["source_sha256"]
        or c._dt(manifest["source"]["request"]["as_of_utc"])
        != c._dt(observation["observed_at_utc"])
    ):
        raise ValueError("full_core_manifest_or_source_pin_required")
    saved_batch = {key: batch[key] for key in e.FIELDS["scan_batches"].split(",")}
    if manifest["source"]["batch"] != saved_batch:
        raise ValueError("manifest_saved_batch_mismatch")
    for row, opportunity, diagnostic in zip(
        core, manifest["opportunities"], export["diagnostics"]
    ):
        actual_op, actual_diagnostic = e._opportunity(
            row, opportunity["saved_decision_sources"], saved_batch, q["protocol"]
        )
        if opportunity != actual_op or diagnostic != actual_diagnostic:
            raise ValueError("saved_export_derivation_mismatch")
    return _complete_inputs(export, q, batch["scan_batch_id"])


def _evidence_key(d):
    export = d["export"]
    return sha(
        {
            "input": d["batch_input_sha256"],
            "opportunities": None
            if export is None
            else export["manifest"]["opportunities"],
            "classifier_rows": None
            if export is None
            else export["combined_classifier_rows"],
        }
    )


def _view(registration, captures, now):
    q = _contract(registration["payload"]["contract"])
    if (
        c._dt(registration["recorded_at_utc"]) >= c._dt(q["start_at_utc"])
        or registration["family_id"] != q["family_id"]
        or registration["registration_id"] != q["registration_id"]
        or registration["payload"]["schedule_slots"] != schedule_slots(q)
    ):
        raise ValueError("registration_identity_schedule_or_future_boundary_invalid")
    frozen: dict = {}
    issues: set[str] = set()
    seen_ids: dict = {}
    first_candidates: dict = {}
    previous_core_by_batch: dict = {}
    prior, last_time, cancelled = None, c._dt(registration["recorded_at_utc"]), False
    last_slots = [
        {**slot, "job_run_ids": [], "completed_batch_ids": []}
        for slot in registration["payload"]["schedule_slots"]
    ]
    for i, receipt in enumerate(captures, 1):
        at = c._dt(receipt["recorded_at_utc"])
        if (
            receipt["sequence"] != i
            or receipt["previous_receipt_sha256"] != prior
            or not last_time <= at <= now
            or cancelled
        ):
            raise ValueError("capture_chain_clock_or_cancel_mismatch")
        p = receipt["payload"]
        if p["kind"] == "cancel":
            _exact(p, {"kind", "reason"}, "invalid_cancel_receipt")
            if p["reason"] != "manual_withdrawal_no_deletion":
                raise ValueError("invalid_cancel_reason")
            cancelled = True
        else:
            _exact(
                p,
                {
                    "kind",
                    "source_sha256",
                    "observed_at_utc",
                    "inventory",
                    "inventory_sha256",
                    "dispositions",
                    "schedule_slots",
                },
                "invalid_capture_fields",
            )
            if p["kind"] != "capture" or not c._dt(q["start_at_utc"]) <= at <= c._dt(
                q["close_at_utc"]
            ):
                raise ValueError("capture_outside_registered_observation_period")
            h._sha(p["source_sha256"], "saved_source")
            if c._dt(p["observed_at_utc"]) > at:
                raise ValueError("observation_after_recording")
            h._pin(p["inventory"], p["inventory_sha256"], "inventory")
            batch_ids = [b["scan_batch_id"] for b in p["inventory"]["batches"]]
            if (
                len(set(batch_ids)) != len(batch_ids)
                or [d["scan_batch_id"] for d in p["dispositions"]] != batch_ids
            ):
                raise ValueError("complete_batch_disposition_required")
            if p["inventory"]["orphan_candidates"]:
                issues.add("orphan_source_candidates")
            changed_batches: set[str] = set()
            for row in [
                *p["inventory"]["linked_candidates"],
                *p["inventory"]["orphan_candidates"],
            ]:
                if row["shadow_tier"] not in (None, 0):
                    continue
                cid, bid = row["candidate_id"], row["scan_batch_id"]
                if cid in seen_ids and seen_ids[cid] != bid:
                    issues.add("candidate_moved_between_batches:" + cid)
                    changed_batches.update((bid, seen_ids[cid]))
                if cid in first_candidates and first_candidates[cid] != row:
                    issues.add("previously_observed_candidate_changed:" + cid)
                    changed_batches.add(bid)
                seen_ids.setdefault(cid, bid)
                first_candidates.setdefault(cid, row)
            for bid in set(frozen) - set(batch_ids):
                issues.add("previously_observed_batch_disappeared:" + bid)
            for d, batch in zip(p["dispositions"], p["inventory"]["batches"]):
                bid = d["scan_batch_id"]
                rows = [
                    x
                    for x in p["inventory"]["linked_candidates"]
                    if x["scan_batch_id"] == bid
                ]
                if (
                    d["deadline_utc"]
                    != _open_after(batch["started_at_utc"]).isoformat()
                    or d["batch_input_sha256"]
                    != sha({"batch": batch, "candidates": rows})
                    or d["core_candidate_ids"]
                    != [
                        x["candidate_id"] for x in rows if x["shadow_tier"] in (None, 0)
                    ]
                ):
                    raise ValueError("batch_census_or_deadline_mismatch")
                current_ids = set(d["core_candidate_ids"])
                if previous_core_by_batch.get(bid, set()) - current_ids:
                    issues.add("previously_observed_candidates_disappeared:" + bid)
                    changed_batches.add(bid)
                previous_core_by_batch.setdefault(bid, set()).update(current_ids)
                export = d["export"]
                if export is not None:
                    complete = _check_export(export, q, batch, rows, p)
                    if d["status"] == "timely_input_freeze" and not complete:
                        raise ValueError("incomplete_saved_inputs_cannot_be_complete")
                if d["status"].startswith("timely_") and (
                    export is None or at >= c._dt(d["deadline_utc"])
                ):
                    raise ValueError("timely_receipt_must_precede_outcome_open")
                if bid not in frozen or frozen[bid]["status"] == "pending":
                    frozen[bid] = {
                        **d,
                        **(
                            {"status": "changed_pending_inputs"}
                            if bid in changed_batches
                            else {}
                        ),
                        "first_recorded_at_utc": receipt["recorded_at_utc"],
                        "capture_receipt_sha256": receipt["receipt_sha256"],
                    }
                elif _evidence_key(frozen[bid]) != _evidence_key(d):
                    issues.add("frozen_batch_inputs_changed:" + bid)
                elif d["status"] == "late_first_observation" and frozen[bid][
                    "status"
                ].startswith("timely_"):
                    # Later observation cannot retroactively make a timely
                    # original late; its original input is preserved above.
                    pass
            if len(p["schedule_slots"]) != len(last_slots):
                raise ValueError("full_schedule_receipt_required")
            for declared, observed in zip(
                registration["payload"]["schedule_slots"], p["schedule_slots"]
            ):
                if any(observed[k] != v for k, v in declared.items()):
                    raise ValueError("schedule_receipt_identity_mismatch")
            last_slots = p["schedule_slots"]
        prior, last_time = receipt["receipt_sha256"], at
    for d in frozen.values():
        if d["status"] != "timely_input_freeze":
            issues.add("batch_" + d["status"] + ":" + d["scan_batch_id"])
    missing = [
        slot["lock_key"]
        for slot in last_slots
        if c._dt(slot["end_at_utc"]) <= now and not slot["completed_batch_ids"]
    ]
    if missing:
        issues.add("unserved_or_unobserved_declared_slots")
    # Every contract in this inactive stage retains these scientific/provenance
    # blockers even when the local pre-outcome clock and complete inputs pass.
    issues.update(
        {
            "inactive_dry_run_no_runtime_activation",
            "executed_combined_policy_not_attested",
            "uncertainty_method_not_validated",
        }
    )
    return {
        "registration": registration,
        "captures": captures,
        "latest_capture_sha256": prior,
        "state": "cancelled"
        if cancelled
        else "expired"
        if now >= c._dt(q["close_at_utc"])
        else "collecting"
        if now >= c._dt(q["start_at_utc"])
        else "registered_future",
        "frozen_batches": list(frozen.values()),
        "schedule_slots": last_slots,
        "missing_elapsed_slots": missing,
        "full_candidate_count": len(seen_ids),
        "ever_observed_core_candidate_ids": sorted(seen_ids),
        "frozen_core_candidate_count": sum(
            len(d["core_candidate_ids"]) for d in frozen.values()
        ),
        "timely_complete_batch_count": sum(
            d["status"] == "timely_input_freeze" for d in frozen.values()
        ),
        "coverage_blockers": sorted(issues),
        "formal_inference": None,
        **FLAGS,
    }


class ProspectiveHold10:
    def __init__(self, connection, *, dry_run=False):
        self.c, self.enabled = connection, dry_run is True

    def _read(self, rid):
        _verify(self.c)
        rows = _records(self.c, TABLES[0], rid)
        if len(rows) != 1:
            raise ValueError("registered_future_contract_required")
        return _view(rows[0], _records(self.c, TABLES[1], rid), _clock())

    def read(self, registration_id):
        with _read_snapshot(self.c):
            return self._read(registration_id)

    def register(self, contract, *, expected_contract_sha256):
        if not self.enabled:
            return {"status": "disabled", **FLAGS}
        q = _contract(contract)
        h._pin(q, expected_contract_sha256, "contract")
        with h._atomic(self.c):
            _verify(self.c)
            rows = _records(self.c, TABLES[0], q["registration_id"])
            if rows:
                if rows[0]["payload"]["contract"] != q:
                    raise ValueError("immutable_registration_conflict")
                return self._read(q["registration_id"])
            if self.c.execute(
                f"SELECT 1 FROM {TABLES[0]} WHERE family_id=?", (FAMILY,)
            ).fetchone():
                raise ValueError("one_local_family_registration_no_alias_or_restart")
            at = _clock()
            if at >= c._dt(q["start_at_utc"]):
                raise ValueError("registration_must_precede_future_interval")
            h._insert(
                self.c,
                TABLES[0],
                {"registration_id": q["registration_id"], "family_id": FAMILY},
                {"contract": q, "schedule_slots": schedule_slots(q)},
                at.isoformat(),
            )
            return self._read(q["registration_id"])

    def capture(
        self,
        snapshot,
        registration_id,
        *,
        expected_registration_sha256,
        expected_previous_capture_sha256,
    ):
        if not self.enabled:
            return {"status": "disabled", **FLAGS}
        before = self.read(registration_id)
        reg = before["registration"]
        if (
            reg["receipt_sha256"] != expected_registration_sha256
            or before["latest_capture_sha256"] != expected_previous_capture_sha256
        ):
            raise ValueError("registration_or_latest_capture_pin_mismatch")
        if before["state"] != "collecting":
            raise ValueError("capture_requires_open_registered_interval")
        observation = _copy(_observe(snapshot, reg, _clock()))
        with h._atomic(self.c):
            state = self._read(registration_id)
            if state["registration"] != reg or state["captures"] != before["captures"]:
                raise ValueError("concurrent_registry_change")
            now = _clock()
            if state["state"] != "collecting":
                raise ValueError("capture_deadline_or_cancellation_changed")
            snapshot.verify()
            # Clock checked at commit, not just before potentially slow reads.
            for d in observation["dispositions"]:
                if d["status"].startswith("timely_") and now >= c._dt(
                    d["deadline_utc"]
                ):
                    d["status"] = "late_at_commit"
            self._append(state, observation, now)
            return self._read(registration_id)

    def _append(self, state, payload, now):
        cancelling = payload.get("kind") == "cancel"
        if len(state["captures"]) >= MAX_CAPTURES + int(cancelling):
            raise ValueError("bounded_capture_history_exceeded")
        size = len(h._json(payload).encode()) + sum(
            len(h._json(row["payload"]).encode()) for row in state["captures"]
        )
        if size > MAX_HISTORY_BYTES + (CANCELLATION_BYTES if cancelling else 0):
            raise ValueError("capture_history_byte_cap_exceeded")
        return h._insert(
            self.c,
            TABLES[1],
            {
                "registration_id": state["registration"]["registration_id"],
                "sequence": len(state["captures"]) + 1,
                "previous_receipt_sha256": state["latest_capture_sha256"],
            },
            payload,
            now.isoformat(),
        )

    def cancel(
        self,
        registration_id,
        *,
        expected_registration_sha256,
        expected_previous_capture_sha256,
    ):
        if not self.enabled:
            return {"status": "disabled", **FLAGS}
        with h._atomic(self.c):
            state = self._read(registration_id)
            if (
                state["registration"]["receipt_sha256"] != expected_registration_sha256
                or state["latest_capture_sha256"] != expected_previous_capture_sha256
            ):
                raise ValueError("registration_or_latest_capture_pin_mismatch")
            if state["state"] == "cancelled":
                return state
            self._append(
                state,
                {"kind": "cancel", "reason": "manual_withdrawal_no_deletion"},
                _clock(),
            )
            return self._read(registration_id)
