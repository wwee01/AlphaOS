"""Synthetic adversarial evidence; no application store or external calls."""
from __future__ import annotations

import copy
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from alphaos.stats.frozen_policy_inference import (
    METHOD, canonical_sha256, evaluate_frozen_policy, membership_sha256,
    outcomes_sha256,
)


def fixture(control=None, challenger=None, design="paired"):
    control = control if control is not None else [i / 1000 for i in range(40)]
    challenger = challenger if challenger is not None else [x + 0.1 + (i % 3) / 100 for i, x in enumerate(control)]
    contract = {
        "method": METHOD, "status": "draft", "protocol_id": "SYNTHETIC-TEST-ONLY",
        "provenance": "synthetic", "design": design, "direction": "greater",
        "effect_target": 0.05, "confidence_level": 0.95, "seed": 20260915,
        "resamples": 999, "min_components_per_arm": 10, "max_component_share": 0.2,
        "dependence_buffer_seconds": 0,
        "frozen_at_utc": "2026-01-01T00:00:00Z", "enrollment_start_utc": "2026-01-02T00:00:00Z",
        "enrollment_end_utc": "2026-12-31T00:00:00Z", "analysis_at_utc": "2027-01-01T00:00:00Z",
        "independence_assumption": "Independent synthetic portfolio episodes; never inferred for actual market data.",
        "control_policy": {"name": "frozen-current", "version": 1},
        "challenger_policy": {"name": "watch-advisory", "version": 1},
        "measurement": {"metric": "net_portfolio_return_fraction", "starting_capital": 100.0,
                        "window_seconds": 3600,
                        "basis": "fixed_window_mark_to_market", "cost_method": "synthetic-observed-v1",
                        "fill_method": "synthetic-v1", "mark_method": "synthetic-v1",
                        "opportunity_membership_method": "all-synthetic-episodes-v1"},
    }
    members, outcomes = [], []
    for arm, values in (("control", control), ("challenger", challenger)):
        for i, value in enumerate(values):
            day = i * 2 if design == "paired" else i * 2 + (arm == "challenger")
            start = datetime(2026, 1, 2, tzinfo=timezone.utc) + timedelta(days=day)
            end = start + timedelta(hours=1)
            key = f"{arm}-{i:03d}"
            member = {
                "member_id": key, "arm": arm, "pair_id": f"pair-{i}" if design == "paired" else None,
                "opportunity_id": f"episode-{i}" if design == "paired" else key,
                "capital_path_id": key, "dependence_keys": [f"market-episode-{day}"],
                "provenance": "synthetic", "enrolled_at_utc": start.isoformat(),
                "decision_at_utc": start.isoformat(), "recorded_at_utc": start.isoformat(),
                "input_available_at_utc": (start-timedelta(seconds=1)).isoformat(),
                "measurement_start_utc": start.isoformat(), "measurement_end_utc": end.isoformat(),
                "input": {"episode": i if design == "paired" else key, "available_information": "synthetic"},
                "policy_sha256": canonical_sha256(contract[f"{arm}_policy"]),
                "measurement_sha256": canonical_sha256(contract["measurement"]),
            }
            member["input_sha256"] = canonical_sha256(member["input"])
            evidence = {"start_equity": 100.0, "end_equity": 100.0 + value * 100.0,
                        "external_net_inflow": 0.0, "costs_not_in_equity": 0.0,
                        "return_denominator": 100.0, "cashflow_coverage": "complete", "cost_coverage": "complete",
                        "start_mark_at_utc": start.isoformat(), "end_mark_at_utc": end.isoformat(),
                        "source": "synthetic accounting fixture"}
            outcome = {"member_id": key, "status": "complete", "value": value,
                       "dependence_start_utc": start.isoformat(), "dependence_end_utc": end.isoformat(),
                       "recorded_at_utc": (end+timedelta(seconds=1)).isoformat(),
                       "evidence": evidence, "evidence_sha256": canonical_sha256(evidence)}
            members.append(member)
            outcomes.append(outcome)
    return contract, members, outcomes


def pins(contract, members, outcomes):
    return {"expected_contract_sha256": canonical_sha256(contract),
            "expected_membership_sha256": membership_sha256(members),
            "expected_outcomes_sha256": outcomes_sha256(outcomes)}


def run(bundle):
    return evaluate_frozen_policy(*bundle, **pins(*bundle))


def change_evidence(outcome, **values):
    outcome["evidence"].update(values)
    outcome["evidence_sha256"] = canonical_sha256(outcome["evidence"])


def test_target_is_stated_effect_not_zero_and_less_direction_is_not_reversed():
    data = fixture()
    result = run(data)
    assert result["target_relation"] == "interval_supports_stated_direction_beyond_target"
    data[0]["effect_target"] = 0.2
    result = run(data)
    assert result["point_estimate"] > 0
    assert result["target_relation"] == "interval_opposes_stated_direction_at_target"
    data[0]["direction"] = "less"
    result = run(data)
    assert result["target_relation"] == "interval_supports_stated_direction_beyond_target"
    assert result["p_value"] is None
    assert result["formal_evaluation_allowed"] is result["promotion_allowed"] is False


def test_unpaired_reference_arm_uncertainty_is_resampled():
    control = [-0.2, -0.1, 0.1, 0.2] * 10
    challenger = [0.05 + 0.001 * (i % 2) for i in range(40)]
    noisy = run(fixture(control, challenger, "unpaired"))
    fixed_reference = run(fixture([0.0] * 40, challenger, "unpaired"))
    assert noisy["status"] == fixed_reference["status"] == "draft_interval"
    assert noisy["ci_high"] - noisy["ci_low"] > 20 * (fixed_reference["ci_high"] - fixed_reference["ci_low"])
    assert noisy["ci_low"] < 0.05 < noisy["ci_high"]


def test_paired_common_shock_covariance_is_kept_and_permutation_matters():
    control = [(-1 if i % 2 else 1) * 0.4 for i in range(40)]
    challenger = [x + 0.05 + (i % 3) / 1000 for i, x in enumerate(control)]
    paired = run(fixture(control, challenger))
    unpaired = run(fixture(control, challenger, "unpaired"))
    assert paired["ci_high"] - paired["ci_low"] < 0.01
    assert unpaired["ci_high"] - unpaired["ci_low"] > 0.1
    permuted = run(fixture(control, challenger[::-1]))
    assert permuted["ci_high"] - permuted["ci_low"] > 0.1


def test_frozen_bundle_is_reproducible_order_invariant_and_pure():
    data = fixture()
    original = copy.deepcopy(data)
    result = run(data)
    data[1].reverse()
    random.Random(88).shuffle(data[2])
    assert run(data) == result
    assert sorted(data[1], key=lambda x: x["member_id"]) == sorted(original[1], key=lambda x: x["member_id"])
    assert original[0] == data[0]


@pytest.mark.parametrize("part", [0, 1, 2])
def test_external_receipt_rejects_changed_contract_membership_or_outcomes(part):
    data = fixture()
    expected = pins(*data)
    if part == 0:
        data[0]["effect_target"] = 0
    else:
        data[part].pop()
    result = evaluate_frozen_policy(*data, **expected)
    assert result["status"] == "invalid_frozen_evidence"
    assert result["point_estimate"] is None


@pytest.mark.parametrize("kind", ["missing", "extra", "duplicate", "pending", "invalid"])
def test_no_selected_complete_case_or_dropped_losing_member(kind):
    data = fixture()
    if kind == "missing":
        data[2].pop()
    elif kind == "extra":
        extra = copy.deepcopy(data[2][-1])
        extra["member_id"] = "unregistered"
        data[2].append(extra)
    elif kind == "duplicate":
        data[2].append(copy.deepcopy(data[2][-1]))
    else:
        data[2][-1]["status"] = kind
    result = run(data)
    assert result["status"] in {"invalid_frozen_evidence", "incomplete_frozen_cohort"}
    assert result["point_estimate"] is None


@pytest.mark.parametrize("field,value", [
    ("provenance", "retrospective"), ("provenance", "legacy"),
    ("policy_sha256", "changed"), ("measurement_sha256", "changed"), ("input_sha256", "changed"),
    ("enrolled_at_utc", "2025-01-01T00:00:00Z"),
    ("recorded_at_utc", "2026-12-01T00:00:00Z"),
    ("input_available_at_utc", "2026-12-01T00:00:00Z"),
    ("decision_at_utc", "2026-01-02T00:00:00"),
    ("capital_path_id", ""), ("dependence_keys", []),
])
def test_provenance_identity_and_no_lookahead_enforced(field, value):
    data = fixture()
    data[1][0][field] = value
    assert run(data)["status"] == "invalid_frozen_evidence"


@pytest.mark.parametrize("mutation", ["arm_missing", "other_input", "other_horizon", "duplicate_opportunity"])
def test_paired_design_requires_exact_same_opportunity_information_and_horizon(mutation):
    data = fixture()
    if mutation == "arm_missing":
        data[1].pop()
        data[2].pop()
    elif mutation == "other_input":
        data[1][-1]["input"] = {"different": "extra future analysis"}
        data[1][-1]["input_sha256"] = canonical_sha256(data[1][-1]["input"])
    elif mutation == "other_horizon":
        data[1][-1]["measurement_end_utc"] = "2026-03-21T00:30:00Z"
    else:
        data[1][-1]["opportunity_id"] = data[1][-2]["opportunity_id"]
    assert run(data)["status"] == "invalid_frozen_evidence"


def test_missing_maturity_or_shortened_actual_interval_cannot_inflate_n():
    data = fixture()
    data[2][0]["dependence_end_utc"] = data[1][0]["measurement_start_utc"]
    assert run(data)["status"] == "invalid_frozen_evidence"


@pytest.mark.parametrize("field,value", [
    ("cashflow_coverage", "unknown"), ("cost_coverage", "unknown"),
    ("end_equity", 10000), ("return_denominator", 1000),
    ("end_mark_at_utc", "2026-01-03T00:00:00Z"),
])
def test_account_marks_costs_flows_and_capital_are_part_of_the_outcome(field, value):
    data = fixture()
    change_evidence(data[2][0], **{field: value})
    assert run(data)["status"] == "invalid_frozen_evidence"


def test_deposit_is_not_profit_and_costs_are_subtracted_once():
    data = fixture()
    row = data[2][0]
    change_evidence(row, end_equity=row["evidence"]["end_equity"] + 1010,
                    external_net_inflow=1000, costs_not_in_equity=10)
    result = run(data)
    original = run(fixture())
    for key in ("point_estimate", "ci_low", "ci_high", "bootstrap_distribution_sha256"):
        assert result[key] == original[key]
    assert result["outcomes_sha256"] != original["outcomes_sha256"]


def test_transitive_actual_overlap_across_symbols_and_arms_is_one_component():
    data = fixture(control=[0, 0.01, 0.02], challenger=[0.1, 0.12, 0.14])
    data[0]["min_components_per_arm"] = 2
    # Actual dependencies extend past measured windows, creating a chain:
    # Jan2-Jan4, Jan4-Jan6, Jan6-Jan8. Equality at endpoints also joins.
    for row in data[2]:
        start = datetime.fromisoformat(row["dependence_start_utc"])
        row["dependence_end_utc"] = (start+timedelta(days=2)).isoformat()
        row["recorded_at_utc"] = (start+timedelta(days=2, seconds=1)).isoformat()
    result = run(data)
    assert result["n_components"] == 1
    assert result["status"] == "descriptive_only"
    assert result["ci_low"] is None


def test_continuing_capital_path_cannot_be_split_into_independent_daily_scores():
    data = fixture()
    for row in data[1]:
        row["capital_path_id"] = "same-continuous-two-week-book"
    result = run(data)
    assert result["n_components"] == 1
    assert result["n_pairs"] == 40
    assert result["ci_low"] is None
    assert result["status"] == "descriptive_only"


def test_repeated_symbol_dependence_and_predeclared_temporal_buffer():
    data = fixture()
    for row in data[1]:
        row["dependence_keys"].append("symbol:META")
    assert run(data)["n_components"] == 1
    data = fixture()
    data[0]["dependence_buffer_seconds"] = 48 * 3600
    assert run(data)["n_components"] == 1


def test_large_component_does_not_masquerade_as_many_small_independent_units():
    data = fixture()
    for row in data[1]:
        if int(row["member_id"].split("-")[1]) < 20:
            row["dependence_keys"].append("shared-shock")
    result = run(data)
    assert result["n_components"] == 21
    assert result["largest_component_share"]["control"] == 0.5
    assert result["status"] == "descriptive_only"


def test_invalid_unpaired_bootstrap_draws_are_not_silently_conditioned_away():
    data = fixture([0, 0.1], [0.2, 0.3], "unpaired")
    data[0].update(min_components_per_arm=2, max_component_share=0.9)
    result = run(data)
    assert result["invalid_resamples"] > 0
    assert result["ci_low"] is None
    assert result["blockers"] == ["bootstrap_draws_missing_arm"]


def test_constant_effect_is_not_fabricated_as_zero_uncertainty_confirmation():
    result = run(fixture([0] * 40, [0.1] * 40))
    assert result["status"] == "descriptive_only"
    assert result["blockers"] == ["degenerate_bootstrap_distribution"]


def test_empty_cohort_is_not_observed_not_a_pass():
    contract, _, _ = fixture()
    result = run((contract, [], []))
    assert result["status"] == "incomplete_frozen_cohort"
    assert result["blockers"] == ["no_enrolled_units"]


@pytest.mark.parametrize("value", [True, "0.1", float("nan"), float("inf")])
def test_malformed_values_cannot_enter_bootstrap(value):
    data = fixture()
    expected = pins(*data)
    data[2][0]["value"] = value
    result = evaluate_frozen_policy(*data, **expected)
    assert result["status"] == "invalid_frozen_evidence"


def test_pooled_pair_estimand_not_unweighted_average_of_component_means():
    data = fixture([0] * 4, [1, 0, 0, 0])
    data[0].update(min_components_per_arm=2, max_component_share=0.9)
    for row in data[1]:
        if not row["member_id"].endswith("000"):
            row["dependence_keys"].append("one-large-component")
    result = run(data)
    assert result["n_components"] == 2
    assert result["point_estimate"] == 0.25  # Equal episode weights; mean of cluster means would be0.5.


def test_repeated_within_component_observations_do_not_manufacture_precision():
    data = fixture()
    before = run(data)
    for member, outcome in list(zip(data[1], data[2])):
        for i in range(3):
            m, o = copy.deepcopy(member), copy.deepcopy(outcome)
            m["member_id"] += f"-copy{i}"
            o["member_id"] = m["member_id"]
            m["opportunity_id"] += f"-copy{i}"
            m["pair_id"] += f"-copy{i}"
            data[1].append(m)
            data[2].append(o)
    after = run(data)
    assert after["n_pairs"] == before["n_pairs"] * 4
    assert after["n_components"] == before["n_components"]
    assert after["ci_low"] == pytest.approx(before["ci_low"])
    assert after["ci_high"] == pytest.approx(before["ci_high"])


def test_seeded_independent_gaussian_smoke_check_is_not_a_market_calibration_claim():
    # Predeclared broad binomial acceptance band for100 deterministic fixtures.
    # This catches gross variance/centering regressions; it cannot validate
    # serially dependent, selected, heavy-tailed actual trading observations.
    covered = 0
    for seed in range(100):
        rng = random.Random(seed + 909)
        control = [rng.gauss(0, 0.05) for _ in range(40)]
        challenger = [x + rng.gauss(0.05, 0.02) for x in control]
        data = fixture(control, challenger)
        data[0]["seed"] = seed
        data[0]["resamples"] = 499
        result = run(data)
        assert result["status"] == "draft_interval"
        covered += result["ci_low"] <= 0.05 <= result["ci_high"]
    assert 88 <= covered <= 100


def test_equal_denominator_does_not_allow_unequal_initial_capital_treatment():
    data = fixture()
    for outcome in data[2]:
        if outcome["member_id"].startswith("challenger"):
            change_evidence(outcome, start_equity=1000, end_equity=outcome["evidence"]["end_equity"] + 900)
    result = run(data)
    assert result["status"] == "invalid_frozen_evidence"
    assert result["blockers"] == ["different_starting_portfolio_equity"]


@pytest.mark.parametrize("mutation", ["member", "contract", "members", "arm", "state", "design"])
def test_malformed_container_and_unhashable_tags_fail_closed(mutation):
    data = list(fixture())
    receipts = pins(*data)
    if mutation == "member":
        data[1][0] = 123
    elif mutation == "contract":
        data[0] = None
    elif mutation == "members":
        data[1] = None
    else:
        if mutation == "arm":
            data[1][0]["arm"] = []
        elif mutation == "state":
            data[2][0]["status"] = []
        else:
            data[0]["design"] = []
        receipts = pins(*data)
    result = evaluate_frozen_policy(*data, **receipts)
    assert result["status"] == "invalid_frozen_evidence"
    assert result["point_estimate"] is None


def test_saved_synthetic_contract_membership_outcomes_and_receipts_replay():
    path = Path(__file__).parents[1] / "docs/research/fixtures/frozen-policy-synthetic-20260915.json"
    bundle = json.loads(path.read_text())
    assert bundle["synthetic_only"] is True
    result = evaluate_frozen_policy(bundle["contract"], bundle["members"], bundle["outcomes"], **bundle["receipts"])
    for key, expected in bundle["expected"].items():
        assert result[key] == expected
