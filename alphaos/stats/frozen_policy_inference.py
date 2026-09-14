"""Draft-only, pure two-arm inference over externally frozen portfolio evidence.

No journal, registration, broker, activation or promotion interface exists here.
Both arms travel together in each dependence-component bootstrap draw. See
docs/research/frozen-policy-inference-draft-20260915.md for the estimand,
assumptions, input contract, mathematical basis and limitations.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

METHOD = "frozen_two_arm_interval_components_percentile_v1"
ARMS = {"control", "challenger"}


class EvidenceError(ValueError):
    """The whole frozen bundle is invalid; no rows may be silently removed."""


def canonical_sha256(value: Any) -> str:
    """JSON preimages, sorted keys, UTF-8, no NaN/Infinity or coercion."""
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError) as exc:
        raise EvidenceError("noncanonical_json") from exc
    return hashlib.sha256(raw).hexdigest()


def membership_sha256(members: list[dict]) -> str:
    return canonical_sha256(sorted(members, key=lambda r: _text(r, "member_id")))


def outcomes_sha256(outcomes: list[dict]) -> str:
    return canonical_sha256(sorted(outcomes, key=lambda r: _text(r, "member_id")))


def _text(row: dict, key: str) -> str:
    if not isinstance(row, dict):
        raise EvidenceError("mapping_required")
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"missing_or_invalid_{key}")
    return value


def _time(row: dict, key: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(_text(row, key).replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError(f"invalid_timestamp_{key}") from exc
    if parsed.utcoffset() is None:
        raise EvidenceError(f"timezone_required_{key}")
    return parsed.astimezone(timezone.utc)


def _number(row: dict, key: str) -> float:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise EvidenceError(f"finite_number_required_{key}")
    return float(value)


def _integer(row: dict, key: str, low: int, high: int) -> int:
    value = row.get(key)
    if type(value) is not int or not low <= value <= high:
        raise EvidenceError(f"invalid_integer_{key}")
    return value


def _hash_matches(preimage: Any, expected: str, name: str) -> None:
    if canonical_sha256(preimage) != expected:
        raise EvidenceError(f"{name}_identity_mismatch")


def _validate_contract(contract: dict) -> None:
    if contract.get("method") != METHOD or contract.get("status") != "draft":
        raise EvidenceError("only_this_draft_method_is_supported")
    if _text(contract, "design") not in {"paired", "unpaired"}:
        raise EvidenceError("explicit_paired_or_unpaired_design_required")
    if _text(contract, "direction") not in {"greater", "less"}:
        raise EvidenceError("direction_must_be_greater_or_less_not_equivalence")
    if _text(contract, "provenance") not in {"prospective", "synthetic"}:
        raise EvidenceError("legacy_and_retrospective_cohorts_not_supported")
    _text(contract, "protocol_id")
    _text(contract, "independence_assumption")
    _number(contract, "effect_target")
    _integer(contract, "min_components_per_arm", 2, 100000)
    _integer(contract, "resamples", 100, 100000)
    _integer(contract, "seed", 0, 2**63 - 1)
    _integer(contract, "dependence_buffer_seconds", 0, 366 * 86400)
    if not 0.5 < _number(contract, "confidence_level") < 1:
        raise EvidenceError("invalid_confidence_level")
    if not 0 < _number(contract, "max_component_share") < 1:
        raise EvidenceError("invalid_max_component_share")
    frozen = _time(contract, "frozen_at_utc")
    start = _time(contract, "enrollment_start_utc")
    end = _time(contract, "enrollment_end_utc")
    analysis = _time(contract, "analysis_at_utc")
    if not frozen <= start <= end <= analysis:
        raise EvidenceError("contract_time_order_invalid")
    for key in ("control_policy", "challenger_policy", "measurement"):
        if not isinstance(contract.get(key), dict) or not contract[key]:
            raise EvidenceError(f"saved_{key}_preimage_required")
    # Only this concrete family/metric is implemented. R-differences and
    # realized-only selected winners cannot silently become portfolio value.
    measure = contract["measurement"]
    if measure.get("metric") != "net_portfolio_return_fraction":
        raise EvidenceError("unsupported_metric")
    for key in ("cost_method", "fill_method", "mark_method", "opportunity_membership_method"):
        _text(measure, key)
    if measure.get("basis") != "fixed_window_mark_to_market":
        raise EvidenceError("fixed_window_mark_to_market_required")
    if _number(measure, "starting_capital") <= 0:
        raise EvidenceError("positive_starting_capital_required")
    _integer(measure, "window_seconds", 1, 366 * 86400)


def _validated_rows(contract: dict, members: list[dict], outcomes: list[dict]) -> tuple[list[dict], list[str]]:
    by_member = {}
    for member in members:
        key = _text(member, "member_id")
        if key in by_member:
            raise EvidenceError("duplicate_member_id")
        by_member[key] = member
    by_outcome = {}
    for outcome in outcomes:
        key = _text(outcome, "member_id")
        if key in by_outcome:
            raise EvidenceError("duplicate_outcome_member_id")
        by_outcome[key] = outcome
    if set(by_member) != set(by_outcome):
        raise EvidenceError("missing_or_extra_outcome_membership")
    if not members:
        return [], ["no_enrolled_units"]
    rows = []
    blockers = []
    pairs: dict[str, list[dict]] = defaultdict(list)
    opportunity_arms = set()
    for key in sorted(by_member):
        member, outcome = by_member[key], by_outcome[key]
        arm = _text(member, "arm")
        if arm not in ARMS:
            raise EvidenceError("unknown_arm")
        if member.get("provenance") != contract["provenance"]:
            raise EvidenceError("mixed_or_retrospective_provenance")
        _hash_matches(contract[f"{arm}_policy"], _text(member, "policy_sha256"), "policy")
        _hash_matches(contract["measurement"], _text(member, "measurement_sha256"), "measurement")
        if not isinstance(member.get("input"), dict) or not member["input"]:
            raise EvidenceError("saved_input_preimage_required")
        _hash_matches(member["input"], _text(member, "input_sha256"), "input")
        if not isinstance(outcome.get("evidence"), dict) or not outcome["evidence"]:
            raise EvidenceError("saved_outcome_evidence_required")
        _hash_matches(outcome["evidence"], _text(outcome, "evidence_sha256"), "outcome_evidence")
        enrolled = _time(member, "enrolled_at_utc")
        decision = _time(member, "decision_at_utc")
        start = _time(member, "measurement_start_utc")
        end = _time(member, "measurement_end_utc")
        if not (_time(contract, "enrollment_start_utc") <= enrolled <= decision
                <= _time(contract, "enrollment_end_utc") and decision <= start < end
                <= _time(contract, "analysis_at_utc")):
            raise EvidenceError("member_time_order_or_window_invalid")
        if (end - start).total_seconds() != contract["measurement"]["window_seconds"]:
            raise EvidenceError("measurement_duration_mismatch")
        if _time(member, "input_available_at_utc") > decision:
            raise EvidenceError("lookahead_input")
        if _time(member, "recorded_at_utc") > start:
            raise EvidenceError("retrospective_membership_recording")
        if _time(member, "recorded_at_utc") < enrolled:
            raise EvidenceError("recording_before_enrollment")
        opportunity = _text(member, "opportunity_id")
        identity = (opportunity, arm)
        if identity in opportunity_arms:
            raise EvidenceError("duplicate_opportunity_arm")
        opportunity_arms.add(identity)
        keys = member.get("dependence_keys")
        if (not isinstance(keys, list) or not keys or
                any(not isinstance(x, str) or not x.strip() for x in keys) or len(set(keys)) != len(keys)):
            raise EvidenceError("explicit_unique_dependence_keys_required")
        # A capital path cannot be split into independent daily rows. Keep
        # this identity separately compulsory even if optional keys change.
        capital_path = _text(member, "capital_path_id")
        pair_id = member.get("pair_id")
        if contract["design"] == "paired":
            pair_id = _text(member, "pair_id")
            pairs[pair_id].append(member)
        elif pair_id is not None:
            raise EvidenceError("unpaired_design_cannot_use_pair_ids")
        state = _text(outcome, "status")
        if state not in {"complete", "pending", "missing", "invalid"}:
            raise EvidenceError("unknown_outcome_status")
        if state != "complete":
            blockers.append(f"{key}:{state}")
            continue
        observed = _time(outcome, "recorded_at_utc")
        dep_start = _time(outcome, "dependence_start_utc")
        dep_end = _time(outcome, "dependence_end_utc")
        if not (dep_start <= start < end <= dep_end <= observed <= _time(contract, "analysis_at_utc")):
            raise EvidenceError("incomplete_actual_interval_or_future_outcome")
        evidence = outcome["evidence"]
        if evidence.get("cashflow_coverage") != "complete" or evidence.get("cost_coverage") != "complete":
            raise EvidenceError("incomplete_cashflow_or_cost_coverage")
        if _time(evidence, "start_mark_at_utc") != start or _time(evidence, "end_mark_at_utc") != end:
            raise EvidenceError("accounting_mark_window_mismatch")
        denominator = _number(evidence, "return_denominator")
        if denominator != contract["measurement"]["starting_capital"]:
            raise EvidenceError("different_capital_denominator")
        if _number(evidence, "start_equity") != contract["measurement"]["starting_capital"]:
            raise EvidenceError("different_starting_portfolio_equity")
        net = (_number(evidence, "end_equity") - _number(evidence, "start_equity")
               - _number(evidence, "external_net_inflow") - _number(evidence, "costs_not_in_equity"))
        value = _number(outcome, "value")
        if not math.isclose(value, net / denominator, rel_tol=1e-12, abs_tol=1e-12):
            raise EvidenceError("outcome_disagrees_with_saved_net_accounting")
        rows.append({"member_id": key, "arm": arm, "pair_id": pair_id,
                     "value": value, "start": dep_start, "end": dep_end,
                     "keys": [f"path:{capital_path}"] + [f"key:{x}" for x in keys]})
    if contract["design"] == "paired":
        for group in pairs.values():
            if len(group) != 2 or {m["arm"] for m in group} != ARMS:
                raise EvidenceError("paired_membership_requires_exactly_both_arms")
            a, b = group
            for field in ("opportunity_id", "input_sha256", "measurement_sha256"):
                if a[field] != b[field]:
                    raise EvidenceError(f"pair_{field}_mismatch")
            for field in ("decision_at_utc", "measurement_start_utc", "measurement_end_utc"):
                if _time(a, field) != _time(b, field):
                    raise EvidenceError(f"pair_{field}_mismatch")
    return rows, blockers


def _components(rows: list[dict], buffer_seconds: int) -> list[list[dict]]:
    """Connected components of CLOSED actual intervals + shared path/key/pair.

    Cross-symbol and cross-arm overlaps share market shocks. Touching endpoints
    also join. The predeclared buffer joins nearby intervals, but does not prove
    independence beyond it. Transitive overlaps are never counted separately.
    """
    parent = list(range(len(rows)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        parent[find(j)] = find(i)

    owners: dict[str, int] = {}
    for i, row in enumerate(rows):
        keys = row["keys"] + ([f"pair:{row['pair_id']}"] if row["pair_id"] is not None else [])
        for key in keys:
            if key in owners:
                union(i, owners[key])
            owners[key] = i
    # One interval sweep joins an overlapping chain, regardless of keys.
    order = sorted(range(len(rows)), key=lambda i: (rows[i]["start"], rows[i]["member_id"]))
    if order:
        anchor = order[0]
        right = rows[anchor]["end"]
        buffer = timedelta(seconds=buffer_seconds)
        for i in order[1:]:
            if rows[i]["start"] <= right + buffer:
                union(i, anchor)
                right = max(right, rows[i]["end"])
            else:
                anchor, right = i, rows[i]["end"]
    grouped: dict[int, list[dict]] = defaultdict(list)
    for i, row in enumerate(rows):
        grouped[find(i)].append(row)
    return sorted((sorted(group, key=lambda r: r["member_id"]) for group in grouped.values()),
                  key=lambda group: group[0]["member_id"])


def _estimate(rows: list[dict], design: str) -> float:
    if design == "paired":
        pairs: dict[str, dict[str, float]] = defaultdict(dict)
        # Used only on original rows; replicated components use the precomputed
        # pair sums below, so repeated bootstrap IDs never overwrite a draw.
        for row in rows:
            pairs[row["pair_id"]][row["arm"]] = row["value"]
        values = [p["challenger"] - p["control"] for p in pairs.values()]
        return math.fsum(values) / len(values)
    arm_values = {arm: [r["value"] for r in rows if r["arm"] == arm] for arm in ARMS}
    return math.fsum(arm_values["challenger"]) / len(arm_values["challenger"]) - math.fsum(arm_values["control"]) / len(arm_values["control"])


def _quantile(sorted_values: list[float], p: float) -> float:
    index = (len(sorted_values) - 1) * p
    lo = math.floor(index)
    hi = math.ceil(index)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (index - lo)


def evaluate_frozen_policy(
    contract: dict, members: list[dict], outcomes: list[dict], *,
    expected_contract_sha256: str, expected_membership_sha256: str,
    expected_outcomes_sha256: str,
) -> dict[str, Any]:
    """Analyze exactly the externally pinned bundle, never a selected subset.

    Digests must come from the review/enrollment/evidence receipts, not be
    recomputed from edited data and represented as trusted original hashes.
    Hashes detect changes against those receipts; they do not attest truth,
    timestamp authenticity, collector completeness or real independence.
    Returns draft/descriptive information only, including for prospective data.
    """
    base: dict[str, Any] = {"method": METHOD, "formal_evaluation_allowed": False,
                            "promotion_allowed": False, "p_value": None,
                            "contract_sha256": expected_contract_sha256,
                            "membership_sha256": expected_membership_sha256,
                            "outcomes_sha256": expected_outcomes_sha256,
                            "point_estimate": None, "ci_low": None, "ci_high": None,
                            "target_relation": "not_assessed", "blockers": []}
    try:
        if not isinstance(contract, dict) or not isinstance(members, list) or not isinstance(outcomes, list):
            raise EvidenceError("contract_mapping_and_row_lists_required")
        _hash_matches(contract, expected_contract_sha256, "contract")
        if membership_sha256(members) != expected_membership_sha256:
            raise EvidenceError("frozen_membership_identity_mismatch")
        if outcomes_sha256(outcomes) != expected_outcomes_sha256:
            raise EvidenceError("frozen_outcomes_identity_mismatch")
        _validate_contract(contract)
        rows, blockers = _validated_rows(contract, members, outcomes)
        base.update({"design": contract["design"], "provenance": contract["provenance"],
                     "effect_target": contract["effect_target"], "direction": contract["direction"],
                     "confidence_level": contract["confidence_level"], "n_enrolled": len(members),
                     "n_complete": len(rows), "blockers": blockers,
                     "independence_assumption": contract["independence_assumption"]})
        if blockers:
            return {**base, "status": "incomplete_frozen_cohort"}
        counts = Counter(r["arm"] for r in rows)
        if set(counts) != ARMS:
            raise EvidenceError("both_arms_required")
        estimate = _estimate(rows, contract["design"])
        if not math.isfinite(estimate):
            raise EvidenceError("nonfinite_estimate")
        components = _components(rows, contract["dependence_buffer_seconds"])
        summaries = []
        per_arm: Counter[str] = Counter()
        shares = {arm: 0.0 for arm in ARMS}
        sufficient = []
        for group in components:
            ns = Counter(r["arm"] for r in group)
            for arm in ARMS:
                per_arm[arm] += ns[arm] > 0
                shares[arm] = max(shares[arm], ns[arm] / counts[arm])
            summaries.append({"members": [r["member_id"] for r in group],
                              "start_utc": min(r["start"] for r in group).isoformat(),
                              "end_utc": max(r["end"] for r in group).isoformat(),
                              "arm_counts": dict(ns)})
            sufficient.append({arm: (math.fsum(r["value"] for r in group if r["arm"] == arm), ns[arm])
                               for arm in ARMS} | {
                                   "paired_delta": (_estimate(group, "paired") * ns["control"], ns["control"])
                                   if contract["design"] == "paired" else (0.0, 0)})
        sign = 1 if contract["direction"] == "greater" else -1
        base.update({"point_estimate": estimate, "n_by_arm": dict(counts),
                     "directional_margin": sign * (estimate - contract["effect_target"]),
                     "components": summaries, "n_components": len(components),
                     "components_per_arm": dict(per_arm), "largest_component_share": shares,
                     "n_pairs": len(rows) // 2 if contract["design"] == "paired" else None})
        if any(per_arm[a] < contract["min_components_per_arm"] for a in ARMS):
            base["blockers"].append("too_few_dependence_components_per_arm")
        if any(shares[a] > contract["max_component_share"] for a in ARMS):
            base["blockers"].append("dominant_dependence_component")
        if base["blockers"]:
            return {**base, "status": "descriptive_only"}
        rng = random.Random(contract["seed"])
        replicates = []
        invalid = 0
        for _ in range(contract["resamples"]):
            draw = [sufficient[rng.randrange(len(components))] for _ in components]
            totals = {arm: (math.fsum(g[arm][0] for g in draw), sum(g[arm][1] for g in draw))
                      for arm in ARMS}
            if any(totals[a][1] == 0 for a in ARMS):
                invalid += 1
                continue
            if contract["design"] == "paired":
                value = math.fsum(g["paired_delta"][0] for g in draw) / sum(g["paired_delta"][1] for g in draw)
            else:
                value = totals["challenger"][0] / totals["challenger"][1] - totals["control"][0] / totals["control"][1]
            if not math.isfinite(value):
                raise EvidenceError("nonfinite_bootstrap_replicate")
            replicates.append(value)
        base.update({"requested_resamples": contract["resamples"], "valid_resamples": len(replicates),
                     "invalid_resamples": invalid, "seed": contract["seed"]})
        if invalid:
            # Do not condition on favourable/bootstrap populations retaining
            # both arms and silently drop the other draws.
            return {**base, "status": "descriptive_only", "blockers": ["bootstrap_draws_missing_arm"]}
        replicates.sort()
        if replicates[0] == replicates[-1]:
            return {**base, "status": "descriptive_only", "blockers": ["degenerate_bootstrap_distribution"]}
        alpha = 1 - contract["confidence_level"]
        lo, hi = _quantile(replicates, alpha / 2), _quantile(replicates, 1 - alpha / 2)
        margin_lo, margin_hi = sorted((sign * (lo-contract["effect_target"]), sign * (hi-contract["effect_target"])))
        relation = "target_inside_interval"
        if margin_lo > 0:
            relation = "interval_supports_stated_direction_beyond_target"
        elif margin_hi < 0:
            relation = "interval_opposes_stated_direction_at_target"
        return {**base, "status": "draft_interval", "ci_low": lo, "ci_high": hi,
                "directional_margin_ci_low": margin_lo, "directional_margin_ci_high": margin_hi,
                "target_relation": relation, "bootstrap_distribution_sha256": canonical_sha256(replicates),
                "ci_method": "joint_component_percentile_linear_quantile",
                "limitations": "Approximate cluster bootstrap, conditional on complete truthful inputs and independent components. No p-value, causal claim, multiplicity clearance or formal verdict. A numerical floor does not establish calibration."}
    except (EvidenceError, OverflowError, ZeroDivisionError) as exc:
        return {**base, "status": "invalid_frozen_evidence", "point_estimate": None,
                "ci_low": None, "ci_high": None, "blockers": [str(exc)]}
