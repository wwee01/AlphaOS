"""Scheduler v1.5 daily digest (PR3).

Pure read function assembling an operator-facing "what happened today" digest
from the journal. No writes: a later stage's job runner is responsible for
recording the ``job_runs`` row for the ``daily_digest`` job itself -- this
module only reads and returns a dict.

Every key listed in the spec is always present, even when there is nothing to
report (empty list / zero-count rather than omitted), so the digest is
operator-readable and never silently hides a failure by leaving a key out.
"""

from __future__ import annotations

from datetime import datetime, time as _time, timedelta, timezone as _tz

from alphaos.execution.protection_watchdog import status_report
from alphaos.proposals import seconds_remaining as _proposal_seconds_remaining
from alphaos.scheduler import cost_guard
from alphaos.util import timeutils


def _start_of_today_sgt_utc(now=None) -> str:
    """UTC ISO timestamp for midnight at the start of today, Asia/Singapore.

    Mirrors JournalStore.start_of_trading_day_utc()'s approach (build the local
    midnight, convert to UTC, compare as ISO strings) but anchored to the SGT
    calendar day rather than the US market day, since the digest is an
    operator-facing SGT-day summary (Scheduler v1.5 cadence is SGT-based; see
    cadence.default_lock_key's daily_digest lock key).
    """
    st = timeutils.stamp(now)
    sgt_date = st.local_sgt[:10]
    try:
        from zoneinfo import ZoneInfo

        sgt = ZoneInfo("Asia/Singapore")
        y, m, d = (int(p) for p in sgt_date.split("-"))
        start_sgt = datetime.combine(datetime(y, m, d).date(), _time(0, 0), tzinfo=sgt)
        return timeutils.to_iso(start_sgt.astimezone(_tz.utc))
    except Exception:  # pragma: no cover -- extreme fallback, same as timeutils
        return timeutils.to_iso(datetime.fromisoformat(sgt_date))


def build_daily_digest(journal, settings, kill_switch) -> dict:
    """Assemble the daily operator digest. PURE READ -- never writes.

    Every key below is always present; empty list / zero-count rather than
    omitted when there is nothing to report.
    """
    since_sgt = _start_of_today_sgt_utc()

    scheduler_runs_today = journal.query(
        "SELECT job_type, status, COUNT(*) AS n FROM job_runs "
        "WHERE started_at_utc >= ? GROUP BY job_type, status",
        (since_sgt,),
    )

    job_failures_today = journal.query(
        "SELECT job_type, error, started_at_utc FROM job_runs "
        "WHERE status = 'failed' AND started_at_utc >= ? ORDER BY id DESC",
        (since_sgt,),
    )

    # Compare against a Python-computed ISO threshold (not SQLite's datetime('now'),
    # which emits "YYYY-MM-DD HH:MM:SS" with no offset/'T' separator and would not
    # lexically compare correctly against started_at_utc's "...+00:00" ISO strings).
    stale_before = timeutils.to_iso(
        timeutils.now_utc() - timedelta(minutes=int(settings.scheduler_stale_job_minutes))
    )
    stale_running_jobs = journal.query(
        "SELECT * FROM job_runs WHERE status = 'started' AND started_at_utc <= ? ORDER BY id DESC",
        (stale_before,),
    )

    scan_results_today = journal.query(
        "SELECT scan_batch_id, scan_type, status, candidates_found, proposals_created, "
        "watch_count, rejected_count, blocked_count, errors_count FROM scan_batches "
        "WHERE created_at_utc >= ? ORDER BY id DESC",
        (since_sgt,),
    )

    proposals_pending = journal.open_proposals()

    rejects_today = journal.query(
        "SELECT * FROM rejected_candidates WHERE created_at_utc >= ? ORDER BY id DESC",
        (since_sgt,),
    )

    protection_status = status_report(journal)

    open_positions = journal.open_positions()

    blocked_protection_reasons = journal.query(
        "SELECT DISTINCT detail FROM protection_checks "
        "WHERE protection_status IN ('unprotected', 'closed_mismatch', 'unverifiable') "
        "AND detail IS NOT NULL AND created_at_utc >= ?",
        (since_sgt,),
    )
    blocked_proposal_reasons = journal.query(
        "SELECT DISTINCT proposal_reason AS detail FROM trade_proposals "
        "WHERE status = 'blocked' AND proposal_reason IS NOT NULL AND created_at_utc >= ?",
        (since_sgt,),
    )
    new_blocked_entry_reasons_today = sorted(
        {
            row["detail"]
            for row in (blocked_protection_reasons + blocked_proposal_reasons)
            if row.get("detail")
        }
    )

    outcomes_update_status = journal.one(
        "SELECT status, finished_at_utc, result_summary_json FROM job_runs "
        "WHERE job_type = 'outcomes_update' ORDER BY id DESC LIMIT 1"
    )

    system_events_today = journal.query(
        "SELECT * FROM system_events WHERE severity IN ('error', 'critical') "
        "AND created_at_utc >= ? ORDER BY id DESC",
        (since_sgt,),
    )
    errors_and_failures = {
        "job_failures": job_failures_today,
        "system_events": system_events_today,
    }

    # PR5: earnings-proximity awareness -- surface EVERY candidate (not just
    # those that became proposals) whose hold window contains an earnings event,
    # candidates in the warning-but-not-hold window, the actionable proposal
    # subset, and provider health (unavailable data / outright failures) so an
    # operator sees both the signal AND whether the signal itself is healthy.
    # The candidate-level hold bucket is queried separately from the proposal
    # bucket so a rejected/watch candidate that is INSIDE the hold window (the
    # most severe signal) is never dropped just because it never became a
    # proposal. Advisory only: nothing here blocks or alters any trade.
    earnings_candidates_hold_window_today = journal.query(
        "SELECT * FROM candidate_earnings WHERE earnings_within_hold_window = 1 "
        "AND created_at_utc >= ? ORDER BY id DESC",
        (since_sgt,),
    )
    earnings_proposals_near_hold_window_today = journal.query(
        "SELECT * FROM trade_proposals WHERE earnings_within_hold_window = 1 "
        "AND created_at_utc >= ? ORDER BY id DESC",
        (since_sgt,),
    )
    earnings_candidates_warning_today = journal.query(
        "SELECT * FROM candidate_earnings WHERE earnings_within_warning_window = 1 "
        "AND earnings_within_hold_window = 0 AND created_at_utc >= ? ORDER BY id DESC",
        (since_sgt,),
    )
    # Provider HEALTH: genuinely-unavailable/unknown/stale/disabled data the
    # provider was ASKED for -- excludes budget-cap skips (enrichment_status
    # 'skipped'), which are a deliberate cost-control choice, not a data-health
    # problem, and would otherwise inflate this bucket and mask a real outage.
    earnings_data_unavailable_today = journal.query(
        "SELECT * FROM candidate_earnings WHERE earnings_data_status != 'ok' "
        "AND enrichment_status != 'skipped' AND created_at_utc >= ? ORDER BY id DESC",
        (since_sgt,),
    )
    earnings_provider_failures_today = journal.query(
        "SELECT * FROM candidate_earnings WHERE enrichment_status = 'error' "
        "AND created_at_utc >= ? ORDER BY id DESC",
        (since_sgt,),
    )
    earnings_proximity = {
        "enabled": bool(settings.earnings_proximity_enabled),
        "provider": settings.earnings_proximity_provider,
        "candidates_near_earnings_hold_window_today": earnings_candidates_hold_window_today,
        "proposals_near_earnings_hold_window_today": earnings_proposals_near_hold_window_today,
        "candidates_earnings_warning_today": earnings_candidates_warning_today,
        "earnings_data_unavailable_today": earnings_data_unavailable_today,
        "earnings_provider_failures_today": earnings_provider_failures_today,
    }

    # PR6: proposal TTL / stale-approval guard visibility. "Active" and "stale
    # (unmarked)" are both computed against a Python "now" (never SQLite's
    # datetime('now') -- see stale_before above for why) since the DB status
    # column is only lazily flipped to 'expired' the moment someone actually
    # attempts to approve one; the digest must not wait for that to happen to
    # show an operator a proposal is effectively no longer approvable. NULL
    # proposal_expires_at_utc (pre-PR6/legacy rows) counts as stale, matching
    # is_expired()'s own fail-safe rule.
    now_iso = timeutils.to_iso(timeutils.now_utc())
    active_proposals_today = journal.query(
        "SELECT * FROM trade_proposals WHERE status IN ('pending_approval', 'proposed') "
        "AND proposal_expires_at_utc IS NOT NULL AND proposal_expires_at_utc > ? "
        "AND created_at_utc >= ? ORDER BY id DESC",
        (now_iso, since_sgt),
    )
    stale_unmarked_proposals_today = journal.query(
        "SELECT * FROM trade_proposals WHERE status IN ('pending_approval', 'proposed') "
        "AND (proposal_expires_at_utc IS NULL OR proposal_expires_at_utc <= ?) "
        "AND created_at_utc >= ? ORDER BY id DESC",
        (now_iso, since_sgt),
    )
    expired_proposals_today = journal.query(
        "SELECT * FROM trade_proposals WHERE status = 'expired' AND created_at_utc >= ? "
        "ORDER BY id DESC",
        (since_sgt,),
    )
    superseded_proposals_today = journal.query(
        "SELECT * FROM trade_proposals WHERE status = 'superseded' AND created_at_utc >= ? "
        "ORDER BY id DESC",
        (since_sgt,),
    )
    for bucket in (active_proposals_today, stale_unmarked_proposals_today):
        for r in bucket:
            r["seconds_remaining"] = _proposal_seconds_remaining(r.get("proposal_expires_at_utc"))
    proposal_lifecycle = {
        "active_proposals_today": active_proposals_today,
        "stale_unmarked_proposals_today": stale_unmarked_proposals_today,
        "expired_proposals_today": expired_proposals_today,
        "superseded_proposals_today": superseded_proposals_today,
    }

    calls_used = cost_guard.calls_in_last_30_days(journal)
    cost_cap_skipped_today = journal.query(
        "SELECT * FROM job_runs WHERE cost_cap_exceeded = 1 AND started_at_utc >= ? ORDER BY id DESC",
        (since_sgt,),
    )
    cost_usage = {
        "calls_in_last_30_days": calls_used,
        "cap": settings.scheduler_ai_cost_cap_calls_per_30d,
        "cost_cap_exceeded_today": len(cost_cap_skipped_today) > 0,
        "cost_cap_skipped_jobs_today": cost_cap_skipped_today,
    }

    return {
        "date_sgt": timeutils.stamp().local_sgt[:10],
        "kill_switch_engaged": kill_switch.is_engaged(),
        "kill_switch_reason": kill_switch.reason(),
        "scheduler_runs_today": scheduler_runs_today,
        "job_failures_today": job_failures_today,
        "stale_running_jobs": stale_running_jobs,
        "scan_results_today": scan_results_today,
        "proposals_pending": proposals_pending,
        "rejects_today": rejects_today,
        "protection_status": protection_status,
        "open_positions": open_positions,
        "new_blocked_entry_reasons_today": new_blocked_entry_reasons_today,
        "outcomes_update_status": outcomes_update_status,
        "errors_and_failures": errors_and_failures,
        "cost_usage": cost_usage,
        "earnings_proximity": earnings_proximity,
        "proposal_lifecycle": proposal_lifecycle,
    }
