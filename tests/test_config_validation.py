"""Config validation: fail-fast on unsupported v1 settings, and no silent
fallback to another data source in live mode (Change Prompt §1, §5, §9)."""

from __future__ import annotations

import pytest

from alphaos.config.settings import SettingsError
from alphaos.data.market_data import MarketDataClient
from conftest import make_settings


def test_invalid_data_provider_fails_fast():
    with pytest.raises(SettingsError):
        make_settings(DATA_PROVIDER="yahoo")
    with pytest.raises(SettingsError):
        make_settings(DATA_PROVIDER="massive")


def test_news_enabled_true_fails_fast():
    with pytest.raises(SettingsError):
        make_settings(NEWS_ENABLED="true")


def test_allow_real_orders_true_fails_fast():
    with pytest.raises(SettingsError):
        make_settings(ALLOW_REAL_ORDERS="true")


def test_unsupported_execution_provider_fails_fast():
    with pytest.raises(SettingsError):
        make_settings(EXECUTION_PROVIDER="alpaca_paper")


def test_default_v1_config_is_alpaca_no_news_simulated():
    s = make_settings()
    assert s.data_provider == "alpaca"
    assert s.market_data_feed == "iex"
    assert s.news_enabled is False
    assert s.execution_provider == "simulated_internal"


def test_live_mode_missing_creds_does_not_fall_back_to_mock(journal):
    # paper (live data) mode with NO Alpaca creds must not silently mock.
    s = make_settings(ALPHAOS_MODE="paper")
    assert s.offline_mode is False
    client = MarketDataClient(s, journal)
    assert client.use_mock is False
    assert client.provider_name == "alpaca"  # NOT alpaca_mock
    snap = client.get_snapshot("AAPL")
    assert snap["is_mock"] is False
    # No creds => unusable data (null timestamp), never fabricated.
    assert snap["source_timestamp"] is None
    assert snap["last_price"] is None


def test_mock_mode_market_data_is_labelled_mock(journal):
    s = make_settings()  # mock
    client = MarketDataClient(s, journal)
    assert client.use_mock is True
    assert client.mode == "mock"
    snap = client.get_snapshot("AAPL")
    assert snap["is_mock"] is True
    assert snap["provider"] == "alpaca_mock"


def test_scheduler_cost_cap_bounds_validation():
    with pytest.raises(SettingsError):
        make_settings(SCHEDULER_AI_COST_CAP_CALLS_PER_30D=49)
    with pytest.raises(SettingsError):
        make_settings(SCHEDULER_AI_COST_CAP_CALLS_PER_30D=100001)
    # EXP-1: SHADOW_AI_CAP_CALLS_PER_30D's own joint-validation (<=25% of the
    # shared pool) must clear this lowered global cap too -- its default of
    # 500 only clears the default global cap of 2000.
    s = make_settings(SCHEDULER_AI_COST_CAP_CALLS_PER_30D=50, SHADOW_AI_CAP_CALLS_PER_30D=12)
    assert s.scheduler_ai_cost_cap_calls_per_30d == 50
    s = make_settings(SCHEDULER_AI_COST_CAP_CALLS_PER_30D=100000)
    assert s.scheduler_ai_cost_cap_calls_per_30d == 100000


def test_debate_daily_cap_cannot_exceed_25pct_of_shared_30day_cap():
    """PR14 audit fix (scope/safety HIGH): DEBATE_MAX_CALLS_PER_DAY's own
    [0, 500] bound and SCHEDULER_AI_COST_CAP_CALLS_PER_30D's own [50, 100000]
    bound are each individually sane, but were NOT jointly validated -- a
    legal combination (daily=500, shared at its own floor of 50) let debate
    alone exhaust the ENTIRE 30-day shared cap in a single day, starving the
    live evaluator for the rest of the window. Reuses the same 25%-of-pool
    ceiling this session's EXP-1 Fable consultation already established for
    an equivalent nested shadow sub-cap (500/2000)."""
    with pytest.raises(SettingsError):
        make_settings(DEBATE_MAX_CALLS_PER_DAY=500, SCHEDULER_AI_COST_CAP_CALLS_PER_30D=50)
    with pytest.raises(SettingsError):
        make_settings(DEBATE_MAX_CALLS_PER_DAY=13, SCHEDULER_AI_COST_CAP_CALLS_PER_30D=50)  # 13 > 12.5
    s = make_settings(
        DEBATE_MAX_CALLS_PER_DAY=12, SCHEDULER_AI_COST_CAP_CALLS_PER_30D=50,
        SHADOW_AI_CAP_CALLS_PER_30D=12,  # EXP-1's own joint-validation must clear this cap too
    )  # 12 <= 12.5
    assert s.debate_max_calls_per_day == 12
    s = make_settings()  # defaults: 10 <= 0.25 * 2000 = 500
    assert s.debate_max_calls_per_day == 10
    assert s.scheduler_ai_cost_cap_calls_per_30d == 2000


def test_benchmark_spine_time_malformed_fails_fast():
    with pytest.raises(SettingsError):
        make_settings(SCHEDULER_BENCHMARK_SPINE_TIME="25:99")
    with pytest.raises(SettingsError):
        make_settings(SCHEDULER_BENCHMARK_SPINE_TIME="not-a-time")


def test_benchmark_spine_time_valid_hhmm_accepted():
    s = make_settings(SCHEDULER_BENCHMARK_SPINE_TIME="09:00")
    assert s.scheduler_benchmark_spine_time == "09:00"
