"""Scheduler v1.5 AI cost guard (PR3; true-up in PR9.5).

Tracks trailing-30-day real (non-mock) OpenAI call volume against
``settings.scheduler_ai_cost_cap_calls_per_30d`` so a scheduled scan can refuse
to spend further AI budget once the cap is reached. Advisory/gate-only: this
module never blocks anything itself, it only reports (bool, detail) for the
caller (a later stage's job runner) to act on.

PR9.5 true-up: this originally counted ONLY ``openai_evaluations`` (the
primary evaluator), silently missing the labeller (``candidate_labels``) and
narrative-polarity (``last30days_polarity``) calls added by later PRs -- each
a genuinely separate real OpenAI API call, undercounting real spend 2-3x
(2026-07-06 exit review finding). All three now count toward the same cap.
"""

from __future__ import annotations

from datetime import timedelta

from alphaos.util import timeutils

_TRAILING_DAYS = 30


def calls_in_last_30_days(journal) -> int:
    """Count of real (non-mock) AI calls across all four call sites in the
    trailing 30 days: the primary evaluator, the playbook labeller, the
    narrative-polarity classifier, and EVAL-1's offline replay harness --
    each a separate real OpenAI API call that should count toward the same
    spend cap.

    ``last30days_polarity`` has no ``is_mock`` column (a PR4-era omission,
    not reproduced here); ``model_provider`` is only ever populated by a real
    live API call (``lineage.ai_call_lineage``'s own "openai" stamp) -- every
    mock/skipped/error path leaves it NULL, so ``model_provider IS NOT NULL``
    is an equally precise real-call filter without a schema change.

    EVAL-1's ``eval_results`` reuses the SAME ``PlaybookClassifier.classify()``
    call as ``candidate_labels`` (a genuinely separate invocation, not a
    duplicate of an existing row), so omitting it here would repeat the exact
    2026-07-06 exit-review undercount finding a third time.
    """
    since = timeutils.to_iso(timeutils.now_utc() - timedelta(days=_TRAILING_DAYS))
    evaluations = journal.count_rows(
        "openai_evaluations", "is_mock = 0 AND created_at_utc >= ?", (since,),
    )
    labels = journal.count_rows(
        "candidate_labels", "is_mock = 0 AND created_at_utc >= ?", (since,),
    )
    polarity = journal.count_rows(
        "last30days_polarity", "model_provider IS NOT NULL AND created_at_utc >= ?", (since,),
    )
    eval_replays = journal.count_rows(
        "eval_results", "is_mock = 0 AND created_at_utc >= ?", (since,),
    )
    return evaluations + labels + polarity + eval_replays


def check_scan_budget(settings, journal) -> tuple[bool, str]:
    """Whether the trailing-30-day AI cost cap still has room. Never raises.

    Returns (False, detail) once the cap is reached/exceeded, (True, detail)
    otherwise. ``detail`` is always a human-readable "N/cap real AI calls used
    in trailing 30 days" string.
    """
    cap = settings.scheduler_ai_cost_cap_calls_per_30d
    try:
        used = calls_in_last_30_days(journal)
    except Exception as exc:  # never crash the caller -- fail toward "don't run"
        return (False, f"error checking AI cost cap: {exc}")

    detail = f"{used}/{cap} real AI calls used in trailing 30 days"
    if used >= cap:
        return (False, f"{detail} -- cap reached")
    return (True, detail)
