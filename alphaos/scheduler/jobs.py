"""Scheduler v1.5 job functions (PR3).

Each ``run_<x>_job(orch, runner)`` is the scheduler-level wrapper around one
existing orchestrator entry point. These functions add the scheduler-specific
safety gating (kill-switch full-skip for scans, AI cost-cap full-skip for
scans) ON TOP of the orchestrator's own internal checks -- they never
reimplement or reorder what the orchestrator methods already do, and they
never call an order-submission/approval/protection-resolution function
directly.

Each job function returns a plain, JSON-serializable dict with at least
``{"status": "completed" | "skipped" | "failed", ...}`` and must NOT raise for
expected/handled conditions (kill switch on, cost cap exceeded) -- only
genuinely unexpected exceptions are allowed to propagate up to
``JobRunner.run_job``'s try/except wrapper.
"""

from __future__ import annotations

from alphaos.constants import TriggerSource
from alphaos.execution import protection_watchdog
from alphaos.scheduler import cost_guard


def run_scan_job(orch, runner) -> dict:
    """Scheduler wrapper around ``orch.run_scan_once()``.

    Adds a NEW scheduler-level gate: when the kill switch is engaged or the AI
    cost cap is exceeded, ``run_scan_once`` is not called AT ALL (full-skip,
    not a partial/AI-only degrade) -- the existing ``run_scan_once`` only logs
    a WARNING and keeps scanning; skipping the call entirely is this stage's
    job.
    """
    if orch.kill_switch.is_engaged():
        return {
            "status": "skipped",
            "reason": "kill switch engaged, scan skipped",
            "kill_switch_engaged": True,
            "cost_cap_exceeded": False,
        }

    within_budget, detail = cost_guard.check_scan_budget(orch.settings, orch.journal)
    if not within_budget:
        return {
            "status": "skipped",
            "reason": detail,
            "kill_switch_engaged": False,
            "cost_cap_exceeded": True,
        }

    scan_summary = orch.run_scan_once(trigger_source=TriggerSource.SCHEDULER.value)
    return {
        "status": "completed",
        "kill_switch_engaged": False,
        "cost_cap_exceeded": False,
        "scan_summary": scan_summary.as_dict(),
    }


def run_monitor_job(orch, runner) -> dict:
    """Scheduler wrapper around ``orch.run_monitor_once()``.

    No kill-switch or cost-cap gating: monitor/protection must keep running
    even when the kill switch is engaged (it only detects + blocks, it never
    submits/cancels/closes on its own). Never calls close_position/cancel/
    resolve_incident/acknowledge_incident.
    """
    monitor_result = orch.run_monitor_once(trigger_source=TriggerSource.SCHEDULER.value)
    blocking = protection_watchdog.has_blocking_incident(orch.journal)
    return {
        "status": "completed",
        "protection_blocking": bool(blocking is not None),
        "monitor_result": monitor_result,
    }


def run_outcomes_job(orch, runner) -> dict:
    """Scheduler wrapper around ``orch.outcomes_update()``. No gating needed --
    pure measurement, never read by any gate/eval/labeller/risk/execution path."""
    outcomes_result = orch.outcomes_update(limit=500)
    return {"status": "completed", "outcomes_result": outcomes_result}


def run_daily_digest_job(orch, runner) -> dict:
    """Scheduler wrapper around ``digest.build_daily_digest()``. PURE READ."""
    from alphaos.scheduler.digest import build_daily_digest

    digest = build_daily_digest(orch.journal, orch.settings, orch.kill_switch)
    return {"status": "completed", "digest": digest}
