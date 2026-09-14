"""Transport-clock regressions: receipt timing never refreshes source data."""
from datetime import datetime, timedelta, timezone
import json
import urllib.error

import pytest

from alphaos.data.freshness_guard import FreshnessGuard
from alphaos.data.intraday_volume_curve import compute_rel_volume_v2
from alphaos.data.providers.alpaca_data import AlpacaDataProvider
from alphaos.util import timeutils
from conftest import make_settings


@pytest.fixture
def transport(monkeypatch):
    clock = [datetime(2026, 9, 14, 14, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(timeutils, "now_utc", lambda: clock[0])
    provider = AlpacaDataProvider(make_settings(ALPACA_API_KEY="fake", ALPACA_SECRET_KEY="fake"))
    calls = []

    def install(*, fail_second=False, source_lag=1, seconds=4):
        def urlopen(request, timeout):
            calls.append(request.full_url)
            if fail_second and len(calls) == 2:
                clock[0] += timedelta(seconds=seconds)
                raise urllib.error.URLError("offline transport failure")
            symbols = request.full_url.split("symbols=")[1].split("&")[0].split(",") if "symbols=" in request.full_url else ["ONE"]

            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def read(self):
                    clock[0] += timedelta(seconds=seconds)
                    payload = {
                        "latestQuote": {"bp": 99, "ap": 101, "t": (clock[0] - timedelta(seconds=source_lag)).isoformat()},
                        "latestTrade": {"p": 100},
                        "minuteBar": {"t": (clock[0] - timedelta(seconds=90)).isoformat()},
                        "dailyBar": {"v": 300_000}, "prevDailyBar": {"v": 1_000_000},
                    }
                    return json.dumps({s: payload for s in symbols} if "symbols=" in request.full_url else payload).encode()
            return Response()
        monkeypatch.setattr("alphaos.data.providers.alpaca_data.urllib.request.urlopen", urlopen)
    return provider, clock, calls, install


def test_single_receipt_after_body_not_before_request(transport):
    provider, clock, calls, install = transport
    install()
    row = provider.get_snapshot("ONE")
    assert row["received_at"] == clock[0].isoformat()
    report = FreshnessGuard().assess(row)
    assert report.data_delay_seconds == 1
    assert report.quote_timestamp == row["quote_timestamp"]
    assert report.is_usable
    assert len(calls) == 1


def test_batch_receipt_per_response_without_extra_requests(transport):
    provider, clock, calls, install = transport
    install()
    symbols = [f"S{i}" for i in range(101)]
    rows = provider.get_snapshots(symbols)
    assert [r["symbol"] for r in rows] == symbols
    assert rows[0]["received_at"] == (clock[0] - timedelta(seconds=4)).isoformat()
    assert rows[99]["received_at"] == rows[0]["received_at"]
    assert rows[100]["received_at"] == clock[0].isoformat()
    assert {FreshnessGuard().assess(r).data_delay_seconds for r in rows} == {1}
    assert len(calls) == 2


def test_failed_later_chunk_keeps_prior_receipt_and_missing_data(transport):
    provider, clock, calls, install = transport
    install(fail_second=True)
    rows = provider.get_snapshots([f"S{i}" for i in range(101)])
    assert rows[0]["received_at"] == (clock[0] - timedelta(seconds=4)).isoformat()
    assert rows[-1]["received_at"] == clock[0].isoformat()
    assert rows[-1]["quote_timestamp"] is None
    assert not FreshnessGuard().assess(rows[-1]).is_usable
    assert len(calls) == 2


@pytest.mark.parametrize(("lag", "reason"), [(95, "STALE_QUOTE"), (-10, "UNVERIFIABLE_DATA")])
def test_receipt_cannot_make_bad_source_timestamp_fresh(transport, lag, reason):
    provider, clock, _, install = transport
    install(source_lag=lag)
    row = provider.get_snapshot("ONE")
    assert row["received_at"] == clock[0].isoformat()
    assert row["quote_timestamp"] == (clock[0] - timedelta(seconds=lag)).isoformat()
    report = FreshnessGuard().assess(row)
    assert not report.is_usable and report.block_reason == reason


def test_volume_curve_uses_actual_receipt_at_session_boundary(transport):
    provider, clock, _, install = transport
    clock[0] = datetime(2026, 9, 14, 13, 59, 50, tzinfo=timezone.utc)
    install(seconds=20)
    row = provider.get_snapshot("ONE")
    assert row["rel_volume"] == round(compute_rel_volume_v2(300_000, 1_000_000, clock[0]), 3)
    assert row["received_at"] == "2026-09-14T14:00:10+00:00"
