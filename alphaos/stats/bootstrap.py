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
