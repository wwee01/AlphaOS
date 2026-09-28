"""Complete-membership retrospective exports over a READ-ONLY copied ledger.

Membership is selected from inputs before examining outcomes. Every selected
member remains present, including unavailable/immature/ambiguous measurements.
This exports a reproducible research draft, not timely prospective enrollment.
"""
from __future__ import annotations

import math
import sqlite3
from collections import Counter, defaultdict
from statistics import mean, median

from alphaos.research.successor_contracts import draft_contract
from alphaos.research.successor_measurement import finite, reconstruct_bracket, utc
from alphaos.research.successor_per import fixed_per_membership
from alphaos.stats.frozen_policy_inference import canonical_sha256

TABLES = ("preregistrations", "hypothesis_proposals", "candidates", "candidate_outcomes",
          "shadow_baseline_decisions", "tqs_scores", "candidate_catalysts", "last30days_polarity",
          "attribution_records", "trade_proposals", "trade_outcomes", "scan_batches", "daily_bars",
          "candidate_labels", "earnings_calendar_cache", "paper_orders", "paper_fills", "positions")


def rank_membership(rows: list[dict], score_key: str, tail_fraction: float) -> dict[str, str]:
    """DRAFT fixed-count ranks: ties broken by candidate ID, disjoint tails.

No outcome enters the ranking. The entire eligible population and scores are
exported. This proposed rule needs review; it is not the original quantile rule.
"""
    ordered = sorted(rows, key=lambda r: (r[score_key], r["candidate_id"]))
    n = min(len(ordered) // 2, math.ceil(len(ordered) * tail_fraction))
    result = {r["candidate_id"]: "middle" for r in ordered}
    for r in ordered[:n]:
        result[r["candidate_id"]] = "reference"
    for r in ordered[len(ordered) - n:] if n else []:
        result[r["candidate_id"]] = "tested"
    return result


def _latest(rows: list[dict], key: str = "candidate_id") -> dict:
    return {r[key]: r for r in sorted(rows, key=lambda r: r["id"])}


def _available_summary(members: list[dict], statistic: str) -> dict:
    """Explicit incomplete-case DESCRIPTIVE values, never the full estimand."""
    observed = [r for r in members if r.get("measurement", {}).get("status") == "complete"
                and finite(r["measurement"].get("value"))]
    by_arm: dict[str, list] = defaultdict(list)
    for r in observed:
        by_arm[r["arm"]].append(r)
    point = None
    if statistic == "paired_mean":
        # Each exported member is a full candidate pair, not two independently
        # selectable outcomes. measurement.value exists only when BOTH resolve.
        point = mean(r["measurement"]["value"] for r in observed) if observed else None
    elif statistic == "single_mean":
        point = mean(r["measurement"]["value"] for r in observed) if observed else None
    elif statistic == "top_mean_minus_population_median":
        selected = [r["measurement"]["value"] for r in observed if r.get("selected")]
        if selected:
            point = mean(selected) - median(r["measurement"]["value"] for r in observed)
    elif by_arm["tested"] and by_arm["reference"]:
        point = mean(r["measurement"]["value"] for r in by_arm["tested"]) - mean(r["measurement"]["value"] for r in by_arm["reference"])
    return {"label": "available_members_descriptive_only_not_complete_cohort_inference",
            "point_estimate": point, "n_observed": len(observed), "n_members": len(members),
            "n_missing": len(members) - len(observed),
            "members_by_arm": dict(Counter(r["arm"] for r in members)),
            "observed_by_arm": dict(Counter(r["arm"] for r in observed)),
            "measurement_reasons": dict(Counter(r.get("measurement", {}).get("reason", "missing") for r in members)),
            "interval": None, "p_value": None, "formal_allowed": False}


def export_cohorts(connection: sqlite3.Connection, *, as_of_utc: str, snapshot_sha256: str,
                   draft_horizons: dict[str, int] | None = None) -> dict:
    """SELECT-only. Caller supplies a saved consistent copy, not a live writer."""
    connection.row_factory = sqlite3.Row
    data = {t: [dict(r) for r in connection.execute(f'SELECT * FROM "{t}" ORDER BY id')] for t in TABLES}
    draft_horizons = draft_horizons or {}
    known = {r["prereg_id"] for r in data["preregistrations"]}
    if any(k not in known or type(v) is not int or not 1 <= v <= 250 for k, v in draft_horizons.items()):
        raise ValueError("invalid_explicit_draft_horizon")
    asof = utc(as_of_utc)
    original_counts = {t: len(rows) for t, rows in data.items()}
    unavailable_sources = []
    # A later recorded feature cannot participate in an earlier ranking. A
    # mutable row observed/updated later cannot reconstruct its prior contents.
    time_fields = ("created_at_utc", "computed_at_utc", "decision_at_utc", "updated_at_utc", "label_frozen_at_utc",
                   "resolved_at_utc", "expired_at_utc", "superseded_at_utc", "fetched_at_utc", "filled_at", "opened_at")
    for table, table_rows in data.items():
        available = []
        for row in table_rows:
            reason = None
            if not row.get("created_at_utc"):
                reason = "missing_source_recorded_timestamp"
            else:
                try:
                    if any(utc(row[k]) > asof for k in time_fields if row.get(k)):
                        reason = "source_recorded_or_updated_after_capture_boundary"
                except (ValueError, TypeError):
                    reason = "invalid_source_timestamp"
            if reason:
                unavailable_sources.append({"table": table, "row_id": row["id"], "reason": reason})
            else:
                available.append(row)
        data[table] = available
    # Capture timestamp is a boundary, not permission to backdate mutable rows.
    candidates = {r["candidate_id"]: r for r in data["candidates"] if utc(r["created_at_utc"]) <= asof}
    outcomes = _latest([r for r in data["candidate_outcomes"] if r.get("candidate_type") != "user_override"])
    baselines = data["shadow_baseline_decisions"]
    scans = {r["scan_batch_id"]: r for r in data["scan_batches"]}
    registry: dict[str, list] = defaultdict(list)
    for r in data["hypothesis_proposals"]:
        registry[r["prereg_id"]].append(r)
    source_rows: dict[str, dict] = {}
    measurements: dict[str, dict] = {}
    bars_by_symbol: dict[str, list] = defaultdict(list)
    for b in data["daily_bars"]:
        bars_by_symbol[b["symbol"]].append(b)

    def source(table: str, row: dict | None) -> str | None:
        if row is None:
            return None
        k = f"{table}:{row['id']}"
        source_rows[k] = row
        return k

    def measure(c: dict, horizon: int | None, *, baseline: dict | None = None) -> dict:
        co = outcomes.get(c["candidate_id"])
        key = (f"baseline:{baseline['id']}" if baseline else f"candidate:{c['candidate_id']}") + f":h{horizon}"
        if key in measurements:
            return measurements[key]
        if baseline:
            plan = {k: baseline.get(k) for k in ("symbol", "decision", "entry", "stop", "target", "direction", "decision_at_utc")}
            ref = source("shadow_baseline_decisions", baseline)
        else:
            plan = {"symbol": c["symbol"], "decision_at_utc": (co or {}).get("decision_at_utc", c["created_at_utc"]),
                    "entry": (co or {}).get("entry_reference_price"), "stop": (co or {}).get("stop_price"),
                    "target": (co or {}).get("target_price"), "direction": (co or {}).get("direction_hint")}
            ref = source("candidate_outcomes", co)
        answer = reconstruct_bracket(plan, bars_by_symbol[c["symbol"]], horizon, as_of_utc)
        answer.update({"measurement_id": key, "input_source_ref": ref})
        answer["evidence_sha256"] = canonical_sha256({k: v for k, v in answer.items() if k != "evidence_sha256"})
        measurements[key] = answer
        return answer

    def member(c: dict, arm: str, horizon: int | None, feature: tuple[str, dict] | None = None) -> dict:
        co = outcomes.get(c["candidate_id"])
        refs = [source("candidates", candidates[c["candidate_id"]]), source("candidate_outcomes", co)]
        if feature:
            refs.append(source(*feature))
        observed = measure(c, horizon)
        return {"member_id": c["candidate_id"], "candidate_id": c["candidate_id"], "arm": arm,
                "symbol": c["symbol"], "source_refs": [r for r in refs if r],
                "primary_decision": (co or {}).get("eval_decision"),
                "final_decision": (co or {}).get("final_decision"),
                "measurement": {k: observed.get(k) for k in ("measurement_id", "status", "value", "reason")},
                "recorded_replay": {k: (co or {}).get(k) for k in ("replay_r", "replay_result", "replay_status", "replay_method_version", "replay_window_days")}}

    families: list[dict] = []
    for registration in data["preregistrations"]:
        draft = draft_contract(registration, registry[registration["prereg_id"]])
        if draft["status"] == "unsupported_registration":
            families.append({"contract": draft, "members": [], "implementation_status": "unsupported_registration"})
            continue
        name, horizon = draft["name"], draft["horizon_sessions"]
        if registration["prereg_id"] in draft_horizons:
            if horizon is not None and horizon != draft_horizons[registration["prereg_id"]]:
                raise ValueError("cannot_replace_original_numeric_horizon_in_this_adapter")
            horizon = draft_horizons[registration["prereg_id"]]
            draft["horizon_sessions"] = horizon
            draft["unreviewed_explicit_draft_horizon"] = horizon
            draft["draft_id"] += f":horizon{horizon}"
        rows: list[dict] = []
        details: dict = {}
        engineering = []
        if name in {"H-AI-1", "H-AI-HOLD10", "H-AI-SHADOW-1"}:
            rule = "threshold_v1_hold10" if name == "H-AI-HOLD10" else "threshold_v1"
            tier = int(name == "H-AI-SHADOW-1")
            for b in baselines:
                c = candidates.get(b["candidate_id"])
                if not c or b["rule_version"] != rule or (c.get("shadow_tier") or 0) != tier:
                    continue
                if tier and c.get("instrument_version") != "instr1":
                    continue
                r = member(c, "pair", horizon, ("shadow_baseline_decisions", b))
                r["member_id"] = b["baseline_decision_id"]
                a, control = measure(c, horizon), measure(c, horizon, baseline=b)
                complete = a["status"] == control["status"] == "complete"
                r["measurement"] = {"status": "complete" if complete else "blocked",
                    "value": a["value"] - control["value"] if complete else None,
                    "reason": "both_reconstructed" if complete else f"ai:{a['reason']}|control:{control['reason']}",
                    "ai_measurement_id": a["measurement_id"], "control_measurement_id": control["measurement_id"]}
                r["rule_version"], r["baseline_input_sha"] = rule, b.get("input_sha")
                rows.append(r)
            details = {"rule": rule, "tier": tier,
                       "primary_proposed": _available_summary([r for r in rows if r["primary_decision"] == "propose"], "paired_mean"),
                       "final_proposed": _available_summary([r for r in rows if r["final_decision"] == "propose"], "paired_mean"),
                       "all_rule_opportunities_are_not_original_proposed_population": True}
        elif name == "H-TQS-1":
            scores = _latest([s for s in data["tqs_scores"] if s["source_type"] == "candidate"
                              and not s["is_mock"] and s["data_quality_status"] == "ok" and finite(s.get("tqs_score"))])
            population = [s for cid, s in scores.items() if cid in candidates]
            ranks = rank_membership(population, "tqs_score", .25)
            for s in population:
                r = member(candidates[s["candidate_id"]], ranks[s["candidate_id"]], 3, ("tqs_scores", s))
                r["score"] = s["tqs_score"]
                rows.append(r)
            details = {"ranking": "draft_fixed_count_disjoint_tails_score_then_candidate_id",
                       "ranking_uses_outcomes": False, "middle_population_retained": True,
                       "original_endpoint_substitution_removed": True}
        elif name in {"H-INT-1", "H-INT-SHADOW-1"}:
            tier = int(name == "H-INT-SHADOW-1")
            population = [c for c in candidates.values() if (c.get("shadow_tier") or 0) == tier
                          and (not tier or c.get("instrument_version") == "instr1") and finite(c.get("interest_score"))]
            ranks = rank_membership(population, "interest_score", .1)
            for c in population:
                r = member(c, "population", horizon)
                r.update({"selected": ranks[c["candidate_id"]] == "tested", "score": c["interest_score"]})
                rows.append(r)
            details = {"tier": tier, "reference_includes_selected": True,
                       "ranking": "draft_fixed_count_top_decile_score_then_candidate_id"}
        elif name in {"H-CAT-1", "H-POL-1"}:
            table, field = ("candidate_catalysts", "catalyst_status") if name == "H-CAT-1" else ("last30days_polarity", "direction_alignment")
            selected = _latest(data[table])
            arms = {"confirmed": "tested", "none_found": "reference"} if name == "H-CAT-1" else {"conflicting": "tested", "aligned": "reference"}
            for cid, f in selected.items():
                if cid not in candidates:
                    continue
                # Keep missing/unclear labels visible, never recode as controls.
                rows.append(member(candidates[cid], arms.get(f.get(field), "unclassified"), horizon, (table, f)))
            details = {"feature_counts": dict(Counter(r.get(field) for r in selected.values())),
                       "unclassified_not_controls": True, "card_scope_not_silently_inferred": True}
        elif name == "H-WIN-1":
            for c in candidates.values():
                s = scans.get(c.get("scan_batch_id"))
                if c.get("shadow_tier") or not s or s.get("market_session") != "regular":
                    continue
                try:
                    hour = int(s["started_at_sgt"][11:13])
                except (TypeError, ValueError):
                    hour = -1
                arm = "unclassified" if hour < 0 else "tested" if hour < 12 else "reference"
                rows.append(member(c, arm, horizon, ("scan_batches", s)))
            details = {"windows": "original_literal_SGT_before_noon_vs_at_or_after_noon_not_US_morning"}
        elif name == "H-REJ-1":
            for a in data["attribution_records"]:
                if a.get("attribution_type") != "propose_user_rejected" or a.get("is_mock"):
                    continue
                complete = a.get("resolved_status") == "resolved" and finite(a.get("delta_r"))
                rows.append({"member_id": a["attribution_id"], "arm": "tested", "source_refs": [source("attribution_records", a)],
                             "measurement": {"status": "complete" if complete else "blocked", "value": a.get("delta_r") if complete else None,
                                             "reason": "recorded_actual_minus_proposed_legacy_not_reconstructed"}})
            details = {"sign": "actual_minus_proposed", "intervals_present": 0}
        elif name == "H-TTL-1":
            actual = defaultdict(list)
            for o in data["trade_outcomes"]:
                actual[o.get("proposal_id")].append(o)
            fills_by_order = defaultdict(list)
            for fill in data["paper_fills"]:
                if finite(fill.get("qty")) and fill["qty"] > 0:
                    fills_by_order[fill.get("order_id")].append(fill)
            orders = {o["order_id"]: o for o in data["paper_orders"] if not o.get("is_demo")}
            executed = defaultdict(list)
            for pos in data["positions"]:
                order = orders.get(pos.get("order_id"))
                if not pos.get("is_demo") and order and fills_by_order[order["order_id"]]:
                    pid = pos.get("proposal_id") or order.get("proposal_id")
                    executed[pid].extend([("positions", pos), ("paper_orders", order)])
                    executed[pid].extend(("paper_fills", fill) for fill in fills_by_order[order["order_id"]])
            for p in data["trade_proposals"]:
                if p.get("is_demo"):
                    continue
                expired = p.get("status") == "expired" or bool(p.get("expired_at_utc"))
                if not expired and p["proposal_id"] not in executed:
                    continue
                refs = [source("trade_proposals", p)]
                if expired:
                    c = candidates.get(p.get("candidate_id"))
                    measurement = measure(c, horizon) if c else {"status": "blocked", "value": None, "reason": "missing_candidate"}
                else:
                    refs.extend(source(t, r) for t, r in executed[p["proposal_id"]])
                    fills = actual[p["proposal_id"]]
                    refs.extend(source("trade_outcomes", o) for o in fills)
                    measurement = {"status": "blocked", "value": None, "reason": "realized_cost_and_original_risk_basis_requires_review",
                                   "recorded_realized_r": [o.get("realized_r") for o in fills]}
                    if not fills:
                        measurement["reason"] = "executed_entry_outcome_still_open_or_missing"
                rows.append({"member_id": p["proposal_id"], "arm": "tested" if expired else "reference", "source_refs": refs,
                             "measurement": measurement})
            details = {"expired_history_retained": True, "executed_reference_is_not_replay": True,
                       "reference_membership_from_entry_fills_not_closed_outcomes": True,
                       "executed_proposals": len(executed), "closed_reference_records": sum(len(actual[p]) for p in executed)}
        else:  # PER: fixed population BEFORE seeing maturity or outcome values.
            caches = {str(e["id"]): e for e in data["earnings_calendar_cache"]}
            per = fixed_per_membership(list(candidates.values()), data["earnings_calendar_cache"], outcomes)
            for c, arm in [(e, "tested") for e in per["events"]] + [(c, "reference") for c in per["controls"]]:
                r = member(c, arm, None)
                event = caches.get(str(c.get("card_assignment_ref")))
                if event:
                    r["source_refs"].append(source("earnings_calendar_cache", event))
                co = outcomes.get(c["candidate_id"], {})
                r["measurement"] = {"status": "blocked", "value": None,
                    "reason": "saved_market_adjusted_values_lack_common_start_mark_receipt",
                    "recorded_market_adjusted_return_5d_pct": co.get("market_adjusted_return_5d_pct")}
                r["event_ref"] = c.get("card_assignment_ref")
                if arm == "tested":
                    r.update({"event_key": c["event_key"], "control_ids": c["control_ids"], "control_rung": c["control_rung"]})
                rows.append(r)
            engineering.append("common_start_mark_market_adjusted_measurement_receipt_collector")
            details = {"not_independent_four_studies": True, "earliest_original_analysis": "2026-10-15",
                       **{k: v for k, v in per.items() if k not in {"events", "controls"}},
                       "event_card_versions": dict(Counter(str(e.get("card_version")) for e in per["events"])),
                       "event_selector_versions": dict(Counter(str(e.get("card_selector_version")) for e in per["events"])),
                       "event_content_hash_receipts_missing": len(per["events"])}
        members_digest = canonical_sha256(rows)
        family = {"contract": draft, "contract_sha256": canonical_sha256(draft), "members": rows, "membership_sha256": members_digest,
                  "details": details, "summary": _available_summary(rows, draft["statistic"]),
                  "inference": {"status": "not_enrolled_prospective_study", "formal_allowed": False,
                                "blockers": draft["shared_requirements"] + draft["required_choices"]},
                  "missing_engineering": engineering,
                  "implementation_status": "cohort_export_and_statistic_primitive" if not engineering else "partial_export_missing_measurement_adapter"}
        families.append(family)
    return {"version": "successor_cohort_export_v1", "provenance": "retrospective_snapshot_export",
            "as_of_utc": as_of_utc, "snapshot_sha256": snapshot_sha256, "formal_allowed": False,
            "families": families, "source_rows": source_rows, "source_rows_sha256": canonical_sha256(source_rows),
            "measurements": measurements, "measurements_sha256": canonical_sha256(measurements),
            "inventory_counts": original_counts, "available_source_counts": {t: len(rows) for t, rows in data.items()},
            "unavailable_sources": unavailable_sources,
            "warnings": ["source_hashes_pin_integrity_not_timely_enrollment", "bar_feed_and_gap_cost_limitations",
                         "available_member_descriptions_may_have_nonrandom_missingness", "no_old_quarantine_or_verdict_changed",
                         "filtering_timestamps_does_not_reconstruct_a_prior_mutable_snapshot"]}
