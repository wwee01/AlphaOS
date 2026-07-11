"""Scheduler v1.5 AI cost guard (PR3; true-up in PR9.5; CANARY + PR14 each
add a term -- see ``calls_in_last_30_days``'s own docstring for the current
count).

Tracks trailing-30-day real (non-mock) AI call volume against
``settings.scheduler_ai_cost_cap_calls_per_30d`` so a scheduled scan can refuse
to spend further AI budget once the cap is reached. Advisory/gate-only: this
module never blocks anything itself, it only reports (bool, detail) for the
caller (a later stage's job runner) to act on.

PR9.5 true-up: this originally counted ONLY ``openai_evaluations`` (the
primary evaluator), silently missing the labeller (``candidate_labels``) and
narrative-polarity (``last30days_polarity``) calls added by later PRs -- each
a genuinely separate real OpenAI API call, undercounting real spend 2-3x
(2026-07-06 exit review finding). All now count toward the same cap.

PR14 adds ``agent_votes`` (the bear-debate agent) here AND a separate,
TIGHTER ``check_debate_budget``/daily sub-cap -- nested INSIDE this shared
cap, not a replacement for it, so a new shadow feature can never starve the
live evaluator's own share of the shared 30-day budget.

HGEN-1 adds ``hypothesis_drafts`` (the LLM hypothesis generator) here AND
its own separate, TIGHTER ``check_hypothesis_gen_budget``/daily sub-cap --
same nested-cap pattern as PR14's bear-debate, never a replacement for the
shared cap.
"""

from __future__ import annotations

from datetime import timedelta

from alphaos.util import timeutils

_TRAILING_DAYS = 30


def calls_in_last_30_days(journal) -> int:
    """Count of real (non-mock) AI calls across all seven call sites in the
    trailing 30 days: the primary evaluator, the playbook labeller, the
    narrative-polarity classifier, EVAL-1's offline replay harness, CANARY's
    weekly drift replay, PR14's bear-debate agent, and HGEN-1's hypothesis
    generator -- each a separate real API call that should count toward the
    same spend cap.

    ``last30days_polarity`` has no ``is_mock`` column (a PR4-era omission,
    not reproduced here); ``model_provider`` is only ever populated by a real
    live API call (``lineage.ai_call_lineage``'s own "openai" stamp) -- every
    mock/skipped/error path leaves it NULL, so ``model_provider IS NOT NULL``
    is an equally precise real-call filter without a schema change.

    EVAL-1's ``eval_results`` reuses the SAME ``PlaybookClassifier.classify()``
    call as ``candidate_labels`` (a genuinely separate invocation, not a
    duplicate of an existing row), so omitting it here would repeat the exact
    2026-07-06 exit-review undercount finding a third time. CANARY's
    ``canary_results`` reuses the same call again (a weekly replay, same
    rationale) -- omitting it would make this a fourth recurrence of the
    identical bug class.
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
    # canary_results has no per-row is_mock column (a run is either fully
    # mock or fully live, never mixed -- see canary_runs.is_mock) so the
    # mock filter joins through the owning run instead of a local column.
    canary_replays = journal.count_rows(
        "canary_results",
        "created_at_utc >= ? AND run_id IN (SELECT run_id FROM canary_runs WHERE is_mock = 0)",
        (since,),
    )
    debate_votes = journal.count_rows(
        "agent_votes", "is_mock = 0 AND created_at_utc >= ?", (since,),
    )
    # hypothesis_drafts has no is_mock column (same rationale as
    # last30days_polarity above): model_provider is only ever populated by
    # HypothesisGenerator._live_generate()'s real API call -- every mock/
    # error path leaves it NULL, so this is an equally precise real-call
    # filter without a schema change.
    hypothesis_gen_calls = journal.count_rows(
        "hypothesis_drafts", "model_provider IS NOT NULL AND created_at_utc >= ?", (since,),
    )
    return (
        evaluations + labels + polarity + eval_replays + canary_replays
        + debate_votes + hypothesis_gen_calls
    )


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


def debate_calls_today(journal) -> int:
    """Real (non-mock) bear-debate calls since the start of the current
    trading day. Uses ``journal.start_of_trading_day_utc()`` -- the same
    trading-day-aligned "today" boundary already used by
    ``count_auto_approvals_today``/``count_paper_orders_today`` -- rather
    than naive UTC midnight, so the daily cap resets in step with every
    other daily cap in this codebase.
    """
    start = journal.start_of_trading_day_utc()
    return journal.count_rows(
        "agent_votes", "is_mock = 0 AND created_at_utc >= ?", (start,),
    )


def check_debate_budget(settings, journal) -> tuple[bool, str]:
    """Whether today's bear-debate sub-cap (``settings.debate_max_calls_per_day``)
    still has room. This is a SEPARATE, TIGHTER cap nested INSIDE the shared
    30-day cap above -- passing this check does not imply the shared cap has
    room; a caller that wants both must check both (see
    ``alphaos/debate/batch.py``'s own call site). Never raises; fails toward
    "don't run."
    """
    cap = settings.debate_max_calls_per_day
    try:
        used = debate_calls_today(journal)
    except Exception as exc:  # never crash the caller -- fail toward "don't run"
        return (False, f"error checking debate daily cap: {exc}")

    detail = f"{used}/{cap} bear-debate calls used today"
    if used >= cap:
        return (False, f"{detail} -- cap reached")
    return (True, detail)


def hypothesis_gen_calls_today(journal) -> int:
    """Real (non-mock, i.e. ``model_provider IS NOT NULL``) hypothesis-
    generation calls since the start of the current trading day. Counts
    GENERATION ATTEMPTS (one ``hypothesis_drafts`` row per candidate a live
    LLM call actually produced -- accepted, rejected, or still pending
    review all count, since the cost was already spent regardless of what
    later happens to the draft), not accepted/reviewed drafts. Same
    trading-day-aligned "today" boundary as ``debate_calls_today``."""
    start = journal.start_of_trading_day_utc()
    return journal.count_rows(
        "hypothesis_drafts", "model_provider IS NOT NULL AND created_at_utc >= ?", (start,),
    )


def check_hypothesis_gen_budget(settings, journal) -> tuple[bool, str]:
    """Whether today's hypothesis-generation sub-cap
    (``settings.hypothesis_gen_max_calls_per_day``) still has room. A
    SEPARATE, TIGHTER cap nested INSIDE the shared 30-day cap above --
    passing this check does not imply the shared cap has room; a caller
    that wants both must check both (mirrors ``check_debate_budget``'s own
    docstring). Never raises; fails toward "don't run."""
    cap = settings.hypothesis_gen_max_calls_per_day
    try:
        used = hypothesis_gen_calls_today(journal)
    except Exception as exc:  # never crash the caller -- fail toward "don't run"
        return (False, f"error checking hypothesis-generation daily cap: {exc}")

    detail = f"{used}/{cap} hypothesis-generation calls used today"
    if used >= cap:
        return (False, f"{detail} -- cap reached")
    return (True, detail)
