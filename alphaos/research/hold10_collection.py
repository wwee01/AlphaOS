"""Inactive complete retained-batch HOLD10 enrollment; no runtime imports/hooks.

Only explicit scratch initialization and dry_run=True write. Source inspection
uses an externally pinned, immutable retained file, never JournalStore/startup.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import stat

from alphaos.research import hold10_enrollment as e
from alphaos.research import hold10_receipts as h
from alphaos.research import hold10_study as s
from alphaos.stats.frozen_policy_inference import canonical_sha256 as sha

METHOD = "hold10_complete_collection_draft_v1"
TABLE = "research_hold10_collections"
TABLES = (*h.TABLES, TABLE, *s.TABLES)
DDL = (
    f"CREATE TABLE {TABLE} (collection_id TEXT PRIMARY KEY, enrollment_key TEXT NOT NULL UNIQUE, {h._COMMON})",
    *(
        f"CREATE TRIGGER {TABLE}_immutable_{action.lower()} BEFORE {action} ON {TABLE} BEGIN SELECT RAISE(ABORT,'immutable HOLD10 collection'); END"
        for action in ("UPDATE", "DELETE")
    ),
)
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
MAX_INPUT_ROWS = 200000


def _now():
    return datetime.now(timezone.utc)


def _dt(value):
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("aware_source_time_required")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("aware_source_time_required")
    return result.astimezone(timezone.utc)


def _copy(value):
    raw = h._json(value)
    if len(raw.encode()) > MAX_JSON_BYTES:
        raise ValueError("collection_json_bound_exceeded")
    return json.loads(raw)


def _file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _identity(path):
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise ValueError("regular_single_link_snapshot_required")
    if not 0 < value.st_size <= MAX_SOURCE_BYTES:
        raise ValueError("snapshot_size_invalid")
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


class SavedLedger:
    """Identity-bound regular source file. root is explicit for offline callers."""

    def __init__(self, path, expected_sha256, *, root):
        self.path = Path(path).absolute()
        self.root = Path(root).absolute()
        if (
            not (self.root / "CURRENT-STATUS.md").is_file()
            or "sources" in self.path.parts
        ):
            raise ValueError("recognized_repair_workspace_required")
        if any(
            (parent / ".env").exists() and (parent / "alphaos").is_dir()
            for parent in (self.path.parent, *self.path.parents)
        ):
            raise ValueError("configured_application_source_refused")
        if self.path != self.path.resolve(
            strict=True
        ) or self.root != self.root.resolve(strict=True):
            raise ValueError("symlink_snapshot_or_root_refused")
        if (
            not self.path.is_relative_to(self.root)
            or "sources" in self.path.relative_to(self.root).parts
        ):
            raise ValueError("snapshot_outside_scratch_root")
        self.expected_sha256 = h._sha(expected_sha256, "snapshot_sha256")
        self.identity = _identity(self.path)
        self.verify()
        self.conn = sqlite3.connect(
            self.path.as_uri() + "?mode=ro&immutable=1", uri=True
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA query_only=ON")
        try:
            self.verify()
        except BaseException:
            self.conn.close()
            raise

    def verify(self):
        if (
            self.path != self.path.resolve(strict=True)
            or _identity(self.path) != self.identity
        ):
            raise ValueError("snapshot_identity_changed")
        if any(
            Path(str(self.path) + suffix).exists()
            for suffix in ("-wal", "-journal", "-shm")
        ):
            raise ValueError("snapshot_sidecar_refused")
        if (
            _file_hash(self.path) != self.expected_sha256
            or _identity(self.path) != self.identity
        ):
            raise ValueError("snapshot_hash_changed")

    def close(self):
        self.conn.close()


def _rows(c, table, fields="*"):
    if table not in {*e.FIELDS, "job_runs", "daily_bars", *TABLES}:
        raise ValueError("unapproved_collection_table")
    cursor = c.execute(
        f"SELECT {fields} FROM {table} ORDER BY rowid LIMIT ?", (MAX_INPUT_ROWS + 1,)
    )
    names = [d[0] for d in cursor.description]
    result = [dict(zip(names, row)) for row in cursor]
    if len(result) > MAX_INPUT_ROWS:
        raise ValueError("collection_row_bound_exceeded")
    return result


def _contract(contract):
    q = _copy(contract)
    if q.get("method") != METHOD or any(q.get(k) is not False for k in s.FLAGS):
        raise ValueError("inactive_complete_collection_required")
    if set(q) != {
        "method",
        *s.FLAGS,
        "collection_id",
        "source_sha256",
        "as_of_utc",
        "start_at_utc",
        "end_at_utc",
        "protocol",
        "study_id",
        "analysis_specification",
        "schedule",
    }:
        raise ValueError("exact_collection_contract_required")
    if not h._text(q["collection_id"], "collection_id").startswith(
        "hold10-collection-draft:"
    ):
        raise ValueError("collection_namespace_required")
    h._sha(q["source_sha256"], "source_sha256")
    start, end, cutoff = (
        _dt(q[k]) for k in ("start_at_utc", "end_at_utc", "as_of_utc")
    )
    if not start < end <= cutoff <= _now():
        raise ValueError("closed_nonfuture_collection_interval_required")
    p = q["protocol"]
    if set(p) != {
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
    }:
        raise ValueError("exact_collection_protocol_fields_required")
    if (
        set(p["policies"]["tested"]) != {"source", "decision_meaning", "identity"}
        or set(p["policies"]["reference"]) != {"rule_version", "setup_card_id"}
        or set(p["risk_contract"])
        != {"R_basis", "risk_budget", "policy_version", "currency"}
    ):
        raise ValueError("exact_collection_policy_and_risk_fields_required")
    e._contract(
        p,
        {
            "cohort_id": "validation",
            "scan_batch_id": "validation",
            "as_of_utc": q["as_of_utc"],
            "source": {"kind": "archived_copy", "sha256": q["source_sha256"]},
        },
        sha(p),
        sha(
            {
                "cohort_id": "validation",
                "scan_batch_id": "validation",
                "as_of_utc": q["as_of_utc"],
                "source": {"kind": "archived_copy", "sha256": q["source_sha256"]},
            }
        ),
    )
    # Validate the unchanged study contract without permitting a supplied roster.
    s._contract(
        _study_contract(
            q,
            [{"cohort_id": "validation", "manifest_sha256": "0" * 64}],
            cutoff.isoformat(),
            None,
        )
    )
    if q["analysis_specification"]["population"] != p["population"]:
        raise ValueError("collection_population_mismatch")
    schedule = q["schedule"]
    if schedule is not None:
        if not isinstance(schedule, dict) or set(schedule) != {
            "source_sha256",
            "scope",
            "slots",
        }:
            raise ValueError("explicit_declared_schedule_required")
        h._sha(schedule["source_sha256"], "schedule_source")
        h._text(schedule["scope"], "schedule_scope")
        keys = set()
        if not isinstance(schedule["slots"], list) or len(schedule["slots"]) > 1000:
            raise ValueError("bounded_declared_schedule_slots_required")
        for slot in schedule["slots"]:
            if set(slot) != {"lock_key", "start_at_utc", "end_at_utc"}:
                raise ValueError("exact_declared_slot_required")
            key = h._text(slot["lock_key"], "slot_key")
            if (
                key in keys
                or not start
                <= _dt(slot["start_at_utc"])
                < _dt(slot["end_at_utc"])
                <= end
            ):
                raise ValueError("duplicate_or_outside_declared_slot")
            keys.add(key)
    return q


def enrollment_key(q):
    # Local anti-alias identity: IDs, snapshot/outcome growth, and analysis-time
    # aliases cannot turn the same declared population into another look.
    protocol = {
        k: v
        for k, v in q["protocol"].items()
        if k not in {"protocol_id", "source_revision"}
    }
    schedule = (
        None
        if q["schedule"] is None
        else sorted(
            (_dt(slot["start_at_utc"]).isoformat(), _dt(slot["end_at_utc"]).isoformat())
            for slot in q["schedule"]["slots"]
        )
    )
    return sha(
        {
            "method": METHOD,
            "start_at_utc": _dt(q["start_at_utc"]).isoformat(),
            "end_at_utc": _dt(q["end_at_utc"]).isoformat(),
            "protocol": protocol,
            "schedule": schedule,
        }
    )


@contextmanager
def _saved_source_only(c, *, measurement=False):
    tables = {"daily_bars"} if measurement else {*e.FIELDS, "job_runs"}
    for name in tables:
        row = c.execute(
            "SELECT type,sql FROM sqlite_master WHERE name=?", (name,)
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
                sqlite3.SQLITE_SAVEPOINT,
            }
            else sqlite3.SQLITE_DENY
        )

    c.set_authorizer(authorize)
    try:
        yield
    finally:
        c.set_authorizer(None)


def _study_contract(q, roster, enrollment_end, collection_ref):
    p = q["protocol"]
    return {
        "method": s.METHOD,
        "status": "draft",
        "study_id": q["study_id"],
        "protocol_id": p["protocol_id"],
        "protocol_sha256": sha(p),
        "source_revision": p["source_revision"],
        "source_kind": "archived_copy",
        "enrollment_at_or_before_utc": enrollment_end,
        "analysis_specification": q["analysis_specification"],
        "cohorts": roster,
        "complete_collection_reference": collection_ref,
        **s.FLAGS,
    }


def _slot_evidence(slot, jobs, batches, cutoff):
    matching = [
        j
        for j in jobs
        if j["job_type"] == "scan"
        and j["lock_key"] == slot["lock_key"]
        and _dt(slot["start_at_utc"])
        <= _dt(j["started_at_utc"])
        < _dt(slot["end_at_utc"])
    ]
    completed = []
    for job in matching:
        if (
            job["status"] != "completed"
            or not job.get("finished_at_utc")
            or _dt(job["finished_at_utc"]) > cutoff
        ):
            continue
        try:
            summary = json.loads(job.get("result_summary_json") or "{}")
            bid = summary["scan_summary"]["scan_batch_id"]
        except (KeyError, TypeError, ValueError):
            continue
        for batch in batches:
            if (
                batch["scan_batch_id"] == bid
                and batch["status"] == "completed"
                and batch["completed_at_utc"]
                and job.get("scheduler_run_id")
                and batch.get("scheduler_run_id") == job["scheduler_run_id"]
                and _dt(job["started_at_utc"])
                <= _dt(batch["started_at_utc"])
                <= _dt(batch["completed_at_utc"])
                <= _dt(job["finished_at_utc"])
            ):
                completed.append(bid)
    return {
        **slot,
        "job_run_ids": [j["job_run_id"] for j in matching],
        "completed_batch_ids": sorted(set(completed)),
    }


def build_collection(snapshot, contract, *, expected_contract_sha256):
    """Input-only census then arm export, one source snapshot, no bars/outcomes."""
    if not isinstance(snapshot, SavedLedger):
        raise ValueError("bound_retained_snapshot_required")
    q = _contract(contract)
    h._pin(q, expected_contract_sha256, "collection_contract")
    snapshot.verify()
    if snapshot.expected_sha256 != q["source_sha256"]:
        raise ValueError("collection_source_pin_mismatch")
    c = snapshot.conn
    start, end, cutoff = (
        _dt(q[k]) for k in ("start_at_utc", "end_at_utc", "as_of_utc")
    )
    with _saved_source_only(c), e._snapshot(c):
        # Census is fixed before any arm/availability query. Batch start is the
        # inclusion clock; linked candidates are retained across interval edges.
        batches = _rows(c, "scan_batches")
        candidates = _rows(c, "candidates", e.FIELDS["candidates"])
        if len({b["scan_batch_id"] for b in batches}) != len(batches):
            raise ValueError("duplicate_source_batch_identity")
        if len({x["candidate_id"] for x in candidates}) != len(candidates):
            raise ValueError("duplicate_source_candidate_identity")
        selected = [b for b in batches if start <= _dt(b["started_at_utc"]) < end]
        if len(selected) > 1000:
            raise ValueError("collection_batch_bound_exceeded")
        selected_ids = {b["scan_batch_id"] for b in selected}
        all_batch_ids = {b["scan_batch_id"] for b in batches}
        linked = [x for x in candidates if x["scan_batch_id"] in selected_ids]
        anomalous = [
            x
            for x in candidates
            if start <= _dt(x["created_at_utc"]) < end
            and x["scan_batch_id"] not in selected_ids
        ]
        orphans = [x for x in anomalous if x["scan_batch_id"] not in all_batch_ids]
        inventory = {
            "batches": selected,
            "linked_candidates": linked,
            "interval_created_other_candidates": anomalous,
            "all_retained_batch_ids_sha256": sha(sorted(all_batch_ids)),
            "selection": "batch_started_at_in_closed_declared_half_open_interval",
            "independent_market_opportunity_census": False,
        }
        frozen_inventory_pin = sha(inventory)
        by_batch = defaultdict(list)
        for candidate in linked:
            by_batch[candidate["scan_batch_id"]].append(candidate)
        exports, dispositions = [], []
        for batch in selected:
            bid = batch["scan_batch_id"]
            core = [x for x in by_batch[bid] if x["shadow_tier"] in (None, 0)]
            disposition = {
                "scan_batch_id": bid,
                "source_status": batch["status"],
                "core_candidate_ids": [x["candidate_id"] for x in core],
                "shadow_candidate_ids": [
                    x["candidate_id"] for x in by_batch[bid] if x["shadow_tier"] == 1
                ],
                "status": "unavailable",
                "reason": None,
            }
            if any(_dt(x["created_at_utc"]) > cutoff for x in by_batch[bid]):
                disposition["reason"] = "candidate_after_source_cutoff"
            elif (
                batch["status"] != "completed"
                or not batch["completed_at_utc"]
                or _dt(batch["completed_at_utc"]) > cutoff
            ):
                disposition["reason"] = "batch_not_completed_by_source_cutoff"
            else:
                request = {
                    "cohort_id": "collection-cohort:" + enrollment_key(q) + ":" + bid,
                    "scan_batch_id": bid,
                    "as_of_utc": q["as_of_utc"],
                    "source": {"kind": "archived_copy", "sha256": q["source_sha256"]},
                }
                try:
                    export = e.build_saved_hold10_manifest(
                        c,
                        protocol=q["protocol"],
                        expected_protocol_sha256=sha(q["protocol"]),
                        request=request,
                        expected_request_sha256=sha(request),
                    )
                    exports.append(export)
                    disposition.update(
                        status="exported",
                        manifest_sha256=export["manifest_sha256"],
                        membership_states=export["membership_states"],
                        issue_counts=export["issue_counts"],
                    )
                except (ValueError, KeyError, TypeError) as exc:
                    disposition["reason"] = (
                        str(exc)
                        if isinstance(exc, e.InvalidSavedInput)
                        else "saved_batch_input_invalid"
                    )
            dispositions.append(disposition)
        # Optional slots are caller-declared historical evidence, not inferred
        # from today's config. Never silently treat source='cli' as manual.
        slots = None
        if q["schedule"] is not None:
            jobs = _rows(c, "job_runs")
            slots = [
                _slot_evidence(slot, jobs, selected, cutoff)
                for slot in q["schedule"]["slots"]
            ]
    snapshot.verify()
    assert sha(inventory) == frozen_inventory_pin
    reasons = []
    if orphans:
        reasons.append("interval_candidates_without_retained_batch")
    if any(d["status"] != "exported" for d in dispositions):
        reasons.append("unavailable_retained_batches")
    if slots is not None and any(not slot["completed_batch_ids"] for slot in slots):
        reasons.append("unserved_declared_schedule_slots")
    return _copy(
        {
            "contract": q,
            "enrollment_key": enrollment_key(q),
            "inventory": inventory,
            "inventory_sha256": frozen_inventory_pin,
            "dispositions": dispositions,
            "exports": exports,
            "schedule_slots": slots,
            "coverage_blockers": reasons,
            "schedule_completeness_certified": False,
            "retained_batch_count": len(selected),
            "core_candidate_count": sum(
                len(d["core_candidate_ids"]) for d in dispositions
            ),
            "interval_orphan_core_count": sum(
                x["shadow_tier"] in (None, 0) for x in orphans
            ),
            "outside_started_batch_boundary_candidates": [
                x["candidate_id"]
                for x in anomalous
                if x["scan_batch_id"] in all_batch_ids
            ],
            **s.FLAGS,
        }
    )


def _schema(c):
    marks = ",".join("?" for _ in TABLES)
    return [
        tuple(r)
        for r in c.execute(
            f"SELECT type,name,tbl_name,sql FROM sqlite_master WHERE tbl_name IN ({marks}) ORDER BY type,name",
            TABLES,
        )
    ]


def _verify_schema(c):
    expected = sqlite3.connect(":memory:")
    try:
        for statement in (*h.DDL, *DDL, *s.DDL):
            expected.execute(statement)
        if _schema(c) != _schema(expected):
            raise ValueError("collection_schema_or_immutability_mismatch")
    finally:
        expected.close()


def initialize_collections(c, *, purpose):
    if purpose != "scratch_or_manual_dry_run":
        raise ValueError("explicit_scratch_initialization_required")
    with h._atomic(c):
        if _schema(c):
            _verify_schema(c)
            return
        for statement in (*h.DDL, *DDL, *s.DDL):
            c.execute(statement)
        _verify_schema(c)


def _state(c):
    _verify_schema(c)
    return {
        "schema": _schema(c),
        "rows": {table: _rows(c, table) for table in TABLES},
        "database_inventory": database_inventory(c),
    }


def database_inventory(c):
    """Exact original schema/rows for copied-data preservation and publish CAS."""
    schema = [
        tuple(r)
        for r in c.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        )
    ]
    tables = {}
    for (name,) in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ):
        quoted = '"' + name.replace('"', '""') + '"'
        columns = [tuple(r) for r in c.execute(f"PRAGMA table_info({quoted})")]
        digest, count = hashlib.sha256(), 0
        for row in c.execute(f"SELECT * FROM {quoted} ORDER BY rowid"):
            normalized = [
                {"sqlite_blob_hex": x.hex()} if isinstance(x, bytes) else x for x in row
            ]
            digest.update(h._json(normalized).encode())
            digest.update(b"\n")
            count += 1
        tables[name] = {"rows": count, "sha256": digest.hexdigest(), "columns": columns}
    return {"schema": schema, "tables": tables}


def _read_collection(c, cid):
    _verify_schema(c)
    row = h._read(c, TABLE, "collection_id", cid)
    if row is None:
        raise ValueError("collection_not_registered")
    q = _contract(row["payload"]["contract"])
    if (
        row["collection_id"] != q["collection_id"]
        or row["enrollment_key"] != enrollment_key(q)
        or _dt(row["recorded_at_utc"]) > _now()
        or _dt(q["as_of_utc"]) > _dt(row["recorded_at_utc"])
        or _dt(row["recorded_at_utc"])
        > _dt(q["analysis_specification"]["analysis_at_utc"])
    ):
        raise ValueError("collection_receipt_identity_or_clock_invalid")
    return row


def _receiver(c, snapshot):
    if not isinstance(snapshot, SavedLedger):
        raise ValueError("bound_retained_snapshot_required")
    path = next((r[2] for r in c.execute("PRAGMA database_list") if r[1] == "main"), "")
    if path:
        target = Path(path).absolute()
        if (
            target != target.resolve(strict=True)
            or not target.is_relative_to(snapshot.root)
            or target == snapshot.path
        ):
            raise ValueError("receiver_must_be_distinct_scratch_database")
        if target.stat().st_nlink != 1:
            raise ValueError("receiver_hardlink_refused")


class CompleteHold10Collections:
    def __init__(self, connection, *, dry_run=False):
        self.c, self.dry_run = connection, dry_run is True

    def register(self, snapshot, contract, *, expected_contract_sha256):
        if not self.dry_run:
            return {"status": "disabled", **s.FLAGS}
        if self.c.in_transaction:
            raise ValueError("collection_requires_own_transaction")
        _receiver(self.c, snapshot)
        payload = build_collection(
            snapshot, contract, expected_contract_sha256=expected_contract_sha256
        )
        q = payload["contract"]
        if _now() > _dt(q["analysis_specification"]["analysis_at_utc"]):
            raise ValueError("collection_registration_after_fixed_cutoff")
        before = _state(self.c)
        for old in before["rows"][TABLE]:
            receipt = _read_collection(self.c, old["collection_id"])
            if receipt["enrollment_key"] == payload["enrollment_key"]:
                if receipt["payload"] != payload:
                    raise ValueError("immutable_collection_semantic_identity_conflict")
                return self.read(
                    snapshot,
                    q["collection_id"],
                    expected_receipt_sha256=receipt["receipt_sha256"],
                )
        # Inherited receipt APIs deliberately reject enclosing transactions.
        # Stage their entire output in memory, then atomically publish the delta.
        stage = sqlite3.connect(":memory:")
        try:
            self.c.backup(stage)
            store = h.Hold10Receipts(stage, dry_run=True)
            store.register_protocol(q["protocol"], expected_sha256=sha(q["protocol"]))
            roster = []
            for export in payload["exports"]:
                m = export["manifest"]
                store.enroll_cohort(
                    q["protocol"]["protocol_id"],
                    m,
                    expected_protocol_sha256=sha(q["protocol"]),
                    expected_manifest_sha256=export["manifest_sha256"],
                )
                roster.append(
                    {
                        "cohort_id": m["cohort_id"],
                        "manifest_sha256": export["manifest_sha256"],
                    }
                )
            now = _now().isoformat()
            with h._atomic(stage):
                collection = h._insert(
                    stage,
                    TABLE,
                    {
                        "collection_id": q["collection_id"],
                        "enrollment_key": payload["enrollment_key"],
                    },
                    payload,
                    now,
                )
            if roster:
                latest_decision = max(
                    [
                        _dt(q["end_at_utc"]),
                        *[
                            _dt(o["decision_at_utc"])
                            for x in payload["exports"]
                            for o in x["manifest"]["opportunities"]
                        ],
                    ]
                )
                definition = _study_contract(
                    q,
                    roster,
                    latest_decision.isoformat(),
                    {
                        "collection_id": q["collection_id"],
                        "receipt_sha256": collection["receipt_sha256"],
                    },
                )
                s.Hold10StudyReceipts(stage, dry_run=True).register_study(
                    definition, expected_study_sha256=sha(definition)
                )
            after = _state(stage)
            snapshot.verify()
            with h._atomic(self.c):
                if _state(self.c) != before:
                    raise ValueError("collection_receiver_changed_before_publish")
                snapshot.verify()
                if _now() > _dt(q["analysis_specification"]["analysis_at_utc"]):
                    raise ValueError("collection_publish_after_fixed_cutoff")
                for table in TABLES:
                    for row in after["rows"][table]:
                        if row not in before["rows"][table]:
                            self.c.execute(
                                f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                                tuple(row.values()),
                            )
            return self.read(
                snapshot,
                q["collection_id"],
                expected_receipt_sha256=collection["receipt_sha256"],
            )
        finally:
            stage.close()

    def read(self, snapshot, collection_id, *, expected_receipt_sha256):
        _receiver(self.c, snapshot)
        with e._snapshot(self.c):
            row = _read_collection(self.c, collection_id)
            if row["receipt_sha256"] != expected_receipt_sha256:
                raise ValueError("collection_receipt_pin_mismatch")
            payload = row["payload"]
            fresh = build_collection(
                snapshot,
                payload["contract"],
                expected_contract_sha256=sha(payload["contract"]),
            )
            if fresh != payload:
                raise ValueError("collection_census_or_roster_changed")
            study = None
            if payload["exports"]:
                q = payload["contract"]
                study = s.Hold10StudyReceipts(self.c).read_study(q["study_id"])
                roster = [
                    {
                        "cohort_id": x["manifest"]["cohort_id"],
                        "manifest_sha256": x["manifest_sha256"],
                    }
                    for x in payload["exports"]
                ]
                latest = max(
                    [
                        _dt(q["end_at_utc"]),
                        *[
                            _dt(o["decision_at_utc"])
                            for x in payload["exports"]
                            for o in x["manifest"]["opportunities"]
                        ],
                    ]
                )
                expected = _study_contract(
                    q,
                    roster,
                    latest.isoformat(),
                    {
                        "collection_id": collection_id,
                        "receipt_sha256": row["receipt_sha256"],
                    },
                )
                if study is None or study["payload"] != expected:
                    raise ValueError("study_does_not_cover_complete_collection")
                # Validate cohort/member chains even before analysis exists.
                s.Hold10StudyReceipts(self.c).capture(
                    q["study_id"], expected_study_receipt_sha256=study["receipt_sha256"]
                )
            return {"collection": row, "study": study}

    def measure(self, snapshot, collection_id, *, expected_receipt_sha256):
        if not self.dry_run:
            return {"status": "disabled", **s.FLAGS}
        bound = self.read(
            snapshot, collection_id, expected_receipt_sha256=expected_receipt_sha256
        )
        payload = bound["collection"]["payload"]
        cutoff = _dt(payload["contract"]["as_of_utc"])
        analysis_cutoff = _dt(
            payload["contract"]["analysis_specification"]["analysis_at_utc"]
        )
        bars = defaultdict(list)
        # This is deliberately after input enrollment. Existing outcome/replay
        # scalar columns are never used as substitute measurement evidence.
        with (
            _saved_source_only(snapshot.conn, measurement=True),
            e._snapshot(snapshot.conn),
        ):
            for row in _rows(snapshot.conn, "daily_bars"):
                if _dt(row["created_at_utc"]) <= cutoff:
                    bars[row["symbol"]].append(row)
        snapshot.verify()
        store = h.Hold10Receipts(self.c, dry_run=True)
        states: Counter[str] = Counter()
        for export in payload["exports"]:
            view = store.cohort_view(
                export["manifest"]["cohort_id"],
                expected_manifest_sha256=export["manifest_sha256"],
            )
            for member, last in zip(view["members"], view["latest_measurements"]):
                if not member["payload"]["eligible"]:
                    states["not_selected_or_unresolved"] += 1
                    continue
                if last:
                    states[
                        last["payload"]["measurement"]["status"] + "_already_recorded"
                    ] += 1
                    continue
                if _now() >= analysis_cutoff:
                    raise ValueError("measurement_fixed_cutoff_reached")
                values = bars[member["payload"]["input"]["symbol"]]
                receipt = store.record_measurement(
                    member["member_id"],
                    values,
                    expected_member_receipt_sha256=member["receipt_sha256"],
                    expected_bars_sha256=sha(values),
                )
                states[receipt["payload"]["measurement"]["status"]] += 1
        snapshot.verify()
        return {"status": "dry_run_measurement_pass", "states": dict(states), **s.FLAGS}

    def analyze(self, snapshot, collection_id, *, expected_receipt_sha256, save=False):
        if save and not self.dry_run:
            return {"status": "disabled", **s.FLAGS}
        bound = self.read(
            snapshot, collection_id, expected_receipt_sha256=expected_receipt_sha256
        )
        row, study = bound["collection"], bound["study"]
        payload, q = row["payload"], row["payload"]["contract"]
        if _dt(q["analysis_specification"]["analysis_at_utc"]) > _now():
            raise ValueError("fixed_analysis_cutoff_not_reached")
        result = {
            "method": METHOD,
            "status": "blocked_complete_collection",
            "collection_receipt_sha256": row["receipt_sha256"],
            "source_sha256": q["source_sha256"],
            "population": q["protocol"]["population"],
            "population_meaning": "saved primary AI proposals"
            if q["protocol"]["population"] == "primary_proposed"
            else "saved final combined-policy proposals, including adjustments; not pure AI effect",
            "retained_batch_count": payload["retained_batch_count"],
            "core_candidate_count": payload["core_candidate_count"],
            "interval_orphan_core_count": payload["interval_orphan_core_count"],
            "outside_started_batch_boundary_candidates": payload[
                "outside_started_batch_boundary_candidates"
            ],
            "batch_dispositions": payload["dispositions"],
            "schedule_slots": payload["schedule_slots"],
            "schedule_completeness_certified": False,
            "coverage_blockers": payload["coverage_blockers"],
            "inference": None,
            "formal_p": None,
            "q_value": None,
            **s.FLAGS,
        }
        if study is None:
            result["blockers"] = [
                *payload["coverage_blockers"],
                "no_exportable_cohorts",
            ]
            return result
        api = s.Hold10StudyReceipts(self.c, dry_run=self.dry_run)
        cap = api.capture(
            study["study_id"], expected_study_receipt_sha256=study["receipt_sha256"]
        )
        underlying = s.analyze_study_capture(cap, expected_capture_sha256=sha(cap))
        if underlying["status"] == "invalid_study_analysis":
            raise ValueError("underlying_study_validation_failed")
        snapshot.verify()
        # An old study receipt cannot represent batches that could not be
        # exported. Keep its calculation diagnostic rather than persisting a
        # disconnected subset result as this collection's analysis receipt.
        saved = (
            api.record_analysis(cap, expected_capture_sha256=sha(cap))
            if save and not payload["coverage_blockers"]
            else None
        )
        blockers = list(
            dict.fromkeys([*payload["coverage_blockers"], *underlying["blockers"]])
        )
        result.update(
            status="blocked_complete_collection"
            if blockers
            else "draft_complete_collection_analysis",
            blockers=blockers,
            study_receipt_sha256=study["receipt_sha256"],
            analysis_receipt_sha256=saved["receipt_sha256"] if saved else None,
            analysis_persistence_limitation=(
                "collection_coverage_incomplete_no_whole_collection_analysis_receipt"
                if payload["coverage_blockers"]
                else None
            ),
            selected_opportunities=underlying["selected_opportunities"],
            selected_span_days=underlying["selected_span_days"],
            known_abstentions=underlying["known_abstentions"],
            unresolved_opportunity_ids=underlying["unresolved_opportunity_ids"],
            selected_missing_member_ids=underlying["selected_missing_member_ids"],
            dependence_support=underlying["dependence_support"],
            inference=underlying["inference"] if not blockers else None,
        )
        # Retain individual actually measured pairs for diagnosis, never call
        # their available-case mean the complete population estimand.
        pairs: dict[str, dict] = defaultdict(dict)
        for cohort in cap["cohorts"]:
            for member, measurement in zip(
                cohort["view"]["members"], cohort["view"]["latest_measurements"]
            ):
                if (
                    member["payload"]["eligible"]
                    and measurement
                    and measurement["payload"]["measurement"]["status"] == "complete"
                ):
                    value = measurement["payload"]["measurement"]
                    pairs[member["opportunity_id"]][member["arm"]] = value
        result["available_pair_measurements"] = [
            {"opportunity_id": cid, "arms": arms}
            for cid, arms in pairs.items()
            if set(arms) == {"tested", "reference"}
        ]
        result["available_pair_count"] = len(result["available_pair_measurements"])
        result["available_pair_scope"] = (
            "individual source-backed paired diagnostics; not a complete-case study estimate"
        )
        return result
