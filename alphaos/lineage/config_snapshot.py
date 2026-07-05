"""Config lineage (PR4): categorized settings hashes.

Settings is one flat dataclass (~160 fields, no internal sub-config objects)
-- these groupings are an editorial slice of field NAMES into the categories
PR4 asks for (scanner/risk/protection/scheduler), not a structural change to
Settings itself. Adding/renaming a settings field later just means updating
the relevant tuple below; nothing else depends on these groupings existing.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from alphaos.lineage.hashing import stable_hash, strip_secrets

SCANNER_CONFIG_FIELDS = (
    "data_provider", "market_data_feed",
    "labelling_enabled", "interest_scan_top_n", "max_candidates_to_ai",
    "label_model", "label_max_output_tokens", "label_propose_threshold",
    "label_min_confidence_to_propose", "labeller_failsafe_warn_rate",
    "labeller_failsafe_critical_rate", "labeller_failsafe_min_sample",
    "interest_near_extreme_pct", "interest_min_score",
    "news_enrichment_enabled", "news_enrichment_provider", "news_lookback_hours",
    "news_max_articles_per_symbol", "news_max_symbols_per_scan", "news_max_age_hours",
    "news_fail_open_as_unavailable",
    "last30days_enabled", "last30days_provider", "last30days_max_symbols_per_scan",
    "last30days_max_themes", "last30days_lookback_hours", "last30days_feed_to_labeller",
    "last30days_fail_open_as_unavailable",
    "last30days_polarity_enabled", "last30days_polarity_model",
    "last30days_polarity_min_confidence", "last30days_polarity_min_source_coverage",
    "last30days_polarity_arming_allowed", "last30days_high_risk_narrative_manual_only",
    "labeller_decision_override_enabled",
)

RISK_CONFIG_FIELDS = (
    "max_risk_per_trade_pct", "max_paper_trades_per_day", "max_open_positions",
    "max_daily_loss_pct", "paper_equity", "max_auto_approvals_per_day",
    "max_spread_pct", "min_dollar_volume",
    "max_data_age_seconds", "max_quote_age_seconds_rth", "max_bar_age_seconds_rth",
    "max_quote_age_seconds_premarket", "max_bar_age_seconds_premarket",
    "max_price_drift_bps_since_proposal",
    "cost_commission_per_share", "cost_min_commission", "cost_slippage_bps",
    "stop_loss_pct", "target_reward_risk", "min_reward_risk",
)

PROTECTION_CONFIG_FIELDS = (
    "protective_order_time_in_force", "requires_persistent_protection",
    "allow_day_tif_for_multiday_positions", "protection_check_error_escalation_threshold",
)

SCHEDULER_CONFIG_FIELDS = (
    "scheduler_scan_windows", "scheduler_monitor_interval_minutes",
    "scheduler_outcomes_interval_minutes", "scheduler_digest_time",
    "scheduler_stale_job_minutes", "scheduler_ai_cost_cap_calls_per_30d",
)

EARNINGS_CONFIG_FIELDS = (
    "earnings_proximity_enabled", "earnings_proximity_provider",
    "earnings_proximity_warning_days", "earnings_proximity_default_hold_days",
    "earnings_proximity_max_symbols_per_scan", "earnings_proximity_timeout_seconds",
    "earnings_proximity_fail_open_as_unavailable",
)

PROPOSAL_TTL_CONFIG_FIELDS = (
    "proposal_ttl_rth_seconds", "proposal_ttl_extended_hours_seconds",
    "proposal_ttl_closed_session_seconds",
)

# PR7: TQS v0's actual scoring parameters (weights, normalization, buckets)
# are CODE CONSTANTS keyed by TQS_VERSION, not settings -- so there is
# nothing to hash here for the formula itself (TQS_VERSION already travels on
# every tqs_scores row directly). The one real setting is the on/off switch;
# a category hash still earns its keep by making "was shadow scoring even
# enabled for this snapshot" a traceable config fact, consistent with every
# other PR's category.
TQS_CONFIG_FIELDS = (
    "tqs_shadow_enabled",
)

# PR8: Attribution v2's formula/taxonomy are CODE CONSTANTS keyed by
# ATTRIBUTION_VERSION (which travels on every attribution_records row
# directly) -- same rationale as TQS_CONFIG_FIELDS above. The one real
# setting is the on/off switch.
ATTRIBUTION_CONFIG_FIELDS = (
    "attribution_enabled",
)


def settings_dict(settings) -> dict[str, Any]:
    """Full settings as a flat, secret-stripped dict. dataclasses.asdict()
    works directly since Settings is itself a plain (frozen) dataclass --
    matches the .to_dict()=asdict(self) pattern already used elsewhere in
    this codebase (timeutils.py, news_service.py, freshness_guard.py, etc)."""
    return strip_secrets(dataclasses.asdict(settings))


def _subset_hash(full: dict, fields: tuple) -> str:
    return stable_hash({k: full.get(k) for k in fields})


def build_config_hashes(settings) -> dict[str, str]:
    """{"config_hash", "scanner_config_hash", "risk_config_hash",
    "protection_config_hash", "scheduler_config_hash", "earnings_config_hash",
    "proposal_ttl_config_hash", "tqs_config_hash", "attribution_config_hash"}.
    Each hash changes iff a relevant field's VALUE changes -- adding/renaming
    a field it doesn't list does not perturb a category hash that doesn't
    reference it."""
    full = settings_dict(settings)
    return {
        "config_hash": stable_hash(full),
        "scanner_config_hash": _subset_hash(full, SCANNER_CONFIG_FIELDS),
        "risk_config_hash": _subset_hash(full, RISK_CONFIG_FIELDS),
        "protection_config_hash": _subset_hash(full, PROTECTION_CONFIG_FIELDS),
        "scheduler_config_hash": _subset_hash(full, SCHEDULER_CONFIG_FIELDS),
        "earnings_config_hash": _subset_hash(full, EARNINGS_CONFIG_FIELDS),
        "proposal_ttl_config_hash": _subset_hash(full, PROPOSAL_TTL_CONFIG_FIELDS),
        "tqs_config_hash": _subset_hash(full, TQS_CONFIG_FIELDS),
        "attribution_config_hash": _subset_hash(full, ATTRIBUTION_CONFIG_FIELDS),
    }
