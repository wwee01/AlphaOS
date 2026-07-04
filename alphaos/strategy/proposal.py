"""Shared trade-proposal model.

Both the swing strategy and the day-trade experiment emit this same structure,
tagged by ``strategy`` so the two books are never co-mingled downstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

from alphaos.constants import TargetProfile
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
    # --- target-profile tracking (evidence only; no behavior change) ---
    target_profile: str = TargetProfile.CONFIGURED_STANDARD.value
    target_reward_risk: Optional[float] = None
    min_reward_risk: Optional[float] = None
    stop_loss_pct: Optional[float] = None
    target_price_source: Optional[str] = None
    stop_price_source: Optional[str] = None
    # --- traceability (Trade Packet v1) ---
    risk_check_id: Optional[str] = None
    claude_review_id: Optional[str] = None
    playbook_name: Optional[str] = None
    setup_classification: Optional[str] = None
    expected_hold_days: Optional[int] = None
    scan_batch_id: Optional[str] = None
    proposal_reason: Optional[str] = None
    # --- proposal TTL / stale-approval guard (Roadmap PR6) ---
    proposal_ttl_seconds: Optional[int] = None
    proposal_expires_at_utc: Optional[str] = None
    expired_reason: Optional[str] = None
    expired_at_utc: Optional[str] = None
    superseded_by_proposal_id: Optional[str] = None
    superseded_at_utc: Optional[str] = None
    proposal_id: str = field(default_factory=lambda: new_id("prop"))
    # Every proposal is born with a stable trade_id — the central correlation key
    # that survives proposal -> order -> position -> exit -> outcome.
    trade_id: str = field(default_factory=lambda: new_id("trade"))

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
            target_profile=row.get("target_profile") or TargetProfile.CONFIGURED_STANDARD.value,
            target_reward_risk=row.get("target_reward_risk"),
            min_reward_risk=row.get("min_reward_risk"),
            stop_loss_pct=row.get("stop_loss_pct"),
            target_price_source=row.get("target_price_source"),
            stop_price_source=row.get("stop_price_source"),
            risk_check_id=row.get("risk_check_id"),
            claude_review_id=row.get("claude_review_id"),
            playbook_name=row.get("playbook_name"),
            setup_classification=row.get("setup_classification"),
            expected_hold_days=row.get("expected_hold_days"),
            scan_batch_id=row.get("scan_batch_id"),
            proposal_reason=row.get("proposal_reason"),
            proposal_ttl_seconds=row.get("proposal_ttl_seconds"),
            proposal_expires_at_utc=row.get("proposal_expires_at_utc"),
            expired_reason=row.get("expired_reason"),
            expired_at_utc=row.get("expired_at_utc"),
            superseded_by_proposal_id=row.get("superseded_by_proposal_id"),
            superseded_at_utc=row.get("superseded_at_utc"),
            proposal_id=row["proposal_id"],
            trade_id=row.get("trade_id") or new_id("trade"),
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
            "target_profile": self.target_profile,
            "target_reward_risk": self.target_reward_risk,
            "min_reward_risk": self.min_reward_risk,
            "stop_loss_pct": self.stop_loss_pct,
            "target_price_source": self.target_price_source,
            "stop_price_source": self.stop_price_source,
            "trade_id": self.trade_id,
            "risk_check_id": self.risk_check_id,
            "claude_review_id": self.claude_review_id,
            "playbook_name": self.playbook_name,
            "setup_classification": self.setup_classification,
            "expected_hold_days": self.expected_hold_days,
            "scan_batch_id": self.scan_batch_id,
            "proposal_reason": self.proposal_reason,
            "proposal_ttl_seconds": self.proposal_ttl_seconds,
            "proposal_expires_at_utc": self.proposal_expires_at_utc,
            "expired_reason": self.expired_reason,
            "expired_at_utc": self.expired_at_utc,
            "superseded_by_proposal_id": self.superseded_by_proposal_id,
            "superseded_at_utc": self.superseded_at_utc,
        }
