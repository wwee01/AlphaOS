"""AlphaOS one-shot CLI runners.

    python -m alphaos scan_once
    python -m alphaos monitor_once
    python -m alphaos generate_daily_report

Plus helpers: status, seed_demo, kill (engage/release), dashboard (hint).
These are one-shot commands, not a daemon (no scheduler in v1).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from alphaos.config.settings import SettingsError, load_settings
from alphaos.orchestrator import Orchestrator
from alphaos.safety import KillSwitch
from alphaos.scheduler import JobRunner


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_scan_once(orch: Orchestrator) -> int:
    summary = orch.run_scan_once()
    _print({"scan_once": summary.as_dict()})
    return 0


def cmd_monitor_once(orch: Orchestrator) -> int:
    _print({"monitor_once": orch.run_monitor_once()})
    return 0


def cmd_generate_daily_report(orch: Orchestrator) -> int:
    rep = orch.generate_daily_report()
    print(rep["content_md"])
    return 0


def cmd_interest_scan(orch: Orchestrator) -> int:
    """Roadmap 2.3: interest scan -> candidate packets -> AI category labels ->
    existing gates -> proposals (manual approval still required; no auto-exec)."""
    _print({"interest_scan": orch.run_scan_once().as_dict()})
    return 0


def cmd_proposals(orch: Orchestrator) -> int:
    views = orch.list_open_proposals()
    _print({"open_proposals": views, "count": len(views)})
    return 0


def cmd_approve(orch: Orchestrator, proposal_id: str, approve_margin: bool) -> int:
    ok, msg = orch.approve_proposal(proposal_id, approver="cli", approve_margin=approve_margin)
    _print({"approve": {"proposal_id": proposal_id, "ok": ok, "message": msg}})
    return 0 if ok else 1


def cmd_reject(orch: Orchestrator, proposal_id: str, reason: str) -> int:
    ok, msg = orch.reject_proposal(proposal_id, approver="cli", reason=reason)
    _print({"reject": {"proposal_id": proposal_id, "ok": ok, "message": msg}})
    return 0 if ok else 1


def cmd_calibration_report(orch: Orchestrator) -> int:
    from alphaos.reports.cost_calibration import render_markdown

    rep = orch.calibration_report()
    print(render_markdown(rep))
    print()
    _print({"calibration": rep["summary"], "recommended_model": rep["recommended_model"]})
    return 0


def cmd_attribution_report(orch: Orchestrator) -> int:
    """User-override attribution learning report (heuristic; never a significance
    claim). PURE READ — no execution, no ledger writes."""
    from alphaos.reports.attribution import render_markdown

    rep = orch.attribution_report()
    print(render_markdown(rep))
    print()
    _print({"attribution_report": rep})
    return 0


def cmd_backfill_mfe_mae(orch: Orchestrator) -> int:
    """Backfill MFE/MAE on closed trades recorded before intra-trade excursion
    tracking existed. Idempotent; write-only to trade_outcomes.mfe/.mae/
    .mfe_mae_source; no exit/order/execution behavior change."""
    res = orch.backfill_mfe_mae()
    _print({"backfill_mfe_mae": res})
    return 0


def cmd_backfill_regime_days(orch: Orchestrator) -> int:
    """REG-1 one-off: extend SPY history, classify the full series into
    regime_days, and stamp pre-existing candidate_packets rows still missing
    a regime. Idempotent; measurement only -- no decision/execution change."""
    res = orch.backfill_regime_days()
    _print({"backfill_regime_days": res})
    return 0 if "error" not in res else 1


def cmd_outcomes_update(orch: Orchestrator) -> int:
    """Counterfactual outcome tracker: seed + resolve candidate_outcomes rows
    (candidates/proposals/rejects/armed-watch/user-overrides) with 1/3/5-day
    forward returns + bracket replay. PURE MEASUREMENT — no execution/approval
    change; idempotent."""
    res = orch.outcomes_update()
    _print({"outcomes_update": res})
    return 0


def cmd_regime_arming_report(orch: Orchestrator) -> int:
    """REG-1: the shadow arming-map scorer report. PURE READ -- pure ledger
    math over existing shadow rows; nothing armed/disarmed for real."""
    from alphaos.reports.regime_arming_scorer import render_markdown

    rep = orch.regime_arming_report()
    print(render_markdown(rep))
    print()
    _print({"regime_arming_report": rep})
    return 0


def cmd_baseline_report(orch: Orchestrator) -> int:
    """BASELINE: does the AI add R report. PURE READ -- pure ledger math
    over existing shadow rows; nothing gated for real."""
    from alphaos.reports.baseline_report import render_markdown

    rep = orch.baseline_report()
    print(render_markdown(rep))
    print()
    _print({"baseline_report": rep})
    return 0


def cmd_baseline_register(orch: Orchestrator) -> int:
    """BASELINE spec item 6: register the pre-registration block (=
    preregistrations row #1, per Prime Directive #4) -- one-off, operator-
    invoked, idempotent (refuses a duplicate rather than creating a second
    row for the same hypothesis, since register_hypothesis() itself is NOT
    idempotent -- every call creates a new row)."""
    from alphaos.reports.baseline_report import FLOOR_DAY_BLOCKS, FLOOR_SPAN_DAYS
    from alphaos.stats.preregistration import register_hypothesis

    hypothesis = (
        "AI adds >= +0.05R mean paired ai_delta_r over threshold_v1 on "
        "proposed candidates, conditional on labeller reach"
    )
    metric = "mean_ai_delta_r = mean(candidate_outcomes.replay_r - shadow_baseline_decisions.replay_r), threshold_v1"
    existing = orch.journal.one(
        "SELECT prereg_id, registered_at_utc FROM preregistrations WHERE hypothesis = ? AND metric = ?",
        (hypothesis, metric),
    )
    if existing:
        print(f"Already registered: {existing['prereg_id']} (at {existing['registered_at_utc']}) -- no-op.")
        _print({"baseline_register": {"status": "already_registered", **existing}})
        return 0

    prereg_id = register_hypothesis(
        orch.journal, hypothesis, metric,
        # register_hypothesis()'s own parameter is named floor_effective_n
        # (PORT-1's generic vocabulary for this bar, regardless of counting
        # axis) -- BASELINE_report's own constant is FLOOR_DAY_BLOCKS since
        # it counts day-blocks, not PORT-1's symbol-clustered effective_n.
        floor_effective_n=FLOOR_DAY_BLOCKS, floor_span_days=FLOOR_SPAN_DAYS,
        analysis_not_before="2026-09-07",
        params={"rule_version": "threshold_v1", "target_delta_r": 0.05},
    )
    print(f"Registered: {prereg_id}")
    _print({"baseline_register": {"status": "registered", "prereg_id": prereg_id}})
    return 0


def cmd_debate_register(orch: Orchestrator) -> int:
    """PR14 spec: register the bear-debate pre-registration block BEFORE
    any real (non-mock) vote can accumulate evidence toward it (Prime
    Directive: pre-registration discipline) -- one-off, operator-invoked,
    idempotent (refuses a duplicate rather than creating a second row for
    the same hypothesis, since register_hypothesis() itself is NOT
    idempotent -- every call creates a new row). Mirrors
    cmd_baseline_register()'s own shape exactly.

    Gates a possible v0.1 expansion to a full triad (bull/neutral agents
    alongside bear): this hypothesis resolving TRUE is the evidence bar
    for that expansion, not an assumption made now.
    """
    from alphaos.stats.preregistration import register_hypothesis

    hypothesis = (
        "Proposals with a high-conviction bear-oppose vote (agent_votes.stance="
        "'oppose' AND conviction >= 0.7) underperform all other proposed trades "
        "by >= 0.3R mean replay_r, conditional on effective_n >= 30 over a "
        "trailing 28-day span"
    )
    metric = (
        "mean_bear_oppose_delta_r = mean(candidate_outcomes.replay_r | "
        "oppose_high_conviction_cohort) - mean(candidate_outcomes.replay_r | "
        "complement_cohort), oppose_high_conviction_v1"
    )
    existing = orch.journal.one(
        "SELECT prereg_id, registered_at_utc FROM preregistrations WHERE hypothesis = ? AND metric = ?",
        (hypothesis, metric),
    )
    if existing:
        print(f"Already registered: {existing['prereg_id']} (at {existing['registered_at_utc']}) -- no-op.")
        _print({"debate_register": {"status": "already_registered", **existing}})
        return 0

    prereg_id = register_hypothesis(
        orch.journal, hypothesis, metric,
        floor_effective_n=30, floor_span_days=28.0,
        # debate_shadow_enabled defaults False -- an operator must first opt
        # in before any real vote accumulates, then needs a 28-day span PLUS
        # enough trading days for effective_n>=30 (not every proposed trade
        # gets a high-conviction oppose vote). ~60 days from registration is
        # a conservative buffer for both, not a claim evidence will exist by
        # then. If the operator never enables debate_shadow_enabled, this
        # date elapses with zero data -- harmless: floor_effective_n=30 below
        # is the actual guard (evaluate_hypothesis reports "insufficient
        # data" under it regardless of the calendar), so a premature date
        # can never produce a spurious/early result (audit LOW finding).
        analysis_not_before="2026-09-08",
        params={"rule_version": "oppose_high_conviction_v1", "stance": "oppose", "conviction_floor": 0.7,
                "target_delta_r": -0.3},
    )
    print(f"Registered: {prereg_id}")
    _print({"debate_register": {"status": "registered", "prereg_id": prereg_id}})
    return 0


def cmd_hypothesis_seed(orch: Orchestrator) -> int:
    """PR12: idempotently register every SEEDED_HYPOTHESES entry. Safe to
    run repeatedly -- a no-op past the first call per hypothesis_id."""
    seeded = orch.hypothesis_seed()
    _print({"hypothesis_seed": {"count": len(seeded), "hypothesis_ids": [h["hypothesis_id"] for h in seeded]}})
    return 0


# S1b: registration/evaluation text for the paired H-PER-1P/H-PER-1N
# hypotheses -- module-level constants (not inlined into the two command
# functions below) so cmd_per_register's own idempotent lookup and
# cmd_per_evaluate's own prereg_id lookup are guaranteed to agree on the
# exact same (hypothesis, metric) strings.
_PER_HYPOTHESIS_POS = (
    "H-PER-1P: post_earnings_reaction_v1 candidates have a POSITIVE mean "
    "5-trading-day market-adjusted excess outcome over contemporaneous "
    "date x tier default-card candidates"
)
_PER_METRIC_POS = "per_excess_market_adjusted_5d, smooth_weight_joint_bootstrap_v1"
_PER_HYPOTHESIS_NEG = (
    "H-PER-1N: post_earnings_reaction_v1 candidates have a NEGATIVE mean "
    "5-trading-day market-adjusted excess outcome over contemporaneous "
    "date x tier default-card candidates"
)
_PER_METRIC_NEG = "per_excess_market_adjusted_5d_negated, smooth_weight_joint_bootstrap_v1"
# Audit-fixup (doc-only): floor_span_days (90.0) matches per_evidence.py's
# own MIN_SPAN_DAYS constant. floor_effective_n (20 PER clusters) has no
# equivalent constant in per_evidence.py -- that module's own population
# gate uses MIN_PER_RAW_N=25 on raw events; the 20-cluster EFFECTIVE-n
# floor exists only here and is enforced solely via this registered value,
# checked by evaluate_two_arm_hypothesis_pair() against the count of PER
# clusters it actually builds (see that function's own
# docstring on reusing each hypothesis's own frozen floor).
_PER_FLOOR_EFFECTIVE_N = 20
_PER_FLOOR_SPAN_DAYS = 90.0


def cmd_per_register(orch: Orchestrator) -> int:
    """SETUP-1 S1b: register BOTH H-PER-1P and H-PER-1N -- one-off,
    operator-invoked, idempotent (refuses to create a duplicate pair if
    either half is already registered, since register_hypothesis() itself
    is not idempotent). Registering does NOT evaluate anything -- no
    candidate_outcomes are read, no bootstrap runs, nothing is gated. The
    frozen S1b statistical design (estimand, control ladder, bootstrap
    method, floors, seed, directionality, placebo construction) is recorded
    verbatim in each row's own ``params_json`` so a later drift between
    this command's assumptions and the actually-implemented engine is at
    least detectable by inspection.

    NEVER invoked as part of building/testing S1b -- this command exists
    for a human operator to run explicitly, later, once the S1b design is
    confirmed ready for production preregistration (per the operator's own
    explicit instruction: "do not silently write H-PER-1P or H-PER-1N into
    an actual production journal as part of branch construction")."""
    from alphaos.cards.selector import PER_CARD_ID, SELECTOR_VERSION
    from alphaos.cards.per_evidence import (
        CROSS_ARM_EXCLUSION_TRADING_DAYS, MAX_EXCLUSION_SHARE, MAX_POOLED_FALLBACK_SHARE,
        MAX_SYMBOL_CONCENTRATION, MIN_CONTROL_RAW_N, MIN_DISTINCT_MONTHS, MIN_PER_RAW_N,
        OUTCOME_METRIC, OUTCOME_WINDOW_TRADING_DAYS, PLACEBO_SHIFT_TRADING_DAYS,
        PLACEBO_TOLERANCE_TRADING_DAYS, RUNG1_MIN_CONTROLS, RUNG2_MIN_CONTROLS,
    )
    from alphaos.stats.preregistration import register_hypothesis
    from alphaos.stats.two_arm import DEFAULT_B, DEFAULT_CI_LEVEL, DEFAULT_SEED, MIN_VALID_REPLICATE_FRACTION

    existing_pos = orch.journal.one(
        "SELECT prereg_id, registered_at_utc FROM preregistrations WHERE hypothesis = ? AND metric = ?",
        (_PER_HYPOTHESIS_POS, _PER_METRIC_POS),
    )
    existing_neg = orch.journal.one(
        "SELECT prereg_id, registered_at_utc FROM preregistrations WHERE hypothesis = ? AND metric = ?",
        (_PER_HYPOTHESIS_NEG, _PER_METRIC_NEG),
    )
    if existing_pos or existing_neg:
        print("Already registered (one or both halves) -- no-op.")
        _print({"per_register": {
            "status": "already_registered",
            "pos": existing_pos, "neg": existing_neg,
        }})
        return 0

    frozen_params = {
        "card_id": PER_CARD_ID,
        "selector_version": SELECTOR_VERSION,
        "outcome_metric": OUTCOME_METRIC,
        "outcome_window_trading_days": OUTCOME_WINDOW_TRADING_DAYS,
        "cross_arm_exclusion_trading_days": CROSS_ARM_EXCLUSION_TRADING_DAYS,
        "rung1_min_controls": RUNG1_MIN_CONTROLS,
        "rung2_min_controls": RUNG2_MIN_CONTROLS,
        "max_pooled_fallback_share": MAX_POOLED_FALLBACK_SHARE,
        "max_exclusion_share": MAX_EXCLUSION_SHARE,
        "min_per_raw_n": MIN_PER_RAW_N,
        "min_distinct_months": MIN_DISTINCT_MONTHS,
        "max_symbol_concentration": MAX_SYMBOL_CONCENTRATION,
        "min_control_raw_n": MIN_CONTROL_RAW_N,
        "placebo_shift_trading_days": PLACEBO_SHIFT_TRADING_DAYS,
        "placebo_tolerance_trading_days": PLACEBO_TOLERANCE_TRADING_DAYS,
        "bootstrap_method": "smooth_weight_joint_clustered_bootstrap_v1",
        "n_resamples": DEFAULT_B,
        "ci_level": DEFAULT_CI_LEVEL,
        "seed": DEFAULT_SEED,
        "min_valid_replicate_fraction": MIN_VALID_REPLICATE_FRACTION,
        "statistic": "arithmetic_mean",
        "tie_rule": "inclusive_both_tails",
        "finite_replicate_correction": "(extreme_count + 1) / (n_valid_replicates + 1)",
    }
    prereg_pos = register_hypothesis(
        orch.journal, _PER_HYPOTHESIS_POS, _PER_METRIC_POS,
        floor_effective_n=_PER_FLOOR_EFFECTIVE_N, floor_span_days=_PER_FLOOR_SPAN_DAYS,
        analysis_not_before="2026-10-15",
        params={**frozen_params, "direction": "positive"},
    )
    prereg_neg = register_hypothesis(
        orch.journal, _PER_HYPOTHESIS_NEG, _PER_METRIC_NEG,
        floor_effective_n=_PER_FLOOR_EFFECTIVE_N, floor_span_days=_PER_FLOOR_SPAN_DAYS,
        analysis_not_before="2026-10-15",
        params={**frozen_params, "direction": "negative"},
    )
    print(f"Registered pair: pos={prereg_pos} neg={prereg_neg}")
    _print({"per_register": {"status": "registered", "prereg_id_pos": prereg_pos, "prereg_id_neg": prereg_neg}})
    return 0


# S1b integrity follow-up: the CORRECTED pair's identity constants
# (PER_HYPOTHESIS_POS_V2 etc.) now live in alphaos.cards.per_evidence --
# the single source of truth both this command and the (dormant)
# s1c_activation_preflight() diagnostic import, so there is exactly one
# place that can drift. See that module's own comment for why a v2 pair
# with distinct hypothesis/metric text (never mutating or reusing
# prereg_eb3ab6bda5a4/prereg_2821a9e1b931) is this codebase's correct,
# disclosed way to correct a preregistration with no formal
# supersedes/withdrawn/version field on the table itself.


def cmd_per_register_v2(orch: Orchestrator) -> int:
    """SETUP-1 S1b integrity follow-up: register the CORRECTED H-PER-1P-v2/
    H-PER-1N-v2 pair -- identical statistical design to cmd_per_register()
    above, PLUS the three identity fields the original registration
    omitted: ``card_version``, exact ``card_content_hash`` (from
    ``setup_cards``, PR13's own hash-guarded registry -- never hand-typed),
    and exact ``selector_semantic_hash`` (from the live golden-fixture
    hash, freshly verified, never assumed).

    Refuses to register -- prints a clear error, writes nothing, returns
    1 -- if ``fetch_active_per_card_identity()`` raises ``CardIdentityError``
    (card absent / wrong version / wrong state / requires_selector
    mismatch / selector semantic hash unavailable or drifted). Otherwise
    idempotent exactly like cmd_per_register().

    NEVER invoked as part of building/testing this follow-up -- operator-
    invoked only, later, against the real production journal (see this
    build's own final report for the backup-restoration reminder)."""
    from alphaos.cards.selector import PER_CARD_ID
    from alphaos.cards.per_evidence import (
        CROSS_ARM_EXCLUSION_TRADING_DAYS, MAX_EXCLUSION_SHARE, MAX_POOLED_FALLBACK_SHARE,
        MAX_SYMBOL_CONCENTRATION, MIN_CONTROL_RAW_N, MIN_DISTINCT_MONTHS, MIN_PER_RAW_N,
        OUTCOME_METRIC, OUTCOME_WINDOW_TRADING_DAYS, PLACEBO_SHIFT_TRADING_DAYS,
        PLACEBO_TOLERANCE_TRADING_DAYS, RUNG1_MIN_CONTROLS, RUNG2_MIN_CONTROLS,
        PER_HYPOTHESIS_NEG_V2, PER_HYPOTHESIS_POS_V2, PER_METRIC_NEG_V2, PER_METRIC_POS_V2,
        CardIdentityError, fetch_active_per_card_identity,
    )
    from alphaos.stats.preregistration import register_hypothesis
    from alphaos.stats.two_arm import DEFAULT_B, DEFAULT_CI_LEVEL, DEFAULT_SEED, MIN_VALID_REPLICATE_FRACTION

    existing_pos = orch.journal.one(
        "SELECT prereg_id, registered_at_utc FROM preregistrations WHERE hypothesis = ? AND metric = ?",
        (PER_HYPOTHESIS_POS_V2, PER_METRIC_POS_V2),
    )
    existing_neg = orch.journal.one(
        "SELECT prereg_id, registered_at_utc FROM preregistrations WHERE hypothesis = ? AND metric = ?",
        (PER_HYPOTHESIS_NEG_V2, PER_METRIC_NEG_V2),
    )
    if existing_pos or existing_neg:
        print("Already registered (one or both halves) -- no-op.")
        _print({"per_register_v2": {
            "status": "already_registered",
            "pos": existing_pos, "neg": existing_neg,
        }})
        return 0

    try:
        identity = fetch_active_per_card_identity(orch.journal)
    except CardIdentityError as exc:
        print(f"Refusing to register: {exc}")
        _print({"per_register_v2": {"status": "refused", "reason": str(exc)}})
        return 1

    assert identity["card_id"] == PER_CARD_ID  # fetch_active_per_card_identity()'s own contract

    frozen_params = {
        "card_id": identity["card_id"],
        "card_version": identity["card_version"],
        "card_content_hash": identity["card_content_hash"],
        "selector_version": identity["selector_version"],
        "selector_semantic_hash": identity["selector_semantic_hash"],
        "outcome_metric": OUTCOME_METRIC,
        "outcome_window_trading_days": OUTCOME_WINDOW_TRADING_DAYS,
        "cross_arm_exclusion_trading_days": CROSS_ARM_EXCLUSION_TRADING_DAYS,
        "rung1_min_controls": RUNG1_MIN_CONTROLS,
        "rung2_min_controls": RUNG2_MIN_CONTROLS,
        "max_pooled_fallback_share": MAX_POOLED_FALLBACK_SHARE,
        "max_exclusion_share": MAX_EXCLUSION_SHARE,
        "min_per_raw_n": MIN_PER_RAW_N,
        "min_distinct_months": MIN_DISTINCT_MONTHS,
        "max_symbol_concentration": MAX_SYMBOL_CONCENTRATION,
        "min_control_raw_n": MIN_CONTROL_RAW_N,
        "placebo_shift_trading_days": PLACEBO_SHIFT_TRADING_DAYS,
        "placebo_tolerance_trading_days": PLACEBO_TOLERANCE_TRADING_DAYS,
        "bootstrap_method": "smooth_weight_joint_clustered_bootstrap_v1",
        "n_resamples": DEFAULT_B,
        "ci_level": DEFAULT_CI_LEVEL,
        "seed": DEFAULT_SEED,
        "min_valid_replicate_fraction": MIN_VALID_REPLICATE_FRACTION,
        "statistic": "arithmetic_mean",
        "tie_rule": "inclusive_both_tails",
        "finite_replicate_correction": "(extreme_count + 1) / (n_valid_replicates + 1)",
    }
    prereg_pos = register_hypothesis(
        orch.journal, PER_HYPOTHESIS_POS_V2, PER_METRIC_POS_V2,
        floor_effective_n=_PER_FLOOR_EFFECTIVE_N, floor_span_days=_PER_FLOOR_SPAN_DAYS,
        analysis_not_before="2026-10-15",
        params={**frozen_params, "direction": "positive"},
    )
    prereg_neg = register_hypothesis(
        orch.journal, PER_HYPOTHESIS_NEG_V2, PER_METRIC_NEG_V2,
        floor_effective_n=_PER_FLOOR_EFFECTIVE_N, floor_span_days=_PER_FLOOR_SPAN_DAYS,
        analysis_not_before="2026-10-15",
        params={**frozen_params, "direction": "negative"},
    )
    print(f"Registered corrected pair: pos={prereg_pos} neg={prereg_neg}")
    _print({"per_register_v2": {"status": "registered", "prereg_id_pos": prereg_pos, "prereg_id_neg": prereg_neg}})
    return 0


def cmd_per_evaluate(orch: Orchestrator) -> int:
    """SETUP-1 S1b: run the ONE atomic paired evaluation for H-PER-1P/
    H-PER-1N, as of right now. Operator-invoked only -- never scheduled,
    never run by any scan. Looks the pair up by its own registered
    (hypothesis, metric) text (same convention as cmd_per_register) rather
    than taking prereg_ids as arguments, so there is exactly one thing to
    type to run this.

    Population/floor failures print a 'deferred' result and touch nothing
    (re-runnable later as more data accrues); a genuine success freezes
    both rows plus the shared evidence snapshot in one transaction. See
    alphaos.stats.preregistration.evaluate_two_arm_hypothesis_pair()'s own
    docstring for the complete contract."""
    from alphaos.stats.preregistration import (
        PreregistrationAlreadyEvaluatedError,
        evaluate_two_arm_hypothesis_pair,
    )
    from alphaos.util import timeutils

    pos_row = orch.journal.one(
        "SELECT prereg_id FROM preregistrations WHERE hypothesis = ? AND metric = ?",
        (_PER_HYPOTHESIS_POS, _PER_METRIC_POS),
    )
    neg_row = orch.journal.one(
        "SELECT prereg_id FROM preregistrations WHERE hypothesis = ? AND metric = ?",
        (_PER_HYPOTHESIS_NEG, _PER_METRIC_NEG),
    )
    if pos_row is None or neg_row is None:
        print("H-PER-1P/H-PER-1N are not registered yet -- run per_register first.")
        _print({"per_evaluate": {"status": "not_registered"}})
        return 1

    as_of_utc = timeutils.now_utc().isoformat()
    try:
        result = evaluate_two_arm_hypothesis_pair(
            orch.journal, pos_row["prereg_id"], neg_row["prereg_id"], as_of_utc,
        )
    except PreregistrationAlreadyEvaluatedError as exc:
        print(f"Already evaluated -- evidence is immutable once written: {exc}")
        _print({"per_evaluate": {"status": "already_evaluated"}})
        return 0
    print(f"Evaluation outcome: {result['outcome']}")
    _print({"per_evaluate": result})
    return 0


def cmd_hypothesis_resolve(orch: Orchestrator) -> int:
    """PR12: one resolver pass -- evaluate any hypothesis_proposals row past
    its own calendar + sample-size floor, then refresh last_verdict/
    last_q_value for the whole evaluated family. PURE WRITE-ONLY to
    hypothesis_proposals/preregistrations; nothing gated for real."""
    result = orch.hypothesis_resolve()
    _print({"hypothesis_resolve": result})
    return 0


def cmd_hypothesis_report(orch: Orchestrator) -> int:
    """PR12: the registry status report -- PURE READ."""
    from alphaos.reports.hypothesis_report import render_markdown

    rep = orch.hypothesis_report()
    print(render_markdown(rep))
    print()
    _print({"hypothesis_report": rep})
    return 0


def cmd_card_scoreboard(orch: Orchestrator) -> int:
    """PR13 slice 1: per-card scoreboard -- PURE READ."""
    from alphaos.cards.scoreboard import render_markdown

    rep = orch.card_scoreboard_report()
    print(render_markdown(rep))
    print()
    _print({"card_scoreboard": rep})
    return 0


def cmd_card_demotion_check(orch: Orchestrator) -> int:
    """PR13 slice 1: one daily pass -- snapshot every live_eligible card,
    demote (+ alert) any card with >= 2 consecutive breach snapshots."""
    result = orch.card_demotion_check()
    _print({"card_demotion_check": result})
    return 0


def cmd_setup_evidence_report(orch: Orchestrator) -> int:
    """EVID-1: per-setup-version evidence report -- PURE READ."""
    from alphaos.cards.setup_evidence import render_markdown

    rep = orch.setup_evidence_report()
    print(render_markdown(rep))
    print()
    _print({"setup_evidence_report": rep})
    return 0


def cmd_setup_population_breakdown(orch: Orchestrator, card_id: str, card_version: int, metric_key: str) -> int:
    """EVID-1: per-population evidence breakdown for one setup-version -- PURE READ."""
    rep = orch.setup_population_breakdown(card_id, card_version, metric_key)
    _print({"setup_population_breakdown": rep})
    return 0


def cmd_hypothesis_mark_status(
    orch: Orchestrator, hypothesis_id: str, new_status: str, decided_by: str, confirm: bool,
) -> int:
    """PR13 slice 2: the only way MET/FAILED/WITHDRAWN ever gets set --
    always an explicit operator action, never a scheduled job. Without
    --confirm, this is a dry-run preview showing the hypothesis's own claim
    text + current status -- no write happens either way unless --confirm
    is passed and the row is still eligible (Fable5 strategy review,
    2026-07-10: this write is permanent by design -- reversible decision #9
    accepted no undo path, but the write itself had no preview ceremony at
    all, unlike its downstream sibling card_promote; matches that command's
    dry-run-by-default pattern exactly)."""
    from alphaos.hypotheses import check_status_change_preconditions

    check = check_status_change_preconditions(orch.journal, hypothesis_id, new_status)
    if not check["eligible"]:
        print(f"NOT ELIGIBLE -- {check['reason_code']}: {check['detail']}")
        _print({"hypothesis_mark_status": {"status": "not_eligible", **check}})
        return 1

    if not confirm:
        print(
            f"ELIGIBLE -- {hypothesis_id} ({check['current_status']} -> {new_status}). "
            f"Claim: {check['claim']!r}. This is PERMANENT -- there is no un-adjudicate command. "
            f"Re-run with --confirm to actually record it."
        )
        _print({"hypothesis_mark_status": {"status": "dry_run_eligible", **check}})
        return 0

    try:
        row = orch.hypothesis_mark_status(hypothesis_id, new_status, decided_by)
    except ValueError as exc:
        print(f"Refused: {exc}")
        _print({"hypothesis_mark_status": {"status": "refused", "error": str(exc)}})
        return 1
    print(f"{hypothesis_id}: status -> {row['status']} (decided_by={decided_by})")
    _print({"hypothesis_mark_status": {"status": "ok", "hypothesis": row}})
    return 0


def cmd_hypothesis_drafts(orch: Orchestrator, status: Optional[str]) -> int:
    """HGEN-1: list drafts (optionally filtered by status) with their
    status/checks -- PURE READ."""
    drafts = orch.hypothesis_drafts_list(status=status)
    _print({"hypothesis_drafts": drafts, "count": len(drafts)})
    return 0


def cmd_hypothesis_accept(orch: Orchestrator, draft_id: str, decided_by: str) -> int:
    """HGEN-1: the authorship act -- calls propose_hypothesis() with the
    draft's own MECHANICAL risk class. This is the ONLY path from draft to
    the real registry."""
    try:
        row = orch.hypothesis_draft_accept(draft_id, decided_by)
    except ValueError as exc:
        print(f"Refused: {exc}")
        _print({"hypothesis_accept": {"status": "refused", "error": str(exc)}})
        return 1
    print(f"{draft_id}: accepted -> {row['accepted_hypothesis_id']} (decided_by={decided_by})")
    _print({"hypothesis_accept": {"status": "ok", "draft": row}})
    return 0


def cmd_hypothesis_reject(orch: Orchestrator, draft_id: str, decided_by: str, reason: str) -> int:
    """HGEN-1: record an operator rejection of a pending draft."""
    try:
        row = orch.hypothesis_draft_reject(draft_id, decided_by, reason)
    except ValueError as exc:
        print(f"Refused: {exc}")
        _print({"hypothesis_reject": {"status": "refused", "error": str(exc)}})
        return 1
    print(f"{draft_id}: rejected (decided_by={decided_by}, reason={reason!r})")
    _print({"hypothesis_reject": {"status": "ok", "draft": row}})
    return 0


def cmd_hypothesis_generate(orch: Orchestrator) -> int:
    """HGEN-1: one hypothesis-generation pass -- operator-triggered only, no
    scheduler job. Default-off; re-checks the G1 runtime gate + unreviewed-
    draft ceiling + cost caps every call. A refused/skipped run is a safe,
    zero-exit-code no-op (matches canary_run's own posture) -- the
    'reason' field explains why."""
    result = orch.hypothesis_generate()
    _print({"hypothesis_generate": result})
    return 0


def cmd_autonomy_readiness(orch: Orchestrator) -> int:
    """PR13 slice 2: every card-gating hypothesis's promotion precondition
    checklist -- PURE READ, never a trigger."""
    from alphaos.reports.autonomy_readiness import render_markdown

    rep = orch.autonomy_readiness_report()
    print(render_markdown(rep))
    print()
    _print({"autonomy_readiness": rep})
    return 0


def cmd_card_promote(
    orch: Orchestrator, hypothesis_id: str, decided_by: str, research_ref: Optional[str], confirm: bool,
) -> int:
    """PR13 slice 2: graduate a card from shadow to live_eligible (content
    unchanged). Without --confirm, this is a dry-run preview of the
    precondition checklist only -- no write happens either way unless
    --confirm is passed and every precondition clears."""
    from alphaos.cards.promotion import check_promotion_preconditions

    check = check_promotion_preconditions(orch.journal, hypothesis_id, research_ref)
    if not check["eligible"]:
        print(f"NOT ELIGIBLE -- {check['reason_code']}: {check['detail']}")
        _print({"card_promote": {"status": "not_eligible", **check}})
        return 1

    if not confirm:
        print(
            f"ELIGIBLE -- {check['card_id']} v{check['card_version']} would be promoted "
            f"shadow -> live_eligible. Re-run with --confirm to actually promote."
        )
        _print({"card_promote": {"status": "dry_run_eligible", **check}})
        return 0

    row = orch.card_promote(hypothesis_id, decided_by, research_ref)
    print(f"Promoted: {row['card_id']} v{row['card_version']} shadow -> live_eligible (decided_by={decided_by})")
    _print({"card_promote": {"status": "promoted", "decision": row}})
    return 0


def cmd_card_demote(
    orch: Orchestrator, card_id: str, card_version: int, decided_by: str, reason: str, confirm: bool,
) -> int:
    """PR13 slice 2: a manual override demotion -- an operator's own
    judgment call, not evidence-gated. Without --confirm, this is a preview
    only -- scope/safety-audit LOW: the preview now runs the SAME
    precondition check demote_card() itself uses, so it can never say
    "would demote" against a card that a --confirm run would then refuse
    (matches cmd_card_promote's own dry-run behavior)."""
    from alphaos.cards.promotion import check_demotion_preconditions

    check = check_demotion_preconditions(orch.journal, card_id, card_version)
    if not check["eligible"]:
        print(f"NOT ELIGIBLE -- {check['reason_code']}: {check['detail']}")
        _print({"card_demote": {"status": "not_eligible", **check}})
        return 1

    if not confirm:
        print(f"Would demote {card_id} v{card_version} (decided_by={decided_by}, reason={reason!r}). "
              "Re-run with --confirm to actually demote.")
        _print({"card_demote": {"status": "dry_run_eligible", **check}})
        return 0

    try:
        row = orch.card_demote_manual(card_id, card_version, decided_by, reason)
    except ValueError as exc:
        print(f"Refused: {exc}")
        _print({"card_demote": {"status": "refused", "error": str(exc)}})
        return 1
    print(f"Demoted: {card_id} v{card_version} (decided_by={decided_by})")
    _print({"card_demote": {"status": "demoted", "decision": row}})
    return 0


def cmd_card_materialize(orch: Orchestrator, hypothesis_id: str, decided_by: Optional[str], confirm: bool) -> int:
    """PR13.5: without --confirm, stages a proposed next-version scaffold +
    evidence packet for the operator to inspect and author (no cards/
    write). With --confirm, verifies the operator has authored, moved, and
    git-committed the new version's YAML, then registers it -- refuses
    otherwise. See alphaos/cards/materialize.py's own module docstring for
    why this is a separate command from card_promote (that one graduates
    an EXISTING version's state; this one mints a NEW version's content)."""
    if not confirm:
        result = orch.card_materialize_prepare(hypothesis_id)
        if not result["prepared"]:
            print(f"NOT ELIGIBLE -- {result['reason_code']}: {result['detail']}")
            _print({"card_materialize": {"status": "not_eligible", **result}})
            return 1
        print(
            f"Staged {result['card_id']} v{result['new_version']} scaffold at {result['scaffold_path']} "
            f"(evidence at {result['evidence_path']}). Edit it, move it into the cards directory, git "
            f"commit it, then re-run with --decided-by <you> --confirm."
        )
        _print({"card_materialize": {"status": "staged", **result}})
        return 0

    if not decided_by:
        print("Refused: --decided-by is required with --confirm")
        _print({"card_materialize": {"status": "refused", "error": "--decided-by is required with --confirm"}})
        return 1

    try:
        result = orch.card_materialize_confirm(hypothesis_id, decided_by)
    except ValueError as exc:
        print(f"Refused: {exc}")
        _print({"card_materialize": {"status": "refused", "error": str(exc)}})
        return 1

    if not result["confirmed"]:
        print(f"NOT ELIGIBLE -- {result['reason_code']}: {result['detail']}")
        _print({"card_materialize": {"status": "not_eligible", **result}})
        return 1

    print(f"Registered: {result['card_id']} v{result['new_version']} (materialized from v{result['old_version']}, "
          f"decided_by={decided_by})")
    _print({"card_materialize": {"status": "confirmed", **result}})
    return 0


def cmd_eval_corpus_build(orch: Orchestrator, corpus_dir: str, limit: int, include_shadow: bool = False) -> int:
    """EVAL-1 one-off: select real, clean (post-PR9.1) candidate_packets rows
    into the frozen golden corpus (additive; never overwrites an existing
    fixture). Does NOT adjudicate ground truth -- the operator reviews the
    written fixture files and fills in ground_truth_label by hand, then
    commits the corpus directory like a card. EXP-1: shadow-tier packets are
    excluded by default -- pass --include-shadow to opt in."""
    res = orch.eval_corpus_build(corpus_dir=corpus_dir, limit=limit, include_shadow=include_shadow)
    _print({"eval_corpus_build": res})
    print(
        f"\n{res['packets_written']} new packet(s) written to {res['corpus_dir']} "
        f"(corpus now {res['corpus_size']} packet(s), version {res['corpus_version']}). "
        "Review the fixtures, fill in ground_truth_label by hand where you can, then "
        "git add/commit the corpus directory -- it is never auto-committed."
    )
    return 0


def cmd_eval(orch: Orchestrator, corpus_dir: str, repeats: int) -> int:
    """EVAL-1: replay the frozen golden corpus through the CURRENT playbook
    classifier (the exact production call, never a reimplementation).
    Stores every result including fail-safe ones. Zero decision surface."""
    res = orch.run_eval(corpus_dir=corpus_dir, repeats=repeats)
    _print({"eval_run": res})
    return 0 if "error" not in res else 1


def cmd_eval_report(orch: Orchestrator) -> int:
    """EVAL-1: the latest eval run's report -- parse rate, label agreement
    vs ground truth, categorical stability across repeats. PURE READ."""
    from alphaos.reports.eval_report import render_markdown

    rep = orch.eval_report()
    print(render_markdown(rep))
    print()
    _print({"eval_report": rep})
    return 0


def cmd_relabel(
    orch: Orchestrator, date_from: str, date_to: str, dry_run: bool, include_shadow: bool = False,
) -> int:
    """TASK-R one-off: replay stored packet_json for candidate_packets rows
    in [date_from, date_to] through the CURRENT labeller. --dry-run prints
    composed prompts with zero network calls; the live run persists new
    candidate_labels rows (relabel_of set, originals never touched) and
    prints an old-vs-new label diff table. EXP-1: shadow-tier packets are
    excluded by default -- pass --include-shadow to opt in."""
    res = orch.relabel_candidates(date_from, date_to, dry_run=dry_run, include_shadow=include_shadow)
    if "error" in res:
        _print({"relabel": res})
        return 1

    if dry_run:
        print(f"DRY RUN -- {res['n_packets']} packet(s) in [{date_from}, {date_to}], zero network calls:\n")
        for p in res["prompts"]:
            print(f"--- {p['symbol']} ({p['packet_id']}) ---")
            print(p["prompt"])
            print()
    else:
        print(f"Relabelled {res['n_relabelled']}/{res['n_packets']} packet(s) in [{date_from}, {date_to}]:\n")
        print(f"{'symbol':<8} {'old label':<20} {'new label':<20} {'old dec':<10} {'new dec':<10}")
        for d in res["diffs"]:
            print(f"{d['symbol']:<8} {str(d['old_label']):<20} {str(d['new_label']):<20} "
                  f"{str(d['old_decision']):<10} {str(d['new_decision']):<10}")
        print()
    _print({"relabel": {k: v for k, v in res.items() if k not in ("prompts",)}})
    return 0


def cmd_canary_corpus_build(orch: Orchestrator, corpus_dir: str, limit: int, include_shadow: bool = False) -> int:
    """CANARY one-off: select real, clean (post-PR9.1) candidate_packets rows
    -- preferring TASK-R relabels -- into the frozen golden corpus (additive;
    never overwrites an existing fixture). Review the fixtures, then git
    add/commit the corpus directory -- it is never auto-committed."""
    res = orch.canary_corpus_build(corpus_dir=corpus_dir, limit=limit, include_shadow=include_shadow)
    _print({"canary_corpus_build": res})
    print(
        f"\n{res['packets_written']} new packet(s) written to {res['corpus_dir']} "
        f"(corpus now {res['corpus_size']} packet(s), version {res['corpus_version']}). "
        "Review the fixtures, then git add/commit the corpus directory -- it is never auto-committed. "
        "Set CANARY_ENABLED=true once you're ready for the weekly job to run."
    )
    return 0


def cmd_canary_run(orch: Orchestrator, corpus_dir: str) -> int:
    """CANARY: replay the frozen golden corpus through the CURRENT playbook
    classifier and compare against the pinned baseline run. Zero decision
    surface."""
    res = orch.canary_run(corpus_dir=corpus_dir)
    _print({"canary_run": res})
    return 0 if "error" not in res else 1


def cmd_canary_status(orch: Orchestrator) -> int:
    """CANARY: the latest run's report -- PURE READ."""
    from alphaos.reports.canary_report import render_markdown

    rep = orch.canary_status()
    print(render_markdown(rep))
    print()
    _print({"canary_status": rep})
    return 0


def cmd_canary_pin_baseline(orch: Orchestrator, run_id: str) -> int:
    """CANARY: mark run_id as THE reference run every future run diffs
    against. Never automatic -- an operator decides when a run is clean
    enough to trust."""
    res = orch.canary_pin_baseline(run_id)
    _print({"canary_pin_baseline": res})
    return 0 if "error" not in res else 1


def cmd_ab_eval_corpus_build(orch: Orchestrator, corpus_dir: str, total: int) -> int:
    """AB-EVAL-1 one-off: select the default corpus (all 2026-07-09/07-10
    kill-zone rows + a stratified later-row sample) and freeze it to disk
    (additive; never overwrites an existing fixture). Review the fixtures,
    then git add/commit the corpus directory -- it is never auto-committed."""
    res = orch.ab_eval_corpus_build(corpus_dir=corpus_dir, total=total)
    _print({"ab_eval_corpus_build": res})
    print(
        f"\n{res['fixtures_written']} new evaluation(s) written to {res['corpus_dir']} "
        f"(corpus now {res['corpus_size']} evaluation(s), version {res['corpus_version']}). "
        "Review the fixtures, then git add/commit the corpus directory -- it is never auto-committed."
    )
    return 0


def cmd_ab_eval_run(orch: Orchestrator, models: Optional[list], arms: Optional[list],
                    corpus_dir: str) -> int:
    """AB-EVAL-1: shadow, read-only replay of the frozen corpus through the
    given models/arms via the production evaluate core. Zero decision
    surface. INSTR-2: ``arms`` (each a "MODEL:VERSION" CLI token, parsed
    below) takes precedence over ``models`` when both were somehow given --
    argparse's own mutually-exclusive group already prevents that in
    practice."""
    parsed_arms = None
    if arms:
        parsed_arms = []
        for token in arms:
            model, sep, version = token.partition(":")
            if not sep or version not in ("v1", "v2"):
                _print({"ab_eval_run": {
                    "error": f"--arms token {token!r} is not MODEL:PROMPT_VERSION shaped "
                             "(expected e.g. 'gpt-5.4-mini:v1'; version must be v1 or v2)"
                }})
                return 1
            parsed_arms.append((model, version))
    res = orch.ab_eval_run(models, corpus_dir=corpus_dir, arms=parsed_arms)
    _print({"ab_eval_run": res})
    return 0 if "error" not in res else 1


def cmd_ab_eval_status(orch: Orchestrator, run_id: str) -> int:
    """AB-EVAL-1: the latest (or named) run's report -- PURE READ."""
    from alphaos.reports.ab_eval_report import render_markdown

    rep = orch.ab_eval_status(ab_run_id=run_id)
    print(render_markdown(rep))
    print()
    _print({"ab_eval_status": rep})
    return 0


def cmd_universe_build(orch: Orchestrator) -> int:
    """EXP-0: screen the tradable universe down to the shadow-tier ADV/price
    band and write the result to the committed universe file (NOT git-add'd
    or committed by this command — reviewing the symbol list and committing
    it is a deliberate operator step, per the spec's own acceptance gate).
    One-off / quarterly refresh; never a scheduler job. Requires live Alpaca
    credentials (mock/offline mode has nothing to screen against)."""
    from alphaos.universe.builder import build_shadow_universe, write_universe_file

    screened = build_shadow_universe(orch.settings, orch.journal)
    if not screened["symbols"] and screened["screened"] == 0:
        _print({
            "universe_build": screened,
            "note": "no live Alpaca screen available (mock/offline mode, or missing credentials) "
                    "-- nothing written",
        })
        return 1
    doc = write_universe_file(screened, orch.settings.shadow_tier_universe_file)
    _print({
        "universe_build": {
            "path": orch.settings.shadow_tier_universe_file,
            "version": doc["version"],
            "sha256": doc["sha256"],
            "as_of_date": doc["as_of_date"],
            "screened": doc["screened"],
            "passed": doc["passed"],
            "skipped_count": len(doc["skipped"]),
            "skipped_reasons": sorted({s["reason"] for s in doc["skipped"]}),
        },
        "next_step": "Review the symbol list, then `git add` + commit the file yourself "
                     "before setting SHADOW_TIER_ENABLED=true.",
    })
    return 0


def cmd_outcomes_report(orch: Orchestrator) -> int:
    """Measurement-visibility summary over candidate_outcomes. No statistical
    claims — always surfaces a small-sample caveat."""
    from alphaos.reports.outcomes_summary import render_markdown

    rep = orch.outcomes_report()
    print(render_markdown(rep))
    print()
    _print({"outcomes_report": rep})
    return 0


def cmd_relative_performance_report(orch: Orchestrator) -> int:
    """PR9.5: paper-equity vs S&P 500 measurement. No statistical claims —
    floor-gated exactly like every other report; PURE READ."""
    from alphaos.reports.relative_performance import render_markdown

    rep = orch.relative_performance_report()
    print(render_markdown(rep))
    print()
    _print({"relative_performance_report": rep})
    return 0


def cmd_brief(orch: Orchestrator) -> int:
    """PR11: the daily human interface. PURE READ."""
    from alphaos.reports.daily_brief import render_markdown

    brief = orch.daily_brief_report()
    print(render_markdown(brief))
    print()
    _print({"daily_brief": brief})
    return 0


def cmd_decision_lineage(orch: Orchestrator, decision_id: str) -> int:
    """READ-ONLY: which code/config/model/prompt/data/scheduler context
    produced this decision. Accepts a candidate_id, proposal_id,
    rejection_id, adjustment_id, override_id, outcome_id, eval_id, review_id,
    or polarity_id."""
    _print({"decision_lineage": orch.decision_lineage_report(decision_id)})
    return 0


def cmd_flatten(orch: Orchestrator) -> int:
    res = orch.flatten_paper_account()
    _print({"flatten": res})
    return 0 if res.get("ok") else 1


def cmd_reconcile_report(orch: Orchestrator) -> int:
    _print({"broker_ledger_reconciliation": orch.broker_ledger_report()})
    return 0


def cmd_protection_status(orch: Orchestrator) -> int:
    """READ-ONLY: broker protection watchdog status -- unprotected/mismatched
    positions, open incidents, and whether new entries are currently blocked."""
    _print({"protection_status": orch.protection_status_report()})
    return 0


def cmd_protection_resolve(orch: Orchestrator, incident_id: str, exit_price: float, note: str) -> int:
    """Human-confirmed resolution of a local-open/broker-closed protection
    incident: calls close_position() with a confirmed exit price -- never raw SQL."""
    res = orch.protection_resolve(incident_id, exit_price=exit_price, note=note, resolved_by="cli")
    _print({"protection_resolve": res})
    return 0 if res.get("ok") else 1


def cmd_protection_ack(orch: Orchestrator, incident_id: str, note: str) -> int:
    """Acknowledge an unprotected/degraded protection incident WITHOUT closing
    the position (lifts the new-entry block once protection is confirmed restored,
    or the risk is explicitly accepted)."""
    res = orch.protection_ack(incident_id, note=note, resolved_by="cli")
    _print({"protection_ack": res})
    return 0 if res.get("ok") else 1


def cmd_scheduler_status(orch: Orchestrator) -> int:
    """READ-ONLY: scheduler job history, lock state, protection/kill-switch/cost-cap summary."""
    _print({"scheduler_status": JobRunner(orch).status_report()})
    return 0


def cmd_scheduler_run_once(orch: Orchestrator) -> int:
    """Run every scheduled job that is currently due (respects cadence windows,
    kill switch, cost cap)."""
    _print({"scheduler_run_once": JobRunner(orch).run_due_jobs()})
    return 0


def cmd_scheduler_run_job(orch: Orchestrator, job_type: str) -> int:
    """Force-run one scheduler job now, bypassing cadence timing (still respects
    kill switch / protection / cost cap / locking)."""
    _print({"scheduler_run_job": JobRunner(orch).run_job(job_type)})
    return 0


def cmd_scheduler_health(orch: Orchestrator) -> int:
    """PR9 dead-man's-switch: exit 0 if a job_runs row completed recently
    enough during market hours (else exit 1 + one alert). Meant to be driven
    by its OWN separate LaunchAgent, not the scheduler's own tick."""
    result = JobRunner(orch).heartbeat_check()
    _print({"scheduler_health": result})
    return 0 if result["ok"] else 1


def cmd_status(orch: Orchestrator) -> int:
    checks = orch.startup()
    _print(
        {
            "mode": orch.settings.mode.value,
            "real_trading_enabled_raw": orch.settings.real_trading_enabled_raw,
            "real_trading_value_ok": orch.settings.real_trading_value_ok,
            "system_health": orch.system_health(),
            "startup_checks": [c.as_dict() for c in checks],
        }
    )
    return 0


def cmd_seed_demo(orch: Orchestrator) -> int:
    _print({"seed_demo": orch.seed_demo()})
    return 0


def cmd_kill(orch: Orchestrator, action: str) -> int:
    ks = KillSwitch()
    if action == "engage":
        ks.engage("cli")
        orch.journal.log_system_event("critical", "kill_switch", "Kill switch ENGAGED via CLI.")
    elif action == "release":
        ks.release()
        orch.journal.log_system_event("warning", "kill_switch", "Kill switch RELEASED via CLI.")
    _print({"kill_switch_engaged": ks.is_engaged()})
    return 0


def cmd_last30days_probe(orch: Orchestrator, symbol: str) -> int:
    """READ-ONLY: run last30days narrative enrichment for ONE symbol and print the
    context. Writes nothing to the ledger; never proposes or executes. Uses the
    configured provider (mock by default; set LAST30DAYS_PROVIDER=cli to test the
    live keyless skill)."""
    _print({"last30days_probe": orch.last30days_probe(symbol)})
    return 0


def cmd_armed_watch(orch: Orchestrator) -> int:
    """List ARMED WATCH (near-action) candidates: override armed but stayed watch."""
    rows = orch.journal.armed_watches(100)
    view = [{k: r.get(k) for k in (
        "symbol", "eval_decision", "label_decision", "final_decision", "arming_classification",
        "armed_watch_reason", "sentiment_label", "label_confidence", "source_coverage_json",
        "proposal_readiness", "labeller_reason",
    )} for r in rows]
    _print({"armed_watch_summary": orch.journal.armed_watch_summary(), "armed_watches": view,
            "count": len(view)})
    return 0


def cmd_override(orch: Orchestrator, args) -> int:
    """Record a USER OVERRIDE of AlphaOS's recommendation (separate decision layer).
    Without --yes this previews AlphaOS's decision; with --yes it records the
    override (safety gates + manual approval still apply; never auto-executes)."""
    if not args.yes:
        cand = orch.journal.one("SELECT * FROM candidates WHERE candidate_id = ?", (args.candidate_id,))
        if not cand:
            _print({"override_preview": f"candidate {args.candidate_id} not found"})
            return 1
        adj = orch.journal.one(
            "SELECT eval_decision, label_decision, final_decision, armed_watch, arming_classification "
            "FROM decision_adjustments WHERE candidate_id = ? ORDER BY id DESC LIMIT 1",
            (args.candidate_id,)) or {}
        _print({"override_preview": {
            "symbol": cand.get("symbol"), "requested_action": args.action,
            "alphaos_final_decision": adj.get("final_decision") or cand.get("label_decision"),
            "armed_watch": bool(adj.get("armed_watch")),
            "arming_classification": adj.get("arming_classification"),
            "high_risk_warning": (adj.get("arming_classification") == "high_risk_narrative"),
            "note": "re-run with --yes to record. Safety gates + manual approval still apply; "
                    "a watch_to_trade only creates a pending_approval proposal (you must `approve` it).",
        }})
        return 0
    res = orch.create_user_override(
        args.candidate_id, args.action, reason_code=args.reason, note=args.note,
        direction=args.direction, size=args.size)
    _print({"override": res})
    return 0 if res.get("ok") else 1


def cmd_overrides(orch: Orchestrator) -> int:
    """List user overrides + attribution summary."""
    _print({"user_override_summary": orch.journal.user_override_summary(),
            "recent_overrides": orch.journal.recent_user_overrides(50)})
    return 0


def cmd_dashboard(_: Orchestrator) -> int:
    print("Run the dashboard with:\n  streamlit run alphaos/dashboard/streamlit_app.py")
    return 0


def cmd_console_set_pin() -> int:
    """ND-3: set/replace the console API's write-PIN (docs/roadmap/
    console-migration-nd.md §3, §4 ND-3 scope). Deliberately does NOT take
    an Orchestrator -- unlike every other command in this file, `console
    set-pin` is handled in `main()` BEFORE `load_settings()`/`Orchestrator(
    ...)` are constructed (see main()'s own early-return for `args.command
    == "console"`), since setting a local operator secret has no business
    needing a scheduler-capable Orchestrator's full constructor cost (or its
    side effects) -- it only ever touches one file (alphaos/api/pin.py's
    PinStore, `data/console_pin.hash` by default), the same "no
    Orchestrator needed, source the one thing you actually need" reasoning
    alphaos/api/deps.py already documents for its own dependencies.

    getpass.getpass() never echoes the PIN to the terminal. A PIN is
    confirmed by re-entry (typo protection) and refused if empty."""
    import getpass

    from alphaos.api.pin import PinStore

    store = PinStore()
    if store.is_configured():
        answer = input(
            f"A console PIN is already configured at {store.path!r}. Overwrite? [y/N]: "
        ).strip().lower()
        if answer != "y":
            print("Cancelled — PIN unchanged.")
            return 1

    pin = getpass.getpass("New console PIN: ")
    if not pin or not pin.strip():
        print("Refused: PIN must not be empty.")
        return 1
    confirm = getpass.getpass("Confirm console PIN: ")
    if pin != confirm:
        print("Refused: PINs did not match.")
        return 1

    store.set_pin(pin)
    print(
        f"Console PIN set (scrypt hash stored at {store.path!r}, file mode 0600). "
        "Every POST /api/v1/actions/* write now requires it."
    )
    return 0


def cmd_console(args) -> int:
    if args.console_action == "set-pin":
        return cmd_console_set_pin()
    return 1  # pragma: no cover -- unreachable, argparse's `required=True` + `choices` already gate this


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m alphaos", description="AlphaOS v1 CLI")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("scan_once", help="run one scan/propose pass (legacy alias of interest_scan)")
    sub.add_parser("interest_scan", help="interest scan -> packet -> AI label -> propose (manual approval)")
    sub.add_parser("monitor_once", help="run one watchdog/exit pass")
    sub.add_parser("generate_daily_report", help="generate today's learning report")
    sub.add_parser("status", help="show mode/safety/startup status")
    sub.add_parser("proposals", help="list open proposals awaiting approval")
    ap = sub.add_parser("approve", help="approve + submit a proposal (paper); re-checks safety/risk/freshness")
    ap.add_argument("proposal_id")
    ap.add_argument("--margin", action="store_true", help="explicitly approve margin/borrow for a short")
    rj = sub.add_parser("reject", help="reject a proposal (removes it from the actionable queue)")
    rj.add_argument("proposal_id")
    rj.add_argument("--reason", default="cli rejected")
    sub.add_parser("calibration_report", help="cost-model calibration: modeled vs actual paper execution")
    sub.add_parser("flatten", help="PAPER-ONLY: cancel open Alpaca paper orders + close paper positions")
    sub.add_parser("reconcile_report", help="broker-vs-ledger reconciliation (detect orphans/mismatches)")
    sub.add_parser("protection_status",
                   help="broker protection watchdog status: unprotected/mismatched positions, open incidents")
    pr = sub.add_parser("protection_resolve",
                        help="human-confirmed resolution of a local-open/broker-closed protection incident "
                             "(calls close_position with a confirmed exit price; never raw SQL)")
    pr.add_argument("incident_id")
    pr.add_argument("--exit-price", type=float, required=True)
    pr.add_argument("--note", default="", help="required context for the audit trail")
    pa = sub.add_parser("protection_ack",
                        help="acknowledge an unprotected/degraded protection incident without closing the "
                             "position (lifts the new-entry block)")
    pa.add_argument("incident_id")
    pa.add_argument("--note", default="")
    sub.add_parser("scheduler_status",
                   help="READ-ONLY: scheduler job history, lock state, protection/kill-switch/cost-cap summary")
    sub.add_parser("scheduler_run_once",
                   help="run every scheduled job that is currently due (respects cadence windows, kill switch, "
                        "cost cap)")
    srj = sub.add_parser("scheduler_run_job",
                         help="force-run one scheduler job now, bypassing cadence timing (still respects kill "
                              "switch / protection / cost cap / locking)")
    srj.add_argument("job_type", choices=[
        "scan", "monitor", "outcomes_update", "daily_digest", "benchmark_spine", "text_archive_pull",
        "atr_update", "canary_run",
        # EXP-1: shadow-tier AI labelling -- own job type (mechanism 4), idempotent
        # per-window rerun (mechanism 4's own tested acceptance criterion).
        "shadow_label",
    ])
    sub.add_parser("scheduler_health",
                   help="dead-man's-switch check: exit 0 if a job completed recently enough during "
                        "market hours, else exit 1 + one alert (run from its own separate LaunchAgent)")
    sub.add_parser("seed_demo", help="create a labelled demo trade (exec/journal/dashboard demo)")
    l30 = sub.add_parser("last30days_probe",
                         help="READ-ONLY: print last30days narrative context for one symbol (no ledger writes)")
    l30.add_argument("symbol")
    sub.add_parser("armed_watch", help="list ARMED WATCH (near-action) candidates: armed but stayed watch")
    ov = sub.add_parser("override", help="record a USER OVERRIDE of AlphaOS's recommendation (gated; manual approval still required)")
    ov.add_argument("--candidate-id", required=True)
    ov.add_argument("--action", required=True,
                    help="watch_to_trade | propose_to_reject | manual_exit | manual_hold | reject_to_trade | ...")
    ov.add_argument("--reason", default=None, help="reason code (e.g. strong_conviction, disagrees_with_ai)")
    ov.add_argument("--note", default=None, help="free-text note")
    ov.add_argument("--direction", default=None, help="override direction (long|short), if changing")
    ov.add_argument("--size", type=float, default=None, help="size override, if applicable")
    ov.add_argument("--yes", action="store_true", help="confirm + record the override (otherwise preview only)")
    sub.add_parser("overrides", help="list user overrides + attribution summary")
    sub.add_parser("attribution_report",
                   help="user-override attribution learning report (AlphaOS vs user; heuristic)")
    sub.add_parser("brief", help="the daily human interface: needs-you, portfolio health, one action (PR11)")
    sub.add_parser("backfill_mfe_mae",
                   help="backfill MFE/MAE on closed trades from before excursion tracking existed (idempotent)")
    sub.add_parser("outcomes_update",
                   help="counterfactual outcome tracker: seed + resolve candidate_outcomes "
                        "(1/3/5-day forward returns + bracket replay; measurement only)")
    sub.add_parser("outcomes_report",
                   help="measurement-visibility summary over candidate_outcomes (no statistical claims)")
    sub.add_parser("relative_performance_report",
                   help="paper-equity vs S&P 500 measurement (no statistical claims; PR9.5)")
    sub.add_parser("universe_build",
                   help="EXP-0: screen + write the shadow-tier universe file ($5-50M ADV band); "
                        "one-off/quarterly, requires live Alpaca creds, never auto-commits")
    sub.add_parser("backfill_regime_days",
                   help="REG-1: backfill regime_days from SPY history + stamp existing packets "
                        "(idempotent, measurement only)")
    sub.add_parser("regime_arming_report",
                   help="REG-1: shadow arming-map scorer (armed_always vs armed_per_map paired "
                        "replay ΔR per card; nothing armed for real)")
    sub.add_parser("baseline_report",
                   help="BASELINE: does the AI add R over threshold_v1/propose_all_v1 (paired "
                        "ai_delta_r, day-block bootstrap CI; nothing gated for real)")
    sub.add_parser("baseline_register",
                   help="BASELINE: one-off, idempotent -- register the pre-registration block "
                        "(preregistrations row #1)")
    sub.add_parser("debate_register",
                   help="PR14: one-off, idempotent -- register the bear-debate pre-registration "
                        "block (oppose_high_conviction_v1)")
    sub.add_parser("hypothesis_seed",
                   help="PR12: idempotently register the 8 seeded hypotheses "
                        "(hypothesis_proposals + preregistrations rows)")
    sub.add_parser("per_register",
                   help="SETUP-1 S1b: one-off, idempotent -- register the H-PER-1P/H-PER-1N pair "
                        "(preregistrations rows only; never evaluates, never gates)")
    sub.add_parser("per_register_v2",
                   help="SETUP-1 S1b integrity follow-up: register the CORRECTED H-PER-1P-v2/"
                        "H-PER-1N-v2 pair with card/selector identity frozen; refuses on any "
                        "identity mismatch (preregistrations rows only; never evaluates)")
    sub.add_parser("per_evaluate",
                   help="SETUP-1 S1b: one atomic paired evaluation of H-PER-1P/H-PER-1N as of now "
                        "(operator-invoked only; deferred population/floor failures write nothing)")
    sub.add_parser("hypothesis_resolve",
                   help="PR12: one resolver pass -- evaluate any hypothesis past its own "
                        "calendar + sample-size floor, refresh cached verdicts for the family")
    sub.add_parser("hypothesis_report",
                   help="PR12: hypothesis registry status report (risk class, claim, "
                        "mechanical status, cached verdict/q-value; nothing gated for real)")
    sub.add_parser("card_scoreboard",
                   help="PR13 slice 1: per-card expectancy/effective-N/span vs floor "
                        "(shadow measurement; nothing gated for real)")
    sub.add_parser("card_demotion_check",
                   help="PR13 slice 1: one daily pass -- snapshot every live_eligible card, "
                        "demote (+ alert) any card with >= 2 consecutive breach snapshots")
    sub.add_parser("setup_evidence_report",
                   help="EVID-1: per-setup-version evidence (full family, incl. shadow/demoted) "
                        "-- market-adjusted return + replay_r, BH-FDR across setups that clear "
                        "their own floor; descriptive only, never gates/promotes/demotes")
    spb = sub.add_parser("setup_population_breakdown",
                         help="EVID-1: one setup-version's evidence side by side across every "
                              "candidate population (proposal/blocked/armed_watch/reject/candidate) "
                              "-- answers 'did rejects outperform approvals?' directly; PURE READ")
    spb.add_argument("card_id")
    spb.add_argument("card_version", type=int)
    spb.add_argument("--metric", default="market_adjusted_return_5d_pct",
                     help="candidate_outcomes column to bootstrap (default: market_adjusted_return_5d_pct)")
    hms = sub.add_parser("hypothesis_mark_met",
                         help="PR13 slice 2: operator adjudication -- mark a 'resolved' hypothesis MET "
                              "(the only writer of this status; never automated). PERMANENT, no undo -- "
                              "dry-run preview unless --confirm is passed")
    hms.add_argument("hypothesis_id")
    hms.add_argument("--decided-by", required=True, help="operator identity, never 'system'")
    hms.add_argument("--confirm", action="store_true", help="actually record it (default: dry-run preview)")
    hmf = sub.add_parser("hypothesis_mark_failed",
                         help="PR13 slice 2: operator adjudication -- mark a 'resolved' hypothesis FAILED. "
                              "PERMANENT, no undo -- dry-run preview unless --confirm is passed")
    hmf.add_argument("hypothesis_id")
    hmf.add_argument("--decided-by", required=True, help="operator identity, never 'system'")
    hmf.add_argument("--confirm", action="store_true", help="actually record it (default: dry-run preview)")
    hmw = sub.add_parser("hypothesis_mark_withdrawn",
                         help="PR13 slice 2: operator adjudication -- mark a 'resolved' hypothesis "
                              "WITHDRAWN. PERMANENT, no undo -- dry-run preview unless --confirm is passed")
    hmw.add_argument("hypothesis_id")
    hmw.add_argument("--decided-by", required=True, help="operator identity, never 'system'")
    hmw.add_argument("--confirm", action="store_true", help="actually record it (default: dry-run preview)")
    hd = sub.add_parser("hypothesis_drafts",
                        help="HGEN-1: list quarantined hypothesis drafts with status/checks -- PURE READ")
    hd.add_argument("--status", default=None, choices=["draft", "accepted", "rejected"],
                    help="filter by draft status (default: all)")
    hac = sub.add_parser("hypothesis_accept",
                         help="HGEN-1: the authorship act -- accept a quarantined draft into the "
                              "real registry via propose_hypothesis() (mechanical risk class)")
    hac.add_argument("draft_id")
    hac.add_argument("--decided-by", required=True, help="operator identity, never 'system'")
    hrj = sub.add_parser("hypothesis_reject",
                         help="HGEN-1: reject a quarantined draft (never touches the real registry)")
    hrj.add_argument("draft_id")
    hrj.add_argument("--decided-by", required=True, help="operator identity, never 'system'")
    hrj.add_argument("--reason", required=True, help="free-text reason, recorded on the draft row")
    sub.add_parser("hypothesis_generate",
                   help="HGEN-1: one hypothesis-generation pass (operator-triggered only, no "
                        "scheduler job). Default-off; refuses over the G1 runtime gate, the "
                        "unreviewed-draft ceiling, or the cost caps -- a refused run is a safe no-op")
    sub.add_parser("autonomy_readiness",
                   help="PR13 slice 2: every card-gating hypothesis's promotion precondition "
                        "checklist -- PURE READ, never a trigger")
    cpr = sub.add_parser("card_promote",
                        help="PR13 slice 2: graduate a card from shadow to live_eligible (content "
                             "unchanged, no new version). Dry-run preview unless --confirm is passed")
    cpr.add_argument("hypothesis_id")
    cpr.add_argument("--decided-by", required=True, help="operator identity, never 'system'")
    cpr.add_argument("--research-ref", default=None, help="required when the hypothesis is risk_class='C'")
    cpr.add_argument("--confirm", action="store_true", help="actually promote (default: dry-run preview)")
    cdm = sub.add_parser("card_demote",
                        help="PR13 slice 2: manual override demotion -- an operator judgment call, "
                             "not evidence-gated. Dry-run preview unless --confirm is passed")
    cdm.add_argument("card_id")
    cdm.add_argument("card_version", type=int)
    cdm.add_argument("--decided-by", required=True, help="operator identity, never 'system'")
    cdm.add_argument("--reason", required=True, help="free-text reason, recorded on the decision row")
    cdm.add_argument("--confirm", action="store_true", help="actually demote (default: dry-run preview)")
    cma = sub.add_parser("card_materialize",
                        help="PR13.5: draft (and, with --confirm, register) a new card version's "
                             "content -- the operator authors the diff themselves; this never writes "
                             "to the cards directory")
    cma.add_argument("hypothesis_id")
    cma.add_argument("--decided-by", default=None, help="operator identity, never 'system' (required with --confirm)")
    cma.add_argument("--confirm", action="store_true",
                      help="register the operator-committed new version (default: stage a scaffold + evidence "
                           "packet for review only)")
    ecb = sub.add_parser("eval_corpus_build",
                         help="EVAL-1: select real, clean candidate_packets rows into the frozen "
                              "golden corpus (additive; ground_truth_label starts null, never "
                              "auto-committed)")
    ecb.add_argument("--corpus-dir", default=None, help="defaults to data/eval")
    ecb.add_argument("--limit", type=int, default=30, help="max NEW packets to select (default 30)")
    ecb.add_argument("--include-shadow", action="store_true",
                     help="EXP-1: also consider shadow-tier (small/mid) packets (default: excluded)")
    ev = sub.add_parser("eval",
                        help="EVAL-1: replay the frozen golden corpus through the current playbook "
                             "classifier; stores every result incl. fail-safe ones")
    ev.add_argument("--corpus-dir", default=None, help="defaults to data/eval")
    ev.add_argument("--repeats", type=int, default=1,
                    help="replay each packet this many times, for categorical-stability measurement")
    sub.add_parser("eval_report",
                   help="EVAL-1: the latest eval run's report (parse rate, label agreement vs "
                        "ground truth, categorical stability)")
    rl = sub.add_parser("relabel",
                        help="TASK-R one-off: retro-relabel candidate_packets in a date range through "
                             "the current labeller; never touches an original row")
    rl.add_argument("--from", dest="date_from", required=True, help="SGT calendar date, YYYY-MM-DD")
    rl.add_argument("--to", dest="date_to", required=True, help="SGT calendar date, YYYY-MM-DD")
    rl.add_argument("--dry-run", action="store_true", help="print composed prompts, zero network calls")
    rl.add_argument("--include-shadow", action="store_true",
                    help="EXP-1: also consider shadow-tier (small/mid) packets (default: excluded)")
    ccb = sub.add_parser("canary_corpus_build",
                         help="CANARY: select real, clean candidate_packets rows (preferring TASK-R "
                              "relabels) into the frozen golden corpus (additive; never auto-committed)")
    ccb.add_argument("--corpus-dir", default=None, help="defaults to data/canary")
    ccb.add_argument("--limit", type=int, default=20, help="max NEW packets to select (default 20)")
    ccb.add_argument("--include-shadow", action="store_true",
                     help="EXP-1: also consider shadow-tier (small/mid) packets (default: excluded)")
    cr = sub.add_parser("canary_run",
                        help="CANARY: replay the frozen golden corpus through the current playbook "
                             "classifier and compare against the pinned baseline run")
    cr.add_argument("--corpus-dir", default=None, help="defaults to data/canary")
    sub.add_parser("canary_status",
                   help="CANARY: the latest canary run's report (drift tier, parse/fail-safe rate)")
    cpb = sub.add_parser("canary_pin_baseline",
                         help="CANARY: mark a run as THE baseline every future run diffs against "
                              "(never automatic -- an operator decides when a run is trustworthy)")
    cpb.add_argument("run_id")
    aeb = sub.add_parser("ab_eval_corpus_build",
                         help="AB-EVAL-1: freeze the default A/B replay corpus (all 2026-07-09/07-10 "
                              "kill-zone rows + a stratified later-row sample) to disk; additive, "
                              "never auto-committed")
    aeb.add_argument("--corpus-dir", default=None, help="defaults to data/ab_eval")
    aeb.add_argument("--total", type=int, default=60, help="target corpus size (default 60)")
    aer = sub.add_parser("ab_eval_run",
                         help="AB-EVAL-1: shadow, read-only replay of the frozen corpus through 2+ "
                              "models/arms via the production evaluate core; never read by any live path")
    aer_group = aer.add_mutually_exclusive_group(required=True)
    aer_group.add_argument("--models", nargs="+", default=None,
                           help="model names to compare, e.g. --models gpt-5.4-mini gpt-5.6-luna "
                                "(sugar for --arms MODEL:<configured OPENAI_PROMPT_VERSION>)")
    aer_group.add_argument("--arms", nargs="+", default=None,
                           help="INSTR-2: MODEL:PROMPT_VERSION pairs to compare, e.g. --arms "
                                "gpt-5.4-mini:v1 gpt-5.6-luna:v1 gpt-5.4-mini:v2 gpt-5.6-luna:v2 "
                                "-- mutually exclusive with --models")
    aer.add_argument("--corpus-dir", default=None, help="defaults to data/ab_eval")
    aes = sub.add_parser("ab_eval_status",
                         help="AB-EVAL-1: the latest (or named) run's report -- PURE READ")
    aes.add_argument("--run-id", default=None, help="defaults to the latest run")
    dl = sub.add_parser("decision_lineage",
                        help="READ-ONLY: which code/config/model/prompt/data/scheduler context produced "
                             "one decision (accepts a candidate_id, proposal_id, rejection_id, "
                             "adjustment_id, override_id, outcome_id, eval_id, review_id, or polarity_id)")
    dl.add_argument("decision_id")
    sub.add_parser("dashboard", help="how to launch the Streamlit dashboard")
    kill = sub.add_parser("kill", help="engage/release the kill switch")
    kill.add_argument("action", choices=["engage", "release"])
    console = sub.add_parser(
        "console", help="ND-3: console API operator actions (currently: PIN management)"
    )
    console_sub = console.add_subparsers(dest="console_action", required=True)
    console_sub.add_parser(
        "set-pin",
        help="set/replace the console write-PIN (prompted via getpass, hashed with scrypt; "
             "required before ANY /api/v1/actions/* write is accepted -- see docs/roadmap/"
             "console-migration-nd.md ND-3)",
    )
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    # ND-3: handled BEFORE load_settings()/Orchestrator(...) below --
    # `console set-pin` needs neither (see cmd_console_set_pin's own
    # docstring) and every other command in this file already pays that
    # construction cost unconditionally, which this one deliberately opts
    # out of rather than inheriting.
    if args.command == "console":
        return cmd_console(args)
    try:
        settings = load_settings()
    except SettingsError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2

    orch = Orchestrator(settings=settings)
    try:
        if args.command == "scan_once":
            return cmd_scan_once(orch)
        if args.command == "interest_scan":
            return cmd_interest_scan(orch)
        if args.command == "monitor_once":
            return cmd_monitor_once(orch)
        if args.command == "generate_daily_report":
            return cmd_generate_daily_report(orch)
        if args.command == "status":
            return cmd_status(orch)
        if args.command == "proposals":
            return cmd_proposals(orch)
        if args.command == "approve":
            return cmd_approve(orch, args.proposal_id, args.margin)
        if args.command == "reject":
            return cmd_reject(orch, args.proposal_id, args.reason)
        if args.command == "calibration_report":
            return cmd_calibration_report(orch)
        if args.command == "flatten":
            return cmd_flatten(orch)
        if args.command == "reconcile_report":
            return cmd_reconcile_report(orch)
        if args.command == "protection_status":
            return cmd_protection_status(orch)
        if args.command == "protection_resolve":
            return cmd_protection_resolve(orch, args.incident_id, args.exit_price, args.note)
        if args.command == "protection_ack":
            return cmd_protection_ack(orch, args.incident_id, args.note)
        if args.command == "scheduler_status":
            return cmd_scheduler_status(orch)
        if args.command == "scheduler_run_once":
            return cmd_scheduler_run_once(orch)
        if args.command == "scheduler_run_job":
            return cmd_scheduler_run_job(orch, args.job_type)
        if args.command == "scheduler_health":
            return cmd_scheduler_health(orch)
        if args.command == "seed_demo":
            return cmd_seed_demo(orch)
        if args.command == "last30days_probe":
            return cmd_last30days_probe(orch, args.symbol)
        if args.command == "armed_watch":
            return cmd_armed_watch(orch)
        if args.command == "override":
            return cmd_override(orch, args)
        if args.command == "overrides":
            return cmd_overrides(orch)
        if args.command == "attribution_report":
            return cmd_attribution_report(orch)
        if args.command == "brief":
            return cmd_brief(orch)
        if args.command == "backfill_mfe_mae":
            return cmd_backfill_mfe_mae(orch)
        if args.command == "outcomes_update":
            return cmd_outcomes_update(orch)
        if args.command == "outcomes_report":
            return cmd_outcomes_report(orch)
        if args.command == "relative_performance_report":
            return cmd_relative_performance_report(orch)
        if args.command == "universe_build":
            return cmd_universe_build(orch)
        if args.command == "backfill_regime_days":
            return cmd_backfill_regime_days(orch)
        if args.command == "regime_arming_report":
            return cmd_regime_arming_report(orch)
        if args.command == "baseline_report":
            return cmd_baseline_report(orch)
        if args.command == "baseline_register":
            return cmd_baseline_register(orch)
        if args.command == "debate_register":
            return cmd_debate_register(orch)
        if args.command == "hypothesis_seed":
            return cmd_hypothesis_seed(orch)
        if args.command == "per_register":
            return cmd_per_register(orch)
        if args.command == "per_register_v2":
            return cmd_per_register_v2(orch)
        if args.command == "per_evaluate":
            return cmd_per_evaluate(orch)
        if args.command == "hypothesis_resolve":
            return cmd_hypothesis_resolve(orch)
        if args.command == "hypothesis_report":
            return cmd_hypothesis_report(orch)
        if args.command == "card_scoreboard":
            return cmd_card_scoreboard(orch)
        if args.command == "card_demotion_check":
            return cmd_card_demotion_check(orch)
        if args.command == "setup_evidence_report":
            return cmd_setup_evidence_report(orch)
        if args.command == "setup_population_breakdown":
            return cmd_setup_population_breakdown(orch, args.card_id, args.card_version, args.metric)
        if args.command == "hypothesis_mark_met":
            return cmd_hypothesis_mark_status(orch, args.hypothesis_id, "met", args.decided_by, args.confirm)
        if args.command == "hypothesis_mark_failed":
            return cmd_hypothesis_mark_status(orch, args.hypothesis_id, "failed", args.decided_by, args.confirm)
        if args.command == "hypothesis_mark_withdrawn":
            return cmd_hypothesis_mark_status(orch, args.hypothesis_id, "withdrawn", args.decided_by, args.confirm)
        if args.command == "hypothesis_drafts":
            return cmd_hypothesis_drafts(orch, args.status)
        if args.command == "hypothesis_accept":
            return cmd_hypothesis_accept(orch, args.draft_id, args.decided_by)
        if args.command == "hypothesis_reject":
            return cmd_hypothesis_reject(orch, args.draft_id, args.decided_by, args.reason)
        if args.command == "hypothesis_generate":
            return cmd_hypothesis_generate(orch)
        if args.command == "autonomy_readiness":
            return cmd_autonomy_readiness(orch)
        if args.command == "card_promote":
            return cmd_card_promote(orch, args.hypothesis_id, args.decided_by, args.research_ref, args.confirm)
        if args.command == "card_demote":
            return cmd_card_demote(orch, args.card_id, args.card_version, args.decided_by, args.reason, args.confirm)
        if args.command == "card_materialize":
            return cmd_card_materialize(orch, args.hypothesis_id, args.decided_by, args.confirm)
        if args.command == "eval_corpus_build":
            return cmd_eval_corpus_build(orch, args.corpus_dir, args.limit, args.include_shadow)
        if args.command == "eval":
            return cmd_eval(orch, args.corpus_dir, args.repeats)
        if args.command == "eval_report":
            return cmd_eval_report(orch)
        if args.command == "relabel":
            return cmd_relabel(orch, args.date_from, args.date_to, args.dry_run, args.include_shadow)
        if args.command == "canary_corpus_build":
            return cmd_canary_corpus_build(orch, args.corpus_dir, args.limit, args.include_shadow)
        if args.command == "canary_run":
            return cmd_canary_run(orch, args.corpus_dir)
        if args.command == "canary_status":
            return cmd_canary_status(orch)
        if args.command == "canary_pin_baseline":
            return cmd_canary_pin_baseline(orch, args.run_id)
        if args.command == "ab_eval_corpus_build":
            return cmd_ab_eval_corpus_build(orch, args.corpus_dir, args.total)
        if args.command == "ab_eval_run":
            return cmd_ab_eval_run(orch, args.models, args.arms, args.corpus_dir)
        if args.command == "ab_eval_status":
            return cmd_ab_eval_status(orch, args.run_id)
        if args.command == "decision_lineage":
            return cmd_decision_lineage(orch, args.decision_id)
        if args.command == "dashboard":
            return cmd_dashboard(orch)
        if args.command == "kill":
            return cmd_kill(orch, args.action)
        return 1
    finally:
        orch.close()


if __name__ == "__main__":
    raise SystemExit(main())
