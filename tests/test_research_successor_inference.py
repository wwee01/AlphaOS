"""Adversarial draft estimands, not profitability or live-market calibration."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from alphaos.research.successor_inference import (
    METHOD,
    bundle_pins,
    evaluate_frozen,
    statistic_value,
)
from alphaos.stats.frozen_policy_inference import canonical_sha256


def fixture(statistic="paired_mean", values=None, reference=None):
    values = (
        values if values is not None else [0.12 + (i % 5 - 2) / 100 for i in range(24)]
    )
    reference = reference if reference is not None else [0.0] * len(values)
    c = {
        "method": METHOD,
        "status": "draft",
        "statistic": statistic,
        "direction": "greater",
        "effect_target": 0.05,
        "resamples": 399,
        "seed": 28,
        "confidence_level": 0.9,
        "min_components_per_arm": 5,
        "max_component_share": 0.25,
        "dependence_buffer_seconds": 0,
        "analysis_at_utc": "2027-01-01T00:00:00Z",
    }
    members, outcomes = [], []
    arms = (
        [("tested", values)]
        if statistic == "single_mean"
        else [("tested", values), ("reference", reference)]
    )
    if statistic == "top_mean_minus_population_median":
        arms = [("population", values)]
    for arm, items in arms:
        for i, value in enumerate(items):
            key = f"{arm}:{i}"
            start = datetime(2026, 1, 2, tzinfo=timezone.utc) + timedelta(
                days=i * 3
                + int(arm == "reference" and statistic == "independent_means")
            )
            m = {
                "member_id": key,
                "observation_id": key,
                "opportunity_id": str(i) if statistic == "paired_mean" else key,
                "arm": arm,
                "input": {"immutable_opportunity": i},
                "dependence_keys": [],
            }
            m["input_sha256"] = canonical_sha256(m["input"])
            if statistic == "paired_mean":
                m["pair_id"] = str(i)
            if statistic == "top_mean_minus_population_median":
                m["selected"] = i >= len(items) * 0.75
            evidence = {"synthetic_observed_value": value, "source_id": key}
            o = {
                "member_id": key,
                "status": "complete",
                "value": value,
                "evidence": evidence,
                "evidence_sha256": canonical_sha256(evidence),
                "start_utc": start.isoformat(),
                "end_utc": (start + timedelta(hours=2)).isoformat(),
                "observed_at_utc": (start + timedelta(hours=3)).isoformat(),
            }
            members.append(m)
            outcomes.append(o)
    return c, members, outcomes


def run(bundle):
    return evaluate_frozen(*bundle, expected_pins=bundle_pins(*bundle))


def test_original_failure_positive_small_effect_cannot_support_point05_target():
    bundle = fixture(values=[0.02] * 24)
    r = run(bundle)
    assert r["point_estimate"] == pytest.approx(0.02)
    assert r["diagnostic_p"] == 1.0
    assert r["interval"][1] < 0.05
    assert r["formal_allowed"] is r["promotion_allowed"] is False


def test_control_uncertainty_is_not_frozen_as_a_constant():
    quiet = run(fixture("independent_means", values=[0.1] * 24))
    noisy = run(
        fixture("independent_means", values=[0.1] * 24, reference=[-1.0, 1.0] * 12)
    )
    assert noisy["interval"][1] - noisy["interval"][0] > 0.3
    assert quiet["interval"][1] - quiet["interval"][0] < 1e-10
    assert noisy["diagnostic_p"] > quiet["diagnostic_p"]


def test_paired_shared_shocks_cancel_without_ignoring_reference():
    common = [(-1) ** i * i for i in range(24)]
    r = run(fixture(reference=common, values=[x + 0.12 for x in common]))
    assert r["n_components"] == 24
    assert r["interval"] == pytest.approx([0.12, 0.12])


def test_population_median_includes_top_and_recomputes_in_each_draw():
    c, members, outcomes = fixture(
        "top_mean_minus_population_median", values=list(range(24))
    )
    r = run((c, members, outcomes))
    assert r["point_estimate"] == pytest.approx(20.5 - 11.5)
    assert r["interval"][1] > r["interval"][0]
    # Finite-difference median behavior is not replacement by bottom mean.
    rows = [{**m, "value": o["value"]} for m, o in zip(members, outcomes)]
    weights = {m["member_id"]: 1.0 for m in members}
    weights[members[-1]["member_id"]] = 100.0
    assert statistic_value(c["statistic"], rows, weights) < 0.0


def test_negative_polarity_and_equivalence_have_their_own_direction():
    c, m, o = fixture("independent_means", values=[-0.5] * 24)
    c.update(direction="less", effect_target=0.0)
    assert run((c, m, o))["diagnostic_p"] < 0.01
    c.update(direction="greater")
    assert run((c, m, o))["diagnostic_p"] == 1.0
    c, m, o = fixture("independent_means", values=[0.0] * 24)
    c.update(direction="equivalent", effect_target=0.0, equivalence_margin=0.05)
    assert run((c, m, o))["diagnostic_p"] < 0.01
    del c["equivalence_margin"]
    assert run((c, m, o))["status"] == "invalid_frozen_evidence"


def test_cross_symbol_transitive_intervals_are_one_component():
    c, m, o = fixture("single_mean")
    for i, row in enumerate(o):
        row["end_utc"] = (
            datetime.fromisoformat(row["start_utc"]) + timedelta(days=4)
        ).isoformat()
        row["observed_at_utc"] = row["end_utc"]
        m[i]["input"]["symbol"] = f"SYM{i}"
        m[i]["input_sha256"] = canonical_sha256(m[i]["input"])
    r = run((c, m, o))
    assert r["n_components"] == 1
    assert r["status"] == "insufficient_dependence_support"
    assert r["diagnostic_p"] is None


def test_shared_control_links_events_not_independent_fake_replicates():
    c, m, o = fixture("independent_means")
    c["statistic"] = "matched_event_excess"
    refs = [x["member_id"] for x in m if x["arm"] == "reference"]
    for row in m:
        if row["arm"] == "tested":
            row["control_ids"] = refs
    r = run((c, m, o))
    assert r["n_components"] == 1 and r["interval"] is None


def test_one_missing_adverse_member_blocks_complete_case_inference():
    c, m, o = fixture()
    o[-1].update(status="missing", value=None)
    r = run((c, m, o))
    assert r["status"] == "incomplete_cohort"
    assert r["point_estimate"] is r["diagnostic_p"] is None
    assert len(r["incomplete_member_ids"]) == 1


@pytest.mark.parametrize("part", ["contract", "member", "outcome"])
def test_externally_pinned_identity_detects_changed_targets_or_selected_rows(part):
    c, m, o = fixture()
    pins = bundle_pins(c, m, o)
    if part == "contract":
        c["effect_target"] = 0.0
    elif part == "member":
        m.pop()
    else:
        o[0]["value"] = 100.0
    assert evaluate_frozen(c, m, o, expected_pins=pins)["blockers"] == [
        "external_input_identity_mismatch"
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "unknown_interval",
        "lookahead",
        "input",
        "evidence",
        "malformed",
        "unpaired_overlap",
    ],
)
def test_invalid_evidence_never_silently_skips_rows(mutation):
    c, m, o = fixture("independent_means")
    if mutation == "duplicate":
        m.append(deepcopy(m[0]))
        o.append(deepcopy(o[0]))
    elif mutation == "unknown_interval":
        del o[0]["end_utc"]
    elif mutation == "lookahead":
        o[0]["observed_at_utc"] = "2028-01-01T00:00:00Z"
    elif mutation == "input":
        m[0]["input"]["x"] = 1
    elif mutation == "evidence":
        o[0]["evidence"]["x"] = 1
    elif mutation == "malformed":
        m[0] = 1
    else:
        m[-1]["opportunity_id"] = m[0]["opportunity_id"]
    assert run((c, m, o))["status"] == "invalid_frozen_evidence"


def test_repeat_is_deterministic_and_inputs_unchanged():
    bundle = fixture()
    saved = deepcopy(bundle)
    assert run(bundle) == run(bundle)
    assert bundle == saved


def test_constant_null_is_conservative_at_float_precision():
    c, m, o = fixture(values=[.05] * 24)
    assert run((c, m, o))["diagnostic_p"] == 1.


def test_matched_events_recompute_each_local_control_contrast():
    c, m, o = fixture("independent_means", values=[.2] * 24, reference=[.1] * 24)
    c["statistic"] = "matched_event_excess"
    for r in m:
        if r["arm"] == "tested":
            r["control_ids"] = [r["member_id"].replace("tested", "reference")]
    r = run((c, m, o))
    assert r["n_components"] == 24
    assert r["point_estimate"] == pytest.approx(.1)
    assert r["diagnostic_p"] < .01
