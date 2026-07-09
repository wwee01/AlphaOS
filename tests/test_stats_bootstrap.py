"""PORT-1: alphaos.stats.bootstrap -- deterministic by construction (fixed
default seed); tests pin properties that must hold for ANY seed rather than
one RNG draw's exact numbers, except where a constant-value fixture makes the
exact number RNG-independent by construction (every possible resample of a
constant is that same constant).
"""

from __future__ import annotations

from alphaos.stats.bootstrap import clustered_bootstrap


def _clusters(values):
    """One single-observation cluster per value."""
    return [[{"v": v}] for v in values]


# --------------------------------------------------------------- insufficient
def test_zero_clusters_is_insufficient_data():
    out = clustered_bootstrap([], "v")
    assert out["status"] == "insufficient_data"
    assert out["point_estimate"] is None


def test_one_cluster_is_insufficient_data():
    out = clustered_bootstrap(_clusters([1.0]), "v")
    assert out["status"] == "insufficient_data"


def test_no_parseable_values_anywhere_is_insufficient_data():
    clusters = [[{"v": None}], [{"v": "not-a-number"}]]
    out = clustered_bootstrap(clusters, "v")
    assert out["status"] == "insufficient_data"


# --------------------------------------------------- constant-value exactness
def test_constant_positive_value_gives_exact_ci_and_zero_p():
    """Every observation is EXACTLY 1.0 -- every possible resample (whichever
    clusters get drawn) also averages to exactly 1.0, so the CI collapses to
    a point and p(resampled <= 0) is exactly 0, independent of the RNG."""
    clusters = _clusters([1.0] * 25)
    out = clustered_bootstrap(clusters, "v", seed=1)
    assert out["status"] == "ok"
    assert out["point_estimate"] == 1.0
    assert out["ci_low"] == 1.0
    assert out["ci_high"] == 1.0
    assert out["one_sided_p_below_zero"] == 0.0


def test_constant_negative_value_gives_exact_ci_and_p_one():
    clusters = _clusters([-1.0] * 25)
    out = clustered_bootstrap(clusters, "v", seed=1)
    assert out["point_estimate"] == -1.0
    assert out["ci_low"] == -1.0
    assert out["ci_high"] == -1.0
    assert out["one_sided_p_below_zero"] == 1.0


def test_constant_zero_value_sits_exactly_on_the_boundary():
    clusters = _clusters([0.0] * 25)
    out = clustered_bootstrap(clusters, "v", seed=1)
    assert out["ci_low"] == 0.0
    assert out["ci_high"] == 0.0
    # <= 0 includes exactly-zero resamples -- boundary counts as "below".
    assert out["one_sided_p_below_zero"] == 1.0


# -------------------------------------------------------------- determinism
def test_same_seed_same_input_gives_identical_output():
    clusters = _clusters([1.0, -1.0, 2.0, -0.5, 3.0, 0.2, -2.0, 1.5])
    out1 = clustered_bootstrap(clusters, "v", seed=42, n_resamples=500)
    out2 = clustered_bootstrap(clusters, "v", seed=42, n_resamples=500)
    assert out1 == out2


# -------------------------------------------------------------- CI sanity
def test_ci_bounds_bracket_point_estimate_direction():
    clusters = _clusters([1.0, 1.2, 0.9, 1.1, 1.05, 0.95, 1.3, 0.8, 1.0, 1.15] * 3)
    out = clustered_bootstrap(clusters, "v", seed=7)
    assert out["status"] == "ok"
    assert out["ci_low"] <= out["point_estimate"] <= out["ci_high"]
    assert out["ci_low"] > 0  # clearly-positive data at 30 clusters -- CI shouldn't touch zero


def test_p_value_and_ci_never_disagree_about_direction():
    """Contract doc Sec 3: p-value and CI must be drawn from the SAME
    resamples so they can never disagree. Clearly-negative data -> CI fully
    below zero AND p(below zero) is high."""
    clusters = _clusters([-1.0, -1.2, -0.9, -1.1, -1.05, -0.95, -1.3, -0.8, -1.0, -1.15] * 3)
    out = clustered_bootstrap(clusters, "v", seed=7)
    assert out["ci_high"] < 0
    assert out["one_sided_p_below_zero"] > 0.9


# -------------------------------------------------------------- pooling
def test_pools_every_observation_in_a_drawn_cluster_not_just_one():
    # A cluster with two wildly different values pools both when drawn --
    # verified indirectly via the point estimate over ALL observations.
    clusters = [[{"v": 10.0}, {"v": -10.0}], [{"v": 1.0}]]
    out = clustered_bootstrap(clusters, "v", seed=3)
    # point_estimate is the mean of ALL raw observations (10, -10, 1) = 1/3,
    # give or take the function's own 6-decimal rounding.
    assert abs(out["point_estimate"] - (1.0 / 3.0)) < 1e-5
