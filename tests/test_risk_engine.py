"""Risk engine blocks bad trades (test #4)."""

from __future__ import annotations

from alphaos.constants import ReasonCode
from alphaos.risk.risk_engine import RiskEngine
from conftest import make_settings


def _engine(**overrides):
    return RiskEngine(make_settings(**overrides))


def test_valid_trade_passes_and_sizes_from_risk():
    eng = _engine(PAPER_EQUITY="100000", MAX_RISK_PER_TRADE_PCT="0.01")
    d = eng.assess(direction="long", entry=100.0, stop=97.0,
                   snapshot={"spread_pct": 0.001, "dollar_volume": 10_000_000})
    assert d.approved is True
    # risk budget 1000 / risk_per_share 3 -> 333 shares
    assert d.sizing.shares == 333


def test_invalid_stop_blocks():
    eng = _engine()
    d = eng.assess(direction="long", entry=100.0, stop=105.0)  # stop above entry for long
    assert d.approved is False
    assert any(b["code"] == ReasonCode.INVALID_STOP.value for b in d.block_reasons)


def test_oversized_blocks_when_size_rounds_to_zero():
    eng = _engine(PAPER_EQUITY="1000", MAX_RISK_PER_TRADE_PCT="0.01")
    # budget = 10; entry 5000 -> cannot afford a single share -> size 0
    d = eng.assess(direction="long", entry=5000.0, stop=4900.0)
    assert d.approved is False
    assert any(b["code"] == ReasonCode.RISK_OVERSIZED.value for b in d.block_reasons)


def test_too_many_positions_blocks():
    eng = _engine(MAX_OPEN_POSITIONS="3")
    d = eng.assess(direction="long", entry=100.0, stop=97.0, open_positions=3)
    assert d.approved is False
    assert any(b["code"] == ReasonCode.TOO_MANY_POSITIONS.value for b in d.block_reasons)


def test_daily_loss_limit_blocks():
    eng = _engine(PAPER_EQUITY="100000", MAX_DAILY_LOSS_PCT="0.03")
    d = eng.assess(direction="long", entry=100.0, stop=97.0, realized_pnl_today=-3500.0)
    assert d.approved is False
    assert any(b["code"] == ReasonCode.DAILY_LOSS_LIMIT.value for b in d.block_reasons)


def test_daily_trade_limit_blocks():
    eng = _engine(MAX_PAPER_TRADES_PER_DAY="5")
    d = eng.assess(direction="long", entry=100.0, stop=97.0, trades_today=5)
    assert d.approved is False
    assert any(b["code"] == ReasonCode.DAILY_TRADE_LIMIT.value for b in d.block_reasons)


def test_wide_spread_and_low_liquidity_block():
    eng = _engine(MAX_SPREAD_PCT="0.01", MIN_DOLLAR_VOLUME="2000000")
    d = eng.assess(direction="long", entry=100.0, stop=97.0,
                   snapshot={"spread_pct": 0.05, "dollar_volume": 100_000})
    assert d.approved is False
    codes = {b["code"] for b in d.block_reasons}
    assert ReasonCode.WIDE_SPREAD.value in codes
    assert ReasonCode.LOW_LIQUIDITY.value in codes


def test_margin_required_without_approval_blocks():
    eng = _engine()
    d = eng.assess(direction="short", entry=100.0, stop=103.0, requires_margin=True, margin_approved=False)
    assert d.approved is False
    assert any(b["code"] == ReasonCode.MARGIN_APPROVAL_REQUIRED.value for b in d.block_reasons)
