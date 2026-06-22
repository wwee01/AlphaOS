"""Central enums and constants for AlphaOS v1.

These string-valued enums are stored directly in SQLite and serialized to JSON,
so they compare cleanly against plain strings. Keeping them in one place makes
the state machine and the safety vocabulary auditable at a glance.
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """A str-backed Enum whose ``value`` is what gets persisted."""

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


class RuntimeMode(StrEnum):
    """Runtime modes for v1.

    Only MOCK and PAPER are active. SHADOW and RESEARCH are recognized-but-
    inactive placeholders. LIVE intentionally does NOT exist as a reachable
    code path in v1.
    """

    MOCK = "mock"
    PAPER = "paper"
    SHADOW = "shadow"      # stub — recognized but inactive
    RESEARCH = "research"  # stub — recognized but inactive


# Modes that are actually implemented / runnable in v1.
ACTIVE_MODES = frozenset({RuntimeMode.MOCK, RuntimeMode.PAPER})
# Modes recognized but deliberately not implemented yet.
STUB_MODES = frozenset({RuntimeMode.SHADOW, RuntimeMode.RESEARCH})


class ApprovalMode(StrEnum):
    MANUAL = "manual"
    AUTO = "auto"


class Decision(StrEnum):
    """OpenAI primary evaluation decision."""

    REJECT = "reject"
    WATCH = "watch"
    PROPOSE = "propose"


class TradeDirection(StrEnum):
    LONG = "long"
    SHORT = "short"


class Strategy(StrEnum):
    """Trades are tagged so the swing book and the day-trade experiment book
    are never co-mingled."""

    SWING = "swing"
    DAYTRADE_EXPERIMENT = "daytrade_experiment"


class ExecutionSource(StrEnum):
    """Low-level fill source. v1 fills are internal simulations; a real Alpaca
    paper fill (ALPACA_PAPER) is only ever set when it comes from the real
    Alpaca paper API. Mock/Alpaca-paper share one schema."""

    MOCK = "mock"
    INTERNAL_SIM = "internal_sim"
    ALPACA_PAPER = "alpaca_paper"


class OrderState(StrEnum):
    """Order lifecycle, modelled as a first-class state machine."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    REPLACED = "replaced"
    EXPIRED = "expired"
    FAILED = "failed"
    CLOSED = "closed"


# Terminal order states (no further transitions expected).
TERMINAL_ORDER_STATES = frozenset(
    {
        OrderState.FILLED,
        OrderState.REJECTED,
        OrderState.CANCELLED,
        OrderState.EXPIRED,
        OrderState.FAILED,
        OrderState.CLOSED,
    }
)


class ProtectionPath(StrEnum):
    """How an order's downside protection was arranged. Logged for every trade."""

    BROKER_NATIVE_BRACKET = "BROKER_NATIVE_BRACKET"
    BROKER_NATIVE_OCO = "BROKER_NATIVE_OCO"
    ENTRY_PLUS_WATCHDOG = "ENTRY_PLUS_WATCHDOG"
    BLOCKED_NO_VALID_EXIT_PROTECTION = "BLOCKED_NO_VALID_EXIT_PROTECTION"


class ApprovalLabel(StrEnum):
    MANUAL_APPROVED = "MANUAL_APPROVED"
    AUTO_APPROVED = "AUTO_APPROVED"
    REJECTED = "REJECTED"


class ExitClassification(StrEnum):
    """Every same-day exit (and every exit generally) is classified as one of
    these six categories."""

    RISK_CONTROL = "risk-control"
    THESIS_INVALIDATION = "thesis-invalidation"
    PROFIT_TAKING = "profit-taking"
    MANUAL_USER = "manual-user"
    EXPERIMENTAL_DAYTRADE = "experimental-daytrade"
    ERROR_DATA_QUALITY = "error-data-quality"


class FreshnessStatus(StrEnum):
    USABLE = "usable"
    STALE = "stale"
    UNVERIFIABLE = "unverifiable"
    MISSING = "missing"
    CLOSED_SESSION = "closed_session"


class DataProvider(StrEnum):
    """Active market-data providers. v1: Alpaca only."""

    ALPACA = "alpaca"


class MarketDataFeed(StrEnum):
    IEX = "iex"
    SIP = "sip"


class MarketDataMode(StrEnum):
    LIVE = "live"
    MOCK = "mock"


class ExecutionProvider(StrEnum):
    """How orders are executed. ``simulated_internal`` fills are simulated;
    ``alpaca_paper`` places real orders against the Alpaca PAPER API (still no
    real money). Real-money execution does not exist."""

    SIMULATED_INTERNAL = "simulated_internal"
    ALPACA_PAPER = "alpaca_paper"


class MarketSession(StrEnum):
    PREMARKET = "premarket"
    REGULAR = "regular"
    AFTERHOURS = "afterhours"
    CLOSED = "closed"


class NewsStatus(StrEnum):
    AVAILABLE = "available"
    NEWS_UNAVAILABLE = "NEWS_UNAVAILABLE"
    DISABLED_V1 = "disabled_v1"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class UniverseTier(StrEnum):
    CORE = "core"                  # core liquid names
    WATCHLIST = "watchlist"
    EXPERIMENTAL = "experimental"  # paper-only experiments
    RESTRICTED = "restricted"      # blocked: illiquid / penny / bad data


# --- Reason codes (free-form-ish, but the common ones are named) -------------
class TargetProfile(StrEnum):
    """How a proposal's target was styled. Tracking only in v1 — every
    system-generated trade uses CONFIGURED_STANDARD; the others are reserved for
    later experiments and are never auto-selected yet."""

    CONFIGURED_STANDARD = "configured_standard"
    CONSERVATIVE = "conservative"
    EXTENDED = "extended"
    AI_SUGGESTED = "ai_suggested"
    MANUAL_OVERRIDE = "manual_override"


class TargetSource(StrEnum):
    """Where a stop/target price actually came from."""

    CONFIG = "config"
    OPENAI = "openai"
    MANUAL = "manual"
    BROKER = "broker"
    BASELINE = "baseline"


# Target-profile evidence fields carried through proposal -> order -> position ->
# exit -> outcome (tracking only; no effect on trading behavior).
TARGET_PROFILE_FIELDS = (
    "target_profile",
    "target_reward_risk",
    "min_reward_risk",
    "stop_loss_pct",
    "target_price_source",
    "stop_price_source",
)


def target_profile_bundle(src) -> dict:
    """Extract the target-profile fields from a proposal object or a journal row,
    defaulting target_profile to configured_standard when absent."""
    get = src.get if isinstance(src, dict) else (lambda k: getattr(src, k, None))
    bundle = {k: get(k) for k in TARGET_PROFILE_FIELDS}
    if not bundle.get("target_profile"):
        bundle["target_profile"] = TargetProfile.CONFIGURED_STANDARD.value
    return bundle


class ReasonCode(StrEnum):
    NO_VERIFIABLE_NEWS = "NO_VERIFIABLE_NEWS"
    NEWS_UNAVAILABLE = "NEWS_UNAVAILABLE"
    NEWS_DISABLED = "NEWS_DISABLED_V1"
    STALE_DATA = "STALE_DATA"
    STALE_QUOTE = "STALE_QUOTE"
    STALE_BAR = "STALE_BAR"
    MISSING_QUOTE = "MISSING_QUOTE"
    MISSING_BAR = "MISSING_BAR"
    CLOSED_SESSION = "CLOSED_SESSION"
    PRICE_DRIFT = "PRICE_DRIFT"
    UNVERIFIABLE_DATA = "UNVERIFIABLE_DATA"
    INVALID_DATA_PROVIDER = "INVALID_DATA_PROVIDER"
    INVENTED_CATALYST = "INVENTED_CATALYST_IN_NO_NEWS_MODE"
    WIDE_SPREAD = "WIDE_SPREAD"
    CROSSED_QUOTE = "CROSSED_QUOTE"
    LOW_LIQUIDITY = "LOW_LIQUIDITY"
    RISK_OVERSIZED = "RISK_OVERSIZED"
    TOO_MANY_POSITIONS = "TOO_MANY_POSITIONS"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    DAILY_TRADE_LIMIT = "DAILY_TRADE_LIMIT"
    AUTO_APPROVAL_LIMIT = "AUTO_APPROVAL_LIMIT"
    NO_VALID_EXIT_PROTECTION = "NO_VALID_EXIT_PROTECTION"
    REAL_TRADING_BLOCKED = "REAL_TRADING_BLOCKED"
    PAPER_SAFETY_FAILED = "PAPER_SAFETY_FAILED"
    ALPACA_SUBMIT_FAILED = "ALPACA_SUBMIT_FAILED"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    INVALID_STOP = "INVALID_STOP"
    OPENAI_REJECT = "OPENAI_REJECT"
    REWARD_RISK_TOO_LOW = "REWARD_RISK_TOO_LOW"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    MARGIN_APPROVAL_REQUIRED = "MARGIN_APPROVAL_REQUIRED"
    DAYTRADE_GATED = "DAYTRADE_GATED"


# --- Labels & sentinels ------------------------------------------------------
# Mock/test news fixtures MUST carry this label and MUST NEVER reach the
# runtime daily scan or proposal engine. The news clients refuse to emit news
# carrying this label outside of tests.
TEST_FIXTURE_NEWS_LABEL = "TEST_FIXTURE_NEWS"

# The one and only acceptable value of REAL_TRADING_ENABLED in v1.
REAL_TRADING_REQUIRED_VALUE = "false"

# --- v1 no-news sentinels (enforced in OpenAI output validation) -------------
CATALYST_NOT_AVAILABLE_V1 = "not_available_v1"
NEWS_STATUS_DISABLED_V1 = "disabled_v1"
BASELINE_MOMENTUM_NO_NEWS_V1 = "momentum_continuation_no_news_v1"
PLAYBOOK_V1 = "momentum continuation (no-news baseline)"
FAILED_VALIDATION_INVENTED_CATALYST = "invented_catalyst_in_no_news_mode"

# Raised by any deferred connector if the active runtime calls it by accident.
DEFERRED_IN_V1 = "deferred in v1"

# Phrases that indicate the model invented a catalyst while in no-news mode.
INVENTED_CATALYST_MARKERS = (
    "upgrade",
    "downgrade",
    "earnings",
    "fda",
    "merger",
    "acquisition",
    "m&a",
    "rumor",
    "analyst",
    "headline",
    "news-driven",
    "news driven",
    "press release",
    "guidance raise",
    "social media",
    "tweet",
    "reported that",
)

# Timezones we stamp events with.
TZ_UTC = "UTC"
TZ_LOCAL = "Asia/Singapore"
TZ_MARKET = "America/New_York"
