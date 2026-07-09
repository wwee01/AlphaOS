"""PORT-1: the ``preregistrations`` registry -- pre-specified hypotheses,
evaluated at most once, evidence immutable thereafter.

See docs/roadmap/ported/nightdesk-stats-contract.md Sec 6. This module is
the only writer of the ``preregistrations`` table: ``register_hypothesis()``
creates a row (unevaluated); ``evaluate_hypothesis()`` fills in its evidence
EXACTLY ONCE (contract doc Sec 1's anti-optional-stopping law -- re-running
a backtest hoping for a luckier p-value must be structurally impossible, not
just discouraged). Neither function ever computes or stores a verdict --
that is ``alphaos.stats.fdr.compute_verdicts()``'s job alone, always fresh,
never cached here.

Nothing in this module writes ``operator_approved_for_forward_test`` -- that
field is operator-only by construction (the lab recommends, it never enrolls
itself; contract doc Sec 6). No PR built tonight sets it.
"""

from __future__ import annotations

from typing import Any, Optional

from alphaos.stats.bootstrap import (
    DEFAULT_CI_LEVEL,
    DEFAULT_N_RESAMPLES,
    DEFAULT_SEED,
    clustered_bootstrap,
)
from alphaos.stats.effective_n import effective_n as _effective_n
from alphaos.util import timeutils
from alphaos.util.ids import new_id


class PreregistrationAlreadyEvaluatedError(RuntimeError):
    """Raised by ``evaluate_hypothesis()`` when the target row already has
    ``evaluated_at_utc`` set. Evidence is immutable once written -- register
    a NEW hypothesis row instead of re-evaluating this one."""


def register_hypothesis(
    journal,
    hypothesis: str,
    metric: str,
    floor_effective_n: int,
    floor_span_days: float,
    analysis_not_before: str,
    params: Optional[dict] = None,
    strong_prior_pre_documented: bool = False,
    strong_prior_reasoning: Optional[str] = None,
) -> str:
    """Create one registry row. Floors and ``analysis_not_before`` have no
    defaults here deliberately -- pre-registration discipline requires every
    hypothesis to pre-specify its own bar, not inherit a shared literal that
    could quietly become "the" bar everyone leans on (contract doc Sec 6).
    Every pre-specified variant is its OWN row -- call this once per variant,
    never reuse a row across variants being compared.
    """
    if strong_prior_pre_documented and not strong_prior_reasoning:
        raise ValueError(
            "strong_prior_pre_documented=True requires strong_prior_reasoning -- "
            "the prior must be written down BEFORE evaluation, not asserted after "
            "the fact (this is what makes the forward-test escape hatch honest)."
        )
    prereg_id = new_id("prereg")
    now = timeutils.stamp()
    journal.insert("preregistrations", {
        "prereg_id": prereg_id,
        "hypothesis": hypothesis,
        "metric": metric,
        "params_json": params,
        "floor_effective_n": floor_effective_n,
        "floor_span_days": floor_span_days,
        "analysis_not_before": analysis_not_before,
        "strong_prior_pre_documented": strong_prior_pre_documented,
        "strong_prior_reasoning": strong_prior_reasoning,
        "registered_at_utc": now.utc,
        "registered_at_sgt": now.local_sgt,
        "operator_approved_for_forward_test": False,
    })
    return prereg_id


def evaluate_hypothesis(
    journal,
    prereg_id: str,
    rows: list[dict],
    value_key: str,
    symbol_key: str = "symbol",
    date_key: str = "decision_date",
    holding_days_key: str = "max_holding_days",
    n_resamples: int = DEFAULT_N_RESAMPLES,
    ci_level: float = DEFAULT_CI_LEVEL,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """Compute ``effective_n()`` + ``clustered_bootstrap()`` over ``rows``
    and freeze the result onto the ``prereg_id`` row's evidence columns.
    Raises ``PreregistrationAlreadyEvaluatedError`` if this hypothesis was
    already evaluated -- the guard is the UPDATE's own
    ``WHERE evaluated_at_utc IS NULL`` clause (a DB-level backstop, not just
    an application-level check-then-write), so it holds even under a
    hypothetical race between two callers. Never partially writes: either
    every evidence column lands together, or none do.

    ``evidence_status`` is ``"ok"`` only when the observed ``effective_n``
    AND ``span_days`` clear THIS hypothesis's own pre-registered
    ``floor_effective_n``/``floor_span_days`` (set once, at registration,
    per the pre-registration discipline -- never ``effective_n()``'s generic
    ``MIN_TRUSTWORTHY_CLUSTERS`` default, which is a sensible value a
    hypothesis MAY register with, not a floor this function silently
    substitutes for whatever was actually pre-specified).
    """
    existing = journal.one("SELECT * FROM preregistrations WHERE prereg_id = ?", (prereg_id,))
    if existing is None:
        raise ValueError(f"no such preregistration: {prereg_id!r}")
    if existing.get("evaluated_at_utc"):
        raise PreregistrationAlreadyEvaluatedError(
            f"preregistration {prereg_id!r} was already evaluated at "
            f"{existing['evaluated_at_utc']!r} -- evidence is immutable once written. "
            "Register a NEW hypothesis row instead of re-evaluating this one."
        )

    en = _effective_n(
        rows, symbol_key=symbol_key, date_key=date_key, holding_days_key=holding_days_key,
    )
    boot = clustered_bootstrap(
        en["clusters"], value_key, n_resamples=n_resamples, ci_level=ci_level, seed=seed,
    )
    clears_own_floor = (
        en["effective_n"] >= existing["floor_effective_n"]
        and (en["span_days"] or 0) >= existing["floor_span_days"]
    )
    evidence_status = "ok" if (clears_own_floor and boot["status"] == "ok") else "insufficient_data"
    now = timeutils.stamp()

    cursor = journal.conn.execute(
        "UPDATE preregistrations SET evaluated_at_utc=?, effective_n=?, n_raw=?, span_days=?, "
        "point_estimate=?, ci_low=?, ci_high=?, ci_level=?, one_sided_p_below_zero=?, evidence_status=? "
        "WHERE prereg_id=? AND evaluated_at_utc IS NULL",
        (
            now.utc, en["effective_n"], en["n_raw"], en["span_days"],
            boot["point_estimate"], boot["ci_low"], boot["ci_high"], boot["ci_level"],
            boot["one_sided_p_below_zero"], evidence_status, prereg_id,
        ),
    )
    journal.conn.commit()
    if cursor.rowcount == 0:
        raise PreregistrationAlreadyEvaluatedError(
            f"preregistration {prereg_id!r} was evaluated by a concurrent writer "
            "between this call's read and write"
        )
    return journal.one("SELECT * FROM preregistrations WHERE prereg_id = ?", (prereg_id,))
