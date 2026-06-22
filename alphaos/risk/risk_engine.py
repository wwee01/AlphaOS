"""Risk engine.

The decision engine proposes; the risk engine disposes. Position size is derived
from risk (equity, max risk per trade, entry, stop) — never from raw buying
power. The engine accumulates ALL violated checks so the journal shows the full
picture, rather than short-circuiting on the first failure.

v1 checks: valid invalidation level, risk-based sizing, no-leverage cap,
max open positions, max trades/day, daily-loss limit, spread, liquidity, and the
margin/short approval gate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Optional

from alphaos.constants import ReasonCode, TradeDirection
from alphaos.config.settings import Settings
from alphaos.data.freshness_guard import quote_crossed_or_invalid


@dataclass
class PositionSizing:
    shares: int
    risk_per_share: float
    dollar_risk: float
    position_value: float
    risk_budget: float
    capped_by_buying_power: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class RiskDecision:
    approved: bool
    block_reasons: list = field(default_factory=list)   # list of {code, detail}
    warnings: list = field(default_factory=list)
    sizing: Optional[PositionSizing] = None

    def as_dict(self) -> dict:
        return {
            "approved": self.approved,
            "block_reasons": self.block_reasons,
            "warnings": self.warnings,
            "sizing": self.sizing.as_dict() if self.sizing else None,
        }

    @property
    def primary_reason(self) -> Optional[str]:
        return self.block_reasons[0]["code"] if self.block_reasons else None


class RiskEngine:
    def __init__(self, settings: Settings):
        self.s = settings

    def size_position(self, entry: float, stop: float, equity: float) -> PositionSizing:
        risk_per_share = abs(float(entry) - float(stop))
        risk_budget = equity * self.s.max_risk_per_trade_pct
        raw_shares = (risk_budget / risk_per_share) if risk_per_share > 0 else 0
        shares = int(math.floor(raw_shares))
        capped = False
        # No leverage in v1: a paper position cannot exceed equity in notional.
        max_affordable = int(math.floor(equity / entry)) if entry > 0 else 0
        if shares > max_affordable:
            shares = max_affordable
            capped = True
        dollar_risk = shares * risk_per_share
        position_value = shares * float(entry)
        return PositionSizing(
            shares=shares,
            risk_per_share=round(risk_per_share, 4),
            dollar_risk=round(dollar_risk, 2),
            position_value=round(position_value, 2),
            risk_budget=round(risk_budget, 2),
            capped_by_buying_power=capped,
        )

    def assess(
        self,
        *,
        direction: str,
        entry: Optional[float],
        stop: Optional[float],
        snapshot: Optional[dict] = None,
        open_positions: int = 0,
        trades_today: int = 0,
        realized_pnl_today: float = 0.0,
        requires_margin: bool = False,
        margin_approved: bool = False,
        equity: Optional[float] = None,
    ) -> RiskDecision:
        equity = float(equity if equity is not None else self.s.paper_equity)
        blocks: list[dict] = []
        warnings: list[str] = []
        sizing: Optional[PositionSizing] = None

        # 1) Valid invalidation level (every trade needs a stop before entry).
        valid_stop = True
        if entry is None or stop is None or entry <= 0 or stop <= 0 or entry == stop:
            valid_stop = False
            blocks.append({"code": ReasonCode.INVALID_STOP.value, "detail": "missing/invalid entry or stop"})
        else:
            if direction == TradeDirection.LONG.value and stop >= entry:
                valid_stop = False
                blocks.append({"code": ReasonCode.INVALID_STOP.value, "detail": "long stop must be below entry"})
            elif direction == TradeDirection.SHORT.value and stop <= entry:
                valid_stop = False
                blocks.append({"code": ReasonCode.INVALID_STOP.value, "detail": "short stop must be above entry"})

        # 2) Risk-based sizing (only if the stop is valid).
        if valid_stop:
            sizing = self.size_position(entry, stop, equity)
            if sizing.shares <= 0:
                blocks.append(
                    {
                        "code": ReasonCode.RISK_OVERSIZED.value,
                        "detail": "risk-based size rounds to 0 shares (risk/price too large)",
                    }
                )
            elif sizing.dollar_risk > sizing.risk_budget + 1e-6:
                blocks.append(
                    {
                        "code": ReasonCode.RISK_OVERSIZED.value,
                        "detail": f"dollar risk {sizing.dollar_risk} exceeds budget {sizing.risk_budget}",
                    }
                )
            if sizing.capped_by_buying_power:
                warnings.append("position size capped by buying power (no leverage in v1)")

        # 3) Concurrency / frequency limits.
        if open_positions >= self.s.max_open_positions:
            blocks.append(
                {
                    "code": ReasonCode.TOO_MANY_POSITIONS.value,
                    "detail": f"open {open_positions} >= max {self.s.max_open_positions}",
                }
            )
        if trades_today >= self.s.max_paper_trades_per_day:
            blocks.append(
                {
                    "code": ReasonCode.DAILY_TRADE_LIMIT.value,
                    "detail": f"trades today {trades_today} >= max {self.s.max_paper_trades_per_day}",
                }
            )

        # 4) Daily loss limit (realized).
        max_loss = equity * self.s.max_daily_loss_pct
        if realized_pnl_today <= -abs(max_loss):
            blocks.append(
                {
                    "code": ReasonCode.DAILY_LOSS_LIMIT.value,
                    "detail": f"realized {realized_pnl_today} breaches -{max_loss}",
                }
            )

        # 5) Spread + liquidity (from the freshness-checked snapshot).
        if snapshot:
            # 5a) Reject crossed/non-positive quotes before the spread gate, so a
            #     malformed quote (ask<=0 or ask<bid -> negative spread) can't slip.
            if quote_crossed_or_invalid(snapshot):
                blocks.append(
                    {
                        "code": ReasonCode.CROSSED_QUOTE.value,
                        "detail": f"crossed/invalid quote bid={snapshot.get('bid')} ask={snapshot.get('ask')}",
                    }
                )
            spread_pct = snapshot.get("spread_pct")
            if spread_pct is not None and spread_pct >= 0 and spread_pct > self.s.max_spread_pct:
                blocks.append(
                    {
                        "code": ReasonCode.WIDE_SPREAD.value,
                        "detail": f"spread {spread_pct:.4f} > max {self.s.max_spread_pct}",
                    }
                )
            dollar_volume = snapshot.get("dollar_volume")
            if dollar_volume is not None and dollar_volume < self.s.min_dollar_volume:
                blocks.append(
                    {
                        "code": ReasonCode.LOW_LIQUIDITY.value,
                        "detail": f"$vol {dollar_volume} < min {self.s.min_dollar_volume}",
                    }
                )

        # 6) Margin / short approval gate. Shorting is paper-only; if it needs
        #    margin/borrow/leverage it must be explicitly approved first.
        if requires_margin and not margin_approved:
            blocks.append(
                {
                    "code": ReasonCode.MARGIN_APPROVAL_REQUIRED.value,
                    "detail": "trade requires margin/borrow/leverage; explicit user approval required",
                }
            )

        return RiskDecision(
            approved=len(blocks) == 0,
            block_reasons=blocks,
            warnings=warnings,
            sizing=sizing,
        )
