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

from alphaos.attribution import ATTRIBUTION_VERSION
from alphaos.constants import ProposalStatus
from alphaos.data.market_data import MarketDataClient
from alphaos.execution.protection_watchdog import status_report
from alphaos.proposals import seconds_remaining as _proposal_seconds_remaining
from alphaos.reports.position_health import assess_positions
from alphaos.safety import ShadowLabelSuspendSwitch
from alphaos.scheduler import cost_guard
from alphaos.scheduler import shadow_label as shadow_label_module
from alphaos.tqs import TQS_VERSION
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
    open_statuses = ProposalStatus.approvable()
    active_proposals_today = journal.query(
        "SELECT * FROM trade_proposals WHERE status IN (?, ?) "
        "AND proposal_expires_at_utc IS NOT NULL AND proposal_expires_at_utc > ? "
        "AND created_at_utc >= ? ORDER BY id DESC",
        (*open_statuses, now_iso, since_sgt),
    )
    stale_unmarked_proposals_today = journal.query(
        "SELECT * FROM trade_proposals WHERE status IN (?, ?) "
        "AND (proposal_expires_at_utc IS NULL OR proposal_expires_at_utc <= ?) "
        "AND created_at_utc >= ? ORDER BY id DESC",
        (*open_statuses, now_iso, since_sgt),
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

    # PR7: TQS v0 shadow scoring visibility. PURE READ, reporting only -- this
    # section (and every field in it) is for an operator to eyeball coverage
    # and score distribution; no decision path may read it. See
    # alphaos/tqs/ module docstring for the enforced boundary.
    tqs_rows_today = journal.query(
        "SELECT tqs_bucket, data_confidence, is_mock FROM tqs_scores WHERE created_at_utc >= ?",
        (since_sgt,),
    )
    tqs_bucket_histogram: dict = {}
    for r in tqs_rows_today:
        tqs_bucket_histogram[r["tqs_bucket"]] = tqs_bucket_histogram.get(r["tqs_bucket"], 0) + 1
    confidences = [r["data_confidence"] for r in tqs_rows_today if r["data_confidence"] is not None]
    # PR10: bucket histogram grouped by setup card -- same shape as
    # bucket_histogram_today above, just sliced by card_id via a join on
    # candidates (tqs_scores itself never stores card_id, preserving its
    # measurement-only isolation -- see alphaos/tqs/ module docstring).
    tqs_rows_by_card_today = journal.query(
        "SELECT t.tqs_bucket, c.card_id FROM tqs_scores t "
        "JOIN candidates c ON c.candidate_id = t.candidate_id "
        "WHERE t.created_at_utc >= ? AND c.card_id IS NOT NULL",
        (since_sgt,),
    )
    tqs_bucket_histogram_by_card: dict = {}
    for r in tqs_rows_by_card_today:
        by_card = tqs_bucket_histogram_by_card.setdefault(r["card_id"], {})
        by_card[r["tqs_bucket"]] = by_card.get(r["tqs_bucket"], 0) + 1
    tqs_shadow = {
        "enabled": bool(settings.tqs_shadow_enabled),
        "tqs_version": TQS_VERSION,
        "scored_count_today": len(tqs_rows_today),
        "bucket_histogram_today": tqs_bucket_histogram,
        "bucket_histogram_by_card_today": tqs_bucket_histogram_by_card,
        "mean_data_confidence_today": round(sum(confidences) / len(confidences), 2) if confidences else None,
        "unscorable_count_today": tqs_bucket_histogram.get("unscorable", 0),
        "mock_share_today": (
            round(sum(1 for r in tqs_rows_today if r["is_mock"]) / len(tqs_rows_today), 2)
            if tqs_rows_today else None
        ),
    }

    # PR8: Attribution v2 visibility. PURE READ, reporting only -- counts and
    # mock-share only (no mean/sum delta_r here; that stays floor-gated inside
    # alphaos/reports/attribution.py's fuller report, not the digest). See
    # alphaos/attribution/ module docstring for the enforced boundary.
    attribution_rows_today = journal.query(
        "SELECT attribution_type, resolved_status, is_mock FROM attribution_records "
        "WHERE created_at_utc >= ?",
        (since_sgt,),
    )
    attribution_type_histogram: dict = {}
    for r in attribution_rows_today:
        attribution_type_histogram[r["attribution_type"]] = (
            attribution_type_histogram.get(r["attribution_type"], 0) + 1
        )
    attribution_shadow = {
        "enabled": bool(settings.attribution_enabled),
        "attribution_version": ATTRIBUTION_VERSION,
        "discovered_count_today": len(attribution_rows_today),
        "type_histogram_today": attribution_type_histogram,
        "pending_count_today": sum(1 for r in attribution_rows_today if r["resolved_status"] == "pending"),
        "resolved_count_today": sum(1 for r in attribution_rows_today if r["resolved_status"] == "resolved"),
        "unresolvable_count_today": sum(
            1 for r in attribution_rows_today if r["resolved_status"] == "unresolvable"
        ),
        "mock_share_today": (
            round(sum(1 for r in attribution_rows_today if r["is_mock"]) / len(attribution_rows_today), 2)
            if attribution_rows_today else None
        ),
    }

    # PR11: portfolio health summary, same bucket-histogram shape as
    # tqs_shadow above. Runs the same live-price sweep benchmark_capture.py
    # (PR9.5) already precedents inside a scheduled report job -- open
    # positions are few (the risk engine caps concurrent count), so this is
    # a modest cost, not a new architecture.
    health_rows = assess_positions(journal, settings, MarketDataClient(settings, journal))
    verdict_histogram: dict = {}
    thesis_histogram: dict = {}
    for h in health_rows:
        verdict_histogram[h["verdict"]] = verdict_histogram.get(h["verdict"], 0) + 1
        thesis_histogram[h["thesis_status"]] = thesis_histogram.get(h["thesis_status"], 0) + 1
    position_health = {
        "open_position_count": len(health_rows),
        "verdict_histogram": verdict_histogram,
        "thesis_histogram": thesis_histogram,
        "exit_review_count": verdict_histogram.get("EXIT_REVIEW", 0),
    }

    # EXP-0: shadow-tier deterministic universe capture visibility. PURE
    # READ, counts only (floor-gating like tqs_shadow/attribution_shadow
    # above doesn't apply here -- there's no delta_r/mean being claimed, just
    # today's scan coverage). feed_coverage measures the free IEX feed's
    # usable-quote rate on the shadow band specifically -- the number that
    # decides (empirically, not by guess) whether the ~$99/mo SIP upgrade is
    # needed before EXP-1 (master reference §9 decision row).
    shadow_tier_rows_today = journal.query(
        "SELECT freshness_status, candidate_found FROM universe_days WHERE created_at_utc >= ?",
        (since_sgt,),
    )
    shadow_scanned = len(shadow_tier_rows_today)
    shadow_fresh = sum(1 for r in shadow_tier_rows_today if r["freshness_status"] == "usable")
    shadow_interest_scores = sorted(
        (r["interest_score"] or 0.0) for r in journal.query(
            "SELECT c.interest_score FROM universe_days u "
            "JOIN candidates c ON c.candidate_id = u.candidate_id "
            "WHERE u.created_at_utc >= ? AND u.candidate_id IS NOT NULL",
            (since_sgt,),
        )
    )
    top_decile_count = 0
    if shadow_interest_scores:
        threshold = shadow_interest_scores[max(0, int(0.9 * (len(shadow_interest_scores) - 1)))]
        top_decile_count = sum(1 for s in shadow_interest_scores if s >= threshold)
    shadow_tier = {
        "enabled": bool(settings.shadow_tier_enabled),
        "scanned_today": shadow_scanned,
        "fresh_today": shadow_fresh,
        "stale_today": shadow_scanned - shadow_fresh,
        "candidates_today": sum(1 for r in shadow_tier_rows_today if r["candidate_found"]),
        # Top decile of TODAY's shadow-tier candidates' own interest scores
        # (nearest-rank) -- a "standouts among tonight's standouts" signal,
        # not a percentile of the whole shadow universe. See
        # Orchestrator._count_top_decile_interest for the same definition
        # computed once already per scan; this is the daily rollup.
        "top_decile_interest_count_today": top_decile_count,
        "feed_coverage_today": round(shadow_fresh / shadow_scanned, 4) if shadow_scanned else None,
    }

    # EXP-1 mechanism 9(d)/12: shadow-tier AI LABELLING visibility. Counts +
    # health only, floor-gated the same way tqs_shadow/attribution_shadow
    # are above -- NO shadow symbol names, ever (mechanism 9(e): the trap is
    # an operator manually trading a surfaced small-cap, contaminating the
    # shadow ledger). Segmented `shadow_tier = 1` labeller health (parse/
    # fail-safe rate, validation_status mix, confidence histogram) per
    # mechanism 12.
    shadow_label_rows_today = journal.query(
        "SELECT validation_status, label_source, label_confidence, primary_label, is_mock "
        "FROM candidate_labels WHERE shadow_tier = 1 AND created_at_utc >= ?",
        (since_sgt,),
    )
    shadow_label_status_histogram: dict = {}
    for r in shadow_label_rows_today:
        key = r["validation_status"] or "unknown"
        shadow_label_status_histogram[key] = shadow_label_status_histogram.get(key, 0) + 1
    shadow_fail_safe_today = sum(1 for r in shadow_label_rows_today if r["label_source"] == "fail_safe")
    shadow_confidences = [r["label_confidence"] for r in shadow_label_rows_today if r["label_confidence"] is not None]
    shadow_label_other_today = sum(1 for r in shadow_label_rows_today if r["primary_label"] == "Other/Unclassified")
    shadow_suspend_engaged = ShadowLabelSuspendSwitch().is_engaged()
    shadow_labelling = {
        "enabled": bool(settings.shadow_labelling_enabled),
        "auto_suspended": shadow_suspend_engaged,
        "labelled_today": len(shadow_label_rows_today),
        "fail_safe_count_today": shadow_fail_safe_today,
        "fail_safe_rate_today": (
            round(shadow_fail_safe_today / len(shadow_label_rows_today), 4) if shadow_label_rows_today else None
        ),
        "validation_status_histogram_today": shadow_label_status_histogram,
        "other_unclassified_count_today": shadow_label_other_today,
        "mean_confidence_today": (
            round(sum(shadow_confidences) / len(shadow_confidences), 3) if shadow_confidences else None
        ),
        "calls_in_last_30_days": shadow_label_module.shadow_calls_in_last_30_days(journal),
        "cap_per_30d": settings.shadow_ai_cap_calls_per_30d,
        "calls_today": shadow_label_module.shadow_calls_today(journal),
        "cap_per_day": settings.shadow_ai_cap_calls_per_day,
    }

    from alphaos.util.market_calendar import is_trading_day

    return {
        "date_sgt": timeutils.stamp().local_sgt[:10],
        "is_trading_day_today": is_trading_day(timeutils.market_date()),
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
        "tqs_shadow": tqs_shadow,
        "attribution_shadow": attribution_shadow,
        "position_health": position_health,
        "shadow_tier": shadow_tier,
        "shadow_labelling": shadow_labelling,
    }
