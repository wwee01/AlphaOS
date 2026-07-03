"""Scheduler v1.5 job runner (PR3).

``JobRunner`` is the outer loop that decides WHEN to run a job (via
``cadence.is_due``), enforces idempotency (via a ``job_runs`` lock row so the
same job/window/interval never runs twice), dispatches to the right function
in ``jobs.py``, and records the outcome. It never runs job logic itself --
that always lives in ``jobs.py`` -- and it never bypasses a job function's
own kill-switch/cost-cap handling.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import timedelta
from typing import Optional

from alphaos.constants import Severity
from alphaos.execution import protection_watchdog
from alphaos.scheduler import cadence, cost_guard, jobs
from alphaos.util import timeutils
from alphaos.util.ids import new_id

_JOB_FUNCS = {
    cadence.JobType.SCAN: jobs.run_scan_job,
    cadence.JobType.MONITOR: jobs.run_monitor_job,
    cadence.JobType.OUTCOMES_UPDATE: jobs.run_outcomes_job,
    cadence.JobType.DAILY_DIGEST: jobs.run_daily_digest_job,
}


class JobRunner:
    """Drives scheduled job execution against one ``Orchestrator`` instance."""

    def __init__(self, orch):
        self.orch = orch
        self.journal = orch.journal

    # -------------------------------------------------------------- locking
    def acquire(self, job_type: str, lock_key: str) -> bool:
        """Claim ``lock_key`` for ``job_type`` by inserting a ``started`` row.

        Returns False (inserting nothing) if a ``started`` or ``completed`` row
        already exists for this exact (job_type, lock_key) -- the idempotency
        guard that keeps the same window/interval/day from running twice.

        This check-then-insert has a narrow race window between two
        concurrent callers (e.g. an overlapping manual CLI run and a
        cron/LaunchAgent-driven one) that both pass the SELECT before either
        INSERT commits. ``idx_jobruns_lock_key_active`` (a partial UNIQUE
        index on job_runs(lock_key) WHERE status IN ('started','completed'))
        backstops this at the DB level -- the loser's INSERT raises
        sqlite3.IntegrityError, which we treat exactly like "already locked"
        rather than an unexpected error.
        """
        existing = self.journal.one(
            "SELECT 1 FROM job_runs WHERE lock_key = ? AND status IN ('started', 'completed')",
            (lock_key,),
        )
        if existing:
            return False

        st = timeutils.stamp()
        try:
            self.journal.insert(
                "job_runs",
                {
                    "job_run_id": new_id("jobrun"),
                    "job_type": job_type,
                    "trigger_source": "scheduler",
                    "lock_key": lock_key,
                    "started_at_utc": st.utc,
                    "started_at_sgt": st.local_sgt,
                    "status": "started",
                },
            )
        except sqlite3.IntegrityError:
            # Another process won the race for this lock_key between our
            # SELECT and INSERT -- the partial unique index caught it.
            return False
        return True

    # ---------------------------------------------------------------- run
    def run_job(self, job_type: str, lock_key: Optional[str] = None) -> dict:
        """Run ``job_type`` now, enforcing the idempotency lock. Never raises --
        genuinely unexpected exceptions from the dispatched job are caught here
        and recorded as a 'failed' job_runs row instead of propagating."""
        if job_type not in _JOB_FUNCS:
            # Validate before acquire() -- an unknown job_type must never claim
            # a lock row it can then never resolve to completed/failed.
            return {"job_type": job_type, "status": "failed", "error": f"unknown job_type: {job_type!r}"}

        if lock_key is None:
            lock_key = cadence.default_lock_key(job_type, self.orch.settings)

        try:
            acquired = self.acquire(job_type, lock_key)
        except Exception as exc:  # noqa: BLE001 - claiming the lock must not crash the caller either
            self._log_failure_best_effort(job_type, lock_key, f"acquire failed for {job_type}: {exc}")
            return {"job_type": job_type, "status": "failed", "error": f"acquire failed: {exc}", "lock_key": lock_key}

        if not acquired:
            return {"job_type": job_type, "status": "skipped", "reason": "duplicate_lock", "lock_key": lock_key}

        job_func = _JOB_FUNCS[job_type]
        started = time.monotonic()
        try:
            result = job_func(self.orch, self)
        except Exception as exc:  # noqa: BLE001 - never let a job crash the scheduler loop
            duration_ms = int((time.monotonic() - started) * 1000)
            done = timeutils.stamp()
            self.journal.conn.execute(
                "UPDATE job_runs SET status = ?, error = ?, finished_at_utc = ?, finished_at_sgt = ?, "
                "duration_ms = ? WHERE lock_key = ? AND status = 'started'",
                ("failed", str(exc), done.utc, done.local_sgt, duration_ms, lock_key),
            )
            self.journal.conn.commit()
            self._log_failure_best_effort(job_type, lock_key, f"{job_type} job failed: {exc}")
            return {"job_type": job_type, "status": "failed", "error": str(exc), "lock_key": lock_key}

        duration_ms = int((time.monotonic() - started) * 1000)
        done = timeutils.stamp()
        self.journal.conn.execute(
            "UPDATE job_runs SET status = ?, kill_switch_engaged = ?, protection_blocking = ?, "
            "cost_cap_exceeded = ?, result_summary_json = ?, finished_at_utc = ?, finished_at_sgt = ?, "
            "duration_ms = ? WHERE lock_key = ? AND status = 'started'",
            (
                result.get("status"),
                result.get("kill_switch_engaged"),
                result.get("protection_blocking"),
                result.get("cost_cap_exceeded"),
                json.dumps(result, default=str),
                done.utc,
                done.local_sgt,
                duration_ms,
                lock_key,
            ),
        )
        self.journal.conn.commit()
        return {"job_type": job_type, "lock_key": lock_key, **result}

    def _log_failure_best_effort(self, job_type: str, lock_key: str, message: str) -> None:
        """Best-effort audit log -- mirrors JournalStore._migrate()'s own
        ``try/except sqlite3.Error: pass`` pattern for its own log_system_event
        call. A logging failure (e.g. the DB is momentarily locked by a
        concurrent writer) must never crash run_job on top of a failure that
        has already been durably recorded in job_runs -- audit logging is
        best-effort and must never gate/mask the actual failure record."""
        try:
            self.journal.log_system_event(
                Severity.ERROR, "scheduler", message, {"job_type": job_type, "lock_key": lock_key},
            )
        except Exception:  # noqa: BLE001 - best-effort, see docstring
            pass

    # ------------------------------------------------------------- cadence
    def run_due_jobs(self) -> list:
        """Run every job type that is currently due, in a fixed order (scan,
        monitor, outcomes_update, daily_digest). Job types that are not due get
        a ``not_due`` entry WITHOUT any job_runs row being inserted."""
        results = []
        for job_type in (
            cadence.JobType.SCAN,
            cadence.JobType.MONITOR,
            cadence.JobType.OUTCOMES_UPDATE,
            cadence.JobType.DAILY_DIGEST,
        ):
            due, reason = cadence.is_due(job_type, self.orch.settings, self.orch.journal)
            if due:
                results.append(self.run_job(job_type))
            else:
                results.append({"job_type": job_type, "status": "not_due", "reason": reason})
        return results

    # ------------------------------------------------------------- status
    def status_report(self, recent_limit: int = 10) -> dict:
        """Read-only operational status for the ``scheduler_status`` CLI command.

        Recent job_runs history per job type, any stale 'started' rows, current
        kill-switch/protection-blocking state, and AI cost-cap usage. Does not
        duplicate every digest field -- the daily digest (stage 3) is the full
        daily summary; this is for a quick operational check.
        """
        recent_by_job_type: dict[str, list] = {}
        for job_type in (
            cadence.JobType.SCAN,
            cadence.JobType.MONITOR,
            cadence.JobType.OUTCOMES_UPDATE,
            cadence.JobType.DAILY_DIGEST,
        ):
            recent_by_job_type[job_type] = self.journal.query(
                "SELECT * FROM job_runs WHERE job_type = ? ORDER BY id DESC LIMIT ?",
                (job_type, recent_limit),
            )

        stale_before = timeutils.to_iso(
            timeutils.now_utc() - timedelta(minutes=int(self.orch.settings.scheduler_stale_job_minutes))
        )
        stale_running_jobs = self.journal.query(
            "SELECT * FROM job_runs WHERE status = 'started' AND started_at_utc <= ? ORDER BY id DESC",
            (stale_before,),
        )

        return {
            "recent_by_job_type": recent_by_job_type,
            "stale_running_jobs": stale_running_jobs,
            "kill_switch_engaged": self.orch.kill_switch.is_engaged(),
            "kill_switch_reason": self.orch.kill_switch.reason(),
            "protection_status": protection_watchdog.status_report(self.orch.journal),
            "cost_usage": {
                "calls_in_last_30_days": cost_guard.calls_in_last_30_days(self.orch.journal),
                "cap": self.orch.settings.scheduler_ai_cost_cap_calls_per_30d,
            },
        }
