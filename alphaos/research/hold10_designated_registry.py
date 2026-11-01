"""Fixed workspace-local inactive family route; no global authority or activation."""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Callable
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat

from alphaos.research import hold10_policy_binding as policy
from alphaos.research import hold10_prospective as v1
from alphaos.research import hold10_prospective_compact as compact

VERSION = "hold10_designated_workspace_family_v1"
DIRECTORY = "hold10-designated-family-v1"
MAX_FILE_BYTES = 131072
NAMES = {
    "owner.lock",
    "intent.json",
    "registry.sqlite",
    "registry.identity.json",
    "designation.json",
    "registry.sqlite-journal",
}


class _GuardedConnection(sqlite3.Connection):
    """Check the wrapper's ownership before the unchanged API commits."""

    guard: Callable | None = None

    def commit(self):
        if self.guard is None:
            raise ValueError("designated_commit_guard_required")
        self.guard()
        super().commit()


def _identity(path, *, directory=False):
    if path.is_symlink():
        raise ValueError("symlink_designation_path_refused")
    s = path.stat()
    if directory:
        if not stat.S_ISDIR(s.st_mode):
            raise ValueError("designation_directory_required")
    elif not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
        raise ValueError("single_link_regular_designation_file_required")
    return {"device": s.st_dev, "inode": s.st_ino}


def _workspace(value):
    raw = Path(value).absolute()
    if any(p.is_symlink() for p in [raw, *raw.parents]):
        raise ValueError("unaliased_workspace_required")
    root = raw.resolve(strict=True)
    if any(
        (ancestor / ".env").exists() and (ancestor / "alphaos").is_dir()
        for ancestor in (root, *root.parents)
    ):
        raise ValueError("configured_application_ancestor_refused")
    if (
        "sources" in root.parts
        or (root / ".env").exists()
        or not (root / "CURRENT-STATUS.md").is_file()
    ):
        raise ValueError("recognized_inactive_repair_workspace_required")
    _identity(root, directory=True)
    _identity(root / "CURRENT-STATUS.md")
    return root


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish(path, value):
    raw = v1.h._json(value).encode()
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("bounded_designation_file_required")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(raw)
        file.flush()
        os.fsync(file.fileno())
    _sync_dir(path.parent)


def _read(path):
    before = _identity(path)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("bounded_designation_file_required")
    value = json.loads(path.read_bytes())
    if before != _identity(path) or path.read_bytes() != v1.h._json(value).encode():
        raise ValueError("designation_file_changed_or_noncanonical")
    return value


@contextmanager
def _lock(root, folder, *, create=False):
    root_id, folder_id = (
        _identity(root, directory=True),
        _identity(folder, directory=True),
    )
    lock = folder / "owner.lock"
    flags = os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT | os.O_EXCL if create else 0)
    fd = os.open(lock, flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        descriptor = os.fstat(fd)

        def check():
            if (
                _identity(root, directory=True) != root_id
                or _identity(folder, directory=True) != folder_id
                or _identity(lock)
                != {"device": descriptor.st_dev, "inode": descriptor.st_ino}
            ):
                raise ValueError("designation_ownership_identity_changed")
            if set(p.name for p in folder.iterdir()) - NAMES:
                raise ValueError("unexpected_designation_directory_content")

        check()
        yield check
        check()
    finally:
        os.close(fd)


class DesignatedHold10:
    def __init__(self, workspace, *, dry_run=False):
        self.workspace, self.enabled = workspace, dry_run is True

    def _folder(self):
        root = _workspace(self.workspace)
        return root, root / DIRECTORY

    @staticmethod
    def _db(folder, guard=None):
        identity = _read(folder / "registry.identity.json")
        path = folder / "registry.sqlite"
        if _identity(path) != identity:
            raise ValueError("designated_registry_identity_changed")
        db = sqlite3.connect(
            path.as_uri() + ("?mode=ro" if guard is None else "?mode=rw"),
            uri=True,
            timeout=0,
            factory=_GuardedConnection,
        )
        if _identity(path) != identity:
            db.close()
            raise ValueError("designated_registry_identity_changed")

        def commit_guard():
            DesignatedHold10._check_db(folder, identity)
            if guard is None:
                raise ValueError("designated_commit_guard_required")
            guard()

        db.guard = commit_guard
        return db, identity

    @staticmethod
    def _check_db(folder, identity):
        if (
            _read(folder / "registry.identity.json") != identity
            or _identity(folder / "registry.sqlite") != identity
        ):
            raise ValueError("designated_registry_identity_changed")

    def register(
        self,
        contract,
        binding,
        *,
        expected_contract_sha256,
        expected_binding_sha256,
        resume=False,
        expected_designation_sha256=None,
    ):
        if not self.enabled:
            return {"status": "disabled", **v1.FLAGS}
        q = v1._contract(contract)
        v1.h._pin(q, expected_contract_sha256, "contract")
        b = policy.validate_binding(
            binding, q, expected_binding_sha256=expected_binding_sha256
        )
        root, folder = self._folder()
        created = False
        try:
            folder.mkdir(mode=0o700)
            created = True
            _sync_dir(root)
        except FileExistsError:
            _identity(folder, directory=True)
        with _lock(root, folder, create=created) as check:
            authority = folder / "designation.json"
            if authority.exists():
                if expected_designation_sha256 is None:
                    raise ValueError("existing_designation_external_pin_required")
                manifest = self._manifest(folder, expected_designation_sha256)
                if (
                    manifest["contract_sha256"] != expected_contract_sha256
                    or manifest["binding_sha256"] != expected_binding_sha256
                ):
                    raise ValueError("designated_family_conflict")
                return self._state(folder, manifest, check)
            if not created and not resume:
                raise ValueError("incomplete_designation_requires_explicit_resume")
            intent_path = folder / "intent.json"
            if created:
                check()
                _publish(
                    intent_path,
                    {
                        "version": VERSION,
                        "family_id": v1.FAMILY,
                        "contract": q,
                        "binding": b,
                        "contract_sha256": expected_contract_sha256,
                        "binding_sha256": expected_binding_sha256,
                        "created_at_utc": v1._clock().isoformat(),
                    },
                )
            intent = _read(intent_path)
            v1._exact(
                intent,
                {
                    "version",
                    "family_id",
                    "contract",
                    "binding",
                    "contract_sha256",
                    "binding_sha256",
                    "created_at_utc",
                },
                "exact_designation_intent_required",
            )
            if (
                intent.get("version") != VERSION
                or intent.get("family_id") != v1.FAMILY
                or intent.get("contract") != q
                or intent.get("binding") != b
                or intent.get("contract_sha256") != expected_contract_sha256
                or intent.get("binding_sha256") != expected_binding_sha256
            ):
                raise ValueError("immutable_designation_intent_conflict")
            if (
                not v1.c._dt(b["recorded_at_utc"])
                <= v1.c._dt(intent["created_at_utc"])
                <= v1._clock()
                < v1.c._dt(q["start_at_utc"])
            ):
                raise ValueError("future_designation_required_no_backdated_resume")
            path = folder / "registry.sqlite"
            identity_path = folder / "registry.identity.json"
            if not path.exists():
                if identity_path.exists():
                    raise ValueError("missing_designated_registry_no_recreation")
                check()
                fd = os.open(
                    path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                )
                os.fsync(fd)
                os.close(fd)
                _sync_dir(folder)
                check()
                _publish(identity_path, _identity(path))

            # A crash between exclusive DB creation and identity publication is
            # intentionally not guessed/repaired: preserve it for manual review.
            def setup_guard():
                check()
                if _read(intent_path) != intent:
                    raise ValueError("designation_intent_changed")

            db, identity = self._db(folder, setup_guard)
            try:
                check()
                compact.initialize_compact_registry(
                    db, purpose="inactive_manual_prospective_dry_run"
                )
                state = compact.CompactProspectiveHold10(db, dry_run=True).register(
                    q, expected_contract_sha256=expected_contract_sha256
                )
                check()
                self._check_db(folder, identity)
                if v1._clock() >= v1.c._dt(q["start_at_utc"]):
                    raise ValueError("future_designation_boundary_crossed")
                manifest = {
                    "version": VERSION,
                    "family_id": v1.FAMILY,
                    "registration_id": q["registration_id"],
                    "registration_sha256": state["registration"]["receipt_sha256"],
                    "contract_sha256": expected_contract_sha256,
                    "binding_sha256": expected_binding_sha256,
                    "intent_sha256": v1.sha(intent),
                    "registry_identity": identity,
                    "registry_filename": "registry.sqlite",
                    "created_at_utc": v1._clock().isoformat(),
                    "scope": "workspace_local_mediated_route_not_global_or_runtime_attestation",
                    **v1.FLAGS,
                }
                _publish(authority, manifest)
                check()
                return {
                    "designation": manifest,
                    "designation_sha256": v1.sha(manifest),
                    "state": state,
                    "policy_binding": b,
                }
            finally:
                db.close()

    @staticmethod
    def _manifest(folder, expected):
        manifest = _read(folder / "designation.json")
        v1.h._pin(manifest, expected, "designation")
        intent = _read(folder / "intent.json")
        v1._exact(
            manifest,
            {
                "version",
                "family_id",
                "registration_id",
                "registration_sha256",
                "contract_sha256",
                "binding_sha256",
                "intent_sha256",
                "registry_identity",
                "registry_filename",
                "created_at_utc",
                "scope",
                *v1.FLAGS,
            },
            "exact_designation_fields_required",
        )
        v1._exact(
            intent,
            {
                "version",
                "family_id",
                "contract",
                "binding",
                "contract_sha256",
                "binding_sha256",
                "created_at_utc",
            },
            "exact_designation_intent_required",
        )
        if (
            manifest["scope"]
            != "workspace_local_mediated_route_not_global_or_runtime_attestation"
            or intent["version"] != VERSION
            or intent["family_id"] != v1.FAMILY
            or intent["contract_sha256"] != manifest["contract_sha256"]
            or intent["binding_sha256"] != manifest["binding_sha256"]
        ):
            raise ValueError("designation_scope_or_intent_mismatch")
        if (
            manifest.get("version") != VERSION
            or manifest.get("family_id") != v1.FAMILY
            or manifest.get("intent_sha256") != v1.sha(intent)
            or manifest.get("registry_filename") != "registry.sqlite"
            or any(manifest.get(k) != val for k, val in v1.FLAGS.items())
        ):
            raise ValueError("invalid_designation_authority")
        if (
            manifest["contract_sha256"] != v1.sha(intent["contract"])
            or manifest["binding_sha256"] != v1.sha(intent["binding"])
            or manifest["registration_id"] != intent["contract"]["registration_id"]
        ):
            raise ValueError("designation_intent_binding_mismatch")
        policy.validate_binding(
            intent["binding"],
            intent["contract"],
            expected_binding_sha256=manifest["binding_sha256"],
        )
        if not (
            v1.c._dt(intent["binding"]["recorded_at_utc"])
            <= v1.c._dt(intent["created_at_utc"])
            <= v1.c._dt(manifest["created_at_utc"])
            <= v1._clock()
        ) or v1.c._dt(manifest["created_at_utc"]) >= v1.c._dt(
            intent["contract"]["start_at_utc"]
        ):
            raise ValueError("designation_clock_invalid")
        if (
            _identity(folder / "registry.sqlite") != manifest["registry_identity"]
            or _read(folder / "registry.identity.json") != manifest["registry_identity"]
        ):
            raise ValueError("designated_registry_identity_changed")
        return manifest

    def _state(self, folder, manifest, check):
        db, identity = self._db(folder)
        try:
            state = compact.CompactProspectiveHold10(db).read(
                manifest["registration_id"]
            )
            if (
                state["registration"]["receipt_sha256"]
                != manifest["registration_sha256"]
            ):
                raise ValueError("designated_registration_mismatch")
            self._check_db(folder, identity)
            check()
            return {
                "designation": manifest,
                "designation_sha256": v1.sha(manifest),
                "state": state,
                "policy_binding": _read(folder / "intent.json")["binding"],
            }
        finally:
            db.close()

    def read(self, *, expected_designation_sha256):
        root, folder = self._folder()
        with _lock(root, folder) as check:
            return self._state(
                folder, self._manifest(folder, expected_designation_sha256), check
            )

    def capture(
        self, snapshot, *, expected_designation_sha256, expected_previous_capture_sha256
    ):
        if not self.enabled:
            return {"status": "disabled", **v1.FLAGS}
        root, folder = self._folder()
        with _lock(root, folder) as check:
            manifest = self._manifest(folder, expected_designation_sha256)
            intent = _read(folder / "intent.json")
            audit = policy.audit_saved_policy(
                snapshot,
                intent["contract"],
                intent["binding"],
                expected_binding_sha256=manifest["binding_sha256"],
            )
            check()
            self._manifest(folder, expected_designation_sha256)
            if audit["contradictory_candidate_count"]:
                return {
                    "status": "capture_refused_binding_contradiction",
                    "policy_audit": audit,
                    "designation_sha256": expected_designation_sha256,
                    **v1.FLAGS,
                }

            def capture_guard():
                check()
                self._manifest(folder, expected_designation_sha256)
                snapshot.verify()

            db, identity = self._db(folder, capture_guard)
            try:
                check()
                state = compact.CompactProspectiveHold10(db, dry_run=True).capture(
                    snapshot,
                    manifest["registration_id"],
                    expected_registration_sha256=manifest["registration_sha256"],
                    expected_previous_capture_sha256=expected_previous_capture_sha256,
                )
                self._check_db(folder, identity)
                check()
                return {
                    "designation": manifest,
                    "designation_sha256": expected_designation_sha256,
                    "state": state,
                    "policy_audit": audit,
                }
            finally:
                db.close()

    def cancel(self, *, expected_designation_sha256, expected_previous_capture_sha256):
        if not self.enabled:
            return {"status": "disabled", **v1.FLAGS}
        root, folder = self._folder()
        with _lock(root, folder) as check:
            manifest = self._manifest(folder, expected_designation_sha256)

            def cancel_guard():
                check()
                self._manifest(folder, expected_designation_sha256)

            db, identity = self._db(folder, cancel_guard)
            try:
                check()
                state = compact.CompactProspectiveHold10(db, dry_run=True).cancel(
                    manifest["registration_id"],
                    expected_registration_sha256=manifest["registration_sha256"],
                    expected_previous_capture_sha256=expected_previous_capture_sha256,
                )
                self._check_db(folder, identity)
                check()
                return {
                    "designation": manifest,
                    "designation_sha256": expected_designation_sha256,
                    "state": state,
                }
            finally:
                db.close()
