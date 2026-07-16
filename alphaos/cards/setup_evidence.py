"""EVID-1: per-setup-version evidence report -- generalizes PR13's
``scoreboard.py`` machinery (``effective_n``/``clustered_bootstrap``, never a
second implementation) beyond live_eligible/demotion into a broader, multi-
population, multi-horizon review with BH-FDR across the full setup family.

PURE READ. Descriptive/diagnostic only, per the Edge Lab audit's own law
(docs/roadmap/edge-lab-stage1-audit.md Stage 7): may propose hypotheses,
never automatically changes thresholds, setup logic, production config,
position sizing, or approval behavior -- exactly the same posture
``scoreboard.py``/``attribution.py``/``relative_performance.py`` already
hold.

Deliberately reuses ``alphaos.stats.fdr``'s RAW primitives (``bh_q_values``,
``benjamini_hochberg``) directly, NOT ``compute_verdicts()`` -- that
function is PR12's own formal, one-shot preregistration-only verdict
machinery (its own module docstring: "the family is always every evaluated
preregistration, full stop"). A setup card's evidence review is the
opposite shape, same as PR13's own scoreboard: an OPERATIONAL,
always-fresh, re-evaluated-daily diagnostic, never a frozen, cited
hypothesis test.
"""

from __future__ import annotations

from alphaos.reports.attribution import (
    MIN_RESOLVED_FOR_V2_AGGREGATE,
    MIN_SPAN_DAYS_FOR_V2_AGGREGATE,
)
from alphaos.stats.bootstrap import clustered_bootstrap
from alphaos.stats.effective_n import effective_n
from alphaos.stats.fdr import DEFAULT_FDR_Q, benjamini_hochberg, bh_q_values

# Reused verbatim from attribution.py -- the SAME bar scoreboard.py's own
# demotion floor already reuses, for the identical reason (a raw sample this
# small makes a bootstrap CI noise, not signal).
MIN_RESOLVED = MIN_RESOLVED_FOR_V2_AGGREGATE
MIN_SPAN_DAYS = MIN_SPAN_DAYS_FOR_V2_AGGREGATE

# Every population a candidate_outcomes row can carry (mirrors
# outcomes_tracker._ALPHAOS_SIDE_TYPES). Unlike scoreboard.py's demotion-
# scoped query (which only counts 'proposal'/'blocked' -- rows the card
# actually acted on), this report can compare across ALL of them, answering
# the Edge Lab audit's own "did rejected candidates outperform approved
# ones?" question (Stage 7).
ALL_CANDIDATE_TYPES = ("proposal", "blocked", "armed_watch", "reject", "candidate")

# Metrics this report knows how to bootstrap -- every one is a
# candidate_outcomes column that's already directionally signed (positive =
# favorable), so a one-sided "is the mean above zero" test means the same
# thing for each of them that it does for replay_r.
METRICS = (
    "replay_r",
    "market_adjusted_return_1d_pct",
    "market_adjusted_return_3d_pct",
    "market_adjusted_return_5d_pct",
)


def all_registered_setups(journal) -> list[dict]:
    """Every distinct (card_id, card_version) ever registered -- unlike
    scoreboard.py's ``live_eligible_cards()``, this includes shadow/demoted
    versions too. An Edge Lab evidence review must show the FULL family
    (PORT-1's own survivorship-denominator law: "any report claiming
    system-level edge must print the full family, never just a live
    subset"), not just currently-live cards."""
    return journal.query(
        "SELECT DISTINCT card_id, version AS card_version FROM setup_cards "
        "ORDER BY card_id, version"
    )


def _rows_for_setup(journal, card_id: str, card_version: int, candidate_types: tuple) -> list[dict]:
    """One row per candidate this (card_id, card_version) produced a
    candidates row for, restricted to the given population(s), shaped for
    effective_n()/clustered_bootstrap(). Dedupes candidate_outcomes to the
    single most recent non-user_override row per candidate_id -- the SAME
    fan-out fix scoreboard.py's own ``_card_replay_r_rows()`` applies (a
    human-overridden candidate carries a second, parallel row that must
    never be double-counted here either)."""
    placeholders = ",".join("?" for _ in candidate_types)
    return journal.query(
        "SELECT c.symbol, co.decision_at_utc, co.replay_r, "
        "co.market_adjusted_return_1d_pct, co.market_adjusted_return_3d_pct, "
        "co.market_adjusted_return_5d_pct, tp.max_holding_days "
        "FROM candidates c "
        "JOIN candidate_outcomes co ON co.id = ("
        "  SELECT co2.id FROM candidate_outcomes co2 "
        "  WHERE co2.candidate_id = c.candidate_id AND co2.candidate_type != 'user_override' "
        "  ORDER BY co2.id DESC LIMIT 1"
        ") "
        "LEFT JOIN trade_proposals tp ON tp.id = ("
        "  SELECT tp2.id FROM trade_proposals tp2 "
        "  WHERE tp2.candidate_id = c.candidate_id ORDER BY tp2.id DESC LIMIT 1"
        ") "
        f"WHERE c.card_id = ? AND c.card_version = ? AND co.candidate_type IN ({placeholders})",
        (card_id, card_version, *candidate_types),
    )


def compute_setup_metric_stats(
    journal, card_id: str, card_version: int, metric_key: str,
    candidate_types: tuple = ("proposal", "blocked"),
) -> dict:
    """One setup-version's evidence for ONE metric over ONE population,
    computed fresh (mirrors scoreboard.py's ``compute_card_scoreboard()``
    exactly, generalized to any metric/population instead of hardcoding
    replay_r/proposal+blocked).

    Audit-fixup (correctness HIGH): rows missing ``metric_key`` (e.g. a
    ``market_adjusted_return_5d_pct`` never resolved because the row was
    already ``outcome_status='complete'`` before this metric existed, or the
    benchmark side hadn't caught up -- see outcomes_tracker's own gating)
    are filtered out BEFORE ``effective_n()``, mirroring scoreboard.py's own
    ``AND co.replay_r IS NOT NULL`` filter. Without this, ``effective_n``/
    ``clears_floor`` were computed over every row regardless of whether it
    actually carried this metric, while ``clustered_bootstrap`` silently
    dropped the nulls -- a setup could "clear the floor" on 35 rows and 32
    of them null, then have its 3 real observations admitted straight into
    BH-FDR as if they were a trustworthy 35-observation sample."""
    if metric_key not in METRICS:
        raise ValueError(f"unknown metric {metric_key!r}, expected one of {METRICS}")
    rows = [r for r in _rows_for_setup(journal, card_id, card_version, candidate_types)
            if r.get(metric_key) is not None]
    en = effective_n(
        [{**r, "decision_date": (r["decision_at_utc"] or "")[:10]} for r in rows],
    )
    boot = clustered_bootstrap(en["clusters"], metric_key)
    clears_floor = en["effective_n"] >= MIN_RESOLVED and (en["span_days"] or 0) >= MIN_SPAN_DAYS
    return {
        "card_id": card_id,
        "card_version": card_version,
        "metric": metric_key,
        "candidate_types": list(candidate_types),
        "point_estimate": boot["point_estimate"],
        "ci_low": boot["ci_low"],
        "ci_high": boot["ci_high"],
        "one_sided_p_below_zero": boot["one_sided_p_below_zero"],
        "effective_n": en["effective_n"],
        "n_raw": en["n_raw"],
        "span_days": en["span_days"],
        "clears_floor": clears_floor,
    }


def population_breakdown(journal, card_id: str, card_version: int, metric_key: str) -> dict:
    """The Edge Lab audit's own Stage-7 question, answered directly: how
    does this setup's metric compare across EVERY population it touched
    (acted on vs rejected vs merely watched vs merely detected)? One
    ``compute_setup_metric_stats()`` call per population type, side by side
    -- never a single blended number that would hide which population is
    actually driving a result."""
    return {
        "card_id": card_id,
        "card_version": card_version,
        "metric": metric_key,
        "by_population": {
            ctype: compute_setup_metric_stats(journal, card_id, card_version, metric_key, (ctype,))
            for ctype in ALL_CANDIDATE_TYPES
        },
    }


def build_setup_evidence_report(
    journal, metric_key: str = "market_adjusted_return_5d_pct", fdr_q: float = DEFAULT_FDR_Q,
) -> dict:
    """Every registered setup-version's evidence for ONE metric over the
    'acted on' population (proposal+blocked, matching scoreboard.py's own
    default population), with BH-FDR applied across every setup that clears
    its own floor (an untrustworthy sample's p-value is never even entered
    into the correction -- exactly PR12's own 'trustworthy' precondition in
    ``compute_verdicts``, just enforced locally instead of via that
    PR12-only function). Descriptive only: never writes, never
    gates/promotes/demotes."""
    setups = all_registered_setups(journal)
    stats = [
        compute_setup_metric_stats(journal, s["card_id"], s["card_version"], metric_key)
        for s in setups
    ]
    for s in stats:
        s["q_value"] = None
        s["bh_discovery"] = False
    testable = [s for s in stats if s["clears_floor"] and s["one_sided_p_below_zero"] is not None]
    p_values = [s["one_sided_p_below_zero"] for s in testable]
    q_values = bh_q_values(p_values)
    discoveries = benjamini_hochberg(p_values, q=fdr_q)
    for s, q, disc in zip(testable, q_values, discoveries):
        s["q_value"] = q
        s["bh_discovery"] = disc
    return {
        "metric": metric_key,
        "fdr_q": fdr_q,
        "n_setups_registered": len(setups),
        "n_setups_testable": len(testable),
        "floor_effective_n": MIN_RESOLVED,
        "floor_span_days": MIN_SPAN_DAYS,
        "setups": stats,
    }


def render_markdown(rep: dict) -> str:
    lines = [
        f"## EVID-1 -- setup-version evidence report ({rep['metric']})",
        # Scope/safety-audit LOW: this shares "q="/"discovery" vocabulary with
        # PR12's own compute_verdicts() output, but it is NOT that function --
        # this is an always-fresh operational diagnostic (same posture as
        # scoreboard.py), never a formal promotion/rejection. Said explicitly
        # here rather than only in the module/CLI docstrings, since this is
        # the text an operator actually reads.
        "_Operational diagnostic -- not a PR12 hypothesis verdict. Never "
        "promotes, demotes, or changes production behavior._",
        f"{rep['n_setups_registered']} registered setup-version(s), "
        f"{rep['n_setups_testable']} clear the floor "
        f"({rep['floor_effective_n']}+ effective_n, {rep['floor_span_days']}+ day span) "
        f"and enter BH-FDR at q={rep['fdr_q']}",
        "",
    ]
    for s in rep["setups"]:
        # Audit-fixup (correctness HIGH, belt-and-suspenders): clears_floor
        # alone doesn't guarantee a real point_estimate -- build_setup_
        # evidence_report's own "testable" gate additionally requires
        # one_sided_p_below_zero is not None. Checking point_estimate here
        # too (rather than trusting clears_floor in isolation) means this
        # render can never crash formatting a None with :+.4f even if some
        # future metric/edge-case produced that combination.
        if not s["clears_floor"] or s["point_estimate"] is None:
            lines.append(
                f"- {s['card_id']} v{s['card_version']}: below floor or unresolved -- "
                f"effective_n={s['effective_n']}, span_days={s['span_days']} "
                "(counts only, no expectancy shown)"
            )
            continue
        marker = " -- BH-FDR discovery" if s.get("bh_discovery") else ""
        lines.append(
            f"- {s['card_id']} v{s['card_version']}: {s['point_estimate']:+.4f} "
            f"[{s['ci_low']:+.4f}, {s['ci_high']:+.4f}] q={s['q_value']} "
            f"(effective_n={s['effective_n']}, span={s['span_days']}d){marker}"
        )
    return "\n".join(lines)
