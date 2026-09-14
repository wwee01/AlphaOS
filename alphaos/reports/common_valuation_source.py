"""Bounded GET-only evidence adapter; explicitly invoked, never scheduled.

Use the existing paper account and configured market-data feed. No fallback
feed, credentials in evidence, account mutation or unbounded pagination.
"""
from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta

from alphaos.config.settings import ALPACA_DATA_BASE_URL, PAPER_BASE_URL
from alphaos.reports.common_valuation import _dt
from alphaos.util import timeutils


class CommonValuationSource:
    MAX_PAGES = 10
    PAGE_SIZE = 100
    MAX_CALLS = 27  # calendar, four account/position reads, two activity traversals, two bar pages

    def __init__(self, settings, *, transport=None, clock=None):
        if (settings.is_mock or settings.offline_mode or not settings.has_alpaca_keys
                or settings.alpaca_base_url.rstrip("/") != PAPER_BASE_URL
                or settings.market_data_feed not in {"iex", "sip"}):
            raise ValueError("manual common valuation requires the configured paper account/data feed")
        self.settings = settings
        self.transport = transport or self._http_get
        self.clock = clock or timeutils.now_utc
        self.calls = 0

    def now(self):
        return _dt(self.clock())

    def _http_get(self, base, path, params):
        url = base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        request = urllib.request.Request(url, method="GET", headers={
            "APCA-API-KEY-ID": self.settings.alpaca_api_key,
            "APCA-API-SECRET-KEY": self.settings.alpaca_secret_key,
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.load(response)
        except (urllib.error.URLError, ValueError) as exc:
            # Do not persist exception URLs, response bodies or credentials.
            raise ValueError(f"valuation GET unavailable ({type(exc).__name__})") from None

    def _get(self, path, params=None, *, data=False):
        if path not in {"/v2/account", "/v2/positions", "/v2/calendar", "/v2/account/activities", "/v2/stocks/bars"}:
            raise ValueError("endpoint is outside the read-only valuation allowlist")
        self.calls += 1
        if self.calls > self.MAX_CALLS:
            raise ValueError("manual valuation request cap reached")
        start = self.now()
        value = self.transport(ALPACA_DATA_BASE_URL if data else PAPER_BASE_URL, path, params or {})
        end = self.now()
        return {"requested_at_utc": start.isoformat(), "received_at_utc": end.isoformat(), "value": value}

    def calendar(self, start, end):
        value = self._get("/v2/calendar", {"start": start.isoformat(), "end": end.isoformat()})["value"]
        if not isinstance(value, list):
            raise ValueError("calendar response is unavailable")
        return [{key: row[key] for key in ("date", "open", "close")} for row in value]

    def account(self):
        part = self._get("/v2/account")
        raw = part["value"]
        if not raw.get("id"):
            raise ValueError("account identity is missing")
        part["value"] = {"account_fingerprint": hashlib.sha256(str(raw["id"]).encode()).hexdigest(),
                         **{key: raw.get(key) for key in ("cash", "currency", "equity")}}
        return part

    def positions(self):
        part = self._get("/v2/positions")
        if not isinstance(part["value"], list):
            raise ValueError("position response is unavailable")
        part["value"] = [{key: row.get(key) for key in ("symbol", "qty", "side", "asset_class")}
                         for row in part["value"]]
        return part

    def activities(self, start):
        until = self.now()
        request = {"after": _dt(start).isoformat(), "until": until.isoformat(),
                   "direction": "asc", "page_size": self.PAGE_SIZE}
        params = dict(request)
        rows, seen, pages = [], set(), []
        for _ in range(self.MAX_PAGES):
            part = self._get("/v2/account/activities", params)
            page = part.pop("value")
            if not isinstance(page, list) or len(page) > self.PAGE_SIZE:
                raise ValueError("invalid account activity page")
            for row in page:
                if not isinstance(row, dict) or not row.get("id") or row["id"] in seen:
                    raise ValueError("missing or repeated account activity ID")
                seen.add(row["id"])
            pages.append({**part, "request": dict(params), "row_count": len(page)})
            rows.extend(page)
            if len(page) < self.PAGE_SIZE:
                return {"requested_at_utc": pages[0]["requested_at_utc"], "received_at_utc": pages[-1]["received_at_utc"],
                        "request": request, "pages": pages, "rows": rows, "pagination_exhausted": True}
            params["page_token"] = page[-1]["id"]
        raise ValueError("account activity pagination cap reached; coverage unknown")

    def final_minute_bars(self, symbols, close_at):
        close_at = _dt(close_at)
        request = {"symbols": sorted(symbols), "timeframe": "1Min", "feed": self.settings.market_data_feed,
                   "adjustment": "raw", "start": (close_at - timedelta(minutes=1)).isoformat(),
                   "end": (close_at - timedelta(seconds=1)).isoformat(), "limit": 1000}
        params = {**request, "symbols": ",".join(request["symbols"])}
        rows: dict[str, list] = {}
        pages, tokens = [], set()
        for _ in range(2):
            part = self._get("/v2/stocks/bars", params, data=True)
            payload = part.pop("value")
            if not isinstance(payload, dict) or not isinstance(payload.get("bars"), dict):
                raise ValueError("common bar response is unavailable")
            for symbol, values in payload["bars"].items():
                if not isinstance(values, list):
                    raise ValueError("common bar response is malformed")
                rows.setdefault(symbol, []).extend(values)
            pages.append({**part, "request": dict(params), "next_page_token": payload.get("next_page_token")})
            token = payload.get("next_page_token")
            if not token:
                return {"requested_at_utc": pages[0]["requested_at_utc"], "received_at_utc": pages[-1]["received_at_utc"],
                        "request": request, "pages": pages, "rows": rows, "pagination_exhausted": True}
            if token in tokens:
                raise ValueError("common bar pagination repeated a token")
            tokens.add(token)
            params["page_token"] = token
        raise ValueError("common bar pagination cap reached; coverage unknown")
