"""Optional scheduler collection bridge; no registration or research activation.

Only an externally pinned config opens existing inactive stores. Scan failures,
collection failures and scoped agreement are distinct. No provider is constructed.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from alphaos.research import hold10_collection_audit as audit
from alphaos.research import hold10_designated_registry as designated
from alphaos.research import hold10_expected_runtime as expected
from alphaos.research import hold10_prospective as v1

VERSION = "hold10_scheduler_bridge_v1"
CONFIG_ENV = "ALPHAOS_HOLD10_COLLECTOR_CONFIG"
HASH_ENV = "ALPHAOS_HOLD10_COLLECTOR_CONFIG_SHA256"
SNAPSHOT_DIRECTORY = "hold10-source-snapshots-v1"
MAX_CONFIG_BYTES = 16384
MAX_SNAPSHOTS = 500
BACKUP_SECONDS = 30
FIELDS = {
    "version", "workspace", "designation_sha256", "audit_path",
    "audit_identity_sha256", "manifest_receipt_sha256", "source_database",
    "source_device", "source_inode", "snapshot_byte_budget",
    "closure_not_before_utc",
    "snapshot_file_byte_limit",
}


def _identity(path):
    path = Path(path).absolute()
    if path != path.resolve(strict=True) or any(x.is_symlink() for x in path.parents):
        raise ValueError("unaliased_collector_path_required")
    s = path.stat()
    if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
        raise ValueError("single_link_collector_file_required")
    return (s.st_dev, s.st_ino)


def _unique(pairs):
    result = {}
    for k, value in pairs:
        if k in result:
            raise ValueError("duplicate_config_field")
        result[k] = value
    return result


def _read(path, limit=MAX_CONFIG_BYTES):
    identity = _identity(path)
    if Path(path).stat().st_size > limit:
        raise ValueError("bounded_collector_file_required")
    raw = Path(path).read_bytes()
    if len(raw) > limit or _identity(path) != identity:
        raise ValueError("collector_file_changed")
    return json.loads(raw, object_pairs_hook=_unique), raw, identity


def _result(status, **extra):
    return {"version": VERSION, "status": status, "expected_runtime_agreement": False,
            "full_runtime_attested": False, **v1.FLAGS, **extra}


class _CommitGuard:
    def __init__(self, connection, guard):
        self.connection, self.guard = connection, guard

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def commit(self):
        self.guard()
        self.connection.commit()


class _BoundJournal(audit.CollectionAuditJournal):
    config: Configuration

    @contextmanager
    def _locked(self):
        with super()._locked() as (db, identity, events, size):
            yield _CommitGuard(db, self.config.guard), identity, events, size


class _BoundDesignation(designated.DesignatedHold10):
    config: Configuration

    def _db(self, folder, guard=None):  # type: ignore[override]
        # Deliberately bind the inherited static factory to this instance guard.
        if guard is None:
            self.config.guard()
            return super()._db(folder)
        def combined():
            self.config.guard()
            if guard is not None:
                guard()
        return super()._db(folder, combined)


class Configuration:
    """Owned config bytes + external hash; neither is an activation approval."""

    def __init__(self, path, digest):
        self.path = Path(path).absolute()
        self.digest = v1.h._sha(digest, "collector_config_sha256")
        value, raw, self.identity = _read(self.path)
        if hashlib.sha256(raw).hexdigest() != self.digest or set(value) != FIELDS:
            raise ValueError("collector_config_pin_or_shape")
        if value["version"] != VERSION:
            raise ValueError("collector_config_version")
        self.value = value
        self.root = designated._workspace(value["workspace"])
        if not self.path.is_relative_to(self.root):
            raise ValueError("config_outside_retained_workspace")
        for key in ("designation_sha256", "audit_identity_sha256", "manifest_receipt_sha256"):
            v1.h._sha(value[key], key)
        for key in ("source_device", "source_inode", "snapshot_byte_budget", "snapshot_file_byte_limit"):
            if type(value[key]) is not int or value[key] < 0:
                raise ValueError("collector_integer_required")
        if not 0 < value["snapshot_byte_budget"] <= MAX_SNAPSHOTS * v1.c.MAX_SOURCE_BYTES:
            raise ValueError("bounded_snapshot_budget_required")
        if (not 0 < value["snapshot_file_byte_limit"] <= v1.c.MAX_SOURCE_BYTES
                or value["snapshot_byte_budget"] < 2 * (value["snapshot_file_byte_limit"] + MAX_CONFIG_BYTES)):
            raise ValueError("snapshot_and_closure_reservation_required")
        self.source = Path(value["source_database"]).absolute()
        if _identity(self.source) != (value["source_device"], value["source_inode"]):
            raise ValueError("source_database_identity_changed")
        self.snapshots = self.root / SNAPSHOT_DIRECTORY
        self.folder_identity = designated._identity(self.snapshots, directory=True)
        self.lock_path = self.snapshots / ".allocation.lock"
        self.lock_identity = _identity(self.lock_path)
        if self.lock_path.stat().st_size:
            raise ValueError("empty_allocation_lock_required")
        if self.snapshots.resolve(strict=True) != self.snapshots:
            raise ValueError("unaliased_snapshot_directory_required")
        self.audit_path = Path(value["audit_path"]).absolute()
        if not self.audit_path.is_relative_to(self.root) or self.audit_path == self.source:
            raise ValueError("audit_outside_retained_workspace")
        _identity(self.audit_path)
        self.api = _BoundDesignation(self.root, dry_run=True)
        self.api.config = self
        self.journal = _BoundJournal(
            self.audit_path, expected_identity_sha256=value["audit_identity_sha256"], enabled=True
        )
        self.journal.config = self
        self.guard()

    def guard(self):
        value, raw, identity = _read(self.path)
        if (identity != self.identity or value != self.value
                or hashlib.sha256(raw).hexdigest() != self.digest
                or designated._workspace(self.root) != self.root
                or designated._identity(self.snapshots, directory=True) != self.folder_identity
                or _identity(self.source) != (self.value["source_device"], self.value["source_inode"])
                or _identity(self.lock_path) != self.lock_identity):
            raise ValueError("collector_configuration_changed")

    def existing(self):
        """Validate the already frozen manifest without creating any authority."""
        self.guard()
        root, folder = self.api._folder()
        with designated._lock(root, folder) as check, self.journal._locked() as (_, identity, events, _):
            designation = self.api._manifest(folder, self.value["designation_sha256"])
            intent = designated._read(folder / "intent.json")
            frozen = expected._frozen(events, identity, self.value["manifest_receipt_sha256"])
            probe = expected.audit_expected(events, identity, designation, intent,
                {"candidates": []}, expected_receipt_sha256=self.value["manifest_receipt_sha256"])
            if probe["global_gaps"]:
                raise ValueError("invalid_existing_runtime_manifest")
            if frozen["payload"]["designation_sha256"] != self.value["designation_sha256"]:
                raise ValueError("collector_designation_mismatch")
            check()
        state = self.api.read(expected_designation_sha256=self.value["designation_sha256"])["state"]
        q = state["registration"]["payload"]["contract"]
        if not (v1.c._dt(q["end_at_utc"]) <= v1.c._dt(self.value["closure_not_before_utc"])
                < v1.c._dt(q["close_at_utc"])):
            raise ValueError("reviewed_closure_boundary_required")
        self.guard()
        return state

    def inventory(self, *, closure=False):
        paths = list(self.snapshots.iterdir())
        if len(paths) > MAX_SNAPSHOTS * 2 + 1:
            raise ValueError("snapshot_directory_count_bound")
        total, count = 0, 0
        for path in paths:
            _identity(path)
            if path == self.lock_path:
                if path.stat().st_size:
                    raise ValueError("empty_allocation_lock_required")
                continue
            if not re.fullmatch(r"(?:scan_[A-Za-z0-9_-]+|closure_[0-9a-f]{64})\.(sqlite|request\.json)", path.name):
                raise ValueError("unrecognized_snapshot_artifact")
            total += path.stat().st_size
            count += path.suffix == ".sqlite"
        if total > self.value["snapshot_byte_budget"] or count >= MAX_SNAPSHOTS - (0 if closure else 1):
            raise ValueError("snapshot_retention_capacity")
        return total


class _Sink:
    def __init__(self, config):
        self.config = config
        self.sink = expected.ExpectedRuntimeSink(config.journal,
            expected_manifest_receipt_sha256=config.value["manifest_receipt_sha256"])

    def append_runtime_identity(self, payload):
        self.config.guard()
        return self.sink.append_runtime_identity(payload)


class Session:
    def __init__(self, orch, path, digest):
        self.orch = orch
        self.config = Configuration(path, digest)
        self.previous_sink = getattr(orch, "runtime_identity_sink", None)
        if self.previous_sink is not None:
            raise ValueError("runtime_sink_already_configured")
        state = self.config.existing()
        if state["state"] != "collecting":
            raise ValueError("collection_interval_not_open")
        if v1._clock() >= v1.c._dt(state["registration"]["payload"]["contract"]["end_at_utc"]):
            raise ValueError("enrollment_closed_checkpoint_required")
        self.previous = state["latest_capture_sha256"]
        self.before_failures = getattr(orch, "runtime_identity_failures", 0)
        self.highwater = orch.journal.scalar("SELECT coalesce(max(rowid),0) FROM scan_batches")
        source_names = orch.journal.conn.execute("PRAGMA database_list").fetchall()
        main = next(row[2] for row in source_names if row[1] == "main")
        if not main or Path(main).resolve() != self.config.source:
            raise ValueError("collector_producer_database_mismatch")
        self.config.inventory()
        self.source = sqlite3.connect(self.config.source.as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            self.source.execute("PRAGMA query_only=ON")
            self.config.guard()
        except BaseException:
            self.source.close()
            raise
        orch.runtime_identity_sink = _Sink(self.config)

    def restore(self):
        self.orch.runtime_identity_sink = self.previous_sink
        self.source.close()

    def record_error(self, error_type):
        # No source snapshot may exist yet: this is an operational error,
        # explicitly not a full-census PREPARED receipt or completed capture.
        with self.config.journal._locked() as (db, identity, events, size):
            return self.config.journal._append(db, identity, events, size, "collection_error",
                {"attempt_id": None, "bridge_version": VERSION,
                 "phase": "bridge_before_collection_completion", "error_type": error_type,
                 "full_census_retained": False, "expected_runtime_agreement": False})

    def finish(self, batch=None):
        config = self.config
        config.guard()
        if batch is None:
            found = self.orch.journal.query(
                "SELECT scan_batch_id FROM scan_batches WHERE rowid>? ORDER BY rowid LIMIT 2",
                (self.highwater,))
            if len(found) != 1:
                raise ValueError("single_new_scan_batch_required")
            batch = found[0]["scan_batch_id"]
        if not isinstance(batch, str) or not re.fullmatch(r"scan_[A-Za-z0-9_-]{1,100}", batch):
            raise ValueError("bounded_scan_batch_identity_required")
        request_path = _save_snapshot(config, self.source, self.previous, batch, "scheduled_scan")
        result = _collect(config, request_path)
        failures = getattr(self.orch, "runtime_identity_failures", 0) - self.before_failures
        if failures:
            result["expected_runtime_agreement"] = False
        return result | {"runtime_receipt_failures": failures}


@contextmanager
def _allocation(config):
    config.guard()
    fd = os.open(config.lock_path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != config.lock_identity:
            raise ValueError("allocation_lock_changed")
        config.guard()
        yield
        config.guard()
    finally:
        os.close(fd)


def _save_snapshot(config, source, previous, batch, kind):
    with _allocation(config):
        closure = kind == "enrollment_closure"
        total = config.inventory(closure=closure)
        reserve = 0 if closure else config.value["snapshot_file_byte_limit"] + MAX_CONFIG_BYTES
        destination = config.snapshots / (batch + ".sqlite")
        request_path = config.snapshots / (batch + ".request.json")
        if destination.exists() or request_path.exists():
            raise ValueError("retained_attempt_requires_explicit_resume")
        pages = source.execute("PRAGMA page_count").fetchone()[0]
        page_size = source.execute("PRAGMA page_size").fetchone()[0]
        size = pages * page_size
        if size > config.value["snapshot_file_byte_limit"] or total + size + reserve + MAX_CONFIG_BYTES > config.value["snapshot_byte_budget"]:
            raise ValueError("snapshot_retention_capacity")
        # O_EXCL never replaces evidence. An interrupted file without a request
        # remains visibly incomplete and needs review; it is never auto-reused.
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        identity = _identity(destination)
        began = time.monotonic()
        def progress(status, remaining, count):
            config.guard()
            if (_identity(destination) != identity or count * page_size > config.value["snapshot_file_byte_limit"]
                    or total + count * page_size + reserve + MAX_CONFIG_BYTES > config.value["snapshot_byte_budget"]
                    or time.monotonic() - began > BACKUP_SECONDS):
                raise ValueError("snapshot_backup_bound_or_identity")
        with closing(sqlite3.connect(destination)) as target:
            source.backup(target, pages=256, progress=progress, sleep=0.01)
        config.guard()
        if _identity(destination) != identity:
            raise ValueError("snapshot_identity_changed")
        with destination.open("rb") as file:
            os.fsync(file.fileno())
        designated._sync_dir(destination.parent)
        request = {"version": VERSION, "config_sha256": config.digest, "scan_batch_id": None if closure else batch, "capture_kind": kind,
            "snapshot_name": destination.name, "source_sha256": v1.c._file_hash(destination),
            "attempt_id": batch, "expected_previous_capture_sha256": previous,
            "expected_designation_sha256": config.value["designation_sha256"],
            "expected_manifest_receipt_sha256": config.value["manifest_receipt_sha256"]}
        config.guard()
        designated._publish(request_path, request)
        return request_path


def _collect(config, request_path):
    config.guard()
    request_path = Path(request_path).absolute()
    if request_path.parent != config.snapshots:
        raise ValueError("resume_request_outside_snapshot_directory")
    request, _, _ = _read(request_path)
    if set(request) != {"version", "config_sha256", "scan_batch_id", "capture_kind", "snapshot_name",
            "source_sha256", "attempt_id", "expected_previous_capture_sha256",
            "expected_designation_sha256", "expected_manifest_receipt_sha256"}:
        raise ValueError("collector_request_shape")
    if (request["version"] != VERSION or request["config_sha256"] != config.digest
            or request["expected_designation_sha256"] != config.value["designation_sha256"]
            or request["expected_manifest_receipt_sha256"] != config.value["manifest_receipt_sha256"]
            or request["snapshot_name"] != request["attempt_id"] + ".sqlite"
            or request_path.name != request["attempt_id"] + ".request.json"):
        raise ValueError("collector_request_pin_mismatch")
    if request["capture_kind"] == "scheduled_scan":
        if request["attempt_id"] != request["scan_batch_id"] or not re.fullmatch(r"scan_[A-Za-z0-9_-]{1,100}", request["attempt_id"]):
            raise ValueError("collector_scan_identity")
    elif request["capture_kind"] == "enrollment_closure":
        if request["scan_batch_id"] is not None or request["attempt_id"] != _closure_id(config):
            raise ValueError("collector_closure_identity")
    else:
        raise ValueError("collector_capture_kind")
    source = v1.c.SavedLedger(config.snapshots / request["snapshot_name"],
        request["source_sha256"], root=config.root)
    try:
        result = expected.collect_designated_expected_runtime(config.api, config.journal, source,
            **{k: request[k] for k in ("attempt_id", "expected_previous_capture_sha256",
                "expected_designation_sha256", "expected_manifest_receipt_sha256")})
        config.guard()
        return _result(result["status"], expected_runtime_agreement=result["expected_runtime_agreement"],
            source_sha256=request["source_sha256"], scan_batch_id=request["scan_batch_id"],
            audit_receipt_sha256=result.get("audit", {}).get("receipt_sha256"))
    finally:
        source.close()


def resume(config_path, expected_config_sha256, request_path):
    """Exact retained-attempt retry only; no new scan, source snapshot or enrollment."""
    config = Configuration(config_path, expected_config_sha256)
    config.existing()
    return _collect(config, request_path)


def _closure_id(config):
    return "closure_" + v1.sha([VERSION, config.value["designation_sha256"], "enrollment_closure"])


def closure_checkpoint(config_path, expected_config_sha256):
    """One explicit reviewed-time checkpoint. Never runs a scan or synthesizes runtime."""
    config = Configuration(config_path, expected_config_sha256)
    state = config.existing()
    attempt = _closure_id(config)
    path = config.snapshots / (attempt + ".request.json")
    if path.exists():
        return _collect(config, path)
    now = v1._clock()
    if now < v1.c._dt(config.value["closure_not_before_utc"]):
        raise ValueError("reviewed_closure_boundary_not_reached")
    if state["state"] != "collecting":
        raise ValueError("closure_requires_open_fixed_deadline")
    with closing(sqlite3.connect(config.source.as_uri() + "?mode=ro", uri=True, timeout=1)) as source:
        source.execute("PRAGMA query_only=ON")
        config.guard()
        path = _save_snapshot(config, source, state["latest_capture_sha256"], attempt, "enrollment_closure")
    return _collect(config, path)


def configured():
    return CONFIG_ENV in os.environ or HASH_ENV in os.environ


@contextmanager
def scan_collection(orch):
    """Errors affect research status only; original scan exceptions propagate."""
    result = _result("disabled")
    session = None
    if configured():
        try:
            session = Session(orch, os.environ.get(CONFIG_ENV), os.environ.get(HASH_ENV))
            result = _result("pending")
        except Exception as exc:
            result = _result("blocked", reason=("enrollment_closed_checkpoint_required" if str(exc) == "enrollment_closed_checkpoint_required" else "collector_configuration_unavailable"), error_type=type(exc).__name__)
    try:
        yield result
    finally:
        if session is not None:
            try:
                result.update(session.finish())
            except Exception as exc:
                result.update(_result("blocked", reason="collector_capture_unavailable", error_type=type(exc).__name__))
                try:
                    error = session.record_error(type(exc).__name__)
                    result["audit_receipt_sha256"] = error["receipt_sha256"]
                except Exception:
                    result["bridge_error_audit_unavailable"] = True
            finally:
                session.restore()
        if result["status"] != "disabled":
            try:
                from alphaos.constants import Severity
                orch.journal.log_system_event(Severity.INFO, "prospective_collection",
                    "Optional HOLD10 collection disposition", result)
            except Exception:
                # A failed journal cannot durably log its own failure.
                pass

