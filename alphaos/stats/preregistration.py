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

from datetime import date as _date
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


def evaluate_two_arm_hypothesis_pair(
    journal,
    prereg_id_pos: str,
    prereg_id_neg: str,
    as_of_utc: str,
    n_resamples: Optional[int] = None,
    seed: Optional[int] = None,
) -> dict[str, Any]:
    """S1b: the two-arm counterpart to ``evaluate_hypothesis()`` above, for
    a PAIR of directional hypotheses (H-PER-1P/H-PER-1N) sharing ONE frozen
    evidence population. Reuses this module's one-shot-per-row guard, but
    writes BOTH rows (plus one shared ``per_evidence_snapshots`` freeze) in
    a SINGLE transaction: either both rows get evaluated together against
    the identical frozen population, or neither does -- no partial,
    single-hypothesis write is possible.

    NEVER CALLED BY ANY SCAN, SCHEDULER, OR PRODUCTION DECISION PATH.
    ``alphaos.cards.per_evidence``/``alphaos.stats.two_arm`` are imported
    HERE, INSIDE this function, rather than at this module's top level --
    deliberately: ``alphaos.stats.preregistration`` (this module) IS
    imported by production-adjacent code (``alphaos.hypotheses.resolver``,
    which runs nightly), and ``alphaos.cards.per_evidence`` itself imports
    ``alphaos.cards.selector`` (S1a's still-unwired selector). A module-level
    import here would transitively pull the selector into the production
    import graph the moment ANY code imports ``preregistration.py`` --
    which happens routinely (every hypothesis resolution run) -- even
    though this specific function is never called from that path. The
    local import confines that exposure to the one place this function is
    actually INVOKED: the operator-invoked registration/evaluation CLI, and
    this module's own test suite (against isolated fixture journals only).

    Population/floor failures DEFER rather than consume the one-shot -- a
    DELIBERATE DIFFERENCE from ``evaluate_hypothesis()`` above, approved in
    the S1b statistical design review specifically for this card (whose
    population grows incrementally as new earnings events occur): nothing
    is written to either prereg row or to ``per_evidence_snapshots`` unless
    evaluation succeeds end to end -- population gates clear, the
    REGISTERED ``floor_effective_n``/``floor_span_days`` clear (reusing
    each hypothesis's own already-frozen floor, never a second
    separately-tuned number -- same principle as the Fable5 fix in
    ``alphaos.hypotheses.queries``), and the bootstrap itself returns
    ``status='ok'``. Only that fully-successful path freezes evidence and
    consumes the one-shot; this pair may be re-attempted, unlimited times,
    as more data accrues, right up until the one attempt that succeeds.

    The placebo diagnostic (Section 11 of the approved spec) is computed
    and stored in ``evidence_detail_json`` whenever it is available, but
    its unavailability NEVER blocks the formal primary evaluation --
    descriptive-only, no gate, no p-value, no BH-FDR membership.

    S1b INTEGRITY FOLLOW-UP -- two additional hard gates, both checked
    before ``build_primary_evidence()`` is ever called (cheap, params-only
    checks first): (1) both rows must carry an IDENTICAL, COMPLETE
    card/selector identity (``card_id``, ``card_version``,
    ``card_content_hash``, ``selector_version``, ``selector_semantic_hash``)
    that still matches what is LIVE right now (via
    ``per_evidence.fetch_active_per_card_identity()``) -- a pair frozen
    before this check existed (no identity fields at all) is refused, not
    silently accepted; (2) both rows' ``analysis_not_before`` must be
    present, identical, parseable, and not later than ``as_of_utc``'s own
    date. Both gates DEFER exactly like every other gate in this function
    (nothing written, one-shot not consumed) and are ADDITIVE to every
    existing population/sample/evidence gate below, never a replacement.

    Returns ``{"outcome": "deferred", "reason": ..., "detail": ...}``
    (nothing written) or ``{"outcome": "evaluated", "snapshot_id": ...,
    "pos": {...}, "neg": {...}}`` (both rows frozen). Raises
    ``PreregistrationAlreadyEvaluatedError`` if either row is already
    evaluated -- checked up front (fast, clear failure, before spending any
    time on evidence construction or bootstrapping) AND at the write
    itself via the same ``WHERE evaluated_at_utc IS NULL`` DB-level
    backstop ``evaluate_hypothesis()`` uses, so a hypothetical race between
    two callers is still caught -- and rolls back BOTH rows' writes (never
    just one) if that race is detected.
    """
    import json as _json

    from alphaos.cards import per_evidence
    from alphaos.stats import two_arm

    n_resamples = two_arm.DEFAULT_B if n_resamples is None else n_resamples
    seed = two_arm.DEFAULT_SEED if seed is None else seed

    pos_row = journal.one("SELECT * FROM preregistrations WHERE prereg_id = ?", (prereg_id_pos,))
    neg_row = journal.one("SELECT * FROM preregistrations WHERE prereg_id = ?", (prereg_id_neg,))
    if pos_row is None:
        raise ValueError(f"no such preregistration: {prereg_id_pos!r}")
    if neg_row is None:
        raise ValueError(f"no such preregistration: {prereg_id_neg!r}")
    if pos_row.get("evaluated_at_utc") or neg_row.get("evaluated_at_utc"):
        raise PreregistrationAlreadyEvaluatedError(
            f"one or both of {prereg_id_pos!r}/{prereg_id_neg!r} was already evaluated -- "
            "evidence is immutable once written. Register a NEW hypothesis pair instead of "
            "re-evaluating this one."
        )
    if (pos_row["floor_effective_n"] != neg_row["floor_effective_n"]
            or pos_row["floor_span_days"] != neg_row["floor_span_days"]):
        raise ValueError(
            f"H-PER-1P/H-PER-1N floor mismatch ({prereg_id_pos!r} vs {prereg_id_neg!r}) -- both "
            "hypotheses in a pair must share the identical floor, since they share the identical "
            "frozen population; this indicates a registration bug, not a data condition."
        )
    floor_effective_n = pos_row["floor_effective_n"]
    floor_span_days = pos_row["floor_span_days"]

    # S1b integrity follow-up: verify BOTH rows carry an identical,
    # complete card/selector identity, AND that it still matches what is
    # actually live right now -- refuses (defers, writes nothing) rather
    # than freeze evidence against a card/selector state that has since
    # drifted, or a pair whose two halves were frozen against DIFFERENT
    # states (only possible from a registration bug). A pair registered
    # before this check existed -- whose params_json has no identity
    # fields at all, e.g. the original H-PER-1P/H-PER-1N pair -- is
    # refused here too: exactly the "the currently registered incomplete
    # pair is not accidentally used as the formal verdict pair" guarantee
    # this follow-up exists to provide.
    identity_keys = ("card_id", "card_version", "card_content_hash", "selector_version", "selector_semantic_hash")
    pos_params = _json.loads(pos_row["params_json"]) if pos_row.get("params_json") else {}
    neg_params = _json.loads(neg_row["params_json"]) if neg_row.get("params_json") else {}
    pos_identity = {k: pos_params.get(k) for k in identity_keys}
    neg_identity = {k: neg_params.get(k) for k in identity_keys}
    if any(v is None for v in pos_identity.values()) or any(v is None for v in neg_identity.values()):
        return {
            "outcome": "deferred", "reason": "identity_fields_missing",
            "detail": {"pos_identity": pos_identity, "neg_identity": neg_identity},
        }
    if pos_identity != neg_identity:
        return {
            "outcome": "deferred", "reason": "identity_mismatch_between_paired_rows",
            "detail": {"pos_identity": pos_identity, "neg_identity": neg_identity},
        }
    try:
        live_identity = per_evidence.fetch_active_per_card_identity(journal)
    except per_evidence.CardIdentityError as exc:
        return {"outcome": "deferred", "reason": "live_identity_unavailable", "detail": {"error": str(exc)}}
    if pos_identity != live_identity:
        return {
            "outcome": "deferred", "reason": "identity_drifted_from_live_state",
            "detail": {"frozen_identity": pos_identity, "live_identity": live_identity},
        }

    # analysis_not_before hard gate -- additional to, never a replacement
    # for, the population/sample/evidence gates below. Uses the SAME
    # as_of_utc the caller already injects (never wall-clock reads
    # anywhere in this function), matching this function's existing
    # determinism/testability contract.
    pos_anb = pos_row.get("analysis_not_before")
    neg_anb = neg_row.get("analysis_not_before")
    if not pos_anb or not neg_anb or pos_anb != neg_anb:
        return {
            "outcome": "deferred", "reason": "analysis_not_before_missing_or_mismatched",
            "detail": {"pos_analysis_not_before": pos_anb, "neg_analysis_not_before": neg_anb},
        }
    try:
        analysis_not_before_date = _date.fromisoformat(str(pos_anb)[:10])
    except ValueError:
        return {
            "outcome": "deferred", "reason": "analysis_not_before_unparseable",
            "detail": {"analysis_not_before": pos_anb},
        }
    try:
        as_of_date = _date.fromisoformat(str(as_of_utc)[:10])
    except ValueError:
        raise ValueError(f"as_of_utc {as_of_utc!r} is not a parseable date") from None
    if as_of_date < analysis_not_before_date:
        return {
            "outcome": "deferred", "reason": "before_analysis_not_before",
            "detail": {"analysis_not_before": pos_anb, "as_of_utc": as_of_utc},
        }

    primary = per_evidence.build_primary_evidence(journal, as_of_utc)
    if primary.status != "ok":
        return {"outcome": "deferred", "reason": primary.reason, "detail": primary.detail}

    n_per_clusters = len(primary.per_clusters)
    span_days = primary.detail.get("span_days") or 0.0
    if n_per_clusters < floor_effective_n or span_days < floor_span_days:
        return {
            "outcome": "deferred",
            "reason": "registered_floor_not_cleared",
            "detail": {
                **primary.detail, "n_per_clusters": n_per_clusters,
                "floor_effective_n": floor_effective_n, "floor_span_days": floor_span_days,
            },
        }

    boot = two_arm.two_arm_bootstrap(
        primary.per_clusters, primary.control_clusters, n_resamples=n_resamples, seed=seed,
    )
    if boot["status"] != "ok":
        return {"outcome": "deferred", "reason": f"bootstrap_{boot['status']}", "detail": boot}

    placebo = per_evidence.build_placebo_evidence(journal, as_of_utc, primary.valid_events)
    placebo_detail: dict[str, Any] = {"status": placebo.status, "reason": placebo.reason}
    if placebo.status == "ok":
        placebo_boot = two_arm.two_arm_bootstrap(
            placebo.per_clusters, placebo.control_clusters, n_resamples=n_resamples, seed=seed,
        )
        placebo_detail["bootstrap"] = placebo_boot

    snapshot_id = new_id("perev")
    snapshot_rows = per_evidence.canonical_snapshot_rows(
        primary, placebo if placebo.status == "ok" else None,
    )

    now = timeutils.stamp()
    evidence_detail_common = {
        "snapshot_id": snapshot_id,
        "n_resamples": n_resamples,
        "seed": seed,
        "n_valid_replicates": boot["n_valid_replicates"],
        "n_invalid_replicates": boot["n_invalid_replicates"],
        "n_control_clusters": boot["n_control_clusters"],
        "population_detail": primary.detail,
        "placebo": placebo_detail,
    }

    cursor_pos = journal.conn.execute(
        "UPDATE preregistrations SET evaluated_at_utc=?, effective_n=?, n_raw=?, span_days=?, "
        "point_estimate=?, ci_low=?, ci_high=?, ci_level=?, one_sided_p_below_zero=?, evidence_status=?, "
        "evidence_detail_json=? WHERE prereg_id=? AND evaluated_at_utc IS NULL",
        (
            now.utc, n_per_clusters, primary.detail.get("n_per_raw"), span_days,
            boot["point_estimate"], boot["ci_low"], boot["ci_high"], boot["ci_level"], boot["p_pos"], "ok",
            _json.dumps({**evidence_detail_common, "hypothesis": "H-PER-1P"}, default=str),
            prereg_id_pos,
        ),
    )
    cursor_neg = journal.conn.execute(
        "UPDATE preregistrations SET evaluated_at_utc=?, effective_n=?, n_raw=?, span_days=?, "
        "point_estimate=?, ci_low=?, ci_high=?, ci_level=?, one_sided_p_below_zero=?, evidence_status=?, "
        "evidence_detail_json=? WHERE prereg_id=? AND evaluated_at_utc IS NULL",
        (
            # H-PER-1N's directional frame (spec Section 7): negate the
            # estimate and swap+negate the CI bounds, never re-derive from
            # scratch, so the two rows can never disagree about direction.
            now.utc, n_per_clusters, primary.detail.get("n_per_raw"), span_days,
            -boot["point_estimate"], -boot["ci_high"], -boot["ci_low"], boot["ci_level"], boot["p_neg"], "ok",
            _json.dumps({**evidence_detail_common, "hypothesis": "H-PER-1N"}, default=str),
            prereg_id_neg,
        ),
    )
    if cursor_pos.rowcount == 0 or cursor_neg.rowcount == 0:
        journal.conn.rollback()
        raise PreregistrationAlreadyEvaluatedError(
            f"one or both of {prereg_id_pos!r}/{prereg_id_neg!r} was evaluated by a concurrent "
            "writer between this call's read and write"
        )

    for row in snapshot_rows:
        journal.conn.execute(
            "INSERT INTO per_evidence_snapshots (snapshot_id, arm, candidate_id, symbol, event_key, "
            "market_date, tier, outcome_value, cluster_id, stratum_key, control_fallback, excluded_reason, "
            "created_at_utc, created_at_sgt) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                snapshot_id, row["arm"], row["candidate_id"], row["symbol"], row["event_key"],
                row["market_date"], row["tier"], row["outcome_value"], row["cluster_id"],
                row["stratum_key"], row["control_fallback"], row["excluded_reason"],
                now.utc, now.local_sgt,
            ),
        )
    journal.conn.commit()

    return {
        "outcome": "evaluated",
        "snapshot_id": snapshot_id,
        "pos": {
            "point_estimate": boot["point_estimate"], "ci_low": boot["ci_low"], "ci_high": boot["ci_high"],
            "p_value": boot["p_pos"],
        },
        "neg": {
            "point_estimate": -boot["point_estimate"], "ci_low": -boot["ci_high"], "ci_high": -boot["ci_low"],
            "p_value": boot["p_neg"],
        },
    }
