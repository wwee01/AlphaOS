"""Cost model: realistic-cost accounting (commission + slippage)."""

from __future__ import annotations

from alphaos.execution.costs import CostModel
from conftest import make_settings


def test_default_slippage_only():
    # Default: $0 commission, 1 bps/side slippage.
    m = CostModel()
    c = m.costs(qty=10, entry_price=100.0, exit_price=106.0)
    assert c.commission == 0.0
    # notional = 10*100 + 10*106 = 2060; 1 bps = 0.206 -> 0.21
    assert c.slippage == round(2060 * 0.0001, 4)
    assert c.total == round(c.commission + c.slippage, 2)


def test_per_share_commission_both_sides():
    m = CostModel(commission_per_share=0.005, slippage_bps=0.0)
    c = m.costs(qty=100, entry_price=50.0, exit_price=55.0)
    # per fill = 100*0.005 = 0.5; both sides = 1.0
    assert c.commission == 1.0
    assert c.slippage == 0.0
    assert c.total == 1.0


def test_min_commission_floor():
    m = CostModel(commission_per_share=0.001, min_commission=1.0, slippage_bps=0.0)
    c = m.costs(qty=10, entry_price=20.0, exit_price=21.0)
    # per fill floored at 1.0; both sides = 2.0
    assert c.commission == 2.0


def test_from_settings():
    s = make_settings(COST_COMMISSION_PER_SHARE="0.01", COST_SLIPPAGE_BPS="2.0", COST_MIN_COMMISSION="0.5")
    m = CostModel.from_settings(s)
    assert m.commission_per_share == 0.01
    assert m.slippage_bps == 2.0
    assert m.min_commission == 0.5
