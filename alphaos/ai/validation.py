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


def enforce_no_news_sentinels(obj: dict) -> dict:
    """Hard-set the no-news sentinels on a (validated) output object."""
    obj = dict(obj)
    obj["catalyst"] = CATALYST_NOT_AVAILABLE_V1
    obj["catalyst_type"] = CATALYST_NOT_AVAILABLE_V1
    obj["news_status"] = "disabled_v1"
    obj["news_sources"] = []
    return obj
