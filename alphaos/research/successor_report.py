"""Human-readable successor output. Never hides data/engineering blockers."""
from __future__ import annotations


def render_report(bundle: dict) -> str:
    lines = ["# Retained research successor export", "",
             "Retrospective, draft-only analysis; no activation, formal verdict, or profit claim.", "",
             f"Snapshot SHA-256: `{bundle['snapshot_sha256']}`. Capture boundary: {bundle['as_of_utc']}.", "",
             "The export retains every input-selected member, including missing outcomes. Available-member means below are descriptive and can be biased by missingness. They are not the complete-population estimand.", "",
             "| Original registration | Method | Members / measured | Available-member estimate | Remaining engineering |",
             "|---|---|---:|---:|---|"]
    for f in bundle["families"]:
        c, s = f["contract"], f.get("summary", {})
        point = s.get("point_estimate")
        text = "unavailable" if point is None else f"{point:+.6f}"
        if c.get("statistic") == "paired_mean":
            text = "see separate primary/final proposed views"
        missing = "; ".join(f.get("missing_engineering", [])) or "reviewed enrollment/finalization integration"
        lines.append(f"| {c.get('name', '?')} `{c['predecessor_id']}` | {c.get('statistic', 'unsupported')} | {s.get('n_members', 0)} / {s.get('n_observed', 0)} | {text} | {missing} |")
    for f in bundle["families"]:
        c, s = f["contract"], f.get("summary", {})
        detail = {k: (f"{len(v)} IDs retained in JSON" if k.endswith("_ids") and isinstance(v, list) else v)
                  for k, v in f.get("details", {}).items()}
        lines += ["", f"## {c.get('name', c['predecessor_id'])}", "",
                  f"Original claim: {c['original']['hypothesis']}", "",
                  f"Original metric: {c['original']['metric']}", "",
                  f"Draft target/direction/horizon: {c.get('effect_target')} / {c.get('direction')} / {c.get('horizon_sessions')} sessions.", "",
                  f"Members by arm: {s.get('members_by_arm', {})}; measured by arm: {s.get('observed_by_arm', {})}.", "",
                  f"Measurement reasons: {s.get('measurement_reasons', {})}.", "",
                  f"Required contract choices: {', '.join(c.get('required_choices', []))}.", "",
                  f"Adapter details: {detail}."]
    lines += ["", "## What this implementation supplies", "",
              "Executable paired, separate-arm mean, top-mean-minus-population-median, single-mean and matched-event primitives recompute all references jointly. Fixed target/direction and equivalence margins are explicit. Actual overlapping intervals, shared controls and pairs travel together; unknown intervals and missing members block inferential output. Null-centered diagnostic tests and uncentered intervals remain draft approximations, without FDR, finalization or promotion permission.", "",
              "Saved-bar reconstruction uses complete scheduled sessions and recorded levels, with actual daily exit bounds. It does not substitute a three-day endpoint for TQS bracket R. It is gross and idealized; data feed, missing observations, costs/gaps and retrospective membership limit interpretation.", "",
              "Missing engineering remains explicit: PER common-start market-adjusted measurement receipt collection, externally receipted prospective enrollment, reviewed multiplicity/stopping, and successor-only finalization integration. PER input-first event/control membership itself is exported, including missing outcomes and all population guards. Contract review and sufficient independent data remain separate requirements. The original fifteen quarantine statuses and frozen results are untouched.", ""]
    return "\n".join(lines)
