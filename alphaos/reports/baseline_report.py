"""BASELINE: the paired "does the AI add R?" report -- pure read over
``shadow_baseline_decisions`` joined to the SAME candidate's real AI-path
outcome (``candidate_outcomes.replay_r``, already computed by the ONE
replay engine, never a second implementation here). Descriptive only until
``ANALYSIS_NOT_BEFORE_DATE`` -- see the module's pre-registration block.
Never read by any gate/eval/risk/execution path (shadow law).
"""

from __future__ import annotations

from typing import Optional

from alphaos.baseline.rules import BASELINE_RULE_VERSIONS
from alphaos.stats.bootstrap import day_block_bootstrap
from alphaos.util import timeutils

# BASELINE's own pre-registration floors (spec item 6) -- reused here as the
# descriptive report's OWN display floor too, so the report and the formal
# pre-registration never disagree about what counts as "enough evidence."
# Matches Attribution v2's own paired-R-comparison floor exactly (same
# question shape: does a deviation/comparison add value in R) -- reusing an
# established codebase bar rather than inventing a third arbitrary number.
FLOOR_EFFECTIVE_N = 30
FLOOR_SPAN_DAYS = 28.0

ANALYSIS_NOT_BEFORE_DATE = "2026-09-07"  # matches REG-1's own checkpoint (60 days from build)

BASELINE_CAVEAT = (
    "BASELINE measures CONDITIONAL added-R: does the AI beat a frozen "
    "deterministic rule, GIVEN a candidate reached the AI evaluator? It "
    "does NOT claim the AI adds value vs. no scanning at all, and it never "
    "gates or influences any real decision (shadow law). ai_delta_r pairs "
    "only where BOTH the AI path and the rule's own replay have resolved; "
    "below the floor, only counts are shown -- no mean/CI."
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


def compute_baseline_report(rows: list[dict]) -> dict:
    """Pure aggregation. ``rows``: one dict per (candidate, rule) pair, each
    ``{"rule_version", "ai_replay_r", "baseline_replay_r", "decision_at_utc"}``
    -- already resolved on BOTH sides (callers filter before calling)."""
    by_rule: dict[str, list[dict]] = {v: [] for v in BASELINE_RULE_VERSIONS}
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
        meets_floor = n_day_blocks >= FLOOR_EFFECTIVE_N and (span or 0) >= FLOOR_SPAN_DAYS

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
        "floor_effective_n": FLOOR_EFFECTIVE_N,
        "floor_span_days": FLOOR_SPAN_DAYS,
        "analysis_not_before": ANALYSIS_NOT_BEFORE_DATE,
        "caveat": BASELINE_CAVEAT,
    }


def build_baseline_report(journal, settings, limit: int = 5000) -> dict:
    """Journal-facing entry point. PURE READ. Joins shadow_baseline_decisions
    (resolved) to the SAME candidate's real AI-path replay_r, taking the
    MOST RECENT resolved candidate_outcomes row per candidate_id (mirrors
    this codebase's established "most-recent-wins" convention, e.g. TASK-R's
    _latest_label_for_packet) -- a candidate very rarely has more than one
    resolved outcome row (PR8 audit LOW-1's own latent, unreachable-today
    edge case), and taking the latest is the same safe default used there."""
    rows = journal.query(
        "SELECT sbd.rule_version, sbd.decision_at_utc, sbd.replay_r AS baseline_replay_r, "
        "(SELECT co.replay_r FROM candidate_outcomes co "
        " WHERE co.candidate_id = sbd.candidate_id AND co.outcome_status = 'resolved' "
        " AND co.replay_r IS NOT NULL ORDER BY co.id DESC LIMIT 1) AS ai_replay_r "
        "FROM shadow_baseline_decisions sbd "
        "WHERE sbd.replay_status = 'complete' AND sbd.replay_r IS NOT NULL "
        "ORDER BY sbd.id DESC LIMIT ?",
        (limit,),
    )
    paired_rows = [r for r in rows if r.get("ai_replay_r") is not None]

    n_shadow_resolved = len(rows)
    rep = compute_baseline_report(paired_rows)
    rep["as_of"] = timeutils.market_date().isoformat()
    rep["n_shadow_resolved"] = n_shadow_resolved
    rep["n_paired_total"] = len(paired_rows)
    today = timeutils.market_date().isoformat()
    rep["analysis_ready"] = today >= ANALYSIS_NOT_BEFORE_DATE
    return rep


def render_markdown(rep: dict) -> str:
    lines = [
        "## BASELINE -- does the AI add R? (shadow, nothing gated for real)",
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
                f"- {rule_version}: below floor ({rep['floor_effective_n']}+ day-blocks AND "
                f"{rep['floor_span_days']}+ day span needed) -- counts only: "
                f"n_paired={r['n_paired']}, day_blocks={r['n_day_blocks']}, span={r['span_days']}d"
            )
    lines += ["", f"> ⚠️ {rep['caveat']}"]
    return "\n".join(lines)
