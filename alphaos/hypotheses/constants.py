"""PR12: risk classes, the frozen floor table, and the 8 seeded hypotheses.

Fable5 consult (2026-07-10, logged in HANDOVER.md): risk_class gates
`min_sample`/`min_span_days` ONLY -- `success_floor` stays per-hypothesis
(a single class-keyed scalar effect floor is meaningless across
heterogeneous metrics: quartile spreads, equivalence tests, <=0 tests).
`success_floor` is DOCUMENTATION for a human reader, not a mechanical gate:
the actual statistical verdict (met/failed/inconclusive) comes entirely from
`alphaos.stats.fdr.compute_verdicts()` testing whether the metric's CI
reliably excludes zero -- the SAME generic zero-anchored test BASELINE's own
H-AI-1 already uses (its own "+0.05R" claim isn't mechanically checked
either). This is deliberate: "one statistical engine, one truth" -- PR12
does not invent a second significance-testing mechanism keyed to an
arbitrary per-hypothesis magnitude.
"""

from __future__ import annotations

from alphaos.constants import StrEnum


class RiskClass(StrEnum):
    """Master build plan PD#9: A = parameter change within pre-declared
    bounds (auto-testable in shadow immediately); B = new card/filter/
    evidence source (paper-testable after human acknowledgment); C =
    structural -- sizing formula, leverage, shorts, new asset class, any
    gate/autonomy change (NightDesk research + human approval, mandatory,
    no exceptions)."""

    A = "A"
    B = "B"
    C = "C"


class HypothesisStatus(StrEnum):
    """Mechanical lifecycle only -- NOT a semantic verdict. PORT-1's generic
    rejected/forward-test-candidate/inconclusive vocabulary describes which
    side of zero a CI reliably lands on; it does not by itself say whether
    that outcome CONFIRMS or REFUTES a given hypothesis's own claim, because
    the seeded set mixes positive-claimed (H-TQS-1/H-CAT-1/H-INT-1),
    negative-claimed (H-POL-1), either-direction-informative (H-WIN-1,
    H-TTL-1), and inverted-good-news (H-REJ-1: a 'rejected' verdict there is
    the CONFIRMING case) hypotheses. Auto-mapping verdict -> MET/FAILED would
    require a per-hypothesis direction flag and risks a silent sign error
    (reversible decision, see HANDOVER.md) -- so the resolver only ever
    drives PROPOSED -> TESTING -> RESOLVED; MET/FAILED/WITHDRAWN are
    reserved for an operator reading the rendered report + claim text
    together, never set by any automated path in v0.
    """

    PROPOSED = "proposed"
    TESTING = "testing"
    RESOLVED = "resolved"
    MET = "met"
    FAILED = "failed"
    WITHDRAWN = "withdrawn"


# Fable5 consult (2026-07-10): the frozen risk-class floor table. 30/28 is a
# FLOOR, not a dial -- nothing gating promotion goes below Class A's own
# number (matches attribution.py's existing MIN_RESOLVED_FOR_V2_AGGREGATE=30/
# MIN_SPAN_DAYS_FOR_V2_AGGREGATE=28, and BASELINE's own H-AI-1 preregistration
# -- one-floor law). Class B (new evidence sources carry search-space risk)
# and Class C (structural / autonomy-adjacent -- the ruin-risk axis) step up
# from there; Class C's 90-day span is a ticket to REQUEST NightDesk review
# as "met," never evidence for autonomy on its own.
RISK_CLASS_FLOORS: dict[str, dict] = {
    RiskClass.A.value: {"min_sample": 30, "min_span_days": 28.0},
    RiskClass.B.value: {"min_sample": 40, "min_span_days": 42.0},
    RiskClass.C.value: {"min_sample": 60, "min_span_days": 90.0},
}


# Seeded v1 set (8, frozen at merge -- docs/roadmap/alphaos-pr-implementation-
# specs.md's PR12 section). `success_floor` is the documented claim only (see
# module docstring) -- never a mechanical gate. `metric_fn_name` names the
# query function in alphaos/hypotheses/queries.py that produces this
# hypothesis's `rows`/`value_key` for evaluate_hypothesis(); H-AI-1 has none
# -- it links to BASELINE's own existing preregistration row instead of
# computing anything itself (see registry.py's special-cased handling).
SEEDED_HYPOTHESES: list[dict] = [
    {
        "hypothesis_id": "H-TQS-1",
        "risk_class": RiskClass.B.value,
        "claim": "Top-vs-bottom-quartile TQS predicts a +0.3R difference in "
                 "3-day replay_r -- precondition for TQS ever leaving shadow.",
        "metric": "mean(3d replay_r | top TQS quartile) centered against "
                  "mean(3d replay_r | bottom TQS quartile)",
        "success_floor": 0.3,
        "metric_fn_name": "h_tqs_1_rows",
        "card_id": None,
    },
    {
        "hypothesis_id": "H-CAT-1",
        "risk_class": RiskClass.B.value,
        "claim": "Catalyst presence predicts a +0.2R difference in replay_r "
                 "-- the card family's core thesis; FALSE starts the §12 "
                 "pivot clock.",
        "metric": "mean(replay_r | catalyst_status='confirmed') centered "
                  "against mean(replay_r | catalyst_status='none_found')",
        "success_floor": 0.2,
        "metric_fn_name": "h_cat_1_rows",
        # Correction (2026-07-10, self-caught during PR13 slice 2 research):
        # the "Cards v2-v5" section of the specs doc explicitly says
        # "Promotion gated on H-CAT-1 resolving TRUE" for v3
        # catalyst_continuation_pullback_v1 (a not-yet-built shadow card) --
        # NOT catalyst_momentum_v2 (the existing, already-live default
        # card). This field is documentation-only in PR12 itself (nothing
        # here acts on it), but PR13 slice 2 will read it to decide which
        # card to promote, so the wrong value would have silently pointed
        # promotion at an unrelated, already-live card.
        "card_id": "catalyst_continuation_pullback_v1",
    },
    {
        "hypothesis_id": "H-INT-1",
        "risk_class": RiskClass.B.value,
        "claim": "Interest-score top decile outperforms the median -- if "
                 "FALSE, the scanner ranking EXP-1 multiplies is noise.",
        "metric": "replay_r (top interest_score decile) centered against "
                  "the population median replay_r",
        "success_floor": 0.0,  # any reliably-positive centered delta counts; the q<0.10 clause is BH-FDR discovery itself
        "metric_fn_name": "h_int_1_rows",
        "card_id": None,
    },
    {
        "hypothesis_id": "H-WIN-1",
        "risk_class": RiskClass.A.value,
        "claim": "Morning vs afternoon scan windows show a real rel_volume-"
                 "driven performance difference -- the rel_volume audit on "
                 "our own ledger.",
        "metric": "replay_r (morning-window candidates, market_session="
                  "'regular', started_at_sgt HH<12) centered against mean "
                  "replay_r (afternoon-window candidates, HH>=12)",
        "success_floor": 0.0,
        "metric_fn_name": "h_win_1_rows",
        "card_id": None,
    },
    {
        "hypothesis_id": "H-TTL-1",
        "risk_class": RiskClass.C.value,
        "claim": "Expired proposals' counterfactual replay_r is approximately "
                 "equal to approved-and-executed proposals' realized_r -- "
                 "TRUE is the evidence case for L3; FALSE kills the "
                 "'approval is the bottleneck' narrative.",
        "metric": "expired-proposal replay_r centered against mean "
                  "approved+executed realized_r (a non-zero CI in EITHER "
                  "direction is informative; see registry.py's framing note)",
        "success_floor": 0.0,
        "metric_fn_name": "h_ttl_1_rows",
        "card_id": None,
    },
    {
        "hypothesis_id": "H-REJ-1",
        "risk_class": RiskClass.C.value,
        "claim": "Operator rejections' foregone ΔR is <= 0 -- either verdict "
                 "direction is gold (validates operator judgment either way).",
        "metric": "attribution_records.delta_r WHERE attribution_type="
                  "'propose_user_rejected'",
        "success_floor": 0.0,
        "metric_fn_name": "h_rej_1_rows",
        "card_id": None,
    },
    {
        "hypothesis_id": "H-POL-1",
        "risk_class": RiskClass.B.value,
        "claim": "Polarity divergence underperforms aligned narratives -- "
                 "gates card v5 (polarity_divergence_reclaim_v1).",
        "metric": "replay_r (direction_alignment='divergent') centered "
                  "against mean replay_r (direction_alignment='aligned')",
        "success_floor": 0.0,
        "metric_fn_name": "h_pol_1_rows",
        "card_id": "polarity_divergence_reclaim_v1",
    },
    {
        "hypothesis_id": "H-AI-1",
        "risk_class": RiskClass.C.value,
        "claim": "AI adds >= +0.05R mean paired ΔR over threshold_v1 on "
                 "proposed candidates, conditional on labeller reach "
                 "(verbatim BASELINE preregistration).",
        "metric": None,  # links to BASELINE's own preregistrations row; never re-evaluated here
        "success_floor": 0.05,
        "metric_fn_name": None,
        "card_id": None,
    },
]
