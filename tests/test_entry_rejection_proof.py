"""Actual alpaca-py HTTP error conversion; every transport is intercepted."""
from copy import deepcopy
from hashlib import sha256
import json
import traceback

import pytest

from alphaos.broker.alpaca_client import AlpacaClient, AlpacaSubmissionRejected, _bound_broker_transport
from conftest import make_proposal, make_settings


@pytest.fixture
def client(monkeypatch):
    pytest.importorskip("alpaca")
    client = AlpacaClient(make_settings(ALPHAOS_MODE="paper", EXECUTION_PROVIDER="alpaca_paper",
                                       ALPACA_API_KEY="PRIVATE_API_KEY", ALPACA_SECRET_KEY="PRIVATE_API_SECRET"))
    sdk = client._trading_client()
    sdk._session.trust_env, sdk._retry_wait = False, 0
    def no_socket(*args, **kwargs):
        raise AssertionError("unmocked socket creation forbidden")
    monkeypatch.setattr("urllib3.util.connection.create_connection", no_socket)
    return client


def response(status, request, body):
    import requests
    result = requests.Response()
    result.status_code, result.request, result.url = status, request, request.url
    result._content = body if isinstance(body, bytes) else json.dumps(body).encode()
    result.headers["Content-Type"] = "application/json; charset=utf-8"
    return result


def intercept(monkeypatch, *, status=403, body=None, change=None):
    from requests.adapters import HTTPAdapter
    calls = []
    body = {"code": 40310000, "message": "potential wash trade detected"} if body is None else body
    def send(adapter, request, **kwargs):
        calls.append(request)
        result = response(status, request, body)
        if change:
            change(result)
        return result
    monkeypatch.setattr(HTTPAdapter, "send", send)
    return calls


@pytest.mark.parametrize("status,code,message,classification", [
    (400, 40010000, "request body format is invalid", "invalid_request"),
    (403, 40310000, "potential wash trade detected", "wash_trade"),
    (403, 40310000, "potential wash trade detected. use complex orders", "wash_trade"),
    (403, 40310000, "insufficient buying power", "insufficient_buying_power"),
    (403, 40310000, "insufficient buying power (required: 102.50, available: 0)", "insufficient_buying_power"),
    (422, 42210000, "invalid input", "invalid_order_terms"),
    (422, 42210000, "invalid limit price", "invalid_order_terms"),
])
def test_actual_first_sdk_rejection_has_request_bound_sanitized_proof(client, monkeypatch, status, code, message, classification):
    body = {"code": code, "message": message, "diagnostic": "PRIVATE_RESPONSE_DETAIL"}
    calls = intercept(monkeypatch, status=status, body=body)
    proposal = make_proposal()
    with pytest.raises(AlpacaSubmissionRejected) as raised:
        client.submit_bracket(proposal)
    proof = raised.value.proof
    assert len(calls) == 1
    assert proof == {"type": "alpaca_entry_broker_rejected", "version": 1, "http_status": status,
                     "error_code": code, "classification": classification,
                     "response_body_sha256": sha256(json.dumps(body).encode()).hexdigest(),
                     "client_order_id": proposal.proposal_id, "symbol": proposal.symbol,
                     "request": json.loads(calls[0].body)}
    visible = json.dumps(proof) + "".join(traceback.format_exception(raised.value))
    assert all(secret not in visible for secret in ["PRIVATE_API_KEY", "PRIVATE_API_SECRET", "PRIVATE_RESPONSE_DETAIL"])
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


@pytest.mark.parametrize("body", [
    {"code": 42210000, "message": "client_order_id must be unique"},
    {"code": 42210000, "message": "duplicate client order id"},
    {"code": 42210000, "message": "order already exists"},
    {"code": 42210000, "message": "new unreviewed rejection reason"},
    {"code": 42210000, "message": "invalid input; prior order might exist"},
    {"code": 42210000}, {"code": "42210000", "message": "invalid input"},
    {"code": True, "message": "invalid input"}, {"code": 42210000, "message": None},
    {"code": 40310000, "message": "invalid input"}, [], b"not JSON",
    b'{"code":42210000,"message":"client_order_id must be unique","message":"invalid input"}',
    b'{"code":42210000,"message":"invalid input","detail":NaN}',
])
def test_duplicate_unknown_or_malformed_body_stays_original_api_error(client, monkeypatch, body):
    from alpaca.common.exceptions import APIError
    calls = intercept(monkeypatch, status=422, body=body)
    with pytest.raises(APIError):
        client.submit_bracket(make_proposal())
    assert len(calls) == 1


@pytest.mark.parametrize("status", [401, 404, 409, 429, 500, 503])
def test_unapproved_status_never_proves_nonacceptance(client, monkeypatch, status):
    from alpaca.common.exceptions import APIError
    client._trading_client()._retry = 0
    calls = intercept(monkeypatch, status=status)
    with pytest.raises(APIError):
        client.submit_bracket(make_proposal())
    assert len(calls) == 1


@pytest.mark.parametrize("field", ["client_order_id", "symbol", "side", "qty", "type", "order_class",
                                   "time_in_force", "limit_price", "take_profit", "stop_loss"])
def test_prepared_request_must_match_every_intended_term(client, monkeypatch, field):
    from alpaca.common.exceptions import APIError
    def mutate(result):
        payload = json.loads(result.request.body)
        payload[field] = "wrong"
        result.request.body = json.dumps(payload).encode()
    intercept(monkeypatch, change=mutate)
    with pytest.raises(APIError):
        client.submit_bracket(make_proposal())


def test_request_kwargs_cannot_change_original_intent(client, monkeypatch):
    from alpaca.common.exceptions import APIError
    intercept(monkeypatch)
    original = client._submit
    def submit(sdk, spec):
        spec["entry"] -= 1
        return original(sdk, spec)
    monkeypatch.setattr(client, "_submit", submit)
    with pytest.raises(APIError):
        client.submit_bracket(make_proposal())


@pytest.mark.parametrize("cause", ["method", "url", "response_url", "redirect", "content_type", "tls"])
def test_untrusted_response_path_never_proves_rejection(client, monkeypatch, cause):
    from alpaca.common.exceptions import APIError
    def mutate(result):
        if cause == "method":
            result.request.method = "GET"
        elif cause == "url":
            result.request.url = "https://other.invalid/v2/orders"
        elif cause == "response_url":
            result.url = "https://other.invalid/v2/orders"
        elif cause == "redirect":
            result.history = [deepcopy(result)]
        elif cause == "content_type":
            result.headers["Content-Type"] = "text/plain"
    if cause == "tls":
        client._trading_client()._session.verify = False
    intercept(monkeypatch, change=mutate)
    with pytest.raises(APIError):
        client.submit_bracket(make_proposal())


@pytest.mark.parametrize("custom", ["session", "adapter", "hook", "auth", "retries"])
def test_nonstandard_transport_stays_ambiguous(client, monkeypatch, custom):
    import requests
    from alpaca.common.exceptions import APIError
    from requests.adapters import HTTPAdapter
    sdk = client._trading_client()
    if custom == "session":
        class CustomSession(requests.Session):
            pass
        sdk._session = CustomSession()
        sdk._session.trust_env = False
        _bound_broker_transport(sdk)
    elif custom == "adapter":
        class CustomAdapter(HTTPAdapter):
            pass
        sdk._session.mount("https://", CustomAdapter())
    elif custom == "hook":
        sdk._session.hooks["response"].append(lambda value, **kwargs: value)
    elif custom == "auth":
        sdk._session.auth = ("other", "credentials")
    else:
        sdk._session.mount("https://", HTTPAdapter(max_retries=1))
    intercept(monkeypatch)
    with pytest.raises(APIError):
        client.submit_bracket(make_proposal())


@pytest.mark.parametrize("prior", ["sdk_retry", "read"])
def test_prior_attempt_invalidates_later_rejection(client, monkeypatch, prior):
    from alpaca.common.exceptions import APIError
    from requests.adapters import HTTPAdapter
    calls = []
    def send(adapter, request, **kwargs):
        calls.append(request.method)
        if len(calls) == 1:
            return response(429 if prior == "sdk_retry" else 200, request, {})
        return response(403, request, {"code": 40310000, "message": "potential wash trade detected"})
    monkeypatch.setattr(HTTPAdapter, "send", send)
    if prior == "read":
        original = client._submit
        def submit(sdk, spec):
            sdk.get("/clock")
            return original(sdk, spec)
        monkeypatch.setattr(client, "_submit", submit)
    with pytest.raises(APIError):
        client.submit_bracket(make_proposal())
    assert len(calls) == 2


@pytest.mark.parametrize("fabrication", ["bare", "http_response", "different_captured_response", "read_timeout"])
def test_caller_exception_cannot_fabricate_transport_rejection(client, monkeypatch, fabrication):
    import requests
    from alpaca.common.exceptions import APIError
    body = {"code": 40310000, "message": "potential wash trade detected"}
    intercept(monkeypatch, body=body)
    original = client._submit
    def submit(sdk, spec):
        if fabrication == "read_timeout":
            raise requests.ReadTimeout("unknown outcome")
        if fabrication == "different_captured_response":
            try:
                return original(sdk, spec)
            except APIError as exc:
                exc._http_error.response = deepcopy(exc.response)
                raise
        error = None
        if fabrication == "http_response":
            prepared = requests.Request("POST", "https://paper-api.alpaca.markets/v2/orders").prepare()
            result = response(403, prepared, body)
            error = requests.HTTPError(response=result, request=prepared)
        raise APIError(json.dumps(body), error)
    monkeypatch.setattr(client, "_submit", submit)
    with pytest.raises(requests.ReadTimeout if fabrication == "read_timeout" else APIError):
        client.submit_bracket(make_proposal())


def test_get_rejection_and_subsequent_fabricated_exception_have_no_stale_proof(client, monkeypatch):
    from alpaca.common.exceptions import APIError
    intercept(monkeypatch)
    with pytest.raises(APIError):
        client.list_positions()
    with pytest.raises(AlpacaSubmissionRejected):
        client.submit_bracket(make_proposal())
    def fabricated(*args):
        raise APIError('{"code":40310000,"message":"potential wash trade detected"}')
    monkeypatch.setattr(client, "_submit", fabricated)
    with pytest.raises(APIError):
        client.submit_bracket(make_proposal())
