"""Bounded GET-only paper transport. This does NOT restrict the key at Alpaca.

No trading client is constructed. No redirects, proxies, or arbitrary paths
are allowed. An independently supplied account pin is checked before any
orders/positions are read. Errors contain types/status codes, never responses.
"""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
import math
import time
from types import SimpleNamespace
from urllib.parse import quote

from alphaos.broker.order_mapping import normalize_order
from alphaos.config.settings import PAPER_BASE_URL
from alphaos.util import timeutils


class ObservationRefused(ValueError):
    pass


class BrokerReadError(RuntimeError):
    pass


def paper_guard(settings) -> None:
    if (getattr(settings.mode, "value", settings.mode) != "paper"
            or settings.execution_provider != "alpaca_paper"
            or settings.real_trading_enabled_raw != "false"
            or settings.alpaca_paper is not True
            or settings.alpaca_base_url.rstrip("/") != PAPER_BASE_URL
            or not settings.has_alpaca_keys):
        raise ObservationRefused("paper_configuration_required")


def _object(data):
    return SimpleNamespace(**{k: [_object(v) for v in value] if k == "legs" and value
                              else value for k, value in data.items()})


def order(data):
    result = normalize_order(_object(data))
    result["updated_at"] = data.get("updated_at")
    return result


class PaperReads:
    def __init__(self, settings, expected_account_id: str, session=None, max_requests=50):
        paper_guard(settings)
        if not expected_account_id or not expected_account_id.strip():
            raise ObservationRefused("independent_account_pin_required")
        self.settings = settings
        self.expected_account_id = expected_account_id
        self.verified = False
        self.receipts: list[dict] = []
        if type(max_requests) is not int or not 1 <= max_requests <= 50:
            raise ObservationRefused("invalid_read_budget")
        self.max_requests = max_requests
        self.requests_sent = 0
        self.deadline = time.monotonic() + 60
        if session is None:
            import requests

            session = requests.Session()
            session.trust_env = False
        self.session = session

    def close(self):
        self.session.close()

    def _get(self, path, params=None, *, allow_missing=False):
        paper_guard(self.settings)  # recheck even if settings changed after construction
        if not self.verified and path != "/v2/account":
            raise ObservationRefused("account_not_verified")
        fixed = {"/v2/account", "/v2/positions", "/v2/orders", "/v2/calendar",
                 "/v2/orders:by_client_order_id"}
        if path not in fixed and not path.startswith("/v2/orders/"):
            raise ObservationRefused("read_endpoint_not_allowed")
        started = timeutils.to_iso(timeutils.now_utc())
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BrokerReadError("poll_deadline_exceeded")
        if self.requests_sent >= self.max_requests:
            raise BrokerReadError("poll_request_budget_exceeded")
        self.requests_sent += 1
        try:
            response = self.session.get(
                PAPER_BASE_URL + path, params=params,
                headers={"APCA-API-KEY-ID": self.settings.alpaca_api_key,
                         "APCA-API-SECRET-KEY": self.settings.alpaca_secret_key},
                timeout=(min(5, remaining), min(10, remaining)), allow_redirects=False, stream=True,
            )
            try:
                body = bytearray()
                for chunk in response.iter_content(chunk_size=65536):
                    if time.monotonic() > self.deadline or len(body) + len(chunk) > 8_000_000:
                        raise BrokerReadError("response_budget_exceeded")
                    body.extend(chunk)
            finally:
                response.close()
        except Exception as exc:
            if isinstance(exc, BrokerReadError):
                raise
            raise BrokerReadError(f"broker_read_{type(exc).__name__}") from None
        received = timeutils.to_iso(timeutils.now_utc())
        self.receipts.append({"path": path, "params": params, "request_at_utc": started,
                              "received_at_utc": received, "http_status": response.status_code,
                              "body_sha256": sha256(body).hexdigest(),
                              "server_date": response.headers.get("Date")})
        if allow_missing and response.status_code == 404:
            return None
        if response.status_code != 200:
            raise BrokerReadError(f"broker_http_{response.status_code}")
        try:
            return json.loads(body)
        except Exception:
            raise BrokerReadError("broker_invalid_json") from None

    def account(self):
        self.verified = False
        value = self._get("/v2/account")
        if not isinstance(value, dict) or value.get("id") != self.expected_account_id:
            raise ObservationRefused("wrong_account")
        if value.get("status") != "ACTIVE":
            raise ObservationRefused("account_not_active")
        self.verified = True
        return {"account_fingerprint": sha256(value["id"].encode()).hexdigest()[:16],
                "status": value["status"], "trading_blocked": value.get("trading_blocked"),
                "credential_capabilities": "server_enforced_read_only_unverified",
                "transport": "GET_only; existing Trading_API_key"}

    def positions(self):
        value = self._get("/v2/positions")
        if not isinstance(value, list):
            raise BrokerReadError("positions_invalid_shape")
        symbols = set()
        for position in value:
            try:
                symbol = position["symbol"]
                qty = float(position["qty"])
                valid = (isinstance(symbol, str) and bool(symbol) and symbol not in symbols
                         and math.isfinite(qty) and position["side"] in {"long", "short"})
            except (KeyError, TypeError, ValueError):
                valid = False
            if not valid:
                raise BrokerReadError("positions_invalid_shape")
            symbols.add(symbol)
        return value

    def open_orders(self):
        value = self._get("/v2/orders", {"status": "open", "nested": "true", "limit": 500})
        if not isinstance(value, list) or len(value) >= 500:
            raise BrokerReadError("open_orders_incomplete")
        return [order(v) for v in value]

    def exact_order(self, broker_id=None, client_id=None):
        if broker_id:
            value = self._get("/v2/orders/" + quote(broker_id, safe=""),
                              {"nested": "true"}, allow_missing=True)
        elif client_id:
            value = self._get("/v2/orders:by_client_order_id",
                              {"client_order_id": client_id}, allow_missing=True)
        else:
            raise ObservationRefused("order_identifier_required")
        return order(value) if value is not None else None

    def session_hours(self, now: datetime):
        market_date = timeutils.to_et(now).date().isoformat()
        value = self._get("/v2/calendar", {"start": market_date, "end": market_date})
        if not isinstance(value, list) or len(value) > 1:
            raise BrokerReadError("calendar_invalid_shape")
        if not value:
            return {"date": market_date, "open": None, "close": None, "source": "broker_calendar"}
        day = value[0]
        if day.get("date") != market_date:
            raise BrokerReadError("calendar_wrong_date")
        # Alpaca's YYYY-MM-DD + HH:MM contract is checked before use.
        for key in ("open", "close"):
            datetime.strptime(day[key], "%H:%M")
        return {"date": market_date, "open": day["open"], "close": day["close"],
                "source": "broker_calendar"}
