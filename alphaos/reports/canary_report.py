"""CANARY: the model-drift canary's report -- persistent state, shows the
latest run regardless of when it happened (CANARY has no daily/interval
cadence of its own to key off, same rationale as EVAL-1's report). Pure
read; zero decision surface.

CANARY-2 (docs/roadmap/alphaos-canary2-drift-confirmation-spec.md, Design 5):
the default (no explicit ``run_id``) query now resolves the latest TRIGGER
run (``confirmation_of IS NULL``), never a confirmation run's own row --
``run_canary_confirmed`` already annotates a trigger's own
``drift_detail_json`` with a ``"confirmation"`` sub-dict once its
confirmation resolves (confirmed / not_confirmed / unconfirmed_page), so
reading the trigger row directly IS reading the confirmed state -- no
special-casing needed here beyond surfacing that sub-dict and, when present,
the confirming run's own summary. This is what makes the daily brief's
canary line "reflect the CONFIRMED state, not the raw first trip" (spec
Design 5's own wording) with no separate code path.
"""

from __future__ import annotations

import json
from typing import Optional

from alphaos.canary.run import DRIFT_NONE


def build_canary_report(journal, run_id: Optional[str] = None) -> dict:
    """Report for one canary run -- defaults to the LATEST TRIGGER run (i.e.
    excluding confirmation replays -- see module docstring) when ``run_id``
    is omitted. An explicit ``run_id`` may name either a trigger or a
    confirmation row; both render, with lineage in either direction.
    ``{"status": "no_runs_yet"}`` is the honest, expected empty state (no
    operator has run `alphaos canary_run` yet, or CANARY_ENABLED is still
    false), never an error."""
    run = (
        journal.one("SELECT * FROM canary_runs WHERE run_id = ?", (run_id,))
        if run_id else
        journal.one("SELECT * FROM canary_runs WHERE confirmation_of IS NULL ORDER BY id DESC LIMIT 1")
    )
    if not run:
        return {"status": "no_runs_yet"}

    baseline = journal.one("SELECT run_id, started_at_sgt FROM canary_runs WHERE is_baseline = 1")
    n_prompts = run["n_prompts"] or 0
    n_failsafe = run["n_parse_or_failsafe"] or 0
    failsafe_rate = round(n_failsafe / n_prompts, 4) if n_prompts else None

    detail_raw = run["drift_detail_json"]
    try:
        detail = json.loads(detail_raw) if detail_raw else {}
    except (TypeError, ValueError):  # tolerate a hand-tampered/legacy row, never raise on a read
        detail = {}
    confirmation = detail.get("confirmation")  # None unless run_canary_confirmed has annotated this row

    report = {
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
        "drift_detail": detail_raw,
        "baseline_pinned": baseline is not None,
        "baseline_run_id": baseline["run_id"] if baseline else None,
        # CANARY-2 confirmation lineage (Design 5 / test 14):
        "confirmation_of": run["confirmation_of"],  # set only when THIS row is itself a confirmation replay
        "confirmation": confirmation,  # {"status": ..., "confirming_run_id": ...} once resolved, else None
    }
    return report


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
    ]
    drift_line = f"- Drift: {rep['drift_tier']}"
    confirmation = rep.get("confirmation")
    if confirmation:
        status = confirmation.get("status")
        cid = confirmation.get("confirming_run_id")
        if status == "confirmed":
            drift_line += f" -- CONFIRMED by same-day re-run {cid}"
        elif status == "not_confirmed":
            drift_line += f" -- NOT confirmed by re-run {cid} (transient wobble, no page sent)"
        elif status == "unconfirmed_page":
            drift_line += f" -- UNCONFIRMED (confirmation could not execute: {confirmation.get('reason')})"
    lines.append(drift_line)
    if rep.get("confirmation_of"):
        lines.append(f"- This run is a same-day confirmation replay of {rep['confirmation_of']}.")
    if rep["failsafe_rate"] is not None:
        lines.append(f"- Parse/fail-safe rate: {rep['failsafe_rate'] * 100:.1f}%")
    if not rep["baseline_pinned"]:
        lines.append("- ⚠️ No baseline pinned yet -- `alphaos canary_pin_baseline <run_id>` "
                      "once a run looks clean; drift cannot be assessed until then.")
    return "\n".join(lines)
