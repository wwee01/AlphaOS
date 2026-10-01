"""Recover only ended, durably identified monitor ownership scopes.

No timestamp/age grants recovery. The OS lock and matching inode identify the
exclusion domain; a fresh epoch proves the prior scope released that lock.
Unknown legacy rows remain blocked. Recovery marks interruption, not success.
"""
from __future__ import annotations

import json
import re

from alphaos.execution.ledger_ops import atomic
from alphaos.scheduler.ownership import current_ownership
from alphaos.util import timeutils

CATEGORY = "monitor_ownership_v1"
_TABLES = {"job_runs": "job_run_id", "scheduler_runs": "scheduler_run_id"}


def _binding(table, row):
    fields: tuple[str, ...] = ("id", _TABLES[table], "started_at_utc")
    fields += ("job_type", "lock_key") if table == "job_runs" else ("run_type", "trigger_source")
    return {key: row[key] for key in fields}


def record_started(journal, table, row, *, parent_job_id=None):
    """Must share the transaction that inserts the started row."""
    owner = current_ownership(journal)
    if not journal.conn.in_transaction or owner is None:
        raise RuntimeError("monitor started receipt requires transaction and ownership")
    journal.log_system_event("INFO", CATEGORY, f"{table}:{row[_TABLES[table]]}", {
        "version": CATEGORY, "table": table, "row": _binding(table, row),
        "owner": owner, "parent_job_id": parent_job_id,
    })


def _receipt(journal, table, row):
    records = journal.query("SELECT detail_json FROM system_events WHERE category=? AND message=?",
                            (CATEGORY, f"{table}:{row[_TABLES[table]]}"))
    if len(records) != 1:
        raise ValueError("monitor ownership receipt missing or ambiguous; review required")
    value = json.loads(records[0]["detail_json"])
    if (not isinstance(value, dict) or value.get("version") != CATEGORY
            or value.get("table") != table or value.get("row") != _binding(table, row)):
        raise ValueError("monitor ownership receipt differs from started row")
    owner = value.get("owner")
    if (not isinstance(owner, dict) or owner.get("version") != "monitor_owner_v1"
            or not isinstance(owner.get("epoch"), str) or not re.fullmatch(r"[0-9a-f]{32}", owner["epoch"])
            or type(owner.get("pid")) is not int or owner["pid"] <= 0):
        raise ValueError("invalid monitor ownership proof")
    return value


def recover_interrupted_monitor(journal, *, allow_current_job=False):
    """Atomically fail all verifiably abandoned job/pass rows, or change none.

    Reentrant callbacks have the SAME epoch and cannot reclaim active work.
    The direct monitor pass may belong to its currently started JobRunner row;
    that one row is retained and returned for exact parent linkage.
    """
    owner = current_ownership(journal)
    if owner is None:
        raise RuntimeError("monitor recovery requires exclusive ownership")
    with atomic(journal) as tx:
        rows = [("job_runs", row) for row in tx.query(
            "SELECT * FROM job_runs WHERE job_type='monitor' AND status='started' ORDER BY id")]
        rows += [("scheduler_runs", row) for row in tx.query(
            "SELECT * FROM scheduler_runs WHERE run_type='monitor' AND status='started' ORDER BY id")]
        receipts = {(table, row[_TABLES[table]]): _receipt(tx, table, row) for table, row in rows}
        interrupted = []
        parent = None
        for table, row in rows:
            receipt = receipts[(table, row[_TABLES[table]])]
            previous = receipt["owner"]
            if any(previous.get(key) != owner.get(key) for key in ("ledger_lock_sha256", "device", "inode")):
                raise ValueError("monitor ownership lock identity changed; review required")
            if previous["epoch"] == owner["epoch"]:
                if allow_current_job and table == "job_runs" and parent is None:
                    parent = row["job_run_id"]
                    continue
                raise ValueError("monitor ownership is still active; review required")
            if table == "scheduler_runs" and receipt.get("parent_job_id"):
                job = tx.one("SELECT * FROM job_runs WHERE job_run_id=?", (receipt["parent_job_id"],))
                if not job or job.get("job_type") != "monitor" or _receipt(tx, "job_runs", job)["owner"] != previous:
                    raise ValueError("monitor pass parent ownership cannot be verified")
            interrupted.append((table, row, receipt))
        done = timeutils.stamp()
        for table, row, receipt in interrupted:
            # Compare every column read under the same write lock. No other
            # writer can slip a completion or different owner into this claim.
            if tx.one(f"SELECT * FROM {table} WHERE id=?", (row["id"],)) != row:
                raise ValueError("monitor row changed during recovery")
            error = "interrupted monitor recovered under new exclusive ownership; fresh reconciliation required"
            if table == "job_runs":
                cursor = tx.conn.execute("UPDATE job_runs SET status='failed', error=?, finished_at_utc=?, "
                                         "finished_at_sgt=? WHERE id=? AND status='started'",
                                         (error, done.utc, done.local_sgt, row["id"]))
            else:
                cursor = tx.conn.execute("UPDATE scheduler_runs SET status='failed', error_count=1, error_summary=?, "
                                         "completed_at_utc=?, completed_at_sgt=? WHERE id=? AND status='started'",
                                         (error, done.utc, done.local_sgt, row["id"]))
            if cursor.rowcount != 1:
                raise ValueError("monitor recovery claim lost")
            tx.log_system_event("ERROR", "monitor_interrupted_recovery", error, {
                "prior_receipt": receipt, "recovering_owner": owner,
            })
        # Check the path again after the database work, before committing.
        if current_ownership(journal) != owner:
            raise ValueError("monitor ownership changed during recovery")
    return parent
