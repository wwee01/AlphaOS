"""Explicit launchd roles: isolated execution monitor, research, and health.

Health/preflight open the ledger read-only and never construct a broker.
The monitor trigger stays at five minutes; existing fifteen-minute cadence,
fuses, kill-switch policy, and broker reconciliation remain authoritative.
"""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace

from alphaos.config.settings import load_settings
from alphaos.journal.journal_store import JournalStore
from alphaos.scheduler.job_runner import JobRunner
from alphaos.scheduler.monitor_deadman import ping_monitor_success
from alphaos.scheduler.monitor_health import monitor_health
from alphaos.scheduler.monitor_notifications import notify_monitor_transition
from alphaos.scheduler.ownership import worker_ownership


def _job_fault(result: dict) -> bool:
    # A healthy monitor currently supervising positions defers research entries.
    # Keep that refusal in the result, but do not report it as a service failure.
    if (result.get("job_type") == "scan" and result.get("status") == "blocked"
            and result.get("reason") == "monitor_not_ready"
            and result.get("monitor_health", {}).get("ok") is True):
        return False
    return result.get("status") in ("failed", "fused", "blocked")


def run_role(role: str, settings) -> tuple[dict, int]:
    if role in ("health", "readiness", "preflight"):
        journal = JournalStore(settings.db_path, read_only=True)
        try:
            health = monitor_health(journal, settings)
            if role == "preflight":
                active = journal.query(
                    "SELECT job_type, started_at_utc FROM job_runs WHERE status = 'started' ORDER BY id"
                )
                passes = journal.query(
                    "SELECT scheduler_run_id, started_at_utc FROM scheduler_runs "
                    "WHERE run_type = 'monitor' AND status = 'started' ORDER BY id"
                )
                return {"role": role, "ready": not active and not passes, "active_jobs": active,
                        "active_monitor_passes": passes, "monitor_health": health}, 0 if not active and not passes else 1
            if role == "health":
                health["notification"] = notify_monitor_transition(journal, settings, health)
            ready = health["ok"] and not health.get("notification", {}).get("state_error")
            if role == "readiness":
                ready = health.get("entry_ready", False) and health.get("last_success_role") == "monitor"
            return {"role": role, "monitor_health": health, "ready": ready}, 0 if ready else 1
        finally:
            journal.close()

    if role not in ("monitor", "research"):
        raise ValueError("unknown worker role")
    # Claim before constructor/startup (including migrations). The monitor
    # runner and Orchestrator share this reentrant lock with manual callers.
    with worker_ownership(SimpleNamespace(db_path=settings.db_path), role) as acquired:
        if not acquired:
            return {"role": role, "status": "skipped", "reason": "worker_owner_busy"}, 0
        from alphaos.orchestrator import Orchestrator

        orch = Orchestrator(settings=settings)
        try:
            results = JobRunner(orch, role=role).run_due_jobs()
            health = monitor_health(orch.journal, settings)
            failed = any(_job_fault(r) for r in results)
            result = {"role": role, "results": results, "monitor_health": health,
                      "entry_ready": health.get("entry_ready", False)}
            if role == "monitor":
                result["deadman"] = ping_monitor_success(settings, role=role, results=results, health=health)
            if role == "research":
                try:
                    from alphaos.research.swing_audit_worker import maybe_run
                    audit = maybe_run(orch)
                except Exception as exc:
                    audit = {"status": "blocked", "error_type": type(exc).__name__}
                if audit is not None:
                    result["swing_availability_audit"] = audit
            return result, int(failed or not health["ok"])
        finally:
            orch.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("monitor", "research", "health", "readiness", "preflight"), required=True)
    args = parser.parse_args(argv)
    try:
        result, code = run_role(args.role, load_settings())
    except Exception as exc:
        # Configuration or SDK exceptions may contain account/provider text.
        # Signal failure clearly without printing credentials or request data.
        result, code = {"role": args.role, "status": "failed", "error_type": type(exc).__name__}, 2
    print(json.dumps(result, default=str))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
