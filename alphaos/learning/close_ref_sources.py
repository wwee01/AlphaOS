"""Injected, read-only data capabilities for close_ref_v1.

No ledger writes, legacy cache reads, price substitution or provider fallback.
Caches last only for this worker invocation and retain their original fetch
times. The stored outcome window must be reused on later invocations.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections import OrderedDict
from dataclasses import asdict
from datetime import date, timedelta

from alphaos.learning import close_ref_engine as engine
from alphaos.util import timeutils

_MAX_SPY_RESPONSES = 32


class SliceYielded(RuntimeError):
    """No new data request was admitted after the cooperative slice ended."""


class CloseRefSources:
    def __init__(self, calendar_client, bars_provider, *, provider: str, feed: str,
                 adjustment: str = "split", now=None, should_yield=None):
        if not provider or not feed or adjustment != "split":
            raise ValueError("close_ref source requires a named feed and split adjustment")
        self.calendar_client = calendar_client
        self.bars_provider = bars_provider
        self.provider, self.feed, self.adjustment = provider, feed, adjustment
        self.now = now or timeutils.now_utc
        self.should_yield = should_yield or (lambda: False)
        self._spy: OrderedDict[tuple, tuple[list[dict], dict]] = OrderedDict()

    def _admit(self):
        if self.should_yield():
            raise SliceYielded("close_ref_slice_yielded")
        self._check_feed()

    def _check_feed(self):
        if self.adjustment != "split":
            raise ValueError("close_ref_adjustment_changed")
        settings = getattr(self.bars_provider, "settings", None)
        if settings is not None and settings.market_data_feed != self.feed:
            raise ValueError("close_ref_provider_feed_changed")

    def window(self, decision_at):
        # The approved bounded table, never broker calendar hours or a nearby bar.
        self._admit()
        return engine.resolve_window(decision_at)

    def observe(self, window: engine.Window, horizon: int, symbol: str) -> dict:
        self._admit()
        endpoint = window.endpoint(horizon)
        now = self.now()
        if now < endpoint.close or now > engine.retry_until(endpoint):
            leg = engine.observe_leg(window, horizon, symbol, [], provider=self.provider,
                                     feed=self.feed, adjustment=self.adjustment,
                                     requested_at=now, fetched_at=now, now=now)
            return {"leg": asdict(leg), "request": None}
        start = window.reference.date
        # Existing REST provider sends end as midnight. Include the entire
        # endpoint date, then the pure engine selects only the two exact dates.
        end = (date.fromisoformat(endpoint.date) + timedelta(days=1)).isoformat()
        key = (self.provider, self.feed, self.adjustment, symbol, start, end)
        cached = self._spy.get(key) if symbol == "SPY" else None
        if cached is None:
            self._admit()
            requested = self.now()
            # Recheck after admission/clock work, before any provider call.
            if requested < endpoint.close or requested > engine.retry_until(endpoint):
                return {"leg": asdict(engine.observe_leg(
                    window, horizon, symbol, [], provider=self.provider, feed=self.feed,
                    adjustment=self.adjustment, requested_at=requested,
                    fetched_at=requested, now=requested)), "request": None}
            self._admit()
            error_type = None
            try:
                bars = self.bars_provider.get_daily_bars(
                    symbol, start=start, end=end, limit=64, adjustment=self.adjustment,
                )
                if not isinstance(bars, list) or len(bars) > 64:
                    raise ValueError("invalid daily bar response")
                encoded = json.dumps(bars, sort_keys=True, separators=(",", ":"), allow_nan=False)
                bars = json.loads(encoded)  # own the bounded provider response
            except Exception as exc:
                bars, encoded, error_type = [], "[]", type(exc).__name__
            fetched = self.now()
            self._check_feed()
            request = {"provider": self.provider, "feed": self.feed, "adjustment": self.adjustment,
                       "symbol": symbol, "timeframe": "1Day", "start": start, "end": end,
                       "limit": 64, "requested_at_utc": requested.isoformat(),
                       "fetched_at_utc": fetched.isoformat(), "returned_rows": len(bars),
                       "mapped_response_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
                       "error_type": error_type}
            if symbol == "SPY":
                self._spy[key] = (bars, request)
                while len(self._spy) > _MAX_SPY_RESPONSES:
                    self._spy.popitem(last=False)
        else:
            bars, request = cached
            requested = timeutils.parse_iso(request["requested_at_utc"])
            fetched = timeutils.parse_iso(request["fetched_at_utc"])
        result = engine.observe_leg(window, horizon, symbol, bars, provider=self.provider,
                                    feed=self.feed, adjustment=self.adjustment,
                                    requested_at=requested, fetched_at=fetched, now=self.now())
        return {"leg": asdict(result), "request": copy.deepcopy(request)}


def make_sources(orch, bars_provider, *, should_yield=None):
    """Runtime adapter; mock/offline callers must inject their test capabilities."""
    from alphaos.data.providers.alpaca_bars import AlpacaBarsProvider

    if (orch.settings.is_mock or orch.settings.offline_mode or
            not isinstance(bars_provider, AlpacaBarsProvider)):
        return None
    return CloseRefSources(None, bars_provider,
                           provider="alpaca", feed=orch.settings.market_data_feed,
                           adjustment="split", should_yield=should_yield)
