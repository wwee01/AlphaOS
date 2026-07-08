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
    """Scheduler wrapper around ``digest.build_daily_digest()``. PURE READ,
    plus (PR11) building the daily brief and sending its compact form via
    ``alerts.send_alert`` -- title is the brief's own one action item, so an
    operator sees what needs them without opening a terminal. ``send_alert``
    never raises and no-ops silently when NTFY_TOPIC is unset."""
    from alphaos.reports.daily_brief import build_daily_brief, render_compact
    from alphaos.scheduler.digest import build_daily_digest
    from alphaos.util import alerts

    digest = build_daily_digest(orch.journal, orch.settings, orch.kill_switch)
    brief = build_daily_brief(orch.journal, orch.settings, orch.kill_switch)
    alerts.send_alert(
        orch.settings,
        title=brief["one_action"],
        message=render_compact(brief),
        priority="default",
        journal=orch.journal,
    )
    return {"status": "completed", "digest": digest, "brief": brief}


def run_benchmark_spine_job(orch, runner) -> dict:
    """Scheduler wrapper around ``benchmark_capture.capture_benchmark_spine()``
    (PR9.5). No gating needed -- pure measurement, never read by any
    gate/eval/labeller/risk/execution path (same rationale as
    run_outcomes_job)."""
    from alphaos.reports.benchmark_capture import capture_benchmark_spine

    result = capture_benchmark_spine(orch.journal, orch.settings)
    return {"status": "completed", "benchmark_spine_result": result}


def run_text_archive_pull_job(orch, runner) -> dict:
    """Scheduler wrapper around ``text_archive.service``'s cik_map refresh +
    filing pull (TEXT-0). No gating needed -- collect only, never read by
    any gate/eval/labeller/risk/execution path (same rationale as
    run_outcomes_job/run_benchmark_spine_job). Zero new docs on what looks
    like a real trading day pages an alert (EDGAR is never truly quiet on a
    trading day -- silence means the fetcher is broken); a weekend never
    does (this codebase has no market-holiday table anywhere yet, so an
    actual holiday can still page -- a pre-existing, accepted limitation,
    not one unique to this job)."""
    from alphaos.text_archive.service import (
        is_probable_trading_day, pull_new_filings, refresh_cik_map,
    )
    from alphaos.util import alerts, timeutils

    if not orch.settings.text_archive_enabled:
        return {"status": "skipped", "reason": "TEXT_ARCHIVE_ENABLED is false"}

    cik_map_result = refresh_cik_map(orch.journal, orch.settings)
    pull_result = pull_new_filings(orch.journal, orch.settings)

    if (
        pull_result.get("docs_fetched") == 0
        and not pull_result.get("error")
        and is_probable_trading_day(timeutils.market_date())
    ):
        alerts.send_alert(
            orch.settings,
            title="AlphaOS text archive: zero documents fetched",
            message=(
                f"text_archive_pull fetched 0 new docs across {pull_result.get('ciks_checked', 0)} "
                "CIKs on a probable trading day -- EDGAR is never truly quiet; this usually means "
                "the fetcher is broken (contact email missing, CIK map empty, or a network/auth issue), "
                "not that nothing happened."
            ),
            priority="high",
            journal=orch.journal,
        )

    return {"status": "completed", "cik_map_result": cik_map_result, "pull_result": pull_result}
