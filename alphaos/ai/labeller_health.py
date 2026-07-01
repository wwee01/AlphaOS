"""Labeller fail-safe health evaluation (VISIBILITY only).

Turns a labeller-source summary into an ok / warn / critical level + message when
the fail-safe rate is high. A failing labeller looks like a conservative reject
(``label_source=fail_safe`` floors the decision), so this is what makes a silent
block obvious. It NEVER changes any decision, gate, approval, or execution — it
only produces a human-facing status string. Pure + hermetic (no I/O, no API).
"""

from __future__ import annotations


def evaluate_failsafe_health(
    summary: dict,
    warn_rate: float,
    critical_rate: float,
    min_sample: int,
) -> dict:
    """Grade a labeller-source ``summary`` (from ``journal.labeller_source_summary``)
    against the configured thresholds.

    Returns ``{level, message, rate, sample, fail_safe, top_reason}`` where level
    is ``ok`` | ``warn`` | ``critical``. Below ``min_sample`` recent labels it
    stays ``ok`` with no message, so a tiny window never false-alarms.
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
        f"Labeller fail-safe rate is {pct}% ({fail}/{total} recent candidates) "
        f"[{level.upper()}].{reason_txt} May indicate API, token-budget, timeout, "
        f"or JSON-parse failure — the labeller is silently failing safe to reject."
    )
    return result
