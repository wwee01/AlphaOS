"""Shared trade-proposal model.

Both the swing strategy and the day-trade experiment emit this same structure,
tagged by ``strategy`` so the two books are never co-mingled downstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

from alphaos.util.ids import new_id


@dataclass
class TradeProposal:
    symbol: str
    direction: str
    strategy: str
    entry: float
    stop: float
    target: float
    max_holding_days: int
    qty: float
    risk_per_share: float
    dollar_risk: float
    expected_r: Optional[float]
    same_day_exit_eligible: bool
    candidate_id: str = ""
    eval_id: str = ""
    requires_margin: bool = False
    margin_approved: bool = False
    protection_path: Optional[str] = None
    status: str = "proposed"
    is_demo: bool = False
    proposal_id: str = field(default_factory=lambda: new_id("prop"))

    @classmethod
    def from_row(cls, row: dict) -> "TradeProposal":
        """Rebuild a proposal object from a persisted ``trade_proposals`` row."""
        return cls(
            symbol=row["symbol"],
            direction=row["direction"],
            strategy=row["strategy"],
            entry=row["entry"],
            stop=row["stop"],
            target=row["target"],
            max_holding_days=row.get("max_holding_days") or 0,
            qty=row["qty"],
            risk_per_share=row.get("risk_per_share") or 0.0,
            dollar_risk=row.get("dollar_risk") or 0.0,
            expected_r=row.get("expected_r"),
            same_day_exit_eligible=bool(row.get("same_day_exit_eligible")),
            candidate_id=row.get("candidate_id") or "",
            eval_id=row.get("eval_id") or "",
            requires_margin=bool(row.get("requires_margin")),
            margin_approved=bool(row.get("margin_approved")),
            protection_path=row.get("protection_path"),
            status=row.get("status") or "proposed",
            is_demo=bool(row.get("is_demo")),
            proposal_id=row["proposal_id"],
        )

    def to_row(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "candidate_id": self.candidate_id,
            "eval_id": self.eval_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "strategy": self.strategy,
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "max_holding_days": self.max_holding_days,
            "qty": self.qty,
            "risk_per_share": self.risk_per_share,
            "dollar_risk": self.dollar_risk,
            "expected_r": self.expected_r,
            "same_day_exit_eligible": 1 if self.same_day_exit_eligible else 0,
            "requires_margin": 1 if self.requires_margin else 0,
            "margin_approved": 1 if self.margin_approved else 0,
            "protection_path": self.protection_path,
            "status": self.status,
            "is_demo": 1 if self.is_demo else 0,
        }
