"""PR12: per-hypothesis metric queries.

Each `h_xxx_1_rows(journal)` returns `(rows, value_key, reference_arm_rows)`
shaped for `alphaos.stats.preregistration.evaluate_hypothesis()` -- every row
in `rows` carries `symbol`/`decision_date`/`max_holding_days` (the last
degrades to 0 via effective_n()'s own graceful-missing handling when no
trade_proposals row exists for that candidate) plus the named `value_key`
column. `reference_arm_rows` (added by the Fable5 strategy review fix below)
is the frozen reference arm's own rows in that same shape, or `None` for
`h_rej_1_rows` (no reference arm to floor-check, by construction).

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

CORRECTNESS-AUDIT FIX (HIGH-1/HIGH-2): ``candidate_outcomes`` is one row per
``(candidate_id, candidate_type)`` (schema.py's own
``idx_candoutcomes_candidate_type``), not one row per candidate -- every
candidate gets exactly one "AlphaOS-side" row (``candidate_type`` in
``outcomes_tracker._ALPHAOS_SIDE_TYPES`` -- mutually exclusive by that
module's own priority classification) PLUS, when a human later overrides
the decision, a SEPARATE ``'user_override'`` row seeded in parallel
(outcomes_tracker.py's own module comment: "seeded separately, in
parallel"). A bare ``JOIN candidate_outcomes ON candidate_id`` therefore
silently double-counts exactly those candidates -- corrupting every
reference-arm mean below, and in the original ``h_ttl_1_rows`` letting the
SAME row land in both the expired and approved arms whenever a candidate's
trade_proposals history has more than one row (a normal re-propose-after-
expiry lifecycle). Every join in this file now pins to a single row per
candidate_id via a `col.id = (SELECT ... ORDER BY id DESC LIMIT 1)`
subquery -- "most recent wins", the SAME convention baseline_report.py and
regime_arming_scorer.py already use for their own candidate_outcomes/
trade_proposals joins -- applied uniformly here rather than trusting
today's absence of a *known* duplicate-producing code path for the other
joined tables (candidate_catalysts/last30days_polarity have no DB-level
uniqueness constraint on candidate_id either).

FABLE5 STRATEGY REVIEW FIX (2026-07-10): the centered-delta design above
(reversible decision #1) freezes one arm's mean as a fixed CONSTANT and
ignores that arm's OWN sampling error -- this is a real, known lean, and it
leans the wrong way: it makes every resulting confidence interval a touch
NARROWER (more confident) than it should be, i.e. ANTI-CONSERVATIVE, the one
direction this codebase's whole measurement philosophy exists to guard
against. The resolver's own floor check (``effective_n()``/
``floor_span_days``) previously applied ONLY to the centered arm -- a
reference mean computed from a handful of rows could silently anchor an
entire comparison. Every ``h_xxx_1_rows()`` function below now ALSO returns
its own reference arm's rows (as a THIRD tuple element, ``None`` for
``h_rej_1_rows``, which centers against nothing), so
``alphaos.hypotheses.resolver`` can run the SAME ``effective_n()``/
``floor_span_days`` check against the reference arm too -- reusing the
hypothesis's own already-frozen floor rather than inventing a second,
separately-tuned number. This narrows, but does not eliminate, the bias:
even a reference arm that clears its own floor is still a point estimate
treated as a known constant. See ``alphaos.reports.hypothesis_report``'s own
rendered caveat for the reader-facing version of this note.
"""

from __future__ import annotations

from typing import Optional

# "Most recent, non-user_override candidate_outcomes row for this
# candidate_id" -- see module docstring. `{ref}` is always a fixed internal
# column reference this module controls (e.g. "t.candidate_id"), never
# user input.
_LATEST_OUTCOME = (
    "co.id = (SELECT co2.id FROM candidate_outcomes co2 "
    "WHERE co2.candidate_id = {ref} AND co2.candidate_type != 'user_override' "
    "ORDER BY co2.id DESC LIMIT 1)"
)
# "Most recent trade_proposals row for this candidate_id" -- used both for
# the plain max_holding_days LEFT JOIN (5 functions) and, in h_ttl_1_rows,
# as the driving table's own dedup.
_LATEST_PROPOSAL = (
    "tp.id = (SELECT tp2.id FROM trade_proposals tp2 "
    "WHERE tp2.candidate_id = {ref} ORDER BY tp2.id DESC LIMIT 1)"
)


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


def _reference_rows(rows: list[dict]) -> list[dict]:
    """Shape a frozen reference arm's own raw query rows into the same
    ``{symbol, decision_date, max_holding_days}`` form ``effective_n()``
    expects -- so the resolver can run the identical floor check against
    the reference arm that it already runs against the centered arm (Fable5
    strategy review fix; see module docstring)."""
    return [
        {
            "symbol": r["symbol"],
            "decision_date": (r["decision_at_utc"] or "")[:10],
            "max_holding_days": r["max_holding_days"],
        }
        for r in rows
    ]


def h_tqs_1_rows(journal) -> tuple[list[dict], str, Optional[list[dict]]]:
    """Top-vs-bottom TQS quartile, on 3d replay_r. Bottom quartile's mean is
    the frozen reference (TQS is meant to distinguish good from bad setups;
    the bottom arm is the "no signal" baseline); each TOP-quartile row's own
    forward_3d_r is centered against it."""
    rows = journal.query(
        "SELECT t.symbol, t.candidate_id, t.tqs_score, co.forward_3d_r, "
        "co.decision_at_utc, tp.max_holding_days "
        "FROM tqs_scores t "
        "JOIN candidate_outcomes co ON " + _LATEST_OUTCOME.format(ref="t.candidate_id") + " "
        "LEFT JOIN trade_proposals tp ON " + _LATEST_PROPOSAL.format(ref="t.candidate_id") + " "
        "WHERE t.source_type = 'candidate' AND t.is_mock = 0 "
        "AND t.data_quality_status = 'ok' AND co.forward_3d_r IS NOT NULL "
        "AND t.id = (SELECT t2.id FROM tqs_scores t2 WHERE t2.candidate_id = t.candidate_id "
        "AND t2.source_type = 'candidate' ORDER BY t2.id DESC LIMIT 1)"
    )
    scores = [r["tqs_score"] for r in rows if r["tqs_score"] is not None]
    if len(scores) < 4:
        return [], "centered_delta", None
    bottom_cut = _quantile(scores, 0.25)
    top_cut = _quantile(scores, 0.75)
    bottom_rows = [r for r in rows if r["tqs_score"] is not None and r["tqs_score"] <= bottom_cut]
    bottom_mean = _mean([r["forward_3d_r"] for r in bottom_rows])
    if bottom_mean is None:
        return [], "centered_delta", None
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
    return out, "centered_delta", _reference_rows(bottom_rows)


def h_cat_1_rows(journal) -> tuple[list[dict], str, Optional[list[dict]]]:
    """Catalyst presence vs absence, on replay_r. 'none_found' (actively
    searched, nothing found) is the frozen reference -- 'unavailable'/'error'
    rows are excluded entirely (missing data, not evidence of absence)."""
    rows = journal.query(
        "SELECT cc.symbol, cc.candidate_id, cc.catalyst_status, co.replay_r, "
        "co.decision_at_utc, tp.max_holding_days "
        "FROM candidate_catalysts cc "
        "JOIN candidate_outcomes co ON " + _LATEST_OUTCOME.format(ref="cc.candidate_id") + " "
        "LEFT JOIN trade_proposals tp ON " + _LATEST_PROPOSAL.format(ref="cc.candidate_id") + " "
        "WHERE cc.catalyst_status IN ('confirmed', 'none_found') "
        "AND co.replay_r IS NOT NULL "
        "AND cc.id = (SELECT cc2.id FROM candidate_catalysts cc2 "
        "WHERE cc2.candidate_id = cc.candidate_id ORDER BY cc2.id DESC LIMIT 1)"
    )
    none_found_rows = [r for r in rows if r["catalyst_status"] == "none_found"]
    none_found_mean = _mean([r["replay_r"] for r in none_found_rows])
    if none_found_mean is None:
        return [], "centered_delta", None
    out = [
        {
            "symbol": r["symbol"],
            "decision_date": (r["decision_at_utc"] or "")[:10],
            "max_holding_days": r["max_holding_days"],
            "centered_delta": r["replay_r"] - none_found_mean,
        }
        for r in rows if r["catalyst_status"] == "confirmed"
    ]
    return out, "centered_delta", _reference_rows(none_found_rows)


def h_int_1_rows(journal) -> tuple[list[dict], str, Optional[list[dict]]]:
    """Interest-score top decile vs the population median, on replay_r --
    the population median (not a bottom-decile mean) is the frozen
    reference, matching the spec's own literal "top decile > median"
    wording. `candidates.candidate_id` is DB-level UNIQUE (schema.py), so
    this driving table needs no dedup of its own -- only the
    candidate_outcomes/trade_proposals joins do. Reference-arm floor: the
    reference here is the WHOLE population (the median includes the tested
    top-decile rows too, not a disjoint arm), so the reference-arm rows
    returned are simply every row this function queried."""
    rows = journal.query(
        "SELECT c.symbol, c.candidate_id, c.interest_score, co.replay_r, "
        "co.decision_at_utc, tp.max_holding_days "
        "FROM candidates c "
        "JOIN candidate_outcomes co ON " + _LATEST_OUTCOME.format(ref="c.candidate_id") + " "
        "LEFT JOIN trade_proposals tp ON " + _LATEST_PROPOSAL.format(ref="c.candidate_id") + " "
        # EXP-1: H-INT-1 is a CORE hypothesis -- shadow_tier=0 excludes
        # shadow-tier rows from pooling into it (its own twin,
        # H-INT-SHADOW-1, is the preregistered home for the shadow-tier
        # version of this exact claim; never the same row population).
        "WHERE c.shadow_tier = 0 AND c.interest_score IS NOT NULL AND co.replay_r IS NOT NULL"
    )
    scores = [r["interest_score"] for r in rows]
    if len(scores) < 10:
        return [], "centered_delta", None
    top_cut = _quantile(scores, 0.90)
    population_median = _median([r["replay_r"] for r in rows])
    if population_median is None:
        return [], "centered_delta", None
    out = [
        {
            "symbol": r["symbol"],
            "decision_date": (r["decision_at_utc"] or "")[:10],
            "max_holding_days": r["max_holding_days"],
            "centered_delta": r["replay_r"] - population_median,
        }
        for r in rows if r["interest_score"] >= top_cut
    ]
    return out, "centered_delta", _reference_rows(rows)


def h_win_1_rows(journal) -> tuple[list[dict], str, Optional[list[dict]]]:
    """Morning (SGT hour < 12) vs afternoon (>= 12) regular-session scan
    windows, on replay_r. Afternoon is the frozen reference (the rel_volume
    formula's own denominator is a full-PRIOR-day baseline, structurally
    least distorted late in the session) -- each morning row's replay_r is
    centered against it. `candidates.candidate_id` is DB-level UNIQUE, so
    only the candidate_outcomes/trade_proposals joins need dedup."""
    rows = journal.query(
        "SELECT c.symbol, c.candidate_id, sb.started_at_sgt, co.replay_r, "
        "co.decision_at_utc, tp.max_holding_days "
        "FROM candidates c "
        "JOIN scan_batches sb ON sb.scan_batch_id = c.scan_batch_id "
        "JOIN candidate_outcomes co ON " + _LATEST_OUTCOME.format(ref="c.candidate_id") + " "
        "LEFT JOIN trade_proposals tp ON " + _LATEST_PROPOSAL.format(ref="c.candidate_id") + " "
        # EXP-1: H-WIN-1 is a CORE hypothesis -- exclude shadow-tier rows
        # (same rationale as h_int_1_rows above).
        "WHERE c.shadow_tier = 0 AND sb.market_session = 'regular' AND co.replay_r IS NOT NULL "
        "AND sb.started_at_sgt IS NOT NULL"
    )
    def _hour(r):
        try:
            return int(r["started_at_sgt"][11:13])
        except (TypeError, ValueError, IndexError):
            return None

    afternoon_rows = [r for r in rows if (_hour(r) or 0) >= 12]
    afternoon_mean = _mean([r["replay_r"] for r in afternoon_rows])
    if afternoon_mean is None:
        return [], "centered_delta", None
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
    return out, "centered_delta", _reference_rows(afternoon_rows)


def h_ttl_1_rows(journal) -> tuple[list[dict], str, Optional[list[dict]]]:
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
    function does not pick a side, it only produces the comparable rows.

    A candidate can carry MULTIPLE trade_proposals rows (re-propose after
    expiry, a normal PR6 lifecycle) -- this pins to each candidate's single
    MOST RECENT proposal (the "final" outcome superseding an earlier
    expiry), never both, so the same candidate_outcomes row can never land
    in both arms (correctness-audit HIGH-1's cross-arm-leakage finding)."""
    rows = journal.query(
        "SELECT tp.status, co.symbol, co.candidate_id, co.replay_r, "
        "co.decision_at_utc, tp.max_holding_days "
        "FROM trade_proposals tp "
        "JOIN candidate_outcomes co ON " + _LATEST_OUTCOME.format(ref="tp.candidate_id") + " "
        "WHERE tp.status IN ('expired', 'approved', 'submitted', 'filled') "
        "AND co.replay_r IS NOT NULL "
        "AND " + _LATEST_PROPOSAL.format(ref="tp.candidate_id")
    )
    approved_rows = [r for r in rows if r["status"] in ("approved", "submitted", "filled")]
    approved_mean = _mean([r["replay_r"] for r in approved_rows])
    if approved_mean is None:
        return [], "centered_delta", None
    out = [
        {
            "symbol": r["symbol"],
            "decision_date": (r["decision_at_utc"] or "")[:10],
            "max_holding_days": r["max_holding_days"],
            "centered_delta": r["replay_r"] - approved_mean,
        }
        for r in rows if r["status"] == "expired"
    ]
    return out, "centered_delta", _reference_rows(approved_rows)


def h_rej_1_rows(journal) -> tuple[list[dict], str, Optional[list[dict]]]:
    """Operator rejections' own already-computed delta_r -- maps directly
    onto the existing attribution machinery, no centering needed (this is
    exactly the same shape as BASELINE's own H-AI-1, just a different
    attribution_type slice). attribution_records carries no candidate_id
    fan-out risk here -- read directly, one row per resolved rejection. No
    frozen reference arm here (nothing is centered against a constant), so
    the third element is always None -- the resolver's reference-arm floor
    check is skipped for this hypothesis by construction, not by omission."""
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
    return out, "delta_r", None


def h_pol_1_rows(journal) -> tuple[list[dict], str, Optional[list[dict]]]:
    """Polarity divergence vs alignment, on replay_r. 'aligned' is the
    frozen reference; each 'divergent' row's replay_r is centered against
    it. NULL/unknown alignment rows are excluded (not the same as
    divergent)."""
    rows = journal.query(
        "SELECT p.symbol, p.candidate_id, p.direction_alignment, co.replay_r, "
        "co.decision_at_utc, tp.max_holding_days "
        "FROM last30days_polarity p "
        "JOIN candidate_outcomes co ON " + _LATEST_OUTCOME.format(ref="p.candidate_id") + " "
        "LEFT JOIN trade_proposals tp ON " + _LATEST_PROPOSAL.format(ref="p.candidate_id") + " "
        "WHERE p.direction_alignment IN ('aligned', 'divergent') "
        "AND co.replay_r IS NOT NULL "
        "AND p.id = (SELECT p2.id FROM last30days_polarity p2 "
        "WHERE p2.candidate_id = p.candidate_id ORDER BY p2.id DESC LIMIT 1)"
    )
    aligned_rows = [r for r in rows if r["direction_alignment"] == "aligned"]
    aligned_mean = _mean([r["replay_r"] for r in aligned_rows])
    if aligned_mean is None:
        return [], "centered_delta", None
    out = [
        {
            "symbol": r["symbol"],
            "decision_date": (r["decision_at_utc"] or "")[:10],
            "max_holding_days": r["max_holding_days"],
            "centered_delta": r["replay_r"] - aligned_mean,
        }
        for r in rows if r["direction_alignment"] == "divergent"
    ]
    return out, "centered_delta", _reference_rows(aligned_rows)


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
