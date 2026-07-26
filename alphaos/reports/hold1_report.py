"""HOLD-1: 10-trading-day shadow outcome horizon -- pre-registered report
(docs/roadmap/alphaos-hold1-10day-shadow-horizon-spec.md, drafted 2026-07-24).

Answers the pre-registered question, stated BEFORE any data was examined
(see the spec's own "Why" section + master reference Sec 9's 2026-07-24
decision row): what fraction of quality setups reach the 2.4xATR favorable-
excursion distance in days 6-10, having failed to reach it by day 5?

Pure read of ``candidate_outcomes``' additive 10d family (populated by
``alphaos.learning.outcomes_tracker``'s own extension -- see that module's
docstring for the resolution mechanics, including why a row needs its OWN
``outcome_status_10d`` column separate from ``outcome_status``). This report
NEVER writes, and is never read by any scan/eval/labeller/risk/execution
path -- same zero-decision-surface law as every other report in this
package (see test_hold1_10day_shadow.py's own structural proof, mirroring
tests/test_pr12_hypotheses.py::test_risk_engine_and_approval_never_
reference_hypotheses_at_all).

Purely descriptive until the pre-registered revisit floor (>=30 independent
clusters, PORT-1's own effective_n() law) is cleared -- see REVISIT_FLOOR
below. Never a significance claim regardless of sample size.
"""

from __future__ import annotations

from typing import Optional

from alphaos.stats.effective_n import effective_n as _effective_n
from alphaos.util import timeutils

# The pre-registered target distance (2.4x ATR) and the "halfway" context
# threshold -- both named explicitly in the spec's own Design Sec 3.
TARGET_R = 2.4
HALFWAY_R = 1.2

# Spec's own pre-registered revisit condition: >=30 resolved 10-day
# observations, INDEPENDENT-CLUSTER counting per PORT-1's effective_n() law
# -- not a raw row count (see master reference Sec 9's 2026-07-24 row).
REVISIT_FLOOR = 30

CAVEAT = (
    "pre-registered 2026-07-24; descriptive until n>=30 independent clusters; "
    "no significance claimed."
)

# Spec's own cohort naming ("proposed / watch / rejected") mapped onto
# candidate_outcomes.candidate_type. 'proposed' folds in 'blocked' (a
# proposal AlphaOS itself blocked pre-execution is still a proposed setup,
# same population framing outcomes_summary.py's own by-type breakdown
# uses). Bare 'candidate' rows (never acted on either way) and
# 'user_override' rows (a different, human-decision population) are
# deliberately excluded -- not one of the spec's 3 named cohorts.
_COHORTS: dict[str, tuple] = {
    "proposed": ("proposal", "blocked"),
    "watch": ("armed_watch",),
    "rejected": ("reject",),
}


def _cohort_of(candidate_type: Optional[str]) -> Optional[str]:
    for cohort, types in _COHORTS.items():
        if candidate_type in types:
            return cohort
    return None


def _resolved_5d_and_10d_rows(journal) -> list[dict]:
    """Rows where BOTH the 5d family (outcome_status='complete') and the 10d
    family (outcome_status_10d='complete') have genuinely resolved -- the
    spec's own precondition for the pre-registered question. Joins
    trade_proposals for max_holding_days, shaping rows for effective_n()
    exactly like cards/scoreboard.py's own _card_replay_r_rows()."""
    return journal.query(
        "SELECT co.symbol, co.candidate_type, co.decision_at_utc, "
        "co.max_favorable_5d_r, co.max_favorable_10d_r, tp.max_holding_days "
        "FROM candidate_outcomes co "
        "LEFT JOIN trade_proposals tp ON tp.id = ("
        "  SELECT tp2.id FROM trade_proposals tp2 "
        "  WHERE tp2.candidate_id = co.candidate_id ORDER BY tp2.id DESC LIMIT 1"
        ") "
        "WHERE co.outcome_status = 'complete' AND co.outcome_status_10d = 'complete' "
        "AND co.max_favorable_5d_r IS NOT NULL AND co.max_favorable_10d_r IS NOT NULL"
    )


def _failed_by_day5(rows: list[dict], cohort: Optional[str] = None) -> list[dict]:
    """Rows that FAILED to reach TARGET_R by day 5 (the spec's own
    denominator), optionally restricted to one named cohort."""
    return [
        r for r in rows
        if (cohort is None or _cohort_of(r.get("candidate_type")) == cohort)
        and r.get("max_favorable_5d_r") is not None
        and r["max_favorable_5d_r"] < TARGET_R
    ]


def _cohort_stats(rows: list[dict], threshold: float) -> dict:
    """Among rows that FAILED to reach TARGET_R by day 5, per cohort: count
    and fraction that reached ``threshold`` by day 10."""
    out = {}
    for cohort in _COHORTS:
        failed_by_day5 = _failed_by_day5(rows, cohort)
        reached = [r for r in failed_by_day5 if r["max_favorable_10d_r"] >= threshold]
        n = len(failed_by_day5)
        out[cohort] = {
            "n_failed_by_day5": n,
            "n_reached_days6_10": len(reached),
            "fraction": round(len(reached) / n, 4) if n else None,
        }
    return out


def _pre_registered_population(rows: list[dict]) -> list[dict]:
    """The pre-registered question's OWN population: rows in one of the 3
    reported cohorts (proposed/watch/rejected) that failed to reach TARGET_R
    by day 5 -- exactly the union, across cohorts, of what ``_cohort_stats``
    already uses as each cohort's own denominator. This is what the revisit
    floor/``effective_n``/digest line must count.

    Audit fixup (2026-07-25, Auditor A MEDIUM): the floor previously counted
    EVERY resolved row regardless of population -- bare 'candidate' rows
    (created for every scanned name, never acted on either way),
    'user_override' rows (a different, human-decision population), and rows
    that already cleared 2.4R by day 5 (not "failed by day 5" at all, so
    outside the pre-registered question entirely). In production,
    'candidate' rows dominate the ledger, so the unscoped count could
    report "floor cleared" while the cohorts the operator's ruling actually
    depends on still held a handful of real observations -- a premature-
    ruling risk on exactly the decision this instrument exists to gate.
    Corrected here, one day after the 2026-07-24 registration and before
    ANY real 10d observation had resolved -- an implementation-matches-
    registration correction, not a post-hoc analysis change (see the
    spec's own dated clarification)."""
    return _failed_by_day5(
        [r for r in rows if _cohort_of(r.get("candidate_type")) is not None]
    )


def compute_hold1_report(journal) -> dict:
    """Pure aggregation. Never raises on an empty/early-stage ledger --
    zero resolved rows is an expected, honest state (this is a NEW measure
    with a multi-week accumulation horizon), not an error."""
    rows = _resolved_5d_and_10d_rows(journal)
    population = _pre_registered_population(rows)
    en = _effective_n([
        {**r, "decision_date": (r.get("decision_at_utc") or "")[:10]} for r in population
    ])
    return {
        "as_of": timeutils.market_date().isoformat(),
        # UNSCOPED diagnostic only -- every candidate_type, including bare
        # 'candidate'/'user_override' rows and rows that already cleared
        # 2.4R by day 5. NOT what the revisit floor gates on -- see
        # n_pre_registered_population/effective_n below.
        "n_resolved_both_families_unscoped": len(rows),
        # The pre-registered question's own population (proposed/watch/
        # rejected cohorts, failed-by-day5 only) -- this is what
        # effective_n/revisit_condition_met/the digest line count.
        "n_pre_registered_population": len(population),
        "effective_n": en["effective_n"],
        "revisit_floor": REVISIT_FLOOR,
        "revisit_condition_met": en["effective_n"] >= REVISIT_FLOOR,
        "by_cohort_target_2_4r": _cohort_stats(rows, TARGET_R),
        "by_cohort_halfway_1_2r": _cohort_stats(rows, HALFWAY_R),
        "caveat": CAVEAT,
    }


def hold1_digest_line(rep: dict) -> str:
    """The exact accumulation line the daily brief surfaces (Design Sec 4) --
    counted in independent clusters OF THE PRE-REGISTERED POPULATION
    (proposed/watch/rejected cohorts, failed-by-day5 only -- see
    ``_pre_registered_population``), matching the revisit floor's own units.
    NOT the unscoped raw resolved-row count (also reported, separately, and
    clearly labeled as unscoped, in the fuller markdown section below)."""
    return f"HOLD-1: {rep['effective_n']}/{rep['revisit_floor']} resolved 10d observations"


def _render_cohort_block(title: str, threshold_label: str, by_cohort: dict) -> list[str]:
    lines = [f"### {title}"]
    for cohort, s in by_cohort.items():
        if s["n_failed_by_day5"] == 0:
            lines.append(f"- {cohort}: (no resolved observations yet)")
        else:
            lines.append(
                f"- {cohort}: {s['n_reached_days6_10']}/{s['n_failed_by_day5']} "
                f"({s['fraction'] * 100:.1f}%) reached >={threshold_label} by day 10"
            )
    return lines


def render_markdown(rep: dict) -> str:
    lines = [
        "## HOLD-1: 10-day shadow outcome horizon (pre-registered)",
        f"- {hold1_digest_line(rep)} "
        f"(diagnostic, UNSCOPED -- all candidate types incl. non-cohort/already-cleared rows: "
        f"{rep['n_resolved_both_families_unscoped']} raw resolved rows)",
        "",
    ]
    lines += _render_cohort_block(
        "Day 6-10 completion, among setups that hadn't reached 2.4xATR by day 5",
        f"{TARGET_R}R", rep["by_cohort_target_2_4r"],
    )
    lines += ["", ]
    lines += _render_cohort_block(
        "Same fraction at halfway (context only)",
        f"{HALFWAY_R}R", rep["by_cohort_halfway_1_2r"],
    )
    lines += ["", f"> {rep['caveat']}"]
    return "\n".join(lines)
