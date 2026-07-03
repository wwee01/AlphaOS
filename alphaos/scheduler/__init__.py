"""Scheduler v1.5 (PR3): cadence rules, cost guard, job runner, daily digest.

Turns the existing manual-CLI orchestrator entry points (``run_scan_once``,
``run_monitor_once``, ``outcomes_update``) into scheduled, idempotent,
kill-switch/cost-cap-aware jobs -- WITHOUT changing any of their existing
behavior beyond the ``trigger_source`` passthrough they already accept. Never
submits, approves, or auto-repairs anything on its own.
"""

from alphaos.scheduler.cadence import JobType
from alphaos.scheduler.digest import build_daily_digest
from alphaos.scheduler.job_runner import JobRunner

__all__ = ["JobType", "JobRunner", "build_daily_digest"]
