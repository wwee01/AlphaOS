"""PORT-1: clustered bootstrap confidence interval + one-sided p-value.

Ported from NightDesk DECISIONS.md #81's "paired forward measurement"
primitive -- see docs/roadmap/ported/nightdesk-stats-contract.md Sec 3/5.

Resamples at the CLUSTER level (never the individual-observation level):
each bootstrap draw samples clusters WITH replacement (``len(clusters)``
draws), then pools every observation belonging to a drawn cluster before
computing that draw's mean. This is what makes non-independent
within-cluster observations count as ONE unit of resampling weight instead
of inflating the resample's effective size -- consume
``alphaos.stats.effective_n.effective_n()``'s own ``clusters`` output here,
never a flat row list.

Deterministic by default (a fixed seed, not wall-clock/system entropy) -- the
same input data always produces the exact same CI/p-value on every render,
matching this codebase's own "no code should look at the wall clock to make
a science decision" convention. Pass an explicit ``seed`` to vary it; tests
should always pass one.
"""

from __future__ import annotations

import random
from typing import Any

DEFAULT_N_RESAMPLES = 2000
DEFAULT_SEED = 20260709  # fixed constant, not wall-clock-derived -- see module docstring
DEFAULT_CI_LEVEL = 0.90  # NightDesk's own default two-sided CI width


def _values(rows: list[dict], value_key: str) -> list[float]:
    out = []
    for r in rows:
        v = r.get(value_key)
        if v is None:
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def clustered_bootstrap(
    clusters: list[list[dict]],
    value_key: str,
    n_resamples: int = DEFAULT_N_RESAMPLES,
    ci_level: float = DEFAULT_CI_LEVEL,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """``clusters``: list of clusters, each a list of row dicts -- the shape
    ``effective_n()`` returns. Draws ``n_resamples`` cluster-level bootstrap
    resamples (sampling clusters WITH replacement, ``len(clusters)`` per
    draw -- the standard non-parametric cluster bootstrap), computing the
    pooled mean of ``value_key`` across every observation in each draw's
    resampled clusters.

    Returns ``{"point_estimate", "ci_low", "ci_high", "ci_level",
    "one_sided_p_below_zero", "n_clusters", "n_resamples", "status"}``.
    ``status`` is ``"insufficient_data"`` (fewer than 2 clusters, or no
    parseable ``value_key`` anywhere) rather than a degenerate/fabricated
    interval -- never raises.
    """
    n_clusters = len(clusters)
    all_values = [v for c in clusters for v in _values(c, value_key)]
    if n_clusters < 2 or not all_values:
        return {
            "point_estimate": None, "ci_low": None, "ci_high": None,
            "ci_level": ci_level, "one_sided_p_below_zero": None,
            "n_clusters": n_clusters, "n_resamples": n_resamples,
            "status": "insufficient_data",
        }

    point_estimate = sum(all_values) / len(all_values)

    rng = random.Random(seed)
    resample_means: list[float] = []
    for _ in range(n_resamples):
        drawn = [clusters[rng.randrange(n_clusters)] for _ in range(n_clusters)]
        pooled = [v for c in drawn for v in _values(c, value_key)]
        if pooled:
            resample_means.append(sum(pooled) / len(pooled))

    if not resample_means:
        return {
            "point_estimate": round(point_estimate, 6), "ci_low": None, "ci_high": None,
            "ci_level": ci_level, "one_sided_p_below_zero": None,
            "n_clusters": n_clusters, "n_resamples": n_resamples,
            "status": "insufficient_data",
        }

    resample_means.sort()
    m = len(resample_means)
    alpha = 1.0 - ci_level
    lo_idx = max(0, min(m - 1, round((alpha / 2) * (m - 1))))
    hi_idx = max(0, min(m - 1, round((1 - alpha / 2) * (m - 1))))

    # One-sided bootstrap p-value: P(resampled mean <= 0), drawn from the
    # SAME resamples used for the CI above (contract doc Sec 3 -- the
    # p-value and the CI must never be able to disagree about direction).
    p_below_zero = sum(1 for v in resample_means if v <= 0) / m

    return {
        "point_estimate": round(point_estimate, 6),
        "ci_low": round(resample_means[lo_idx], 6),
        "ci_high": round(resample_means[hi_idx], 6),
        "ci_level": ci_level,
        "one_sided_p_below_zero": round(p_below_zero, 6),
        "n_clusters": n_clusters,
        "n_resamples": n_resamples,
        "status": "ok",
    }


# --------------------------------------------------------------- BASELINE
#
# Day-block bootstrap CI (BCa), spec item 5: "paired mean ΔR with a
# day-block bootstrap CI (resample whole decision-days, 10k resamples, BCa;
# fallback: se = sd(ΔR)/sqrt(effective_n))." A DIFFERENT clustering axis
# from clustered_bootstrap above (which clusters by symbol + overlapping
# holding-window) -- this resamples whole calendar DAYS, so a single
# volatile/correlated trading day (many paired candidates all sharing that
# day's market move) can never masquerade as many independent paired
# observations. Both are legitimate cluster-bootstrap variants answering
# different questions; BASELINE's paired ΔR claim is specifically about
# day-to-day variability, not per-symbol correlation (see
# alphaos-pr-implementation-specs.md's BASELINE section).
DEFAULT_DAY_BLOCK_N_RESAMPLES = 10000
DEFAULT_DAY_BLOCK_SEED = 20260709  # same fixed constant as clustered_bootstrap, for consistency

# Below this many day-blocks, BCa's own jackknife acceleration term is too
# noisy to trust (a 2-point jackknife has no meaningful skew estimate) --
# fall back to the spec's own named fallback instead of a degenerate BCa.
MIN_DAY_BLOCKS_FOR_BCA = 3


def _day_blocks(rows: list[dict], value_key: str, date_key: str) -> list[list[float]]:
    """Group rows into day-blocks (rows sharing the same date_key, first 10
    chars), sorted by date for determinism. A row missing either field is
    silently excluded (uncountable, never fabricated as its own block or
    folded into an adjacent one)."""
    by_date: dict[str, list[float]] = {}
    for r in rows:
        d = r.get(date_key)
        v = r.get(value_key)
        if not d or v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        by_date.setdefault(str(d)[:10], []).append(v)
    return [by_date[d] for d in sorted(by_date)]


def _normal_approx_interval(all_values: list[float], n_blocks: int, ci_level: float, nd) -> tuple:
    """Fallback CI: point_estimate +/- z*se, se = sd(values)/sqrt(n_blocks)
    -- the spec's own named fallback when BCa cannot be trusted."""
    n = len(all_values)
    if n < 2 or n_blocks < 1:
        return None, None
    mean = sum(all_values) / n
    variance = sum((v - mean) ** 2 for v in all_values) / (n - 1)
    sd = variance ** 0.5
    se = sd / (n_blocks ** 0.5)
    if not se:
        return mean, mean
    z = nd.inv_cdf(1 - (1 - ci_level) / 2)
    return mean - z * se, mean + z * se


def _bca_interval(
    point_estimate: float, resample_means: list[float], blocks: list[list[float]], ci_level: float,
) -> tuple:
    """Bias-corrected and accelerated (BCa) interval (Efron & Tibshirani
    1993). Returns ``(ci_low, ci_high, method)`` where ``method`` is
    ``"bca"`` on success or ``"normal_approx"``/``"normal_approx_degenerate"``
    when any step of BCa is undefined (degenerate bias proportion, too few
    day-blocks for jackknife, a zero jackknife-variance denominator, or a
    non-finite adjusted percentile) -- falls back rather than ever returning
    a NaN/inf bound."""
    from statistics import NormalDist

    nd = NormalDist()
    m = len(resample_means)
    n_blocks = len(blocks)
    all_values = [v for block in blocks for v in block]

    def _fallback():
        lo, hi = _normal_approx_interval(all_values, n_blocks, ci_level, nd)
        method = "normal_approx" if lo is not None else "normal_approx_degenerate"
        return lo, hi, method

    # Bias-correction z0: inverse-CDF of the proportion of bootstrap
    # replicates strictly below the point estimate. A degenerate proportion
    # (0 or 1 -- every replicate on one side) has no finite z0.
    prop_below = sum(1 for v in resample_means if v < point_estimate) / m
    if prop_below <= 0.0 or prop_below >= 1.0:
        return _fallback()
    z0 = nd.inv_cdf(prop_below)

    if n_blocks < MIN_DAY_BLOCKS_FOR_BCA:
        return _fallback()

    # Acceleration via jackknife: leave-one-day-block-out pooled means.
    jack_means = []
    for i in range(n_blocks):
        rest = [v for j, block in enumerate(blocks) if j != i for v in block]
        if rest:
            jack_means.append(sum(rest) / len(rest))
    if len(jack_means) < MIN_DAY_BLOCKS_FOR_BCA:
        return _fallback()
    jack_avg = sum(jack_means) / len(jack_means)
    num = sum((jack_avg - jm) ** 3 for jm in jack_means)
    den = 6.0 * (sum((jack_avg - jm) ** 2 for jm in jack_means) ** 1.5)
    if den == 0:
        return _fallback()
    a_hat = num / den

    alpha = 1.0 - ci_level
    z_lo = nd.inv_cdf(alpha / 2)
    z_hi = nd.inv_cdf(1 - alpha / 2)

    def _adjust(z):
        denom = 1 - a_hat * (z0 + z)
        if denom == 0:
            return None
        return nd.cdf(z0 + (z0 + z) / denom)

    alpha1, alpha2 = _adjust(z_lo), _adjust(z_hi)
    if alpha1 is None or alpha2 is None:
        return _fallback()

    sorted_means = sorted(resample_means)
    lo_idx = max(0, min(m - 1, round(alpha1 * (m - 1))))
    hi_idx = max(0, min(m - 1, round(alpha2 * (m - 1))))
    lo, hi = sorted_means[lo_idx], sorted_means[hi_idx]
    if lo > hi:  # pathological-ordering guard -- never return an inverted interval
        lo, hi = hi, lo
    return lo, hi, "bca"


def day_block_bootstrap(
    rows: list[dict],
    value_key: str,
    date_key: str = "decision_date",
    n_resamples: int = DEFAULT_DAY_BLOCK_N_RESAMPLES,
    ci_level: float = DEFAULT_CI_LEVEL,
    seed: int = DEFAULT_DAY_BLOCK_SEED,
) -> dict[str, Any]:
    """BASELINE spec item 5. ``rows``: dicts carrying ``value_key`` (the
    paired ΔR) and ``date_key`` (the decision date, "YYYY-MM-DD" or an ISO
    timestamp -- only the first 10 chars are used). Resamples whole
    decision-days with replacement (``len(day_blocks)`` draws per resample),
    pooling every observation in each drawn day before computing that
    resample's mean -- never resamples individual observations.

    Returns ``{"point_estimate", "ci_low", "ci_high", "ci_level",
    "one_sided_p_below_zero", "n_day_blocks", "n_resamples", "ci_method",
    "status"}``. ``status`` is ``"insufficient_data"`` (fewer than 2
    day-blocks, or no parseable value anywhere) rather than a
    fabricated/degenerate interval -- never raises. ``ci_method`` is
    ``"bca"`` when the full BCa correction could be computed, else the named
    fallback (``"normal_approx"``) the spec itself calls for.
    """
    blocks = _day_blocks(rows, value_key, date_key)
    n_blocks = len(blocks)
    all_values = [v for block in blocks for v in block]
    if n_blocks < 2 or not all_values:
        return {
            "point_estimate": None, "ci_low": None, "ci_high": None,
            "ci_level": ci_level, "one_sided_p_below_zero": None,
            "n_day_blocks": n_blocks, "n_resamples": n_resamples,
            "ci_method": None, "status": "insufficient_data",
        }

    point_estimate = sum(all_values) / len(all_values)

    rng = random.Random(seed)
    resample_means: list[float] = []
    for _ in range(n_resamples):
        drawn_idxs = [rng.randrange(n_blocks) for _ in range(n_blocks)]
        pooled = [v for i in drawn_idxs for v in blocks[i]]
        if pooled:
            resample_means.append(sum(pooled) / len(pooled))

    if not resample_means:
        return {
            "point_estimate": round(point_estimate, 6), "ci_low": None, "ci_high": None,
            "ci_level": ci_level, "one_sided_p_below_zero": None,
            "n_day_blocks": n_blocks, "n_resamples": n_resamples,
            "ci_method": None, "status": "insufficient_data",
        }

    p_below_zero = sum(1 for v in resample_means if v <= 0) / len(resample_means)
    ci_low, ci_high, ci_method = _bca_interval(point_estimate, resample_means, blocks, ci_level)

    return {
        "point_estimate": round(point_estimate, 6),
        "ci_low": round(ci_low, 6) if ci_low is not None else None,
        "ci_high": round(ci_high, 6) if ci_high is not None else None,
        "ci_level": ci_level,
        "one_sided_p_below_zero": round(p_below_zero, 6),
        "n_day_blocks": n_blocks,
        "n_resamples": n_resamples,
        "ci_method": ci_method,
        "status": "ok",
    }
