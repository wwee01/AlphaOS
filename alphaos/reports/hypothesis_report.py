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

from alphaos.util import timeutils


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
    ]
    return "\n".join(lines)
