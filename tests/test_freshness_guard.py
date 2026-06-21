"""Freshness guard blocks stale / unverifiable data (test #3)."""

from __future__ import annotations

from datetime import timedelta

from alphaos.constants import FreshnessStatus, ReasonCode
from alphaos.data.freshness_guard import FreshnessGuard
from alphaos.scanner.candidate_scanner import CandidateScanner
from alphaos.util import timeutils
from conftest import make_settings


def _snap(source_ts):
    now = timeutils.now_utc()
    return {
        "symbol": "AAPL", "provider": "massive", "last_price": 100.0,
        "dollar_volume": 10_000_000, "spread_pct": 0.001,
        "source_timestamp": source_ts, "received_at": now.isoformat(),
        "change_pct": 0.03, "rel_volume": 2.0,
    }


def test_fresh_data_is_usable():
    g = FreshnessGuard(max_data_age_seconds=120)
    rep = g.assess(_snap(timeutils.now_utc().isoformat()))
    assert rep.is_usable is True
    assert rep.freshness_status == FreshnessStatus.USABLE.value


def test_stale_data_blocked():
    g = FreshnessGuard(max_data_age_seconds=120)
    old = (timeutils.now_utc() - timedelta(minutes=10)).isoformat()
    rep = g.assess(_snap(old))
    assert rep.is_usable is False
    assert rep.freshness_status == FreshnessStatus.STALE.value
    assert rep.block_reason == ReasonCode.STALE_DATA.value


def test_unverifiable_data_blocked():
    g = FreshnessGuard(max_data_age_seconds=120)
    rep = g.assess(_snap(None))
    assert rep.is_usable is False
    assert rep.freshness_status == FreshnessStatus.UNVERIFIABLE.value
    assert rep.block_reason == ReasonCode.UNVERIFIABLE_DATA.value


class _StaleMarket:
    """Market client that always returns stale data."""

    def get_snapshot(self, symbol):
        old = (timeutils.now_utc() - timedelta(minutes=30)).isoformat()
        return {
            "symbol": symbol, "provider": "massive", "last_price": 100.0,
            "dollar_volume": 10_000_000, "spread_pct": 0.001,
            "source_timestamp": old, "received_at": timeutils.now_utc().isoformat(),
            "change_pct": 0.05, "rel_volume": 3.0,
        }


def test_scanner_blocks_stale_and_records_rejection(journal):
    s = make_settings()
    scanner = CandidateScanner(s, journal, market_data=_StaleMarket())
    result = scanner.scan(symbols=["AAPL", "MSFT"])
    assert result.candidates == []
    assert result.blocked_stale == 2
    rejections = journal.query("SELECT * FROM rejected_candidates WHERE reason_code = ?", (ReasonCode.STALE_DATA.value,))
    assert len(rejections) == 2
