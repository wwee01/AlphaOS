"""Candidate Packet (Roadmap 2.3).

A compact, structured evidence packet for ONE shortlisted candidate — the ONLY
thing sent to the AI category labeller. We never send raw noisy market data
(no `_snapshot`, no bar arrays): `to_prompt_dict()` whitelists compact keys only.

Placeholder context fields (catalyst/news/last30days/sentiment) are explicit
"unavailable" markers — no news/catalyst integration exists in v1; the fields are
here so later enrichment slots in without faking data now.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from alphaos.constants import CONTEXT_UNAVAILABLE_V1
from alphaos.scanner.interest_scanner import InterestSignals
from alphaos.util.ids import new_id

# The exact compact keys sent to the AI. Kept explicit so a test can assert the
# packet never leaks raw data and stays token-efficient.
PROMPT_KEYS = (
    "symbol", "last_price", "direction",
    "freshness_status", "spread_pct", "liquidity_ok", "dollar_volume",
    "change_pct", "rel_volume", "rel_strength_vs_spy", "rel_strength_vs_qqq",
    "near_day_high", "near_day_low", "gap_pct",
    "structure_hint", "setup_hint", "tradeable_volatility",
    "interest_score", "shortlist_reason", "momentum_score",
    "missing_data_flags",
    "catalyst_status", "official_news_context", "last30days_context", "sentiment_context",
)


@dataclass
class CandidatePacket:
    packet_id: str
    candidate_id: str
    symbol: str
    last_price: Optional[float]
    direction: str
    freshness_status: str
    spread_pct: Optional[float]
    liquidity_ok: bool
    dollar_volume: Optional[float]
    change_pct: Optional[float]
    rel_volume: Optional[float]
    rel_strength_vs_spy: Optional[float]
    rel_strength_vs_qqq: Optional[float]
    near_day_high: bool
    near_day_low: bool
    gap_pct: Optional[float]
    structure_hint: str
    setup_hint: str
    tradeable_volatility: bool
    interest_score: float
    interest_rank: Optional[int]
    shortlist_reason: str
    momentum_score: Optional[float]
    missing_data_flags: list = field(default_factory=list)
    # placeholder context — always these literals in v1 (no news/catalyst yet)
    catalyst_status: str = CONTEXT_UNAVAILABLE_V1
    official_news_context: str = CONTEXT_UNAVAILABLE_V1
    last30days_context: str = CONTEXT_UNAVAILABLE_V1
    sentiment_context: str = CONTEXT_UNAVAILABLE_V1

    def to_prompt_dict(self) -> dict:
        """The compact dict sent to the AI. Whitelist only — never raw data."""
        d = {
            "symbol": self.symbol,
            "last_price": self.last_price,
            "direction": self.direction,
            "freshness_status": self.freshness_status,
            "spread_pct": self.spread_pct,
            "liquidity_ok": bool(self.liquidity_ok),
            "dollar_volume": self.dollar_volume,
            "change_pct": self.change_pct,
            "rel_volume": self.rel_volume,
            "rel_strength_vs_spy": self.rel_strength_vs_spy,
            "rel_strength_vs_qqq": self.rel_strength_vs_qqq,
            "near_day_high": bool(self.near_day_high),
            "near_day_low": bool(self.near_day_low),
            "gap_pct": self.gap_pct,
            "structure_hint": self.structure_hint,
            "setup_hint": self.setup_hint,
            "tradeable_volatility": bool(self.tradeable_volatility),
            "interest_score": self.interest_score,
            "shortlist_reason": self.shortlist_reason,
            "momentum_score": self.momentum_score,
            "missing_data_flags": list(self.missing_data_flags),
            "catalyst_status": self.catalyst_status,
            "official_news_context": self.official_news_context,
            "last30days_context": self.last30days_context,
            "sentiment_context": self.sentiment_context,
        }
        return d

    def to_row(self, scan_batch_id: Optional[str] = None) -> dict:
        """Row for the ``candidate_packets`` journal table (full compact packet)."""
        return {
            "packet_id": self.packet_id,
            "candidate_id": self.candidate_id,
            "scan_batch_id": scan_batch_id,
            "symbol": self.symbol,
            "interest_score": self.interest_score,
            "interest_rank": self.interest_rank,
            "shortlist_reason": self.shortlist_reason,
            "packet_json": self.to_prompt_dict(),
            "missing_data_flags_json": list(self.missing_data_flags),
            "catalyst_status": self.catalyst_status,
            "official_news_context": self.official_news_context,
            "last30days_context": self.last30days_context,
            "sentiment_context": self.sentiment_context,
        }


def build_packet(cand: dict, snapshot: dict, signals: InterestSignals,
                 interest_rank: Optional[int] = None) -> CandidatePacket:
    """Build a compact packet from a scanner candidate + its snapshot + interest
    signals. Pure — no I/O. ``cand`` carries momentum_score; ``signals`` carries
    the deterministic interest evidence."""
    return CandidatePacket(
        packet_id=new_id("pkt"),
        candidate_id=cand.get("candidate_id", ""),
        symbol=cand.get("symbol"),
        last_price=snapshot.get("last_price"),
        direction=cand.get("direction") or signals.direction_hint,
        freshness_status=snapshot.get("freshness_status") or "usable",
        spread_pct=snapshot.get("spread_pct"),
        liquidity_ok=bool(cand.get("liquidity_ok", 1)),
        dollar_volume=snapshot.get("dollar_volume"),
        change_pct=signals.change_pct,
        rel_volume=signals.rel_volume,
        rel_strength_vs_spy=signals.rel_strength_vs_spy,
        rel_strength_vs_qqq=signals.rel_strength_vs_qqq,
        near_day_high=signals.near_day_high,
        near_day_low=signals.near_day_low,
        gap_pct=signals.gap_pct,
        structure_hint=signals.structure_hint,
        setup_hint=signals.setup_hint,
        tradeable_volatility=signals.tradeable_volatility,
        interest_score=signals.interest_score,
        interest_rank=interest_rank,
        shortlist_reason=signals.shortlist_reason,
        momentum_score=cand.get("momentum_score"),
        missing_data_flags=list(signals.missing_data_flags),
    )
