"""Settings loader + startup-safety validation.

This module is deliberately conservative. It is the first line of the v1 safety
posture:

* ``REAL_TRADING_ENABLED`` must be exactly ``"false"`` — anything else is a
  critical condition and downstream order placement is blocked.
* ``mock`` mode loads with zero external keys and never triggers broker calls.
* ``paper`` mode requires the full Alpaca paper safety set, or paper execution
  refuses to start (logged to ``system_events`` by the caller).
* ``live`` is not a valid value and cannot be selected.

Settings are plain data. Enforcement lives here (validation) and in
``alphaos.safety`` (runtime guards).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from alphaos.constants import (
    ACTIVE_MODES,
    STUB_MODES,
    ApprovalMode,
    DataProvider,
    ExecutionProvider,
    MarketDataMode,
    REAL_TRADING_REQUIRED_VALUE,
    RuntimeMode,
    Severity,
)

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
ALPACA_DATA_BASE_URL = "https://data.alpaca.markets"


class SettingsError(Exception):
    """Raised only for unrecoverable configuration problems (e.g. mode=live)."""


@dataclass(frozen=True)
class StartupCheck:
    """Result of one startup-safety check, surfaced to System Health + logs."""

    name: str
    ok: bool
    detail: str
    severity: Severity = Severity.ERROR

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "severity": self.severity.value,
        }


def _get(env: dict, key: str, default: str = "") -> str:
    val = env.get(key, default)
    return val.strip() if isinstance(val, str) else val


def _get_bool(env: dict, key: str, default: bool) -> bool:
    raw = _get(env, key, "true" if default else "false").lower()
    return raw in ("1", "true", "yes", "on")


def _get_float(env: dict, key: str, default: float) -> float:
    try:
        return float(_get(env, key, str(default)))
    except (ValueError, TypeError):
        return default


def _get_int(env: dict, key: str, default: int) -> int:
    try:
        return int(float(_get(env, key, str(default))))
    except (ValueError, TypeError):
        return default


def _parse_hhmm(value: str, key: str) -> tuple[int, int]:
    """Parse a strict 24-hour "HH:MM" string; raise SettingsError on malformed input."""
    parts = value.split(":")
    if len(parts) != 2:
        raise SettingsError(
            f"{key}={value!r} is not a valid HH:MM time. Expected 24-hour format, e.g. '18:00'."
        )
    hh_raw, mm_raw = parts
    if not (hh_raw.isdigit() and mm_raw.isdigit() and len(hh_raw) == 2 and len(mm_raw) == 2):
        raise SettingsError(
            f"{key}={value!r} is not a valid HH:MM time. Expected 24-hour format, e.g. '18:00'."
        )
    hh, mm = int(hh_raw), int(mm_raw)
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise SettingsError(
            f"{key}={value!r} is not a valid HH:MM time. Hour must be 00-23 and minute 00-59."
        )
    return hh, mm


def _parse_scan_windows(value: str) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """Parse "HH:MM-HH:MM,HH:MM-HH:MM,..." into (start,end) pairs, end after start.

    Raises SettingsError on any malformed window instead of silently ignoring it —
    a bad scan window would otherwise silently starve the scan job for that slot.
    """
    windows: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for raw_window in value.split(","):
        window = raw_window.strip()
        if not window:
            raise SettingsError(
                f"SCHEDULER_SCAN_WINDOWS={value!r} contains an empty window. Expected a "
                f"comma-separated list of 'HH:MM-HH:MM' ranges, e.g. "
                f"'09:35-09:50,12:00-12:15,15:45-16:00'."
            )
        bounds = window.split("-")
        if len(bounds) != 2:
            raise SettingsError(
                f"SCHEDULER_SCAN_WINDOWS={value!r} has a malformed window {window!r}. "
                f"Expected 'HH:MM-HH:MM', e.g. '09:35-09:50'."
            )
        start_raw, end_raw = bounds
        start = _parse_hhmm(start_raw.strip(), "SCHEDULER_SCAN_WINDOWS")
        end = _parse_hhmm(end_raw.strip(), "SCHEDULER_SCAN_WINDOWS")
        if end <= start:
            raise SettingsError(
                f"SCHEDULER_SCAN_WINDOWS={value!r} has window {window!r} where the end "
                f"time is not after the start time."
            )
        windows.append((start, end))
    if not windows:
        raise SettingsError(
            f"SCHEDULER_SCAN_WINDOWS={value!r} must contain at least one 'HH:MM-HH:MM' window."
        )
    return windows


def load_dotenv(path: str = ".env") -> dict:
    """Minimal `.env` parser. Does NOT override variables already in the env.

    Returns the dict of values it applied (for visibility/testing). Keeping this
    dependency-free means mock mode needs nothing installed.
    """
    applied: dict[str, str] = {}
    if not os.path.exists(path):
        return applied
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
                    applied[key] = value
    except OSError:
        pass
    return applied


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of resolved configuration."""

    # --- AI ---
    openai_api_key: str
    openai_primary_model: str
    openai_review_model: str
    anthropic_api_key: str
    claude_review_model: str

    # --- market data (v1: Alpaca only, IEX feed) ---
    data_provider: str
    market_data_feed: str

    # --- news (v1: disabled / no-news mode) ---
    news_enabled: bool
    news_provider: str

    # --- deferred provider keys (NOT used in v1) ---
    massive_api_key: str
    benzinga_api_key: str

    # --- broker (Alpaca paper only) ---
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_paper: bool
    alpaca_base_url: str

    # --- execution (v1: simulated internally) ---
    execution_provider: str
    allow_real_orders_raw: str
    require_manual_approval: bool

    # --- broker protection watchdog / multi-day TIF policy ---
    # A swing-hold bracket (max_holding_days >= 1, i.e. may cross a session
    # boundary) must not use day-TIF protective legs (root cause of the
    # 2026-07-02 META incident: day-TIF legs expired at session close, leaving
    # the position naked overnight). GTC is the default for any max_holding_days
    # >= 1 unless explicitly opted out via allow_day_tif_for_multiday_positions;
    # only max_holding_days==0 (pure intraday) keeps day-TIF (Opus audit HIGH-1).
    protective_order_time_in_force: str
    requires_persistent_protection: bool
    allow_day_tif_for_multiday_positions: bool
    # PR2.6 hardening: consecutive per-position broker-lookup failures
    # (check_error) before the watchdog escalates to CRITICAL/unverifiable and
    # blocks new entries -- protection state that can't be confirmed must not
    # be silently treated as safe forever.
    protection_check_error_escalation_threshold: int

    # --- scheduler v1.5 (cadence layer; scan/monitor/outcomes/digest jobs) ---
    scheduler_ai_cost_cap_calls_per_30d: int
    scheduler_scan_windows: str
    scheduler_monitor_interval_minutes: int
    scheduler_outcomes_interval_minutes: int
    scheduler_digest_time: str
    scheduler_stale_job_minutes: int
    # --- scheduler v1.6 (PR9: unattended cadence -- self-halt fuse + heartbeat) ---
    scheduler_max_consecutive_failures: int
    scheduler_heartbeat_stale_minutes: int
    # --- scheduler v1.7 (PR9.5: benchmark spine capture cadence) ---
    scheduler_benchmark_spine_time: str

    # --- notifications ---
    ntfy_topic: str

    # --- runtime / safety ---
    mode: RuntimeMode
    approval_mode: ApprovalMode
    real_trading_enabled_raw: str
    run_mode: str
    offline_mode_flag: bool

    # --- risk limits ---
    max_risk_per_trade_pct: float
    max_paper_trades_per_day: int
    max_open_positions: int
    max_daily_loss_pct: float
    paper_equity: float
    max_auto_approvals_per_day: int
    max_spread_pct: float
    min_dollar_volume: float

    # --- freshness ---
    max_data_age_seconds: float
    max_quote_age_seconds_rth: float
    max_bar_age_seconds_rth: float
    max_quote_age_seconds_premarket: float
    max_bar_age_seconds_premarket: float
    max_price_drift_bps_since_proposal: float

    # --- cost model (realistic-cost accounting) ---
    cost_commission_per_share: float
    cost_min_commission: float
    cost_slippage_bps: float

    # --- trade sizing (stop distance fraction of entry / target reward:risk) ---
    stop_loss_pct: float
    target_reward_risk: float
    min_reward_risk: float

    # --- interest scanner + AI category labelling (Roadmap 2.3) ---
    labelling_enabled: bool
    interest_scan_top_n: int
    max_candidates_to_ai: int
    label_model: str
    label_max_output_tokens: int
    label_propose_threshold: float
    label_min_confidence_to_propose: float
    labeller_failsafe_warn_rate: float
    labeller_failsafe_critical_rate: float
    labeller_failsafe_min_sample: int
    interest_near_extreme_pct: float
    interest_min_score: float

    # --- official news / catalyst enrichment (Roadmap 2.4) ---
    # DISTINCT from the no-news posture (NEWS_ENABLED / NEWS_PROVIDER): this layer
    # adds OFFICIAL catalyst CONTEXT to the candidate packet + dashboard. It never
    # reaches the OpenAI momentum eval (which stays no-news) and never executes.
    news_enrichment_enabled: bool
    news_enrichment_provider: str          # mock | alpaca | disabled
    news_lookback_hours: float
    news_max_articles_per_symbol: int
    news_max_symbols_per_scan: int
    news_max_age_hours: float
    news_timeout_seconds: float
    news_fail_open_as_unavailable: bool

    # --- last30days research / narrative-context enrichment (Roadmap 2.5) ---
    # SEPARATE from official news (2.4) and from the no-news eval posture. Disabled
    # by default; the live provider shells out to a globally-installed last30days
    # skill (no vendored code). Context only — never execution authority.
    last30days_enabled: bool
    last30days_provider: str               # mock | cli | disabled
    last30days_python: str                 # interpreter for the skill (>=3.12; NOT system python3)
    last30days_repo_path: str              # dir containing scripts/last30days.py ("" = auto-resolve)
    last30days_cmd: str                    # full command-template override ("" = build from parts)
    last30days_sources: str                # keyless-only by default
    last30days_profile: str                # quick | deep
    last30days_timeout_seconds: float
    last30days_max_symbols_per_scan: int
    last30days_max_themes: int
    last30days_lookback_hours: float
    last30days_feed_to_labeller: bool
    last30days_fail_open_as_unavailable: bool

    # --- last30days narrative polarity (Roadmap 2.7) ---
    # LLM-derived directional polarity over last30days clusters. Context only:
    # can ARM an override upgrade when aligned + high-confidence, but never trades,
    # bypasses a gate, or skips manual approval. Hype/meme/squeeze -> high-risk
    # narrative (manual-only). Safe defaults: disabled + arming off.
    last30days_polarity_enabled: bool
    last30days_polarity_model: str
    last30days_polarity_min_confidence: float
    last30days_polarity_min_source_coverage: str   # low | medium | high
    last30days_polarity_arming_allowed: bool
    last30days_high_risk_narrative_manual_only: bool

    # --- earnings proximity (Roadmap PR5) ---
    # Event-risk AWARENESS, not execution authority: flags whether a candidate's
    # intended holding window contains an earnings event. Advisory only in this
    # PR -- it never hard-blocks a trade, never bypasses a gate/approval, and is
    # never fed into the AI eval/labeller prompt (unlike last30days). Enabled by
    # default with a mock/static provider (zero-cost, deterministic) since this
    # is informational, not a paid live API call; a real provider can replace
    # the mock behind the same factory later without touching call sites.
    earnings_proximity_enabled: bool
    earnings_proximity_provider: str          # mock | static | disabled
    earnings_proximity_warning_days: int      # "near-term" warning window (calendar days)
    earnings_proximity_default_hold_days: int  # fallback when max_holding_days isn't known yet
    earnings_proximity_max_symbols_per_scan: int
    earnings_proximity_timeout_seconds: float  # reserved for a future live provider; mock ignores it
    earnings_proximity_fail_open_as_unavailable: bool

    # --- proposal TTL / stale-approval guard (Roadmap PR6) ---
    # Safety guard, not a toggle-able feature: a proposal is only approvable
    # while fresh. There is deliberately NO master enable/disable switch here
    # (unlike earnings' provider toggle) -- unlike an advisory flag, this PR's
    # whole purpose is an unconditional guarantee ("no stale proposal can be
    # approved"), so it stays active. Extended-hours (premarket+afterhours)
    # share ONE conservative bucket, mirroring FreshnessGuard's own precedent
    # of using a single lenient threshold pair for both. A proposal born while
    # the market is CLOSED gets the shortest TTL of all (fails safe) -- belt
    # and suspenders on top of the freshness guard's existing hard block on
    # CLOSED-session approval.
    proposal_ttl_rth_seconds: int
    proposal_ttl_extended_hours_seconds: int
    proposal_ttl_closed_session_seconds: int

    # --- TQS v0 shadow scoring (Roadmap PR7) ---
    # Measurement-only: computes an attention-worthiness ranking signal for
    # every scored candidate/proposal and journals it to a SEPARATE tqs_scores
    # table for later comparison against candidate_outcomes/trade_outcomes.
    # No decision path may read this table (see alphaos/tqs/ module
    # docstring). Weights, normalization rules, and bucket thresholds are
    # CODE CONSTANTS (alphaos/tqs/scoring.py's TQS_VERSION), not settings --
    # unlike earnings' provider toggle, there is nothing here an operator
    # should be able to tune, since env-tunable weights would destroy
    # comparability across the shadow record this PR exists to build. The
    # only setting is the master on/off switch (pure computation, zero cost,
    # so default True; off only disables the shadow write entirely, it can
    # never disable a gate since TQS never gates anything).
    tqs_shadow_enabled: bool

    # --- Attribution v2 / counterfactual ΔR (Roadmap PR8) ---
    # Measurement-only: pairs a decision-divergence event (user override, gate
    # block, TTL expiry, or execution vs frozen plan) with the ΔR the outcome
    # ledger (candidate_outcomes/trade_outcomes) already resolved, and journals
    # it to a SEPARATE attribution_records table. No decision path may read
    # this table (see alphaos/attribution/ module docstring). Formula/taxonomy
    # are CODE CONSTANTS tied to ATTRIBUTION_VERSION, not settings -- same
    # rationale as TQS's own single toggle. Off disables discovery/resolution
    # entirely (zero queries, not just zero writes); it can never disable a
    # gate since attribution never gates anything.
    attribution_enabled: bool

    # --- gated labeller decision override (Roadmap 2.6) ---
    # Default OFF -> the AI label stays DOWNGRADE-ONLY (legacy safe behaviour).
    # When ON, the label may move the eval's decision UP or DOWN, but ONLY when
    # armed: real AI + a real (non-mock, non-'unknown') catalyst/sentiment driver.
    # Inert while mock. Never bypasses gates / manual approval / real-money guard.
    labeller_decision_override_enabled: bool

    # --- EXP-0: shadow-tier deterministic universe capture ---
    # Measurement-only expansion of the scanned universe downward in
    # liquidity: a committed, git-versioned symbol file (built by the
    # ``universe_build`` CLI, never a scheduler job) is scanned every window
    # alongside the core 20-name book, tagged ``tier=watchlist``. Zero AI
    # calls, zero decision surface -- the labeller/proposal chokepoints
    # structurally refuse ``shadow_tier=1`` candidates (see orchestrator.py).
    # Defaults OFF: the shadow pass has nothing safe to iterate until an
    # operator has actually run ``universe_build``, reviewed the resulting
    # symbol list, and committed it (the spec's own acceptance gate) --
    # flipping this on is a deliberate, later, separate step, not a side
    # effect of shipping the mechanism.
    shadow_tier_enabled: bool
    shadow_tier_universe_file: str        # committed JSON path
    shadow_tier_min_adv_usd: float         # $5M -- 20d avg dollar volume floor
    shadow_tier_max_adv_usd: float         # $50M -- ceiling (megacap book starts above this)
    shadow_tier_min_price: float           # $5 -- excludes penny-stock manipulation risk
    shadow_tier_max_price: float           # $100
    shadow_tier_adv_lookback_days: int     # 20
    shadow_tier_target_count: int          # ~300 names, builder soft target
    shadow_tier_max_count: int             # 500 hard cap

    # --- REG-1: regime classifier + packet stamping ---
    # Shadow/measurement only -- no arming, no gating, no allocation changes.
    # Defaults ON (unlike shadow_tier_enabled): unlike EXP-0's shadow tier,
    # there is no human-review gate blocking this from being safe on day
    # one -- it's pure computation from data already being captured
    # (benchmark_bars), and the whole point is that every packet journaled
    # from now on is born regime-stamped instead of retrofitted, so waiting
    # for a manual flip would defeat that.
    regime_enabled: bool
    regime_backfill_lookback_days: int     # ~2.5yr: covers the classifier's own
                                            # trailing-1-year vol-percentile window
                                            # plus meaningful history beyond it

    # --- TEXT-0: point-in-time EDGAR text archive (collect only) ---
    # No trading logic, no scanner, no AI calls, no scoring -- pure collection.
    # Defaults OFF (unlike REG-1): unlike a pure local computation, this makes
    # real outbound HTTP requests to a third party under the operator's own
    # identifying contact email -- that's a deliberate one-time operator
    # input this mechanism should never assume for them, same posture as
    # EXP-0's shadow_tier_enabled default-off pending a reviewed universe
    # file. text_archive_enabled=true with no contact email configured is
    # ALSO inert (make_edgar_provider refuses without one) -- belt and
    # suspenders, not just a single switch.
    text_archive_enabled: bool
    sec_edgar_contact_email: str            # required for any live EDGAR fetch (SEC fair-access policy)
    scheduler_text_archive_pull_time: str    # "HH:MM" SGT, once-daily cadence (mirrors benchmark_spine)

    # --- storage / dev ---
    db_path: str
    jsonl_mirror: bool
    allow_fixture_news: bool

    # ------------------------------------------------------------------ helpers
    @property
    def is_mock(self) -> bool:
        return self.mode == RuntimeMode.MOCK

    @property
    def is_paper(self) -> bool:
        return self.mode == RuntimeMode.PAPER

    @property
    def real_trading_enabled(self) -> bool:
        """True ONLY if the raw value is exactly 'false'... inverted on purpose.

        We never return True here. The raw string is what matters: anything
        other than exactly 'false' is treated as a fault by the order guard.
        v1 has no path that flips this on.
        """
        return False

    @property
    def real_trading_value_ok(self) -> bool:
        return self.real_trading_enabled_raw == REAL_TRADING_REQUIRED_VALUE

    @property
    def allow_real_orders(self) -> bool:
        """Never True in v1 — real orders are unreachable regardless of raw value."""
        return False

    @property
    def allow_real_orders_value_ok(self) -> bool:
        return self.allow_real_orders_raw == "false"

    @property
    def offline_mode(self) -> bool:
        """True when market data should be mocked (no live provider calls).

        Driven by mock runtime mode or the explicit OFFLINE_MODE / RUN_MODE=mock
        toggles. Mock mode is always explicit and surfaced in System Health.
        """
        return self.is_mock or self.offline_mode_flag or self.run_mode == "mock"

    @property
    def market_data_mode(self) -> str:
        return MarketDataMode.MOCK.value if self.offline_mode else MarketDataMode.LIVE.value

    @property
    def real_paper_execution(self) -> bool:
        """True when real Alpaca PAPER orders should be placed (still no real money)."""
        return self.execution_provider == ExecutionProvider.ALPACA_PAPER.value

    @property
    def effective_approval_mode(self) -> ApprovalMode:
        """Auto only if APPROVAL_MODE=auto AND REQUIRE_MANUAL_APPROVAL is off."""
        if self.approval_mode == ApprovalMode.AUTO and not self.require_manual_approval:
            return ApprovalMode.AUTO
        return ApprovalMode.MANUAL

    @property
    def has_openai_key(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def has_anthropic_key(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def has_massive_key(self) -> bool:
        return bool(self.massive_api_key)

    @property
    def has_benzinga_key(self) -> bool:
        return bool(self.benzinga_api_key)

    @property
    def has_alpaca_keys(self) -> bool:
        return bool(self.alpaca_api_key) and bool(self.alpaca_secret_key)

    # ------------------------------------------------------------- validation
    def validate_startup(self) -> list[StartupCheck]:
        """Run all startup-safety checks. Never raises; returns a report.

        The caller (orchestrator/dashboard) logs each failing check to
        ``system_events`` and decides whether to refuse paper execution.
        """
        checks: list[StartupCheck] = []

        # 1) REAL_TRADING_ENABLED must be exactly "false".
        checks.append(
            StartupCheck(
                name="real_trading_disabled",
                ok=self.real_trading_value_ok,
                detail=(
                    "REAL_TRADING_ENABLED is exactly 'false'."
                    if self.real_trading_value_ok
                    else f"REAL_TRADING_ENABLED must be 'false', got "
                    f"{self.real_trading_enabled_raw!r}. All orders are blocked."
                ),
                severity=Severity.CRITICAL,
            )
        )

        # 2) Mode must be an ACTIVE mode to actually run. Stubs are recognized
        #    but not runnable; live is not even a member of the enum.
        if self.mode in ACTIVE_MODES:
            mode_ok, mode_detail, sev = True, f"mode={self.mode.value}", Severity.INFO
        elif self.mode in STUB_MODES:
            mode_ok, mode_detail, sev = (
                False,
                f"mode={self.mode.value} is a recognized-but-inactive stub in v1.",
                Severity.ERROR,
            )
        else:  # pragma: no cover - load_settings rejects unknown modes earlier
            mode_ok, mode_detail, sev = (False, f"mode={self.mode} unsupported", Severity.CRITICAL)
        checks.append(StartupCheck("runtime_mode", mode_ok, mode_detail, sev))

        # 3) Paper-mode broker safety set.
        if self.is_paper:
            checks.append(
                StartupCheck(
                    "alpaca_paper_flag",
                    self.alpaca_paper,
                    "ALPACA_PAPER=true" if self.alpaca_paper else "ALPACA_PAPER must be true",
                    Severity.CRITICAL,
                )
            )
            base_ok = self.alpaca_base_url.rstrip("/") == PAPER_BASE_URL
            checks.append(
                StartupCheck(
                    "alpaca_base_url",
                    base_ok,
                    f"ALPACA_BASE_URL={self.alpaca_base_url}"
                    if base_ok
                    else f"ALPACA_BASE_URL must be {PAPER_BASE_URL}",
                    Severity.CRITICAL,
                )
            )
            checks.append(
                StartupCheck(
                    "alpaca_credentials",
                    self.has_alpaca_keys,
                    "Alpaca API key + secret present"
                    if self.has_alpaca_keys
                    else "ALPACA_API_KEY and ALPACA_SECRET_KEY are required in paper mode",
                    Severity.CRITICAL,
                )
            )

        # 4) Mock mode must not require any external key (informational).
        if self.is_mock:
            checks.append(
                StartupCheck(
                    "mock_offline_ready",
                    True,
                    "mock mode runs offline with zero external keys; no broker calls.",
                    Severity.INFO,
                )
            )

        # 5) Fixture-news dev flag must be OFF in paper mode.
        if self.allow_fixture_news and self.is_paper:
            checks.append(
                StartupCheck(
                    "fixture_news_flag",
                    False,
                    "ALLOW_FIXTURE_NEWS must not be enabled in paper mode.",
                    Severity.CRITICAL,
                )
            )

        # 5b) Market-data provider must be Alpaca (the only active v1 provider).
        provider_ok = self.data_provider == DataProvider.ALPACA.value
        checks.append(
            StartupCheck(
                "data_provider",
                provider_ok,
                f"DATA_PROVIDER={self.data_provider} (feed={self.market_data_feed})"
                if provider_ok
                else f"DATA_PROVIDER must be 'alpaca' in v1, got {self.data_provider!r}",
                Severity.CRITICAL,
            )
        )

        # 5c) Live market data requires Alpaca creds — never a silent fallback.
        if not self.offline_mode:
            checks.append(
                StartupCheck(
                    "market_data_credentials",
                    self.has_alpaca_keys,
                    "Alpaca market-data credentials present"
                    if self.has_alpaca_keys
                    else "live market data requires Alpaca creds; NO silent fallback to mock/other",
                    Severity.CRITICAL,
                )
            )
        else:
            checks.append(
                StartupCheck(
                    "market_data_mode",
                    True,
                    "market data is MOCKED (offline); clearly labelled, not live.",
                    Severity.WARNING,
                )
            )

        # 5d) News must be disabled in v1 (no-news mode).
        checks.append(
            StartupCheck(
                "news_disabled_v1",
                not self.news_enabled,
                "news disabled (no-news momentum baseline)"
                if not self.news_enabled
                else "NEWS_ENABLED=true is unsupported in v1",
                Severity.CRITICAL,
            )
        )

        # 5e) Execution stays simulated-internal; real orders unreachable.
        exec_ok = self.execution_provider in (
            ExecutionProvider.SIMULATED_INTERNAL.value,
            ExecutionProvider.ALPACA_PAPER.value,
        )
        checks.append(
            StartupCheck(
                "execution_provider",
                exec_ok,
                f"execution_provider={self.execution_provider}"
                + (" (real Alpaca PAPER orders; no real money)" if self.real_paper_execution else " (simulated)"),
                Severity.CRITICAL,
            )
        )
        # Real paper execution needs the full Alpaca paper safety set.
        if self.real_paper_execution:
            checks.append(
                StartupCheck(
                    "alpaca_paper_exec_ready",
                    self.is_paper and self.has_alpaca_keys and self.alpaca_paper
                    and self.alpaca_base_url.rstrip("/") == PAPER_BASE_URL,
                    "alpaca_paper execution prerequisites met"
                    if (self.is_paper and self.has_alpaca_keys)
                    else "alpaca_paper execution requires paper mode + Alpaca paper creds + paper base URL",
                    Severity.CRITICAL,
                )
            )
        checks.append(
            StartupCheck(
                "real_orders_disabled",
                self.allow_real_orders_value_ok,
                "ALLOW_REAL_ORDERS=false"
                if self.allow_real_orders_value_ok
                else f"ALLOW_REAL_ORDERS must be 'false', got {self.allow_real_orders_raw!r}",
                Severity.CRITICAL,
            )
        )

        # 6) Risk limits sanity.
        risk_ok = (
            self.max_risk_per_trade_pct > 0
            and self.max_open_positions > 0
            and self.max_paper_trades_per_day > 0
            and self.paper_equity > 0
            and self.max_auto_approvals_per_day >= 0
        )
        checks.append(
            StartupCheck(
                "risk_limits_sane",
                risk_ok,
                "risk limits look sane" if risk_ok else "one or more risk limits are non-positive",
                Severity.ERROR,
            )
        )

        return checks

    def startup_ok(self) -> bool:
        """True if no CRITICAL or ERROR check failed."""
        for c in self.validate_startup():
            if not c.ok and c.severity in (Severity.ERROR, Severity.CRITICAL):
                return False
        return True

    def paper_execution_allowed(self) -> tuple[bool, list[StartupCheck]]:
        """Whether Alpaca paper execution may start. Returns (ok, failing_checks)."""
        failing = [c for c in self.validate_startup() if not c.ok and c.severity == Severity.CRITICAL]
        if not self.is_paper:
            # In mock mode we don't *start* paper execution at all.
            return (False, failing)
        return (len(failing) == 0, failing)


def load_settings(load_env_file: bool = True, env: Optional[dict] = None) -> Settings:
    """Resolve settings from the environment (and ``.env`` unless disabled).

    Raises SettingsError only for a genuinely unsupported/unreachable mode
    (e.g. ``live``), which must never become a code path.
    """
    if load_env_file and env is None:
        load_dotenv()
    src = env if env is not None else os.environ

    mode_raw = _get(src, "ALPHAOS_MODE", "mock").lower()
    # `live` must be unreachable. Reject it loudly.
    if mode_raw == "live":
        raise SettingsError(
            "ALPHAOS_MODE=live is not a valid or reachable mode in v1. "
            "Real-money trading does not exist in this build."
        )
    try:
        mode = RuntimeMode(mode_raw)
    except ValueError:
        raise SettingsError(
            f"ALPHAOS_MODE={mode_raw!r} is not recognized. "
            f"Valid: mock | paper (shadow/research are inactive stubs)."
        )

    approval_raw = _get(src, "APPROVAL_MODE", "manual").lower()
    try:
        approval_mode = ApprovalMode(approval_raw)
    except ValueError:
        approval_mode = ApprovalMode.MANUAL

    # --- v1 fail-fast config validation -------------------------------------
    data_provider = _get(src, "DATA_PROVIDER", "alpaca").lower()
    if data_provider != DataProvider.ALPACA.value:
        raise SettingsError(
            f"DATA_PROVIDER={data_provider!r} is not supported in v1. "
            f"Alpaca is the only active market-data provider (Massive is deferred)."
        )

    news_enabled = _get_bool(src, "NEWS_ENABLED", False)
    if news_enabled:
        raise SettingsError(
            "NEWS_ENABLED=true is unsupported in v1. The system runs in no-news mode; "
            "Benzinga/web news are deferred. Set NEWS_ENABLED=false."
        )

    execution_provider = _get(src, "EXECUTION_PROVIDER", "simulated_internal").lower()
    _valid_exec = {ExecutionProvider.SIMULATED_INTERNAL.value, ExecutionProvider.ALPACA_PAPER.value}
    if execution_provider not in _valid_exec:
        raise SettingsError(
            f"EXECUTION_PROVIDER={execution_provider!r} is not supported. "
            f"Valid: simulated_internal | alpaca_paper (paper-only)."
        )
    if execution_provider == ExecutionProvider.ALPACA_PAPER.value and mode != RuntimeMode.PAPER:
        raise SettingsError(
            "EXECUTION_PROVIDER=alpaca_paper requires ALPHAOS_MODE=paper "
            "(real paper orders need live Alpaca connectivity)."
        )

    allow_real_orders_raw = _get(src, "ALLOW_REAL_ORDERS", "false").lower()
    if allow_real_orders_raw != "false":
        raise SettingsError(
            f"ALLOW_REAL_ORDERS={allow_real_orders_raw!r} is not allowed in v1. "
            f"Real orders are unreachable; ALLOW_REAL_ORDERS must be 'false'."
        )

    protective_order_time_in_force = _get(src, "PROTECTIVE_ORDER_TIME_IN_FORCE", "gtc").lower()
    if protective_order_time_in_force not in ("gtc", "day"):
        raise SettingsError(
            f"PROTECTIVE_ORDER_TIME_IN_FORCE={protective_order_time_in_force!r} is not supported. "
            f"Valid: gtc | day."
        )
    allow_day_tif_for_multiday_positions = _get_bool(src, "ALLOW_DAY_TIF_FOR_MULTIDAY_POSITIONS", False)
    # PR2.6 hardening: reject the contradictory combination that would silently
    # make EVERY swing hold (max_holding_days >= 1) submit day-TIF protective
    # legs -- exactly the failure mode PROTECTIVE_ORDER_TIME_IN_FORCE/
    # ALLOW_DAY_TIF_FOR_MULTIDAY_POSITIONS exist to prevent (root cause of the
    # 2026-07-02 META incident). A silent override here would be exactly the
    # anti-pattern this whole policy exists to close off; fail fast instead.
    if protective_order_time_in_force == "day" and not allow_day_tif_for_multiday_positions:
        raise SettingsError(
            "PROTECTIVE_ORDER_TIME_IN_FORCE=day contradicts "
            "ALLOW_DAY_TIF_FOR_MULTIDAY_POSITIONS=false (the default): this combination "
            "would silently make every swing hold (max_holding_days>=1) submit day-TIF "
            "protective legs, which expire at session close and can leave a position "
            "naked overnight undetected -- the exact 2026-07-02 META incident. "
            "Either set PROTECTIVE_ORDER_TIME_IN_FORCE=gtc (recommended, the default), "
            "or explicitly set ALLOW_DAY_TIF_FOR_MULTIDAY_POSITIONS=true to acknowledge "
            "you intend day-TIF protection for swing holds too."
        )

    protection_check_error_escalation_threshold = _get_int(
        src, "PROTECTION_CHECK_ERROR_ESCALATION_THRESHOLD", 3)
    # A value that's too low escalates on noise; a value that's too high (or
    # unbounded) silently defeats the whole point of escalation -- an
    # unverifiable position would sit in fail-open check_error indefinitely.
    # 1-10 keeps escalation both meaningful and reachable in practice.
    if not (1 <= protection_check_error_escalation_threshold <= 10):
        raise SettingsError(
            f"PROTECTION_CHECK_ERROR_ESCALATION_THRESHOLD="
            f"{protection_check_error_escalation_threshold!r} must be between 1 and 10. "
            f"1 escalates on the first broker-lookup failure; too high a value silently "
            f"disables escalation and reintroduces fail-open behavior for an unverifiable "
            f"position."
        )

    # --- scheduler v1.5: AI cost cap, scan windows, monitor/outcomes cadence,
    # digest time, stale-job threshold (all cadence-layer, none change scan/
    # AI-labeller/risk/strategy behavior) --------------------------------------
    scheduler_ai_cost_cap_calls_per_30d = _get_int(
        src, "SCHEDULER_AI_COST_CAP_CALLS_PER_30D", 2000)
    if not (50 <= scheduler_ai_cost_cap_calls_per_30d <= 100000):
        raise SettingsError(
            f"SCHEDULER_AI_COST_CAP_CALLS_PER_30D={scheduler_ai_cost_cap_calls_per_30d!r} "
            f"must be between 50 and 100000. Too low trips on ordinary daily scan volume "
            f"and silently starves the scan job every day; too high (or unbounded) defeats "
            f"the purpose of a runaway-cost safety net."
        )

    scheduler_scan_windows = _get(
        src, "SCHEDULER_SCAN_WINDOWS", "09:35-09:50,12:00-12:15,15:45-16:00")
    _parse_scan_windows(scheduler_scan_windows)

    scheduler_monitor_interval_minutes = _get_int(
        src, "SCHEDULER_MONITOR_INTERVAL_MINUTES", 15)
    if not (1 <= scheduler_monitor_interval_minutes <= 240):
        raise SettingsError(
            f"SCHEDULER_MONITOR_INTERVAL_MINUTES={scheduler_monitor_interval_minutes!r} "
            f"must be between 1 and 240. Too low (e.g. <1) risks hammering the broker API "
            f"every scheduler tick; too high (>240) delays reaction to a real protection "
            f"incident or exit condition, defeating the purpose of a monitor cadence."
        )

    scheduler_outcomes_interval_minutes = _get_int(
        src, "SCHEDULER_OUTCOMES_INTERVAL_MINUTES", 60)
    if not (5 <= scheduler_outcomes_interval_minutes <= 1440):
        raise SettingsError(
            f"SCHEDULER_OUTCOMES_INTERVAL_MINUTES={scheduler_outcomes_interval_minutes!r} "
            f"must be between 5 and 1440. Too low wastes cycles for no benefit "
            f"(forward-return windows are measured in days, not minutes); too high "
            f"(>1440, i.e. more than a day) risks a large unmeasured backlog before it's "
            f"flagged."
        )

    scheduler_digest_time = _get(src, "SCHEDULER_DIGEST_TIME", "18:00")
    _parse_hhmm(scheduler_digest_time, "SCHEDULER_DIGEST_TIME")

    scheduler_stale_job_minutes = _get_int(src, "SCHEDULER_STALE_JOB_MINUTES", 30)
    if not (5 <= scheduler_stale_job_minutes <= 1440):
        raise SettingsError(
            f"SCHEDULER_STALE_JOB_MINUTES={scheduler_stale_job_minutes!r} must be between "
            f"5 and 1440. Too low false-flags a job that's just running a bit long as "
            f"'stale'; too high delays operator awareness of a genuinely crashed/stuck job."
        )

    # --- scheduler v1.6 (PR9): self-halt fuse threshold + heartbeat staleness.
    # A value too low fuses/pages on ordinary transient noise (a single flaky
    # broker timeout); too high (or unbounded) lets a genuinely broken job type
    # keep retrying forever with no operator awareness -- the exact failure
    # mode PR9 exists to close. Mirrors
    # PROTECTION_CHECK_ERROR_ESCALATION_THRESHOLD's validation pattern.
    scheduler_max_consecutive_failures = _get_int(src, "SCHEDULER_MAX_CONSECUTIVE_FAILURES", 3)
    if not (1 <= scheduler_max_consecutive_failures <= 20):
        raise SettingsError(
            f"SCHEDULER_MAX_CONSECUTIVE_FAILURES={scheduler_max_consecutive_failures!r} "
            f"must be between 1 and 20. 1 fuses on the first failure; too high silently "
            f"defeats the self-halt fuse and lets a broken job type keep retrying "
            f"unattended indefinitely."
        )

    scheduler_heartbeat_stale_minutes = _get_int(src, "SCHEDULER_HEARTBEAT_STALE_MINUTES", 120)
    if not (5 <= scheduler_heartbeat_stale_minutes <= 1440):
        raise SettingsError(
            f"SCHEDULER_HEARTBEAT_STALE_MINUTES={scheduler_heartbeat_stale_minutes!r} must "
            f"be between 5 and 1440. Too low false-pages during normal scan-window gaps; "
            f"too high delays the dead-man's-switch alert past the point of being useful."
        )

    # --- scheduler v1.7 (PR9.5): benchmark spine capture time. Defaults to
    # 30 minutes before the daily digest so a future digest/brief can consume
    # the same day's equity/SPY capture. Reuses the same HH:MM validation as
    # SCHEDULER_DIGEST_TIME.
    scheduler_benchmark_spine_time = _get(src, "SCHEDULER_BENCHMARK_SPINE_TIME", "17:30")
    _parse_hhmm(scheduler_benchmark_spine_time, "SCHEDULER_BENCHMARK_SPINE_TIME")

    # TEXT-0: once-daily EDGAR pull cadence. Reuses the same HH:MM validation.
    scheduler_text_archive_pull_time = _get(src, "SCHEDULER_TEXT_ARCHIVE_PULL_TIME", "07:00")
    _parse_hhmm(scheduler_text_archive_pull_time, "SCHEDULER_TEXT_ARCHIVE_PULL_TIME")

    # --- trade sizing: stop distance + target reward:risk (drive the mock
    # baseline; min_reward_risk also clamps live OpenAI proposals) ------------
    stop_loss_pct = _get_float(src, "STOP_LOSS_PCT", 0.03)
    target_reward_risk = _get_float(src, "TARGET_REWARD_RISK", 1.5)
    min_reward_risk = _get_float(src, "MIN_REWARD_RISK", 1.2)
    if not (0.0 < stop_loss_pct < 0.5):
        raise SettingsError(
            f"STOP_LOSS_PCT={stop_loss_pct!r} must be a fraction of entry in (0, 0.5)."
        )
    if target_reward_risk <= 0:
        raise SettingsError(f"TARGET_REWARD_RISK={target_reward_risk!r} must be > 0.")
    if min_reward_risk < 0:
        raise SettingsError(f"MIN_REWARD_RISK={min_reward_risk!r} must be >= 0.")

    # --- earnings proximity (PR5): warning window + conservative hold-days
    # fallback. Not a safety gate (advisory only), but a nonsensical value
    # (0 hold days, a multi-year warning window) would silently make the flag
    # meaningless, so bound it to a sane range.
    earnings_proximity_warning_days = _get_int(src, "EARNINGS_PROXIMITY_WARNING_DAYS", 7)
    if not (0 <= earnings_proximity_warning_days <= 90):
        raise SettingsError(
            f"EARNINGS_PROXIMITY_WARNING_DAYS={earnings_proximity_warning_days!r} must be "
            f"between 0 and 90. 0 disables the warning window (hold-window flagging still "
            f"works); >90 days stops being a meaningful 'near-term' warning for a 1-5 day "
            f"swing strategy."
        )
    earnings_proximity_default_hold_days = _get_int(src, "EARNINGS_PROXIMITY_DEFAULT_HOLD_DAYS", 3)
    if not (1 <= earnings_proximity_default_hold_days <= 30):
        raise SettingsError(
            f"EARNINGS_PROXIMITY_DEFAULT_HOLD_DAYS={earnings_proximity_default_hold_days!r} "
            f"must be between 1 and 30. This is the fallback hold period used only when a "
            f"real max_holding_days isn't known yet; 0 or unbounded both make the "
            f"hold-window flag meaningless."
        )

    # --- proposal TTL (PR6): this IS a safety gate, so bound it tightly. Too
    # long defeats the point (a stale proposal stays approvable); too short
    # (or negative) would make every proposal instantly unapprovable.
    proposal_ttl_rth_seconds = _get_int(src, "PROPOSAL_TTL_RTH_SECONDS", 1800)
    if not (60 <= proposal_ttl_rth_seconds <= 7200):
        raise SettingsError(
            f"PROPOSAL_TTL_RTH_SECONDS={proposal_ttl_rth_seconds!r} must be between 60 and "
            f"7200 (1-120 minutes). Too short and every regular-hours proposal expires before "
            f"a human can react; too long defeats the point of a staleness guard."
        )
    proposal_ttl_extended_hours_seconds = _get_int(src, "PROPOSAL_TTL_EXTENDED_HOURS_SECONDS", 300)
    if not (0 <= proposal_ttl_extended_hours_seconds <= 3600):
        raise SettingsError(
            f"PROPOSAL_TTL_EXTENDED_HOURS_SECONDS={proposal_ttl_extended_hours_seconds!r} must "
            f"be between 0 and 3600 (up to 60 minutes). Premarket/afterhours liquidity is "
            f"thinner, so this should stay conservative -- 0 means such proposals are never "
            f"approvable (immediate expiry), which is a valid conservative choice."
        )
    proposal_ttl_closed_session_seconds = _get_int(src, "PROPOSAL_TTL_CLOSED_SESSION_SECONDS", 0)
    if not (0 <= proposal_ttl_closed_session_seconds <= 3600):
        raise SettingsError(
            f"PROPOSAL_TTL_CLOSED_SESSION_SECONDS={proposal_ttl_closed_session_seconds!r} must "
            f"be between 0 and 3600. This is the fail-safe bucket for a proposal born while the "
            f"market is closed (an edge case already blocked separately by the freshness guard's "
            f"CLOSED_SESSION check) -- default 0 means immediate expiry."
        )

    return Settings(
        openai_api_key=_get(src, "OPENAI_API_KEY"),
        openai_primary_model=_get(src, "OPENAI_PRIMARY_MODEL", "gpt-4o-mini"),
        openai_review_model=_get(src, "OPENAI_REVIEW_MODEL", "gpt-4o-mini"),
        anthropic_api_key=_get(src, "ANTHROPIC_API_KEY"),
        claude_review_model=_get(src, "CLAUDE_REVIEW_MODEL", "claude-sonnet-4-6"),
        data_provider=data_provider,
        market_data_feed=_get(src, "MARKET_DATA_FEED", "iex").lower(),
        news_enabled=news_enabled,
        news_provider=_get(src, "NEWS_PROVIDER", "disabled").lower(),
        massive_api_key=_get(src, "MASSIVE_API_KEY"),
        benzinga_api_key=_get(src, "BENZINGA_API_KEY"),
        alpaca_api_key=_get(src, "ALPACA_API_KEY"),
        alpaca_secret_key=_get(src, "ALPACA_SECRET_KEY"),
        alpaca_paper=_get_bool(src, "ALPACA_PAPER", True),
        alpaca_base_url=_get(src, "ALPACA_BASE_URL", PAPER_BASE_URL),
        execution_provider=execution_provider,
        allow_real_orders_raw=allow_real_orders_raw,
        require_manual_approval=_get_bool(src, "REQUIRE_MANUAL_APPROVAL", True),
        protective_order_time_in_force=protective_order_time_in_force,
        requires_persistent_protection=_get_bool(src, "REQUIRES_PERSISTENT_PROTECTION", True),
        allow_day_tif_for_multiday_positions=allow_day_tif_for_multiday_positions,
        protection_check_error_escalation_threshold=protection_check_error_escalation_threshold,
        scheduler_ai_cost_cap_calls_per_30d=scheduler_ai_cost_cap_calls_per_30d,
        scheduler_scan_windows=scheduler_scan_windows,
        scheduler_monitor_interval_minutes=scheduler_monitor_interval_minutes,
        scheduler_outcomes_interval_minutes=scheduler_outcomes_interval_minutes,
        scheduler_digest_time=scheduler_digest_time,
        scheduler_stale_job_minutes=scheduler_stale_job_minutes,
        scheduler_max_consecutive_failures=scheduler_max_consecutive_failures,
        scheduler_heartbeat_stale_minutes=scheduler_heartbeat_stale_minutes,
        scheduler_benchmark_spine_time=scheduler_benchmark_spine_time,
        ntfy_topic=_get(src, "NTFY_TOPIC"),
        mode=mode,
        approval_mode=approval_mode,
        real_trading_enabled_raw=_get(src, "REAL_TRADING_ENABLED", "false").lower(),
        run_mode=_get(src, "RUN_MODE", "").lower(),
        offline_mode_flag=_get_bool(src, "OFFLINE_MODE", False),
        max_risk_per_trade_pct=_get_float(src, "MAX_RISK_PER_TRADE_PCT", 0.01),
        max_paper_trades_per_day=_get_int(src, "MAX_PAPER_TRADES_PER_DAY", 5),
        max_open_positions=_get_int(src, "MAX_OPEN_POSITIONS", 5),
        max_daily_loss_pct=_get_float(src, "MAX_DAILY_LOSS_PCT", 0.03),
        paper_equity=_get_float(src, "PAPER_EQUITY", 100000.0),
        max_auto_approvals_per_day=_get_int(src, "MAX_AUTO_APPROVALS_PER_DAY", 1),
        max_spread_pct=_get_float(src, "MAX_SPREAD_PCT", 0.01),
        min_dollar_volume=_get_float(src, "MIN_DOLLAR_VOLUME", 2_000_000.0),
        max_data_age_seconds=_get_float(src, "MAX_DATA_AGE_SECONDS", 120.0),
        max_quote_age_seconds_rth=_get_float(src, "MAX_QUOTE_AGE_SECONDS_RTH", 60.0),
        max_bar_age_seconds_rth=_get_float(src, "MAX_BAR_AGE_SECONDS_RTH", 180.0),
        max_quote_age_seconds_premarket=_get_float(src, "MAX_QUOTE_AGE_SECONDS_PREMARKET", 300.0),
        max_bar_age_seconds_premarket=_get_float(src, "MAX_BAR_AGE_SECONDS_PREMARKET", 600.0),
        max_price_drift_bps_since_proposal=_get_float(src, "MAX_PRICE_DRIFT_BPS_SINCE_PROPOSAL", 50.0),
        cost_commission_per_share=_get_float(src, "COST_COMMISSION_PER_SHARE", 0.0),
        cost_min_commission=_get_float(src, "COST_MIN_COMMISSION", 0.0),
        cost_slippage_bps=_get_float(src, "COST_SLIPPAGE_BPS", 1.0),
        stop_loss_pct=stop_loss_pct,
        target_reward_risk=target_reward_risk,
        min_reward_risk=min_reward_risk,
        labelling_enabled=_get_bool(src, "LABELLING_ENABLED", True),
        interest_scan_top_n=_get_int(src, "INTEREST_SCAN_TOP_N", 15),
        max_candidates_to_ai=_get_int(src, "MAX_CANDIDATES_TO_AI", 15),
        label_model=_get(src, "LABEL_MODEL", "") or _get(src, "OPENAI_PRIMARY_MODEL", "gpt-4o-mini"),
        # The labeller emits a ~16-field JSON object (labels + free-text reason /
        # thesis / invalidation / risk + advisory readiness). That needs ~250
        # completion tokens; the old 220 default truncated it (finish_reason=
        # length) so JSON parsing raised and EVERY candidate failed safe to
        # reject — the live pipeline could never propose. 800 leaves headroom and
        # costs nothing extra (billed on actual tokens; the model stops ~260).
        label_max_output_tokens=_get_int(src, "LABEL_MAX_OUTPUT_TOKENS", 800),
        label_propose_threshold=_get_float(src, "LABEL_PROPOSE_THRESHOLD", 0.40),
        label_min_confidence_to_propose=_get_float(src, "LABEL_MIN_CONFIDENCE_TO_PROPOSE", 0.50),
        # Labeller fail-safe VISIBILITY thresholds (warn/critical on high fail-safe
        # rate). Advisory only — never change the fail-safe behaviour itself.
        labeller_failsafe_warn_rate=_get_float(src, "LABELLER_FAILSAFE_WARN_RATE", 0.25),
        labeller_failsafe_critical_rate=_get_float(src, "LABELLER_FAILSAFE_CRITICAL_RATE", 0.50),
        labeller_failsafe_min_sample=_get_int(src, "LABELLER_FAILSAFE_MIN_SAMPLE", 5),
        interest_near_extreme_pct=_get_float(src, "INTEREST_NEAR_EXTREME_PCT", 0.005),
        interest_min_score=_get_float(src, "INTEREST_MIN_SCORE", 0.5),
        news_enrichment_enabled=_get_bool(src, "NEWS_ENRICHMENT_ENABLED", True),
        news_enrichment_provider=_get(src, "NEWS_ENRICHMENT_PROVIDER", "mock").lower(),
        news_lookback_hours=_get_float(src, "NEWS_LOOKBACK_HOURS", 48.0),
        news_max_articles_per_symbol=_get_int(src, "NEWS_MAX_ARTICLES_PER_SYMBOL", 5),
        news_max_symbols_per_scan=_get_int(src, "NEWS_MAX_SYMBOLS_PER_SCAN", 10),
        news_max_age_hours=_get_float(src, "NEWS_MAX_AGE_HOURS", 48.0),
        news_timeout_seconds=_get_float(src, "NEWS_TIMEOUT_SECONDS", 8.0),
        news_fail_open_as_unavailable=_get_bool(src, "NEWS_FAIL_OPEN_AS_UNAVAILABLE", True),
        last30days_enabled=_get_bool(src, "LAST30DAYS_ENABLED", False),
        last30days_provider=_get(src, "LAST30DAYS_PROVIDER", "mock").lower(),
        last30days_python=_get(src, "LAST30DAYS_PYTHON", "python3.12"),
        last30days_repo_path=_get(src, "LAST30DAYS_REPO_PATH", ""),
        last30days_cmd=_get(src, "LAST30DAYS_CMD", ""),
        last30days_sources=_get(src, "LAST30DAYS_SOURCES", "reddit,hackernews,polymarket,github"),
        last30days_profile=_get(src, "LAST30DAYS_PROFILE", "quick").lower(),
        last30days_timeout_seconds=_get_float(src, "LAST30DAYS_TIMEOUT_SECONDS", 45.0),
        last30days_max_symbols_per_scan=_get_int(src, "LAST30DAYS_MAX_SYMBOLS_PER_SCAN", 10),
        last30days_max_themes=_get_int(src, "LAST30DAYS_MAX_THEMES", 5),
        last30days_lookback_hours=_get_float(src, "LAST30DAYS_LOOKBACK_HOURS", 720.0),
        last30days_feed_to_labeller=_get_bool(src, "LAST30DAYS_FEED_TO_LABELLER", True),
        last30days_fail_open_as_unavailable=_get_bool(src, "LAST30DAYS_FAIL_OPEN_AS_UNAVAILABLE", True),
        labeller_decision_override_enabled=_get_bool(src, "LABELLER_DECISION_OVERRIDE_ENABLED", False),
        last30days_polarity_enabled=_get_bool(src, "LAST30DAYS_POLARITY_ENABLED", False),
        last30days_polarity_model=(_get(src, "LAST30DAYS_POLARITY_MODEL", "")
                                   or _get(src, "OPENAI_PRIMARY_MODEL", "gpt-4o-mini")),
        last30days_polarity_min_confidence=_get_float(src, "LAST30DAYS_POLARITY_MIN_CONFIDENCE", 0.6),
        last30days_polarity_min_source_coverage=_get(src, "LAST30DAYS_POLARITY_MIN_SOURCE_COVERAGE", "medium").lower(),
        last30days_polarity_arming_allowed=_get_bool(src, "LAST30DAYS_POLARITY_ARMING_ALLOWED", False),
        last30days_high_risk_narrative_manual_only=_get_bool(src, "LAST30DAYS_HIGH_RISK_NARRATIVE_MANUAL_ONLY", True),
        earnings_proximity_enabled=_get_bool(src, "EARNINGS_PROXIMITY_ENABLED", True),
        earnings_proximity_provider=_get(src, "EARNINGS_PROXIMITY_PROVIDER", "mock").lower(),
        earnings_proximity_warning_days=earnings_proximity_warning_days,
        earnings_proximity_default_hold_days=earnings_proximity_default_hold_days,
        earnings_proximity_max_symbols_per_scan=_get_int(src, "EARNINGS_PROXIMITY_MAX_SYMBOLS_PER_SCAN", 10),
        earnings_proximity_timeout_seconds=_get_float(src, "EARNINGS_PROXIMITY_TIMEOUT_SECONDS", 10.0),
        earnings_proximity_fail_open_as_unavailable=_get_bool(
            src, "EARNINGS_PROXIMITY_FAIL_OPEN_AS_UNAVAILABLE", True),
        shadow_tier_enabled=_get_bool(src, "SHADOW_TIER_ENABLED", False),
        shadow_tier_universe_file=_get(
            src, "SHADOW_TIER_UNIVERSE_FILE", "alphaos/universe/shadow_universe.json"),
        shadow_tier_min_adv_usd=_get_float(src, "SHADOW_TIER_MIN_ADV_USD", 5_000_000.0),
        shadow_tier_max_adv_usd=_get_float(src, "SHADOW_TIER_MAX_ADV_USD", 50_000_000.0),
        shadow_tier_min_price=_get_float(src, "SHADOW_TIER_MIN_PRICE", 5.0),
        shadow_tier_max_price=_get_float(src, "SHADOW_TIER_MAX_PRICE", 100.0),
        shadow_tier_adv_lookback_days=_get_int(src, "SHADOW_TIER_ADV_LOOKBACK_DAYS", 20),
        shadow_tier_target_count=_get_int(src, "SHADOW_TIER_TARGET_COUNT", 300),
        shadow_tier_max_count=_get_int(src, "SHADOW_TIER_MAX_COUNT", 500),
        regime_enabled=_get_bool(src, "REGIME_ENABLED", True),
        regime_backfill_lookback_days=_get_int(src, "REGIME_BACKFILL_LOOKBACK_DAYS", 900),
        text_archive_enabled=_get_bool(src, "TEXT_ARCHIVE_ENABLED", False),
        sec_edgar_contact_email=_get(src, "SEC_EDGAR_CONTACT_EMAIL", ""),
        scheduler_text_archive_pull_time=scheduler_text_archive_pull_time,
        proposal_ttl_rth_seconds=proposal_ttl_rth_seconds,
        proposal_ttl_extended_hours_seconds=proposal_ttl_extended_hours_seconds,
        proposal_ttl_closed_session_seconds=proposal_ttl_closed_session_seconds,
        tqs_shadow_enabled=_get_bool(src, "TQS_SHADOW_ENABLED", True),
        attribution_enabled=_get_bool(src, "ATTRIBUTION_ENABLED", True),
        db_path=_get(src, "ALPHAOS_DB_PATH", "data/alphaos.db"),
        jsonl_mirror=_get_bool(src, "ALPHAOS_JSONL_MIRROR", False),
        allow_fixture_news=_get_bool(src, "ALLOW_FIXTURE_NEWS", False),
    )
