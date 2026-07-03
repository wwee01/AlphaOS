"""Scheduler v1.5 AI cost guard (PR3).

Tracks trailing-30-day real (non-mock) OpenAI call volume against
``settings.scheduler_ai_cost_cap_calls_per_30d`` so a scheduled scan can refuse
to spend further AI budget once the cap is reached. Advisory/gate-only: this
module never blocks anything itself, it only reports (bool, detail) for the
caller (a later stage's job runner) to act on.
"""

from __future__ import annotations

from datetime import timedelta

from alphaos.util import timeutils

_TRAILING_DAYS = 30


def calls_in_last_30_days(journal) -> int:
    """Count of real (non-mock) OpenAI evaluations in the trailing 30 days."""
    since = timeutils.to_iso(timeutils.now_utc() - timedelta(days=_TRAILING_DAYS))
    return journal.count_rows(
        "openai_evaluations",
        "is_mock = 0 AND created_at_utc >= ?",
        (since,),
    )


def check_scan_budget(settings, journal) -> tuple[bool, str]:
    """Whether the trailing-30-day AI cost cap still has room. Never raises.

    Returns (False, detail) once the cap is reached/exceeded, (True, detail)
    otherwise. ``detail`` is always a human-readable "N/cap OpenAI calls used
    in trailing 30 days" string.
    """
    cap = settings.scheduler_ai_cost_cap_calls_per_30d
    try:
        used = calls_in_last_30_days(journal)
    except Exception as exc:  # never crash the caller -- fail toward "don't run"
        return (False, f"error checking AI cost cap: {exc}")

    detail = f"{used}/{cap} OpenAI calls used in trailing 30 days"
    if used >= cap:
        return (False, f"{detail} -- cap reached")
    return (True, detail)
