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
    # PR9: self-halt fuse threshold + heartbeat staleness window.
    "scheduler_max_consecutive_failures", "scheduler_heartbeat_stale_minutes",
    # PR9.5: benchmark spine capture time.
    "scheduler_benchmark_spine_time",
    # INSTR-1: ATR(14) capture time.
    "scheduler_atr_update_time",
)

EARNINGS_CONFIG_FIELDS = (
    "earnings_proximity_enabled", "earnings_proximity_provider",
    "earnings_proximity_warning_days", "earnings_proximity_default_hold_days",
    "earnings_proximity_max_symbols_per_scan", "earnings_proximity_timeout_seconds",
    "earnings_proximity_fail_open_as_unavailable",
    # EARN-1: the live provider's own on/off-relevant settings (the API key
    # itself is excluded via SECRET_SETTINGS_FIELDS, same as benzinga/massive).
    "scheduler_earnings_calendar_pull_time", "earnings_calendar_staleness_days",
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

# EXP-0: shadow-tier universe capture screen parameters + master switch. The
# committed universe FILE's own sha/version (not a settings field) is the
# per-symbol-list lineage stamp; this hash is the screen/threshold lineage.
SHADOW_TIER_CONFIG_FIELDS = (
    "shadow_tier_enabled", "shadow_tier_universe_file",
    "shadow_tier_min_adv_usd", "shadow_tier_max_adv_usd",
    "shadow_tier_min_price", "shadow_tier_max_price",
    "shadow_tier_adv_lookback_days", "shadow_tier_target_count", "shadow_tier_max_count",
)

# REG-1: the classifier's actual thresholds are CODE CONSTANTS tied to
# REGIME_RULES_V1 (alphaos/regime/classifier.py), not settings -- same
# rationale as TQS/attribution: env-tunable thresholds would destroy
# comparability across the shadow record this PR exists to build. The real
# settings are the on/off switch + the one-off backfill's lookback depth.
REGIME_CONFIG_FIELDS = (
    "regime_enabled", "regime_backfill_lookback_days",
)

# TEXT-0: the form catalog itself is a CODE CONSTANT tied to EDGAR_FORMS_V1
# (alphaos/text_archive/forms.py), not settings -- same rationale as TQS/
# attribution/REGIME. The real settings are the on/off switch, the cadence
# time, and the contact-email input (a secret-stripped preimage via
# strip_secrets/SECRET_SETTINGS_FIELDS would normally scrub anything email-
# shaped, but sec_edgar_contact_email is deliberately NOT a credential -- it
# has to be sent in plaintext in every EDGAR request's User-Agent header
# anyway, so hashing it here reveals nothing hashing wouldn't already not
# protect).
TEXT_ARCHIVE_CONFIG_FIELDS = (
    "text_archive_enabled", "sec_edgar_contact_email", "scheduler_text_archive_pull_time",
)

# BASELINE: the two frozen rules' own formulas (threshold_v1's interest_score
# cutoff, the ATR stop multiplier, the reward:risk target ratio) are CODE
# CONSTANTS / already-hashed settings fields (min_reward_risk is already in
# RISK_CONFIG_FIELDS; the ATR multiplier travels via ATR_RULES_V1 on every
# atr_history row) -- same rationale as TQS/attribution/REGIME/TEXT-0. The
# real setting is the on/off switch.
BASELINE_CONFIG_FIELDS = (
    "baseline_enabled",
)

# CANARY: the drift-tier thresholds are real, operator-tunable settings
# (spec's own "threshold in config, not code") -- unlike TQS/attribution/
# REGIME/BASELINE, there's no frozen code-constant formula to omit here.
CANARY_CONFIG_FIELDS = (
    "canary_enabled", "scheduler_canary_run_weekday", "scheduler_canary_run_time",
    "canary_tier2_label_diff_pct", "canary_tier3_confidence_shift_band",
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
    "proposal_ttl_config_hash", "tqs_config_hash", "attribution_config_hash",
    "shadow_tier_config_hash"}.
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
        "shadow_tier_config_hash": _subset_hash(full, SHADOW_TIER_CONFIG_FIELDS),
        "regime_config_hash": _subset_hash(full, REGIME_CONFIG_FIELDS),
        "text_archive_config_hash": _subset_hash(full, TEXT_ARCHIVE_CONFIG_FIELDS),
        "baseline_config_hash": _subset_hash(full, BASELINE_CONFIG_FIELDS),
        "canary_config_hash": _subset_hash(full, CANARY_CONFIG_FIELDS),
    }
