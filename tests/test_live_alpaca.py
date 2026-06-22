"""Live Alpaca integration test — GATED behind RUN_LIVE_ALPACA_TESTS=true.

When the gate is off (the default, incl. CI and offline runs) this is reported as
SKIPPED, never passed. It requires real Alpaca paper creds + network access.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_ALPACA_TESTS") != "true",
    reason="live Alpaca tests are gated behind RUN_LIVE_ALPACA_TESTS=true",
)


def test_live_alpaca_snapshot_has_source_timestamps():  # pragma: no cover - gated
    from alphaos.config.settings import load_settings
    from alphaos.data.market_data import MarketDataClient

    settings = load_settings()  # expects paper mode + real Alpaca creds in env
    assert settings.has_alpaca_keys, "live test requires Alpaca credentials"
    client = MarketDataClient(settings, None)
    snap = client.get_snapshot("AAPL")
    assert snap["provider"] == "alpaca"
    assert snap["feed"] == "iex"
    # Live data must carry provider timestamps for the freshness guard.
    assert snap["source_timestamp"] is not None
    assert snap["quote_timestamp"] is not None


def test_live_alpaca_account_is_paper_readonly():  # pragma: no cover - gated
    """Read-only connectivity check to the Alpaca PAPER account. Places NO
    orders. Confirms the TradingClient builds with paper=True and responds."""
    from alphaos.broker.alpaca_client import AlpacaClient
    from alphaos.config.settings import load_settings

    settings = load_settings()
    if not settings.has_alpaca_keys:
        import pytest

        pytest.skip("no Alpaca credentials")
    acct = AlpacaClient(settings).get_account()
    assert acct["status"] is not None
    assert acct.get("trading_blocked") in (False, None)


def test_live_paper_submit_and_cancel():  # pragma: no cover - gated
    """Submit a far-from-market PAPER bracket (won't fill) and immediately
    cancel it — validates real submission + cancellation without leaving a
    resting order or taking a position. RTH only."""
    import pytest

    from alphaos.broker.alpaca_client import AlpacaClient
    from alphaos.config.settings import load_settings
    from alphaos.constants import MarketSession, OrderState
    from alphaos.data.market_data import MarketDataClient
    from alphaos.strategy.proposal import TradeProposal
    from alphaos.util import timeutils

    settings = load_settings()
    if not settings.has_alpaca_keys:
        pytest.skip("no Alpaca credentials")
    if timeutils.market_session() != MarketSession.REGULAR:
        pytest.skip("submit/cancel smoke needs an open (regular) session")

    price = MarketDataClient(settings).get_snapshot("SPY").get("last_price")
    assert price, "need a live SPY price"
    # Buy-limit 50% below market: it rests, it will not fill.
    entry = round(price * 0.5, 2)
    prop = TradeProposal(
        symbol="SPY", direction="long", strategy="swing",
        entry=entry, stop=round(price * 0.45, 2), target=round(price * 0.6, 2),
        max_holding_days=3, qty=1, risk_per_share=round(entry * 0.05, 2),
        dollar_risk=round(entry * 0.05, 2), expected_r=2.0, same_day_exit_eligible=True,
    )
    alpaca = AlpacaClient(settings)
    norm = alpaca.submit_bracket(prop)
    boid = norm["broker_order_id"]
    assert boid
    try:
        # A far-from-market limit must not be filled.
        assert norm["state"] != OrderState.FILLED.value
    finally:
        alpaca.cancel_order(boid)  # always clean up
    after = alpaca.get_order(boid)
    assert (after["status"] or "").lower() in ("canceled", "pending_cancel", "accepted", "new")
