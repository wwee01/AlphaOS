"""Read-only collection health and complete retained experiment inventory.

No provider calls, evaluation writes or switch changes. A configured feature
is not evidence that collection works; a frozen statistic is not a promotion.
"""

from __future__ import annotations

import json
from datetime import timedelta

from alphaos.canary.recovery_evidence import validate_canary_recovery
from alphaos.safety import KillSwitch, ShadowLabelSuspendSwitch
from alphaos.stats.fdr import compute_verdicts
from alphaos.util import timeutils

_LEGACY_REVIEW_NOTES = {
    "H-TQS-1": "Historical centered-delta estimate: reference-arm uncertainty and correlated observations require review; a raw row count is not independent evidence.",
    "H-INT-1": "Historical top-decile versus population-median design needs selection and dependence review; inconclusive does not validate the ranking.",
    "H-WIN-1": "Original query split at noon Singapore time; this does not identify US morning versus afternoon scan performance.",
    "H-CAT-1": "Both confirmed-catalyst and none-found populations must independently meet their floors. Availability of many catalysts does not establish a sufficient control.",
    "H-POL-1": "Original divergent/conflicting vocabulary and side interpretation require explicit evidence review. A repaired forward query does not validate historical polarity labels.",
    "H-TTL-1": "The existing difference test does not establish equivalence; the query's approved replay arm differs from the claim's realized-return wording.",
    "H-REJ-1": "Measures operator-rejected proposal attribution, not evaluator rejection-screen quality; observational selection is not randomized evidence.",
    "H-AI-1": "Historical baseline evidence needs horizon and decision-population review. A coherent prospective cohort is separate; a report read is not a finalized registry verdict.",
}


def _params(row: dict) -> dict:
    try:
        parsed = json.loads(row.get("params_json") or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def build_experiment_registry_report(journal, hypothesis_report: dict) -> dict:
    """All retained registrations, including superseded records and manual lanes.

    CI/estimate are the stored, frozen numbers. q/verdict are recomputed over
    the full frozen family by the shared verdict function, never a new test.
    Methodological validity cannot be inferred from evidence_status='ok'.
    """
    registered = journal.query("SELECT * FROM preregistrations ORDER BY id")
    links = {r["prereg_id"]: r for r in hypothesis_report["hypotheses"] if r.get("prereg_id")}
    verdicts = {r["prereg_id"]: r for r in compute_verdicts(
        [r for r in registered if r.get("evaluated_at_utc")]
    )}
    today = timeutils.stamp().local_sgt[:10]
    corrected_per = {
        r["hypothesis"].split(":", 1)[0]: r["prereg_id"]
        for r in registered
        if r["hypothesis"].startswith(("H-PER-1P-v2:", "H-PER-1N-v2:"))
        and _params(r).get("card_content_hash")
    }
    experiments = []
    for row in registered:
        link = links.get(row["prereg_id"], {})
        v = verdicts.get(row["prereg_id"], {})
        name = row["hypothesis"].split(":", 1)[0]
        superseded_by = corrected_per.get(name + "-v2") if name in ("H-PER-1P", "H-PER-1N") else None
        evaluated = bool(row.get("evaluated_at_utc"))
        date_reached = bool(row.get("analysis_not_before")) and today >= row["analysis_not_before"]
        automatic = bool(link.get("metric_fn_name"))
        state = ("superseded" if superseded_by else "evaluated" if evaluated else
                 "awaiting evidence / analysis" if date_reached else "awaiting analysis date")
        validity = ("Superseded registration retained for history." if superseded_by else
                    "Historical frozen evidence; methodological validity requires review. No promotion implied."
                    if evaluated else "Not evaluated; the date and sample floors do not establish test validity or power.")
        method_note = _LEGACY_REVIEW_NOTES.get(link.get("hypothesis_id"))
        if method_note and not superseded_by:
            validity += " " + method_note
        if _params(row).get("rule_version") == "threshold_v1_hold10":
            validity += " Both replay arms must have a verified complete ten-day horizon; legacy replay rows are not proof of that."
        if name.startswith(("H-PER-1P-v2", "H-PER-1N-v2")):
            validity += " Paired earnings events, controls, identity, concentration and placebo checks must all pass; candidate rows are not independent earnings events."
        experiments.append({
            "prereg_id": row["prereg_id"], "hypothesis_id": link.get("hypothesis_id"),
            "claim": row["hypothesis"], "metric": row["metric"], "state": state,
            "superseded_by": superseded_by, "operator_status": link.get("status"),
            "analysis_not_before": row["analysis_not_before"],
            "registry_date_mismatch": bool(link) and link.get("analysis_not_before") != row["analysis_not_before"],
            "analysis_date_reached": date_reached,
            "evaluation_path": "scheduled registry resolver" if automatic else "reviewed manual analysis required",
            "progress": link.get("progress"),
            "floor_effective_n": row["floor_effective_n"], "floor_span_days": row["floor_span_days"],
            "sample_unit": "day blocks" if _params(row).get("rule_version") in
                ("threshold_v1", "threshold_v1_hold10") else "effective clusters",
            "effective_n": row.get("effective_n"), "n_raw": row.get("n_raw"),
            "span_days": row.get("span_days"), "evaluated_at_utc": row.get("evaluated_at_utc"),
            "point_estimate": row.get("point_estimate"), "ci_low": row.get("ci_low"),
            "ci_high": row.get("ci_high"), "ci_level": row.get("ci_level"),
            "verdict": v.get("verdict"), "q_value": v.get("q_value"), "verdict_reason": v.get("reason"),
            "evidence_status": row.get("evidence_status"), "validity_context": validity,
            "operator_notes": row.get("operator_notes"),
        })
    return {
        "registered_count": len(experiments),
        "evaluated_count": sum(bool(r["evaluated_at_utc"]) for r in experiments),
        "superseded_count": sum(bool(r["superseded_by"]) for r in experiments),
        "experiments": experiments,
        "caveat": "Results use frozen evidence, with q-values across the full evaluated family. Read each claim's direction; rejected does not uniformly mean the claim failed. Historical methods may require correction; no strategy is promoted here.",
    }


def build_collection_status(journal, settings, suspend_switch=None, kill_switch=None) -> dict:
    """Current latch plus independent recovery checks, never an automatic clear.

    Optional switches let offline reviews inspect copied marker files. A clear
    current check never overrides the persistent latch or proves collection.
    """
    from alphaos.scheduler import cost_guard, shadow_label

    suspend = suspend_switch if suspend_switch is not None else ShadowLabelSuspendSwitch()
    kill = kill_switch if kill_switch is not None else KillSwitch()
    labels = journal.one(
        "SELECT COUNT(*) AS successful_labels, MAX(created_at_utc) AS last_successful_label_at_utc "
        "FROM candidate_labels WHERE shadow_tier = 1 AND is_mock = 0 AND validation_status = 'passed'"
    ) or {}
    last_job = journal.one(
        "SELECT status, started_at_utc, finished_at_utc FROM job_runs "
        "WHERE job_type = 'shadow_label' ORDER BY id DESC LIMIT 1"
    )
    latest_canary = None
    checks = {}
    try:
        triggered, reason = shadow_label.check_auto_suspend(journal, settings)
        coverage_ok, coverage_reason = shadow_label.check_feed_coverage_gate(journal, settings)
        # Worst-case configured window, not an invented spending allowance.
        planned = settings.shadow_label_top_k + max(1, round(settings.shadow_explore_fraction * settings.shadow_label_top_k))
        budget_ok, budget_reason = shadow_label.check_shadow_budget(settings, journal, planned)
        canary_evidence = validate_canary_recovery(journal, settings)
        if canary_evidence.get("run_id"):
            latest_canary = journal.one(
                "SELECT run_id, started_at_utc, finished_at_utc, drift_tier, n_prompts, "
                "n_parse_or_failsafe, total_tokens FROM canary_runs WHERE run_id = ?",
                (canary_evidence["run_id"],),
            )
        canary_age = canary_evidence.get("age_seconds")
        checks = {
            "auto_suspend_triggered": triggered, "auto_suspend_reason": reason,
            "coverage_ok": coverage_ok, "coverage_reason": coverage_reason,
            "budget_ok": budget_ok, "budget_reason": budget_reason, "planned_max_calls": planned,
            "shadow_calls_30d": shadow_label.shadow_calls_in_last_30_days(journal),
            "shadow_calls_today": shadow_label.shadow_calls_today(journal),
            "shared_calls_30d": cost_guard.calls_in_last_30_days(journal),
            "latest_canary_recent_and_complete": canary_evidence["ok"],
            "canary_recovery_evidence": canary_evidence,
            "canary_age_days": canary_age / 86400 if canary_age is not None else None,
        }
    except Exception:
        # Do not expose arbitrary provider/config strings. A failed diagnostic
        # must never be represented as evidence that a clear would be safe.
        checks = {"error": "Recovery checks unavailable; inspect the collection service."}
    suspended = suspend.is_engaged()
    killed = kill.is_engaged()
    enabled = settings.shadow_labelling_enabled and settings.shadow_tier_enabled
    last_success = timeutils.parse_iso(labels.get("last_successful_label_at_utc") or "")
    last_market_date = timeutils.market_date(last_success).isoformat() if last_success else ""
    capture_gap = journal.scalar(
        "SELECT COUNT(DISTINCT market_date) FROM universe_days WHERE market_date > ?", (last_market_date,),
    ) or 0
    state = ("disabled" if not enabled else "suspended" if suspended else
             "kill switch engaged" if killed else "checks unavailable" if checks.get("error") else
             "blocked by current conditions" if checks.get("auto_suspend_triggered") or not checks.get("coverage_ok") or not checks.get("budget_ok") else
             "latest canary needs review" if not checks.get("latest_canary_recent_and_complete") else
             "no successful labels yet" if not labels.get("successful_labels") else
             "label collection stale" if capture_gap >= 2 else "awaiting next collection")
    since = timeutils.to_iso(timeutils.now_utc() - timedelta(days=7))
    enrichment = journal.one(
        "SELECT COUNT(*) AS attempts, "
        "SUM(CASE WHEN enrichment_status = 'error' THEN 1 ELSE 0 END) AS errors, "
        "SUM(CASE WHEN enrichment_status = 'error' AND (LOWER(enrichment_error) LIKE '%timeout%' "
        "OR LOWER(enrichment_error) LIKE '%timed out%') THEN 1 ELSE 0 END) AS timeouts, "
        "MAX(CASE WHEN enrichment_status = 'ok' THEN created_at_utc END) AS last_success_at_utc "
        "FROM candidate_last30days WHERE created_at_utc >= ? AND enrichment_status IN ('ok', 'error')",
        (since,),
    ) or {}
    text = journal.one("SELECT COUNT(*) AS documents, MAX(seen_at) AS last_seen_at_utc FROM text_documents") or {}
    return {
        "shadow": {"enabled": enabled, "state": state, "suspended": suspended,
                   "suspension_reason": suspend.reason() if suspended else None,
                   "kill_switch_engaged": killed, **labels, "last_job": last_job,
                   "captured_sessions_since_last_success": capture_gap,
                   "latest_canary": latest_canary, "current_checks": checks,
                   "daily_cap": settings.shadow_ai_cap_calls_per_day,
                   "shadow_30d_cap": settings.shadow_ai_cap_calls_per_30d,
                   "shared_30d_cap": settings.scheduler_ai_cost_cap_calls_per_30d,
                   "note": "Current checks do not clear a latch. Collection is proven by successful real labels, not the enabled flag."},
        "last30days": {"enabled": settings.last30days_enabled, "window_days": 7, **enrichment},
        "text_archive": {"enabled": settings.text_archive_enabled, **text},
        "regime": {"enabled": settings.regime_enabled,
                   **(journal.one("SELECT COUNT(*) AS days, MAX(market_date) AS last_market_date FROM regime_days") or {})},
    }
