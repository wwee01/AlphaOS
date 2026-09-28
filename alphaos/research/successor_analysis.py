"""Executable bridge from preserved cohort exports to draft inference.

Unsupported or incomplete inputs remain explicit. No complete-case filtering
can remove a missing selected tail member or an unresolved paired control.
"""
from __future__ import annotations

from alphaos.research.successor_inference import METHOD, bundle_pins, evaluate_frozen
from alphaos.stats.frozen_policy_inference import canonical_sha256


def analyze_export(bundle: dict, family: dict, *, paired_population: str | None = None,
                   resamples: int = 2000, seed: int = 20260928,
                   dependence_buffer_seconds: int = 86400) -> dict:
    """Analyze the declared full exported membership, not just measured rows.

The one-day buffer and 20% concentration bound are explicit draft sensitivity
settings, not silently substituted original contract terms. All legacy data
stay retrospective. Changing settings produces a different pinned draft.
"""
    draft = family["contract"]
    if (canonical_sha256(draft) != family["contract_sha256"]
            or canonical_sha256(family["members"]) != family["membership_sha256"]
            or canonical_sha256(bundle["source_rows"]) != bundle["source_rows_sha256"]
            or canonical_sha256(bundle["measurements"]) != bundle["measurements_sha256"]):
        return {"status": "invalid_export_identity", "formal_allowed": False}
    stat = draft["statistic"]
    if stat == "paired_mean" and paired_population not in {"primary_proposed", "final_proposed"}:
        return {"status": "explicit_proposed_population_required", "formal_allowed": False}
    # We do not turn undefined horizons, equivalence margins or unreviewed
    # stored-reference measurements into a supported formal contract.
    if draft["horizon_sessions"] is None or draft["direction"] is None or draft["effect_target"] is None:
        return {"status": "unresolved_contract", "blockers": draft["required_choices"], "formal_allowed": False}
    if stat == "matched_event_excess":
        return {"status": "measurement_receipts_required", "blockers": family["missing_engineering"],
                "counts": family["details"]["counts"], "formal_allowed": False}
    c = {"method": METHOD, "status": "draft", "statistic": stat, "direction": draft["direction"],
         "effect_target": draft["effect_target"], "confidence_level": .9,
         "resamples": resamples, "seed": seed, "min_components_per_arm": max(2, draft["floor_effective_n"] or 2),
         "max_component_share": .2, "dependence_buffer_seconds": dependence_buffer_seconds,
         "analysis_at_utc": bundle["as_of_utc"], "predecessor_id": draft["predecessor_id"],
         "cohort_provenance": bundle["provenance"], "paired_population": paired_population,
         "draft_contract_sha256": canonical_sha256(draft), "snapshot_sha256": bundle["snapshot_sha256"]}
    members, outcomes = [], []
    for row in family["members"]:
        if stat == "independent_means" and row["arm"] not in {"tested", "reference"}:
            continue  # input-defined middle/unclassified membership, not outcome selection
        if stat == "paired_mean":
            key = "primary_decision" if paired_population == "primary_proposed" else "final_decision"
            if row[key] != "propose":
                continue
            legs = [("tested", row["measurement"]["ai_measurement_id"]),
                    ("reference", row["measurement"]["control_measurement_id"])]
        else:
            legs = [(row["arm"], row["measurement"].get("measurement_id"))]
        for arm, measure_id in legs:
            key = row["member_id"] + ":" + arm
            sources = {k: bundle["source_rows"][k] for k in row["source_refs"]}
            m = {"member_id": key, "observation_id": key, "opportunity_id": row["candidate_id"],
                 "arm": arm, "selected": row.get("selected", False), "input": sources,
                 "input_sha256": canonical_sha256(sources), "dependence_keys": []}
            if stat == "paired_mean":
                m["pair_id"] = row["candidate_id"]
            observation = bundle["measurements"].get(measure_id, {})
            outcome = {"member_id": key, "status": observation.get("status", "missing"),
                       "value": observation.get("value"), "evidence": observation,
                       "evidence_sha256": canonical_sha256(observation),
                       "start_utc": observation.get("measurement_start_utc"),
                       "end_utc": observation.get("dependence_end_utc"), "observed_at_utc": bundle["as_of_utc"]}
            members.append(m)
            outcomes.append(outcome)
    pins = bundle_pins(c, members, outcomes)
    result = evaluate_frozen(c, members, outcomes, expected_pins=pins)
    return {"contract": c, "members": members, "outcomes": outcomes, "pins": pins, "result": result,
            "formal_allowed": False, "timely_enrollment_proven": False}
