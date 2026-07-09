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


class ProtectionStatus(StrEnum):
    """Broker protection watchdog verdict for one open, broker-managed position
    (docs/roadmap/protection-watchdog.md). UNPROTECTED, CLOSED_MISMATCH, and
    UNVERIFIABLE are CRITICAL and block all new entries; DEGRADED and a single
    CHECK_ERROR are WARNING-only and do not."""

    UNKNOWN = "unknown"            # never checked yet, or not broker-managed
    PROTECTED = "protected"        # stop + target both live at the broker
    DEGRADED = "degraded"          # target missing only; stop still live -- WARNING, non-blocking
    UNPROTECTED = "unprotected"    # stop missing -- CRITICAL, blocks new entries
    CLOSED_MISMATCH = "closed_mismatch"  # local open, broker has no matching position -- CRITICAL, blocks
    CHECK_ERROR = "check_error"    # broker lookup failed this pass; below the escalation threshold -- WARNING, non-blocking
    UNVERIFIABLE = "unverifiable"  # broker lookup has failed N consecutive passes (PR2.6) -- CRITICAL, blocks


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
    # INSTR-1: the live evaluator's stop was overridden by k*ATR(14) --
    # target_price_source stays OPENAI (the AI still sets the target); only
    # stop_price_source becomes this.
    ATR_V1 = "atr_v1"


class ScanType(StrEnum):
    """What kind of scan produced a batch (audit only)."""

    PREMARKET = "premarket"
    POST_OPEN = "post_open"
    FOLLOW_UP = "follow_up"
    MANUAL = "manual"
    TEST = "test"
    UNKNOWN = "unknown"


class RunStatus(StrEnum):
    """Lifecycle status of a scan batch / scheduler run."""

    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"
    SKIPPED = "skipped"


class SchedulerRunType(StrEnum):
    """What a scheduler run did (records exist even without a real scheduler)."""

    SCAN = "scan"
    MONITOR = "monitor"
    REPORT = "report"
    NOTIFY = "notify"
    TEST = "test"


class TriggerSource(StrEnum):
    """Who triggered a run (v1 is manual/CLI; scheduler is future)."""

    MANUAL_CLI = "manual_cli"
    DASHBOARD = "dashboard"
    FUTURE_SCHEDULER = "future_scheduler"
    TEST = "test"
    CLI = "cli"
    SCHEDULER = "scheduler"


class BaselineType(StrEnum):
    """Comparison baselines tracked alongside live decisions."""

    MOMENTUM_ONLY = "momentum_only"
    NO_NEWS = "no_news"
    AI_REJECTED_TRACKING = "ai_rejected_tracking"
    OPENAI_ONLY = "openai_only"
    OPENAI_PLUS_CLAUDE_REVIEWED = "openai_plus_claude_reviewed"


class OutcomeClass(StrEnum):
    """Coarse trade outcome classification."""

    WIN = "win"
    LOSS = "loss"
    BREAKEVEN = "breakeven"


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
    PROTECTION_INTEGRITY_FAILURE = "PROTECTION_INTEGRITY_FAILURE"
    INVALID_STOP = "INVALID_STOP"
    OPENAI_REJECT = "OPENAI_REJECT"
    REWARD_RISK_TOO_LOW = "REWARD_RISK_TOO_LOW"
    NO_ATR_DATA = "NO_ATR_DATA"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    MARGIN_APPROVAL_REQUIRED = "MARGIN_APPROVAL_REQUIRED"
    DAYTRADE_GATED = "DAYTRADE_GATED"
    # --- Roadmap 2.3: AI category/playbook labelling ---
    LABEL_UNCLASSIFIED = "LABEL_UNCLASSIFIED"
    LABEL_MALFORMED = "LABEL_MALFORMED"
    LABEL_LOW_CONFIDENCE = "LABEL_LOW_CONFIDENCE"
    # --- Roadmap PR6: proposal TTL / stale-approval guard ---
    PROPOSAL_EXPIRED = "PROPOSAL_EXPIRED"
    PROPOSAL_SUPERSEDED = "PROPOSAL_SUPERSEDED"
    # --- EXP-0: shadow-tier deterministic universe capture ---
    SHADOW_TIER_EXCLUDED = "SHADOW_TIER_EXCLUDED"
    # --- PR10: setup cards / exit-first invariant ---
    EXIT_PLAN_INCOMPLETE = "EXIT_PLAN_INCOMPLETE"


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


# --- Roadmap 2.3: AI category / playbook labelling ----------------------------
# The FIXED official label set. The AI labeller may ONLY choose a primary_label
# from this set; it may suggest new tags, but those are stored as UNOFFICIAL
# candidate tags and never promoted to official labels automatically.
OFFICIAL_LABELS = frozenset(
    {
        "Momentum",
        "Breakout",
        "Dip Buy",
        "Mean Reversion",
        "News Reaction",
        "Earnings Reaction",
        "Sector Sympathy Move",
        "Other/Unclassified",
    }
)
LABEL_OTHER = "Other/Unclassified"
LABEL_VERSION_V1 = "v1"


class LabelSource(StrEnum):
    """Where a candidate's playbook label came from."""

    OPENAI = "openai"
    MOCK = "mock"
    FAIL_SAFE = "fail_safe"   # malformed/missing AI output -> safe default


class FailsafeReason(StrEnum):
    """Why a label fell back to the conservative fail-safe (Other/Unclassified +
    REJECT). Stored in ``candidate_labels.validation_status`` when
    ``label_source='fail_safe'`` — for VISIBILITY only; never changes behaviour.
    A failing labeller looks like a conservative reject, so surfacing the reason
    (and rate) is what turns a silent block into an obvious alarm."""

    LIVE_EXCEPTION = "live_exception"     # unexpected error in the live call
    PARSE_ERROR = "parse_error"           # response had no parseable JSON object
    TRUNCATED_OUTPUT = "truncated_output" # finish_reason=length (token budget too small)
    TIMEOUT = "timeout"                   # network/API timeout
    MALFORMED_JSON = "malformed_json"     # parsed, but core fields missing
    UNAVAILABLE = "unavailable"           # provider disabled / returned nothing
    UNKNOWN = "unknown"


# Placeholder context sentinels (Roadmap 2.3): last30days/sentiment are NOT
# implemented yet. These are clean, explicit "unavailable" markers so the schema
# and packets are ready for later enrichment without faking any data now.
CONTEXT_UNAVAILABLE_V1 = "unavailable"


# --- Roadmap 2.4: official news / catalyst enrichment -------------------------
# Catalyst context is CONTEXT, not execution authority: it can add risk tags +
# explanation, suggest a label review, and shape the AI thesis — but it NEVER
# forces a proposal, bypasses a gate, mints an official label, or executes.
class CatalystStatus(StrEnum):
    CONFIRMED = "confirmed"
    POSSIBLE = "possible"
    NONE_FOUND = "none_found"
    STALE = "stale"
    CONFLICTING = "conflicting"
    UNAVAILABLE = "unavailable"   # no provider configured / disabled
    ERROR = "error"              # provider call failed (fail-safe)


class CatalystType(StrEnum):
    EARNINGS = "earnings"
    ANALYST_UPGRADE = "analyst_upgrade"
    ANALYST_DOWNGRADE = "analyst_downgrade"
    COMPANY_NEWS = "company_news"
    SEC_FILING = "sec_filing"
    PRODUCT_LAUNCH = "product_launch"
    PARTNERSHIP = "partnership"
    M_AND_A = "m_and_a"
    LEGAL_REGULATORY = "legal_regulatory"
    SECTOR_NEWS = "sector_news"
    MACRO = "macro"
    NO_CLEAR_CATALYST = "no_clear_catalyst"
    UNKNOWN = "unknown"


# Catalyst types that clearly OPPOSE a direction -- never a positive upgrade
# driver for it (an analyst downgrade / legal-regulatory hit can't upgrade a
# long; an upgrade / launch / partnership can't upgrade a short). Shared
# single source of truth: Orchestrator._real_decision_driver (Roadmap 2.7) and
# alphaos/tqs/scoring.py (PR7) both need this same opposition list, and it
# lives here (not on Orchestrator) specifically so alphaos/tqs -- which
# orchestrator.py imports -- never has to import orchestrator.py back.
BEARISH_CATALYST_TYPES = frozenset({CatalystType.ANALYST_DOWNGRADE.value,
                                    CatalystType.LEGAL_REGULATORY.value})
BULLISH_CATALYST_TYPES = frozenset({CatalystType.ANALYST_UPGRADE.value,
                                    CatalystType.PRODUCT_LAUNCH.value,
                                    CatalystType.PARTNERSHIP.value})


class EnrichmentSource(StrEnum):
    MOCK = "mock"
    ALPACA = "alpaca"
    DISABLED = "disabled"
    NONE = "none"


class EarningsDataStatus(StrEnum):
    """PR5: status of the earnings-proximity fetch for one candidate. Never
    "safe" by omission -- UNAVAILABLE/UNKNOWN/STALE/PROVIDER_DISABLED are all
    distinct from OK so a caller can never mistake missing data for a
    confirmed no-earnings-nearby result."""

    OK = "ok"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"
    STALE = "stale"
    PROVIDER_DISABLED = "provider_disabled"


class EarningsTiming(StrEnum):
    BEFORE_OPEN = "before_open"
    AFTER_CLOSE = "after_close"
    UNKNOWN = "unknown"


class ProposalStatus(StrEnum):
    """Full lifecycle of a ``trade_proposals`` row (PR6 formalizes this as an
    enum; every value below was already in use as a raw string except EXPIRED
    and SUPERSEDED, which PR6 adds). A row is only ever ADDITIVELY updated —
    never deleted — so every status a proposal ever passed through remains
    reconstructable from system_events/approvals history."""

    PROPOSED = "proposed"
    PENDING_APPROVAL = "pending_approval"
    BLOCKED = "blocked"
    APPROVED = "approved"
    SUBMITTED = "submitted"
    FILLED = "filled"
    REJECTED = "rejected"
    # --- Roadmap PR6 ---
    EXPIRED = "expired"        # TTL exceeded before an approval attempt succeeded
    SUPERSEDED = "superseded"  # a fresher proposal for the same symbol replaced it

    @classmethod
    def approvable(cls) -> tuple:
        """Statuses a proposal must be in to be eligible for approve_proposal()."""
        return (cls.PENDING_APPROVAL.value, cls.PROPOSED.value)


class CandidateStatus(StrEnum):
    """Lifecycle of a ``candidates`` row (enum-ified during the ScanContext
    structural refactor -- every value below was already in use as a raw
    string). Rows are never deleted; status is a point-in-time label, the
    full history lives in system_events/candidate_labels/decision_adjustments."""

    DETECTED = "detected"
    WATCH = "watch"
    PROPOSED = "proposed"
    REJECTED = "rejected"


class TqsSourceType(StrEnum):
    """PR7: what a tqs_scores row was computed for. A candidate-level row
    exists for every scored candidate; a proposal-level row is a SEPARATE,
    ADDITIONAL row for the subset that also became a proposal (recomputed
    against proposal-level fields like the built proposal's own expected_r) --
    never a mutation of the candidate-level row."""

    CANDIDATE = "candidate"
    PROPOSAL = "proposal"


class TqsBucket(StrEnum):
    """PR7: TQS v0 score buckets. Boundaries are v0-arbitrary (chosen for
    digest readability, not calibrated) -- part of TQS_VERSION; changing them
    is a version bump, not a config tweak. UNSCORABLE is distinct from WEAK:
    weak means "scored low", unscorable means "no evidence was available to
    score at all" -- collapsing the two would hide a data-coverage gap behind
    what looks like a real (if poor) assessment."""

    UNSCORABLE = "unscorable"
    WEAK = "weak"
    MIXED = "mixed"
    WATCH = "watch"
    GOOD = "good"
    STRONG = "strong"


class TqsDataQualityStatus(StrEnum):
    """PR7: overall evidence-quality label for one tqs_scores row."""

    OK = "ok"                # majority of applicable components were live/available
    DEGRADED = "degraded"    # scored, but most components were unavailable
    MOCK = "mock"            # the candidate/proposal itself is mock-mode/demo data
    UNSCORABLE = "unscorable"  # zero components available; no score was fabricated


class AttributionType(StrEnum):
    """PR8: the 5 supported Attribution v2 event types. An attribution_records
    row exists ONLY where two paths diverged -- user vs AlphaOS, gate vs
    AlphaOS, operational expiry vs AlphaOS, or execution vs the frozen
    AlphaOS plan. Pure one-path no-action decisions (reject-no-action,
    watch-no-action, armed-watch-no-action) get NO rows -- they are already
    measured by candidate_outcomes and analyzed via report-time joins."""

    PROPOSE_USER_REJECTED = "propose_user_rejected"
    USER_OVERRIDE_TRADE = "user_override_trade"
    PROPOSE_APPROVED_EXECUTED = "propose_approved_executed"
    PROPOSE_EXPIRED = "propose_expired"
    PROPOSE_BLOCKED = "propose_blocked"


class AttributionAgent(StrEnum):
    """PR8: which agent's deviation this row measures. Used for aggregation
    ONLY (never a per-event moral judgment) -- see the reporting floor rules
    in alphaos/reports/attribution.py."""

    USER = "user"
    GATE = "gate"
    OPERATIONAL = "operational"
    EXECUTION = "execution"


class AttributionResolvedStatus(StrEnum):
    """PR8: lifecycle state of one attribution_records row. 'partial' is
    propose_approved_executed-specific (the executed side resolved but the
    counterfactual replay side did not, or vice versa)."""

    PENDING = "pending"
    RESOLVED = "resolved"
    PARTIAL = "partial"
    UNRESOLVABLE = "unresolvable"


class AttributionDataQuality(StrEnum):
    """PR8: overall evidence-quality label for one attribution_records row --
    same shape as TqsDataQualityStatus, deliberately: 'mock' takes precedence
    over ok/degraded whenever the row's own settings/eval mock signal is set."""

    OK = "ok"
    DEGRADED = "degraded"
    MOCK = "mock"
    UNRESOLVABLE = "unresolvable"


# Maps a catalyst type to the OFFICIAL label it would imply, used ONLY to compute
# an advisory ``catalyst_suggested_label`` + ``label_review_required`` flag. It
# never overwrites the frozen primary_label (no auto-relabelling in v1).
CATALYST_TYPE_TO_LABEL = {
    CatalystType.EARNINGS.value: "Earnings Reaction",
    CatalystType.ANALYST_UPGRADE.value: "News Reaction",
    CatalystType.ANALYST_DOWNGRADE.value: "News Reaction",
    CatalystType.COMPANY_NEWS.value: "News Reaction",
    CatalystType.SEC_FILING.value: "News Reaction",
    CatalystType.PRODUCT_LAUNCH.value: "News Reaction",
    CatalystType.PARTNERSHIP.value: "News Reaction",
    CatalystType.M_AND_A.value: "News Reaction",
    CatalystType.LEGAL_REGULATORY.value: "News Reaction",
    CatalystType.SECTOR_NEWS.value: "Sector Sympathy Move",
    CatalystType.MACRO.value: "Sector Sympathy Move",
}


# --- Roadmap 2.5: last30days research / narrative-context enrichment ----------
# A SEPARATE social/research layer (last30days skill) distinct from official news
# (Roadmap 2.4). It is narrative CONTEXT, not execution authority: it can enrich
# thesis/risk tags + suggest a label review only. It NEVER forces a proposal,
# bypasses a gate, mints/overwrites an official label, affects sizing, or executes;
# and it NEVER enters the no-news OpenAI momentum eval. Disabled by default; the
# live provider shells out to a globally-installed skill (no vendored code).
class Last30DaysStatus(StrEnum):
    AVAILABLE = "available"               # ran; a usable recent narrative was found
    NONE_FOUND = "none_found"             # ran; little/no clear narrative
    STALE = "stale"                       # ran; narrative older than the window
    UNAVAILABLE = "unavailable"           # provider disabled / missing / not configured
    ERROR = "error"                       # provider call failed (fail-safe)
    DISABLED = "disabled"                 # last30days enrichment intentionally off
    SKIPPED_BUDGET_CAP = "skipped_budget_cap"  # eligible but outside the per-scan cap


class Last30DaysProvider(StrEnum):
    MOCK = "mock"
    CLI = "cli"
    DISABLED = "disabled"
    NONE = "none"


# Advisory sentiment polarity derived from the research (context only — never a
# trade signal). "unknown" is the honest default for keyless retrieval, which has
# no reliable polarity.
class SentimentLabel(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    MIXED = "mixed"
    NEUTRAL = "neutral"
    UNCLEAR = "unclear"      # Roadmap 2.7: explicit "evidence too weak to call"
    UNKNOWN = "unknown"      # no polarity attempted (keyless default)


MOCK_L30D_SOURCE = "MOCK_L30D"   # clearly-mock label so nothing is mistaken for live data
L30D_SKIPPED_REASON = "outside LAST30DAYS_MAX_SYMBOLS_PER_SCAN cap"


# --- Roadmap 2.6: gated labeller decision override ----------------------------
# By default the AI label is DOWNGRADE-ONLY. When LABELLER_DECISION_OVERRIDE_ENABLED
# is on AND real signals are present (real AI + a live catalyst/sentiment driver),
# the label decision becomes authoritative and may move the eval's call UP or DOWN.
# Every move is tagged + the driver recorded for learning. It still cannot bypass
# the deterministic gates, skip manual approval, or upgrade a non-tradeable eval
# (one with no levels / unusable freshness — i.e. a data-integrity reject).
class DecisionAdjustment(StrEnum):
    UPGRADED = "upgraded"       # label raised the eval's decision (real driver only)
    DOWNGRADED = "downgraded"   # label lowered the eval's decision (always allowed)
    UNCHANGED = "unchanged"     # label agreed with the eval / no net change


# --- Roadmap 2.7: LLM-derived last30days narrative polarity --------------------
# Interprets last30days cluster evidence into a directional polarity + a
# narrative-driver classification. Polarity is CONTEXT that can ARM an upgrade
# (via the gated override) when directionally aligned + high-confidence; it can
# NEVER directly create a trade, bypass a gate, or skip manual approval. Hype /
# meme / social-momentum / squeeze narratives are NOT auto-suppressed — they are
# flagged as HIGH-RISK and may arm only as `high_risk_narrative` (manual-only).
class DirectionAlignment(StrEnum):
    ALIGNED = "aligned"
    CONFLICTING = "conflicting"
    NEUTRAL = "neutral"
    UNCLEAR = "unclear"


class SourceCoverageQuality(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class NarrativeDriverType(StrEnum):
    FUNDAMENTAL = "fundamental"
    CATALYST = "catalyst"
    SOCIAL_MOMENTUM = "social_momentum"
    MEME_HYPE = "meme_hype"
    SQUEEZE_RISK = "squeeze_risk"
    MIXED = "mixed"
    UNCLEAR = "unclear"


# Narrative driver types that are short-term / crowd-driven -> arm only as HIGH-RISK.
HIGH_RISK_NARRATIVE_TYPES = frozenset({
    NarrativeDriverType.SOCIAL_MOMENTUM.value,
    NarrativeDriverType.MEME_HYPE.value,
    NarrativeDriverType.SQUEEZE_RISK.value,
})


class HypeRisk(StrEnum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ArmingClassification(StrEnum):
    NORMAL_DRIVER = "normal_driver"
    HIGH_RISK_NARRATIVE = "high_risk_narrative"
    NON_ARMING = "non_arming"


class PolarityParseStatus(StrEnum):
    PARSED = "parsed"
    INVALID_JSON = "invalid_json"
    SCHEMA_ERROR = "schema_error"
    MODEL_ERROR = "model_error"
    SKIPPED = "skipped"


# Source-coverage quality ordering for threshold comparisons (low < medium < high).
SOURCE_COVERAGE_RANK = {
    SourceCoverageQuality.LOW.value: 0,
    SourceCoverageQuality.MEDIUM.value: 1,
    SourceCoverageQuality.HIGH.value: 2,
}

POLARITY_PROMPT_VERSION = "v1"

HIGH_RISK_NARRATIVE_WARNING = (
    "This setup is partly driven by hype / social narrative / squeeze-style "
    "attention. This may create short-term price action but carries elevated "
    "reversal, crowding, and liquidity risk. Manual approval required."
)


# --- Roadmap 2.8: Armed Watch + labeller reasoning + User Override ------------
# An ARMED WATCH = the override/polarity armed a support signal but the final
# decision stayed WATCH (no proposal/order) because eval/labeller produced no
# higher actionable decision. It is a NEAR-ACTION watchlist item, NOT a reject.
class ArmedWatchReason(StrEnum):
    LABELLER_DID_NOT_UPGRADE = "polarity_armed_but_labeller_did_not_upgrade"
    EVAL_NOT_TRADEABLE = "polarity_armed_but_eval_not_tradeable"
    ARMED_NO_UPGRADE = "armed_no_upgrade"


# Advisory labeller readiness (Part B; visibility only — never changes behaviour).
class ProposalReadiness(StrEnum):
    NOT_READY = "not_ready"
    DEVELOPING = "developing"
    NEAR_ACTION = "near_action"
    READY = "ready"
    UNCLEAR = "unclear"


# --- User Override Mode (Part C): a SEPARATE decision layer. A user override
# NEVER rewrites AlphaOS's original recommendation; both are stored side by side.
class UserOverrideAction(StrEnum):
    WATCH_TO_TRADE = "watch_to_trade"
    PROPOSE_TO_REJECT = "propose_to_reject"
    REJECT_TO_WATCH = "reject_to_watch"
    REJECT_TO_TRADE = "reject_to_trade"
    LONG_TO_SHORT = "long_to_short"
    SHORT_TO_LONG = "short_to_long"
    NORMAL_TO_HIGH_CONVICTION = "normal_to_high_conviction"
    REDUCE_SIZE = "reduce_size"
    INCREASE_SIZE = "increase_size"
    MANUAL_EXIT = "manual_exit"
    MANUAL_HOLD = "manual_hold"


class OverrideAggressiveness(StrEnum):
    MORE_AGGRESSIVE = "more_aggressive"
    MORE_CONSERVATIVE = "more_conservative"
    DIRECTION_CHANGE = "direction_change"
    EXIT_OVERRIDE = "exit_override"
    HOLD_OVERRIDE = "hold_override"


class UserReasonCode(StrEnum):
    STRONG_CONVICTION = "strong_conviction"
    MARKET_INTUITION = "market_intuition"
    SECTOR_KNOWLEDGE = "sector_knowledge"
    NEWS_JUST_BROKE = "news_just_broke"
    DISAGREES_WITH_AI = "disagrees_with_ai"
    TESTING_HYPOTHESIS = "testing_hypothesis"
    WANTS_ACTION = "wants_action"
    RISK_REDUCTION = "risk_reduction"
    PROFIT_PROTECTION = "profit_protection"
    STOP_LOSS_CONCERN = "stop_loss_concern"
    OTHER = "other"


class OverrideOutcomeStatus(StrEnum):
    PENDING = "pending"
    WON = "won"
    LOST = "lost"
    BREAKEVEN = "breakeven"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class AttributionResult(StrEnum):
    USER_OUTPERFORMED = "user_outperformed"
    ALPHAOS_OUTPERFORMED = "alphaos_outperformed"
    INCONCLUSIVE = "inconclusive"
    PENDING = "pending"


class OverrideBlockedReason(StrEnum):
    SAFETY_GATE_FAILED = "safety_gate_failed"
    STALE_DATA = "stale_data"
    WIDE_SPREAD = "wide_spread"
    LOW_LIQUIDITY = "low_liquidity"
    RISK_GATE_FAILED = "risk_gate_failed"
    NO_VALID_EXIT_PROTECTION = "no_valid_exit_protection"
    NO_OPEN_POSITION = "no_open_position"
    NO_PROPOSAL = "no_proposal"
    SHADOW_TIER_EXCLUDED = "shadow_tier_excluded"
    OTHER = "other"
