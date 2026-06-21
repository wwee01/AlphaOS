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
    """Where an order was executed. Mock and Alpaca-paper share one schema."""

    MOCK = "mock"
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


class MarketSession(StrEnum):
    PREMARKET = "premarket"
    REGULAR = "regular"
    AFTERHOURS = "afterhours"
    CLOSED = "closed"


class NewsStatus(StrEnum):
    AVAILABLE = "available"
    NEWS_UNAVAILABLE = "NEWS_UNAVAILABLE"


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
class ReasonCode(StrEnum):
    NO_VERIFIABLE_NEWS = "NO_VERIFIABLE_NEWS"
    NEWS_UNAVAILABLE = "NEWS_UNAVAILABLE"
    STALE_DATA = "STALE_DATA"
    UNVERIFIABLE_DATA = "UNVERIFIABLE_DATA"
    WIDE_SPREAD = "WIDE_SPREAD"
    LOW_LIQUIDITY = "LOW_LIQUIDITY"
    RISK_OVERSIZED = "RISK_OVERSIZED"
    TOO_MANY_POSITIONS = "TOO_MANY_POSITIONS"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    DAILY_TRADE_LIMIT = "DAILY_TRADE_LIMIT"
    AUTO_APPROVAL_LIMIT = "AUTO_APPROVAL_LIMIT"
    NO_VALID_EXIT_PROTECTION = "NO_VALID_EXIT_PROTECTION"
    REAL_TRADING_BLOCKED = "REAL_TRADING_BLOCKED"
    PAPER_SAFETY_FAILED = "PAPER_SAFETY_FAILED"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    INVALID_STOP = "INVALID_STOP"
    OPENAI_REJECT = "OPENAI_REJECT"
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

# Timezones we stamp events with.
TZ_UTC = "UTC"
TZ_LOCAL = "Asia/Singapore"
TZ_MARKET = "America/New_York"
