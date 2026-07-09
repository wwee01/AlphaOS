"""PORT-1: alphaos.stats.fdr -- BH-FDR, Bonferroni, and the always-fresh
three-way verdict function.
"""

from __future__ import annotations

from alphaos.stats.fdr import (
    benjamini_hochberg,
    bh_q_values,
    bonferroni_significant,
    compute_verdicts,
    expected_false_positives,
    preregistration_family_summary,
)

# Textbook BH vector (10 hypotheses, q=0.10): hand-derived expected q-values
# via the running-minimum form (contract doc Sec 3). p_(6..10) are each
# individually within their own rank's raw threshold territory but the
# running-minimum enforces monotonicity back down from the tail.
_TEXTBOOK_P = [0.01, 0.02, 0.03, 0.04, 0.05, 0.20, 0.30, 0.50, 0.80, 0.90]
_TEXTBOOK_Q = [0.1, 0.1, 0.1, 0.1, 0.1, 0.333333, 0.428571, 0.625, 0.888889, 0.9]
_TEXTBOOK_DISCOVERY = [True, True, True, True, True, False, False, False, False, False]


def test_benjamini_hochberg_textbook_vector():
    assert benjamini_hochberg(_TEXTBOOK_P, q=0.10) == _TEXTBOOK_DISCOVERY


def test_bh_q_values_textbook_vector_exact():
    assert bh_q_values(_TEXTBOOK_P) == _TEXTBOOK_Q


def test_q_value_le_q_agrees_with_boolean_discovery():
    """The two BH exposures (boolean step-up vs q-value) must always agree --
    same math, different shape (contract doc Sec 3)."""
    q_values = bh_q_values(_TEXTBOOK_P)
    discovery = benjamini_hochberg(_TEXTBOOK_P, q=0.10)
    assert [q <= 0.10 for q in q_values] == discovery


def test_q_value_and_boolean_discovery_agree_at_an_exact_boundary_tie():
    """Correctness-audit regression: benjamini_hochberg() and bh_q_values()
    used to be independently factored (p <= (k/n)*q vs (n/k)*p <= q), which
    round to opposite sides of an exact float64 tie -- silently violating
    the documented equivalence and producing a self-contradictory verdict
    row (q_value <= q but verdict says inconclusive). Both must now be
    derived from the same running-minimum computation."""
    p = [0.469, 0.417, 0.006, 0.24, 0.01, 0.81]
    q = 0.03
    discovery = benjamini_hochberg(p, q=q)
    q_values = bh_q_values(p)
    assert [qv <= q for qv in q_values] == discovery


def test_q_value_boolean_agreement_holds_across_many_vectors_and_thresholds():
    """Broader sweep for the same equivalence claim, not just one vector."""
    import random

    rng = random.Random(20260709)
    for _ in range(200):
        n = rng.randint(1, 15)
        p = [round(rng.random(), 3) for _ in range(n)]
        q_values = bh_q_values(p)
        for q_threshold in (0.01, 0.05, 0.10, 0.20):
            discovery = benjamini_hochberg(p, q=q_threshold)
            assert [qv <= q_threshold for qv in q_values] == discovery, (p, q_threshold)


def test_bh_preserves_input_order_not_sorted_order():
    unsorted_p = [0.90, 0.01, 0.50]
    discovery = benjamini_hochberg(unsorted_p, q=0.10)
    # p=0.01 (index 1) is the smallest -- must be a discovery; the other two
    # (0.90, 0.50) at n=3 cannot be (their own rank thresholds are far below
    # their p-values).
    assert discovery[1] is True
    assert discovery[0] is False
    assert discovery[2] is False


def test_empty_p_values_returns_empty():
    assert benjamini_hochberg([]) == []
    assert bh_q_values([]) == []
    assert bonferroni_significant([]) == []


# ------------------------------------------------------------- Bonferroni
def test_bonferroni_stricter_than_bh_at_same_alpha():
    """A vector where BH-FDR (q=0.10) calls all 4 hypotheses discoveries but
    Bonferroni's much stricter per-hypothesis bar (alpha/n = 0.05/4 =
    0.0125) clears only the smallest -- the two gates are meant to diverge,
    reported alongside each other rather than one replacing the other
    (contract doc Sec 3)."""
    p = [0.001, 0.02, 0.03, 0.04]
    assert benjamini_hochberg(p, q=0.10) == [True, True, True, True]
    assert bonferroni_significant(p, alpha=0.05) == [True, False, False, False]


def test_expected_false_positives():
    assert expected_false_positives(20, alpha=0.05) == 1.0
    assert expected_false_positives(0, alpha=0.05) == 0.0


# --------------------------------------------------------------- verdicts
def _row(prereg_id, p, ci_low, ci_high, trustworthy=True, strong_prior=False):
    return {
        "prereg_id": prereg_id, "one_sided_p_below_zero": p,
        "ci_low": ci_low, "ci_high": ci_high,
        "evidence_status": "ok" if trustworthy else "insufficient_data",
        "strong_prior_pre_documented": strong_prior,
    }


def test_verdict_rejected_when_ci_fully_below_zero():
    out = compute_verdicts([_row("h1", 0.01, ci_low=-1.0, ci_high=-0.1)])
    assert out[0]["verdict"] == "rejected"


def test_verdict_forward_test_candidate_when_ci_above_zero_trustworthy_and_bh_survives():
    out = compute_verdicts([_row("h1", 0.01, ci_low=0.5, ci_high=2.0, trustworthy=True)])
    assert out[0]["verdict"] == "forward-test-candidate"
    assert "BH-FDR" in out[0]["reason"]


def test_verdict_positive_ci_alone_is_not_enough_without_bh_survival():
    """A positive CI that does NOT survive the family-wide FDR gate must NOT
    become a forward-test-candidate -- this is the whole point of the gate."""
    out = compute_verdicts([_row("h1", 0.5, ci_low=0.1, ci_high=2.0, trustworthy=True)])
    assert out[0]["verdict"] == "inconclusive"


def test_verdict_strong_prior_escape_hatch_on_inconclusive_evidence():
    out = compute_verdicts([
        _row("h1", 0.5, ci_low=-0.5, ci_high=1.0, trustworthy=False, strong_prior=True),
    ])
    assert out[0]["verdict"] == "forward-test-candidate"
    assert "prior" in out[0]["reason"]


def test_verdict_inconclusive_is_the_default():
    out = compute_verdicts([_row("h1", 0.5, ci_low=-0.5, ci_high=1.0, trustworthy=False)])
    assert out[0]["verdict"] == "inconclusive"


def test_verdict_rejected_wins_over_strong_prior():
    """A clearly-negative CI is rejected even with a strong prior -- priors
    can argue for testing, never for keeping a result the data rejects
    (contract doc Sec 2)."""
    out = compute_verdicts([
        _row("h1", 0.01, ci_low=-2.0, ci_high=-0.5, strong_prior=True),
    ])
    assert out[0]["verdict"] == "rejected"


def test_compute_verdicts_empty_family():
    assert compute_verdicts([]) == []


def test_verdict_p_equals_zero_is_the_strongest_result_not_the_weakest():
    """Correctness-audit regression: `r.get(...) or 1.0` silently rewrote a
    LEGITIMATE p=0.0 (zero resample means fell at/below zero -- the
    strongest possible positive edge, a routine bootstrap output) into 1.0
    ("certainly null"), exactly inverting the mechanism PORT-1 exists to
    implement. Must be a forward-test-candidate with q_value=0.0, not
    inconclusive with q_value=1.0."""
    out = compute_verdicts([_row("h1", 0.0, ci_low=0.5, ci_high=2.0, trustworthy=True)])
    assert out[0]["verdict"] == "forward-test-candidate"
    assert out[0]["q_value"] == 0.0


def test_verdict_p_equals_zero_does_not_contaminate_the_rest_of_the_family():
    """The same bug also poisoned every OTHER hypothesis's q-value via the
    shared running minimum, since the corrupted p=1.0 was sorted into the
    family alongside real p-values."""
    family = [
        _row("a", 0.0, ci_low=0.1, ci_high=1.0, trustworthy=True),
        _row("b", 0.5, ci_low=-1.0, ci_high=1.0, trustworthy=True),
        _row("c", 0.5, ci_low=-1.0, ci_high=1.0, trustworthy=True),
    ]
    by_id = {r["prereg_id"]: r for r in compute_verdicts(family)}
    assert by_id["a"]["q_value"] == 0.0
    assert by_id["b"]["q_value"] == 0.5
    assert by_id["c"]["q_value"] == 0.5


# -------------------------------------------------------- family stability
def test_two_renders_of_the_same_evaluated_set_give_identical_verdicts():
    """PORT-1 spec's own required test: the verdict/q-value function is
    pure and deterministic over its input family -- calling it twice with
    the exact same evaluated set must yield byte-identical output."""
    family = [
        _row("h1", 0.01, ci_low=0.5, ci_high=2.0, trustworthy=True),
        _row("h2", 0.5, ci_low=-0.5, ci_high=1.0, trustworthy=False),
        _row("h3", 0.9, ci_low=-2.0, ci_high=-0.5, trustworthy=True),
    ]
    out1 = compute_verdicts(family)
    out2 = compute_verdicts(family)
    assert out1 == out2


def test_verdict_can_be_demoted_as_the_family_grows():
    """The mechanism working as intended (contract doc Sec 4): a hypothesis
    that looked like a discovery at N=1 can correctly lose that status once
    more hypotheses are evaluated and the family-wide correction tightens."""
    lone = [_row("h1", 0.04, ci_low=0.1, ci_high=1.0, trustworthy=True)]
    assert compute_verdicts(lone)[0]["verdict"] == "forward-test-candidate"

    # Add 19 more hypotheses with large p-values -- h1's raw p=0.04 no longer
    # clears its own rank's BH threshold once N grows to 20.
    grown = lone + [_row(f"h{i}", 0.9, ci_low=-1.0, ci_high=1.0) for i in range(2, 21)]
    grown_out = {r["prereg_id"]: r for r in compute_verdicts(grown)}
    assert grown_out["h1"]["verdict"] == "inconclusive"


# ------------------------------------------------- survivorship denominator
def test_preregistration_family_summary_counts_full_family():
    rows = [
        {"evaluated_at_utc": "2026-01-01T00:00:00+00:00", "operator_approved_for_forward_test": 1},
        {"evaluated_at_utc": "2026-01-02T00:00:00+00:00", "operator_approved_for_forward_test": 0},
        {"evaluated_at_utc": None, "operator_approved_for_forward_test": 0},
    ]
    out = preregistration_family_summary(rows)
    assert out == {"hypotheses_registered": 3, "hypotheses_tested": 2, "promoted": 1}


def test_preregistration_family_summary_empty_registry():
    assert preregistration_family_summary([]) == {
        "hypotheses_registered": 0, "hypotheses_tested": 0, "promoted": 0,
    }
