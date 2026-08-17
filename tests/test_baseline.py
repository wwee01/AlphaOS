"""BASELINE: the deterministic shadow baseline (§H.1 direct construction
throughout -- no wall-clock dependence). Covers:
* the two frozen rules' pure math (threshold_v1/propose_all_v1, the shared
  bracket-builder, no_action vs unavailable, determinism, input_sha),
* the day-block BCa bootstrap (insufficient data, BCa vs normal_approx
  fallback, the adversarial "clustered CI wider than naive CI" proof),
* the pure report aggregation (floor gating, paired ai_delta_r, a by-hand
  reproduction),
* journal-aware recording (2:1 write ratio, no_action/unavailable resolve
  immediately, pending rows resolve later via the ONE replay engine),
* orchestrator wiring (behavior-neutrality A/B, shadow-law isolation,
  never-raises fail-safety),
* schema/lineage additive migration.

All offline, in-memory, mock mode. No real money, no network.
"""

from __future__ import annotations

import inspect
import pathlib

import pytest

from alphaos.baseline.rules import (
    BASELINE_RULE_VERSIONS,
    BASELINE_RULE_VERSIONS_HOLD10,
    PROPOSE_ALL_V1,
    PROPOSE_ALL_V1_HOLD10,
    THRESHOLD_V1,
    THRESHOLD_V1_HOLD10,
    THRESHOLD_V1_INTEREST_SCORE,
    apply_propose_all_v1,
    apply_threshold_v1,
)
from alphaos.baseline.tracker import (
    BASELINE_HOLD10_PINNED_CARD_ID,
    BASELINE_V1_PINNED_CARD_ID,
    record_shadow_baseline_decisions,
    resolve_pending_baseline_decisions,
)
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.reports.baseline_report import (
    FLOOR_DAY_BLOCKS,
    FLOOR_SPAN_DAYS,
    HOLD10_ANALYSIS_NOT_BEFORE_DATE,
    HOLD10_REPORT_TITLE,
    build_baseline_hold10_report,
    build_baseline_report,
    compute_baseline_report,
    render_full_baseline_markdown,
    render_markdown,
)
from alphaos.stats.bootstrap import day_block_bootstrap
from alphaos.util import timeutils
from conftest import make_settings

ALPHAOS_DIR = pathlib.Path(__file__).resolve().parent.parent / "alphaos"


def _orch(**over):
    return Orchestrator(settings=make_settings(**over), journal=JournalStore(":memory:"))


# ------------------------------------------------------------- pure rules
def test_frozen_rule_versions_are_exactly_two():
    assert BASELINE_RULE_VERSIONS == (THRESHOLD_V1, PROPOSE_ALL_V1)


def test_threshold_v1_interest_score_constant_is_pinned():
    """Regression pin: this is a FROZEN literal (spec item 1) -- a change
    here must be a deliberate threshold_v2, never an accidental drift."""
    assert THRESHOLD_V1_INTEREST_SCORE == 0.661


def test_apply_threshold_v1_proposes_above_threshold_long():
    row = {"symbol": "AAPL", "direction": "long", "last_price": 100.0, "interest_score": 0.8}
    out = apply_threshold_v1(row, atr_14=2.0, min_reward_risk=1.2, max_holding_days_default=3)
    assert out["decision"] == "propose"
    assert out["decision_reason"] == "above_threshold"
    assert out["entry"] == 100.0
    assert out["stop"] == 96.0  # 100 - 2*2.0
    assert out["target"] == 104.8  # 100 + 1.2*4.0
    assert out["max_holding_days"] == 3
    assert out["rule_version"] == THRESHOLD_V1


def test_apply_threshold_v1_no_action_below_threshold():
    row = {"symbol": "MSFT", "direction": "long", "last_price": 50.0, "interest_score": 0.3}
    out = apply_threshold_v1(row, atr_14=1.0, min_reward_risk=1.2, max_holding_days_default=3)
    assert out["decision"] == "no_action"
    assert out["decision_reason"] == "below_threshold"
    for k in ("direction", "entry", "stop", "target", "max_holding_days"):
        assert out[k] is None


def test_apply_threshold_v1_at_exactly_the_threshold_proposes():
    """>= is inclusive (spec: "PROPOSE iff interest_score >= X")."""
    row = {"symbol": "AAPL", "direction": "long", "last_price": 100.0,
          "interest_score": THRESHOLD_V1_INTEREST_SCORE}
    out = apply_threshold_v1(row, atr_14=2.0, min_reward_risk=1.2, max_holding_days_default=3)
    assert out["decision"] == "propose"


def test_apply_threshold_v1_unavailable_on_missing_interest_score():
    """Missing data is UNAVAILABLE, never silently treated as a confident
    no_action (unknown != safe)."""
    row = {"symbol": "AAPL", "direction": "long", "last_price": 100.0, "interest_score": None}
    out = apply_threshold_v1(row, atr_14=2.0, min_reward_risk=1.2, max_holding_days_default=3)
    assert out["decision"] == "unavailable"
    assert out["decision_reason"] == "no_interest_score"


def test_apply_propose_all_v1_always_proposes_regardless_of_interest_score():
    row = {"symbol": "MSFT", "direction": "long", "last_price": 50.0, "interest_score": 0.01}
    out = apply_propose_all_v1(row, atr_14=1.0, min_reward_risk=1.2, max_holding_days_default=3)
    assert out["decision"] == "propose"
    assert out["decision_reason"] == "propose_all"


def test_apply_propose_all_v1_short_direction_sign_correct():
    row = {"symbol": "TSLA", "direction": "short", "last_price": 200.0, "interest_score": 0.9}
    out = apply_propose_all_v1(row, atr_14=5.0, min_reward_risk=1.2, max_holding_days_default=3)
    assert out["stop"] == 210.0  # short: stop ABOVE entry, 200 + 2*5.0
    assert out["target"] == 188.0  # short: target BELOW entry, 200 - 1.2*10.0
    assert out["stop"] > out["entry"] > out["target"]


def test_apply_propose_all_v1_long_direction_sign_correct():
    row = {"symbol": "AAPL", "direction": "long", "last_price": 100.0, "interest_score": 0.9}
    out = apply_propose_all_v1(row, atr_14=2.0, min_reward_risk=1.2, max_holding_days_default=3)
    assert out["stop"] < out["entry"] < out["target"]


@pytest.mark.parametrize("rule_fn", [apply_threshold_v1, apply_propose_all_v1])
def test_rules_unavailable_on_missing_entry_price(rule_fn):
    row = {"symbol": "AAPL", "direction": "long", "last_price": None, "interest_score": 0.9}
    out = rule_fn(row, atr_14=2.0, min_reward_risk=1.2, max_holding_days_default=3)
    assert out["decision"] == "unavailable"
    assert out["decision_reason"] == "no_entry_price"


@pytest.mark.parametrize("rule_fn", [apply_threshold_v1, apply_propose_all_v1])
def test_rules_unavailable_on_missing_atr(rule_fn):
    row = {"symbol": "AAPL", "direction": "long", "last_price": 100.0, "interest_score": 0.9}
    out = rule_fn(row, atr_14=None, min_reward_risk=1.2, max_holding_days_default=3)
    assert out["decision"] == "unavailable"
    assert out["decision_reason"] == "no_atr_data"


@pytest.mark.parametrize("rule_fn", [apply_threshold_v1, apply_propose_all_v1])
def test_rules_unavailable_on_zero_or_negative_atr(rule_fn):
    row = {"symbol": "AAPL", "direction": "long", "last_price": 100.0, "interest_score": 0.9}
    out = rule_fn(row, atr_14=0.0, min_reward_risk=1.2, max_holding_days_default=3)
    assert out["decision"] == "unavailable"
    assert out["decision_reason"] == "no_atr_data"


def test_apply_threshold_v1_missing_direction_defaults_long():
    row = {"symbol": "AAPL", "direction": None, "last_price": 100.0, "interest_score": 0.9}
    out = apply_threshold_v1(row, atr_14=2.0, min_reward_risk=1.2, max_holding_days_default=3)
    assert out["direction"] == "long"
    assert out["stop"] < out["entry"]


def test_rules_are_deterministic_across_repeated_calls():
    row = {"symbol": "AAPL", "direction": "long", "last_price": 100.0, "interest_score": 0.9}
    out1 = apply_threshold_v1(row, atr_14=2.0, min_reward_risk=1.2, max_holding_days_default=3)
    out2 = apply_threshold_v1(row, atr_14=2.0, min_reward_risk=1.2, max_holding_days_default=3)
    assert out1 == out2
    assert out1["input_sha"] == out2["input_sha"]


def test_input_sha_changes_when_any_relevant_input_changes():
    row = {"symbol": "AAPL", "direction": "long", "last_price": 100.0, "interest_score": 0.9}
    base = apply_threshold_v1(row, atr_14=2.0, min_reward_risk=1.2, max_holding_days_default=3)
    diff_atr = apply_threshold_v1(row, atr_14=2.5, min_reward_risk=1.2, max_holding_days_default=3)
    diff_rr = apply_threshold_v1(row, atr_14=2.0, min_reward_risk=1.5, max_holding_days_default=3)
    diff_row = apply_threshold_v1({**row, "last_price": 101.0}, atr_14=2.0, min_reward_risk=1.2,
                                  max_holding_days_default=3)
    shas = {base["input_sha"], diff_atr["input_sha"], diff_rr["input_sha"], diff_row["input_sha"]}
    assert len(shas) == 4  # all four genuinely distinct


def test_rules_ignore_underscore_prefixed_keys_injected_into_row():
    """Defense in depth even post-SC (ScanContext.__setitem__ already
    refuses private keys structurally) -- the rule functions themselves
    never read anything beyond symbol/direction/last_price/interest_score,
    so injecting a private-looking key must never change the output."""
    row = {"symbol": "AAPL", "direction": "long", "last_price": 100.0, "interest_score": 0.9}
    poisoned = {**row, "_snapshot": {"catalyst": "leaked narrative text"}}
    clean_out = apply_threshold_v1(row, atr_14=2.0, min_reward_risk=1.2, max_holding_days_default=3)
    poisoned_out = apply_threshold_v1(poisoned, atr_14=2.0, min_reward_risk=1.2, max_holding_days_default=3)
    assert clean_out == poisoned_out


# ------------------------------------------------------- day-block bootstrap
def test_day_block_bootstrap_insufficient_data_single_block():
    r = day_block_bootstrap([{"delta_r": 1.0, "decision_date": "2026-01-01"}], "delta_r")
    assert r["status"] == "insufficient_data"
    assert r["n_day_blocks"] == 1


def test_day_block_bootstrap_insufficient_data_empty():
    r = day_block_bootstrap([], "delta_r")
    assert r["status"] == "insufficient_data"


def test_day_block_bootstrap_excludes_rows_missing_value_or_date():
    rows = [
        {"delta_r": 1.0, "decision_date": "2026-01-01"},
        {"delta_r": None, "decision_date": "2026-01-02"},  # excluded: no value
        {"delta_r": 1.0, "decision_date": None},  # excluded: no date
        {"delta_r": 1.0, "decision_date": "2026-01-03"},
    ]
    r = day_block_bootstrap(rows, "delta_r", n_resamples=500, seed=1)
    assert r["n_day_blocks"] == 2


def test_day_block_bootstrap_groups_same_day_rows_into_one_block():
    rows = [{"delta_r": v, "decision_date": "2026-01-01"} for v in (0.1, 0.2, 0.3)]
    rows += [{"delta_r": v, "decision_date": "2026-01-02"} for v in (0.4, 0.5)]
    r = day_block_bootstrap(rows, "delta_r", n_resamples=500, seed=1)
    assert r["n_day_blocks"] == 2  # 5 rows, but only 2 distinct days


def test_day_block_bootstrap_degenerate_zero_variance_falls_back_to_point_interval():
    rows = [{"delta_r": 0.3, "decision_date": f"2026-01-{d:02d}"} for d in range(1, 11)]
    r = day_block_bootstrap(rows, "delta_r", n_resamples=2000, seed=1)
    assert r["status"] == "ok"
    assert r["ci_method"] == "normal_approx"
    assert r["ci_low"] == r["ci_high"] == 0.3


def test_day_block_bootstrap_real_variance_uses_bca():
    import random as _random

    rng = _random.Random(7)
    rows = [{"delta_r": 0.2 + rng.gauss(0, 0.5), "decision_date": f"2026-01-{d:02d}"}
           for d in range(1, 31)]
    r = day_block_bootstrap(rows, "delta_r", n_resamples=10000, seed=42)
    assert r["status"] == "ok"
    assert r["ci_method"] == "bca"
    assert r["ci_low"] < r["point_estimate"] < r["ci_high"]


def test_day_block_bootstrap_below_min_blocks_for_bca_uses_normal_approx():
    """Fewer than MIN_DAY_BLOCKS_FOR_BCA (3) day-blocks -- jackknife
    acceleration is too noisy to trust; must fall back, never crash or
    silently attempt BCa anyway."""
    rows = [{"delta_r": 0.1, "decision_date": "2026-01-01"},
           {"delta_r": 0.9, "decision_date": "2026-01-02"}]
    r = day_block_bootstrap(rows, "delta_r", n_resamples=2000, seed=1)
    assert r["status"] == "ok"
    assert r["ci_method"] == "normal_approx"


def test_day_block_bootstrap_one_sided_p_below_zero_is_between_0_and_1():
    rows = [{"delta_r": -0.5, "decision_date": f"2026-01-{d:02d}"} for d in range(1, 11)]
    r = day_block_bootstrap(rows, "delta_r", n_resamples=2000, seed=1)
    assert r["status"] == "ok"
    assert 0.0 <= r["one_sided_p_below_zero"] <= 1.0
    assert r["one_sided_p_below_zero"] > 0.5  # clearly-negative data: mostly below zero


def test_day_block_bootstrap_never_raises_on_pathological_input():
    weird = [{"delta_r": float("nan"), "decision_date": "2026-01-01"}]
    # NaN parses as a float without raising; the function must not crash even
    # though downstream comparisons involving NaN are inherently degenerate.
    day_block_bootstrap(weird, "delta_r")  # must not raise


def test_day_block_bootstrap_clustered_ci_wider_than_naive_ci_adversarial_proof():
    """THE central claim (spec's own test-list item): a fixture with only 3
    REAL independent days but 10 correlated observations crammed into each
    must produce a WIDER, honestly-uncertain day-block CI than a naive
    (non-blocked, per-row) bootstrap would -- and the naive method must
    falsely exclude zero while the day-block method does not."""
    import random as _random

    rng = _random.Random(99)
    day_means = {"2026-03-02": -0.10, "2026-03-03": 0.50, "2026-03-04": 0.40}
    rows = [
        {"delta_r": mean + rng.gauss(0, 0.02), "decision_date": date}
        for date, mean in day_means.items()
        for _ in range(10)
    ]

    blocked = day_block_bootstrap(rows, "delta_r", n_resamples=10000, seed=42)
    assert blocked["status"] == "ok"

    # Naive: bootstrap over INDIVIDUAL rows, ignoring day structure entirely
    # -- exactly what a non-clustered method would do.
    import random as _r2
    vals = [r["delta_r"] for r in rows]
    n = len(vals)
    naive_rng = _r2.Random(42)
    naive_means = sorted(
        sum(vals[naive_rng.randrange(n)] for _ in range(n)) / n for _ in range(10000)
    )
    naive_lo = naive_means[round(0.05 * (len(naive_means) - 1))]
    naive_hi = naive_means[round(0.95 * (len(naive_means) - 1))]

    naive_width = naive_hi - naive_lo
    blocked_width = blocked["ci_high"] - blocked["ci_low"]

    assert naive_lo > 0, "fixture must produce a naive false-positive (excludes zero)"
    assert blocked["ci_low"] <= 0 <= blocked["ci_high"], (
        "the day-block method must honestly include zero given only 3 real independent days"
    )
    assert blocked_width > naive_width, "day-block CI must be wider than the naive CI"


# ------------------------------------------------------------- pure report
def test_compute_baseline_report_empty_input():
    rep = compute_baseline_report([])
    assert rep["rules"][THRESHOLD_V1]["status"] == "below_sample_floor"
    assert rep["rules"][PROPOSE_ALL_V1]["status"] == "below_sample_floor"
    assert rep["floor_day_blocks"] == FLOOR_DAY_BLOCKS
    assert rep["floor_span_days"] == FLOOR_SPAN_DAYS


def test_compute_baseline_report_paired_delta_r_reproduced_by_hand():
    """One paired ΔR reproduced by hand matches stored (spec's own
    acceptance criterion)."""
    rows = [
        {"rule_version": THRESHOLD_V1, "ai_replay_r": 1.5, "baseline_replay_r": 0.5,
         "decision_at_utc": "2026-01-01T00:00:00+00:00"},
        {"rule_version": THRESHOLD_V1, "ai_replay_r": 1.0, "baseline_replay_r": 1.0,
         "decision_at_utc": "2026-01-02T00:00:00+00:00"},
    ]
    rep = compute_baseline_report(rows)
    assert rep["rules"][THRESHOLD_V1]["status"] == "below_sample_floor"
    # below floor, but the underlying pooled value is what a floor-cleared
    # render would show -- verify via day_block_bootstrap directly on the
    # SAME derived per-row delta_r the report computes internally.
    hand_deltas = [1.5 - 0.5, 1.0 - 1.0]
    boot = day_block_bootstrap(
        [{"delta_r": d, "decision_date": f"2026-01-0{i + 1}"} for i, d in enumerate(hand_deltas)],
        "delta_r",
    )
    assert boot["point_estimate"] == sum(hand_deltas) / len(hand_deltas) == 0.5


def test_compute_baseline_report_meets_floor_shows_aggregate():
    """30 DISTINCT days (Jan 1 -> Jan 30, 29-day span) clears BOTH the
    day-block floor (30) and the span floor (28) -- the floor is measured in
    day-BLOCKS, not raw rows (a repeated day would not count twice)."""
    timestamps = [f"2026-01-{d:02d}T00:00:00+00:00" for d in range(1, 31)]
    rows = [
        {"rule_version": THRESHOLD_V1, "ai_replay_r": 1.0, "baseline_replay_r": 0.0,
         "decision_at_utc": ts}
        for ts in timestamps
    ]
    assert len(rows) == 30
    rep = compute_baseline_report(rows)
    r = rep["rules"][THRESHOLD_V1]
    assert r["status"] == "ok"
    assert r["mean_ai_delta_r"] == 1.0
    assert r["n_paired"] == 30
    assert r["span_days"] == 29.0


def test_compute_baseline_report_below_span_floor_even_with_enough_rows():
    """30+ rows crammed into fewer than 28 days must still show
    below_sample_floor -- a burst never masquerades as weeks of evidence."""
    rows = [
        {"rule_version": PROPOSE_ALL_V1, "ai_replay_r": 1.0, "baseline_replay_r": 0.0,
         "decision_at_utc": f"2026-01-{(d % 10) + 1:02d}T00:00:00+00:00"}
        for d in range(35)
    ]
    rep = compute_baseline_report(rows)
    assert rep["rules"][PROPOSE_ALL_V1]["status"] == "below_sample_floor"


def test_compute_baseline_report_rules_are_independent():
    """threshold_v1 rows must never bleed into propose_all_v1's own
    aggregate or vice versa."""
    rows = [
        {"rule_version": THRESHOLD_V1, "ai_replay_r": 5.0, "baseline_replay_r": 0.0,
         "decision_at_utc": "2026-01-01T00:00:00+00:00"},
    ]
    rep = compute_baseline_report(rows)
    assert rep["rules"][THRESHOLD_V1]["n_paired"] == 1
    assert rep["rules"][PROPOSE_ALL_V1]["n_paired"] == 0


def test_compute_baseline_report_hold10_rule_versions_param_never_mixes_with_v1():
    """Audit-fixup HOLD-2 (HIGH-4 / STATUS CORRECTION item 4):
    compute_baseline_report() now takes rule_versions so the SAME
    aggregation serves the hold10 report -- v1 rows passed in must be
    silently ignored when computing the hold10 arm-set, exactly like
    hold10 rows were always silently ignored computing the v1 arm-set
    (test_compute_baseline_report_rules_are_independent above)."""
    rows = [
        {"rule_version": THRESHOLD_V1, "ai_replay_r": 5.0, "baseline_replay_r": 0.0,
         "decision_at_utc": "2026-01-01T00:00:00+00:00"},
        {"rule_version": THRESHOLD_V1_HOLD10, "ai_replay_r": 3.0, "baseline_replay_r": 1.0,
         "decision_at_utc": "2026-01-01T00:00:00+00:00"},
    ]
    v1_rep = compute_baseline_report(rows)  # default rule_versions -- unchanged behavior
    hold10_rep = compute_baseline_report(rows, rule_versions=BASELINE_RULE_VERSIONS_HOLD10)

    assert v1_rep["rules"][THRESHOLD_V1]["n_paired"] == 1
    assert THRESHOLD_V1_HOLD10 not in v1_rep["rules"]  # never mixed in
    assert hold10_rep["rules"][THRESHOLD_V1_HOLD10]["n_paired"] == 1
    assert THRESHOLD_V1 not in hold10_rep["rules"]  # never mixed in


def test_compute_baseline_report_analysis_not_before_param():
    """Default reproduces the pre-fixup v1 date exactly; hold10 gets its
    OWN fresh pre-registration date ('v2+ arms are new pre-registrations')."""
    assert compute_baseline_report([])["analysis_not_before"] == "2026-09-07"
    assert (
        compute_baseline_report([], analysis_not_before=HOLD10_ANALYSIS_NOT_BEFORE_DATE)["analysis_not_before"]
        == HOLD10_ANALYSIS_NOT_BEFORE_DATE
        == "2026-10-05"
    )


def _seed_complete_paired_row(journal, *, candidate_id, rule_version, ai_r=1.0, baseline_r=0.0,
                              decision_at_utc="2026-01-01T00:00:00+00:00"):
    """A shadow_baseline_decisions row already resolved ('complete',
    replay_r set) PLUS the matching candidate's own resolved
    candidate_outcomes row -- the minimum shape build_baseline_report()'s
    own JOIN needs to count this as 'paired'."""
    from alphaos.util.ids import new_id

    journal.insert("shadow_baseline_decisions", {
        "baseline_decision_id": new_id("basedec"), "candidate_id": candidate_id, "symbol": "AAPL",
        "rule_version": rule_version, "decision": "propose", "decision_reason": "above_threshold",
        "direction": "long", "entry": 100.0, "stop": 96.0, "target": 104.8,
        "max_holding_days": 3, "input_sha": "deadbeef", "decision_at_utc": decision_at_utc,
        "replay_status": "complete", "replay_result": "target_hit", "replay_r": baseline_r,
    })
    journal.insert("candidate_outcomes", {
        "outcome_id": new_id("outc"), "candidate_id": candidate_id, "symbol": "AAPL",
        # VOCAB-1: was the phantom 'resolved' literal build_baseline_report()
        # never actually filters on (the real writer vocabulary is 'complete',
        # not 'resolved' -- see constants.OUTCOME_STATUSES); this fixture
        # helper's whole point is a PAIRED row, so 'complete' is correct.
        "candidate_type": "candidate", "outcome_status": "complete", "replay_r": ai_r,
    })


def test_build_baseline_report_never_pairs_a_partial_outcome_row(journal, settings):
    """Audit-fixup GROUP-A round 1 (FIX-7, both blind Opus audits'
    highest-value finding): VOCAB-1's own spec text is explicit -- 'partial'
    is deliberately NOT included alongside 'complete' in build_baseline_
    report()'s outcome_status filter, because a 'partial' row's 1/3/5-day
    columns are still on a TRUNCATED bar window (fewer than 5 forward bars
    resolved yet) and get silently OVERWRITTEN by a later
    update_pending_outcomes() pass once more bars arrive -- pairing on one
    now would freeze that censored, still-moving value into BASELINE's
    pre-registered evidence forever. This exclusion had ZERO test coverage:
    an auditor mutated the SQL filter to
    ``outcome_status IN ('complete', 'partial')`` -- a direct violation of
    the spec's own ruling -- and the FULL SUITE STAYED GREEN. Proof: a
    'partial' row with replay_r already populated (exactly the shape a
    still-resolving row can have) must NOT be counted as paired, even
    though every other join condition (matching candidate_id, non-null
    replay_r) is satisfied."""
    from alphaos.util.ids import new_id

    journal.insert("shadow_baseline_decisions", {
        "baseline_decision_id": new_id("basedec"), "candidate_id": "cand_partial", "symbol": "AAPL",
        "rule_version": THRESHOLD_V1, "decision": "propose", "decision_reason": "above_threshold",
        "direction": "long", "entry": 100.0, "stop": 96.0, "target": 104.8,
        "max_holding_days": 3, "input_sha": "deadbeef", "decision_at_utc": "2026-01-01T00:00:00+00:00",
        "replay_status": "complete", "replay_result": "target_hit", "replay_r": 0.0,
    })
    journal.insert("candidate_outcomes", {
        "outcome_id": new_id("outc"), "candidate_id": "cand_partial", "symbol": "AAPL",
        # The row under test: 'partial', NOT 'complete' -- still on a
        # truncated bar window -- but replay_r is already non-NULL (the
        # exact shape that would slip through an accidental 'IN
        # (complete, partial)' filter).
        "candidate_type": "candidate", "outcome_status": "partial", "replay_r": 1.0,
    })
    # A genuinely paired 'complete' row too, so the assertion below proves
    # the partial row is SPECIFICALLY excluded, not that pairing is broken
    # entirely.
    _seed_complete_paired_row(journal, candidate_id="cand_complete", rule_version=THRESHOLD_V1)

    rep = build_baseline_report(journal, settings)
    assert rep["n_shadow_resolved"] == 2  # both shadow_baseline_decisions rows are 'complete'
    assert rep["n_paired_total"] == 1  # only the 'complete' outcome row pairs


def test_build_baseline_report_v1_not_diluted_by_newer_hold10_rows(journal, settings):
    """Audit-fixup HOLD-2 (HIGH-4, both audits convergent): before this fix,
    the SQL read ALL rule versions before the Python-side filter dropped
    the ones compute_baseline_report() didn't recognize -- so a shared
    LIMIT (ORDER BY id DESC, favoring recency) would drop the OLDEST v1
    rows once enough NEWER hold10 rows existed (audit A measured n_paired
    1600->1250 on a representative corpus). Proof: 2 OLD v1 rows inserted
    FIRST, then 5 NEWER hold10 rows, with limit=3 -- pre-fixup this returns
    3 hold10 rows and ZERO v1 rows (the 2 old v1 rows never even enter the
    LIMIT window); post-fixup the v1-only query never sees hold10 rows at
    all, so both v1 rows survive regardless of how small the limit is."""
    for i in range(2):
        _seed_complete_paired_row(journal, candidate_id=f"cand_v1_{i}", rule_version=THRESHOLD_V1)
    for i in range(5):
        _seed_complete_paired_row(journal, candidate_id=f"cand_h10_{i}", rule_version=THRESHOLD_V1_HOLD10)

    rep = build_baseline_report(journal, settings, limit=3)
    assert rep["n_shadow_resolved"] == 2
    assert rep["n_paired_total"] == 2
    assert THRESHOLD_V1_HOLD10 not in rep["rules"]


def test_build_baseline_hold10_report_only_sees_hold10_rows(journal, settings):
    _seed_complete_paired_row(journal, candidate_id="cand_v1_x", rule_version=THRESHOLD_V1)
    _seed_complete_paired_row(journal, candidate_id="cand_h10_x", rule_version=THRESHOLD_V1_HOLD10)

    rep = build_baseline_hold10_report(journal, settings)
    assert rep["n_shadow_resolved"] == 1
    assert rep["n_paired_total"] == 1
    assert rep["analysis_not_before"] == HOLD10_ANALYSIS_NOT_BEFORE_DATE
    assert THRESHOLD_V1 not in rep["rules"]
    assert THRESHOLD_V1_HOLD10 in rep["rules"]


def test_render_full_baseline_markdown_segments_hold10_clearly(journal, settings):
    """MEDIUM-9: the hold10 section is a CLEARLY SEGMENTED, distinctly-
    titled block -- never a mixed/merged section."""
    _seed_complete_paired_row(journal, candidate_id="cand_v1_y", rule_version=THRESHOLD_V1)
    _seed_complete_paired_row(journal, candidate_id="cand_h10_y", rule_version=THRESHOLD_V1_HOLD10)

    md = render_full_baseline_markdown(journal, settings)
    assert "## BASELINE -- does the AI add R? (shadow, nothing gated for real)" in md
    assert HOLD10_REPORT_TITLE in md
    # The v1 section's own header appears strictly before the hold10 one.
    assert md.index("## BASELINE --") < md.index(HOLD10_REPORT_TITLE)


def test_resolve_pending_baseline_decisions_rule_aware_v1_not_starved_by_hold10_backlog():
    """Audit-fixup HOLD-2 (HIGH-4, both audits convergent): before this
    fix, a single 'ORDER BY id ASC LIMIT n' query spanned both arm-sets --
    an aging hold10 backlog (resolves markedly slower: 10-trading-day hold
    + 30-day give-up, vs v1's 3-day hold + 15-day give-up) could
    permanently occupy the front of that window and starve v1 resolution
    once the combined backlog exceeded the limit. Proof: 5 OLD hold10
    pending rows inserted FIRST (lower ids, all still genuinely pending --
    no bars provided for them), then 1 NEWER v1 pending row, with limit=2.
    Pre-fixup: the shared query returns only the 2 oldest rows by id (both
    hold10), and the v1 row is never even read this call. Post-fixup: the
    v1 row is resolved regardless, because v1 rows are queried
    independently of the hold10 backlog's size."""
    j = JournalStore(":memory:")
    for i in range(5):
        _pending_row(j, candidate_id=f"cand_h10_{i}", days_ago=1, max_holding_days=10,
                     rule_version=THRESHOLD_V1_HOLD10)
    decision_at_utc = _pending_row(j, candidate_id="cand_v1_new", days_ago=1, rule_version=THRESHOLD_V1)
    bar_date = _bar_dates_after(decision_at_utc, 1)[0]
    provider = _FakeBarsProvider({"AAPL": [
        {"date": bar_date, "high": 106.0, "low": 101.0, "close": 105.0},
    ]})

    counts = resolve_pending_baseline_decisions(j, bars_provider=provider, limit=2)

    v1_row = j.one("SELECT replay_status FROM shadow_baseline_decisions WHERE candidate_id = 'cand_v1_new'")
    assert v1_row["replay_status"] == "complete"  # resolved -- NOT starved by the hold10 backlog
    assert counts["completed"] >= 1
    j.close()


def test_baseline_register_hold10_cli_is_idempotent():
    """MEDIUM-9: mirrors test_baseline_register_cli_is_idempotent()'s own
    shape for the NEW hold10 registration command."""
    from alphaos.__main__ import cmd_baseline_register_hold10

    o = _orch()
    assert cmd_baseline_register_hold10(o) == 0
    assert o.journal.count_rows("preregistrations") == 1
    row = o.journal.one("SELECT * FROM preregistrations")
    assert row["analysis_not_before"] == HOLD10_ANALYSIS_NOT_BEFORE_DATE
    assert cmd_baseline_register_hold10(o) == 0  # no-op, not a duplicate row
    assert o.journal.count_rows("preregistrations") == 1
    o.close()


def test_baseline_register_and_baseline_register_hold10_are_independent():
    """Registering the v1 pre-registration must never satisfy/duplicate
    the hold10 one, and vice versa -- distinct hypothesis/metric text means
    both land as separate rows."""
    from alphaos.__main__ import cmd_baseline_register, cmd_baseline_register_hold10

    o = _orch()
    assert cmd_baseline_register(o) == 0
    assert cmd_baseline_register_hold10(o) == 0
    assert o.journal.count_rows("preregistrations") == 2
    o.close()


def test_render_markdown_below_floor_and_ok_paths_both_render():
    rep = compute_baseline_report([])
    rep["as_of"] = "2026-07-09"
    rep["n_shadow_resolved"] = 0
    rep["n_paired_total"] = 0
    rep["analysis_ready"] = False
    md = render_markdown(rep)
    assert "BASELINE" in md
    assert "below floor" in md
    assert "NOT YET REACHED" in md


# --------------------------------------------------------- journal-aware
def _card_default_holding_days():
    from alphaos.cards.registry import get_card_by_id
    return get_card_by_id(BASELINE_V1_PINNED_CARD_ID)["max_holding_days_default"]


def _hold10_card_holding_days():
    from alphaos.cards.registry import get_card_by_id
    return get_card_by_id(BASELINE_HOLD10_PINNED_CARD_ID)["max_holding_days_default"]


def _make_cand(journal, candidate_id="cand1", symbol="AAPL", direction="long",
               interest_score=0.9, last_price=100.0, card_id=None):
    """``last_price`` is a harmless in-process convenience scalar (per
    ScanContext's own docstring) -- it was NEVER a `candidates` DB column, so
    it must be added to the in-memory dict AFTER insert/fetch, never passed
    to journal.insert() directly."""
    row = {"candidate_id": candidate_id, "symbol": symbol, "direction": direction,
          "interest_score": interest_score}
    if card_id is not None:
        row["card_id"] = card_id
    journal.insert("candidates", row)
    return {**journal.candidate_by_id(candidate_id), "last_price": last_price}


def test_record_shadow_baseline_decisions_writes_two_rows_per_candidate():
    """HOLD-2: now 4 rows/candidate -- the pinned v1 arms (2, unchanged)
    PLUS the new 10-day arm (2 more, additive)."""
    j = JournalStore(":memory:")
    cand = _make_cand(j)
    record_shadow_baseline_decisions(j, make_settings(), cand, scan_batch_id="sb1")
    rows = j.query("SELECT * FROM shadow_baseline_decisions WHERE candidate_id = 'cand1'")
    assert len(rows) == 4
    assert {r["rule_version"] for r in rows} == {
        THRESHOLD_V1, PROPOSE_ALL_V1, THRESHOLD_V1_HOLD10, PROPOSE_ALL_V1_HOLD10,
    }
    for r in rows:
        assert r["scan_batch_id"] == "sb1"
        assert r["input_sha"]
    j.close()


def test_record_shadow_baseline_decisions_no_atr_marks_unavailable_immediately():
    j = JournalStore(":memory:")
    cand = _make_cand(j)
    record_shadow_baseline_decisions(j, make_settings(), cand)
    rows = j.query("SELECT * FROM shadow_baseline_decisions WHERE candidate_id = 'cand1'")
    for r in rows:
        assert r["decision"] == "unavailable"
        assert r["replay_status"] == "unavailable"
        assert r["replay_r"] is None
    j.close()


def test_record_shadow_baseline_decisions_below_threshold_resolves_zero_immediately():
    """A no_action row's replay_r is 0.0 IMMEDIATELY -- a directly-observed
    fact (no position opened), never left pending waiting for bars."""
    j = JournalStore(":memory:")
    cand = _make_cand(j, interest_score=0.1)
    j.insert("atr_history", {
        "atr_id": "atr1", "symbol": "AAPL", "market_date": "2026-01-01",
        "atr_14": 2.0, "rules_version": "atr_rules_v1", "n_bars_fetched": 15,
    })
    record_shadow_baseline_decisions(j, make_settings(), cand)
    threshold_row = j.one(
        "SELECT * FROM shadow_baseline_decisions WHERE candidate_id = 'cand1' AND rule_version = ?",
        (THRESHOLD_V1,),
    )
    assert threshold_row["decision"] == "no_action"
    assert threshold_row["replay_status"] == "complete"
    assert threshold_row["replay_r"] == 0.0
    propose_all_row = j.one(
        "SELECT * FROM shadow_baseline_decisions WHERE candidate_id = 'cand1' AND rule_version = ?",
        (PROPOSE_ALL_V1,),
    )
    assert propose_all_row["decision"] == "propose"
    assert propose_all_row["replay_status"] == "pending"
    j.close()


def test_record_shadow_baseline_decisions_uses_default_card_holding_days():
    """HOLD-2: the v1 arms are PINNED to catalyst_momentum_v2's hold days
    (3) regardless of ACTIVE_CARD_ID; the new hold10 arm always uses
    catalyst_momentum_v3's hold days (10) -- see
    test_record_shadow_baseline_decisions_pin_unaffected_by_active_card_id_swap
    below for the swap-doesn't-move-v1 proof (spec test obligation 6)."""
    j = JournalStore(":memory:")
    cand = _make_cand(j)
    j.insert("atr_history", {
        "atr_id": "atr1", "symbol": "AAPL", "market_date": "2026-01-01",
        "atr_14": 2.0, "rules_version": "atr_rules_v1", "n_bars_fetched": 15,
    })
    record_shadow_baseline_decisions(j, make_settings(), cand)
    rows = j.query("SELECT * FROM shadow_baseline_decisions WHERE candidate_id = 'cand1'")
    for r in rows:
        if r["rule_version"] in BASELINE_RULE_VERSIONS:
            assert r["max_holding_days"] == _card_default_holding_days()
        else:
            assert r["rule_version"] in BASELINE_RULE_VERSIONS_HOLD10
            assert r["max_holding_days"] == _hold10_card_holding_days()
    j.close()


def test_record_shadow_baseline_decisions_pin_unaffected_by_active_card_id_swap():
    """HOLD-2 spec test obligation 6: swapping ACTIVE_CARD_ID must NOT move
    the v1 arms' hold days (they are pinned to catalyst_momentum_v2 by
    explicit id, never get_default_card()/settings.active_card_id)."""
    j = JournalStore(":memory:")
    cand = _make_cand(j)
    j.insert("atr_history", {
        "atr_id": "atr1", "symbol": "AAPL", "market_date": "2026-01-01",
        "atr_14": 2.0, "rules_version": "atr_rules_v1", "n_bars_fetched": 15,
    })
    swapped_settings = make_settings(ACTIVE_CARD_ID="catalyst_momentum_v3")
    assert swapped_settings.active_card_id == "catalyst_momentum_v3"
    record_shadow_baseline_decisions(j, swapped_settings, cand)
    rows = j.query("SELECT * FROM shadow_baseline_decisions WHERE candidate_id = 'cand1'")
    v1_rows = [r for r in rows if r["rule_version"] in BASELINE_RULE_VERSIONS]
    hold10_rows = [r for r in rows if r["rule_version"] in BASELINE_RULE_VERSIONS_HOLD10]
    assert len(v1_rows) == 2
    assert len(hold10_rows) == 2
    for r in v1_rows:
        assert r["max_holding_days"] == 3  # catalyst_momentum_v2, unmoved by the swap
    for r in hold10_rows:
        assert r["max_holding_days"] == 10  # catalyst_momentum_v3, unaffected either way
    j.close()


def test_record_shadow_baseline_decisions_stamps_setup_card_id_from_candidate():
    """Audit-fixup HOLD-2 (S-a): setup_card_id stamps the card that
    ACTUALLY supplied each ARM's max_holding_days_default (the pin), not
    the candidate's own live card assignment. Proven with a candidate
    card_id that matches NEITHER pin -- if setup_card_id were still reading
    cand.get('card_id') this would fail on every row."""
    j = JournalStore(":memory:")
    cand = _make_cand(j, card_id="post_earnings_reaction")
    record_shadow_baseline_decisions(j, make_settings(), cand)
    rows = j.query("SELECT * FROM shadow_baseline_decisions WHERE candidate_id = 'cand1'")
    assert rows
    v1_rows = [r for r in rows if r["rule_version"] in BASELINE_RULE_VERSIONS]
    hold10_rows = [r for r in rows if r["rule_version"] in BASELINE_RULE_VERSIONS_HOLD10]
    assert len(v1_rows) == 2
    assert len(hold10_rows) == 2
    for r in v1_rows:
        assert r["setup_card_id"] == BASELINE_V1_PINNED_CARD_ID == "catalyst_momentum_v2"
    for r in hold10_rows:
        assert r["setup_card_id"] == BASELINE_HOLD10_PINNED_CARD_ID == "catalyst_momentum_v3"
    j.close()


def test_record_shadow_baseline_decisions_entry_fill_status_assumed_filled_for_propose():
    """Spec item 4 (2026-07-09 audit finding LOW-1): a propose row is always
    entry_fill_status='assumed_filled' (a real last_price is required to
    reach 'propose' at all); no_action/unavailable rows get None (no entry
    attempt to characterize)."""
    j = JournalStore(":memory:")
    cand = _make_cand(j, interest_score=0.1)  # below threshold -> no_action for threshold_v1
    j.insert("atr_history", {
        "atr_id": "atr1", "symbol": "AAPL", "market_date": "2026-01-01",
        "atr_14": 2.0, "rules_version": "atr_rules_v1", "n_bars_fetched": 15,
    })
    record_shadow_baseline_decisions(j, make_settings(), cand)
    threshold_row = j.one(
        "SELECT * FROM shadow_baseline_decisions WHERE candidate_id = 'cand1' AND rule_version = ?",
        (THRESHOLD_V1,),
    )
    assert threshold_row["decision"] == "no_action"
    assert threshold_row["entry_fill_status"] is None
    propose_all_row = j.one(
        "SELECT * FROM shadow_baseline_decisions WHERE candidate_id = 'cand1' AND rule_version = ?",
        (PROPOSE_ALL_V1,),
    )
    assert propose_all_row["decision"] == "propose"
    assert propose_all_row["entry_fill_status"] == "assumed_filled"
    j.close()


def test_record_shadow_baseline_decisions_entry_fill_status_none_when_unavailable():
    j = JournalStore(":memory:")
    cand = _make_cand(j)  # no atr_history seeded -> both rules unavailable
    record_shadow_baseline_decisions(j, make_settings(), cand)
    rows = j.query("SELECT * FROM shadow_baseline_decisions WHERE candidate_id = 'cand1'")
    for r in rows:
        assert r["decision"] == "unavailable"
        assert r["entry_fill_status"] is None
    j.close()


def test_record_shadow_baseline_decisions_one_rule_failure_does_not_block_the_other(monkeypatch):
    """2026-07-09 correctness-audit NIT-2: a poisoned field only ONE rule
    reads must not suppress the OTHER rule's row. Simulated by monkeypatching
    ONE entry of RULE_FUNCTIONS to raise; the other rule's row must still be
    written. HOLD-2: the v1-pin loop and the hold10 loop both read the SAME
    RULE_FUNCTIONS dict, so threshold_v1's poisoning is isolated per-ARM too
    -- propose_all_v1's row AND propose_all_v1_hold10's row must both still
    be written (2 rows, not 1)."""
    import alphaos.baseline.tracker as tracker_mod

    def _exploding_rule(row, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setitem(tracker_mod.RULE_FUNCTIONS, THRESHOLD_V1, _exploding_rule)

    j = JournalStore(":memory:")
    cand = _make_cand(j)
    j.insert("atr_history", {
        "atr_id": "atr1", "symbol": "AAPL", "market_date": "2026-01-01",
        "atr_14": 2.0, "rules_version": "atr_rules_v1", "n_bars_fetched": 15,
    })
    record_shadow_baseline_decisions(j, make_settings(), cand)  # must not raise

    rows = j.query("SELECT * FROM shadow_baseline_decisions WHERE candidate_id = 'cand1'")
    assert len(rows) == 2  # propose_all_v1 + propose_all_v1_hold10 -- threshold_v1's failure isolated in BOTH arms
    assert {r["rule_version"] for r in rows} == {PROPOSE_ALL_V1, PROPOSE_ALL_V1_HOLD10}
    j.close()


def test_record_shadow_baseline_decisions_never_raises_on_missing_candidate_id():
    """Shadow recording must be invisible to the live decision -- a
    malformed candidate dict must not raise."""
    j = JournalStore(":memory:")
    record_shadow_baseline_decisions(j, make_settings(), {"symbol": "AAPL"})  # no candidate_id
    assert j.count_rows("shadow_baseline_decisions") == 0
    j.close()


def test_record_shadow_baseline_decisions_idempotent_index_guards_duplicate_insert():
    """The (candidate_id, rule_version) unique index is the real backstop --
    a duplicate insert attempt is caught and logged, never raised. HOLD-2:
    4 distinct rule_version labels now exist per candidate (2 v1-pinned + 2
    hold10), so a re-run must still land on exactly 4, not 8."""
    j = JournalStore(":memory:")
    cand = _make_cand(j)
    record_shadow_baseline_decisions(j, make_settings(), cand)
    record_shadow_baseline_decisions(j, make_settings(), cand)  # must not raise
    assert j.count_rows("shadow_baseline_decisions") == 4  # still exactly 4, not 8
    j.close()


class _FakeBarsProvider:
    def __init__(self, bars_by_symbol):
        self.bars_by_symbol = bars_by_symbol

    def get_daily_bars(self, symbol, start, end, limit=200):
        return [b for b in self.bars_by_symbol.get(symbol, []) if start <= b["date"] <= end]


def _days_ago_iso(n: int) -> str:
    """§H.1: relative to REAL wall-clock now, never a hardcoded literal date
    -- resolve_pending_baseline_decisions computes age_days against
    timeutils.now_utc(), so a fixed past date would silently drift stale as
    real time advances (exactly this codebase's own recurring flaky-test
    class)."""
    from datetime import timedelta
    return timeutils.to_iso(timeutils.now_utc() - timedelta(days=n))


def _pending_row(j, *, entry=100.0, stop=96.0, target=104.8, direction="long",
                 days_ago: int = 1, max_holding_days=3, candidate_id="cand1",
                 rule_version=THRESHOLD_V1):
    from alphaos.util.ids import new_id
    decision_at_utc = _days_ago_iso(days_ago)
    j.insert("shadow_baseline_decisions", {
        "baseline_decision_id": new_id("basedec"), "candidate_id": candidate_id, "symbol": "AAPL",
        "rule_version": rule_version, "decision": "propose", "decision_reason": "above_threshold",
        "direction": direction, "entry": entry, "stop": stop, "target": target,
        "max_holding_days": max_holding_days, "input_sha": "deadbeef",
        "decision_at_utc": decision_at_utc, "replay_status": "pending",
    })
    return decision_at_utc


def _bar_dates_after(decision_at_utc: str, n: int) -> list[str]:
    """n consecutive calendar-day dates strictly after decision_at_utc's own
    date -- forward_bars filtering requires bar date > decision_date."""
    from datetime import timedelta
    decision_date = timeutils.parse_iso(decision_at_utc).date()
    return [(decision_date + timedelta(days=i)).isoformat() for i in range(1, n + 1)]


def test_resolve_pending_baseline_decisions_no_provider_is_a_noop():
    j = JournalStore(":memory:")
    _pending_row(j)
    counts = resolve_pending_baseline_decisions(j, bars_provider=None)
    assert counts == {"total": 0, "updated": 0, "completed": 0, "skipped": 0, "unavailable": 0}
    row = j.one("SELECT replay_status FROM shadow_baseline_decisions LIMIT 1")
    assert row["replay_status"] == "pending"
    j.close()


def test_resolve_pending_baseline_decisions_target_hit_resolves_complete():
    j = JournalStore(":memory:")
    decision_at_utc = _pending_row(j)
    bar_date = _bar_dates_after(decision_at_utc, 1)[0]
    provider = _FakeBarsProvider({"AAPL": [
        {"date": bar_date, "high": 106.0, "low": 101.0, "close": 105.0},
    ]})
    counts = resolve_pending_baseline_decisions(j, bars_provider=provider)
    assert counts["completed"] == 1
    row = j.one("SELECT * FROM shadow_baseline_decisions WHERE candidate_id = 'cand1'")
    assert row["replay_status"] == "complete"
    assert row["replay_result"] == "target_hit"
    assert row["replay_r"] == pytest.approx(1.2, abs=0.01)  # rr = 4.8/4.0
    j.close()


def test_resolve_pending_baseline_decisions_stop_hit_resolves_minus_one():
    j = JournalStore(":memory:")
    decision_at_utc = _pending_row(j)
    bar_date = _bar_dates_after(decision_at_utc, 1)[0]
    provider = _FakeBarsProvider({"AAPL": [
        {"date": bar_date, "high": 99.0, "low": 94.0, "close": 95.0},
    ]})
    counts = resolve_pending_baseline_decisions(j, bars_provider=provider)
    assert counts["completed"] == 1
    row = j.one("SELECT * FROM shadow_baseline_decisions WHERE candidate_id = 'cand1'")
    assert row["replay_result"] == "stop_hit"
    assert row["replay_r"] == -1.0
    j.close()


def test_resolve_pending_baseline_decisions_neither_with_partial_window_stays_pending():
    """The window (max_holding_days=3) has NOT actually elapsed yet -- only
    1 of 3 forward bars exists. A premature 'neither' read must NOT be
    treated as final."""
    j = JournalStore(":memory:")
    decision_at_utc = _pending_row(j, max_holding_days=3)
    bar_date = _bar_dates_after(decision_at_utc, 1)[0]
    provider = _FakeBarsProvider({"AAPL": [
        {"date": bar_date, "high": 101.0, "low": 99.0, "close": 100.5},
    ]})
    counts = resolve_pending_baseline_decisions(j, bars_provider=provider)
    assert counts["skipped"] == 1
    assert counts["completed"] == 0
    row = j.one("SELECT replay_status FROM shadow_baseline_decisions WHERE candidate_id = 'cand1'")
    assert row["replay_status"] == "pending"
    j.close()


def test_resolve_pending_baseline_decisions_neither_with_full_window_resolves_complete():
    """All 3 of 3 forward bars now exist, neither level touched -- a
    genuine, final mark-to-market resolution. days_ago=5 (comfortably more
    than the 3-day window) so all 3 forward bar dates are safely in the past
    relative to real wall-clock now -- the fake provider (like a real one)
    can never return a bar dated after "today"."""
    j = JournalStore(":memory:")
    decision_at_utc = _pending_row(j, max_holding_days=3, days_ago=5)
    dates = _bar_dates_after(decision_at_utc, 3)
    provider = _FakeBarsProvider({"AAPL": [
        {"date": dates[0], "high": 101.0, "low": 99.0, "close": 100.5},
        {"date": dates[1], "high": 102.0, "low": 100.0, "close": 101.0},
        {"date": dates[2], "high": 102.5, "low": 100.5, "close": 101.5},
    ]})
    counts = resolve_pending_baseline_decisions(j, bars_provider=provider)
    assert counts["completed"] == 1
    row = j.one("SELECT * FROM shadow_baseline_decisions WHERE candidate_id = 'cand1'")
    assert row["replay_status"] == "complete"
    assert row["replay_result"] == "neither"
    assert row["replay_r"] is not None
    j.close()


def test_resolve_pending_baseline_decisions_no_bars_ever_stays_pending_until_stale():
    j = JournalStore(":memory:")
    _pending_row(j, days_ago=1)  # recent -- well under UNAVAILABLE_AFTER_DAYS
    provider = _FakeBarsProvider({})  # never any bars for AAPL
    counts = resolve_pending_baseline_decisions(j, bars_provider=provider)
    assert counts["skipped"] == 1
    row = j.one("SELECT replay_status FROM shadow_baseline_decisions WHERE candidate_id = 'cand1'")
    assert row["replay_status"] == "pending"
    j.close()


def test_resolve_pending_baseline_decisions_stale_no_bars_marks_unavailable():
    j = JournalStore(":memory:")
    _pending_row(j, days_ago=20)  # past UNAVAILABLE_AFTER_DAYS (15)
    provider = _FakeBarsProvider({})
    counts = resolve_pending_baseline_decisions(j, bars_provider=provider)
    assert counts["unavailable"] == 1
    row = j.one("SELECT replay_status FROM shadow_baseline_decisions WHERE candidate_id = 'cand1'")
    assert row["replay_status"] == "unavailable"
    j.close()


def test_resolve_pending_baseline_decisions_warns_on_orphaned_rule_version():
    """Round-2 audit-fixup (FIX-B, audit A NEW-2, LOW): HIGH-4's rule-aware
    split turned "resolve every pending row" into "resolve rows in these
    two hardcoded arm-sets" -- a foreign rule_version (e.g. a future
    threshold_v2) would otherwise stay pending FOREVER with zero signal.
    Proof: seed a pending row under a made-up rule_version, run the
    resolver, assert (a) it stays pending -- no resolution attempted --
    and (b) a WARNING system_events row fires naming the count and the
    label, this codebase's own established convention for "the warning
    fired" (log_system_event writes to system_events, not Python
    logging -- see e.g. test_preflight_failure_emits_warning_and_assigns_
    zero_per_candidates in test_s1c_activation.py for the same pattern)."""
    j = JournalStore(":memory:")
    _pending_row(j, candidate_id="cand_orphan", rule_version="threshold_v2_made_up")
    provider = _FakeBarsProvider({})

    resolve_pending_baseline_decisions(j, bars_provider=provider)

    row = j.one("SELECT replay_status FROM shadow_baseline_decisions WHERE candidate_id = 'cand_orphan'")
    assert row["replay_status"] == "pending"  # never touched

    events = j.query(
        "SELECT * FROM system_events WHERE category = 'baseline' AND severity = 'warning'"
    )
    assert events, "expected a WARNING system_events row naming the orphaned rule_version"
    assert any("threshold_v2_made_up" in (e["message"] or "") for e in events)
    assert any("1 shadow_baseline_decisions row" in (e["message"] or "") for e in events)
    j.close()


def test_resolve_pending_baseline_decisions_no_warning_when_nothing_orphaned():
    """The guard must not false-positive on ordinary v1/hold10 rows."""
    j = JournalStore(":memory:")
    _pending_row(j, candidate_id="cand_v1_ok", rule_version=THRESHOLD_V1)
    _pending_row(j, candidate_id="cand_h10_ok", rule_version=THRESHOLD_V1_HOLD10)
    provider = _FakeBarsProvider({})

    resolve_pending_baseline_decisions(j, bars_provider=provider)

    events = j.query(
        "SELECT * FROM system_events WHERE category = 'baseline' AND severity = 'warning' "
        "AND message LIKE '%rule_version(s) this resolver does not know about%'"
    )
    assert not events
    j.close()


def test_hold10_arm_uses_wider_give_up_v1_arm_unaffected():
    """Audit-fixup HOLD-2 (HIGH-3, STATUS CORRECTION item 3, both audits
    convergent): UNAVAILABLE_AFTER_DAYS (15.0 calendar days) is sized for
    the OLD 3-trading-day hold -- a 10-trading-day window spans more than
    15 calendar days on ~9% of trading dates, and ONLY the 0-R 'neither'
    outcomes get marked unavailable/window_never_completed (a stop/target
    hit resolves immediately regardless of age), so the shared give-up
    SELECTIVELY censors hold10's own losing/flat outcomes -- a directional
    bias. Proof: two rows with the IDENTICAL shape (16 calendar days
    elapsed, 9 forward bars, neither level hit, max_holding_days=10) must
    resolve DIFFERENTLY purely based on rule_version: the v1-labelled row
    (15.0 give-up) gives up (unavailable); the _hold10-labelled row (30.0
    give-up) stays pending -- more bars may still complete its real
    10-trading-day window."""
    j = JournalStore(":memory:")
    decision_at_utc = _pending_row(
        j, candidate_id="cand_v1", days_ago=16, max_holding_days=10, rule_version=THRESHOLD_V1,
    )
    _pending_row(
        j, candidate_id="cand_h10", days_ago=16, max_holding_days=10, rule_version=THRESHOLD_V1_HOLD10,
    )
    dates = _bar_dates_after(decision_at_utc, 9)  # neither level hit, window not yet complete (needs 10)
    bars = [{"date": d, "high": 101.0, "low": 99.0, "close": 100.5} for d in dates]
    provider = _FakeBarsProvider({"AAPL": bars})

    resolve_pending_baseline_decisions(j, bars_provider=provider)

    v1_row = j.one("SELECT replay_status FROM shadow_baseline_decisions WHERE candidate_id = 'cand_v1'")
    h10_row = j.one("SELECT replay_status FROM shadow_baseline_decisions WHERE candidate_id = 'cand_h10'")
    assert v1_row["replay_status"] == "unavailable"
    assert h10_row["replay_status"] == "pending"


def test_hold10_arm_eventually_converges_past_its_own_wider_give_up():
    """The wider hold10 give-up is not "never gives up" -- past 30 calendar
    days with no bars at all, a hold10 row still converges to unavailable,
    same terminal-state guarantee the v1 arms have always had."""
    j = JournalStore(":memory:")
    _pending_row(
        j, candidate_id="cand_h10_old", days_ago=31, max_holding_days=10, rule_version=THRESHOLD_V1_HOLD10,
    )
    provider = _FakeBarsProvider({})  # no bars ever
    counts = resolve_pending_baseline_decisions(j, bars_provider=provider)
    row = j.one("SELECT replay_status FROM shadow_baseline_decisions WHERE candidate_id = 'cand_h10_old'")
    assert row["replay_status"] == "unavailable"
    assert counts["unavailable"] == 1
    j.close()


def test_resolve_pending_baseline_decisions_idempotent_only_touches_pending():
    j = JournalStore(":memory:")
    decision_at_utc = _pending_row(j)
    bar_date = _bar_dates_after(decision_at_utc, 1)[0]
    provider = _FakeBarsProvider({"AAPL": [
        {"date": bar_date, "high": 106.0, "low": 101.0, "close": 105.0},
    ]})
    resolve_pending_baseline_decisions(j, bars_provider=provider)
    counts2 = resolve_pending_baseline_decisions(j, bars_provider=provider)
    assert counts2["total"] == 0  # already-complete row never revisited
    j.close()


# ------------------------------------------------------------ orchestrator
def test_baseline_records_2_to_1_with_ai_evaluations_acceptance_criterion():
    """Spec's own acceptance criterion: shadow_baseline_decisions rows 2:1
    with AI evaluations (two rules). HOLD-2: now 4:1 -- the pinned v1 arms
    (2, unchanged ratio) PLUS the new additive 10-day arm (2 more)."""
    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12", LABELLING_ENABLED="true")
    o.run_scan_once()
    n_evals = o.journal.count_rows("openai_evaluations")
    n_baseline = o.journal.count_rows("shadow_baseline_decisions")
    assert n_evals > 0, "scan produced zero evaluations -- test fixture is vacuous"
    assert n_baseline == 4 * n_evals
    o.close()


def test_baseline_disabled_writes_zero_rows():
    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12", LABELLING_ENABLED="true",
             BASELINE_ENABLED="false")
    o.run_scan_once()
    assert o.journal.count_rows("openai_evaluations") > 0
    assert o.journal.count_rows("shadow_baseline_decisions") == 0
    o.close()


def _fingerprint_proposals(journal):
    return [dict(r) for r in journal.query(
        "SELECT symbol, direction, entry, stop, target, qty, status, expected_r, "
        "risk_per_share, dollar_risk, requires_margin, margin_approved, "
        "setup_classification, playbook_name FROM trade_proposals ORDER BY symbol, entry"
    )]


def _fingerprint_rejected(journal):
    return [dict(r) for r in journal.query(
        "SELECT symbol, stage, reason_code, direction, would_be_entry, would_be_stop "
        "FROM rejected_candidates ORDER BY symbol, stage, reason_code"
    )]


def _fingerprint_candidates(journal):
    return [dict(r) for r in journal.query(
        "SELECT symbol, status, label_decision, armed_watch, card_id, card_version "
        "FROM candidates ORDER BY symbol, candidate_id"
    )]


def test_baseline_toggle_does_not_change_decision_artifacts():
    """The core behavior-neutrality claim (shadow law): with BASELINE_ENABLED
    on vs off, every decision-bearing table's content is byte-identical."""
    base = {"INTEREST_SCAN_TOP_N": "12", "MAX_CANDIDATES_TO_AI": "12", "LABELLING_ENABLED": "true"}
    off = _orch(BASELINE_ENABLED="false", **base)
    summ_off = off.run_scan_once()
    proposals_off = _fingerprint_proposals(off.journal)
    rejected_off = _fingerprint_rejected(off.journal)
    candidates_off = _fingerprint_candidates(off.journal)
    off.close()

    on = _orch(BASELINE_ENABLED="true", **base)
    summ_on = on.run_scan_once()
    proposals_on = _fingerprint_proposals(on.journal)
    rejected_on = _fingerprint_rejected(on.journal)
    candidates_on = _fingerprint_candidates(on.journal)
    on.close()

    assert summ_on.proposed == summ_off.proposed
    assert summ_on.watch == summ_off.watch
    assert summ_on.rejected == summ_off.rejected
    assert summ_on.risk_blocked == summ_off.risk_blocked
    assert proposals_on == proposals_off
    assert rejected_on == rejected_off
    assert candidates_on == candidates_off
    # non-vacuity: the A/B must have actually produced rows to compare
    assert proposals_off or rejected_off or candidates_off


def test_record_shadow_baseline_decisions_never_raises_on_a_poisoned_candidate():
    """A candidate whose __getitem__ raises (the closest realistic
    simulation of an unexpected internal error mid-scan) must never
    propagate out of record_shadow_baseline_decisions -- shadow recording is
    best-effort by construction (shadow law: it must be invisible to the
    live decision even when IT is the thing that's broken, not just when
    its inputs are merely missing)."""
    class _PoisonedCand(dict):
        def __getitem__(self, key):
            if key == "symbol":
                raise RuntimeError("boom")
            return super().__getitem__(key)

    j = JournalStore(":memory:")
    cand = _PoisonedCand({"candidate_id": "cand1", "symbol": "AAPL"})
    record_shadow_baseline_decisions(j, make_settings(), cand)  # must not raise
    assert j.count_rows("shadow_baseline_decisions") == 0
    j.close()


def test_decision_functions_never_reference_baseline_module_except_run_scan_once():
    """Precise structural complement to the A/B test above: extract the
    SOURCE of each decision-making function and confirm the new BASELINE
    symbols appear ONLY at the one clearly-marked call site inside
    run_scan_once (never inside _handle_proposal/_resolve_decision/approve/
    reject -- the actual decision-making functions)."""
    decision_functions = (
        "_handle_proposal", "_resolve_decision", "approve_proposal", "reject_proposal",
        "_label_candidate", "_freeze_label", "run_scan_once",
    )
    needles = ("shadow_baseline_decisions", "record_shadow_baseline_decisions", "alphaos.baseline")
    for fn_name in decision_functions:
        fn = getattr(Orchestrator, fn_name)
        source = inspect.getsource(fn)
        if fn_name == "run_scan_once":
            marker = "# BASELINE: the deterministic shadow baseline"
            assert marker in source, "expected BASELINE call-site marker not found in run_scan_once"
            source = source.split(marker)[0]
        for needle in needles:
            assert needle not in source, f"Orchestrator.{fn_name} references {needle}"


def test_risk_engine_and_approval_never_reference_baseline_at_all():
    """The strong, unambiguous version: the actual gate/approval logic must
    not mention the new BASELINE symbols in any form."""
    import alphaos.approval as approval_mod
    import alphaos.risk.risk_engine as risk_mod

    for mod, name in ((approval_mod, "approval.py"), (risk_mod, "risk_engine.py")):
        text = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        assert "shadow_baseline_decisions" not in text
        assert "alphaos.baseline" not in text


def test_strategy_and_swing_never_reference_baseline():
    import alphaos.strategy.swing_strategy as swing_mod

    text = pathlib.Path(swing_mod.__file__).read_text(encoding="utf-8")
    assert "shadow_baseline_decisions" not in text
    assert "alphaos.baseline" not in text


def test_baseline_tracker_uses_the_one_replay_engine_not_a_reimplementation():
    """Structural proof: alphaos/baseline/tracker.py must import
    replay_bracket from outcomes_engine, never define its own bracket-replay
    logic (house pattern: one replay engine, one truth)."""
    text = (ALPHAOS_DIR / "baseline" / "tracker.py").read_text(encoding="utf-8")
    assert "from alphaos.learning.outcomes_engine import" in text
    assert "replay_bracket" in text
    # No local reimplementation of the hit-detection arithmetic.
    assert "stop_breach" not in text
    assert "target_breach" not in text


def test_baseline_report_never_recomputes_replay_only_reads_stored_values():
    """alphaos/reports/baseline_report.py must only ever READ
    candidate_outcomes.replay_r / shadow_baseline_decisions.replay_r as
    already computed -- never re-derive a replay from bars itself."""
    text = (ALPHAOS_DIR / "reports" / "baseline_report.py").read_text(encoding="utf-8")
    assert "get_daily_bars" not in text
    assert "replay_bracket" not in text


# ------------------------------------------------------------------ CLI
def test_baseline_register_cli_is_idempotent():
    from alphaos.__main__ import cmd_baseline_register

    o = _orch()
    assert cmd_baseline_register(o) == 0
    assert o.journal.count_rows("preregistrations") == 1
    assert cmd_baseline_register(o) == 0  # no-op, not a duplicate row
    assert o.journal.count_rows("preregistrations") == 1
    o.close()


def test_baseline_report_cli_runs_end_to_end():
    from alphaos.__main__ import cmd_baseline_report

    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12", LABELLING_ENABLED="true")
    o.run_scan_once()
    assert cmd_baseline_report(o) == 0
    o.close()


# -------------------------------------------------------------- schema/lineage
def test_old_db_gets_shadow_baseline_decisions_table_added_additively(tmp_path):
    db_path = tmp_path / "pre_baseline.db"
    j1 = JournalStore(str(db_path))
    j1.conn.execute("DROP TABLE IF EXISTS shadow_baseline_decisions")
    j1.conn.execute("DROP INDEX IF EXISTS idx_baseline_decisions_candidate_rule")
    j1.conn.commit()
    j1.close()

    j2 = JournalStore(str(db_path))  # re-opening must additively recreate it
    cols = j2._cols("shadow_baseline_decisions")
    for expected in ("baseline_decision_id", "candidate_id", "symbol", "rule_version", "decision",
                    "entry", "stop", "target", "max_holding_days", "input_sha", "decision_at_utc",
                    "replay_status", "replay_r", "lineage_id"):
        assert expected in cols, f"missing column {expected}"
    idx = {r["name"] for r in j2.query(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='shadow_baseline_decisions'")}
    assert "idx_baseline_decisions_candidate_rule" in idx
    j2.close()


def test_old_db_gets_baseline_config_hash_column_added_additively(tmp_path):
    db_path = tmp_path / "pre_baseline_lineage.db"
    j1 = JournalStore(str(db_path))
    j1.close()
    # Simulate a pre-BASELINE lineage_snapshots (missing the new column) by
    # rebuilding it without baseline_config_hash, then reopening.
    raw = __import__("sqlite3").connect(str(db_path))
    raw.execute("ALTER TABLE lineage_snapshots RENAME TO lineage_snapshots_old")
    raw.execute(
        "CREATE TABLE lineage_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "lineage_id TEXT NOT NULL UNIQUE, created_at_utc TEXT NOT NULL, created_at_sgt TEXT NOT NULL)"
    )
    raw.commit()
    raw.close()

    j2 = JournalStore(str(db_path))
    cols = j2._cols("lineage_snapshots")
    assert "baseline_config_hash" in cols
    j2.close()


def test_baseline_config_hash_present_on_lineage_snapshot():
    from alphaos import lineage

    j = JournalStore(":memory:")
    s = make_settings()
    lineage_id = lineage.get_or_create_lineage_id(j, s)
    row = j.one("SELECT baseline_config_hash FROM lineage_snapshots WHERE lineage_id = ?", (lineage_id,))
    assert row["baseline_config_hash"]  # non-empty hash string
    j.close()


def test_baseline_config_hash_changes_when_baseline_enabled_changes():
    from alphaos.lineage.config_snapshot import build_config_hashes

    on = build_config_hashes(make_settings(BASELINE_ENABLED="true"))
    off = build_config_hashes(make_settings(BASELINE_ENABLED="false"))
    assert on["baseline_config_hash"] != off["baseline_config_hash"]
    # And it must NOT perturb unrelated categories.
    assert on["risk_config_hash"] == off["risk_config_hash"]
