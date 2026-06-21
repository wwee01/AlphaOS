"""Alpaca/IEX freshness guard: blocks stale/missing quote or bar, reads
Alpaca-shaped timestamps, and labels closed sessions (Change Prompt §1, §9)."""

from __future__ import annotations

from datetime import timedelta

from alphaos.constants import FreshnessStatus, MarketSession, ReasonCode
from alphaos.data.freshness_guard import FreshnessGuard
from alphaos.scanner.candidate_scanner import CandidateScanner
from alphaos.util import timeutils
from conftest import make_settings


def _guard():
    return FreshnessGuard(max_quote_age_rth=60, max_bar_age_rth=180,
                          max_quote_age_premarket=300, max_bar_age_premarket=600,
                          max_price_drift_bps=50)


def _snap(quote_ts, bar_ts, session="regular"):
    return {
        "symbol": "AAPL", "provider": "alpaca", "feed": "iex", "is_mock": False,
        "last_price": 100.0, "dollar_volume": 10_000_000, "spread_pct": 0.001,
        "quote_timestamp": quote_ts, "bar_timestamp": bar_ts,
        "source_timestamp": quote_ts, "received_at": timeutils.now_utc().isoformat(),
        "market_session": session, "change_pct": 0.03, "rel_volume": 2.0,
    }


def _now():
    return timeutils.now_utc().isoformat()


def _old(seconds):
    return (timeutils.now_utc() - timedelta(seconds=seconds)).isoformat()


def test_fresh_quote_and_bar_usable():
    rep = _guard().assess(_snap(_now(), _now()))
    assert rep.is_usable is True
    assert rep.freshness_status == FreshnessStatus.USABLE.value
    # Reads Alpaca-shaped timestamps.
    assert rep.quote_timestamp is not None and rep.bar_timestamp is not None
    assert rep.quote_age_seconds is not None and rep.bar_age_seconds is not None


def test_stale_quote_blocked():
    rep = _guard().assess(_snap(_old(120), _now()))  # quote older than 60s RTH
    assert rep.is_usable is False
    assert rep.block_reason == ReasonCode.STALE_QUOTE.value


def test_missing_quote_blocked():
    rep = _guard().assess(_snap(None, _now()))
    assert rep.is_usable is False
    assert rep.freshness_status == FreshnessStatus.MISSING.value
    assert rep.block_reason == ReasonCode.MISSING_QUOTE.value


def test_stale_bar_blocked():
    rep = _guard().assess(_snap(_now(), _old(600)))  # bar older than 180s RTH
    assert rep.is_usable is False
    assert rep.block_reason == ReasonCode.STALE_BAR.value


def test_missing_bar_blocked():
    rep = _guard().assess(_snap(_now(), None))
    assert rep.is_usable is False
    assert rep.block_reason == ReasonCode.MISSING_BAR.value


def test_closed_session_blocked():
    rep = _guard().assess(_snap(_now(), _now(), session=MarketSession.CLOSED.value))
    assert rep.is_usable is False
    assert rep.freshness_status == FreshnessStatus.CLOSED_SESSION.value
    assert rep.block_reason == ReasonCode.CLOSED_SESSION.value


def test_premarket_uses_lenient_thresholds():
    # 120s quote age is stale in RTH but fresh in premarket (limit 300s).
    rep = _guard().assess(_snap(_old(120), _old(120), session=MarketSession.PREMARKET.value))
    assert rep.is_usable is True


class _StaleMarket:
    def get_snapshot(self, symbol):
        old = (timeutils.now_utc() - timedelta(minutes=30)).isoformat()
        return _snap(old, old)


def test_scanner_blocks_stale_alpaca_data(journal):
    s = make_settings()
    scanner = CandidateScanner(s, journal, market_data=_StaleMarket())
    result = scanner.scan(symbols=["AAPL", "MSFT"])
    assert result.candidates == []
    assert result.blocked_stale == 2
    rejections = journal.query(
        "SELECT * FROM rejected_candidates WHERE reason_code = ?", (ReasonCode.STALE_QUOTE.value,)
    )
    assert len(rejections) == 2
