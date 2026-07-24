"""INSTR-3: trend_rules_v1 -- an honest, versioned multi-day trend measure,
computed from persisted daily bars (``daily_bars``, see
``alphaos/data/daily_bars.py``) plus the SAME ATR(14) ruler
``alphaos.ai.openai_client._latest_atr`` uses for the live evaluator's own
stop override -- one ruler, never a second independently-issued ATR read
that could silently disagree with the one ``_apply_atr_stop`` enforces
against.

Motivating evidence (see
``docs/roadmap/alphaos-instr3-trend-context-spec.md``): the scanner's
existing ``trend_quality`` column (``candidate_scanner.py``) is
``round(min(1.0, abs(change_pct) * 10), 3)`` -- the SAME intraday
change_pct the model already sees, relabeled as a trend measure. This
module replaces nothing: ``trend_quality`` keeps its exact pre-INSTR-3
semantics for existing consumers (a Non-goal, grepped and left alone) --
this computes two NEW, ADDITIVE fields (``trend_score``/
``trend_rules_version``) that the v3 prompt shows INSTEAD of the dishonest
one; the old field is simply popped from what the model sees under v3 (see
``alphaos/ai/openai_client.py``'s ``_live_eval``), never overwritten or
repurposed here.
"""

from __future__ import annotations

from typing import Optional

from alphaos.ai.openai_client import _latest_atr
from alphaos.constants import TradeDirection
from alphaos.data.atr import ATR_STOP_MULTIPLIER_V1
from alphaos.data.daily_bars import get_recent_daily_bars

TREND_RULES_V1 = "trend_rules_v1"

# The window both sub-measures share: the last 10 COMPLETED daily sessions
# (never the scan day itself -- get_recent_daily_bars's own "before_date"
# contract already excludes it). Fewer than this many bars persisted yet in
# `daily_bars` -> honest absence (None, None), never a partial-window
# number and never a fallback to trend_quality's own formula.
_WINDOW_SESSIONS = 10


def compute_trend_score(
    journal, settings, symbol: str, direction: Optional[str], before_date: str,
) -> "tuple[Optional[float], Optional[str]]":
    """Returns ``(trend_score, trend_rules_version)``.

    ``consistency = (up_days - down_days) / 10`` -- ``up_days``/``down_days``
    are counted over the session-to-session CLOSE transitions WITHIN the
    10-bar window (9 transitions total: the window's own oldest bar has no
    in-window prior close to compare against); a flat day (close == prior
    close) counts toward neither. The divisor stays the fixed constant 10
    (the spec's own worked example: 7 up / 2 down out of 9 transitions ->
    consistency = (7-2)/10 = 0.5 exactly), not the count of transitions
    actually classified.

    ``extension = clamp((close_last - close_oldest_in_window) /
    (min_reward_risk x ATR_STOP_MULTIPLIER_V1 x ATR14), -1, +1)`` --
    denominated in the trade's OWN minimum target distance: the identical
    ``min_reward_risk x stop_multiplier`` product ATR_STOP_POLICY's own
    ``min_target_distance`` uses (computed from live config here, rather
    than the spec's own illustrative "2.4" literal, so this can never
    silently diverge from a real operator change to MIN_REWARD_RISK or
    ATR_STOP_MULTIPLIER_V1 -- equals 2.4 x ATR14 under today's settings:
    1.2 x 2.0).

    ``signed_trend = round(0.5 x consistency + 0.5 x extension, 3)``;
    ``trend_score`` = ``signed_trend`` for a long candidate, the NEGATED
    value for a short (alignment with trade direction: a name in a strong
    downtrend should score as a strong LONG-unfriendly / SHORT-friendly
    trend, not a uniformly "bad" number regardless of which way the
    candidate is pointed).

    ``(None, None)`` whenever fewer than 10 completed daily bars exist for
    ``symbol`` in ``daily_bars`` as of ``before_date``, OR no ATR(14) is
    available -- an honest absence the v3 prompt renders as "omit the
    trend line entirely", never a fabricated number."""
    bars = get_recent_daily_bars(journal, symbol, before_date, _WINDOW_SESSIONS)
    if len(bars) < _WINDOW_SESSIONS:
        return None, None

    closes: list[float] = []
    for b in bars:
        raw_close = b.get("close")
        if raw_close is None:
            return None, None
        closes.append(float(raw_close))

    atr = _latest_atr(journal, symbol)
    if atr is None or atr <= 0:
        return None, None

    up_days = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i - 1])
    down_days = sum(1 for i in range(1, len(closes)) if closes[i] < closes[i - 1])
    consistency = (up_days - down_days) / _WINDOW_SESSIONS

    target_distance_multiplier = settings.min_reward_risk * ATR_STOP_MULTIPLIER_V1
    denom = target_distance_multiplier * atr
    extension = (closes[-1] - closes[0]) / denom if denom else 0.0
    extension = max(-1.0, min(1.0, extension))

    signed_trend = round(0.5 * consistency + 0.5 * extension, 3)
    trend_score = signed_trend if direction != TradeDirection.SHORT.value else -signed_trend
    return round(trend_score, 3), TREND_RULES_V1
