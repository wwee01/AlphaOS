"""Crossed/zero-quote hardening: a malformed IEX quote (ask<=0 or ask<bid ->
negative spread) must not slip the spread gate (live-validation finding)."""

from __future__ import annotations

from alphaos.constants import ReasonCode
from alphaos.data.freshness_guard import quote_crossed_or_invalid
from alphaos.risk.risk_engine import RiskEngine
from alphaos.scanner.candidate_scanner import CandidateScanner
from alphaos.util import timeutils
from conftest import make_settings


def test_quote_helper_flags_crossed_and_zero():
    assert quote_crossed_or_invalid({"bid": 10.0, "ask": 0.0}) is True       # zero ask
    assert quote_crossed_or_invalid({"bid": 10.0, "ask": 9.0}) is True        # crossed
    assert quote_crossed_or_invalid({"bid": 0.0, "ask": 5.0}) is True         # zero bid
    assert quote_crossed_or_invalid({"bid": 10.0, "ask": 10.5}) is False      # normal
    assert quote_crossed_or_invalid({"bid": None, "ask": None}) is False      # missing handled elsewhere


def test_risk_engine_blocks_crossed_quote():
    eng = RiskEngine(make_settings())
    # ask=0 -> spread_pct negative; must be rejected as CROSSED_QUOTE, not pass.
    snap = {"bid": 283.52, "ask": 0.0, "spread_pct": -0.95, "dollar_volume": 50_000_000}
    d = eng.assess(direction="long", entry=283.52, stop=275.0, snapshot=snap)
    assert d.approved is False
    codes = {b["code"] for b in d.block_reasons}
    assert ReasonCode.CROSSED_QUOTE.value in codes
    assert ReasonCode.WIDE_SPREAD.value not in codes  # negative spread is not "wide"


def test_risk_engine_blocks_crossed_bid_above_ask():
    eng = RiskEngine(make_settings())
    snap = {"bid": 10.0, "ask": 9.0, "spread_pct": -0.1, "dollar_volume": 50_000_000}
    d = eng.assess(direction="long", entry=9.5, stop=9.0, snapshot=snap)
    assert any(b["code"] == ReasonCode.CROSSED_QUOTE.value for b in d.block_reasons)


class _CrossedMarket:
    def get_snapshot(self, symbol):
        ts = timeutils.now_utc().isoformat()
        return {
            "symbol": symbol, "provider": "alpaca", "feed": "iex", "is_mock": False,
            "last_price": 297.86, "bid": 283.52, "ask": 0.0, "spread": -283.52,
            "spread_pct": -0.95, "dollar_volume": 50_000_000,
            "change_pct": 0.03, "rel_volume": 2.0,
            "quote_timestamp": ts, "bar_timestamp": ts, "source_timestamp": ts,
            "received_at": ts, "market_session": "regular",
        }


def test_scanner_rejects_crossed_quote_during_open_session(journal):
    scanner = CandidateScanner(make_settings(), journal, market_data=_CrossedMarket())
    result = scanner.scan(symbols=["MSFT"])
    assert result.candidates == []
    rej = journal.query(
        "SELECT * FROM rejected_candidates WHERE reason_code = ?", (ReasonCode.CROSSED_QUOTE.value,)
    )
    assert len(rej) == 1
