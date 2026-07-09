"""CANARY: the model-drift canary's report -- persistent state, shows the
latest run regardless of when it happened (CANARY has no daily/interval
cadence of its own to key off, same rationale as EVAL-1's report). Pure
read; zero decision surface.
"""

from __future__ import annotations

from typing import Optional

from alphaos.canary.run import DRIFT_NONE


def build_canary_report(journal, run_id: Optional[str] = None) -> dict:
    """Report for one canary run -- defaults to the LATEST run when
    ``run_id`` is omitted. ``{"status": "no_runs_yet"}`` is the honest,
    expected empty state (no operator has run `alphaos canary_run` yet,
    or CANARY_ENABLED is still false), never an error."""
    run = (
        journal.one("SELECT * FROM canary_runs WHERE run_id = ?", (run_id,))
        if run_id else
        journal.one("SELECT * FROM canary_runs ORDER BY id DESC LIMIT 1")
    )
    if not run:
        return {"status": "no_runs_yet"}

    baseline = journal.one("SELECT run_id, started_at_sgt FROM canary_runs WHERE is_baseline = 1")
    n_prompts = run["n_prompts"] or 0
    n_failsafe = run["n_parse_or_failsafe"] or 0
    failsafe_rate = round(n_failsafe / n_prompts, 4) if n_prompts else None

    return {
        "status": "ok",
        "run_id": run["run_id"],
        "started_at_sgt": run["started_at_sgt"],
        "configured_model": run["configured_model"],
        "is_mock": bool(run["is_mock"]),
        "n_prompts": n_prompts,
        "n_parse_or_failsafe": n_failsafe,
        "failsafe_rate": failsafe_rate,
        "mean_confidence": run["mean_confidence"],
        "drift_tier": run["drift_tier"] or DRIFT_NONE,
        "drift_detail": run["drift_detail_json"],
        "baseline_pinned": baseline is not None,
        "baseline_run_id": baseline["run_id"] if baseline else None,
    }


def render_markdown(rep: dict) -> str:
    if rep["status"] == "no_runs_yet":
        return (
            "## Canary (model-drift)\n"
            "- No canary runs yet -- `python -m alphaos canary_corpus_build` then "
            "`python -m alphaos canary_run` (requires `CANARY_ENABLED=true`)."
        )
    lines = [
        "## Canary (model-drift)",
        f"- Last run: {rep['started_at_sgt']} SGT · model={rep['configured_model']}"
        f"{' (mock)' if rep['is_mock'] else ''} · {rep['n_prompts']} packet(s)",
        f"- Drift: {rep['drift_tier']}",
    ]
    if rep["failsafe_rate"] is not None:
        lines.append(f"- Parse/fail-safe rate: {rep['failsafe_rate'] * 100:.1f}%")
    if not rep["baseline_pinned"]:
        lines.append("- ⚠️ No baseline pinned yet -- `alphaos canary_pin_baseline <run_id>` "
                      "once a run looks clean; drift cannot be assessed until then.")
    return "\n".join(lines)
