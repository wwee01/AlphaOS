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
from dataclasses import dataclass, field
from typing import Optional

from alphaos.constants import (
    ACTIVE_MODES,
    STUB_MODES,
    ApprovalMode,
    DataProvider,
    ExecutionProvider,
    MarketDataFeed,
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
        label_max_output_tokens=_get_int(src, "LABEL_MAX_OUTPUT_TOKENS", 220),
        label_propose_threshold=_get_float(src, "LABEL_PROPOSE_THRESHOLD", 0.40),
        label_min_confidence_to_propose=_get_float(src, "LABEL_MIN_CONFIDENCE_TO_PROPOSE", 0.50),
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
        db_path=_get(src, "ALPHAOS_DB_PATH", "data/alphaos.db"),
        jsonl_mirror=_get_bool(src, "ALPHAOS_JSONL_MIRROR", False),
        allow_fixture_news=_get_bool(src, "ALLOW_FIXTURE_NEWS", False),
    )
