"""Trade cost model — realistic-cost accounting for paper P&L.

Net P&L is computed after costs so the no-news baseline numbers are meaningful
(the build discipline requires "after realistic costs"). Costs are explicit and
configurable; the breakdown is commission (per fill) + slippage (bps per side).

Defaults: Alpaca US equities have $0 commission, so commission defaults to 0;
a small slippage (1 bps per side) is applied so net != gross by default.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TradeCosts:
    commission: float
    slippage: float
    total: float

    def as_dict(self) -> dict:
        return {"commission": self.commission, "slippage": self.slippage, "total": self.total}


@dataclass(frozen=True)
class CostModel:
    commission_per_share: float = 0.0   # Alpaca US equities: $0
    min_commission: float = 0.0         # per-fill floor
    slippage_bps: float = 1.0           # per side (entry + exit)

    @classmethod
    def from_settings(cls, settings) -> "CostModel":
        return cls(
            commission_per_share=settings.cost_commission_per_share,
            min_commission=settings.cost_min_commission,
            slippage_bps=settings.cost_slippage_bps,
        )

    def costs(self, qty: float, entry_price: float, exit_price: float) -> TradeCosts:
        qty = abs(float(qty or 0))
        entry_price = float(entry_price or 0)
        exit_price = float(exit_price or 0)
        per_fill_commission = max(self.min_commission, qty * self.commission_per_share)
        commission = round(per_fill_commission * 2, 4)  # entry + exit
        notional = qty * entry_price + qty * exit_price
        slippage = round(notional * (self.slippage_bps / 10_000.0), 4)
        return TradeCosts(commission=commission, slippage=slippage, total=round(commission + slippage, 2))
