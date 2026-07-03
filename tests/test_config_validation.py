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
    s = make_settings(SCHEDULER_AI_COST_CAP_CALLS_PER_30D=50)
    assert s.scheduler_ai_cost_cap_calls_per_30d == 50
    s = make_settings(SCHEDULER_AI_COST_CAP_CALLS_PER_30D=100000)
    assert s.scheduler_ai_cost_cap_calls_per_30d == 100000
