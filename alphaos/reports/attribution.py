"""User-override attribution learning report (Roadmap 2.8, Part C follow-up).

Compares AlphaOS's original recommendation against the user's final decision on
every USER OVERRIDE, and — once outcomes are resolved — asks *who outperformed*:
the user, AlphaOS, or inconclusive. Pure read of ``user_decision_overrides`` (the
SEPARATE override decision layer; AlphaOS's own recommendation is never rewritten)
plus an optional ``trade_outcomes`` baseline for the AlphaOS-followed expectancy.

DESCRIPTIVE / HEURISTIC ONLY. Overrides are inherently low-frequency, so the
``small_sample`` flag and ``caveat`` are always surfaced; callers must NOT claim
statistical significance on a small forward sample. This report never writes to
the ledger and never touches gates, approval, or execution.
"""

from __future__ import annotations

from typing import Optional

from alphaos.attribution import ATTRIBUTION_VERSION
from alphaos.constants import (
    ArmingClassification,
    AttributionResult,
    AttributionType,
    OverrideOutcomeStatus,
)
from alphaos.reports.metrics import compute_metrics
from alphaos.util import timeutils

# Below this many *resolved* overrides, attribution is anecdotal, not statistical.
MIN_MEANINGFUL_OVERRIDE_SAMPLE = 20

# --- Attribution v2 (PR8) sample floors -- deliberately stricter than the v1
# heuristic block above: v2 claims a DIRECTIONAL R comparison, not just a
# win/loss count, so it demands both a minimum resolved sample AND a minimum
# calendar span (a burst of 30 same-day events is not the same evidence as 30
# spread across weeks).
MIN_RESOLVED_FOR_V2_AGGREGATE = 30
MIN_SPAN_DAYS_FOR_V2_AGGREGATE = 28
MIN_RESOLVED_FOR_V2_SUBSLICE = 20

ATTRIBUTION_V2_CAVEAT = (
    "Attribution v2 measures whether a DEVIATION from AlphaOS's frozen path "
    "(a user override, a gate block, a TTL expiry, or execution vs the frozen "
    "plan) added or cost value in R -- aggregated by attribution_type and "
    "agent ONLY, never summed into one global 'system value' (a single "
    "candidate lifecycle can generate more than one attribution row, e.g. a "
    "blocked proposal a user later overrode). This is NOT a per-event "
    "verdict: no single trade proves AlphaOS or the user 'right' or 'wrong'. "
    "Mock/demo rows are always excluded from aggregates. Below the sample "
    "floor, only counts are shown -- no mean/sum delta_r."
)

# Outcome statuses that count as a closed/decided override (have a real result).
_TERMINAL = frozenset({
    OverrideOutcomeStatus.WON.value,
    OverrideOutcomeStatus.LOST.value,
    OverrideOutcomeStatus.BREAKEVEN.value,
})

ATTRIBUTION_CAVEAT = (
    "Attribution is a HEURISTIC comparison of the user's override vs AlphaOS's "
    "original recommendation, not a statistically significant claim. It credits "
    "the user only when AlphaOS would NOT have traded; 'both traded' / 'both "
    "passed' / breakeven resolve to inconclusive. Treat small samples as "
    "anecdotal and never as proof that the user (or AlphaOS) is the better "
    "decision-maker."
)


def _num(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _count_by(rows: list[dict], key: str) -> dict:
    """Count non-null values of ``key``. Null is skipped (e.g. no reason given,
    or blocked_reason on a non-blocked override), so a breakdown may sum to fewer
    than the total — that is intentional and transparent."""
    out: dict[str, int] = {}
    for r in rows:
        v = r.get(key)
        if v is None or v == "":
            continue
        out[str(v)] = out.get(str(v), 0) + 1
    return out


def _mean(xs: list[float]) -> Optional[float]:
    return (sum(xs) / len(xs)) if xs else None


def compute_attribution(
    overrides: list[dict],
    alphaos_outcomes: Optional[list[dict]] = None,
    total_recommendations: Optional[int] = None,
) -> dict:
    """Pure aggregation over ``user_decision_overrides`` rows. ``alphaos_outcomes``
    (``trade_outcomes`` rows) is an optional baseline for AlphaOS-followed
    expectancy; ``total_recommendations`` is the candidate count for an override
    rate. Returns a JSON-safe dict; never raises on missing fields."""
    n = len(overrides)

    by_action = _count_by(overrides, "user_override_action")
    by_reason = _count_by(overrides, "user_reason_code")
    by_arming = _count_by(overrides, "arming_classification")
    by_blocked = _count_by(overrides, "blocked_reason")

    armed_watch_overrides = sum(1 for o in overrides if o.get("armed_watch"))
    high_risk_overrides = sum(
        1 for o in overrides
        if o.get("arming_classification") == ArmingClassification.HIGH_RISK_NARRATIVE.value
    )
    executed = sum(1 for o in overrides if o.get("execution_allowed"))
    blocked = sum(1 for o in overrides if o.get("blocked_reason"))
    nightdesk_candidates = sum(1 for o in overrides if o.get("nightdesk_research_candidate"))
    by_nightdesk_reason = _count_by(overrides, "nightdesk_research_reason")

    # --- outcomes by status ---
    def _status_n(status: str) -> int:
        return sum(1 for o in overrides if o.get("outcome_status") == status)

    completed = [o for o in overrides if o.get("outcome_status") in _TERMINAL]
    completed_n = len(completed)
    won = _status_n(OverrideOutcomeStatus.WON.value)
    lost = _status_n(OverrideOutcomeStatus.LOST.value)
    breakeven = _status_n(OverrideOutcomeStatus.BREAKEVEN.value)

    # --- attribution tallies ---
    by_attribution = _count_by(overrides, "attribution_result")

    def _attr_n(result: str) -> int:
        return by_attribution.get(result, 0)

    # --- user-override performance (resolved overrides only) ---
    user_win_rate = round(won / completed_n, 3) if completed_n else None
    present_r = [_num(o.get("outcome_r")) for o in completed if o.get("outcome_r") is not None]
    present_pnl = [_num(o.get("outcome_pnl")) for o in completed if o.get("outcome_pnl") is not None]
    user_expectancy_r = round(_mean(present_r), 3) if present_r else None
    user_expectancy_pnl = round(_mean(present_pnl), 2) if present_pnl else None

    # --- AlphaOS-followed baseline (separate trade_outcomes ledger) ---
    alphaos_metrics = compute_metrics(alphaos_outcomes or [])
    alphaos_followed_expectancy_pnl = alphaos_metrics["expectancy"]
    alphaos_followed_sample = alphaos_metrics["trades"]

    small_sample = completed_n < MIN_MEANINGFUL_OVERRIDE_SAMPLE
    if completed_n == 0:
        perf_note = "no resolved overrides yet — outcomes pending"
    elif small_sample:
        perf_note = (f"resolved={completed_n} (< {MIN_MEANINGFUL_OVERRIDE_SAMPLE}); "
                     "descriptive only, not statistically significant")
    else:
        perf_note = f"resolved={completed_n}"

    return {
        "total_recommendations": total_recommendations,
        "total_overrides": n,
        "override_rate": (round(n / total_recommendations, 3)
                          if total_recommendations else None),
        "by_action": by_action,
        "by_reason_code": by_reason,
        "by_arming_classification": by_arming,
        "armed_watch_overrides": armed_watch_overrides,
        "high_risk_narrative_overrides": high_risk_overrides,
        "executed": executed,
        "blocked": blocked,
        "by_blocked_reason": by_blocked,
        "nightdesk_research_candidates": nightdesk_candidates,
        "by_nightdesk_reason": by_nightdesk_reason,
        "outcomes": {
            "pending": _status_n(OverrideOutcomeStatus.PENDING.value),
            "completed": completed_n,
            "won": won,
            "lost": lost,
            "breakeven": breakeven,
            "cancelled": _status_n(OverrideOutcomeStatus.CANCELLED.value),
            "expired": _status_n(OverrideOutcomeStatus.EXPIRED.value),
        },
        "attribution": {
            "user_outperformed": _attr_n(AttributionResult.USER_OUTPERFORMED.value),
            "alphaos_outperformed": _attr_n(AttributionResult.ALPHAOS_OUTPERFORMED.value),
            "inconclusive": _attr_n(AttributionResult.INCONCLUSIVE.value),
            "pending": _attr_n(AttributionResult.PENDING.value),
        },
        "performance": {
            "completed_sample": completed_n,
            "user_win_rate": user_win_rate,
            "user_expectancy_r": user_expectancy_r,
            "user_expectancy_pnl": user_expectancy_pnl,
            "alphaos_followed_expectancy_pnl": alphaos_followed_expectancy_pnl,
            "alphaos_followed_sample": alphaos_followed_sample,
            "small_sample": small_sample,
            "note": perf_note,
        },
        "caveat": ATTRIBUTION_CAVEAT,
    }


def _span_days(rows: list[dict], ts_key: str = "resolved_at_utc") -> Optional[float]:
    dts = []
    for r in rows:
        dt = timeutils.parse_iso(r.get(ts_key))
        if dt is not None:
            dts.append(dt)
    if len(dts) < 2:
        return None
    return (max(dts) - min(dts)).total_seconds() / 86400.0


def _floor_gated_v2_aggregate(rows: list[dict], delta_key: str,
                              min_resolved: int = MIN_RESOLVED_FOR_V2_AGGREGATE) -> dict:
    """``rows`` must already be resolved, non-mock, non-demo rows for ONE
    slice (a type+agent pair, or an execution-gap slice). Returns counts
    always; mean/sum ``delta_key`` ONLY when both the resolved count AND the
    calendar span between the first and last resolved event meet the floor --
    a burst of same-day events never masquerades as weeks of evidence."""
    n = len(rows)
    span = _span_days(rows)
    meets_floor = n >= min_resolved and (span or 0) >= MIN_SPAN_DAYS_FOR_V2_AGGREGATE
    deltas = [r[delta_key] for r in rows if r.get(delta_key) is not None]
    if not meets_floor:
        return {"resolved_count": n, "span_days": round(span, 1) if span is not None else None,
                "mean_delta_r": None, "sum_delta_r": None, "status": "below_sample_floor"}
    return {
        "resolved_count": n, "span_days": round(span, 1) if span is not None else None,
        "mean_delta_r": round(sum(deltas) / len(deltas), 4) if deltas else None,
        "sum_delta_r": round(sum(deltas), 4) if deltas else None,
        "status": "ok",
    }


def compute_attribution_v2(records: list[dict]) -> dict:
    """Pure aggregation over ``attribution_records`` rows (PR8). Aggregates by
    ``attribution_type`` AND ``agent`` ONLY -- deliberately never sums delta_r
    across all types/agents into one global 'system value', since a single
    candidate lifecycle can generate more than one row (e.g. a blocked
    proposal a user later overrode), and mixing agents there would
    misattribute one agent's value to another. Mock rows are always excluded
    from aggregates (counted separately, never silently dropped)."""
    live = [r for r in records if not r.get("is_mock")]
    mock_excluded = sum(1 for r in records if r.get("is_mock"))

    counts_by_type_and_status: dict = {}
    for r in records:
        t, s = r.get("attribution_type"), r.get("resolved_status")
        counts_by_type_and_status.setdefault(t, {}).setdefault(s, 0)
        counts_by_type_and_status[t][s] += 1

    aggregate_by_type_and_agent: dict = {}
    for t in AttributionType:
        resolved_rows = [r for r in live if r.get("attribution_type") == t.value
                         and r.get("resolved_status") == "resolved"]
        agents = sorted({r.get("agent") for r in resolved_rows if r.get("agent")})
        by_agent = {
            agent: _floor_gated_v2_aggregate(
                [r for r in resolved_rows if r.get("agent") == agent], "delta_r",
            )
            for agent in agents
        }
        if by_agent:
            aggregate_by_type_and_agent[t.value] = by_agent

    execution_rows = [
        r for r in live
        if r.get("attribution_type") == AttributionType.PROPOSE_APPROVED_EXECUTED.value
        and r.get("execution_delta_r") is not None
    ]
    execution_gap = _floor_gated_v2_aggregate(execution_rows, "execution_delta_r") if execution_rows else {
        "resolved_count": 0, "span_days": None, "mean_delta_r": None, "sum_delta_r": None,
        "status": "below_sample_floor",
    }

    # PR10: by_card slice -- documentation for learning, not a gate (v1 has
    # exactly one card, so this is mostly scaffolding for when a second card
    # exists). A lighter floor than the main aggregate (MIN_RESOLVED_FOR_V2_
    # SUBSLICE, not MIN_RESOLVED_FOR_V2_AGGREGATE) since a per-card slice is
    # inherently a smaller sample than the whole system's aggregate.
    resolved_live = [r for r in live if r.get("resolved_status") == "resolved"]
    card_ids = sorted({r.get("card_id") for r in resolved_live if r.get("card_id")})
    aggregate_delta_r_by_card = {
        card_id: _floor_gated_v2_aggregate(
            [r for r in resolved_live if r.get("card_id") == card_id], "delta_r",
            min_resolved=MIN_RESOLVED_FOR_V2_SUBSLICE,
        )
        for card_id in card_ids
    }

    return {
        "attribution_version": ATTRIBUTION_VERSION,
        "total_records": len(records),
        "mock_excluded_count": mock_excluded,
        "counts_by_type_and_status": counts_by_type_and_status,
        "aggregate_delta_r_by_type_and_agent": aggregate_by_type_and_agent,
        "aggregate_delta_r_by_card": aggregate_delta_r_by_card,
        "execution_gap_propose_approved_executed": execution_gap,
        "sample_floor_resolved": MIN_RESOLVED_FOR_V2_AGGREGATE,
        "sample_floor_span_days": MIN_SPAN_DAYS_FOR_V2_AGGREGATE,
        "sample_floor_subslice_resolved": MIN_RESOLVED_FOR_V2_SUBSLICE,
        "caveat": ATTRIBUTION_V2_CAVEAT,
    }


def build_attribution_report(journal, settings, limit: int = 1000) -> dict:
    """Read the override layer (+ trade_outcomes baseline) and aggregate. PURE
    READ — safe to call any time; never writes, never touches gates/execution."""
    overrides = journal.recent_user_overrides(limit)
    alphaos_outcomes = journal.query(
        "SELECT * FROM trade_outcomes ORDER BY id DESC LIMIT ?", (limit,)
    )
    try:
        total_recommendations = journal.count_rows("candidates")
    except Exception:
        total_recommendations = None
    rep = compute_attribution(
        overrides, alphaos_outcomes=alphaos_outcomes,
        total_recommendations=total_recommendations,
    )
    rep["as_of"] = timeutils.market_date().isoformat()
    rep["mode"] = settings.mode.value
    rep["execution_provider"] = settings.execution_provider

    # PR8: Attribution v2 -- counterfactual ΔR ledger, a SEPARATE nested block
    # (never merged into the v1 heuristic fields above, which answer a
    # coarser "who outperformed" question over a different table).
    attribution_records = journal.query(
        "SELECT ar.attribution_type, ar.agent, ar.resolved_status, ar.delta_r, "
        "ar.execution_delta_r, ar.is_mock, ar.resolved_at_utc, c.card_id "
        "FROM attribution_records ar LEFT JOIN candidates c ON c.candidate_id = ar.candidate_id "
        "ORDER BY ar.id DESC LIMIT ?",
        (limit,),
    )
    rep["v2"] = compute_attribution_v2(attribution_records)
    return rep


def _fmt(v, money: bool = False) -> str:
    if v is None:
        return "—"
    if money:
        return f"${v:,.2f}"
    return str(v)


def render_markdown(rep: dict) -> str:
    """Human-readable markdown for the CLI."""
    o = rep["outcomes"]
    a = rep["attribution"]
    p = rep["performance"]
    lines = [
        f"# User-Override Attribution Report — {rep.get('as_of', '')}",
        f"_mode: {rep.get('mode')} · execution: {rep.get('execution_provider')}_",
        "",
        f"- AlphaOS recommendations: **{_fmt(rep['total_recommendations'])}**",
        f"- User overrides: **{rep['total_overrides']}** "
        f"(override rate {_fmt(rep['override_rate'])})",
        f"- Executed (gates passed): **{rep['executed']}**  ·  "
        f"Blocked by safety: **{rep['blocked']}**",
        f"- Armed-watch overrides: **{rep['armed_watch_overrides']}**  ·  "
        f"High-risk-narrative overrides: **{rep['high_risk_narrative_overrides']}**",
        "",
        "## Overrides by action",
    ]
    lines += [f"- {k}: {v}" for k, v in rep["by_action"].items()] or ["- (none)"]
    lines += ["", "## Overrides by reason code"]
    lines += [f"- {k}: {v}" for k, v in rep["by_reason_code"].items()] or ["- (none)"]
    lines += ["", "## Overrides by arming classification"]
    lines += [f"- {k}: {v}" for k, v in rep["by_arming_classification"].items()] or ["- (none)"]
    if rep["by_blocked_reason"]:
        lines += ["", "## Blocked overrides by reason"]
        lines += [f"- {k}: {v}" for k, v in rep["by_blocked_reason"].items()]
    if rep["nightdesk_research_candidates"]:
        lines += ["", f"## NightDesk research candidates: {rep['nightdesk_research_candidates']}"]
        lines += [f"- {k}: {v}" for k, v in rep["by_nightdesk_reason"].items()]
    lines += [
        "",
        "## Outcomes",
        f"- pending: {o['pending']}  ·  completed: {o['completed']} "
        f"(won {o['won']} / lost {o['lost']} / breakeven {o['breakeven']})  ·  "
        f"cancelled: {o['cancelled']}  ·  expired: {o['expired']}",
        "",
        "## Who outperformed",
        f"- user_outperformed: {a['user_outperformed']}",
        f"- alphaos_outperformed: {a['alphaos_outperformed']}",
        f"- inconclusive: {a['inconclusive']}",
        f"- pending: {a['pending']}",
        "",
        "## Performance (resolved overrides only)",
        f"- completed sample: {p['completed_sample']}",
        f"- user win rate: {_fmt(p['user_win_rate'])}",
        f"- user expectancy (R): {_fmt(p['user_expectancy_r'])}",
        f"- user expectancy (P&L): {_fmt(p['user_expectancy_pnl'], money=True)}",
        f"- AlphaOS-followed expectancy (P&L): "
        f"{_fmt(p['alphaos_followed_expectancy_pnl'], money=True)} "
        f"(n={p['alphaos_followed_sample']})",
        f"- {p['note']}",
        "",
        f"> ⚠️ {rep['caveat']}",
    ]
    if "v2" in rep:
        v2 = rep["v2"]
        lines += [
            "",
            f"# Attribution v2 (counterfactual ΔR) — version {v2['attribution_version']}",
            f"- Total records: **{v2['total_records']}**  ·  Mock-excluded: **{v2['mock_excluded_count']}**",
            "",
            "## Counts by type and status",
        ]
        for t, statuses in v2["counts_by_type_and_status"].items():
            lines.append(f"- {t}: " + ", ".join(f"{s}={n}" for s, n in statuses.items()))
        if not v2["counts_by_type_and_status"]:
            lines.append("- (none)")
        lines += ["", "## Aggregate ΔR by type and agent (floor-gated)"]
        for t, by_agent in v2["aggregate_delta_r_by_type_and_agent"].items():
            for agent, agg in by_agent.items():
                if agg["status"] == "ok":
                    lines.append(
                        f"- {t} / {agent}: n={agg['resolved_count']} "
                        f"(span {agg['span_days']}d) mean ΔR={agg['mean_delta_r']} "
                        f"sum ΔR={agg['sum_delta_r']}"
                    )
                else:
                    lines.append(
                        f"- {t} / {agent}: n={agg['resolved_count']} — below_sample_floor "
                        f"(needs ≥{v2['sample_floor_resolved']} resolved over "
                        f"≥{v2['sample_floor_span_days']}d)"
                    )
        if not v2["aggregate_delta_r_by_type_and_agent"]:
            lines.append("- (none)")
        lines += ["", "## Aggregate ΔR by setup card (floor-gated)"]
        for card_id, agg in v2["aggregate_delta_r_by_card"].items():
            if agg["status"] == "ok":
                lines.append(
                    f"- {card_id}: n={agg['resolved_count']} (span {agg['span_days']}d) "
                    f"mean ΔR={agg['mean_delta_r']} sum ΔR={agg['sum_delta_r']}"
                )
            else:
                lines.append(
                    f"- {card_id}: n={agg['resolved_count']} — below_sample_floor "
                    f"(needs ≥{v2['sample_floor_subslice_resolved']} resolved)"
                )
        if not v2["aggregate_delta_r_by_card"]:
            lines.append("- (none)")
        eg = v2["execution_gap_propose_approved_executed"]
        lines += ["", "## Execution gap (propose_approved_executed)"]
        if eg["status"] == "ok":
            lines.append(
                f"- n={eg['resolved_count']} (span {eg['span_days']}d) "
                f"mean execution ΔR={eg['mean_delta_r']} sum={eg['sum_delta_r']}"
            )
        else:
            lines.append(f"- n={eg['resolved_count']} — below_sample_floor")
        lines += ["", f"> ⚠️ {v2['caveat']}"]
    return "\n".join(lines)
