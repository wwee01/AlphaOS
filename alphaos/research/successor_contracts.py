"""Successor draft catalog. Original registrations are evidence, never edited.

Targets absent from the original text remain None. A statistical primitive
being implemented does not resolve population, measurement or policy choices.
"""
from __future__ import annotations

from copy import deepcopy

VERSION = "research_successor_drafts_20260928_v1"

# prereg_id: name, statistic, direction, target, horizon, choices
_SPECS = {
    "prereg_e4900d5ee489": ("H-AI-1", "paired_mean", "greater", .05, None,
        ["meaning_of_proposed_primary_or_final", "v1_horizon", "registry_analysis_date_conflict"]),
    "prereg_bb6dc46834dd": ("H-TQS-1", "independent_means", "greater", .30, 3,
        ["score_population_and_frozen_rank_tie_rule"]),
    "prereg_7d9799b146e1": ("H-CAT-1", "independent_means", "greater", .20, None,
        ["horizon", "card_scope_and_searched_control_availability"]),
    "prereg_debff71c8fe4": ("H-INT-1", "top_mean_minus_population_median", "greater", 0., None,
        ["horizon", "score_population_and_frozen_rank_tie_rule"]),
    "prereg_76c33938c0a4": ("H-WIN-1", "independent_means", "two_sided", 0., None,
        ["literal_SGT_windows_or_new_ET_windows", "horizon", "useful_effect_if_required"]),
    "prereg_09080b322ff2": ("H-TTL-1", "independent_means", "equivalent", None, None,
        ["equivalence_margin", "counterfactual_vs_realized_cost_basis", "horizon"]),
    "prereg_37ec53a06798": ("H-REJ-1", "single_mean", "less", 0., None,
        ["actual_minus_proposed_sign_and_policy_interpretation", "horizon"]),
    "prereg_dfc7ac34b379": ("H-POL-1", "independent_means", "less", 0., None,
        ["approve_conflicting_replacing_divergent", "horizon", "classifier_identity_and_missingness"]),
    "prereg_09365c29313f": ("H-INT-SHADOW-1", "top_mean_minus_population_median", "greater", 0., None,
        ["horizon", "score_population_and_frozen_rank_tie_rule", "shadow_collection_under_existing_gates"]),
    "prereg_89fcdf3974ff": ("H-AI-SHADOW-1", "paired_mean", None, None, None,
        ["direction_and_target", "horizon", "optional_shadow_collection"]),
    "prereg_eb3ab6bda5a4": ("H-PER-1P", "matched_event_excess", "greater", 0., 5,
        ["successor_chronological_membership_and_identity", "joint_positive_negative_family"]),
    "prereg_2821a9e1b931": ("H-PER-1N", "matched_event_excess", "less", 0., 5,
        ["successor_chronological_membership_and_identity", "joint_positive_negative_family"]),
    "prereg_e9c3cb494d50": ("H-PER-1P-v2", "matched_event_excess", "greater", 0., 5,
        ["successor_chronological_membership_and_identity", "joint_positive_negative_family"]),
    "prereg_45c5239c4d23": ("H-PER-1N-v2", "matched_event_excess", "less", 0., 5,
        ["successor_chronological_membership_and_identity", "joint_positive_negative_family"]),
    "prereg_35bbce762e7f": ("H-AI-HOLD10", "paired_mean", "greater", .05, 10,
        ["meaning_of_proposed_primary_or_final", "candidate_R_not_whole_policy"]),
}


def draft_contract(registration: dict, registry: list[dict]) -> dict:
    """Retain verbatim originals plus an explicitly unactivated method draft."""
    key = registration["prereg_id"]
    spec = _SPECS.get(key)
    if spec is None:
        return {"predecessor_id": key, "status": "unsupported_registration",
                "original": deepcopy(registration), "formal_allowed": False}
    name, statistic, direction, target, horizon, choices = spec
    return {
        "draft_id": f"{key}:successor-draft-20260928", "version": VERSION,
        "predecessor_id": key, "name": name, "status": "draft_not_activated",
        "original": deepcopy(registration), "registry": deepcopy(registry),
        "statistic": statistic, "direction": direction, "effect_target": target,
        "horizon_sessions": horizon, "equivalence_margin": None,
        "measurement_basis": "gross_idealized_bracket_R" if not name.startswith("H-PER") else "market_adjusted_return_fraction",
        "required_choices": list(choices), "formal_allowed": False,
        "shared_requirements": ["reviewed_successor_contract_and_future_enrollment",
            "complete_saved_opportunity_membership", "source_inputs_and_measurement_identity",
            "complete_actual_intervals_and_joint_dependence", "fixed_family_and_stopping_rule"],
        "floor_effective_n": registration.get("floor_effective_n"),
        "floor_span_days": registration.get("floor_span_days"),
        "analysis_not_before": registration.get("analysis_not_before"),
    }


def known_registration_ids() -> frozenset[str]:
    return frozenset(_SPECS)
