"""Earnings-proximity provider abstraction (PR5): factory gating and the
deterministic mock — HERMETIC (no network, no subprocess)."""

from __future__ import annotations

from alphaos.constants import EarningsDataStatus, EarningsTiming
from alphaos.earnings.earnings_provider import (
    MockEarningsProximityProvider,
    make_earnings_provider,
)
from conftest import make_settings


def test_factory_none_when_master_switch_off():
    assert make_earnings_provider(make_settings(EARNINGS_PROXIMITY_ENABLED="false")) is None


def test_factory_none_when_provider_disabled():
    s = make_settings(EARNINGS_PROXIMITY_ENABLED="true", EARNINGS_PROXIMITY_PROVIDER="disabled")
    assert make_earnings_provider(s) is None


def test_factory_mock_when_enabled():
    s = make_settings(EARNINGS_PROXIMITY_ENABLED="true", EARNINGS_PROXIMITY_PROVIDER="mock")
    assert isinstance(make_earnings_provider(s), MockEarningsProximityProvider)


def test_factory_static_is_an_alias_for_mock():
    s = make_settings(EARNINGS_PROXIMITY_ENABLED="true", EARNINGS_PROXIMITY_PROVIDER="static")
    assert isinstance(make_earnings_provider(s), MockEarningsProximityProvider)


def test_factory_force_ignores_master_switch():
    s = make_settings(EARNINGS_PROXIMITY_ENABLED="false", EARNINGS_PROXIMITY_PROVIDER="mock")
    assert make_earnings_provider(s) is None
    assert isinstance(make_earnings_provider(s, force=True), MockEarningsProximityProvider)


def test_mock_is_deterministic():
    p = MockEarningsProximityProvider()
    a = p.get_earnings_for_symbol("AAPL")
    b = p.get_earnings_for_symbol("AAPL")
    assert a.earnings_date == b.earnings_date
    assert a.status == b.status
    assert a.earnings_timing == b.earnings_timing
    assert a.source == "mock"


def test_mock_never_defaults_to_ok_silently():
    """Every result the mock returns has an EXPLICIT status -- never left blank
    for the caller to assume "safe"."""
    p = MockEarningsProximityProvider()
    for i in range(50):
        res = p.get_earnings_for_symbol(f"SYM{i}")
        assert res.status in (EarningsDataStatus.OK.value, EarningsDataStatus.UNAVAILABLE.value)
        if res.status == EarningsDataStatus.UNAVAILABLE.value:
            assert res.earnings_date is None
        else:
            assert res.earnings_date is not None
            assert res.earnings_timing in (
                EarningsTiming.BEFORE_OPEN.value, EarningsTiming.AFTER_CLOSE.value,
                EarningsTiming.UNKNOWN.value,
            )


def test_mock_produces_a_natural_mix_across_symbols():
    """Across enough symbols, the mock naturally exercises unavailable / in-window
    / far-out scenarios -- not just one flat case."""
    p = MockEarningsProximityProvider()
    statuses = {p.get_earnings_for_symbol(f"SYM{i}").status for i in range(60)}
    assert EarningsDataStatus.OK.value in statuses
    assert EarningsDataStatus.UNAVAILABLE.value in statuses
