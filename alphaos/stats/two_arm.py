"""S1b: two-arm smooth-weight (Bayesian/Dirichlet) joint clustered bootstrap
for post_earnings_reaction_v1's formal test (H-PER-1P/H-PER-1N).

Ported from the approved S1b Statistical Mechanisms Specification (v2.0, as
amended by the v2.1-FINAL corrections). This is a DIFFERENT engine from
``alphaos.stats.bootstrap.clustered_bootstrap()`` -- that function is a
ONE-SAMPLE bootstrap (single arm, CI + one-sided p-value against a fixed
zero). This module answers a genuinely TWO-ARM question (PER events vs a
date x tier default-card control reference) and needed a new engine for two
reasons the spec worked through explicitly:

1. The existing two-arm convention this codebase already has
   (``alphaos.hypotheses.queries``'s "centered-delta against a frozen
   reference mean") treats the reference arm's mean as a known constant --
   its own module docstring names this as a real, acknowledged
   anti-conservative bias (narrower CIs than warranted). This module
   instead resamples BOTH arms jointly, every replicate, so the control
   reference's own sampling error is represented, not assumed away.
2. An ORDINARY (non-null-centered) bootstrap tail proportion reads the
   WRONG TAIL under skew (5-trading-day post-earnings returns are exactly
   where skew lives) and conflates the estimation distribution with the
   null distribution. This module null-centers explicitly (Efron-Tibshirani
   shift method) for the p-value, while keeping a SEPARATE, uncentred
   distribution for the effect-size CI -- two different jobs, two different
   distributions, by design (spec Section 8).

SMOOTH-WEIGHT ENGINE (the v2.1 correction over the v2.0 draft): earlier
drafts used an ordinary multinomial cluster bootstrap with per-replicate
"drop the event if its stratum lost support" handling. That construction
was rejected during design review: a multinomial draw can (and at v1's
expected floor minimums, routinely does) leave a thin stratum with ZERO
resampled controls, forcing either event-dropping (which changes WHICH
population each replicate estimates -- a different estimand per replicate,
biased toward whichever events happen to keep support) or invalidating a
large fraction of replicates (which conditions the tail distribution on
draw configuration, a bias in itself). The fix is this module's own
Dirichlet/Exp(1)-weight ("smooth-weight" / Bayesian bootstrap, Rubin 1981;
first-order equivalent to the ordinary bootstrap for smooth functionals of
means, Lo 1987) resampling: every cluster gets an ALMOST-SURELY POSITIVE
random weight every replicate, so no cluster (and therefore no stratum, and
therefore no PER event) can ever lose support. Every valid replicate
therefore estimates the mean over the EXACT SAME fixed population, with
only the resampling WEIGHTS varying -- the whole point of the correction.

WHAT THIS MODULE DOES NOT DO: it never touches the journal, the card
registry, the selector, or any candidate/proposal/risk/approval table. It
is pure arithmetic over caller-supplied dicts. ``alphaos.cards.per_evidence``
is the (also dormant, also zero-production-caller) module that builds those
dicts from real journal rows; ``alphaos.stats.preregistration``'s
``evaluate_two_arm_hypothesis_pair()`` is the only place either of them is
ever invoked, and that function itself is only ever called by an
operator-invoked registration/evaluation command -- never by any scan,
scheduler, or production decision path.
"""

from __future__ import annotations

import math
import random
from datetime import date as _date
from typing import Any, Optional

from alphaos.util.market_calendar import nth_trading_day_after

DEFAULT_B = 2000
DEFAULT_CI_LEVEL = 0.90
# Distinct from alphaos.stats.bootstrap.DEFAULT_SEED -- an independent
# module, an independent registered constant (the date SETUP-1 was scoped).
# Fixed, not wall-clock-derived, matching this codebase's "no code looks at
# the wall clock to make a science decision" convention.
DEFAULT_SEED = 20260716

# >=98% of replicates must be valid (spec v2.1 Section 3): under the
# smooth-weight engine, a valid replicate's ONLY possible invalidity is a
# non-finite statistic from numerical pathology -- expected rate is ~0, so
# any material invalid rate signals an implementation/data defect, not
# ordinary sampling noise, and evaluation must defer rather than publish
# tails from a distribution conditioned on that defect.
MIN_VALID_REPLICATE_FRACTION = 0.98


def _is_finite(x: Optional[float]) -> bool:
    return x is not None and math.isfinite(x)


def _all_equal(values: list[float]) -> bool:
    """Audit-fixup (correctness MED): exact float ``==`` let mathematically-
    constant data slip past the zero-spread guard whenever the underlying
    values weren't bit-exact (e.g. ``outcomes_tracker.py``'s own
    ``round(..., 4)``-quantized outcomes) -- a weighted mean of identical
    non-representable floats (like ``0.1``) lands on a handful of distinct
    IEEE-754 values across replicates (spread ~1e-15), which compared
    unequal under ``==`` and let the engine report the single most
    extreme-possible p-value on data carrying zero real information.
    ``math.isclose`` against the first value (both a relative AND absolute
    tolerance, since the center value can legitimately be exactly/near
    zero, where a pure relative tolerance is meaningless) treats that
    float-noise floor as "no real spread" while still catching genuine
    variation at any observable scale."""
    if len(values) < 2:
        return True
    first = values[0]
    return all(math.isclose(v, first, rel_tol=1e-9, abs_tol=1e-9) for v in values[1:])


# ------------------------------------------------------------- clustering
def build_trading_day_clusters(
    observations: list[dict],
    symbol_key: str = "symbol",
    date_key: str = "market_date",
    window_trading_days: int = 5,
) -> list[list[dict]]:
    """Sweep-line interval-merge, the SAME algorithm shape as
    ``alphaos.stats.effective_n.effective_n()``'s own cluster construction,
    but in TRADING-DAY space (``nth_trading_day_after``) rather than
    calendar-day ``timedelta``. This distinction is load-bearing: a Friday
    observation's true 5-TRADING-day window end is ~7 calendar days out
    (it spans a weekend), and a calendar-day merge would understate the
    window, silently missing a real overlap with the following Monday's or
    Tuesday's own window -- exactly the same-symbol correlation this
    clustering exists to catch.

    ``observations``: any list of dicts carrying ``symbol_key``/``date_key``
    (``date_key`` a ``'YYYY-MM-DD'`` string or ``date``). A row missing
    either is silently excluded (uncountable, matching ``effective_n()``'s
    own convention) -- this function does not validate its inputs.

    Returns clusters grouped per symbol, each a list of the ORIGINAL row
    dicts (never mutated, never copied), in a FULLY DETERMINISTIC order
    (symbols sorted lexically, each symbol's own intervals sorted by start
    date with a stable secondary sort) -- required so the caller's
    downstream per-cluster weight index never depends on database
    insertion/iteration order.
    """
    parsed: list[tuple[str, _date, dict]] = []
    for r in observations:
        symbol = r.get(symbol_key)
        raw_date = r.get(date_key)
        if not symbol or not raw_date:
            continue
        try:
            d = _date.fromisoformat(str(raw_date)[:10])
        except ValueError:
            continue
        parsed.append((str(symbol), d, r))

    by_symbol: dict[str, list[tuple[_date, _date, dict]]] = {}
    for symbol, d, r in parsed:
        window_end = nth_trading_day_after(d, window_trading_days) if window_trading_days > 0 else d
        by_symbol.setdefault(symbol, []).append((d, window_end, r))

    clusters: list[list[dict]] = []
    for symbol in sorted(by_symbol):
        intervals = sorted(by_symbol[symbol], key=lambda t: (t[0], t[1]))
        current_cluster: list[dict] = []
        current_end: Optional[_date] = None
        for start, end, r in intervals:
            if current_end is not None and start <= current_end:
                current_cluster.append(r)
                current_end = max(current_end, end)
            else:
                if current_cluster:
                    clusters.append(current_cluster)
                current_cluster = [r]
                current_end = end
        if current_cluster:
            clusters.append(current_cluster)
    return clusters


# --------------------------------------------------------------- estimator
def _weighted_mean(items: list[tuple[int, float]], weights: Optional[list[float]]) -> Optional[float]:
    """``items``: list of ``(control_cluster_idx, value)`` pairs belonging
    to one frozen stratum. ``weights``: per-control-cluster weight array, or
    ``None`` for the uniform (unweighted, original-estimate) mean."""
    if not items:
        return None
    if weights is None:
        vals = [v for _, v in items]
        return sum(vals) / len(vals)
    num = sum(weights[cidx] * v for cidx, v in items)
    den = sum(weights[cidx] for cidx, _v in items)
    if den == 0:
        return None
    return num / den


def _theta(
    per_events: list[dict],
    stratum_members: dict[Any, list[tuple[int, float]]],
    weights_per: Optional[list[float]],
    weights_ctl: Optional[list[float]],
) -> Optional[float]:
    """The estimand (spec Section 1): the equal-weighted mean, over EVERY
    event in the fixed population ``per_events``, of that event's own value
    minus its FROZEN stratum's (weighted) reference mean. ``weights_per``/
    ``weights_ctl`` of ``None`` reproduce the original (uniform-weight)
    estimate; real replicate weight arrays reproduce one smooth-weight
    resample. A stratum reference mean is computed ONCE per replicate (not
    once per event) since multiple events can share a stratum."""
    ref_cache: dict[Any, Optional[float]] = {}
    num = 0.0
    den = 0.0
    for event in per_events:
        skey = event["stratum_key"]
        if skey not in ref_cache:
            ref_cache[skey] = _weighted_mean(stratum_members.get(skey, []), weights_ctl)
        ref = ref_cache[skey]
        if ref is None:
            return None
        w = 1.0 if weights_per is None else weights_per[event["_cluster_idx"]]
        num += w * (event["value"] - ref)
        den += w
    if den == 0:
        return None
    return num / den


def _insufficient(
    n_resamples: int,
    ci_level: float,
    *,
    status: str = "insufficient_data",
    point_estimate: Optional[float] = None,
    n_per_clusters: Optional[int] = None,
    n_control_clusters: Optional[int] = None,
    n_events: Optional[int] = None,
    n_valid_replicates: int = 0,
    n_invalid_replicates: int = 0,
) -> dict[str, Any]:
    return {
        "point_estimate": point_estimate,
        "ci_low": None,
        "ci_high": None,
        "ci_level": ci_level,
        "p_pos": None,
        "p_neg": None,
        "n_per_clusters": n_per_clusters,
        "n_control_clusters": n_control_clusters,
        "n_events": n_events,
        "n_resamples": n_resamples,
        "n_valid_replicates": n_valid_replicates,
        "n_invalid_replicates": n_invalid_replicates,
        "status": status,
    }


def two_arm_bootstrap(
    per_clusters: list[list[dict]],
    control_clusters: list[list[dict]],
    n_resamples: int = DEFAULT_B,
    ci_level: float = DEFAULT_CI_LEVEL,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """The approved smooth-weight joint clustered bootstrap. Never raises.

    ``per_clusters``: list of PER-event clusters (as returned by
    ``build_trading_day_clusters()``, or any caller that reproduces its
    determinism contract); each cluster is a list of event dicts, each
    carrying ``{"value": Y_i, "stratum_key": <frozen ladder reference,
    assigned ONCE by the caller before this function ever runs -- opaque to
    this function, any hashable>}``.

    ``control_clusters``: list of control-observation clusters; each
    cluster is a list of control dicts, each carrying ``{"value": float,
    "stratum_keys": <iterable of every stratum this control counts toward
    -- normally its own rung-1 (market_date, tier) key AND its tier's
    rung-2 pooled key, both precomputed by the caller>}``. This function
    never itself knows about dates, tiers, or rungs -- it only sums
    whichever ``stratum_keys`` a control was tagged with under whichever
    stratum a PER event references.

    Returns ``{"point_estimate", "ci_low", "ci_high", "ci_level", "p_pos",
    "p_neg", "n_per_clusters", "n_control_clusters", "n_events",
    "n_resamples", "n_valid_replicates", "n_invalid_replicates",
    "status"}``. ``status`` is ``"insufficient_data"`` (fewer than 2
    clusters on either arm, an unreachable stratum reference, or fewer than
    ``MIN_VALID_REPLICATE_FRACTION`` of replicates valid) or
    ``"zero_spread"`` (every valid replicate produced the identical
    statistic -- e.g. a constant control arm) rather than ever returning a
    fabricated interval or p-value.

    ``p_pos``/``p_neg`` are the H-PER-1P/H-PER-1N one-sided, null-centred,
    finite-replicate-corrected p-values, BOTH drawn from the SAME shared
    replicate distribution (spec Section 7) -- callers needing PR12's
    directional-frame storage convention (H-PER-1N stores the negated
    estimate/CI) do that mapping themselves; this function always returns
    the natural (unflipped) frame.
    """
    n_per_clusters = len(per_clusters)
    n_ctl_clusters = len(control_clusters)
    per_events = [
        {**e, "_cluster_idx": cidx} for cidx, cluster in enumerate(per_clusters) for e in cluster
    ]
    n_events = len(per_events)
    if n_per_clusters < 2 or n_ctl_clusters < 2 or n_events == 0:
        return _insufficient(
            n_resamples, ci_level,
            n_per_clusters=n_per_clusters, n_control_clusters=n_ctl_clusters, n_events=n_events,
        )

    referenced_strata = {e["stratum_key"] for e in per_events}
    stratum_members: dict[Any, list[tuple[int, float]]] = {s: [] for s in referenced_strata}
    for cidx, cluster in enumerate(control_clusters):
        for obs in cluster:
            for skey in obs.get("stratum_keys", ()):
                if skey in stratum_members:
                    stratum_members[skey].append((cidx, obs["value"]))

    # Every frozen event's stratum must have at least one control member --
    # per_evidence.py's own ladder floors already guarantee this before E*
    # is frozen, so an empty stratum here means an upstream construction
    # bug, not a resampling degeneracy. Surfacing it as insufficient_data
    # (never a crash, never a fabricated reference of 0) keeps this
    # function's "never raises" contract while still refusing to guess.
    if any(not members for members in stratum_members.values()):
        return _insufficient(
            n_resamples, ci_level,
            n_per_clusters=n_per_clusters, n_control_clusters=n_ctl_clusters, n_events=n_events,
        )

    theta_hat = _theta(per_events, stratum_members, weights_per=None, weights_ctl=None)
    if theta_hat is None:
        return _insufficient(
            n_resamples, ci_level,
            n_per_clusters=n_per_clusters, n_control_clusters=n_ctl_clusters, n_events=n_events,
        )

    # Independent derived RNG streams (spec Section 3): seed for PER,
    # seed+1 for control. Deterministic given a registered seed; the SAME
    # input data always produces the exact same result on every render.
    rng_per = random.Random(seed)
    rng_ctl = random.Random(seed + 1)

    theta_star: list[float] = []
    n_invalid = 0
    for _ in range(n_resamples):
        # Every cluster drawn every replicate, with an almost-surely
        # positive weight -- no multinomial "cluster absent this draw" is
        # possible, which is exactly what keeps every replicate's estimand
        # fixed to the SAME population as theta_hat (v2.1 correction).
        w_per = [rng_per.expovariate(1.0) for _ in range(n_per_clusters)]
        w_ctl = [rng_ctl.expovariate(1.0) for _ in range(n_ctl_clusters)]
        theta_b = _theta(per_events, stratum_members, w_per, w_ctl)
        if theta_b is None or not _is_finite(theta_b):
            n_invalid += 1
            continue
        theta_star.append(theta_b)

    b_valid = len(theta_star)
    if n_resamples <= 0 or b_valid < MIN_VALID_REPLICATE_FRACTION * n_resamples:
        return _insufficient(
            n_resamples, ci_level,
            point_estimate=round(theta_hat, 6),
            n_per_clusters=n_per_clusters, n_control_clusters=n_ctl_clusters, n_events=n_events,
            n_valid_replicates=b_valid, n_invalid_replicates=n_invalid,
        )

    if _all_equal(theta_star):
        return _insufficient(
            n_resamples, ci_level, status="zero_spread",
            point_estimate=round(theta_hat, 6),
            n_per_clusters=n_per_clusters, n_control_clusters=n_ctl_clusters, n_events=n_events,
            n_valid_replicates=b_valid, n_invalid_replicates=n_invalid,
        )

    # Null-centred directional p-values (spec Section 7): T*_b = theta*_b -
    # theta_hat; T*_b >= theta_hat <=> theta*_b >= 2*theta_hat (upper tail,
    # H-PER-1P) and T*_b <= theta_hat <=> theta*_b <= 2*theta_hat (lower
    # tail, H-PER-1N). Ties at exactly 2*theta_hat count as extreme for
    # BOTH hypotheses (conservative both directions, by construction).
    threshold = 2.0 * theta_hat
    extreme_pos = sum(1 for t in theta_star if t >= threshold)
    extreme_neg = sum(1 for t in theta_star if t <= threshold)
    p_pos = (extreme_pos + 1) / (b_valid + 1)
    p_neg = (extreme_neg + 1) / (b_valid + 1)

    # Effect-size CI: the SEPARATE, UNCENTRED percentile interval over the
    # same valid theta_star draws (spec Section 8) -- never the null-
    # centred T_star distribution used for the p-values above.
    sorted_star = sorted(theta_star)
    m = len(sorted_star)
    alpha = 1.0 - ci_level
    lo_idx = max(0, min(m - 1, round((alpha / 2) * (m - 1))))
    hi_idx = max(0, min(m - 1, round((1 - alpha / 2) * (m - 1))))

    return {
        "point_estimate": round(theta_hat, 6),
        "ci_low": round(sorted_star[lo_idx], 6),
        "ci_high": round(sorted_star[hi_idx], 6),
        "ci_level": ci_level,
        "p_pos": round(p_pos, 6),
        "p_neg": round(p_neg, 6),
        "n_per_clusters": n_per_clusters,
        "n_control_clusters": n_ctl_clusters,
        "n_events": n_events,
        "n_resamples": n_resamples,
        "n_valid_replicates": b_valid,
        "n_invalid_replicates": n_invalid,
        "status": "ok",
    }
