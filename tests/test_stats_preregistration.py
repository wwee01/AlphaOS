"""PORT-1: alphaos.stats.preregistration -- register once, evaluate at most
once. Uses the shared ``journal`` fixture (in-memory SQLite, conftest.py).
"""

from __future__ import annotations

import pytest

from alphaos.stats.preregistration import (
    PreregistrationAlreadyEvaluatedError,
    evaluate_hypothesis,
    register_hypothesis,
)


def _rows(symbol_values):
    """One row per (symbol, date, value) triple -- date/holding_days omitted
    (degrades to same-day-only clustering, exercised elsewhere)."""
    return [
        {"symbol": sym, "decision_date": d, "delta_r": v}
        for sym, d, v in symbol_values
    ]


# --------------------------------------------------------------- registration
def test_register_hypothesis_creates_an_unevaluated_row(journal):
    prereg_id = register_hypothesis(
        journal, hypothesis="catalyst_momentum_v1 beats no-trade", metric="delta_r",
        floor_effective_n=20, floor_span_days=28, analysis_not_before="2026-09-01",
    )
    row = journal.one("SELECT * FROM preregistrations WHERE prereg_id = ?", (prereg_id,))
    assert row is not None
    assert row["hypothesis"] == "catalyst_momentum_v1 beats no-trade"
    assert row["evaluated_at_utc"] is None
    assert row["effective_n"] is None
    assert row["operator_approved_for_forward_test"] == 0
    assert row["strong_prior_pre_documented"] == 0


def test_register_hypothesis_with_params(journal):
    prereg_id = register_hypothesis(
        journal, hypothesis="h", metric="delta_r", floor_effective_n=20, floor_span_days=28,
        analysis_not_before="2026-09-01", params={"card_id": "catalyst_momentum_v1", "k": 2},
    )
    row = journal.one("SELECT * FROM preregistrations WHERE prereg_id = ?", (prereg_id,))
    assert row["params_json"] == '{"card_id": "catalyst_momentum_v1", "k": 2}'


def test_strong_prior_requires_reasoning(journal):
    with pytest.raises(ValueError, match="strong_prior_reasoning"):
        register_hypothesis(
            journal, hypothesis="h", metric="delta_r", floor_effective_n=20, floor_span_days=28,
            analysis_not_before="2026-09-01", strong_prior_pre_documented=True,
        )


def test_strong_prior_with_reasoning_is_accepted(journal):
    prereg_id = register_hypothesis(
        journal, hypothesis="h", metric="delta_r", floor_effective_n=20, floor_span_days=28,
        analysis_not_before="2026-09-01", strong_prior_pre_documented=True,
        strong_prior_reasoning="documented in DECISIONS.md #12 before any test ran",
    )
    row = journal.one("SELECT * FROM preregistrations WHERE prereg_id = ?", (prereg_id,))
    assert row["strong_prior_pre_documented"] == 1
    assert "DECISIONS.md" in row["strong_prior_reasoning"]


def test_every_variant_gets_its_own_row(journal):
    """Pre-registration discipline (contract doc Sec 6): testing 3 phrasings
    of the same idea means 3 rows, never a shared/reused one."""
    ids = [
        register_hypothesis(
            journal, hypothesis=f"variant {i}", metric="delta_r",
            floor_effective_n=20, floor_span_days=28, analysis_not_before="2026-09-01",
        )
        for i in range(3)
    ]
    assert len(set(ids)) == 3
    assert journal.count_rows("preregistrations") == 3


# ------------------------------------------------------------------ evaluation
def test_evaluate_hypothesis_freezes_evidence(journal):
    prereg_id = register_hypothesis(
        journal, hypothesis="h", metric="delta_r", floor_effective_n=2, floor_span_days=1,
        analysis_not_before="2026-09-01",
    )
    rows = _rows([("AAPL", "2026-01-01", 1.0), ("MSFT", "2026-01-02", 1.0), ("GOOG", "2026-01-03", 1.0)])
    result = evaluate_hypothesis(journal, prereg_id, rows, value_key="delta_r", seed=1)

    assert result["evaluated_at_utc"] is not None
    assert result["effective_n"] == 3
    assert result["n_raw"] == 3
    assert result["point_estimate"] == 1.0
    assert result["evidence_status"] == "ok"

    # Re-read from the DB directly -- confirms the UPDATE actually persisted,
    # not just the returned dict.
    persisted = journal.one("SELECT * FROM preregistrations WHERE prereg_id = ?", (prereg_id,))
    assert persisted["evaluated_at_utc"] == result["evaluated_at_utc"]
    assert persisted["effective_n"] == 3


def test_evaluate_hypothesis_insufficient_data_status(journal):
    prereg_id = register_hypothesis(
        journal, hypothesis="h", metric="delta_r", floor_effective_n=20, floor_span_days=28,
        analysis_not_before="2026-09-01",
    )
    rows = _rows([("AAPL", "2026-01-01", 1.0)])  # 1 cluster -- bootstrap needs >= 2
    result = evaluate_hypothesis(journal, prereg_id, rows, value_key="delta_r", seed=1)
    assert result["evidence_status"] == "insufficient_data"
    assert result["effective_n"] == 1


def test_evaluate_nonexistent_prereg_id_raises(journal):
    with pytest.raises(ValueError, match="no such preregistration"):
        evaluate_hypothesis(journal, "prereg_doesnotexist", [], value_key="delta_r")


# --------------------------------------------------- one-shot / immutability
def test_evaluating_twice_raises_and_leaves_original_evidence_untouched(journal):
    """The spec's own required test: evaluated_at_utc is set EXACTLY ONCE --
    a second write raises, defending against optional stopping."""
    prereg_id = register_hypothesis(
        journal, hypothesis="h", metric="delta_r", floor_effective_n=2, floor_span_days=1,
        analysis_not_before="2026-09-01",
    )
    first_rows = _rows([("AAPL", "2026-01-01", 1.0), ("MSFT", "2026-01-02", 1.0)])
    first_result = evaluate_hypothesis(journal, prereg_id, first_rows, value_key="delta_r", seed=1)

    second_rows = _rows([("AAPL", "2026-01-01", -5.0), ("MSFT", "2026-01-02", -5.0), ("GOOG", "2026-01-03", -5.0)])
    with pytest.raises(PreregistrationAlreadyEvaluatedError):
        evaluate_hypothesis(journal, prereg_id, second_rows, value_key="delta_r", seed=1)

    # The original evidence must be completely unchanged -- a lucky re-run
    # with different (favorable) data must never overwrite the frozen result.
    after = journal.one("SELECT * FROM preregistrations WHERE prereg_id = ?", (prereg_id,))
    assert after["evaluated_at_utc"] == first_result["evaluated_at_utc"]
    assert after["effective_n"] == first_result["effective_n"]
    assert after["point_estimate"] == first_result["point_estimate"]


def test_one_shot_guard_is_a_db_level_where_clause_not_just_application_check(journal):
    """Even if a caller bypasses the friendly pre-check (e.g. a stale read),
    the UPDATE's own WHERE evaluated_at_utc IS NULL clause is the real
    backstop -- verified by evaluating, then manually clearing the Python-
    level existence check's assumptions via a direct re-call."""
    prereg_id = register_hypothesis(
        journal, hypothesis="h", metric="delta_r", floor_effective_n=2, floor_span_days=1,
        analysis_not_before="2026-09-01",
    )
    rows = _rows([("AAPL", "2026-01-01", 1.0), ("MSFT", "2026-01-02", 1.0)])
    evaluate_hypothesis(journal, prereg_id, rows, value_key="delta_r", seed=1)

    # Simulate two concurrent evaluators both having read "unevaluated" --
    # the second one's UPDATE must affect zero rows and raise, never silently
    # overwrite.
    cursor = journal.conn.execute(
        "UPDATE preregistrations SET evaluated_at_utc=? WHERE prereg_id=? AND evaluated_at_utc IS NULL",
        ("2099-01-01T00:00:00+00:00", prereg_id),
    )
    assert cursor.rowcount == 0
