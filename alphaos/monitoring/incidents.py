"""Separate incident state; trading SQLite and existing heartbeat state untouched.

Semantic keys survive changing ages and receipts. Recovery is recorded once;
optional ntfy delivery retries after failure, with pending saved before sending.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any

from alphaos.util import alerts


def _save(path, state):
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(state, output, sort_keys=True, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def record_transitions(report, path, settings, notify=False):
    """Explicit opt-in local sidecar. No alerts by default, no ledger writes."""
    path = Path(path).resolve()
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            source_fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            state: dict[str, Any] = {"version": 1, "active": {}, "pending": []}
        else:
            with os.fdopen(source_fd) as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 2_000_000:
                    raise ValueError("incident_state_invalid")
                state = json.load(source)
            if state.get("version") != 1 or not isinstance(state.get("active"), dict) or not isinstance(state.get("pending"), list):
                raise ValueError("incident_state_invalid")
        current = {f["key"]: f for f in report["findings"]}
        events = []
        previous = state["active"]
        rank = {"warning": 1, "unknown": 2, "error": 3}
        for key, finding in current.items():
            if key not in previous or rank.get(finding["severity"], 3) > rank.get(previous[key]["severity"], 3):
                events.append({"key": key, "transition": "opened" if key not in previous else "escalated", "at_utc": report["observed_at_utc"]})
        # A failed broker read cannot establish recovery of broker incidents.
        broker_codes = {"order_missing", "order_identity_mismatch", "order_terms_mismatch", "order_state_mismatch", "fill_quantity_mismatch", "fill_price_mismatch", "fill_timestamp_unverified", "partial_fill", "unattributed_open_order", "broker_position_invalid", "position_quantity_mismatch", "missing_protective_order", "missing_profit_order", "protective_terms_mismatch", "exit_fill_mismatch", "position_data_stale"}
        order_codes = {"order_missing", "order_identity_mismatch", "order_terms_mismatch", "order_state_mismatch", "fill_quantity_mismatch", "fill_price_mismatch", "fill_timestamp_unverified", "partial_fill", "protective_terms_mismatch", "lineage_missing", "lineage_identity_mismatch", "risk_receipt_mismatch", "entry_data_stale", "size_liquidity_warning"}
        scoped_orders = set(report["scope"]["order_ids"])
        for key, finding in previous.items():
            if key in current:
                continue
            out_of_scope = finding["code"] in order_codes and finding["entity"].split(":")[0] not in scoped_orders
            if out_of_scope or (not report.get("broker_verified") and finding["code"] in broker_codes):
                current[key] = finding
            else:
                events.append({"key": key, "transition": "recovered", "at_utc": report["observed_at_utc"]})
        pending = state["pending"] + events if notify else state["pending"]
        state.update(active=current, pending=pending, observed_at_utc=report["observed_at_utc"],
                     last_transitions=events, source_sha256=report["ledger_source_sha256"])
        if report.get("broker_verified") and report.get("completed_at_utc"):
            # Domain findings still constitute a completed observation. Read
            # failures never advance the success clock or clear old incidents.
            state["last_complete_broker_poll_at_utc"] = report["completed_at_utc"]
        _save(path, state)
        delivered = None
        if notify and pending:
            # Send only stable codes/counts, no order/account identifiers,
            # raw evidence, credentials, broker text or config values.
            delivered = bool(alerts.send_alert(settings, title="AlphaOS paper observer transition",
                message=f"{len(pending)} incident transitions; {len(current)} unresolved. " + ", ".join(sorted({e["key"].split(":")[0] + " " + e["transition"] for e in pending})), priority="high" if current else "default"))
            if delivered:
                state["pending"] = []
                _save(path, state)
        return {"events": events, "active_count": len(current), "delivered": delivered, "pending_count": len(state["pending"])}
    finally:
        os.close(fd)
