"""PR12: the nightly resolver -- finds ``hypothesis_proposals`` rows due for
evaluation, computes their metric, freezes evidence via
``alphaos.stats.preregistration.evaluate_hypothesis()``, then refreshes
``last_verdict``/``last_q_value``/``last_reason`` for the WHOLE evaluated
family (never just this run's own rows -- ``compute_verdicts()``'s family is
"every evaluated preregistration" project-wide, per schema.py's own comment
on the ``preregistrations`` table, and BH-FDR correction can shift an
ALREADY-evaluated hypothesis's q-value when a new sibling joins the family).

``status`` only ever moves ``proposed -> testing -> resolved`` here -- see
``constants.HypothesisStatus``'s own docstring for why MET/FAILED/WITHDRAWN
stay operator-only (a reversible decision, logged in HANDOVER.md).

"Due" means: ``prereg_id`` is set, ``status == testing``, ``metric_fn_name``
is not NULL (H-AI-1-shaped rows are only ever synced, never computed here),
and today (SGT) >= ``analysis_not_before``. That calendar bound is a FLOOR,
not a trigger: a row that clears the calendar gate but does NOT yet clear
its OWN ``floor_effective_n``/``floor_span_days`` (checked via
``effective_n()`` BEFORE calling ``evaluate_hypothesis()``, never after) is
left in ``testing`` for a later run rather than freezing insufficient-data
evidence early and burning the one-shot evaluation on a data-starved day.
This pre-check is a sample-size/coverage gate only -- it never inspects
``value_key``'s sign or magnitude, so it does not reintroduce the
optional-stopping risk ``evaluate_hypothesis()``'s own immutability guard
exists to prevent.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from alphaos.hypotheses.constants import HypothesisStatus
from alphaos.hypotheses.queries import METRIC_FUNCTIONS
from alphaos.stats.effective_n import effective_n
from alphaos.stats.fdr import compute_verdicts
from alphaos.stats.preregistration import (
    PreregistrationAlreadyEvaluatedError,
    evaluate_hypothesis,
)
from alphaos.util import timeutils


def resolve_due_hypotheses(journal, now: Optional[datetime] = None) -> dict:
    """One resolver pass over every ``testing``-status hypothesis. Never
    raises -- a bug or exception evaluating one row is caught and that row
    is simply left for the next run (per-item failure isolation; one
    hypothesis's own query bug must never block the other 7 from
    resolving). Returns a summary dict for the caller/report/tests:
    ``{"evaluated", "not_yet_sufficient", "skipped_no_prereg", "synced",
    "refreshed", "errors"}`` -- each a list of ``hypothesis_id`` (or, for
    ``errors``, ``{"hypothesis_id", "error"}`` dicts).
    """
    today = date.fromisoformat(timeutils.stamp(now).local_sgt[:10])
    summary: dict = {
        "evaluated": [], "not_yet_sufficient": [], "skipped_no_prereg": [],
        "synced": [], "refreshed": [], "errors": [],
    }

    testing_rows = journal.query(
        "SELECT * FROM hypothesis_proposals WHERE status = ?",
        (HypothesisStatus.TESTING.value,),
    )
    for row in testing_rows:
        hid = row["hypothesis_id"]
        try:
            _resolve_one(journal, row, today, summary)
        except PreregistrationAlreadyEvaluatedError:
            # Concurrent writer evaluated it between our read and write --
            # sync forward as "resolved" instead of surfacing an error.
            _mark_resolved(journal, hid, timeutils.stamp(now).utc)
            summary["synced"].append(hid)
        except Exception as exc:  # never let one bad row block the other 7
            summary["errors"].append({"hypothesis_id": hid, "error": str(exc)})

    summary["refreshed"] = _refresh_all_verdicts(journal)
    return summary


def _mark_resolved(journal, hypothesis_id: str, resolved_at_utc: Optional[str]) -> None:
    journal.conn.execute(
        "UPDATE hypothesis_proposals SET status = ?, resolved_at_utc = ? WHERE hypothesis_id = ?",
        (HypothesisStatus.RESOLVED.value, resolved_at_utc, hypothesis_id),
    )
    journal.conn.commit()


def _resolve_one(journal, row: dict, today: date, summary: dict) -> None:
    hid = row["hypothesis_id"]

    if not row["prereg_id"]:
        summary["skipped_no_prereg"].append(hid)
        return

    prereg = journal.one("SELECT * FROM preregistrations WHERE prereg_id = ?", (row["prereg_id"],))
    if prereg is None:
        summary["skipped_no_prereg"].append(hid)
        return

    if prereg.get("evaluated_at_utc"):
        # Already evaluated elsewhere (H-AI-1's own BASELINE evaluation
        # path, or a prior resolver run that wrote preregistrations but
        # crashed before updating this row) -- sync, never re-evaluate.
        _mark_resolved(journal, hid, prereg["evaluated_at_utc"])
        summary["synced"].append(hid)
        return

    if row["metric_fn_name"] is None:
        return  # H-AI-1-shaped row, nothing to compute here; wait for BASELINE's own path

    if today.isoformat() < row["analysis_not_before"]:
        return  # calendar floor not yet reached

    metric_fn = METRIC_FUNCTIONS.get(row["metric_fn_name"])
    if metric_fn is None:
        summary["errors"].append(
            {"hypothesis_id": hid, "error": f"unknown metric_fn_name {row['metric_fn_name']!r}"}
        )
        return

    rows, value_key, reference_arm_rows = metric_fn(journal)
    en = effective_n(rows)
    clears_floor = (
        en["effective_n"] >= prereg["floor_effective_n"]
        and (en["span_days"] or 0) >= prereg["floor_span_days"]
    )
    # FABLE5 STRATEGY REVIEW FIX (2026-07-10): the centered-delta design
    # (alphaos.hypotheses.queries's own module docstring) freezes one arm's
    # mean as a fixed constant, ignoring that arm's OWN sampling error -- an
    # ANTI-CONSERVATIVE lean (narrower CIs than warranted) that gets WORSE
    # the thinner the reference arm is. Apply the SAME floor to the
    # reference arm (reusing this hypothesis's own already-frozen numbers,
    # never a second, separately-tuned threshold) before evaluating --
    # h_rej_1_rows has no reference arm (reference_arm_rows is None) and is
    # exempt by construction, not by omission.
    if reference_arm_rows is not None:
        ref_en = effective_n(reference_arm_rows)
        clears_floor = clears_floor and (
            ref_en["effective_n"] >= prereg["floor_effective_n"]
            and (ref_en["span_days"] or 0) >= prereg["floor_span_days"]
        )
    if not clears_floor:
        summary["not_yet_sufficient"].append(hid)
        return

    evaluate_hypothesis(journal, row["prereg_id"], rows, value_key)
    _mark_resolved(journal, hid, timeutils.stamp().utc)
    summary["evaluated"].append(hid)


def _refresh_all_verdicts(journal) -> list[str]:
    """Recompute ``compute_verdicts()`` over the FULL evaluated
    ``preregistrations`` family and write ``last_verdict``/``last_q_value``/
    ``last_reason`` onto every ``hypothesis_proposals`` row with a matching
    ``prereg_id`` -- runs every pass regardless of whether anything new was
    evaluated this run (see module docstring)."""
    evaluated = journal.query("SELECT * FROM preregistrations WHERE evaluated_at_utc IS NOT NULL")
    if not evaluated:
        return []
    verdicts = compute_verdicts(evaluated)
    by_prereg = {v["prereg_id"]: v for v in verdicts}

    linked = journal.query(
        "SELECT hypothesis_id, prereg_id FROM hypothesis_proposals WHERE prereg_id IS NOT NULL"
    )
    refreshed = []
    for link in linked:
        v = by_prereg.get(link["prereg_id"])
        if v is None:
            continue
        journal.conn.execute(
            "UPDATE hypothesis_proposals SET last_verdict = ?, last_q_value = ?, last_reason = ? "
            "WHERE hypothesis_id = ?",
            (v["verdict"], v["q_value"], v["reason"], link["hypothesis_id"]),
        )
        refreshed.append(link["hypothesis_id"])
    journal.conn.commit()
    return refreshed
