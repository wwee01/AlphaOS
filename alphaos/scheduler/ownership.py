"""Process-lifetime worker ownership, shared by scheduled and manual monitors.

Locks are per canonical ledger path, nonblocking, and reentrant only in the
owning thread. Never delete a lock file: replacing its inode would split owners.
SQLite interval keys remain the durable deduplication layer after a process exits.
"""

from __future__ import annotations

import fcntl
import os
import threading
import time
from hashlib import sha256
from uuid import uuid4
from contextlib import contextmanager
from pathlib import Path

_guard = threading.Lock()
_held: dict[str, tuple[int, int, int, int | None]] = {}
_epochs: dict[str, dict] = {}


def _after_fork() -> None:
    global _guard, _held, _epochs
    for _, _, _, fd in _held.values():
        if fd is not None:
            os.close(fd)
    _held = {}
    _epochs = {}
    _guard = threading.Lock()


os.register_at_fork(after_in_child=_after_fork)


def _key(journal, role: str) -> tuple[str, bool]:
    if role not in ("monitor", "research"):
        raise ValueError("unknown worker ownership role")
    path = str(journal.db_path)
    if path in ("", ":memory:"):
        connection = getattr(journal, "conn", journal)
        connection = getattr(connection, "connection", connection)
        return f"memory:{id(connection)}:{role}", False
    if path.startswith("file:"):
        raise ValueError("worker ownership requires a filesystem ledger path")
    return str(Path(path).resolve()) + f".{role}.lock", True


@contextmanager
def worker_ownership(journal, role: str):
    key, persistent = _key(journal, role)
    owner = (os.getpid(), threading.get_ident())
    acquired = False
    with _guard:
        current = _held.get(key)
        if current is not None:
            if current[:2] == owner:
                _held[key] = (*owner, current[2] + 1, current[3])
                acquired = True
        else:
            fd = None
            try:
                if persistent:
                    fd = os.open(key, os.O_CREAT | os.O_RDWR, 0o600)
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                stat = os.fstat(fd) if fd is not None else None
                _epochs[key] = {"version": "monitor_owner_v1", "epoch": uuid4().hex,
                                "pid": owner[0], "ledger_lock_sha256": sha256(key.encode()).hexdigest(),
                                "device": stat.st_dev if stat else None,
                                "inode": stat.st_ino if stat else None}
                _held[key] = (*owner, 1, fd)
                acquired = True
            except BlockingIOError:
                if fd is not None:
                    os.close(fd)
            except Exception:
                if fd is not None:
                    os.close(fd)
                raise
    try:
        yield acquired
    finally:
        if acquired:
            with _guard:
                current = _held[key]
                if current[2] > 1:
                    _held[key] = (*owner, current[2] - 1, current[3])
                else:
                    del _held[key]
                    del _epochs[key]
                    if current[3] is not None:
                        fcntl.flock(current[3], fcntl.LOCK_UN)
                        os.close(current[3])


@contextmanager
def monitor_ownership(journal, *, timeout_seconds: float = 0.0):
    """Optional bounded entry wait; scheduled/manual monitors stay nonblocking.

    Sleep outside the registry mutex and never hold a SQLite transaction while
    waiting. Successful ownership still covers the caller's entire context.
    """
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        with worker_ownership(journal, "monitor") as acquired:
            if acquired or time.monotonic() >= deadline:
                yield acquired
                return
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))


def current_ownership(journal, role: str = "monitor") -> dict | None:
    """Proof of this thread's lock scope, stable across reentrant acquisition.

    A different epoch proves a previous compliant ownership scope ended only
    when both receipts identify the SAME locked inode. Deleted/replaced lock
    files never authorize reclaiming the old inode's owner.
    """
    key, _ = _key(journal, role)
    with _guard:
        held = _held.get(key)
        if not held or held[:2] != (os.getpid(), threading.get_ident()):
            return None
        receipt = dict(_epochs[key])
        fd = held[3]
        if fd is not None:
            actual, path = os.fstat(fd), os.stat(key, follow_symlinks=False)
            expected = (receipt["device"], receipt["inode"])
            if (actual.st_dev, actual.st_ino) != expected or (path.st_dev, path.st_ino) != expected:
                raise RuntimeError("monitor ownership lock inode changed; review required")
        return receipt
