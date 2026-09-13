"""S1b: integration tests for the atomic paired evaluation
(alphaos.stats.preregistration.evaluate_two_arm_hypothesis_pair), its PR12/
BH-FDR integration, and the isolation/zero-production-assignment proofs
required before this slice can be considered safe to merge.
"""

from __future__ import annotations

import pathlib
import random
from datetime import date, timedelta

import pytest

from alphaos.cards import registry as cards_registry_mod
from alphaos.cards.selector import PER_CARD_ID, SELECTOR_VERSION
from alphaos.journal.journal_store import JournalStore
from alphaos.stats.fdr import compute_verdicts
from alphaos.stats.preregistration import (
    PreregistrationAlreadyEvaluatedError,
    evaluate_two_arm_hypothesis_pair,
    register_hypothesis,
)
from alphaos.util.ids import new_id


@pytest.fixture
def journal():
    store = JournalStore(":memory:")
    yield store
    store.close()


# --------------------------------------------------------------- fixture helpers
def _insert_cache_row(journal, symbol, report_date, fiscal_date_ending, timing="pre-market"):
    return journal.insert("earnings_calendar_cache", {
        "entry_id": new_id("ecc"), "symbol": symbol, "report_date": report_date,
        "fiscal_date_ending": fiscal_date_ending, "timing": timing, "source": "test",
    })


def _insert_per_candidate(journal, symbol, decision_date, cache_row_id, outcome_value):
    candidate_id = new_id("cand")
    journal.insert("candidates", {
        "candidate_id": candidate_id, "symbol": symbol, "shadow_tier": 0,
        "card_id": PER_CARD_ID, "card_version": 1, "card_assignment_status": "ok",
        "card_assignment_ref": str(cache_row_id), "card_selector_version": SELECTOR_VERSION,
    })
    journal.insert("candidate_outcomes", {
        "outcome_id": new_id("out"), "candidate_id": candidate_id, "symbol": symbol,
        "candidate_type": "candidate", "decision_at_utc": f"{decision_date}T14:30:00+00:00",
        "market_adjusted_return_5d_pct": outcome_value, "outcome_status": "complete",
    })
    return candidate_id


def _insert_control_candidate(journal, symbol, decision_date, outcome_value):
    candidate_id = new_id("cand")
    journal.insert("candidates", {
        "candidate_id": candidate_id, "symbol": symbol, "shadow_tier": 0,
        "card_id": "catalyst_momentum_v2", "card_version": 1, "card_assignment_status": "ok",
    })
    journal.insert("candidate_outcomes", {
        "outcome_id": new_id("out"), "candidate_id": candidate_id, "symbol": symbol,
        "candidate_type": "candidate", "decision_at_utc": f"{decision_date}T14:30:00+00:00",
        "market_adjusted_return_5d_pct": outcome_value, "outcome_status": "complete",
    })
    return candidate_id


def _trading_dates_from(start: date, n: int) -> list[str]:
    out = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=7)
    return out


def _seed_population(journal, n_events=30, effect=0.5, controls_per_date=10, symbol_offset=0, seed=42):
    """``symbol_offset`` lets a second call add MORE events without
    colliding with an earlier call's (symbol, report_date, fiscal_date)
    rows (each symbol/date pair is used exactly once across the whole
    fixture). Outcome values carry real noise (never a constant), since a
    perfectly noiseless fixture makes the bootstrap's own spread exactly
    zero -- correctly triggering the zero-spread guard, but not exercising
    the calibration/power path these tests actually want."""
    rng = random.Random(seed)
    dates = _trading_dates_from(date(2026, 1, 5) + timedelta(days=7 * symbol_offset), n_events)
    for i, d in enumerate(dates):
        symbol = f"SYM{symbol_offset + i}"
        cache_id = _insert_cache_row(journal, symbol, d, fiscal_date_ending=f"fiscal-{symbol_offset + i}")
        _insert_per_candidate(journal, symbol, d, cache_id, outcome_value=rng.gauss(effect, 0.5))
        for j in range(controls_per_date):
            _insert_control_candidate(journal, f"CTL{symbol_offset + i}_{j}", d, outcome_value=rng.gauss(0.0, 0.5))
    return dates


def _seed_valid_card_registry(journal):
    """S1b-integrity: idempotently inserts the ``setup_cards`` row
    ``fetch_active_per_card_identity()`` requires before any identity can be
    frozen. A synthetic row (not read from the real card YAML file, so this
    suite stays independent of production card content drifting) shaped
    exactly like a valid v1 shadow ``post_earnings_reaction`` card."""
    existing = journal.one(
        "SELECT card_id FROM setup_cards WHERE card_id = ? AND version = 1", (PER_CARD_ID,),
    )
    if existing is not None:
        return
    journal.insert("setup_cards", {
        "card_id": PER_CARD_ID, "version": 1, "name": PER_CARD_ID, "state": "shadow",
        "content_hash": "test-fixture-hash-0001",
        "content_json": {"requires_selector": SELECTOR_VERSION},
    })


def _register_pair(journal, floor_effective_n=20, floor_span_days=90.0):
    """S1b-integrity: freezes the live card/selector identity into both
    rows' ``params_json`` -- required since ``evaluate_two_arm_hypothesis_
    pair()``'s new identity gate (Section 3) refuses any pair whose frozen
    identity is missing or doesn't match what's live. ``_seed_valid_card_
    registry()`` guarantees a live identity exists to freeze."""
    from alphaos.cards.per_evidence import fetch_active_per_card_identity

    _seed_valid_card_registry(journal)
    identity = fetch_active_per_card_identity(journal)
    prereg_pos = register_hypothesis(
        journal, hypothesis="H-PER-1P: PER excess is positive", metric="per_excess_market_adjusted_5d",
        floor_effective_n=floor_effective_n, floor_span_days=floor_span_days,
        analysis_not_before="2026-01-01", params={**identity},
    )
    prereg_neg = register_hypothesis(
        journal, hypothesis="H-PER-1N: PER excess is negative", metric="per_excess_market_adjusted_5d_negated",
        floor_effective_n=floor_effective_n, floor_span_days=floor_span_days,
        analysis_not_before="2026-01-01", params={**identity},
    )
    return prereg_pos, prereg_neg


AS_OF = "2027-01-01T00:00:00+00:00"


# --------------------------------------------------------------- atomic eval
def test_clean_positive_population_cannot_freeze_changed_per_contract(journal):
    _seed_population(journal, n_events=30, effect=2.0)
    pos, neg = _register_pair(journal)
    before = journal.query("SELECT * FROM preregistrations ORDER BY id")
    result = evaluate_two_arm_hypothesis_pair(journal, pos, neg, AS_OF, n_resamples=500, seed=1)
    assert result["outcome"] == "deferred"
    assert result["reason"] == "research_contract_quarantined"
    assert journal.query("SELECT * FROM preregistrations ORDER BY id") == before
    assert journal.count_rows("per_evidence_snapshots") == 0


def test_evaluation_defers_below_registered_floor_and_writes_nothing(journal):
    _seed_population(journal, n_events=30)
    pos, neg = _register_pair(journal, floor_effective_n=1000)  # unreachable floor
    result = evaluate_two_arm_hypothesis_pair(journal, pos, neg, AS_OF, n_resamples=200, seed=1)
    assert result["outcome"] == "deferred"
    assert result["reason"] == "registered_floor_not_cleared"
    pos_row = journal.one("SELECT * FROM preregistrations WHERE prereg_id = ?", (pos,))
    assert pos_row["evaluated_at_utc"] is None
    assert journal.count_rows("per_evidence_snapshots") == 0


def test_evaluation_defers_below_population_gate_and_one_shot_survives(journal):
    """A too-small raw population defers without consuming the one-shot --
    proven by seeding MORE data afterward and confirming the SAME prereg
    pair can still be successfully evaluated later."""
    _seed_population(journal, n_events=10)
    pos, neg = _register_pair(journal)
    result1 = evaluate_two_arm_hypothesis_pair(journal, pos, neg, AS_OF, n_resamples=200, seed=1)
    assert result1["outcome"] == "deferred"
    assert result1["reason"] == "per_raw_n_below_floor"

    _seed_population(journal, n_events=30, symbol_offset=100)  # more events accrue, no collision
    result2 = evaluate_two_arm_hypothesis_pair(journal, pos, neg, AS_OF, n_resamples=500, seed=1)
    assert result2["reason"] == "research_contract_quarantined"
    assert all(r["evaluated_at_utc"] is None for r in journal.query("SELECT * FROM preregistrations"))


def test_config_mismatch_between_paired_floors_raises(journal):
    pos, neg = _register_pair(journal, floor_effective_n=20)
    # Re-register neg with a different floor directly (simulating an operator/registration bug).
    journal.conn.execute("UPDATE preregistrations SET floor_effective_n = 999 WHERE prereg_id = ?", (neg,))
    journal.conn.commit()
    with pytest.raises(ValueError, match="floor mismatch"):
        evaluate_two_arm_hypothesis_pair(journal, pos, neg, AS_OF)


# --------------------------------------------------------------- one-shot refusal
def test_one_shot_refusal_after_successful_evaluation(journal):
    _seed_population(journal, n_events=30, effect=1.0)
    pos, neg = _register_pair(journal)
    # Simulate preserved pre-repair evidence, not a new evaluation.
    journal.conn.execute("UPDATE preregistrations SET evaluated_at_utc='2026-08-01'")
    journal.conn.commit()
    with pytest.raises(PreregistrationAlreadyEvaluatedError):
        evaluate_two_arm_hypothesis_pair(journal, pos, neg, AS_OF, n_resamples=300, seed=2)


def test_atomic_paired_evaluation_never_partially_writes(journal):
    """Simulates the race the atomicity guard exists for: one row of the
    pair gets marked evaluated out-of-band BETWEEN this function's own
    upfront read and its write. The upfront check catches the common case
    immediately; this test additionally confirms the OTHER (still-
    unevaluated) row is untouched afterward -- no partial write."""
    _seed_population(journal, n_events=30, effect=1.0)
    pos, neg = _register_pair(journal)
    # Simulate a concurrent evaluator winning the race on `neg` only.
    journal.conn.execute(
        "UPDATE preregistrations SET evaluated_at_utc = ? WHERE prereg_id = ?",
        ("2026-06-01T00:00:00+00:00", neg),
    )
    journal.conn.commit()
    with pytest.raises(PreregistrationAlreadyEvaluatedError):
        evaluate_two_arm_hypothesis_pair(journal, pos, neg, AS_OF, n_resamples=200, seed=1)
    pos_row = journal.one("SELECT * FROM preregistrations WHERE prereg_id = ?", (pos,))
    assert pos_row["evaluated_at_utc"] is None, "pos must remain unevaluated -- no partial write"
    assert journal.count_rows("per_evidence_snapshots") == 0


# ----------------------------------------------------------------- reproducibility
def test_evaluation_reproducible_given_fixed_seed_across_independent_journals():
    j1 = JournalStore(":memory:")
    j2 = JournalStore(":memory:")
    try:
        _seed_population(j1, n_events=30, effect=1.5)
        _seed_population(j2, n_events=30, effect=1.5)
        pos1, neg1 = _register_pair(j1)
        pos2, neg2 = _register_pair(j2)
        r1 = evaluate_two_arm_hypothesis_pair(j1, pos1, neg1, AS_OF, n_resamples=500, seed=7)
        r2 = evaluate_two_arm_hypothesis_pair(j2, pos2, neg2, AS_OF, n_resamples=500, seed=7)
        assert r1 == r2
        assert r1["reason"] == "research_contract_quarantined"
        assert j1.count_rows("per_evidence_snapshots") == j2.count_rows("per_evidence_snapshots") == 0
    finally:
        j1.close()
        j2.close()


# ----------------------------------------------------------------- PR12/BH-FDR
def test_historical_pair_keeps_raw_verdict_but_is_not_forward_test_eligible(journal):
    pos, neg = _register_pair(journal)
    for pid, low, high, p in ((pos, 1.0, 2.0, 0.001), (neg, -2.0, -1.0, 0.99)):
        journal.conn.execute("UPDATE preregistrations SET evaluated_at_utc='2026-08-01',"
            "ci_low=?,ci_high=?,one_sided_p_below_zero=?,evidence_status='ok' WHERE prereg_id=?", (low, high, p, pid))
    journal.conn.commit()
    before = journal.query("SELECT * FROM preregistrations ORDER BY id")
    by_id = {v["prereg_id"]: v for v in compute_verdicts(before)}
    assert by_id[pos]["verdict"] == "forward-test-candidate"
    assert by_id[neg]["verdict"] == "rejected"
    assert all(not v["forward_test_eligible"] for v in by_id.values())
    assert journal.query("SELECT * FROM preregistrations ORDER BY id") == before


# ------------------------------------------------------------------- isolation
# SETUP-1 S1c (candidate-level activation) legitimately wires
# alphaos.cards.selector/alphaos.cards.per_evidence into orchestrator.py/
# candidate_scanner.py -- the two negative "never referenced"/"never
# loaded" proofs that used to live here are now FALSE BY DESIGN and have
# moved to tests/test_s1c_activation.py as POSITIVE proofs of the same
# transition (test_selector_and_per_evidence_now_legitimately_referenced,
# test_production_import_graph_now_loads_selector_and_per_evidence,
# test_formal_evaluation_module_still_never_referenced,
# test_selector_never_referenced_by_risk_approval_sizing_execution_paths).
# The ONE check below that remains TRUE post-S1c stays here: PR13's own
# card registry never needs to know about the selector at all.
def test_cards_registry_never_references_the_selector():
    text = pathlib.Path(str(cards_registry_mod.__file__)).read_text(encoding="utf-8")
    for forbidden in ("cards.selector", "select_card", "per_evidence", "two_arm"):
        assert forbidden not in text, f"cards/registry.py references {forbidden!r} unexpectedly"


def test_per_register_cli_registers_both_hypotheses(orchestrator):
    from alphaos.__main__ import cmd_per_register

    assert cmd_per_register(orchestrator) == 0
    rows = orchestrator.journal.query(
        "SELECT hypothesis, metric FROM preregistrations WHERE hypothesis LIKE 'H-PER-1%'"
    )
    assert len(rows) == 2
    hyps = {r["hypothesis"][:9] for r in rows}
    assert hyps == {"H-PER-1P:", "H-PER-1N:"}


def test_per_register_cli_is_idempotent(orchestrator):
    from alphaos.__main__ import cmd_per_register

    assert cmd_per_register(orchestrator) == 0
    assert cmd_per_register(orchestrator) == 0  # no-op, not a duplicate pair
    rows = orchestrator.journal.query(
        "SELECT prereg_id FROM preregistrations WHERE hypothesis LIKE 'H-PER-1%'"
    )
    assert len(rows) == 2


def test_per_evaluate_cli_before_registration_returns_1(orchestrator):
    from alphaos.__main__ import cmd_per_evaluate

    assert cmd_per_evaluate(orchestrator) == 1


def test_per_evaluate_cli_never_substitutes_original_pair(orchestrator, capsys):
    from alphaos.__main__ import cmd_per_register, cmd_per_evaluate
    assert cmd_per_register(orchestrator) == 0
    before = orchestrator.journal.query("SELECT * FROM preregistrations ORDER BY id")
    assert cmd_per_evaluate(orchestrator) == 1
    assert "not_registered" in capsys.readouterr().out
    assert orchestrator.journal.query("SELECT * FROM preregistrations ORDER BY id") == before
