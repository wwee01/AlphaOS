"""PR12: per-hypothesis metric queries.

Each `h_xxx_1_rows(journal)` returns `(rows, value_key)` shaped for
`alphaos.stats.preregistration.evaluate_hypothesis()` -- every row carries
`symbol`/`decision_date`/`max_holding_days` (the last degrades to 0 via
effective_n()'s own graceful-missing handling when no trade_proposals row
exists for that candidate) plus the named `value_key` column.

DESIGN NOTE (a reversible decision, logged in HANDOVER.md): most of these
hypotheses are naturally TWO-ARM comparisons (top vs bottom, catalyst vs
none, divergent vs aligned), but the existing statistical engine
(`evaluate_hypothesis`/`compute_verdicts`) is a ONE-SAMPLE test on whether a
`value_key`'s bootstrap CI reliably excludes zero -- the same machinery
BASELINE's own H-AI-1 already uses. Rather than build a second significance-
testing mechanism ("one truth" law), every two-arm hypothesis here is
reduced to a per-observation delta EXACTLY the way BASELINE reduces "AI path
vs threshold_v1 path" to a single `ai_delta_r` per candidate: freeze one
arm's mean as a fixed reference, then center each of the OTHER arm's own
observations against that fixed reference, producing one delta per row.
This is a real design choice, not the only possible one -- see each
function's own docstring for which arm was frozen as the reference and why.
"""

from __future__ import annotations

from typing import Optional


def _mean(values: list[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _median(values: list[float]) -> Optional[float]:
    vals = sorted(v for v in values if v is not None)
    n = len(vals)
    if n == 0:
        return None
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _quantile(values: list[float], q: float) -> Optional[float]:
    """Nearest-rank quantile -- simple, deterministic, no interpolation
    surprises; adequate for a quartile/decile CUTOFF (not a report-grade
    percentile), matching this module's "reduce to a defensible delta,
    don't over-engineer" posture."""
    vals = sorted(v for v in values if v is not None)
    n = len(vals)
    if n == 0:
        return None
    idx = min(n - 1, max(0, round(q * (n - 1))))
    return vals[idx]


def h_tqs_1_rows(journal) -> tuple[list[dict], str]:
    """Top-vs-bottom TQS quartile, on 3d replay_r. Bottom quartile's mean is
    the frozen reference (TQS is meant to distinguish good from bad setups;
    the bottom arm is the "no signal" baseline); each TOP-quartile row's own
    forward_3d_r is centered against it."""
    rows = journal.query(
        "SELECT t.symbol, t.candidate_id, t.tqs_score, co.forward_3d_r, "
        "co.decision_at_utc, tp.max_holding_days "
        "FROM tqs_scores t "
        "JOIN candidate_outcomes co ON co.candidate_id = t.candidate_id "
        "LEFT JOIN trade_proposals tp ON tp.candidate_id = t.candidate_id "
        "WHERE t.source_type = 'candidate' AND t.is_mock = 0 "
        "AND t.data_quality_status = 'ok' AND co.forward_3d_r IS NOT NULL"
    )
    scores = [r["tqs_score"] for r in rows if r["tqs_score"] is not None]
    if len(scores) < 4:
        return [], "centered_delta"
    bottom_cut = _quantile(scores, 0.25)
    top_cut = _quantile(scores, 0.75)
    bottom_mean = _mean([r["forward_3d_r"] for r in rows if r["tqs_score"] is not None and r["tqs_score"] <= bottom_cut])
    if bottom_mean is None:
        return [], "centered_delta"
    out = []
    for r in rows:
        if r["tqs_score"] is None or r["tqs_score"] < top_cut:
            continue
        out.append({
            "symbol": r["symbol"],
            "decision_date": (r["decision_at_utc"] or "")[:10],
            "max_holding_days": r["max_holding_days"],
            "centered_delta": r["forward_3d_r"] - bottom_mean,
        })
    return out, "centered_delta"


def h_cat_1_rows(journal) -> tuple[list[dict], str]:
    """Catalyst presence vs absence, on replay_r. 'none_found' (actively
    searched, nothing found) is the frozen reference -- 'unavailable'/'error'
    rows are excluded entirely (missing data, not evidence of absence)."""
    rows = journal.query(
        "SELECT cc.symbol, cc.candidate_id, cc.catalyst_status, co.replay_r, "
        "co.decision_at_utc, tp.max_holding_days "
        "FROM candidate_catalysts cc "
        "JOIN candidate_outcomes co ON co.candidate_id = cc.candidate_id "
        "LEFT JOIN trade_proposals tp ON tp.candidate_id = cc.candidate_id "
        "WHERE cc.catalyst_status IN ('confirmed', 'none_found') "
        "AND co.replay_r IS NOT NULL"
    )
    none_found_mean = _mean([r["replay_r"] for r in rows if r["catalyst_status"] == "none_found"])
    if none_found_mean is None:
        return [], "centered_delta"
    out = [
        {
            "symbol": r["symbol"],
            "decision_date": (r["decision_at_utc"] or "")[:10],
            "max_holding_days": r["max_holding_days"],
            "centered_delta": r["replay_r"] - none_found_mean,
        }
        for r in rows if r["catalyst_status"] == "confirmed"
    ]
    return out, "centered_delta"


def h_int_1_rows(journal) -> tuple[list[dict], str]:
    """Interest-score top decile vs the population median, on replay_r --
    the population median (not a bottom-decile mean) is the frozen
    reference, matching the spec's own literal "top decile > median"
    wording."""
    rows = journal.query(
        "SELECT c.symbol, c.candidate_id, c.interest_score, co.replay_r, "
        "co.decision_at_utc, tp.max_holding_days "
        "FROM candidates c "
        "JOIN candidate_outcomes co ON co.candidate_id = c.candidate_id "
        "LEFT JOIN trade_proposals tp ON tp.candidate_id = c.candidate_id "
        "WHERE c.interest_score IS NOT NULL AND co.replay_r IS NOT NULL"
    )
    scores = [r["interest_score"] for r in rows]
    if len(scores) < 10:
        return [], "centered_delta"
    top_cut = _quantile(scores, 0.90)
    population_median = _median([r["replay_r"] for r in rows])
    if population_median is None:
        return [], "centered_delta"
    out = [
        {
            "symbol": r["symbol"],
            "decision_date": (r["decision_at_utc"] or "")[:10],
            "max_holding_days": r["max_holding_days"],
            "centered_delta": r["replay_r"] - population_median,
        }
        for r in rows if r["interest_score"] >= top_cut
    ]
    return out, "centered_delta"


def h_win_1_rows(journal) -> tuple[list[dict], str]:
    """Morning (SGT hour < 12) vs afternoon (>= 12) regular-session scan
    windows, on replay_r. Afternoon is the frozen reference (the rel_volume
    formula's own denominator is a full-PRIOR-day baseline, structurally
    least distorted late in the session) -- each morning row's replay_r is
    centered against it."""
    rows = journal.query(
        "SELECT c.symbol, c.candidate_id, sb.started_at_sgt, co.replay_r, "
        "co.decision_at_utc, tp.max_holding_days "
        "FROM candidates c "
        "JOIN scan_batches sb ON sb.scan_batch_id = c.scan_batch_id "
        "JOIN candidate_outcomes co ON co.candidate_id = c.candidate_id "
        "LEFT JOIN trade_proposals tp ON tp.candidate_id = c.candidate_id "
        "WHERE sb.market_session = 'regular' AND co.replay_r IS NOT NULL "
        "AND sb.started_at_sgt IS NOT NULL"
    )
    def _hour(r):
        try:
            return int(r["started_at_sgt"][11:13])
        except (TypeError, ValueError, IndexError):
            return None

    afternoon_mean = _mean([r["replay_r"] for r in rows if (_hour(r) or 0) >= 12])
    if afternoon_mean is None:
        return [], "centered_delta"
    out = []
    for r in rows:
        hh = _hour(r)
        if hh is None or hh >= 12:
            continue
        out.append({
            "symbol": r["symbol"],
            "decision_date": (r["decision_at_utc"] or "")[:10],
            "max_holding_days": r["max_holding_days"],
            "centered_delta": r["replay_r"] - afternoon_mean,
        })
    return out, "centered_delta"


def h_ttl_1_rows(journal) -> tuple[list[dict], str]:
    """Expired proposals' counterfactual replay_r vs approved-and-beyond
    proposals' OWN replay_r (both measured via the SAME counterfactual
    field -- candidate_outcomes.replay_r -- rather than mixing a
    counterfactual for expired against a realized fill price for approved,
    which would not be apples-to-apples). Approved-and-beyond's mean is the
    frozen reference; each EXPIRED candidate's replay_r is centered against
    it. A reliably NEGATIVE centered delta here (expired underperforms
    approved) argues FOR "approval adds value"; a delta indistinguishable
    from zero argues AGAINST it (approval isn't the bottleneck) -- per the
    hypothesis's own "either direction is informative" framing, this
    function does not pick a side, it only produces the comparable rows."""
    rows = journal.query(
        "SELECT tp.status, co.symbol, co.candidate_id, co.replay_r, "
        "co.decision_at_utc, tp.max_holding_days "
        "FROM trade_proposals tp "
        "JOIN candidate_outcomes co ON co.candidate_id = tp.candidate_id "
        "WHERE tp.status IN ('expired', 'approved', 'submitted', 'filled') "
        "AND co.replay_r IS NOT NULL"
    )
    approved_mean = _mean([r["replay_r"] for r in rows if r["status"] in ("approved", "submitted", "filled")])
    if approved_mean is None:
        return [], "centered_delta"
    out = [
        {
            "symbol": r["symbol"],
            "decision_date": (r["decision_at_utc"] or "")[:10],
            "max_holding_days": r["max_holding_days"],
            "centered_delta": r["replay_r"] - approved_mean,
        }
        for r in rows if r["status"] == "expired"
    ]
    return out, "centered_delta"


def h_rej_1_rows(journal) -> tuple[list[dict], str]:
    """Operator rejections' own already-computed delta_r -- maps directly
    onto the existing attribution machinery, no centering needed (this is
    exactly the same shape as BASELINE's own H-AI-1, just a different
    attribution_type slice)."""
    rows = journal.query(
        "SELECT symbol, delta_r, decision_at_utc "
        "FROM attribution_records "
        "WHERE attribution_type = 'propose_user_rejected' "
        "AND resolved_status = 'resolved' AND is_mock = 0 "
        "AND delta_r IS NOT NULL"
    )
    out = [
        {
            "symbol": r["symbol"],
            "decision_date": (r["decision_at_utc"] or "")[:10],
            "max_holding_days": None,  # attribution_records carries no holding-window field (effective_n degrades to same-day)
            "delta_r": r["delta_r"],
        }
        for r in rows
    ]
    return out, "delta_r"


def h_pol_1_rows(journal) -> tuple[list[dict], str]:
    """Polarity divergence vs alignment, on replay_r. 'aligned' is the
    frozen reference; each 'divergent' row's replay_r is centered against
    it. NULL/unknown alignment rows are excluded (not the same as
    divergent)."""
    rows = journal.query(
        "SELECT p.symbol, p.candidate_id, p.direction_alignment, co.replay_r, "
        "co.decision_at_utc, tp.max_holding_days "
        "FROM last30days_polarity p "
        "JOIN candidate_outcomes co ON co.candidate_id = p.candidate_id "
        "LEFT JOIN trade_proposals tp ON tp.candidate_id = p.candidate_id "
        "WHERE p.direction_alignment IN ('aligned', 'divergent') "
        "AND co.replay_r IS NOT NULL"
    )
    aligned_mean = _mean([r["replay_r"] for r in rows if r["direction_alignment"] == "aligned"])
    if aligned_mean is None:
        return [], "centered_delta"
    out = [
        {
            "symbol": r["symbol"],
            "decision_date": (r["decision_at_utc"] or "")[:10],
            "max_holding_days": r["max_holding_days"],
            "centered_delta": r["replay_r"] - aligned_mean,
        }
        for r in rows if r["direction_alignment"] == "divergent"
    ]
    return out, "centered_delta"


# Dispatch table -- registry.py looks up by SEEDED_HYPOTHESES' own
# "metric_fn_name" string rather than importing each function by name, so
# adding a 9th hypothesis later never requires touching the dispatch site.
METRIC_FUNCTIONS = {
    "h_tqs_1_rows": h_tqs_1_rows,
    "h_cat_1_rows": h_cat_1_rows,
    "h_int_1_rows": h_int_1_rows,
    "h_win_1_rows": h_win_1_rows,
    "h_ttl_1_rows": h_ttl_1_rows,
    "h_rej_1_rows": h_rej_1_rows,
    "h_pol_1_rows": h_pol_1_rows,
}
