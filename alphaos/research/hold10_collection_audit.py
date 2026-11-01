"""Inactive, bounded collection journal and recoverable designated collection route.

A durable intent precedes the separate compact commit. No retry replays a capture;
an interrupted operation is linked to its single exact successor or remains refused.
The journal is evidence, not a production activation or broad runtime attestation.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
import base64
import zlib
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

from alphaos.research import hold10_designated_registry as d
from alphaos.research import hold10_policy_binding as policy
from alphaos.research import hold10_prospective as v1
from alphaos.research import hold10_prospective_compact as compact

VERSION = "hold10_collection_audit_v1"
MAX_BYTES = 64 * 1024 * 1024
MAX_EVENTS = 5000
TERMINAL_RESERVE = 8192
CAPACITY_RESERVE = 2048


def _size(value):
    return len(v1.h._json(value).encode())


MAX_EXPANDED_EVENT = 8 * 1024 * 1024
MAX_EXPANDED_HISTORY = 256 * 1024 * 1024


def _encode(row):
    raw = v1.h._json(row["payload"]).encode()
    if len(raw) > MAX_EXPANDED_EVENT:
        raise ValueError("bounded_audit_payload_required")
    return {k: v for k, v in row.items() if k != "payload"} | {
        "payload_zlib_b64": base64.b64encode(zlib.compress(raw, 9)).decode(),
        "payload_sha256": v1.sha(row["payload"]),
        "expanded_bytes": len(raw),
    }


def _decode(stored):
    if (
        type(stored.get("expanded_bytes")) is not int
        or not 0 <= stored["expanded_bytes"] <= MAX_EXPANDED_EVENT
    ):
        raise ValueError("bounded_audit_payload_required")
    decoder = zlib.decompressobj()
    raw = decoder.decompress(
        base64.b64decode(stored["payload_zlib_b64"], validate=True),
        MAX_EXPANDED_EVENT + 1,
    )
    if not decoder.eof or decoder.unused_data or len(raw) != stored["expanded_bytes"]:
        raise ValueError("invalid_compressed_audit")
    payload = json.loads(raw)
    if (
        v1.h._json(payload).encode() != raw
        or v1.sha(payload) != stored["payload_sha256"]
    ):
        raise ValueError("invalid_audit_payload_hash")
    if stored.get("attempt_id") != payload.get("attempt_id"):
        raise ValueError("audit_attempt_identity_mismatch")
    return {
        k: v
        for k, v in stored.items()
        if k not in {"payload_zlib_b64", "payload_sha256", "expanded_bytes"}
    } | {"payload": payload}


class _Event(dict):
    """Authenticate compressed envelope eagerly, expand payload only on access."""

    def __init__(self, stored):
        super().__init__(
            {
                k: v
                for k, v in stored.items()
                if k not in {"payload_zlib_b64", "payload_sha256", "expanded_bytes"}
            }
        )
        self.stored = stored
        self.expanded_bytes = stored["expanded_bytes"]
        self.decoded = None

    def __getitem__(self, key):
        if key == "payload":
            if self.decoded is None:
                self.decoded = _decode(self.stored)["payload"]
            return self.decoded
        return super().__getitem__(key)

    def materialize(self):
        return dict(self) | {"payload": self["payload"]}


class CollectionAuditJournal:
    """External identity pin is returned at creation and required on every reopen."""

    def __init__(self, path, *, expected_identity_sha256, enabled=False):
        self.path = Path(path).absolute()
        self.expected = expected_identity_sha256
        self.enabled = enabled is True

    @classmethod
    def create(cls, path, *, designation_sha256, enabled=False):
        if enabled is not True:
            return {"status": "disabled", **v1.FLAGS}
        path = Path(path).absolute()
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("unaliased_audit_path_required")
        if any((p / ".env").exists() for p in path.parents) or "sources" in path.parts:
            raise ValueError("inactive_audit_path_required")
        if not re.fullmatch(r"[0-9a-f]{64}", designation_sha256):
            raise ValueError("designation_pin_required")
        lock = Path(str(path) + ".lock")
        lock_fd = os.open(
            lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
        )
        os.close(lock_fd)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        identity = {
            "version": VERSION,
            "file": d._identity(path),
            "lock": d._identity(lock),
            "journal_id": str(uuid.uuid4()),
            "designation_sha256": designation_sha256,
        }
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE identity(payload TEXT NOT NULL)")
            db.execute("INSERT INTO identity VALUES(?)", (v1.h._json(identity),))
            db.execute(
                "CREATE TABLE events(sequence INTEGER PRIMARY KEY,payload TEXT NOT NULL)"
            )
            for table in ("identity", "events"):
                for action in ("UPDATE", "DELETE"):
                    db.execute(
                        f"CREATE TRIGGER immutable_{table}_{action} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'immutable_audit'); END"
                    )
            db.commit()
        d._sync_dir(path.parent)
        return {"identity": identity, "identity_sha256": v1.sha(identity), **v1.FLAGS}

    @contextmanager
    def _locked(self):
        if any(p.is_symlink() for p in (self.path, *self.path.parents)):
            raise ValueError("unaliased_audit_path_required")
        fd = os.open(str(self.path) + ".lock", os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with closing(
                sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=0)
            ) as db:
                db.execute("PRAGMA synchronous=FULL")
                identity, events, size = self._read(db)
                if identity["lock"] != {
                    "device": os.fstat(fd).st_dev,
                    "inode": os.fstat(fd).st_ino,
                }:
                    raise ValueError("audit_identity_changed")
                yield db, identity, events, size
        finally:
            os.close(fd)

    def _read(self, db):
        expected_schema = {("identity", "table"), ("events", "table")} | {
            (f"immutable_{table}_{action}", "trigger")
            for table in ("identity", "events")
            for action in ("UPDATE", "DELETE")
        }
        schema = db.execute(
            "SELECT name,type,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if {(x[0], x[1]) for x in schema} != expected_schema:
            raise ValueError("audit_schema_changed")
        for name, kind, sql in schema:
            if kind == "trigger":
                _, table, action = name.split("_")
                expected_sql = f"CREATE TRIGGER immutable_{table}_{action} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'immutable_audit'); END"
                if sql != expected_sql:
                    raise ValueError("audit_schema_changed")
            elif kind == "table":
                wanted = {
                    "identity": "CREATE TABLE identity(payload TEXT NOT NULL)",
                    "events": "CREATE TABLE events(sequence INTEGER PRIMARY KEY,payload TEXT NOT NULL)",
                }[name]
                if sql != wanted:
                    raise ValueError("audit_schema_changed")
        expanded_total = 0
        # Bound raw storage before materializing untrusted JSON.
        count, raw = db.execute(
            "SELECT count(*),coalesce(sum(length(CAST(payload AS BLOB))),0) FROM events"
        ).fetchone()
        if count > MAX_EVENTS or raw > MAX_BYTES:
            raise ValueError("audit_capacity_corrupt")
        for (raw_text,) in db.execute("SELECT payload FROM events"):
            metadata = json.loads(raw_text)
            expanded = metadata.get("expanded_bytes")
            if type(expanded) is not int or not 0 <= expanded <= MAX_EXPANDED_EVENT:
                raise ValueError("bounded_audit_payload_required")
            expanded_total += expanded
            if expanded_total > MAX_EXPANDED_HISTORY:
                raise ValueError("audit_expanded_capacity_exhausted")
        identities = db.execute("SELECT payload FROM identity LIMIT 2").fetchall()
        if len(identities) != 1 or len(identities[0][0]) > 2048:
            raise ValueError("audit_identity_invalid")
        identity = json.loads(identities[0][0])
        if v1.sha(identity) != self.expected or identity["file"] != d._identity(
            self.path
        ):
            raise ValueError("audit_identity_pin_mismatch")
        events: list[_Event] = []
        prior: str | None = None
        size = _size(identity)
        prior_time = None
        for sequence, raw_text in db.execute(
            "SELECT sequence,payload FROM events ORDER BY sequence"
        ):
            stored = json.loads(raw_text)
            if raw_text != v1.h._json(stored):
                raise ValueError("noncanonical_audit")
            if (
                set(stored)
                != {
                    "version",
                    "sequence",
                    "previous_sha256",
                    "recorded_at_utc",
                    "kind",
                    "attempt_id",
                    "payload_zlib_b64",
                    "payload_sha256",
                    "expanded_bytes",
                    "receipt_sha256",
                }
                or stored["version"] != VERSION
                or stored["kind"]
                not in {
                    "runtime_identity",
                    "expected_runtime_manifest",
                    "collection_prepared",
                    "collection_terminal",
                    "collection_error",
                    "request_refused",
                    "capacity_exhausted",
                }
            ):
                raise ValueError("audit_event_shape_invalid")
            at = v1.c._dt(stored["recorded_at_utc"])
            if at > v1._clock() or (prior_time is not None and at < prior_time):
                raise ValueError("audit_clock_invalid")
            prior_time = at
            unsigned = {k: v for k, v in stored.items() if k != "receipt_sha256"}
            digest = stored["receipt_sha256"]
            if (
                sequence != len(events) + 1
                or stored["sequence"] != sequence
                or stored["previous_sha256"] != prior
                or v1.sha(unsigned) != digest
            ):
                raise ValueError("audit_chain_invalid")
            row = _Event(stored)
            prior = digest
            size += _size(stored)
            events.append(row)
        if size > MAX_BYTES:
            raise ValueError("audit_capacity_corrupt")
        return identity, events, size

    def _append(
        self, db, identity, events, size, kind, payload, *, reserve=CAPACITY_RESERVE
    ):
        if events and v1._clock() < v1.c._dt(events[-1]["recorded_at_utc"]):
            raise ValueError("audit_clock_regression")
        row = {
            "version": VERSION,
            "sequence": len(events) + 1,
            "previous_sha256": events[-1]["receipt_sha256"] if events else None,
            "recorded_at_utc": v1._clock().isoformat(),
            "kind": kind,
            "attempt_id": payload.get("attempt_id"),
            "payload": v1._copy(payload),
        }
        pending = {
            x["attempt_id"] for x in events if x["kind"] == "collection_prepared"
        } - {x["attempt_id"] for x in events if x["kind"] == "collection_terminal"}
        if pending and not (
            kind == "collection_terminal" and payload.get("attempt_id") in pending
        ):
            reserve = max(reserve, TERMINAL_RESERVE + CAPACITY_RESERVE)
        stored = _encode(row)
        row["receipt_sha256"] = v1.sha(stored)
        stored["receipt_sha256"] = row["receipt_sha256"]
        if (
            sum(
                x.expanded_bytes if isinstance(x, _Event) else _size(x["payload"])
                for x in events
            )
            + stored["expanded_bytes"]
            + reserve
            > MAX_EXPANDED_HISTORY
        ):
            raise ValueError("audit_capacity_exhausted")
        if (
            size + _size(stored) + reserve > MAX_BYTES
            or len(events)
            + 1
            + (2 if reserve > CAPACITY_RESERVE else 1 if reserve else 0)
            > MAX_EVENTS
        ):
            raise ValueError("audit_capacity_exhausted")
        if identity["file"] != d._identity(self.path):
            raise ValueError("audit_identity_changed")
        db.execute(
            "INSERT INTO events VALUES(?,?)", (row["sequence"], v1.h._json(stored))
        )
        db.commit()
        events.append(row)
        return row

    def append_runtime_identity(self, payload):
        if not self.enabled:
            return {"status": "disabled", **v1.FLAGS}
        if _size(payload) > 1024 * 1024:
            raise ValueError("bounded_runtime_receipt_required")
        with self._locked() as (db, identity, events, size):
            return self._append(
                db, identity, events, size, "runtime_identity", payload
            )["receipt_sha256"]

    def read(self):
        with self._locked() as (_, identity, events, size):
            return {
                "identity": identity,
                "events": [
                    x.materialize() if isinstance(x, _Event) else x for x in events
                ],
                "canonical_bytes": size,
                "max_bytes": MAX_BYTES,
                "max_events": MAX_EVENTS,
                "physical_sqlite_bytes_included": False,
                "max_expanded_history_bytes": MAX_EXPANDED_HISTORY,
                **v1.FLAGS,
            }


def _collect_designated(
    api,
    journal,
    snapshot,
    *,
    attempt_id,
    expected_designation_sha256,
    expected_previous_capture_sha256,
    runtime_requirement=None,
):
    """Default-inert, durable input census + exact two-store recovery.

    Recovery never creates a new observation at an old time. Conflicting or
    multiple compact successors stay explicitly unresolved. Journal unavailable
    means no compact write; catastrophic disk failure cannot be durably logged.
    """
    if not api.enabled or not journal.enabled:
        return {"status": "disabled", **v1.FLAGS}
    if not isinstance(attempt_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,80}", attempt_id
    ):
        raise ValueError("bounded_attempt_id_required")
    if not isinstance(
        getattr(snapshot, "expected_sha256", None), str
    ) or not re.fullmatch(r"[0-9a-f]{64}", snapshot.expected_sha256):
        raise ValueError("valid_source_pin_required")
    from alphaos.research.runtime_identity import audit_runtime_identity

    root, folder = api._folder()
    with (
        d._lock(root, folder) as check,
        journal._locked() as (db, identity, events, size),
    ):
        if identity["designation_sha256"] != expected_designation_sha256:
            raise ValueError("audit_designation_mismatch")
        manifest = api._manifest(folder, expected_designation_sha256)
        request = {
            "attempt_id": attempt_id,
            "designation_sha256": expected_designation_sha256,
            "source_sha256": snapshot.expected_sha256,
            "registration_id": manifest["registration_id"],
            "registration_sha256": manifest["registration_sha256"],
            "previous_capture_sha256": expected_previous_capture_sha256,
        }
        if runtime_requirement is not None:
            if (
                not isinstance(runtime_requirement, dict)
                or set(runtime_requirement) != {"version", "manifest_receipt_sha256"}
                or runtime_requirement["version"] != "hold10_expected_scoped_runtime_v1"
                or not isinstance(runtime_requirement["manifest_receipt_sha256"], str)
                or not re.fullmatch(
                    r"[0-9a-f]{64}", runtime_requirement["manifest_receipt_sha256"]
                )
            ):
                raise ValueError("valid_expected_manifest_receipt_required")
            request["runtime_requirement"] = v1._copy(runtime_requirement)
        if events and events[-1]["kind"] == "capacity_exhausted":
            return {"status": "audit_capacity_exhausted", **v1.FLAGS}
        matches = [
            x
            for x in events
            if x["kind"]
            in {"collection_prepared", "collection_terminal", "collection_error"}
            and x.get("attempt_id") == attempt_id
        ]
        if matches and matches[0]["payload"]["request"] != request:
            raise ValueError("attempt_id_request_conflict")
        if matches and matches[-1]["kind"] == "collection_terminal":
            return {
                "status": "already_recorded",
                "audit": matches[-1].materialize()
                if isinstance(matches[-1], _Event)
                else matches[-1],
                **v1.FLAGS,
            }
        # A different incomplete request must be explicitly recovered first.
        prepared = {
            x["attempt_id"] for x in events if x["kind"] == "collection_prepared"
        }
        ended = {x["attempt_id"] for x in events if x["kind"] == "collection_terminal"}
        if prepared - ended - {attempt_id}:
            raise ValueError("recover_pending_collection_first")

        def terminal(status, **extra):
            nonlocal size
            payload = {
                "attempt_id": attempt_id,
                "request": request,
                "status": status,
                **extra,
            }
            if _size(payload) > TERMINAL_RESERVE - 1024:
                raise ValueError("bounded_terminal_required")
            row = journal._append(
                db, identity, events, size, "collection_terminal", payload
            )
            return {"status": status, "audit": row, **v1.FLAGS}

        if matches:
            state = api._state(folder, manifest, check)["state"]
            prior = request["previous_capture_sha256"]
            candidates = [
                x for x in state["captures"] if x["previous_receipt_sha256"] == prior
            ]
            if not candidates:
                if state["latest_capture_sha256"] != prior:
                    return terminal("recovery_conflict")
                return terminal("interrupted_before_capture")
            if (
                len(candidates) != 1
                or state["latest_capture_sha256"] != candidates[0]["receipt_sha256"]
            ):
                return terminal("recovery_conflict")
            candidate = candidates[0]
            conn, _ = api._db(folder)
            try:
                receipt = compact.CompactProspectiveHold10(conn).expand_capture(
                    manifest["registration_id"],
                    candidate["sequence"],
                    expected_capture_sha256=candidate["receipt_sha256"],
                )
            finally:
                conn.close()
            if receipt["payload"].get("source_sha256") != request[
                "source_sha256"
            ] or receipt["payload"].get("inventory_sha256") != matches[0][
                "payload"
            ].get("inventory_sha256"):
                return terminal("recovery_conflict")
            return terminal(
                "observed_successor_unattributed",
                capture_sha256=candidate["receipt_sha256"],
            )

        intent = d._read(folder / "intent.json")
        runtime_rows = [x for x in events if x["kind"] == "runtime_identity"]
        audit, runtime_audit, observation = None, None, None
        expected_audit = None
        try:
            snapshot.verify()
            observation = v1._observe(
                snapshot,
                {
                    "payload": {
                        "contract": intent["contract"],
                        "schedule_slots": v1.schedule_slots(intent["contract"]),
                    }
                },
                v1._clock(),
            )
            audit = policy.audit_saved_policy(
                snapshot,
                intent["contract"],
                intent["binding"],
                expected_binding_sha256=manifest["binding_sha256"],
            )
            runtime_audit = audit_runtime_identity(
                snapshot,
                [x["candidate_id"] for x in audit["candidates"]],
                [x["payload"] for x in runtime_rows],
            )
            if runtime_requirement is not None:
                from alphaos.research.hold10_expected_runtime import audit_expected

                expected_audit = audit_expected(
                    events,
                    identity,
                    manifest,
                    intent,
                    runtime_audit,
                    expected_receipt_sha256=runtime_requirement[
                        "manifest_receipt_sha256"
                    ],
                )
            snapshot.verify()
            error = None
        except Exception as exc:
            error = type(
                exc
            ).__name__  # Never persist arbitrary exception text or secrets.
        if observation is not None:
            observation = v1._copy(observation)
            for disposition in observation["dispositions"]:
                export = disposition.pop("export", None)
                disposition["export_sha256"] = (
                    v1.sha(export) if export is not None else None
                )
        payload = {
            "attempt_id": attempt_id,
            "request": request,
            "observation": observation,
            "policy_audit": audit,
            "runtime_audit": runtime_audit,
            "runtime_receipt_sha256s": [x["receipt_sha256"] for x in runtime_rows],
            "inventory_sha256": observation.get("inventory_sha256")
            if observation
            else None,
            "error_type": error,
        }
        if runtime_requirement is not None:
            payload["expected_runtime_audit"] = expected_audit
        try:
            prepared_row = journal._append(
                db,
                identity,
                events,
                size,
                "collection_prepared",
                payload,
                reserve=TERMINAL_RESERVE + CAPACITY_RESERVE,
            )
        except ValueError as exc:
            if str(exc) != "audit_capacity_exhausted":
                raise
            if events and events[-1]["kind"] == "capacity_exhausted":
                return {"status": "audit_capacity_exhausted", **v1.FLAGS}
            row = journal._append(
                db,
                identity,
                events,
                size,
                "capacity_exhausted",
                {
                    "status": "no_capture_attempted",
                    "attempt_id": attempt_id,
                    "request_sha256": v1.sha(request),
                },
                reserve=0,
            )
            return {"status": "audit_capacity_exhausted", "audit": row, **v1.FLAGS}
        size += _size(_encode(prepared_row))
        if error:
            return terminal("collection_error", error_type=error)
        if not isinstance(audit, dict) or not isinstance(runtime_audit, dict):
            return terminal("collection_error", error_type="AuditResultUnavailable")
        if audit["contradictory_candidate_count"]:
            return terminal("refused_policy_contradiction")
        if any(x.get("status") == "contradictory" for x in runtime_audit["candidates"]):
            return terminal("refused_runtime_contradiction")

        if runtime_requirement is not None and (
            not isinstance(expected_audit, dict)
            or expected_audit["global_gaps"]
            or expected_audit["eligible_count"] == 0
            or expected_audit["complete_count"] != expected_audit["eligible_count"]
        ):
            return terminal("refused_expected_runtime_agreement")

        def guard():
            check()
            api._manifest(folder, expected_designation_sha256)
            snapshot.verify()
            if identity["file"] != d._identity(journal.path):
                raise ValueError("audit_identity_changed")

        conn, ident = api._db(folder, guard)
        try:
            try:
                state = compact.CompactProspectiveHold10(conn, dry_run=True).capture(
                    snapshot,
                    manifest["registration_id"],
                    expected_registration_sha256=manifest["registration_sha256"],
                    expected_previous_capture_sha256=expected_previous_capture_sha256,
                )
                api._check_db(folder, ident)
                check()
            except Exception as exc:
                # Preserve PREPARED for recovery when commit outcome may be ambiguous.
                # No terminal claim is made merely because a post-commit check failed.
                journal._append(
                    db,
                    identity,
                    events,
                    size,
                    "collection_error",
                    {
                        "attempt_id": attempt_id,
                        "request": request,
                        "error_type": type(exc).__name__,
                    },
                    reserve=TERMINAL_RESERVE,
                )
                return {"status": "capture_error_recovery_required", **v1.FLAGS}
            return terminal("captured", capture_sha256=state["latest_capture_sha256"])
        finally:
            conn.close()


def _collect_guarded(
    api,
    journal,
    snapshot,
    *,
    attempt_id,
    expected_designation_sha256,
    expected_previous_capture_sha256,
    runtime_requirement=None,
):
    """Persist available-journal admission refusals without echoing secret inputs."""
    try:
        return _collect_designated(
            api,
            journal,
            snapshot,
            attempt_id=attempt_id,
            expected_designation_sha256=expected_designation_sha256,
            expected_previous_capture_sha256=expected_previous_capture_sha256,
            runtime_requirement=runtime_requirement,
        )
    except Exception as exc:
        if not api.enabled or not journal.enabled:
            raise
        with journal._locked() as (db, identity, events, size):
            payload = {
                "request_sha256": v1.sha(
                    {
                        "attempt_id": attempt_id,
                        "designation": expected_designation_sha256,
                        "previous": expected_previous_capture_sha256,
                        **(
                            {"runtime_requirement": runtime_requirement}
                            if runtime_requirement is not None
                            else {}
                        ),
                    }
                ),
                "source_sha256": getattr(snapshot, "expected_sha256", None)
                if isinstance(getattr(snapshot, "expected_sha256", None), str)
                and re.fullmatch(r"[0-9a-f]{64}", snapshot.expected_sha256)
                else None,
                "error_type": type(exc).__name__,
                "status": "admission_refused",
            }
            row = journal._append(
                db, identity, events, size, "request_refused", payload
            )
            return {"status": "admission_refused", "audit": row, **v1.FLAGS}


def collect_designated(
    api,
    journal,
    snapshot,
    *,
    attempt_id,
    expected_designation_sha256,
    expected_previous_capture_sha256,
):
    """Legacy unbound collection; does not certify expected runtime agreement."""
    return _collect_guarded(
        api,
        journal,
        snapshot,
        attempt_id=attempt_id,
        expected_designation_sha256=expected_designation_sha256,
        expected_previous_capture_sha256=expected_previous_capture_sha256,
    )
