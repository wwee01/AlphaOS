"""PORT-1: multiple-comparisons correction + the always-fresh three-way verdict.

Ported from NightDesk DECISIONS.md #85 -- see
docs/roadmap/ported/nightdesk-stats-contract.md Sec 2/3/4.

THE VERDICT AND ITS Q-VALUE ARE NEVER STORED AS AUTHORITATIVE (contract doc
Sec 4 -- a deliberate, documented departure from the AlphaOS punch-list
spec's literal "q_value stored... immutable, one-shot" wording, adopted
because NightDesk's own real, adversarially-verified implementation stores
no verdict at all and treats fresh recomputation as the mechanism working as
intended, not a bug). Every caller MUST go through ``compute_verdicts()``
here, which pulls every EVALUATED preregistration's frozen p-value fresh and
re-runs the correction over the full, current family every time. A
hypothesis correctly loses ``forward-test-candidate`` status as N grows --
this is intended, not a defect. Do not cache a verdict anywhere outside this
function's own return value; do not add a code path that recomputes BH over
an ad-hoc slice (a single card, a single date range) -- the family is always
"every evaluated preregistration," full stop.

By contrast, the EVIDENCE each hypothesis carries into this function (its
one-sided bootstrap p-value, CI, effective-N) IS immutable once written --
that half of the original spec's "immutable, one-shot" language is honored
exactly, by ``alphaos.stats.preregistration.evaluate_hypothesis()``'s
one-shot guard, not by anything in this module.
"""

from __future__ import annotations

from typing import Any

DEFAULT_FDR_Q = 0.10
DEFAULT_BONFERRONI_ALPHA = 0.05


def benjamini_hochberg(p_values: list[float], q: float = DEFAULT_FDR_Q) -> list[bool]:
    """Standard BH step-up procedure. Returns a same-length list of booleans
    (True = discovery at level ``q``), in the SAME order as ``p_values``."""
    n = len(p_values)
    if n == 0:
        return []
    indexed = sorted(range(n), key=lambda i: p_values[i])
    sorted_p = [p_values[i] for i in indexed]

    largest_k = 0
    for k in range(1, n + 1):
        if sorted_p[k - 1] <= (k / n) * q:
            largest_k = k

    discovery = [False] * n
    for rank, orig_idx in enumerate(indexed, start=1):
        discovery[orig_idx] = rank <= largest_k
    return discovery


def bh_q_values(p_values: list[float]) -> list[float]:
    """Per-hypothesis q-value (contract doc Sec 3's running-minimum form) --
    same order as ``p_values``. ``q_value <= q`` iff the hypothesis is a
    BH-FDR discovery at level ``q`` -- mathematically equivalent to
    ``benjamini_hochberg()`` above (same discoveries at any threshold),
    exposed as a directly comparable per-hypothesis number instead of a
    boolean-at-one-threshold."""
    n = len(p_values)
    if n == 0:
        return []
    indexed = sorted(range(n), key=lambda i: p_values[i])
    sorted_p = [p_values[i] for i in indexed]

    raw = [min((n / (i + 1)) * sorted_p[i], 1.0) for i in range(n)]
    running_min = [0.0] * n
    running_min[n - 1] = raw[n - 1]
    for i in range(n - 2, -1, -1):
        running_min[i] = min(raw[i], running_min[i + 1])

    q_values = [0.0] * n
    for rank_pos, orig_idx in enumerate(indexed):
        q_values[orig_idx] = round(running_min[rank_pos], 6)
    return q_values


def bonferroni_significant(p_values: list[float], alpha: float = DEFAULT_BONFERRONI_ALPHA) -> list[bool]:
    """A hypothesis is Bonferroni-significant iff ``p <= alpha / n`` -- the
    stricter family-wise cross-check reported ALONGSIDE BH-FDR, never in
    place of it (contract doc Sec 3)."""
    n = len(p_values)
    if n == 0:
        return []
    threshold = alpha / n
    return [p <= threshold for p in p_values]


def expected_false_positives(n: int, alpha: float = DEFAULT_BONFERRONI_ALPHA) -> float:
    """Context, not a gate: "N hypotheses tested; ~N*alpha false positives
    expected by chance alone" -- surfaced alongside any result so a
    good-looking point estimate at high N is read in its true context."""
    return round(n * alpha, 3)


def compute_verdicts(
    evaluated: list[dict],
    fdr_q: float = DEFAULT_FDR_Q,
    bonferroni_alpha: float = DEFAULT_BONFERRONI_ALPHA,
) -> list[dict]:
    """THE shared, always-fresh verdict function -- see module docstring.

    ``evaluated``: every preregistration row with ``evaluated_at_utc`` set
    (the full family -- contract doc Sec 3's "family defined explicitly as
    all pre-registered hypotheses with evaluated_at_utc set to date").  Each
    row needs ``prereg_id``, ``one_sided_p_below_zero``, ``ci_low``,
    ``ci_high``, ``evidence_status`` (``"ok"`` iff
    ``alphaos.stats.preregistration.evaluate_hypothesis()`` found the sample
    trustworthy -- clears THIS hypothesis's own pre-registered effective-N +
    span floors), and ``strong_prior_pre_documented``.

    Returns one dict per input row: ``{"prereg_id", "verdict", "q_value",
    "bonferroni_significant", "reason"}``. ``verdict`` is one of
    ``"rejected"``, ``"forward-test-candidate"``, ``"inconclusive"`` (contract
    doc Sec 2) -- never anything else, and this function alone decides it;
    nothing else in this codebase may compute a verdict independently.
    """
    n = len(evaluated)
    if n == 0:
        return []

    p_values = [float(r.get("one_sided_p_below_zero") or 1.0) for r in evaluated]
    q_values = bh_q_values(p_values)
    bh_discovery = benjamini_hochberg(p_values, q=fdr_q)
    bonf = bonferroni_significant(p_values, alpha=bonferroni_alpha)

    out: list[dict] = []
    for i, row in enumerate(evaluated):
        ci_low = row.get("ci_low")
        ci_high = row.get("ci_high")
        trustworthy = row.get("evidence_status") == "ok"
        strong_prior = bool(row.get("strong_prior_pre_documented"))

        if ci_high is not None and ci_high < 0:
            verdict, reason = "rejected", "OOS confidence interval fully below zero"
        elif ci_low is not None and ci_low > 0 and trustworthy and bh_discovery[i]:
            verdict, reason = "forward-test-candidate", "CI above zero, trustworthy sample, survives BH-FDR"
        elif strong_prior:
            verdict, reason = (
                "forward-test-candidate",
                "inconclusive evidence but a strong, pre-documented prior (cheap forward-test escape hatch)",
            )
        else:
            verdict, reason = "inconclusive", "does not meet the rejection or discovery bar"

        out.append({
            "prereg_id": row.get("prereg_id"),
            "verdict": verdict,
            "q_value": q_values[i],
            "bonferroni_significant": bonf[i],
            "reason": reason,
        })
    return out


def preregistration_family_summary(rows: list[dict]) -> dict[str, Any]:
    """The survivorship-denominator caveat's own counts (contract doc's port
    spec item 5 / Sec 6): any report claiming system-level edge must print
    ``hypotheses_tested`` (the FULL evaluated family, promoted + demoted +
    withdrawn -- never just the promoted subset) alongside ``promoted``.
    ``rows``: raw preregistrations rows (evaluated or not)."""
    evaluated = [r for r in rows if r.get("evaluated_at_utc")]
    promoted = [r for r in evaluated if r.get("operator_approved_for_forward_test")]
    return {
        "hypotheses_registered": len(rows),
        "hypotheses_tested": len(evaluated),
        "promoted": len(promoted),
    }
