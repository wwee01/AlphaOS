"""WATCH-1 prospective decision diagnostic. This module cannot route orders.

Only a validated long primary PROPOSE vetoed to WATCH by a valid classifier
WATCH is changed in the hypothetical decision. The caller always retains the
actual combined decision. A hypothetical PROPOSE is before portfolio, risk,
approval, and execution checks; it is neither a fill nor a profit estimate.
"""

from __future__ import annotations

import math

from alphaos.constants import LABEL_OTHER, OFFICIAL_LABELS


POLICY_VERSION = "long_watch_advisory_v1"
PROTOCOL_ID = "WATCH-1-20260913"


def _finite(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def decision_diagnostic(evaluation, classification, current_decision: str, *,
                        confidence_floor: float, min_reward_risk: float) -> dict:
    """Snapshot the narrow alternative without modifying any input object.

    Fail closed on missing/coerced/invalid label output, invalid primary plans,
    non-finite prices and sub-floor confidence/RR. Existing explicit REJECT and
    all other decision branches remain identical. Downstream gates are not
    simulated here. The complete portfolio comparison is a separate deliverable.
    """
    base = getattr(evaluation, "decision", None)
    label = getattr(classification, "label_decision", None)
    entry = _finite(getattr(evaluation, "entry", None))
    stop = _finite(getattr(evaluation, "stop", None))
    target = _finite(getattr(evaluation, "target", None))
    confidence = _finite(getattr(classification, "confidence", None))
    raw = getattr(classification, "raw", None)
    raw_confidence = raw.get("confidence") if isinstance(raw, dict) else None
    # The old validator rounds/clamps WATCH confidence and can turn True,
    # NaN or >1 into 1.0 with status passed. Do not let this new diagnostic
    # mistake that coercion (or .4999 rounded to .500) for valid evidence.
    original_confidence = (_finite(raw_confidence)
                           if isinstance(raw_confidence, (int, float)) else None)
    floor = _finite(confidence_floor)
    rr_floor = _finite(min_reward_risk)
    hold = getattr(evaluation, "max_holding_days", None)
    reasons = []
    if not (base == "propose" and label == "watch" and current_decision == "watch"):
        reasons.append("outside_long_propose_watch_veto")
    if getattr(evaluation, "direction", None) != "long":
        reasons.append("primary_not_long")
    if getattr(evaluation, "validation_status", None) != "passed":
        reasons.append("primary_validation_not_passed")
    if getattr(evaluation, "data_freshness_status", None) != "usable":
        reasons.append("primary_data_not_usable")
    valid_levels = (entry is not None and stop is not None and target is not None
                    and 0 < stop < entry < target)
    if not valid_levels:
        reasons.append("invalid_long_levels")
    elif rr_floor is None or rr_floor <= 0 or (target - entry) / (entry - stop) < rr_floor:
        reasons.append("reward_risk_below_floor_or_unknown")
    if not isinstance(hold, int) or isinstance(hold, bool) or hold <= 0:
        reasons.append("invalid_holding_window")
    if getattr(classification, "validation_status", None) != "passed":
        reasons.append("classifier_validation_not_passed")
    primary_label = getattr(classification, "primary_label", None)
    if primary_label not in OFFICIAL_LABELS or primary_label == LABEL_OTHER:
        reasons.append("classifier_not_official_setup")
    if (confidence is None or floor is None or not 0 <= floor <= 1
            or not floor <= confidence <= 1):
        reasons.append("classifier_confidence_below_floor_or_unknown")
    if (original_confidence is None or floor is None or not 0 <= floor <= 1
            or not floor <= original_confidence <= 1):
        reasons.append("raw_classifier_confidence_invalid_below_floor_or_missing")

    changed = not reasons
    return {
        "protocol_id": PROTOCOL_ID,
        "policy_version": POLICY_VERSION,
        "mode": "decision_diagnostic_only",
        "eval_id": getattr(evaluation, "eval_id", None),
        "label_id": getattr(classification, "label_id", None),
        "is_mock": not (getattr(evaluation, "is_mock", None) is False
                        and getattr(classification, "is_mock", None) is False),
        "primary_decision": base,
        "classifier_decision": label,
        "current_decision": current_decision,
        "challenger_decision_before_risk": "propose" if changed else current_decision,
        "changed": changed,
        "exclusion_reasons": reasons,
        "entry": entry, "stop": stop, "target": target,
        "max_holding_days": (hold if isinstance(hold, int) and not isinstance(hold, bool)
                             and hold > 0 else None),
        "direction": getattr(evaluation, "direction", None),
        "classifier_confidence": confidence,
        "raw_classifier_confidence": original_confidence,
        "confidence_floor": floor,
        "min_reward_risk": rr_floor,
        "downstream_gates": "not_evaluated",
        "portfolio_outcome": "not_measured",
    }
