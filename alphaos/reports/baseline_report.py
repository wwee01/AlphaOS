"""BASELINE: the paired "does the AI add R?" report -- pure read over
``shadow_baseline_decisions`` joined to the SAME candidate's real AI-path
outcome (``candidate_outcomes.replay_r``, already computed by the ONE
replay engine, never a second implementation here). Descriptive only until
``ANALYSIS_NOT_BEFORE_DATE`` -- see the module's pre-registration block.
Never read by any gate/eval/risk/execution path (shadow law).

KIV, explicitly deferred, NOT a safety gap (2026-07-09 scope/safety audit
finding LOW-1): spec item 5 asks for the one-sided test to be reported as
``q`` (PORT-1 BH-FDR), not raw ``p``. This report intentionally does NOT do
that -- ``alphaos.stats.fdr.compute_verdicts()`` computes q FRESH over the
full family of EVALUATED preregistrations (``evaluated_at_utc IS NOT
NULL``), and BASELINE's own hypothesis has not been evaluated yet (that is
an operator-invoked, one-shot action via ``evaluate_hypothesis()``, not
before ``ANALYSIS_NOT_BEFORE_DATE``). Computing a q-value against a family
that doesn't yet include this hypothesis would be statistically incoherent,
not just premature. This report's own ``one_sided_p_below_zero`` field
(inside ``day_block_bootstrap()``'s output) is a live, always-recomputed
bootstrap diagnostic -- never the formal test, never stored, never
mistaken for a q-value. Once an operator runs the (not-yet-built) formal
evaluation CLI after the analysis date, ITS output should report q via
compute_verdicts(); wiring that up is future work, tracked here so it isn't
silently forgotten.
"""

from __future__ import annotations

from typing import Optional

from alphaos.baseline.rules import BASELINE_RULE_VERSIONS, BASELINE_RULE_VERSIONS_HOLD10
from alphaos.stats.bootstrap import day_block_bootstrap
from alphaos.util import timeutils

# BASELINE's own pre-registration floors (spec item 6) -- reused here as the
# descriptive report's OWN display floor too, so the report and the formal
# pre-registration never disagree about what counts as "enough evidence."
# Matches Attribution v2's own paired-R-comparison floor exactly (same
# question shape: does a deviation/comparison add value in R) -- reusing an
# established codebase bar rather than inventing a third arbitrary number.
#
# Named FLOOR_DAY_BLOCKS (2026-07-09 correctness-audit NIT-1, was
# FLOOR_EFFECTIVE_N): this gates day_block_bootstrap()'s own n_day_blocks
# count, a DIFFERENT axis from PORT-1's effective_n() (symbol + holding-
# window clustering) -- the old name implied the latter.
FLOOR_DAY_BLOCKS = 30
FLOOR_SPAN_DAYS = 28.0

ANALYSIS_NOT_BEFORE_DATE = "2026-09-07"  # matches REG-1's own checkpoint (60 days from build)

# HOLD-2 (audit-fixup, MEDIUM-9 / STATUS CORRECTION item 4): the hold10
# arm's OWN fresh pre-registration date -- "v2+ arms are new pre-
# registrations" (decision-log law from 2026-07), never inherited from the
# v1 arms' ANALYSIS_NOT_BEFORE_DATE above. 2026-10-05 matches the spec's
# own operator-action step 4.
HOLD10_ANALYSIS_NOT_BEFORE_DATE = "2026-10-05"
HOLD10_REPORT_TITLE = (
    "## BASELINE-10 -- 10-trading-day hold arm (shadow, SEGMENTED from the "
    "v1 report above -- a separate, freshly pre-registered arm, never mixed "
    "into v1's n_shadow_resolved/n_paired_total/mean_ai_delta_r)"
)

# The "gross, gap-free upper bound" sentence is VERBATIM from the spec
# (audit C1): the replay engine idealizes fills at the exact stop/target
# level with no slippage/gap risk modeled -- every ΔR number here is an
# upper bound on what a real, cost-and-gap-aware fill would have achieved,
# not a final answer. Stop-hit rows are earmarked for a future COST-1
# gap-haircut re-statement, applied identically to every arm (never
# BASELINE-specific), once that PR exists.
BASELINE_CAVEAT = (
    "BASELINE measures CONDITIONAL added-R: does the AI beat a frozen "
    "deterministic rule, GIVEN a candidate reached the AI evaluator? It "
    "does NOT claim the AI adds value vs. no scanning at all, and it never "
    "gates or influences any real decision (shadow law). ai_delta_r pairs "
    "only where BOTH the AI path and the rule's own replay have resolved; "
    "below the floor, only counts are shown -- no mean/CI. Every ΔR number "
    "is a gross, gap-free upper bound: replay idealizes fills at the exact "
    "stop/target level with no slippage or gap risk modeled -- until COST-1 "
    "lands, treat these as ceilings, not realized outcomes."
)


def _span_days(dates: list[str]) -> Optional[float]:
    parsed = []
    for d in dates:
        dt = timeutils.parse_iso(d)
        if dt is not None:
            parsed.append(dt)
    if len(parsed) < 2:
        return None
    return (max(parsed) - min(parsed)).total_seconds() / 86400.0


def compute_baseline_report(
    rows: list[dict], rule_versions: tuple = BASELINE_RULE_VERSIONS,
    analysis_not_before: str = ANALYSIS_NOT_BEFORE_DATE,
) -> dict:
    """Pure aggregation. ``rows``: one dict per (candidate, rule) pair, each
    ``{"rule_version", "ai_replay_r", "baseline_replay_r", "decision_at_utc"}``
    -- already resolved on BOTH sides (callers filter before calling).

    Audit-fixup HOLD-2 (HIGH-4 / STATUS CORRECTION item 4): ``rule_versions``
    and ``analysis_not_before`` are now parameters, not hardcoded module
    constants -- the SAME aggregation now also serves the SEGMENTED hold10
    report (``rule_versions=BASELINE_RULE_VERSIONS_HOLD10``,
    ``analysis_not_before=HOLD10_ANALYSIS_NOT_BEFORE_DATE``) without a
    second implementation. Defaults reproduce the pre-fixup v1-only
    behavior exactly -- every existing caller keeps working unchanged."""
    by_rule: dict[str, list[dict]] = {v: [] for v in rule_versions}
    for r in rows:
        if r["rule_version"] in by_rule:
            by_rule[r["rule_version"]].append(r)

    rule_reports = {}
    for rule_version, rule_rows in by_rule.items():
        paired = [
            {
                "delta_r": r["ai_replay_r"] - r["baseline_replay_r"],
                "decision_date": (r.get("decision_at_utc") or "")[:10],
            }
            for r in rule_rows
        ]
        n_paired = len(paired)
        dates = [p["decision_date"] for p in paired if p["decision_date"]]
        span = _span_days(dates)

        boot = day_block_bootstrap(paired, "delta_r", n_resamples=10000)
        n_day_blocks = boot["n_day_blocks"]
        meets_floor = n_day_blocks >= FLOOR_DAY_BLOCKS and (span or 0) >= FLOOR_SPAN_DAYS

        if not meets_floor or boot["status"] != "ok":
            rule_reports[rule_version] = {
                "n_paired": n_paired, "n_day_blocks": n_day_blocks,
                "span_days": round(span, 1) if span is not None else None,
                "mean_ai_delta_r": None, "ci_low": None, "ci_high": None,
                "ci_method": None, "status": "below_sample_floor",
            }
        else:
            rule_reports[rule_version] = {
                "n_paired": n_paired, "n_day_blocks": n_day_blocks,
                "span_days": round(span, 1) if span is not None else None,
                "mean_ai_delta_r": boot["point_estimate"],
                "ci_low": boot["ci_low"], "ci_high": boot["ci_high"],
                "ci_method": boot["ci_method"], "status": "ok",
            }

    return {
        "rules": rule_reports,
        "floor_day_blocks": FLOOR_DAY_BLOCKS,
        "floor_span_days": FLOOR_SPAN_DAYS,
        "analysis_not_before": analysis_not_before,
        "caveat": BASELINE_CAVEAT,
    }


def build_baseline_report(
    journal, settings, limit: int = 5000, rule_versions: tuple = BASELINE_RULE_VERSIONS,
    analysis_not_before: str = ANALYSIS_NOT_BEFORE_DATE,
) -> dict:
    """Journal-facing entry point. PURE READ. Joins shadow_baseline_decisions
    (resolved) to the SAME candidate's real AI-path replay_r, taking the
    MOST RECENT resolved candidate_outcomes row per candidate_id (mirrors
    this codebase's established "most-recent-wins" convention, e.g. TASK-R's
    _latest_label_for_packet) -- a candidate very rarely has more than one
    resolved outcome row (PR8 audit LOW-1's own latent, unreachable-today
    edge case), and taking the latest is the same safe default used there.

    Audit-fixup HOLD-2 (HIGH-4, both audits convergent / STATUS CORRECTION
    item 4): ``rule_versions`` now filters the SQL query itself (``AND
    sbd.rule_version IN (...)``) BEFORE the shared ``LIMIT``, not just in
    the Python aggregation layer. Pre-fixup, the query read ALL rule
    versions (v1 arms AND hold10 arms) before ``compute_baseline_report``'s
    own Python-side filter dropped the ones it didn't recognize -- so the
    hold10 rows this build adds would consume slots in the shared
    ``LIMIT 5000``, silently dropping the OLDEST v1 rows first (measured:
    n_paired 1600->1250 on a representative corpus), and the headline
    ``n_shadow_resolved``/``n_paired_total`` counts (rendered into the daily
    digest) would include hold10 rows, visibly doubling those numbers at
    merge with zero actual change to the v1 evidence. Filtering at the SQL
    layer means the v1 report's own ``LIMIT`` is never diluted by rows it
    was never going to report on anyway -- default ``rule_versions``
    reproduces the pre-fixup v1-only query exactly."""
    placeholders = ",".join("?" for _ in rule_versions)
    rows = journal.query(
        "SELECT sbd.rule_version, sbd.decision_at_utc, sbd.replay_r AS baseline_replay_r, "
        "(SELECT co.replay_r FROM candidate_outcomes co "
        " WHERE co.candidate_id = sbd.candidate_id AND co.outcome_status = 'resolved' "
        " AND co.replay_r IS NOT NULL ORDER BY co.id DESC LIMIT 1) AS ai_replay_r "
        "FROM shadow_baseline_decisions sbd "
        "WHERE sbd.replay_status = 'complete' AND sbd.replay_r IS NOT NULL "
        f"AND sbd.rule_version IN ({placeholders}) "
        "ORDER BY sbd.id DESC LIMIT ?",
        (*rule_versions, limit),
    )
    paired_rows = [r for r in rows if r.get("ai_replay_r") is not None]

    n_shadow_resolved = len(rows)
    rep = compute_baseline_report(paired_rows, rule_versions=rule_versions, analysis_not_before=analysis_not_before)
    rep["as_of"] = timeutils.market_date().isoformat()
    rep["n_shadow_resolved"] = n_shadow_resolved
    rep["n_paired_total"] = len(paired_rows)
    today = timeutils.market_date().isoformat()
    rep["analysis_ready"] = today >= analysis_not_before
    return rep


def build_baseline_hold10_report(journal, settings, limit: int = 5000) -> dict:
    """Audit-fixup HOLD-2 (MEDIUM-9 / STATUS CORRECTION item 4): the
    SEGMENTED hold10 report -- same aggregation, own arm-set
    (``BASELINE_RULE_VERSIONS_HOLD10``), own pre-registration date
    (``HOLD10_ANALYSIS_NOT_BEFORE_DATE``). Never mixed into the v1 report's
    own numbers (see ``build_baseline_report``'s own SQL-layer filtering)."""
    return build_baseline_report(
        journal, settings, limit=limit, rule_versions=BASELINE_RULE_VERSIONS_HOLD10,
        analysis_not_before=HOLD10_ANALYSIS_NOT_BEFORE_DATE,
    )


def render_markdown(
    rep: dict, title: str = "## BASELINE -- does the AI add R? (shadow, nothing gated for real)",
) -> str:
    """HOLD-2 audit-fixup (MEDIUM-9): ``title`` is now a parameter -- default
    unchanged (byte-identical for every pre-existing caller), so
    ``render_full_baseline_markdown`` below can reuse this SAME renderer for
    the SEGMENTED hold10 section with ``HOLD10_REPORT_TITLE`` instead of a
    second hand-written render function."""
    lines = [
        title,
        f"Analysis not before `{rep['analysis_not_before']}`"
        + ("" if rep.get("analysis_ready") else " (NOT YET REACHED -- descriptive only)"),
        f"- {rep['n_shadow_resolved']} resolved shadow rows, {rep['n_paired_total']} paired with a "
        "resolved AI-path outcome",
        "",
    ]
    for rule_version, r in rep["rules"].items():
        if r["status"] == "ok":
            lines.append(
                f"- {rule_version}: AI ΔR mean={r['mean_ai_delta_r']:+.4f} "
                f"[{r['ci_low']:+.4f}, {r['ci_high']:+.4f}] ({r['ci_method']}) "
                f"(n_paired={r['n_paired']}, day_blocks={r['n_day_blocks']}, span={r['span_days']}d)"
            )
        else:
            lines.append(
                f"- {rule_version}: below floor ({rep['floor_day_blocks']}+ day-blocks AND "
                f"{rep['floor_span_days']}+ day span needed) -- counts only: "
                f"n_paired={r['n_paired']}, day_blocks={r['n_day_blocks']}, span={r['span_days']}d"
            )
    lines += ["", f"> ⚠️ {rep['caveat']}"]
    return "\n".join(lines)


def render_full_baseline_markdown(journal, settings, limit: int = 5000) -> str:
    """Audit-fixup HOLD-2 (MEDIUM-9 / STATUS CORRECTION item 4): the v1
    report PLUS the hold10 report, rendered as two CLEARLY SEGMENTED
    sections (distinct headers, distinct pre-registration dates, distinct
    counts) -- never a single merged section that could be misread as one
    analysis. This is the function ``cmd_baseline_report`` calls now;
    ``render_markdown(build_baseline_report(...))`` alone (the pre-fixup
    call) still works unchanged for any other caller that only wants the
    v1 section."""
    v1_rep = build_baseline_report(journal, settings, limit=limit)
    hold10_rep = build_baseline_hold10_report(journal, settings, limit=limit)
    return "\n\n".join([render_markdown(v1_rep), render_markdown(hold10_rep, title=HOLD10_REPORT_TITLE)])
