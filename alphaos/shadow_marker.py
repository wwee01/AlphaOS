"""Serialized, bounded shadow-marker IO; separate from the trading kill switch."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

MAX_BYTES = 16_384
_guard = threading.Lock()
_condition = threading.Condition(_guard)
_held: dict[str, tuple[int, int, int | None]] = {}
_poisoned: set[str] = set()


class MarkerError(RuntimeError):
    pass


def canonical(path) -> Path:
    p = Path(path).absolute()
    return p.parent.resolve() / p.name


def pending_path(path) -> Path:
    return Path(str(canonical(path)) + ".recovery-pending")


def fingerprint(path) -> tuple[dict | None, bytes | None]:
    p = canonical(path)
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        raise MarkerError("marker cannot be safely opened") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_BYTES:
            raise MarkerError("marker must be a bounded regular unlinked-to-other-names file")
        content = os.read(fd, MAX_BYTES + 1)
        after = os.fstat(fd)
        current = os.stat(p, follow_symlinks=False)
        def identity(s):
            return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if len(content) != before.st_size or identity(before) != identity(after) or identity(after) != identity(current):
            raise MarkerError("marker changed during read")
        return {"path": str(p), "device": after.st_dev, "inode": after.st_ino,
                "size": after.st_size, "mtime_ns": after.st_mtime_ns, "ctime_ns": after.st_ctime_ns,
                "sha256": hashlib.sha256(content).hexdigest()}, content
    finally:
        os.close(fd)


def sync_parent(path) -> None:
    fd = os.open(canonical(path).parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _after_fork() -> None:
    global _guard, _condition, _held, _poisoned
    for _, _, fd in _held.values():
        if fd is not None:
            os.close(fd)
    _guard = threading.Lock()
    _condition = threading.Condition(_guard)
    _held = {}
    _poisoned = set()


os.register_at_fork(after_in_child=_after_fork)


@contextmanager
def locked(path, *, blocking=False):
    p = canonical(path)
    key = str(p)
    with _condition:
        while key in _held:
            if _held[key][:2] == (os.getpid(), threading.get_ident()):
                if blocking:
                    _poisoned.add(key)
                raise MarkerError("nested shadow marker operation refused")
            if not blocking:
                raise MarkerError("shadow marker operation already active in this process")
            _condition.wait()
        _held[key] = (os.getpid(), threading.get_ident(), None)
    fd = None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        lock = Path(key + ".lock")
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with _guard:
            _held[key] = (os.getpid(), threading.get_ident(), fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise MarkerError("invalid marker lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise MarkerError("shadow marker writer busy") from exc
        current = os.stat(lock, follow_symlinks=False)
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise MarkerError("shadow marker lock identity changed")
        yield
        current = os.stat(lock, follow_symlinks=False)
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise MarkerError("shadow marker lock identity changed")
    finally:
        if fd is not None:
            os.close(fd)
        with _condition:
            _held.pop(key, None)
            _poisoned.discard(key)
            _condition.notify_all()


def require_locked(path) -> None:
    with _guard:
        held = _held.get(str(canonical(path)))
        if str(canonical(path)) in _poisoned:
            raise MarkerError("new suspension attempted during recovery; scope aborted")
        if held is None or held[:2] != (os.getpid(), threading.get_ident()):
            raise MarkerError("serialized shadow marker scope required")
        if held[2] is None:
            raise MarkerError("marker lock is not acquired")
        owned = os.fstat(held[2])
        current = os.stat(str(canonical(path)) + ".lock", follow_symlinks=False)
        if (owned.st_dev, owned.st_ino) != (current.st_dev, current.st_ino):
            raise MarkerError("shadow marker lock identity changed")


def write_exclusive(path, data: bytes) -> None:
    if len(data) > MAX_BYTES:
        raise MarkerError("marker payload too large")
    fd = os.open(canonical(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    sync_parent(path)


def engage(path, reason: str) -> None:
    if not isinstance(reason, str) or len(reason.encode()) > 8192:
        raise MarkerError("invalid suspension reason")
    p = canonical(path)
    with locked(p, blocking=True):
        # Refuse unsafe old paths; a valid existing marker may be replaced.
        fingerprint(p)
        temp = p.with_name(p.name + "." + uuid4().hex + ".tmp")
        body = json.dumps({"version": "shadow_suspend_marker_v1", "generation": uuid4().hex,
                           "reason": reason}, sort_keys=True).encode()
        write_exclusive(temp, body)
        try:
            require_locked(p)
            os.replace(temp, p)
            sync_parent(p)
        finally:
            if temp.exists():
                temp.unlink()


def remove_matching(path, expected: dict, *, marker_path) -> None:
    require_locked(marker_path)
    if canonical(path) not in (canonical(marker_path), pending_path(marker_path)):
        raise MarkerError("foreign removal target")
    actual, raw = fingerprint(path)
    if actual != expected:
        raise MarkerError("marker identity changed; refusing removal")
    require_locked(marker_path)
    os.unlink(canonical(path))
    try:
        sync_parent(path)
    except OSError:
        if canonical(path) == pending_path(marker_path) and raw is not None:
            # A failed final directory sync must not be reported as clean release.
            # Restore only an absent guard; never replace a concurrent/new file.
            # Persistent storage failure can still defeat durability, so propagate.
            try:
                write_exclusive(path, raw)
            except FileExistsError:
                pass
        raise


def is_engaged(path) -> bool:
    # Unreadable, dangling and invalid paths remain blocked.
    for target in (canonical(path), pending_path(path)):
        try:
            os.lstat(target)
            return True
        except FileNotFoundError:
            continue
        except OSError:
            return True
    return False


def reason(path) -> str | None:
    if os.path.lexists(pending_path(path)):
        return "shadow recovery pending; explicit review/resume required"
    try:
        identity, raw = fingerprint(path)
        if identity is None:
            return None
        text = (raw or b"").decode("utf-8").strip()
        try:
            value = json.loads(text)
        except ValueError:
            return text or "engaged"
        if isinstance(value, dict) and value.get("version") == "shadow_suspend_marker_v1":
            return value.get("reason") if isinstance(value.get("reason"), str) else "engaged"
        return text or "engaged"
    except (OSError, ValueError, MarkerError):
        return "engaged; suspension marker unreadable"
