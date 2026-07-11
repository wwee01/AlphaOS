"""PR12: the hypothesis-registry status report -- every seeded hypothesis's
risk class, claim, MECHANICAL status, and (once evaluated) the fresh
verdict/q-value cached by the resolver. PURE READ over ``hypothesis_proposals``;
nothing gated for real, never read by any gate/eval/labeller/risk/execution
path (same shadow-report posture as baseline_report.py/regime_arming_scorer.py).

Also closes the loop baseline_report.py's own module docstring named as
deferred future work: once BASELINE's own H-AI-1 preregistration has been
evaluated, ITS row shows up here too (same hypothesis_proposals table, same
fresh compute_verdicts() call) -- this is the first report to surface a
q-value for that hypothesis, rather than the raw always-descriptive
one_sided_p_below_zero baseline_report.py itself intentionally stops short of.

verdict/status are deliberately reported RAW, side by side with each row's
own claim text -- never collapsed into a single "PASSED"/"FAILED" column.
See alphaos.hypotheses.constants.HypothesisStatus's own docstring for why:
PORT-1's rejected/forward-test-candidate/inconclusive vocabulary describes
which side of zero a CI lands on, not whether that confirms or refutes any
particular hypothesis's own directional claim -- a human reading the claim
text next to the raw verdict is the intended, safer interface.
"""

from __future__ import annotations

from typing import Optional

from alphaos.hypotheses.queries import METRIC_FUNCTIONS
from alphaos.stats.effective_n import effective_n
from alphaos.util import timeutils


def _progress_for_row(journal, row: dict) -> Optional[dict]:
    """PR-UI-B2: testing-status progress vs this hypothesis's own frozen
    floor -- n/N effective-cluster count and days/span, for the Learning
    tab's Hypotheses panel (UI/UX doc §5: "testing progress bars n/N and
    days/span"). Computed FRESH via the SAME metric function + effective_n()
    + reference-arm floor check ``alphaos.hypotheses.resolver._resolve_one``
    itself uses -- never a second, separately-tuned notion of "close to
    ready" that could silently disagree with what the resolver would
    actually do on its next pass (one truth law).

    PURE READ -- never writes, never calls evaluate_hypothesis(). Returns
    None for a row that isn't 'testing', has no metric_fn_name (H-AI-1,
    which links to BASELINE's own preregistration instead of computing
    anything here), or has no prereg_id yet (nothing to floor-check
    against)."""
    if row.get("status") != "testing":
        return None
    metric_fn_name = row.get("metric_fn_name")
    if not metric_fn_name:
        return None
    metric_fn = METRIC_FUNCTIONS.get(metric_fn_name)
    if metric_fn is None or not row.get("prereg_id"):
        return None
    prereg = journal.one(
        "SELECT floor_effective_n, floor_span_days FROM preregistrations WHERE prereg_id = ?",
        (row["prereg_id"],),
    )
    if prereg is None:
        return None

    # PERF NOTE (audit MEDIUM-1, accepted for now): each testing hypothesis
    # runs its full metric query + effective_n() per report build (~7 scans
    # per Learning-panel render at today's table sizes -- milliseconds).
    # Revisit with a cache/row-cap when EXP-1 multiplies data volume; a
    # premature cache here would be a second source of truth to keep honest.
    rows, _value_key, reference_arm_rows = metric_fn(journal)
    en = effective_n(rows)
    clears_floor = (
        en["effective_n"] >= prereg["floor_effective_n"]
        and (en["span_days"] or 0) >= prereg["floor_span_days"]
    )
    if reference_arm_rows is not None:
        ref_en = effective_n(reference_arm_rows)
        clears_floor = clears_floor and (
            ref_en["effective_n"] >= prereg["floor_effective_n"]
            and (ref_en["span_days"] or 0) >= prereg["floor_span_days"]
        )
    # Audit fixup (LOW-1): clears_floor alone could read as "resolver will
    # act on its next pass" -- but the resolver ALSO enforces the
    # analysis_not_before calendar floor, which this progress view
    # deliberately ignores (it answers "how far along", not "due"). Surface
    # the calendar state explicitly so the tab can never imply readiness
    # before the pre-registered date.
    from alphaos.util import timeutils

    anb = row.get("analysis_not_before") or ""
    calendar_floor_reached = bool(anb) and timeutils.stamp().local_sgt[:10] >= str(anb)
    return {
        "effective_n": en["effective_n"],
        "floor_effective_n": prereg["floor_effective_n"],
        "span_days": en["span_days"],
        "floor_span_days": prereg["floor_span_days"],
        "clears_floor": clears_floor,
        "calendar_floor_reached": calendar_floor_reached,
        "resolver_ready": clears_floor and calendar_floor_reached,
    }


def build_hypothesis_report(journal) -> dict:
    rows = journal.query(
        "SELECT * FROM hypothesis_proposals ORDER BY "
        "CASE risk_class WHEN 'C' THEN 0 WHEN 'B' THEN 1 ELSE 2 END, hypothesis_id"
    )
    today = timeutils.stamp().local_sgt[:10]
    for row in rows:
        row["overdue"] = (
            row["status"] == "testing"
            and row["metric_fn_name"] is not None
            and row["analysis_not_before"] is not None
            and today >= row["analysis_not_before"]
        )
        # PR-UI-B2: progress vs floor, display-only (see _progress_for_row's
        # own docstring) -- added as an extra field on each row rather than a
        # parallel list, so a caller iterating `rep["hypotheses"]` never has
        # to zip two lists back together.
        row["progress"] = _progress_for_row(journal, row)
    return {
        "as_of": today,
        "n_total": len(rows),
        "n_resolved": sum(1 for r in rows if r["status"] == "resolved"),
        "n_testing": sum(1 for r in rows if r["status"] == "testing"),
        "n_proposed": sum(1 for r in rows if r["status"] == "proposed"),
        "hypotheses": rows,
    }


def render_markdown(rep: dict) -> str:
    lines = [
        "## PR12 -- hypothesis registry (shadow, nothing gated for real)",
        f"As of {rep['as_of']} SGT: {rep['n_total']} seeded, {rep['n_resolved']} resolved, "
        f"{rep['n_testing']} testing, {rep['n_proposed']} proposed (no linked preregistration yet)",
        "",
    ]
    for h in rep["hypotheses"]:
        marker = " (OVERDUE)" if h.get("overdue") else ""
        lines.append(f"- **{h['hypothesis_id']}** [{h['risk_class']}] {h['status']}{marker}")
        lines.append(f"  claim: {h['claim']}")
        if h["status"] == "resolved":
            q = h.get("last_q_value")
            q_str = f"{q:.4f}" if q is not None else "n/a"
            lines.append(f"  verdict: {h.get('last_verdict')} (q={q_str}) -- {h.get('last_reason')}")
    lines += [
        "",
        "> ⚠️ Raw verdict/status only -- read each claim's own directional framing "
        "before treating a 'rejected' or 'forward-test-candidate' verdict as good or bad news "
        "for that specific hypothesis (they are not uniformly the same direction). "
        "MET/FAILED/WITHDRAWN are operator-only; this report never sets them.",
        "> ⚠️ Most hypotheses here use a centered-delta design: one arm's mean is frozen as a "
        "fixed reference and every other observation is measured against it, ignoring that "
        "reference arm's own sampling error. This is a real, known lean, and it leans "
        "ANTI-CONSERVATIVE -- confidence intervals read a touch narrower (more confident) than "
        "they should. The resolver now requires the reference arm to ALSO clear this "
        "hypothesis's own sample-size/span floor before evaluating (Fable5 strategy review, "
        "2026-07-10), which narrows but does not eliminate this bias.",
    ]
    return "\n".join(lines)
