"""INSTR-1 (part 2): Average True Range -- a volatility-scaled distance for
setting stops, replacing a fixed percentage that means wildly different
things for a low-vol name vs a high-vol one (a fixed 3% stop is roughly 4
daily sigma on SPY and under 1 sigma on TSLA -- "1R" currently means
different trades depending only on which symbol got scanned).

Frozen, deterministic, no external indicator library -- same house style as
REG-1's regime classifier (``alphaos/regime/classifier.py``): a pure
function over ordinary OHLC bars, versioned, no account/position/P&L input
possible by construction.

Uses a simple moving average of True Range (the classic, most widely cited
"ATR" variant), not Wilder's original smoothed/exponential average -- a
deliberate simplicity choice, consistent with this codebase's existing
preference for plain, auditable arithmetic over a smoothing recursion. A
future ``atr_rules_v2`` could switch to Wilder smoothing as its own
pre-registered, versioned change if that ever turns out to matter.
"""

from __future__ import annotations

from typing import Optional

ATR_RULES_V1 = "atr_rules_v1"

ATR_PERIOD = 14


def true_range(high: float, low: float, prev_close: Optional[float]) -> float:
    """TR = max(H-L, |H-PC|, |L-PC|). On the very first bar (no prior close),
    TR degrades to the simple H-L range -- the only well-defined value."""
    if prev_close is None:
        return high - low
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def compute_atr(bars: list[dict], period: int = ATR_PERIOD) -> Optional[float]:
    """``bars``: chronological list of ``{"high", "low", "close"}`` dicts
    (ascending by date, oldest first), typically the trailing ~20 calendar
    days of daily bars for one symbol. Needs at least ``period + 1`` bars
    (the extra one supplies the first bar's previous close) -- fewer than
    that returns ``None``, never a fabricated/partial-window average.

    Returns the simple average of True Range over the most recent
    ``period`` bars.
    """
    if len(bars) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(bars)):
        high = bars[i].get("high")
        low = bars[i].get("low")
        prev_close = bars[i - 1].get("close")
        if high is None or low is None:
            continue
        trs.append(true_range(float(high), float(low), float(prev_close) if prev_close is not None else None))
    if len(trs) < period:
        return None
    window = trs[-period:]
    return sum(window) / len(window)
