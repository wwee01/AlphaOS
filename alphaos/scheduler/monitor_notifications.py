"""Transition-only monitor alerts, separate from the read-only trading ledger.

Private per-ledger JSON plus an OS file lock serialize both heartbeat callers.
Only semantic health changes matter; ages, timestamps and per-pass row IDs do
not create new incidents. Failed delivery remains pending for a later retry.
"""
from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
import threading
import weakref
from pathlib import Path

from alphaos.util import alerts, timeutils

_VERSION = 1
_MEMORY_STATES: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_MEMORY_LOCK = threading.Lock()
_LEVELS = {
    "monitor_missing": 1, "monitor_stale": 1,
    "monitor_failed": 2, "monitor_stalled": 2,
    "invalid_monitor_timestamp": 3, "ownership_ambiguous": 3,
    "execution_incident": 3, "health_unavailable": 3,
    "monitor_fused": 4, "protection_blocking": 4,
}
_MEANINGFUL_NEW = {"ownership_ambiguous", "execution_incident", "health_unavailable",
                   "invalid_monitor_timestamp", "monitor_fused", "protection_blocking"}


def _signature(health: dict) -> dict:
    codes = sorted(set(health.get("issue_codes") or ["monitor_unhealthy"]))
    if health.get("fused") and "monitor_fused" not in codes:
        codes.append("monitor_fused")
    if health.get("protection_blocking") and "protection_blocking" not in codes:
        codes.append("protection_blocking")
    return {"level": max((_LEVELS.get(c, 1) for c in codes), default=1),
            "fused": bool(health.get("fused")), "issues": sorted(codes),
            "protection": sorted(set(health.get("protection_issues") or []))}


def _initial() -> dict:
    return {"version": _VERSION, "active": False, "pending": False, "observed": None}


def _validate(value) -> dict:
    if (not isinstance(value, dict) or value.get("version") != _VERSION
            or type(value.get("active")) is not bool or type(value.get("pending")) is not bool):
        raise ValueError("invalid notification metadata")
    observed = value.get("observed")
    if value["active"]:
        if (not isinstance(observed, dict) or type(observed.get("level")) is not int
                or not 1 <= observed["level"] <= 4 or type(observed.get("fused")) is not bool):
            raise ValueError("invalid notification signature")
        for key in ("issues", "protection"):
            values = observed.get(key)
            if (not isinstance(values, list) or len(values) > 100
                    or any(not isinstance(v, str) or len(v) > 256 for v in values)):
                raise ValueError("invalid notification signature members")
    elif observed is not None or value["pending"]:
        raise ValueError("invalid recovered notification state")
    return value


def _read(path: Path) -> dict:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return _initial()
    with os.fdopen(fd, "r") as src:
        info = os.fstat(src.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise ValueError("invalid notification state file")
        return _validate(json.load(src))


def _write(path: Path, state: dict) -> None:
    _validate(state)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as dest:
            json.dump(state, dest, sort_keys=True)
            dest.write("\n")
            dest.flush()
            os.fsync(dest.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _transition(state: dict, health: dict, settings, save, now=None) -> dict:
    stamp = timeutils.to_iso(now or timeutils.now_utc())
    if health.get("ok") is True:
        if state["active"]:
            save({**_initial(), "recovered_at_utc": stamp})
            return {"status": "recovered", "delivery_pending": False}
        return {"status": "healthy", "delivery_pending": False}

    current = _signature(health)
    previous = state["observed"] or {}
    initial = not state["active"]
    escalation = bool(previous) and (
        current["level"] > previous["level"]
        or (current["fused"] and not previous["fused"])
        or bool(set(current["protection"]) - set(previous["protection"]))
        or bool((set(current["issues"]) - set(previous["issues"])) & _MEANINGFUL_NEW)
    )
    notify = initial or state["pending"] or escalation
    next_state = {**state, "active": True, "observed": current,
                  "pending": bool(notify), "updated_at_utc": stamp}
    if not notify:
        if current != previous:
            save(next_state)
        return {"status": "unchanged", "delivery_pending": False}

    # Persist pending BEFORE the external send. A crash/failed send cannot be
    # mistaken for delivery. As with any nontransactional external service,
    # a crash after delivery but before the final save may cause one retry.
    next_state["last_attempt_at_utc"] = stamp
    save(next_state)
    try:
        delivered = bool(alerts.send_alert(
            settings, title="AlphaOS execution monitor unhealthy",
            message=health.get("detail") or "Execution monitor requires attention.", priority="high",
        ))
    except Exception:
        delivered = False
    next_state.update(pending=not delivered, last_delivery_ok=delivered)
    if delivered:
        next_state["last_delivered_at_utc"] = stamp
    try:
        save(next_state)
    except Exception as exc:
        return {"status": "state_write_failed", "delivered": delivered,
                "delivery_pending": True, "state_error": type(exc).__name__}
    return {"status": "sent" if delivered else "delivery_failed", "delivered": delivered,
            "delivery_pending": not delivered,
            "reason": "initial" if initial else "escalation" if escalation else "retry"}


def notify_monitor_transition(journal, settings, health: dict, now=None) -> dict:
    """Never writes SQLite. Return notification delivery/state failures explicitly."""
    try:
        db_path = str(journal.db_path)
        if db_path in ("", ":memory:"):
            # Isolated in-memory tests have no adjacent persistent directory.
            with _MEMORY_LOCK:
                state = _MEMORY_STATES.get(journal, _initial())
                return _transition(state, health, settings,
                                   lambda value: _MEMORY_STATES.__setitem__(journal, value), now)
        if db_path.startswith("file:"):
            raise ValueError("notification state requires a filesystem ledger path")
        state_path = Path(str(Path(db_path).resolve()) + ".monitor-notifications.json")
        lock_path = Path(str(state_path) + ".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"status": "notification_owner_busy", "delivery_pending": None}
            warning = None
            try:
                state = _read(state_path)
            except (ValueError, UnicodeError):
                # Unknown prior delivery is never treated as acknowledged.
                # Rebuild only operational metadata and disclose the reset.
                state = _initial()
                warning = "invalid_notification_state_reset"
                _write(state_path, state)
            result = _transition(state, health, settings, lambda value: _write(state_path, value), now)
            if warning:
                result["state_warning"] = warning
            return result
        finally:
            # Closing releases only this process's notification lock.
            os.close(fd)
    except Exception as exc:
        return {"status": "notification_state_unavailable", "delivery_pending": True,
                "state_error": type(exc).__name__}
