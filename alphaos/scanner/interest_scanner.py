"""Market Interest Scanner (Roadmap 2.3).

Deterministic, evidence-based "is this worth a closer look?" scoring over a single
market snapshot (+ optional SPY/QQQ reference snapshots). It produces an
``interest_score`` and structured behaviour signals — gap, near day high/low,
relative volume, relative strength vs the index, breakout/reversal structure,
tradeable volatility — plus a short ``shortlist_reason`` and ``missing_data_flags``.

IMPORTANT: "interesting" is NOT "trade". This module never decides to trade, never
touches risk/freshness/approval/execution. It only ranks what is worth sending to
the AI category labeller. It is pure (no I/O, no randomness, no timestamps), so a
scan is reproducible and hermetically testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

from alphaos.constants import TradeDirection


def _f(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _norm(x: Optional[float], scale: float) -> float:
    """Normalise |x| onto 0..1 with a soft cap at ``scale``."""
    if x is None or scale <= 0:
        return 0.0
    return min(1.0, abs(x) / scale)


@dataclass
class InterestSignals:
    interest_score: float
    direction_hint: str
    change_pct: Optional[float] = None
    rel_volume: Optional[float] = None
    gap_pct: Optional[float] = None
    near_day_high: bool = False
    near_day_low: bool = False
    rel_strength_vs_spy: Optional[float] = None
    rel_strength_vs_qqq: Optional[float] = None
    structure_hint: str = "range"          # breakout | reversal | trend | range
    setup_hint: str = ""
    tradeable_volatility: bool = False
    shortlist_reason: str = ""
    missing_data_flags: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


class InterestScanner:
    """Scores a snapshot for tradeable interest. Stateless apart from config.

    ``change_scale``/``rel_vol_scale``/``day_range_min`` default to the
    original megacap-calibrated literals (0.06 / 2.0 / 0.02) so every
    existing core-tier caller stays byte-identical. EXP-1 mechanism 3:
    these are the ONLY thing a tier-scoped caller (the shadow-tier scan
    pass) may override -- same formula shape, no fork, no new code path;
    "recalibrate the pre-rank, never redesign it."
    """

    def __init__(
        self, settings, change_scale: float = 0.06, rel_vol_scale: float = 2.0,
        day_range_min: float = 0.02,
    ):
        self.s = settings
        self.change_scale = change_scale
        self.rel_vol_scale = rel_vol_scale
        self.day_range_min = day_range_min

    def score(self, snapshot: dict, spy: Optional[dict] = None,
              qqq: Optional[dict] = None) -> InterestSignals:
        last = _f(snapshot.get("last_price"))
        prev_close = _f(snapshot.get("prev_close"))
        change = _f(snapshot.get("change_pct"))
        rel_vol = _f(snapshot.get("rel_volume"))
        high = _f(snapshot.get("bar_high"))
        low = _f(snapshot.get("bar_low"))
        op = _f(snapshot.get("bar_open"))

        missing: list[str] = []
        if prev_close is None:
            missing.append("no_prev_close")
        if snapshot.get("bid") is None or snapshot.get("ask") is None:
            missing.append("no_bid_ask")
        if rel_vol is None:
            missing.append("no_rel_volume")
        if change is None:
            missing.append("no_change_pct")

        gap = ((op - prev_close) / prev_close) if (op and prev_close) else None
        near = self.s.interest_near_extreme_pct
        near_high = bool(last and high and last <= high and (high - last) / last <= near)
        near_low = bool(last and low and last >= low and (last - low) / low <= near)
        rs_spy = (change - _f(spy.get("change_pct"))) if (change is not None and spy and spy.get("change_pct") is not None) else None
        rs_qqq = (change - _f(qqq.get("change_pct"))) if (change is not None and qqq and qqq.get("change_pct") is not None) else None
        day_range = ((high - low) / last) if (high and low and last) else None
        tradeable_vol = bool(day_range is not None and day_range >= self.day_range_min)

        # --- structure classification (deterministic, conservative) ---
        if near_high and (change or 0) > 0 and (rel_vol or 1.0) >= 1.3:
            structure, setup, direction = "breakout", "breakout continuation", TradeDirection.LONG.value
        elif (gap is not None and gap <= -0.01 and last and op and last > op) or (near_low and (change or 0) < 0):
            structure, setup, direction = "reversal", "reversal / dip off the low", TradeDirection.LONG.value
        elif abs(change or 0) >= 0.02:
            structure = "trend"
            setup = "trend continuation"
            direction = TradeDirection.LONG.value if (change or 0) >= 0 else TradeDirection.SHORT.value
        else:
            structure, setup, direction = "range", "range / no clear setup", (
                TradeDirection.LONG.value if (change or 0) >= 0 else TradeDirection.SHORT.value
            )

        # --- weighted interest score (0..1) ---
        score = (
            0.30 * _norm(change, self.change_scale)
            + 0.20 * _norm((rel_vol - 1.0) if rel_vol is not None else None, self.rel_vol_scale)
            + 0.15 * _norm(gap, 0.05)
            + 0.15 * _norm(rs_spy, 0.05)
            + 0.10 * (1.0 if (near_high or near_low) else 0.0)
            + 0.10 * (1.0 if tradeable_vol else 0.0)
        )
        if structure in ("breakout", "reversal"):
            score = min(1.0, score + 0.05)
        score = round(score, 4)

        reasons: list[str] = []
        if near_high:
            reasons.append("near day high")
        if near_low:
            reasons.append("near day low")
        if gap is not None and abs(gap) >= 0.02:
            reasons.append(f"gap {round(gap * 100, 1)}%")
        if rel_vol is not None and rel_vol >= 1.5:
            reasons.append(f"rel-vol {round(rel_vol, 1)}x")
        if change is not None and abs(change) >= 0.02:
            reasons.append(f"move {round(change * 100, 1)}%")
        if rs_spy is not None and abs(rs_spy) >= 0.02:
            reasons.append(f"rel-strength vs SPY {round(rs_spy * 100, 1)}%")
        reason = f"{structure}: " + (", ".join(reasons) if reasons else "tradeable behaviour")

        return InterestSignals(
            interest_score=score,
            direction_hint=direction,
            change_pct=round(change, 4) if change is not None else None,
            rel_volume=round(rel_vol, 3) if rel_vol is not None else None,
            gap_pct=round(gap, 4) if gap is not None else None,
            near_day_high=near_high,
            near_day_low=near_low,
            rel_strength_vs_spy=round(rs_spy, 4) if rs_spy is not None else None,
            rel_strength_vs_qqq=round(rs_qqq, 4) if rs_qqq is not None else None,
            structure_hint=structure,
            setup_hint=setup,
            tradeable_volatility=tradeable_vol,
            shortlist_reason=reason,
            missing_data_flags=missing,
        )
