"""PR-UI-B2: a broader-window TQS shadow-scoring summary for the Learning
tab's TQS panel -- bucket histogram, mean data_confidence, and per-component
availability rates, over the most recent ``limit`` candidate-level scores
(not the daily digest's "since midnight SGT" window; a Learning-tab operator
wants a representative recent distribution, not just today's).

PURE READ over ``tqs_scores`` -- same measurement-only posture as
``alphaos/tqs/`` itself and ``alphaos/scheduler/digest.py``'s own
``tqs_shadow`` block (this module deliberately does not import or modify
that block: same shape, different window, kept as two small, independently
readable functions rather than one over-parameterized shared one -- see
``alphaos/scheduler/digest.py`` for the "since midnight" daily-digest
version). Nothing here is read by any gate/eval/labeller/risk/execution
path (PR7 TQS v0's own shadow-measurement boundary).

Mock rows (``is_mock=1``) are EXCLUDED from the bucket histogram and mean
confidence (UI/UX doc §1.4 evidence-state honesty: mock/paper data is never
styled identically to live/proven data) but their count is always surfaced
separately, never silently dropped.
"""

from __future__ import annotations

import json
from typing import Optional

# The 7 named components a real (non-degenerate) TQS computation can carry --
# read directly off alphaos/tqs/scoring.py's own WEIGHTS dict keys (the same
# dispatch table compute_tqs() iterates), never a second, hand-maintained
# list that could drift from the real component set.
from alphaos.tqs.scoring import WEIGHTS as _TQS_COMPONENT_WEIGHTS

_COMPONENT_NAMES: tuple[str, ...] = tuple(_TQS_COMPONENT_WEIGHTS.keys())


def _parse_json_col(value) -> dict:
    """tqs_scores.components_json/missing_components_json come back as raw
    JSON TEXT from journal.query() (JournalStore never auto-deserializes a
    ``*_json`` column on read -- only on write); a NULL/empty/malformed value
    degrades to an empty dict rather than raising, matching this module's
    "never fabricate, never crash on a display-only read" posture."""
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_tqs_report(journal, limit: int = 1000) -> dict:
    """Most recent ``limit`` candidate-level tqs_scores rows (source_type=
    'candidate' -- excludes any proposal-level re-score rows, matching the
    daily digest's own scope). Returns:
    ``{"scored_count", "mock_excluded_count", "bucket_histogram",
    "mean_data_confidence", "component_availability"}`` -- the last is
    ``{component_name: {"available": n, "missing": n, "availability_rate"}}``
    over the SAME live (non-mock) row set the histogram/confidence use, so
    every number on this report is drawn from one consistent slice."""
    rows = journal.query(
        "SELECT tqs_bucket, data_confidence, is_mock, components_json, missing_components_json "
        "FROM tqs_scores WHERE source_type = 'candidate' ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    live_rows = [r for r in rows if not r.get("is_mock")]
    mock_excluded_count = len(rows) - len(live_rows)

    bucket_histogram: dict = {}
    for r in live_rows:
        bucket_histogram[r["tqs_bucket"]] = bucket_histogram.get(r["tqs_bucket"], 0) + 1

    confidences = [r["data_confidence"] for r in live_rows if r.get("data_confidence") is not None]
    mean_data_confidence = round(sum(confidences) / len(confidences), 2) if confidences else None

    availability: dict[str, dict] = {
        name: {"available": 0, "missing": 0} for name in _COMPONENT_NAMES
    }
    for r in live_rows:
        available = _parse_json_col(r.get("components_json"))
        missing = _parse_json_col(r.get("missing_components_json"))
        for name in _COMPONENT_NAMES:
            if name in available:
                availability[name]["available"] += 1
            elif name in missing:
                availability[name]["missing"] += 1
            # neither present nor missing (an older/short row) -- silently
            # excluded from this component's own denominator, never counted
            # as either available or missing (uncountable, not fabricated).

    component_availability: dict[str, dict] = {}
    for name, counts in availability.items():
        total = counts["available"] + counts["missing"]
        rate: Optional[float] = round(counts["available"] / total, 2) if total else None
        component_availability[name] = {
            "available": counts["available"], "missing": counts["missing"],
            "availability_rate": rate,
        }

    return {
        "scored_count": len(live_rows),
        "mock_excluded_count": mock_excluded_count,
        "bucket_histogram": bucket_histogram,
        "mean_data_confidence": mean_data_confidence,
        "component_availability": component_availability,
    }
