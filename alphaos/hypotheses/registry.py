"""PR12: the ``hypothesis_proposals`` registry wrapper.

``propose_hypothesis()`` is the ONLY way a PR12 hypothesis gets its
``floor_effective_n``/``floor_span_days`` -- callers never supply a floor
directly. Risk class mechanically determines it via ``RISK_CLASS_FLOORS``
(Fable5 ruling, 2026-07-10, see constants.py's own module docstring), so no
proposal can ever undercut its own class's bar by construction.

H-AI-1 is special-cased here: it has no ``metric_fn_name`` and is never
independently registered -- it links ``prereg_id`` to BASELINE's own
existing ``preregistrations`` row (found by the exact hypothesis+metric text
match ``cmd_baseline_register()`` itself uses) so PR12 surfaces BASELINE's
existing evidence without ever creating a second row or a second evaluation
path for the same claim.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from typing import Optional

from alphaos.hypotheses.constants import (
    RISK_CLASS_FLOORS,
    SEEDED_HYPOTHESES,
    HypothesisStatus,
)
from alphaos.stats.preregistration import register_hypothesis
from alphaos.util import timeutils

# Verbatim strings cmd_baseline_register() registers BASELINE's own
# preregistrations row with -- duplicated here deliberately rather than
# imported, since __main__.py's cmd_baseline_register() defines them as
# local variables, not module-level exports; a text-match lookup is the
# only stable link between the two features (BASELINE predates PR12 and
# owns that row's registration, PR12 only ever reads it).
_BASELINE_HYPOTHESIS_TEXT = (
    "AI adds >= +0.05R mean paired ai_delta_r over threshold_v1 on "
    "proposed candidates, conditional on labeller reach"
)
_BASELINE_METRIC_TEXT = (
    "mean_ai_delta_r = mean(candidate_outcomes.replay_r - "
    "shadow_baseline_decisions.replay_r), threshold_v1"
)


def _find_baseline_prereg_id(journal) -> Optional[str]:
    row = journal.one(
        "SELECT prereg_id FROM preregistrations WHERE hypothesis = ? AND metric = ?",
        (_BASELINE_HYPOTHESIS_TEXT, _BASELINE_METRIC_TEXT),
    )
    return row["prereg_id"] if row else None


def _default_analysis_not_before(risk_class: str, now: Optional[datetime] = None) -> str:
    """Today (SGT calendar date) plus this risk class's own ``min_span_days``
    -- the earliest day the sample could possibly clear its own span floor
    anyway, so the wait period is structurally tied to the floor table
    rather than a second, independently-chosen literal. ``now`` follows the
    same injectable-clock idiom as ``alphaos.scheduler.cadence`` (tests pass
    a fixed instant instead of mocking the system clock)."""
    today = date.fromisoformat(timeutils.stamp(now).local_sgt[:10])
    floors = RISK_CLASS_FLOORS[risk_class]
    return (today + timedelta(days=floors["min_span_days"])).isoformat()


def propose_hypothesis(journal, spec: dict, now: Optional[datetime] = None) -> dict:
    """Create ONE ``hypothesis_proposals`` row from a ``SEEDED_HYPOTHESES``-
    shaped ``spec`` dict, adding the idempotency ``register_hypothesis()``
    itself deliberately lacks (matches ``cmd_baseline_register()``'s own
    idiom: check-then-register, never register blindly). No-ops (returns
    the existing row) if ``hypothesis_id`` is already registered.

    H-AI-1 (``spec['metric_fn_name'] is None``): looks up BASELINE's own
    ``prereg_id`` by text match instead of calling ``register_hypothesis()``.
    If BASELINE hasn't been registered yet, ``prereg_id`` stays ``None`` and
    ``status`` stays ``proposed`` -- the resolver skips rows with no
    ``prereg_id`` rather than treating that as an error (BASELINE's own
    registration is outside this module's control and may simply not have
    run yet).

    Correctness-audit LOW-1: the check-then-insert above has a narrow race
    window between two truly-concurrent callers (not reachable under this
    codebase's real single-nightly-scheduler-job model, since JobRunner's
    own lock_key already serializes same-job-type runs -- see
    job_runner.py's own ``acquire()`` docstring for the identical class of
    race). ``hypothesis_id``'s DB-level UNIQUE constraint is the real
    backstop: on an IntegrityError from that race, re-SELECT and return the
    winner's row rather than raising -- the same "partial index/unique
    constraint catches the loser, treat it as already-locked" idiom
    ``JobRunner.acquire()`` uses for its own lock_key race.
    """
    existing = journal.one(
        "SELECT * FROM hypothesis_proposals WHERE hypothesis_id = ?",
        (spec["hypothesis_id"],),
    )
    if existing:
        return existing

    risk_class = spec["risk_class"]
    floors = RISK_CLASS_FLOORS[risk_class]
    analysis_not_before = _default_analysis_not_before(risk_class, now)

    if spec["metric_fn_name"] is None:
        prereg_id = _find_baseline_prereg_id(journal)
    else:
        prereg_id = register_hypothesis(
            journal,
            hypothesis=spec["claim"],
            metric=spec["metric"],
            floor_effective_n=floors["min_sample"],
            floor_span_days=floors["min_span_days"],
            analysis_not_before=analysis_not_before,
            params={"metric_fn_name": spec["metric_fn_name"], "card_id": spec.get("card_id")},
        )

    try:
        journal.insert("hypothesis_proposals", {
            "hypothesis_id": spec["hypothesis_id"],
            "risk_class": risk_class,
            "claim": spec["claim"],
            "metric_description": spec["metric"],
            "success_floor": spec["success_floor"],
            "metric_fn_name": spec["metric_fn_name"],
            "card_id": spec.get("card_id"),
            "prereg_id": prereg_id,
            "status": (
                HypothesisStatus.TESTING.value if prereg_id else HypothesisStatus.PROPOSED.value
            ),
            "analysis_not_before": analysis_not_before,
        })
    except sqlite3.IntegrityError:
        # Lost a race against a concurrent seeder for this hypothesis_id --
        # the prereg_id/preregistrations row registered just above is now
        # orphaned (harmless: it is simply never linked from any
        # hypothesis_proposals row), and the winner's row is authoritative.
        winner = journal.one(
            "SELECT * FROM hypothesis_proposals WHERE hypothesis_id = ?",
            (spec["hypothesis_id"],),
        )
        if winner is not None:
            return winner
        raise  # genuinely unexpected -- re-raise rather than return None
    return journal.one(
        "SELECT * FROM hypothesis_proposals WHERE hypothesis_id = ?",
        (spec["hypothesis_id"],),
    )


def seed_all(journal, now: Optional[datetime] = None) -> list[dict]:
    """Propose every ``SEEDED_HYPOTHESES`` entry (idempotent per-row --
    calling this on every startup/scheduler tick is safe)."""
    return [propose_hypothesis(journal, spec, now) for spec in SEEDED_HYPOTHESES]
