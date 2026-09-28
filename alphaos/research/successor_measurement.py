"""Read-only reconstruction from saved daily bars; no source or ledger writes.

The day after the ET decision date is session one. Every scheduled bar must
exist, even after an early barrier, so availability cannot select only early
winners/losers. Result timing is a daily interval bound, NOT an intraday fill.
This is a retrospective idealized measurement, never a prospective receipt.
"""
from __future__ import annotations

import math
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo
from typing import TypeGuard

from alphaos.learning.outcomes_engine import replay_bracket_with_maturity
from alphaos.stats.frozen_policy_inference import canonical_sha256
from alphaos.util.market_calendar import nth_trading_day_after

METHOD = "saved_daily_complete_horizon_bracket_v1"
ET = ZoneInfo("America/New_York")


def utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp_required")
    d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if d.utcoffset() is None:
        raise ValueError("timezone_required")
    return d.astimezone(timezone.utc)


def finite(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def reconstruct_bracket(plan: dict, bars: list[dict], horizon: int | None, as_of: str) -> dict:
    """Save all supplied source inputs and refuse missing/duplicate/bad bars.

16:00 ET is a conservative end bound on early-close days. It delays readiness
rather than admitting an incomplete day. The exact daily touch time is unknown.
"""
    result: dict = {"method": METHOD, "provenance": "retrospective_reconstruction",
                    "status": "blocked", "value": None, "formal_allowed": False,
                    "plan": plan, "horizon_sessions": horizon, "source_bars": []}

    def blocked(reason: str) -> dict:
        result["reason"] = reason
        result["evidence_sha256"] = canonical_sha256({k: v for k, v in result.items() if k != "evidence_sha256"})
        return result

    if type(horizon) is not int or not 1 <= horizon <= 250:
        return blocked("unfixed_or_invalid_horizon")
    try:
        decision, now = utc(plan.get("decision_at_utc")), utc(as_of)
    except (ValueError, TypeError):
        return blocked("invalid_decision_or_analysis_timestamp")
    if decision > now:
        return blocked("future_decision")
    dates = [nth_trading_day_after(decision.astimezone(ET).date(), n).isoformat()
             for n in range(1, horizon + 1)]
    end = datetime.combine(datetime.fromisoformat(dates[-1]).date(), time(16), ET).astimezone(timezone.utc)
    result.update({"scheduled_dates": dates, "measurement_start_utc": decision.isoformat(),
                   "scheduled_end_bound_utc": end.isoformat(), "risk_per_share": None})
    if end > now:
        return blocked("horizon_not_mature")
    if plan.get("decision") == "no_action":
        result.update({"status": "complete", "value": 0., "result": "no_action",
                       "dependence_end_utc": end.isoformat()})
        return blocked("cash_over_declared_horizon")
    entry, stop, target = (plan.get(k) for k in ("entry", "stop", "target"))
    if not (finite(entry) and finite(stop) and finite(target) and min(entry, stop, target) > 0):
        return blocked("missing_or_invalid_levels")
    direction = plan.get("direction")
    if not ((direction == "long" and stop < entry < target) or
            (direction == "short" and target < entry < stop)):
        return blocked("invalid_direction_or_level_order")
    result["risk_per_share"] = abs(entry - stop)
    grouped: dict[str, list[dict]] = {}
    for bar in bars:
        if bar.get("symbol") == plan.get("symbol") and bar.get("market_date") in dates:
            grouped.setdefault(bar["market_date"], []).append(bar)
    result["source_bars"] = [b for d in dates for b in grouped.get(d, [])]
    result["missing_dates"] = [d for d in dates if d not in grouped]
    if result["missing_dates"]:
        return blocked("missing_scheduled_bars")
    if any(len(grouped[d]) != 1 for d in dates):
        return blocked("duplicate_bar_date")
    window = [grouped[d][0] for d in dates]
    if len({b.get("source_feed") for b in window}) != 1:
        return blocked("mixed_bar_feeds")
    for bar in window:
        if not all(finite(bar.get(k)) and bar[k] > 0 for k in ("open", "high", "low", "close")):
            return blocked("invalid_bar_prices")
        if not bar["low"] <= min(bar["open"], bar["close"]) <= max(bar["open"], bar["close"]) <= bar["high"]:
            return blocked("incoherent_bar_prices")
        try:
            observed = utc(bar.get("created_at_utc"))
            close_bound = datetime.combine(datetime.fromisoformat(bar["market_date"]).date(), time(16), ET).astimezone(timezone.utc)
            if observed < close_bound:
                return blocked("bar_first_recorded_before_completed_session")
            if observed > now:
                return blocked("bar_recorded_after_analysis")
        except (ValueError, TypeError):
            return blocked("missing_bar_observation_timestamp")
        if not bar.get("source_feed") or not bar.get("bar_id"):
            return blocked("missing_bar_identity")
    replay = replay_bracket_with_maturity(entry, stop, target, direction, window, max_days=horizon)
    result["replay"] = replay
    if not finite(replay.get("replay_r")):
        return blocked("ambiguous_or_unavailable_replay")
    exit_index = horizon - 1
    if replay["result"] in {"stop_hit", "target_hit"}:
        for index, b in enumerate(window):
            if ((direction == "long" and (b["low"] <= stop or b["high"] >= target)) or
                    (direction == "short" and (b["high"] >= stop or b["low"] <= target))):
                exit_index = index
                break
    exit_bound = datetime.combine(datetime.fromisoformat(dates[exit_index]).date(), time(16), ET).astimezone(timezone.utc)
    result.update({"status": "complete", "value": replay["replay_r"],
                   "result": replay["result"], "exit_session": dates[exit_index],
                   "dependence_end_utc": exit_bound.isoformat(),
                   "interval_precision": "decision_to_daily_exit_upper_bound"})
    return blocked("reconstructed_gross_idealized_not_prospective")
