"""PER input-first fixed event/control membership, independent of outcomes.

This corrects an otherwise subtle survivorship error: earliest *completed*
event candidates are not necessarily earliest eligible event candidates.
Every absent outcome remains attached to its selected original opportunity.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
from zoneinfo import ZoneInfo

from alphaos.research.successor_measurement import finite, utc
from alphaos.util.market_calendar import trading_days_between


def fixed_per_membership(candidates: list[dict], earnings: list[dict], outcomes: dict) -> dict:
    cache = {str(e["id"]): e for e in earnings}
    events: dict[tuple, tuple] = {}
    malformed = []

    def dated(c: dict) -> dict:
        # One immutable input clock. Outcome-row presence or later backfills
        # must never change enrollment order; this is a named draft convention.
        decision = c["created_at_utc"]
        return {**c, "selection_time_utc": decision,
                "market_date": utc(decision).astimezone(ZoneInfo("America/New_York")).date().isoformat(),
                "tier": int(c.get("shadow_tier") or 0)}

    eligible = [dated(c) for c in candidates if c.get("card_assignment_status") == "ok"]
    for c in eligible:
        if c.get("card_id") != "post_earnings_reaction":
            continue
        e = cache.get(str(c.get("card_assignment_ref")))
        if not e or not e.get("report_date"):
            malformed.append(c["candidate_id"])
            continue
        key = (c["symbol"], e.get("fiscal_date_ending") or e["report_date"])
        order = (utc(c["selection_time_utc"]), c["candidate_id"])
        if key not in events or order < events[key][0]:
            events[key] = (order, {**c, "event_key": "|".join(key), "earnings_row_id": e["id"]})
    selected = [v[1] for _, v in sorted(events.items())]
    dates = {c["market_date"] for c in selected}
    controls: dict[tuple, dict] = {}
    exclusions = []
    for c in eligible:
        if not c.get("card_id") or c["card_id"] == "post_earnings_reaction" or c["market_date"] not in dates:
            continue
        d = date.fromisoformat(c["market_date"])
        overlap = any(c["symbol"] == e["symbol"] and
                      trading_days_between(min(d, date.fromisoformat(e["market_date"])), max(d, date.fromisoformat(e["market_date"]))) <= 5
                      for e in selected)
        if overlap:
            exclusions.append(c["candidate_id"])
            continue
        control_key = (c["symbol"], c["market_date"], c["tier"])
        if control_key not in controls or (utc(c["selection_time_utc"]), c["candidate_id"]) < (utc(controls[control_key]["selection_time_utc"]), controls[control_key]["candidate_id"]):
            controls[control_key] = c
    control_rows = list(controls.values())
    for e in selected:
        same_day = [c for c in control_rows if (c["market_date"], c["tier"]) == (e["market_date"], e["tier"])]
        tier = [c for c in control_rows if c["tier"] == e["tier"]]
        refs, rung = (same_day, "date_tier") if len(same_day) >= 5 else (tier, "pooled_tier") if len(tier) >= 30 else ([], "no_rung")
        e["control_ids"], e["control_rung"] = sorted(c["candidate_id"] for c in refs), rung

    def recorded(c: dict) -> bool:
        o = outcomes.get(c["candidate_id"], {})
        return o.get("outcome_status") == "complete" and finite(o.get("market_adjusted_return_5d_pct"))

    months = {e["market_date"][:7] for e in selected}
    sorted_dates = sorted(dates)
    span = (date.fromisoformat(sorted_dates[-1]) - date.fromisoformat(sorted_dates[0])).days if sorted_dates else 0
    maximum = max(Counter(e["symbol"] for e in selected).values(), default=0) / len(selected) if selected else 0.
    rungs = Counter(e["control_rung"] for e in selected)
    guards = {"25_events": len(selected) >= 25, "100_controls": len(control_rows) >= 100,
              "90_day_span": span >= 90, "3_months": len(months) >= 3, "max_symbol_20pct": maximum <= .2,
              "max_pooled_20pct": rungs["pooled_tier"] <= .2 * len(selected),
              "max_no_rung_10pct": rungs["no_rung"] <= .1 * len(selected)}
    return {"events": selected, "controls": control_rows, "missing_event_reference_ids": malformed,
            "overlap_excluded_control_ids": sorted(exclusions), "event_membership_uses_outcomes": False,
            "selection_clock": "immutable_candidate_created_at_utc_not_outcome_timestamp",
            "rungs_fixed_before_outcomes": True, "population_guards_all_reported": guards,
            "counts": {"events": len(selected), "controls": len(control_rows), "recorded_event_values": sum(recorded(e) for e in selected),
                       "recorded_control_values": sum(recorded(c) for c in control_rows), "span_days": span,
                       "distinct_months": len(months), "maximum_symbol_share": maximum, "rungs": dict(rungs)}}
