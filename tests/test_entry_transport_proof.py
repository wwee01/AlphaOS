"""Real SDK/requests error paths, with socket creation replaced before I/O."""
from concurrent.futures import ThreadPoolExecutor
import errno
import json
import socket
import traceback

import pytest

from alphaos.broker.alpaca_client import AlpacaClient, AlpacaSubmissionNotSent, _bound_broker_transport
from conftest import make_proposal, make_settings


@pytest.fixture
def client(monkeypatch):
    pytest.importorskip("alpaca")
    settings = make_settings(ALPHAOS_MODE="paper", EXECUTION_PROVIDER="alpaca_paper",
                             ALPACA_API_KEY="test-key", ALPACA_SECRET_KEY="test-secret")
    client = AlpacaClient(settings)
    sdk = client._trading_client()
    sdk._session.trust_env = False
    sdk._retry_wait = 0
    # Universal backstop: no test is allowed to perform DNS or open a socket.
    def forbidden_socket(*args, **kwargs):
        raise AssertionError("unmocked socket creation forbidden")
    monkeypatch.setattr("urllib3.util.connection.create_connection", forbidden_socket)
    return client


def fail_connect(monkeypatch, *, timeout=False):
    calls = []
    def refused(*args, **kwargs):
        calls.append(1)
        if timeout:
            raise socket.timeout("SENSITIVE_SENTINEL")
        raise OSError(errno.ECONNREFUSED, "SENSITIVE_SENTINEL")
    monkeypatch.setattr("urllib3.util.connection.create_connection", refused)
    return calls


def response(status, request, body=b'{}'):
    import requests
    result = requests.Response()
    result.status_code, result.request, result.url = status, request, request.url
    result._content = body
    return result


@pytest.mark.parametrize("timeout,reason", [(False, "new_connection_failure"), (True, "connect_timeout")])
def test_first_real_request_preconnect_failure_has_sanitized_proof(client, monkeypatch, timeout, reason):
    calls = fail_connect(monkeypatch, timeout=timeout)
    with pytest.raises(AlpacaSubmissionNotSent) as raised:
        client.submit_bracket(make_proposal())
    assert calls == [1]
    assert raised.value.proof == {"type": "alpaca_entry_transport_not_sent", "version": 1, "reason": reason}
    assert "SENSITIVE_SENTINEL" not in json.dumps(raised.value.proof)
    assert "SENSITIVE_SENTINEL" not in "".join(traceback.format_exception(raised.value))
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


@pytest.mark.parametrize("timeout", [False, True])
def test_connection_failure_after_sdk_429_retry_is_ambiguous(client, monkeypatch, timeout):
    import requests
    from requests.adapters import HTTPAdapter
    calls = fail_connect(monkeypatch, timeout=timeout)
    original_send = HTTPAdapter.send
    outbound = []
    def send(adapter, request, **kwargs):
        outbound.append(request.method)
        if len(outbound) == 1:
            return response(429, request, b'{"code":429,"message":"rate limited"}')
        return original_send(adapter, request, **kwargs)
    monkeypatch.setattr(HTTPAdapter, "send", send)
    with pytest.raises(requests.ConnectionError) as raised:
        client.submit_bracket(make_proposal())
    assert not isinstance(raised.value, AlpacaSubmissionNotSent)
    assert outbound == ["POST", "POST"] and calls == [1]


@pytest.mark.parametrize("failure", ["generic", "read", "protocol", "unstructured_connect_timeout"])
def test_ambiguous_transport_errors_never_gain_no_send_proof(client, monkeypatch, failure):
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.exceptions import ProtocolError
    errors = {
        "generic": requests.ConnectionError("connection refused after an unknown request"),
        "read": requests.ReadTimeout("response lost after accept"),
        "protocol": requests.ConnectionError(ProtocolError("reset after sending bytes")),
        "unstructured_connect_timeout": requests.ConnectTimeout("no structured transport evidence"),
    }
    expected = errors[failure]
    def send(*args, **kwargs):
        raise expected
    monkeypatch.setattr(HTTPAdapter, "send", send)
    with pytest.raises(type(expected)) as raised:
        client.submit_bracket(make_proposal())
    assert raised.value is expected


@pytest.mark.parametrize("custom", ["adapter", "session", "retries", "hook"])
def test_nonstandard_or_retrying_transport_cannot_prove_unsent(client, monkeypatch, custom):
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    calls = fail_connect(monkeypatch)
    sdk = client._trading_client()
    if custom == "adapter":
        class CustomAdapter(HTTPAdapter):
            pass
        sdk._session.mount("https://", CustomAdapter())
    elif custom == "session":
        class CustomSession(requests.Session):
            pass
        sdk._session = CustomSession()
        sdk._session.trust_env = False
        _bound_broker_transport(sdk)
    elif custom == "retries":
        sdk._session.mount("https://", HTTPAdapter(max_retries=Retry(total=1)))
    else:
        sdk._session.hooks["response"].append(lambda value, **kwargs: value)
    with pytest.raises(requests.ConnectionError):
        client.submit_bracket(make_proposal())
    assert len(calls) == (2 if custom == "retries" else 1)


def test_read_only_failure_has_no_entry_proof(client, monkeypatch):
    import requests
    calls = fail_connect(monkeypatch)
    with pytest.raises(requests.ConnectionError):
        client.list_positions()
    assert calls == [1]


def test_prior_request_in_entry_scope_prevents_proof(client, monkeypatch):
    import requests
    from requests.adapters import HTTPAdapter
    calls = fail_connect(monkeypatch)
    original_send = HTTPAdapter.send
    def send(adapter, request, **kwargs):
        if request.method == "GET":
            return response(200, request)
        return original_send(adapter, request, **kwargs)
    monkeypatch.setattr(HTTPAdapter, "send", send)
    original_submit = client._submit
    def submit(sdk, spec):
        assert sdk.get("/clock") == {}
        return original_submit(sdk, spec)
    monkeypatch.setattr(client, "_submit", submit)
    with pytest.raises(requests.ConnectionError):
        client.submit_bracket(make_proposal())
    assert calls == [1]


@pytest.mark.parametrize("status", [500, 503])
def test_server_error_stays_ambiguous(client, monkeypatch, status):
    from alpaca.common.exceptions import APIError
    from requests.adapters import HTTPAdapter
    outbound = []
    def send(adapter, request, **kwargs):
        outbound.append(request)
        return response(status, request, b'{"code":500,"message":"unknown outcome"}')
    monkeypatch.setattr(HTTPAdapter, "send", send)
    with pytest.raises(APIError):
        client.submit_bracket(make_proposal())
    assert len(outbound) == 1


def test_normalization_failure_after_response_remains_ambiguous(client, monkeypatch):
    import requests
    from requests.adapters import HTTPAdapter
    outbound = []
    def send(adapter, request, **kwargs):
        outbound.append(request)
        return response(200, request)
    def normalize(order):
        raise requests.ConnectionError("post-response local processing failed")
    client._trading_client()._use_raw_data = True
    monkeypatch.setattr(HTTPAdapter, "send", send)
    monkeypatch.setattr("alphaos.broker.order_mapping.normalize_order", normalize)
    with pytest.raises(requests.ConnectionError):
        client.submit_bracket(make_proposal())
    assert len(outbound) == 1


def test_scope_resets_between_entries_and_isolated_between_threads(client, monkeypatch):
    calls = fail_connect(monkeypatch)
    def attempt(_):
        try:
            client.submit_bracket(make_proposal())
        except AlpacaSubmissionNotSent as exc:
            return exc.proof
        raise AssertionError("expected narrow no-dispatch proof")
    assert attempt(0)["version"] == 1 and attempt(1)["version"] == 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [2, 3]))
    assert len(calls) == 4 and all(r["reason"] == "new_connection_failure" for r in results)


def test_fake_generic_refusal_does_not_gain_transport_proof():
    from test_alpaca_paper_execution import FakeTradingClient, _paper_om
    class RefusedFake(FakeTradingClient):
        def submit(self, spec):
            raise ConnectionRefusedError("fake failure at an unspecified stage")
    _, journal, manager = _paper_om(RefusedFake())
    result = manager.execute_proposal(make_proposal())
    assert result.submission_uncertain and result.state == "submitted"
    journal.close()
