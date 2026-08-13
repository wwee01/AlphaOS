"""Output validation for no-news mode.

Prompt wording alone is not trusted. In no-news mode the model must not invent a
catalyst. We enforce that on the parsed output:

* ``news_sources`` must be empty,
* ``catalyst`` must be the sentinel (or empty/none),
* no invented-catalyst markers may appear in the reasoning/catalyst text.

If any of these fail, the evaluation is rejected with
``invented_catalyst_in_no_news_mode``.
"""

from __future__ import annotations

from typing import Optional

from alphaos.constants import (
    CATALYST_NOT_AVAILABLE_V1,
    FAILED_VALIDATION_INVENTED_CATALYST,
    INVENTED_CATALYST_MARKERS,
)

_ALLOWED_CATALYST = {"", "none", "n/a", "na", "null", "unavailable", CATALYST_NOT_AVAILABLE_V1}


def validate_no_news_eval(obj: dict) -> Optional[str]:
    """Return a failure reason string if the output invents a catalyst, else None."""
    sources = obj.get("news_sources")
    if sources:  # any non-empty source is an invented catalyst in no-news mode
        return FAILED_VALIDATION_INVENTED_CATALYST

    catalyst = str(obj.get("catalyst") or obj.get("catalyst_type") or "").strip().lower()
    if catalyst not in _ALLOWED_CATALYST:
        return FAILED_VALIDATION_INVENTED_CATALYST

    text = " ".join(
        str(obj.get(k, "")) for k in ("reasoning_summary", "catalyst", "catalyst_type", "thesis", "sentiment")
    ).lower()
    for marker in INVENTED_CATALYST_MARKERS:
        if marker in text:
            return FAILED_VALIDATION_INVENTED_CATALYST
    return None


def validate_max_holding_days_range(obj: dict, bound: int) -> Optional[str]:
    """HOLD-2, v4 ONLY: the parser's accepted range for ``max_holding_days``
    is 1..``bound``, where ``bound`` is the ACTIVE card's own
    ``max_holding_days_default`` (never a second hardcoded literal -- see
    docs/roadmap/alphaos-hold2-10day-window-spec.md section 3.3). Returns a
    failure reason string when the value is missing/non-integral/out of
    range, else None. Callers gate this to PROPOSE decisions only -- a
    reject/watch carries no real holding-window commitment.

    Audit-fixup HOLD-2 (MEDIUM-7, both audits convergent / STATUS
    CORRECTION item 6): only INTEGRAL values are accepted now -- plain
    ``int(value)`` silently TRUNCATED a fractional float (``int(10.9) ==
    10``), so a model response of ``10.9`` passed this check as if it had
    said ``10``, while the ORIGINAL ``10.9`` was left sitting in ``obj``
    for every downstream reader (audit B reproduced this concretely: a
    position held for 11 trading days off a ``10.9`` value that this
    validator's own success implied was "10"). Booleans are rejected too
    (``bool`` is an ``int`` subclass in Python -- ``True``/``False`` must
    never silently read as ``1``/``0``). On success, the validated
    ``int`` is written BACK into ``obj["max_holding_days"]`` -- what
    persists downstream is exactly what was validated, never a leftover
    float/string/bool the check merely tolerated."""
    value = obj.get("max_holding_days")
    if isinstance(value, bool):
        return f"max_holding_days {value!r} must be an integer, not a bool"
    if isinstance(value, int):
        value_int = value
    elif isinstance(value, float):
        if not value.is_integer():
            return f"max_holding_days {value!r} must be an integral value (no fractional part)"
        value_int = int(value)
    elif value is None:
        # mypy: int() itself has no None overload -- narrowed explicitly
        # here rather than leaning on the try/except below to catch it
        # (same outcome, same message, statically honest about the type).
        return f"max_holding_days {value!r} is missing or not an integer"
    else:
        try:
            value_int = int(value)
        except (TypeError, ValueError):
            return f"max_holding_days {value!r} is missing or not an integer"
    if not (1 <= value_int <= bound):
        return f"max_holding_days {value_int} outside the allowed range 1-{bound}"
    obj["max_holding_days"] = value_int
    return None


def enforce_no_news_sentinels(obj: dict) -> dict:
    """Hard-set the no-news sentinels on a (validated) output object."""
    obj = dict(obj)
    obj["catalyst"] = CATALYST_NOT_AVAILABLE_V1
    obj["catalyst_type"] = CATALYST_NOT_AVAILABLE_V1
    obj["news_status"] = "disabled_v1"
    obj["news_sources"] = []
    return obj
