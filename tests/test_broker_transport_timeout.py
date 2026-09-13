"""Actual installed SDK transport, with HTTP replaced before any request."""

import pytest

from alphaos.broker.alpaca_client import AlpacaClient, AlpacaNotConnected, BROKER_HTTP_TIMEOUT
from conftest import make_settings


def _settings():
    return make_settings(ALPHAOS_MODE="paper", EXECUTION_PROVIDER="alpaca_paper",
                         ALPACA_API_KEY="test-key", ALPACA_SECRET_KEY="test-secret")


def test_sdk_http_calls_receive_connection_and_read_timeouts(monkeypatch):
    pytest.importorskip("alpaca")
    import requests

    calls = []
    def request(_session, method, url, **kwargs):
        calls.append((method, url, kwargs.get("timeout")))
        response = requests.Response()
        response.status_code = 200
        response._content = b"[]"
        return response
    monkeypatch.setattr(requests.Session, "request", request)
    client = AlpacaClient(_settings())
    assert client.list_positions() == []
    assert len(calls) == 1
    assert calls[0][1].startswith("https://paper-api.alpaca.markets/")
    assert calls[0][2] == BROKER_HTTP_TIMEOUT


def test_sdk_timeout_propagates_without_blind_retry(monkeypatch):
    pytest.importorskip("alpaca")
    import requests

    calls = []
    def request(*args, **kwargs):
        calls.append(kwargs.get("timeout"))
        raise requests.Timeout("fake response timeout")
    monkeypatch.setattr(requests.Session, "request", request)
    with pytest.raises(requests.Timeout):
        AlpacaClient(_settings()).list_positions()
    assert calls == [BROKER_HTTP_TIMEOUT]


def test_unrecognized_sdk_transport_never_leaves_an_unbounded_cached_client(monkeypatch):
    pytest.importorskip("alpaca")
    import alpaca.trading.client

    monkeypatch.setattr(alpaca.trading.client, "TradingClient", lambda **kw: object())
    client = AlpacaClient(_settings())
    with pytest.raises(AlpacaNotConnected):
        client._trading_client()
    assert client._client is None
