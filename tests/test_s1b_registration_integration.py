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

import alphaos.orchestrator as orchestrator_mod
import alphaos.scanner.candidate_scanner as candidate_scanner_mod
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


def _register_pair(journal, floor_effective_n=20, floor_span_days=90.0):
    prereg_pos = register_hypothesis(
        journal, hypothesis="H-PER-1P: PER excess is positive", metric="per_excess_market_adjusted_5d",
        floor_effective_n=floor_effective_n, floor_span_days=floor_span_days,
        analysis_not_before="2026-01-01",
    )
    prereg_neg = register_hypothesis(
        journal, hypothesis="H-PER-1N: PER excess is negative", metric="per_excess_market_adjusted_5d_negated",
        floor_effective_n=floor_effective_n, floor_span_days=floor_span_days,
        analysis_not_before="2026-01-01",
    )
    return prereg_pos, prereg_neg


AS_OF = "2027-01-01T00:00:00+00:00"


# --------------------------------------------------------------- atomic eval
def test_atomic_evaluation_succeeds_on_a_clean_positive_population(journal):
    _seed_population(journal, n_events=30, effect=2.0)
    pos, neg = _register_pair(journal)
    result = evaluate_two_arm_hypothesis_pair(journal, pos, neg, AS_OF, n_resamples=500, seed=1)
    assert result["outcome"] == "evaluated"
    assert result["pos"]["p_value"] < 0.05
    assert result["neg"]["p_value"] > 0.5
    assert result["pos"]["point_estimate"] == pytest.approx(-result["neg"]["point_estimate"])

    pos_row = journal.one("SELECT * FROM preregistrations WHERE prereg_id = ?", (pos,))
    neg_row = journal.one("SELECT * FROM preregistrations WHERE prereg_id = ?", (neg,))
    assert pos_row["evaluated_at_utc"] is not None
    assert neg_row["evaluated_at_utc"] is not None
    assert pos_row["evidence_status"] == "ok" == neg_row["evidence_status"]
    # Directional frame mapping (spec Section 7): H-PER-1N stores the
    # negated estimate and swapped+negated CI.
    assert neg_row["point_estimate"] == pytest.approx(-pos_row["point_estimate"])
    assert neg_row["ci_low"] == pytest.approx(-pos_row["ci_high"])
    assert neg_row["ci_high"] == pytest.approx(-pos_row["ci_low"])

    snapshot_rows = journal.query(
        "SELECT * FROM per_evidence_snapshots WHERE snapshot_id = ?", (result["snapshot_id"],),
    )
    assert len(snapshot_rows) > 0
    assert any(r["arm"] == "per_event" for r in snapshot_rows)
    assert any(r["arm"] == "control" for r in snapshot_rows)


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
    assert result2["outcome"] == "evaluated", "the one-shot must still be available after a deferred attempt"


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
    evaluate_two_arm_hypothesis_pair(journal, pos, neg, AS_OF, n_resamples=300, seed=1)
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
        assert r1["pos"]["point_estimate"] == r2["pos"]["point_estimate"]
        assert r1["pos"]["p_value"] == r2["pos"]["p_value"]
        assert r1["pos"]["ci_low"] == r2["pos"]["ci_low"]
        assert r1["pos"]["ci_high"] == r2["pos"]["ci_high"]
    finally:
        j1.close()
        j2.close()


# ----------------------------------------------------------------- PR12/BH-FDR
def test_evaluated_pair_integrates_with_existing_compute_verdicts_unmodified(journal):
    """Reuses PR12 rather than a parallel verdict framework: the SAME
    compute_verdicts() every other hypothesis uses reads these two rows
    correctly, with ZERO changes to fdr.py."""
    _seed_population(journal, n_events=30, effect=3.0)
    pos, neg = _register_pair(journal)
    evaluate_two_arm_hypothesis_pair(journal, pos, neg, AS_OF, n_resamples=1000, seed=1)

    rows = journal.query("SELECT * FROM preregistrations WHERE evaluated_at_utc IS NOT NULL")
    verdicts = compute_verdicts(rows)
    by_id = {v["prereg_id"]: v for v in verdicts}
    assert by_id[pos]["verdict"] in ("forward-test-candidate", "inconclusive", "rejected")
    assert by_id[neg]["verdict"] == "rejected", "a strongly positive true effect must hard-reject H-PER-1N"


# ------------------------------------------------------------------- isolation
PRODUCTION_FILES = [
    (orchestrator_mod, "orchestrator.py"),
    (candidate_scanner_mod, "candidate_scanner.py"),
    (cards_registry_mod, "cards/registry.py"),
]


@pytest.mark.parametrize("mod,name", PRODUCTION_FILES)
def test_s1b_modules_never_referenced_by_production_files(mod, name):
    """A substring check on each file's own source text -- catches a
    direct reference, but a static string match can miss a differently-
    spelled import (e.g. ``from alphaos.cards import selector``, which
    contains neither ``"cards.selector"`` nor ``"select_card"`` as a
    literal substring) and says nothing about the TRANSITIVE import graph.
    ``test_s1b_production_import_graph_never_loads_dormant_modules`` below
    is the real, load-bearing guarantee; this test is a fast first-pass
    sanity check, kept for its historical value against a direct-reference
    regression."""
    text = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    for forbidden in ("cards.selector", "select_card", "per_evidence", "two_arm",
                      "evaluate_two_arm_hypothesis_pair"):
        assert forbidden not in text, f"{name} references {forbidden!r} -- S1b must remain unwired"


def test_s1b_production_import_graph_never_loads_dormant_modules():
    """Audit-fixup (architecture LOW): the real guarantee, stronger than a
    source-text grep -- importing the full production stack (orchestrator,
    scanner, card registry, and the nightly hypothesis resolver) must never
    cause Python to actually LOAD any of S1b's still-dormant modules,
    regardless of how they might be imported (aliased, re-exported,
    star-imported, etc. -- none of which a plain substring match would
    catch). Run in a FRESH SUBPROCESS deliberately: this test file's own
    top-level ``from alphaos.cards.selector import ...`` (needed for its
    other fixtures) would already be sitting in THIS process's
    ``sys.modules`` by the time this test runs, which would make an
    in-process check pass or fail for the wrong reason regardless of the
    real production import graph."""
    import subprocess
    import sys

    script = (
        "import sys\n"
        "import alphaos.hypotheses.resolver\n"
        "import alphaos.orchestrator\n"
        "import alphaos.scanner.candidate_scanner\n"
        "import alphaos.cards.registry\n"
        "import alphaos.stats.preregistration\n"
        "leaked = [m for m in "
        "('alphaos.cards.selector', 'alphaos.cards.per_evidence', 'alphaos.stats.two_arm') "
        "if m in sys.modules]\n"
        "print(','.join(leaked))\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    leaked = [m for m in result.stdout.strip().split(",") if m]
    assert not leaked, (
        f"{leaked} loaded merely by importing the production stack -- S1b leaked into the "
        "production import graph"
    )


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


def test_per_evaluate_cli_runs_full_pipeline_and_is_idempotent_after_success(orchestrator):
    from alphaos.__main__ import cmd_per_evaluate, cmd_per_register

    _seed_population(orchestrator.journal, n_events=30, effect=2.0)
    assert cmd_per_register(orchestrator) == 0
    assert cmd_per_evaluate(orchestrator) == 0
    rows = orchestrator.journal.query(
        "SELECT evaluated_at_utc FROM preregistrations WHERE hypothesis LIKE 'H-PER-1%'"
    )
    assert all(r["evaluated_at_utc"] is not None for r in rows)
    # A second CLI call must not raise -- it prints "already_evaluated" and returns 0.
    assert cmd_per_evaluate(orchestrator) == 0


def test_production_scan_produces_zero_per_assignments(orchestrator):
    """The hard requirement: a full production scan at S1b HEAD must
    produce zero post_earnings_reaction assignments -- select_card() is
    never called, so no candidate can ever be stamped with this card_id."""
    orchestrator.run_scan_once()
    total_candidates = orchestrator.journal.scalar("SELECT COUNT(*) FROM candidates")
    # Audit-fixup (architecture LOW): without this, the test would stay
    # green even if a future change made the scan produce ZERO candidates
    # of any kind -- a vacuous pass that proves nothing. The mock-mode scan
    # is expected to produce real candidates every run.
    assert total_candidates > 0, "scan produced no candidates at all -- this test can no longer prove anything"
    count = orchestrator.journal.scalar(
        "SELECT COUNT(*) FROM candidates WHERE card_id = ?", (PER_CARD_ID,),
    )
    assert count == 0
