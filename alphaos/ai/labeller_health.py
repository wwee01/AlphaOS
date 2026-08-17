"""Labeller / evaluator fail-safe health evaluation (VISIBILITY only).

Turns a fail-safe summary into an ok / warn / critical level + message when the
fail-safe rate is high. A failing labeller looks like a conservative reject
(``label_source=fail_safe`` floors the decision), so this is what makes a silent
block obvious. It NEVER changes any decision, gate, approval, or execution — it
only produces a human-facing status string. Pure + hermetic (no I/O, no API).

PRE-1a generalizes this module to grade the PRIMARY evaluator's
``openai_evaluations`` rows (the path that actually gates trades) the SAME way
it already grades the labeller's ``candidate_labels`` rows, rather than
forking a second copy of this logic. ``evaluate_failsafe_health`` itself was
already source-agnostic (it only ever looked at a pre-built summary dict); the
part that genuinely differed per source -- turning a row source into that
summary dict, and the human-facing noun in the message -- is what
``summarize_failsafe_rows``/``source_label`` below add.
"""

from __future__ import annotations

import json
from typing import Callable, Optional

from alphaos.constants import ReasonCode


def evaluate_failsafe_health(
    summary: dict,
    warn_rate: float,
    critical_rate: float,
    min_sample: int,
    source_label: str = "Labeller",
) -> dict:
    """Grade a fail-safe ``summary`` (from ``journal.labeller_source_summary``,
    or from ``summarize_failsafe_rows`` below for any other row source) against
    the configured thresholds.

    Returns ``{level, message, rate, sample, fail_safe, top_reason}`` where level
    is ``ok`` | ``warn`` | ``critical``. Below ``min_sample`` recent labels it
    stays ``ok`` with no message, so a tiny window never false-alarms.

    ``source_label`` names WHOSE fail-safe rate this is in the human-facing
    message (default ``"Labeller"`` preserves the exact pre-PRE-1a message
    text byte-for-byte for every existing caller that doesn't pass it) --
    PRE-1a's evaluator caller passes ``source_label="Evaluator"``.
    """
    total = int(summary.get("total", 0) or 0)
    rate = float(summary.get("fail_safe_rate", 0.0) or 0.0)
    fail = int(summary.get("fail_safe", 0) or 0)
    reasons = summary.get("by_failsafe_reason", {}) or {}
    top_reason = max(reasons, key=reasons.get) if reasons else None

    result = {
        "level": "ok",
        "message": None,
        "rate": rate,
        "sample": total,
        "fail_safe": fail,
        "top_reason": top_reason,
    }

    if total < max(1, int(min_sample)):
        result["note"] = f"insufficient sample ({total} < {min_sample})"
        return result

    if rate >= critical_rate:
        level = "critical"
    elif rate >= warn_rate:
        level = "warn"
    else:
        return result

    pct = round(rate * 100)
    reason_txt = f" Top reason: {top_reason}." if top_reason else ""
    result["level"] = level
    result["message"] = (
        f"{source_label} fail-safe rate is {pct}% ({fail}/{total} recent candidates) "
        f"[{level.upper()}].{reason_txt} May indicate API, token-budget, timeout, "
        f"or JSON-parse failure — the {source_label.lower()} is silently failing safe to reject."
    )
    return result


def summarize_failsafe_rows(
    rows: list,
    is_failsafe: Callable[[dict], bool],
    reason_of: Callable[[dict], Optional[str]],
) -> dict:
    """Generic version of ``JournalStore.labeller_source_summary``'s own
    counting logic (total / fail_safe / fail_safe_rate / by_failsafe_reason),
    usable against ANY row source via caller-supplied predicates -- this is
    the "take a row source" generalization PRE-1a asks for, so the SAME
    counting logic grades both the labeller's ``candidate_labels`` rows and
    the evaluator's ``openai_evaluations`` rows, never a forked second copy.

    ``is_failsafe(row) -> bool`` decides whether one row counts as a
    fail-safe; ``reason_of(row) -> Optional[str]`` extracts its reason label
    (only called for rows ``is_failsafe`` accepted -- ``None`` becomes
    ``"unknown"``, matching ``labeller_source_summary``'s own
    ``FailsafeReason.UNKNOWN`` fallback).
    """
    total = len(rows)
    fail = 0
    by_failsafe_reason: dict = {}
    for row in rows:
        if is_failsafe(row):
            fail += 1
            reason = reason_of(row) or "unknown"
            by_failsafe_reason[reason] = by_failsafe_reason.get(reason, 0) + 1
    return {
        "total": total,
        "fail_safe": fail,
        "fail_safe_rate": round(fail / total, 3) if total else 0.0,
        "by_failsafe_reason": by_failsafe_reason,
    }


def is_openai_reject_row(row: dict) -> bool:
    """PRE-1a: the evaluator-specific fail-safe predicate for
    ``summarize_failsafe_rows``, applied to ``openai_evaluations`` rows.

    True exactly when ``risk_flags_json`` contains ``OPENAI_REJECT`` -- the
    provider/pipeline itself failing (a live call exception, or a post_process
    step that hit an exception mid-pipeline). Deliberately NOT
    ``prompt_hash IS NULL`` alone: RR-floor (``REWARD_RISK_TOO_LOW``) and
    ``NO_ATR_DATA`` rejections are ALSO hash-less on this table (they are
    normal, working-as-designed model-level rejections, not provider
    failures) -- keying off a null hash would false-alarm on a perfectly
    healthy day and, worse, would have stayed SILENT during the six-day
    dead-provider incident this ticket responds to (every rejection during
    that window looked identical to an ordinary RR-floor/NO_ATR_DATA reject
    by that measure). ``risk_flags_json`` is the raw TEXT column as read from
    SQLite (JournalStore.query/one do not auto-decode ``*_json`` columns) --
    parsed defensively; a malformed value degrades to a plain substring
    check rather than raising.
    """
    raw = row.get("risk_flags_json")
    if not raw:
        return False
    if isinstance(raw, str):
        try:
            flags = json.loads(raw)
        except (TypeError, ValueError):
            return ReasonCode.OPENAI_REJECT.value in raw
    else:
        flags = raw
    return ReasonCode.OPENAI_REJECT.value in (flags or [])
