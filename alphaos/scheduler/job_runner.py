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
from datetime import datetime, timedelta
from typing import Optional

from alphaos.constants import MarketSession, Severity
from alphaos.execution import protection_watchdog
from alphaos.scheduler import cadence, cost_guard, jobs
from alphaos.util import alerts, timeutils
from alphaos.util.ids import new_id

_JOB_FUNCS = {
    cadence.JobType.SCAN: jobs.run_scan_job,
    cadence.JobType.MONITOR: jobs.run_monitor_job,
    cadence.JobType.OUTCOMES_UPDATE: jobs.run_outcomes_job,
    cadence.JobType.DAILY_DIGEST: jobs.run_daily_digest_job,
    cadence.JobType.BENCHMARK_SPINE: jobs.run_benchmark_spine_job,
    cadence.JobType.TEXT_ARCHIVE_PULL: jobs.run_text_archive_pull_job,
    cadence.JobType.ATR_UPDATE: jobs.run_atr_update_job,
    cadence.JobType.EARNINGS_CALENDAR_PULL: jobs.run_earnings_calendar_pull_job,
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
            self._alert_job_failure(job_type, str(exc))
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

    def _alert_job_failure(self, job_type: str, error: str) -> None:
        """PR9: page on a job transitioning to 'failed' (priority=high). Never
        raises -- suspenders on top of send_alert's own belt (it never raises
        either); alerting must never compound a job failure with a crash."""
        try:
            alerts.send_alert(
                self.orch.settings,
                title=f"AlphaOS job failed: {job_type}",
                message=error,
                priority="high",
                journal=self.journal,
            )
        except Exception:  # noqa: BLE001 - see docstring
            pass

    # ------------------------------------------------------------- cadence
    def run_due_jobs(self) -> list:
        """Run every job type that is currently due, in a fixed order (scan,
        monitor, outcomes_update, daily_digest, benchmark_spine,
        text_archive_pull). Job types that are not due get a ``not_due``
        entry WITHOUT any job_runs row being inserted.

        PR9: a due job type that is currently FUSED (too many consecutive
        failures -- see ``cadence.is_fused``) is also skipped, with no
        job_runs row inserted (same "not dispatched, nothing recorded"
        contract as not_due) -- the fuse's own alert/log happens once per
        fused state via ``_handle_fuse``, not per tick.
        """
        results = []
        for job_type in (
            cadence.JobType.SCAN,
            cadence.JobType.MONITOR,
            cadence.JobType.OUTCOMES_UPDATE,
            cadence.JobType.DAILY_DIGEST,
            cadence.JobType.BENCHMARK_SPINE,
            cadence.JobType.TEXT_ARCHIVE_PULL,
            cadence.JobType.ATR_UPDATE,
            cadence.JobType.EARNINGS_CALENDAR_PULL,
        ):
            due, reason = cadence.is_due(job_type, self.orch.settings, self.orch.journal)
            if not due:
                results.append({"job_type": job_type, "status": "not_due", "reason": reason})
                continue

            fused, fuse_reason, streak = cadence.is_fused(
                job_type, self.orch.settings.scheduler_max_consecutive_failures, self.orch.journal,
            )
            if fused:
                self._handle_fuse(job_type, fuse_reason, streak)
                results.append({"job_type": job_type, "status": "fused", "reason": fuse_reason})
                continue

            results.append(self.run_job(job_type))
        return results

    def _handle_fuse(self, job_type: str, reason: str, streak: int) -> None:
        """Log + alert ONCE per fused state, not per tick (a fused job type
        would otherwise re-check every 5-minute scheduler tick forever).

        Dedupe: a fuse episode's watermark is the last 'completed' job_runs
        row for this job_type (or the epoch if none exists yet). If a
        ``scheduler_fused`` system_events row for this job_type already exists
        at/after that watermark, this exact fused state has already been
        reported -- do nothing. The watermark advances only when the job type
        actually completes again (clearing the fuse), so the very next fused
        episode automatically gets a fresh watermark and WILL re-alert.
        """
        last_completed = self.journal.one(
            "SELECT finished_at_utc FROM job_runs WHERE job_type = ? AND status = 'completed' "
            "ORDER BY finished_at_utc DESC LIMIT 1",
            (job_type,),
        )
        # A NULL finished_at_utc must fall back to the epoch sentinel too, not
        # bind SQL NULL below -- "created_at_utc >= NULL" is never true (SQL's
        # three-valued logic), which would silently defeat the dedupe (every
        # tick would think "not yet reported" and re-alert forever). No
        # current writer leaves finished_at_utc NULL on a 'completed' row, but
        # the column is nullable by schema -- fail toward correct dedupe, not
        # toward a query that can never match.
        since = (last_completed or {}).get("finished_at_utc") or "0001-01-01T00:00:00+00:00"
        message_prefix = f"scheduler_fused:{job_type}:"
        already_reported = self.journal.one(
            "SELECT 1 FROM system_events WHERE category = 'scheduler_fused' AND message LIKE ? "
            "AND created_at_utc >= ? LIMIT 1",
            (f"{message_prefix}%", since),
        )
        if already_reported:
            return

        try:
            self.journal.log_system_event(
                Severity.ERROR,
                "scheduler_fused",
                f"{message_prefix}{reason}",
                {
                    "job_type": job_type,
                    "consecutive_failures": streak,
                    "threshold": self.orch.settings.scheduler_max_consecutive_failures,
                },
            )
        except Exception:  # noqa: BLE001 - best-effort, mirrors _log_failure_best_effort
            pass

        try:
            alerts.send_alert(
                self.orch.settings,
                title=f"AlphaOS scheduler fused: {job_type}",
                message=reason,
                priority="high",
                journal=self.journal,
            )
        except Exception:  # noqa: BLE001 - suspenders: must never crash run_due_jobs
            pass

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
            cadence.JobType.BENCHMARK_SPINE,
            cadence.JobType.TEXT_ARCHIVE_PULL,
            cadence.JobType.ATR_UPDATE,
            cadence.JobType.EARNINGS_CALENDAR_PULL,
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

    # ---------------------------------------------------------- heartbeat
    def heartbeat_check(self, now: Optional[datetime] = None) -> dict:
        """Dead-man's-switch check for the ``scheduler_health`` CLI command,
        driven by its OWN separate LaunchAgent (PR9) so its failure modes are
        never shared with the scheduler tick itself.

        Outside market hours (``MarketSession.CLOSED`` -- nights/weekends)
        this always reports healthy WITHOUT checking staleness or alerting:
        there is no expectation of a fresh completed job during a period the
        scheduler isn't expected to be doing anything. During any other
        session (premarket/regular/afterhours), monitor/outcomes jobs run on
        their own interval around the clock regardless of scan windows, so a
        live scheduler should always have a recently-completed job_runs row;
        a stale/missing one pages once per invocation (the heartbeat
        LaunchAgent's own tick interval is the natural repeat -- unlike the
        scheduler fuse, there is no separate dedupe here by design).

        Never raises.
        """
        session = timeutils.market_session(now)
        if session == MarketSession.CLOSED:
            return {
                "ok": True,
                "market_hours": False,
                "detail": "market closed; heartbeat staleness not enforced",
            }

        stale_minutes = self.orch.settings.scheduler_heartbeat_stale_minutes
        last_completed = self.journal.one(
            "SELECT job_type, finished_at_utc FROM job_runs WHERE status = 'completed' "
            "ORDER BY finished_at_utc DESC LIMIT 1"
        )

        age_seconds = None
        if last_completed:
            age_seconds = timeutils.age_seconds(last_completed["finished_at_utc"], now)

        if last_completed and age_seconds is not None and age_seconds <= stale_minutes * 60:
            return {
                "ok": True,
                "market_hours": True,
                "detail": (
                    f"last completed job ({last_completed['job_type']}) "
                    f"{age_seconds / 60:.1f}m ago (<= {stale_minutes}m)"
                ),
                "last_job_type": last_completed["job_type"],
                "age_minutes": age_seconds / 60.0,
            }

        if not last_completed:
            detail = "no completed job_runs row found"
        elif age_seconds is None:
            detail = f"last completed job ({last_completed['job_type']}) has an unparseable timestamp"
        else:
            detail = (
                f"last completed job ({last_completed['job_type']}) "
                f"{age_seconds / 60:.1f}m ago (> {stale_minutes}m)"
            )

        try:
            alerts.send_alert(
                self.orch.settings,
                title="AlphaOS scheduler heartbeat stale",
                message=detail,
                priority="high",
                journal=self.journal,
            )
        except Exception:  # noqa: BLE001 - suspenders: must never crash the CLI command
            pass

        return {"ok": False, "market_hours": True, "detail": detail}
