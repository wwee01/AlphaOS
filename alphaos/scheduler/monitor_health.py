"""Read-only execution-monitor readiness; research successes never substitute.

Monitoring is scheduled around the clock, including weekends. A closed market
does not excuse a missing monitor when protection/reconciliation remain expected.
No broker, alert, ownership acquisition, fuse reset or ledger writes occur here.
"""

from __future__ import annotations

import json

from alphaos.constants import MarketSession
from alphaos.execution import protection_watchdog
from alphaos.util import timeutils

TRIGGER_MINUTES = 5


def monitor_health(journal, settings, now=None) -> dict:
    result: dict = {"ok": False, "job_type": "monitor", "last_job_type": "monitor",
              "expected": "24/7; existing monitor cadence including weekends",
              "market_hours": timeutils.market_session(now) != MarketSession.CLOSED}
    try:
        interval = max(1, int(settings.scheduler_monitor_interval_minutes))
        stale_minutes = min(int(settings.scheduler_heartbeat_stale_minutes),
                            max(2 * interval, interval + 2 * TRIGGER_MINUTES))
        result["stale_after_minutes"] = stale_minutes
        rows = journal.query(
            "SELECT id, status, started_at_utc, finished_at_utc, result_summary_json "
            "FROM job_runs WHERE job_type = 'monitor' AND status != 'started' "
            "ORDER BY id DESC LIMIT ?", (max(1, settings.scheduler_max_consecutive_failures),),
        )
        streak = 0
        for row in rows:
            if row["status"] != "failed":
                break
            streak += 1
        fused = streak >= settings.scheduler_max_consecutive_failures
        result.update(fused=fused, consecutive_failures=streak)
        last = journal.one(
            "SELECT id, finished_at_utc, result_summary_json FROM job_runs WHERE job_type = 'monitor' "
            "AND status = 'completed' ORDER BY id DESC LIMIT 1"
        )
        result["last_success_at_utc"] = (last or {}).get("finished_at_utc")
        result["last_success_role"] = json.loads((last or {}).get("result_summary_json") or "{}").get("worker_role")
        age = timeutils.age_seconds(result["last_success_at_utc"], now) if last else None
        result["age_minutes"] = age / 60 if age is not None else None
        running = journal.query(
            "SELECT id, started_at_utc FROM job_runs WHERE job_type = 'monitor' "
            "AND status = 'started' ORDER BY id DESC"
        )
        result["running_count"] = len(running)
        # Manual monitor calls have scheduler_runs but no job_runs wrapper.
        # Every unfinished pass remains uncertain until receipt-based recovery
        # explicitly resolves it. A later success cannot supersede missing proof.
        manual_running = []
        for row in journal.query(
            "SELECT started_at_utc FROM scheduler_runs WHERE run_type = 'monitor' AND status = 'started'"
        ):
            pass_age = timeutils.age_seconds(row["started_at_utc"], now)
            manual_running.append((row, pass_age))
        result["unfinished_monitor_passes"] = len(manual_running)
        problems = []
        issue_codes = set()
        def problem(code: str, detail: str) -> None:
            issue_codes.add(code)
            problems.append(detail)
        if len(manual_running) > 1:
            problem("ownership_ambiguous", "multiple unfinished monitor passes; ownership requires review")
        if any(a is None or a < 0 or a > stale_minutes * 60 for _, a in manual_running):
            problem("monitor_stalled", "unfinished monitor pass stale or invalid; explicit recovery required")
        if fused:
            problem("monitor_fused", "monitor fused; explicit reviewed recovery required")
        elif rows and rows[0]["status"] == "failed":
            problem("monitor_failed", "latest monitor attempt failed")
        if len(running) > 1:
            problem("ownership_ambiguous", "multiple started monitor rows; ownership requires review")
        for row in running:
            run_age = timeutils.age_seconds(row["started_at_utc"], now)
            if run_age is None or run_age < 0 or run_age > stale_minutes * 60:
                problem("monitor_stalled", "monitor in-flight state stale or invalid; explicit recovery required")
        if not last:
            problem("monitor_missing", "no completed job_runs row found for monitor")
        elif age is None:
            problem("invalid_monitor_timestamp", "last completed monitor has an unparseable timestamp")
        elif age < 0:
            problem("invalid_monitor_timestamp", "last completed monitor timestamp is in the future")
        elif age > stale_minutes * 60:
            problem("monitor_stale", f"last completed monitor {age / 60:.1f}m ago (> {stale_minutes}m)")
        if last:
            summary = json.loads(last.get("result_summary_json") or "{}")
            if not isinstance(summary, dict):
                raise ValueError("invalid monitor summary")
            inner = summary.get("monitor_result") or {}
            if (summary.get("protection_blocking") or inner.get("errors")
                    or inner.get("time_exits_errors") or inner.get("error_count")):
                problem("execution_incident", "latest monitor reported execution/protection errors")
        unfinished_closes = []
        for row in journal.query(
            "SELECT o.order_id,o.raw_request_json FROM paper_orders o "
            "WHERE o.execution_source='alpaca_paper' AND o.order_type IN ('market','oco')"
        ):
            metadata = json.loads(row["raw_request_json"] or "{}")
            if metadata.get("phase") in {"canceling", "submitting", "working", "unknown"}:
                pos = journal.position_by_id(metadata.get("position_id"))
                if pos and pos.get("status") == "closed":
                    unfinished_closes.append(row["order_id"])
        result["unfinished_closed_intents"] = unfinished_closes
        if unfinished_closes:
            problem("closed_intent_unverified", "closed origin has unfinished execution intent; reconciliation required")
        blocking = protection_watchdog.has_blocking_incident(journal)
        result["protection_blocking"] = blocking is not None
        if blocking:
            problem("protection_blocking", "unresolved protection incident")
        # Stable semantic identity, not per-pass row IDs or timestamps, so
        # audit observations of one unresolved incident do not become pages.
        result["protection_issues"] = [
            f"{r.get('position_id') or r.get('symbol') or 'unknown'}:{r['protection_status']}"
            for r in journal.query(
                "SELECT DISTINCT position_id, symbol, protection_status FROM protection_checks "
                "WHERE protection_status IN ('unprotected','closed_mismatch','unverifiable') "
                "AND resolved_at_utc IS NULL ORDER BY position_id, symbol, protection_status"
            )
        ] if blocking else []
        result["issue_codes"] = sorted(issue_codes)
        result["ok"] = not problems
        result["entry_ready"] = not problems and not running and not manual_running
        result["detail"] = "; ".join(problems) if problems else f"monitor completed {(age or 0) / 60:.1f}m ago"
    except Exception as exc:
        result.update(ok=False, entry_ready=False, issue_codes=["health_unavailable"],
                      detail=f"monitor health unavailable ({type(exc).__name__})")
    return result
