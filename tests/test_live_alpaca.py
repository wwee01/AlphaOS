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
