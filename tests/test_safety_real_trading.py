"""Real-money trading is disabled and unreachable (deliverable test #1)."""

from __future__ import annotations

import pytest

from alphaos.broker.alpaca_client import AlpacaClient, AlpacaSafetyError
from alphaos.constants import ReasonCode
from alphaos.execution.order_manager import OrderManager
from alphaos.safety import real_trading_guard
from conftest import make_settings, make_proposal


def test_real_trading_enabled_property_is_always_false():
    s = make_settings(REAL_TRADING_ENABLED="true")
    # Even with a misconfigured raw value, the property never reports enabled.
    assert s.real_trading_enabled is False
    assert s.real_trading_value_ok is False


def test_real_trading_guard_denies_when_value_not_false():
    s = make_settings(REAL_TRADING_ENABLED="TRUE")
    verdict = real_trading_guard(s)
    assert verdict.allowed is False


def test_real_trading_guard_allows_only_paper_when_false():
    s = make_settings(REAL_TRADING_ENABLED="false")
    verdict = real_trading_guard(s)
    assert verdict.allowed is True  # "allowed" == paper/mock only; no live path exists


def test_order_manager_blocks_order_when_real_trading_not_false(journal):
    s = make_settings(REAL_TRADING_ENABLED="true")
    om = OrderManager(s, journal)
    result = om.execute_proposal(make_proposal())
    assert result.blocked is True
    assert result.block_reason == ReasonCode.REAL_TRADING_BLOCKED.value
    # No position may be opened.
    assert journal.count_open_positions() == 0
    # The blocked attempt is logged to system_events.
    events = journal.query(
        "SELECT * FROM system_events WHERE category='execution' AND severity='critical'"
    )
    assert any("BLOCKED" in e["message"] for e in events)


def test_alpaca_connector_refuses_unless_paper_and_real_false():
    # real trading not false -> refuse
    with pytest.raises(AlpacaSafetyError):
        AlpacaClient(make_settings(REAL_TRADING_ENABLED="true", ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s")).preflight()
    # paper flag off -> refuse
    with pytest.raises(AlpacaSafetyError):
        AlpacaClient(make_settings(ALPACA_PAPER="false", ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s")).preflight()
    # wrong base url -> refuse
    with pytest.raises(AlpacaSafetyError):
        AlpacaClient(
            make_settings(ALPACA_BASE_URL="https://api.alpaca.markets", ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s")
        ).preflight()
    # missing keys -> refuse
    with pytest.raises(AlpacaSafetyError):
        AlpacaClient(make_settings()).preflight()


def test_alpaca_submit_order_enforces_guard():
    bad = AlpacaClient(make_settings(REAL_TRADING_ENABLED="true", ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s"))
    with pytest.raises(AlpacaSafetyError):
        bad.submit_order({"symbol": "AAPL"})
