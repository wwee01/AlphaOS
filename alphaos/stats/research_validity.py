"""Executable quarantine for the contracts invalidated by the September audit.

This is not an alternative statistical verdict or a successor activation
mechanism. Historical evidence remains readable. No parameter/approval flag
can remove these identified contract defects from the original registration.
Pure descriptive calculations remain available outside the writing APIs.
"""

from __future__ import annotations

import json


class ResearchContractQuarantinedError(ValueError):
    pass


_METRIC_REASONS = {
    "h_tqs_1_rows": "target, reference uncertainty, mature 3-day cohort and actual overlap intervals require a reviewed successor",
    "h_cat_1_rows": "target, reference uncertainty, sparse controls and mixed replay horizons require a reviewed successor",
    "h_int_1_rows": "population-median uncertainty, prospective cohort and actual overlap intervals require a reviewed successor",
    "h_win_1_rows": "session definition, reference uncertainty and overstated independence require a reviewed successor",
    "h_ttl_1_rows": "equivalence margin, comparable outcomes and missing expired arm require a reviewed successor",
    "h_rej_1_rows": "negative direction, actual overlap intervals and frozen cohort require a reviewed successor",
    "h_pol_1_rows": "producer vocabulary changed; direction, both-arm uncertainty and frozen cohort require a reviewed successor",
}
_IDS = {f"H-{name.upper()}-1": f"h_{name}_1_rows" for name in ("tqs", "cat", "int", "win", "ttl", "rej", "pol")}


def registration_validity(row: dict, proposal: dict | None = None) -> dict:
    """Identify audited contracts by executable function or registered text.

    Other contracts are *not flagged by this audit*, not certified valid.
    This intentionally does not offer an allow/force flag for old contracts.
    """
    try:
        params = row.get("params_json") or {}
        params = json.loads(params) if isinstance(params, str) else params
        if not isinstance(params, dict):
            raise ValueError
    except (ValueError, TypeError):
        return {"quarantined": True, "reason_code": "INVALID_REGISTRATION_PARAMS",
                "reason": "registration parameters cannot be verified"}
    proposal = proposal or {}
    fn = params.get("metric_fn_name") or proposal.get("metric_fn_name") or _IDS.get(proposal.get("hypothesis_id") or "")
    metric = row.get("metric") or ""
    hypothesis = row.get("hypothesis") or ""
    reason = _METRIC_REASONS.get(fn or "")
    if not reason and ("ai_delta_r" in metric or proposal.get("hypothesis_id") == "H-AI-1"):
        reason = "AI policy/cohort, target, replay maturity/horizons and day-block finalization require a reviewed successor"
    if not reason and (metric.startswith("per_excess_") or hypothesis.startswith("H-PER-")
                       or params.get("card_id") == "post_earnings_reaction"):
        reason = "PER first-event chronology changed; original/v2 registrations cannot freeze the repaired event population"
    if not reason and params.get("shadow_tier") == 1:
        reason = "shadow selection/cohort and final evaluator contract require review before freezing evidence"
    return {
        "quarantined": reason is not None,
        "reason_code": "AUDITED_CONTRACT_REQUIRES_SUCCESSOR" if reason else None,
        "reason": reason or "not flagged by this audit; this is not a validity certification",
    }


def finalization_validity(journal, row: dict) -> dict:
    proposal = journal.one(
        "SELECT hypothesis_id,metric_fn_name FROM hypothesis_proposals WHERE prereg_id=?",
        (row.get("prereg_id"),),
    )
    return registration_validity(row, proposal)


def require_finalization_allowed(journal, row: dict) -> None:
    validity = finalization_validity(journal, row)
    if validity["quarantined"]:
        raise ResearchContractQuarantinedError(
            f"{validity['reason_code']}: {validity['reason']}. Preserve this registration; "
            "draft a corrected prospective contract for review. No evidence was written."
        )
