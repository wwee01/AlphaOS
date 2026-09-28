"""Draft-only joint multiplier inference for frozen research populations.

Every arm/reference is recomputed in each draw. One positive Exp(1) weight
per connected dependence component preserves membership and reference support.
Intervals are uncentered percentile intervals; diagnostic tail areas use
theta* - theta against theta - target, NOT P(theta* <= 0). This is an
asymptotic cluster-bootstrap approximation, not an exact/randomization test.
No result from this module authorizes finalization, promotion or trading.
"""
from __future__ import annotations

import math
import random
from collections import defaultdict
from datetime import timedelta

from alphaos.research.successor_measurement import finite, utc
from alphaos.stats.frozen_policy_inference import canonical_sha256

METHOD = "successor_joint_interval_multiplier_v1"
STATISTICS = {"paired_mean", "independent_means", "top_mean_minus_population_median",
              "single_mean", "matched_event_excess"}


def bundle_pins(contract: dict, members: list[dict], outcomes: list[dict]) -> dict:
    """Caller must retain these outside the mutable bundle; not a time receipt."""
    return {"contract": canonical_sha256(contract), "members": canonical_sha256(members),
            "outcomes": canonical_sha256(outcomes)}


def _mean(values: list[tuple[float, float]]) -> float:
    if not values or sum(w for _, w in values) <= 0:
        raise ValueError("empty_statistic_support")
    return sum(v * w for v, w in values) / sum(w for _, w in values)


def _median(values: list[tuple[float, float]]) -> float:
    ordered = sorted(values)
    half, cumulative = sum(w for _, w in ordered) / 2, 0.
    if not ordered or half <= 0:
        raise ValueError("empty_median_population")
    for i, (v, w) in enumerate(ordered):
        cumulative += w
        if math.isclose(cumulative, half, rel_tol=1e-14, abs_tol=1e-14) and i + 1 < len(ordered):
            return (v + ordered[i + 1][0]) / 2
        if cumulative >= half:
            return v
    return ordered[-1][0]


def _validate_members(statistic: str, members: list[dict]) -> None:
    ids = [m["member_id"] for m in members]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate_member_identity")
    observations = [m["observation_id"] for m in members]
    if len(set(observations)) != len(observations):
        raise ValueError("duplicated_underlying_observation")
    if statistic == "paired_mean":
        pairs: dict[str, list[dict]] = defaultdict(list)
        for m in members:
            pairs[m["pair_id"]].append(m)
        for pair in pairs.values():
            if len(pair) != 2 or {m["arm"] for m in pair} != {"tested", "reference"}:
                raise ValueError("pair_requires_exactly_both_arms")
            if len({m["opportunity_id"] for m in pair}) != 1:
                raise ValueError("pair_opportunity_mismatch")
    elif statistic == "independent_means":
        if {m["arm"] for m in members} != {"tested", "reference"}:
            raise ValueError("two_disjoint_arms_required")
        if len({m["opportunity_id"] for m in members}) != len(members):
            raise ValueError("unpaired_opportunities_must_be_disjoint")
    elif statistic == "top_mean_minus_population_median":
        if any(m["arm"] != "population" or type(m.get("selected")) is not bool for m in members):
            raise ValueError("single_population_with_fixed_top_membership_required")
        if not any(m["selected"] for m in members):
            raise ValueError("empty_top_population")
    elif statistic == "single_mean":
        if any(m["arm"] != "tested" for m in members):
            raise ValueError("single_mean_requires_tested_arm")
    elif statistic == "matched_event_excess":
        refs = {m["member_id"] for m in members if m["arm"] == "reference"}
        events = [m for m in members if m["arm"] == "tested"]
        if not events or not refs or any(m["arm"] not in {"tested", "reference"} for m in members):
            raise ValueError("events_and_references_required")
        for e in events:
            controls = e.get("control_ids")
            if (not isinstance(controls, list) or not controls or len(set(controls)) != len(controls)
                    or not set(controls) <= refs):
                raise ValueError("fixed_unique_control_membership_required")


def statistic_value(statistic: str, rows: list[dict], weights: dict[str, float] | None = None) -> float:
    """Pure point statistic; caller validates full membership before inference."""
    weights = weights or {r["member_id"]: 1. for r in rows}
    values = {r["member_id"]: (r["value"], weights[r["member_id"]]) for r in rows}
    if statistic == "single_mean":
        return _mean(list(values.values()))
    if statistic == "top_mean_minus_population_median":
        return _mean([values[r["member_id"]] for r in rows if r["selected"]]) - _median(list(values.values()))
    if statistic == "paired_mean":
        pairs: dict[str, dict] = defaultdict(dict)
        for r in rows:
            pairs[r["pair_id"]][r["arm"]] = r
        contrasts = []
        for p in pairs.values():
            a, b = p["tested"], p["reference"]
            if weights[a["member_id"]] != weights[b["member_id"]]:
                raise ValueError("paired_weights_must_travel_together")
            contrasts.append((a["value"] - b["value"], weights[a["member_id"]]))
        return _mean(contrasts)
    if statistic == "matched_event_excess":
        return _mean([(r["value"] - _mean([values[k] for k in r["control_ids"]]), weights[r["member_id"]])
                      for r in rows if r["arm"] == "tested"])
    if statistic == "independent_means":
        return (_mean([values[r["member_id"]] for r in rows if r["arm"] == "tested"])
                - _mean([values[r["member_id"]] for r in rows if r["arm"] == "reference"]))
    raise ValueError("unsupported_statistic")


def joint_components(rows: list[dict], buffer_seconds: int) -> list[list[str]]:
    """Closed actual intervals across ALL symbols/arms plus shared keys/pairs.

Transitive overlap is intentional. Shared controls are single observations;
their reuse also links all consuming events. Missing intervals are errors.
"""
    parents = list(range(len(rows)))

    def root(i: int) -> int:
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    def union(a: int, b: int) -> None:
        parents[root(a)] = root(b)

    ordered = sorted((utc(r["start_utc"]), utc(r["end_utc"]), i) for i, r in enumerate(rows))
    end, representative = None, 0
    for start, stop, i in ordered:
        if stop < start:
            raise ValueError("reversed_measurement_interval")
        if end is not None and start <= end:
            union(i, representative)
            end = max(end, stop + timedelta(seconds=buffer_seconds))
        else:
            end, representative = stop + timedelta(seconds=buffer_seconds), i
    keys: dict[str, int] = {}
    ids = {r["member_id"]: i for i, r in enumerate(rows)}
    for i, r in enumerate(rows):
        shared = list(r["dependence_keys"])
        if r.get("pair_id"):
            shared.append("pair:" + r["pair_id"])
        for k in shared:
            if k in keys:
                union(i, keys[k])
            keys[k] = i
        for control in r.get("control_ids", []):
            union(i, ids[control])
    groups: dict[int, list[str]] = defaultdict(list)
    for i, r in enumerate(rows):
        groups[root(i)].append(r["member_id"])
    return sorted((sorted(g) for g in groups.values()), key=lambda g: g[0])


def evaluate_frozen(contract: dict, members: list[dict], outcomes: list[dict], *, expected_pins: dict) -> dict:
    """Pure, nonpersisting draft analysis; any missing member blocks inference."""
    base: dict = {"method": METHOD, "formal_allowed": False, "promotion_allowed": False,
                  "status": "invalid_frozen_evidence", "point_estimate": None,
                  "interval": None, "diagnostic_p": None, "blockers": []}
    try:
        if not isinstance(contract, dict):
            raise ValueError("mapping_contract_required")
        if bundle_pins(contract, members, outcomes) != expected_pins:
            raise ValueError("external_input_identity_mismatch")
        if contract.get("status") != "draft" or contract.get("method") != METHOD:
            raise ValueError("only_versioned_draft_contract_supported")
        statistic = contract["statistic"]
        if statistic not in STATISTICS:
            raise ValueError("unsupported_statistic")
        direction = contract["direction"]
        if direction not in {"greater", "less", "two_sided", "equivalent"}:
            raise ValueError("explicit_direction_required")
        target = contract["effect_target"]
        if not finite(target):
            raise ValueError("finite_effect_target_required")
        b, seed = contract["resamples"], contract["seed"]
        nmin, buffer = contract["min_components_per_arm"], contract["dependence_buffer_seconds"]
        share, ci = contract["max_component_share"], contract["confidence_level"]
        if (type(b) is not int or not 100 <= b <= 100000 or type(seed) is not int or seed < 0
                or type(nmin) is not int or nmin < 2 or type(buffer) is not int or buffer < 0
                or not finite(share) or not 0 < share < 1 or not finite(ci) or not .5 < ci < 1):
            raise ValueError("invalid_resampling_or_dependence_contract")
        if direction == "equivalent" and (not finite(contract.get("equivalence_margin")) or contract["equivalence_margin"] <= 0):
            raise ValueError("positive_equivalence_margin_required")
        if not isinstance(members, list) or not isinstance(outcomes, list):
            raise ValueError("membership_and_outcomes_must_be_lists")
        for m in members:
            if not isinstance(m, dict):
                raise ValueError("malformed_member")
            for key in ("member_id", "observation_id", "opportunity_id", "arm"):
                if not isinstance(m.get(key), str) or not m[key]:
                    raise ValueError("missing_member_identity")
            if not isinstance(m.get("input"), dict) or not m["input"]:
                raise ValueError("saved_input_preimage_required")
            if canonical_sha256(m["input"]) != m.get("input_sha256"):
                raise ValueError("member_input_identity_mismatch")
            keys = m.get("dependence_keys")
            if not isinstance(keys, list) or any(not isinstance(k, str) or not k for k in keys):
                raise ValueError("explicit_dependence_keys_required")
        if not members:
            return {**base, "status": "insufficient_data", "blockers": ["no_members"]}
        _validate_members(statistic, members)
        by_outcome = {}
        for o in outcomes:
            if not isinstance(o, dict) or not isinstance(o.get("member_id"), str):
                raise ValueError("malformed_outcome")
            if o["member_id"] in by_outcome:
                raise ValueError("duplicate_outcome")
            by_outcome[o["member_id"]] = o
        if set(by_outcome) != {m["member_id"] for m in members}:
            raise ValueError("missing_or_extra_outcome_membership")
        incomplete = [k for k, o in by_outcome.items() if o.get("status") != "complete"]
        if incomplete:
            return {**base, "status": "incomplete_cohort", "blockers": ["missing_outcomes_no_complete_case_inference"],
                    "incomplete_member_ids": sorted(incomplete), "n_members": len(members)}
        rows = []
        for m in members:
            o = by_outcome[m["member_id"]]
            if not finite(o.get("value")):
                raise ValueError("finite_outcome_required")
            if not isinstance(o.get("evidence"), dict) or not o["evidence"]:
                raise ValueError("saved_outcome_preimage_required")
            if canonical_sha256(o["evidence"]) != o.get("evidence_sha256"):
                raise ValueError("outcome_evidence_identity_mismatch")
            start, end = utc(o["start_utc"]), utc(o["end_utc"])
            if not start <= end <= utc(o["observed_at_utc"]) <= utc(contract["analysis_at_utc"]):
                raise ValueError("invalid_actual_interval_or_future_observation")
            rows.append({**m, "value": o["value"], "start_utc": o["start_utc"], "end_utc": o["end_utc"]})
        estimate = statistic_value(statistic, rows)
        components = joint_components(rows, buffer)
        by_id = {r["member_id"]: r for r in rows}
        groups = {r["arm"] for r in rows}
        if statistic == "top_mean_minus_population_median":
            groups.add("selected")
        support = {}
        concentration = {}
        for arm in sorted(groups):
            counts = [sum((by_id[k].get("selected") is True if arm == "selected" else by_id[k]["arm"] == arm) for k in comp) for comp in components]
            support[arm] = sum(n > 0 for n in counts)
            concentration[arm] = max(counts) / sum(counts)
        base.update({"status": "insufficient_dependence_support", "point_estimate": estimate,
                     "n_members": len(rows), "n_components": len(components),
                     "component_members": components, "components_per_arm": support,
                     "max_component_share_per_arm": concentration, "target": target, "direction": direction})
        if min(support.values()) < nmin or max(concentration.values()) > share:
            base["blockers"] = ["too_few_or_concentrated_joint_components"]
            return base
        rng = random.Random(seed)
        draws = []
        for _ in range(b):
            weights = {}
            for comp in components:
                w = max(rng.expovariate(1.), 1e-300)
                for k in comp:
                    weights[k] = w
            draws.append(statistic_value(statistic, rows, weights))
        ordered = sorted(draws)
        tail = (1 - ci) / 2

        def quantile(q: float) -> float:
            x = q * (b - 1)
            lo = int(x)
            hi = min(lo + 1, b - 1)
            return ordered[lo] * (hi - x) + ordered[hi] * (x - lo) if hi != lo else ordered[lo]

        noise = [v - estimate for v in draws]

        def p_at(value: float, side: str) -> float:
            observed = estimate - value
            tolerance = 1e-12 * max(1., abs(estimate), abs(value))
            if side == "greater":
                count = sum(v >= observed - tolerance for v in noise)
            elif side == "less":
                count = sum(v <= observed + tolerance for v in noise)
            else:
                count = sum(abs(v) >= abs(observed) - tolerance for v in noise)
            return (count + 1) / (b + 1)

        if direction == "equivalent":
            margin = contract["equivalence_margin"]
            p = max(p_at(target - margin, "greater"), p_at(target + margin, "less"))
        else:
            p = p_at(target, direction)
        base.update({"status": "draft_inference", "blockers": [],
                     "interval": [quantile(tail), quantile(1 - tail)], "confidence_level": ci,
                     "diagnostic_p": p, "resamples": b, "seed": seed,
                     "p_interpretation": "approximate_null_centered_joint_multiplier_not_formal_or_FDR",
                     "assumption": "components_exchangeable_independent_after_declared_dependence_links_and_buffer"})
        return base
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        return {**base, "status": "invalid_frozen_evidence", "blockers": [str(exc)]}
