"""Frozen, deterministic four-state market regime classifier (REG-1).

LAW FOR THIS SUBSYSTEM (do not violate): inputs describe the market, never
the account. ``classify_regime_series()`` takes ONLY a chronological list of
``{"date", "close"}`` bars -- there is no parameter through which P&L,
drawdown, position, or any account-state field COULD enter, by construction,
not by convention. A test enforces this at the signature level
(``inspect.signature``) plus a source-level keyword-blacklist check, so a
future edit that tries to widen the signature to accept account state fails
loudly.

Not a market-timing model: thresholds are literals, versioned as
``REGIME_RULES_V1`` ("regime_rules_v1"), fixed here BEFORE any conditional
result is examined against outcomes (anti-data-mining law). Changing any
threshold is a new pre-registered version (``regime_rules_v2``) -- v1 rows
are never relabeled in place; they simply stop being produced once a v2
module ships, and existing ``regime_days`` rows stamped v1 stay v1 forever.

Design note (EOD-only, no intraday peeking): callers pass whatever daily
bars are available AT SCAN TIME. Under normal cadence (the benchmark-spine
job runs once daily, after market close) the latest available close as of a
morning scan is naturally YESTERDAY's officially-closed session -- that is
the intended behavior, not a bug: "today's" regime label is deliberately
informed by the most recent CLOSED bar, never a live/intraday price.
"""

from __future__ import annotations

from typing import Optional

REGIME_RULES_V1 = "regime_rules_v1"

_SMA_SHORT_DAYS = 50
_SMA_LONG_DAYS = 200
_VOL_LOOKBACK_DAYS = 20
_VOL_PERCENTILE_WINDOW_DAYS = 252  # ~1 trading year
_CHOP_DEV_THRESHOLD_PCT = 1.5  # strictly less-than
_CHOP_MIN_CONSECUTIVE_SESSIONS = 5  # inclusive, counting today
_CRISIS_VOL_PERCENTILE = 90.0  # inclusive
_TRADING_DAYS_PER_YEAR = 252

# The earliest classifiable day needs this many PRIOR bars: the vol-percentile
# window itself needs _VOL_PERCENTILE_WINDOW_DAYS worth of already-computed
# 20-day vol values, each of which itself needs _VOL_LOOKBACK_DAYS prior
# closes. Days short of this are simply absent from the output -- unknown
# history is never fabricated into a classification.
MIN_BARS_FOR_FIRST_CLASSIFICATION = _VOL_PERCENTILE_WINDOW_DAYS + _VOL_LOOKBACK_DAYS


def _sma(closes: list, n: int) -> Optional[float]:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def _realized_vol(closes: list, n: int) -> Optional[float]:
    """Annualized stdev of daily simple returns over the trailing ``n``
    closes (``n + 1`` prices needed for ``n`` returns). Annualizing is a
    constant scalar -- it changes the NUMBER but never the RANK, so it has
    zero effect on the percentile computation below; used only because an
    annualized figure is the conventional, spot-checkable one."""
    if len(closes) < n + 1:
        return None
    window = closes[-(n + 1):]
    returns = [(window[i] - window[i - 1]) / window[i - 1] for i in range(1, len(window))]
    mean_r = sum(returns) / len(returns)
    variance = sum((r - mean_r) ** 2 for r in returns) / len(returns)
    return (variance ** 0.5) * (_TRADING_DAYS_PER_YEAR ** 0.5)


def _percentile_rank(value: float, population: list) -> float:
    """Nearest-rank percentile (0-100) of ``value`` WITHIN ``population``
    (which should already include ``value`` itself, matching "today's own
    vol as a percentile of its trailing distribution"). Deterministic, no
    numpy dependency."""
    if not population:
        return 0.0
    return 100.0 * sum(1 for v in population if v <= value) / len(population)


def classify_regime_series(bars: list) -> list:
    """``bars``: chronological list of ``{"date": "YYYY-MM-DD", "close":
    float}`` for a single symbol (SPY), ascending by date. Weekends/holidays
    are simply absent (matching ``benchmark_bars``' own trading-calendar-
    implicit storage) -- no gap-filling performed or assumed.

    Returns one dict per day with ENOUGH trailing history to classify
    (fewer than ``MIN_BARS_FOR_FIRST_CLASSIFICATION`` prior bars -> that day
    is simply absent from the output): ``{"date", "regime", "rules_version",
    "spy_close", "sma_50", "sma_200", "realized_vol_20d",
    "vol_percentile_1y", "dev_from_sma50_pct", "chop_streak_days"}``.

    THE ONLY PARAMETER IS ``bars`` -- see module docstring's law. Do not
    widen this signature to accept account/P&L/position data.
    """
    closes = [b["close"] for b in bars]
    dates = [b["date"] for b in bars]
    n = len(bars)

    sma50_series: list = [None] * n
    sma200_series: list = [None] * n
    vol20_series: list = [None] * n
    dev_pct_series: list = [None] * n
    for i in range(n):
        window_closes = closes[: i + 1]
        sma50_series[i] = _sma(window_closes, _SMA_SHORT_DAYS)
        sma200_series[i] = _sma(window_closes, _SMA_LONG_DAYS)
        vol20_series[i] = _realized_vol(window_closes, _VOL_LOOKBACK_DAYS)
        if sma50_series[i]:
            dev_pct_series[i] = abs(closes[i] - sma50_series[i]) / sma50_series[i] * 100.0

    out = []
    for i in range(n):
        if i + 1 < MIN_BARS_FOR_FIRST_CLASSIFICATION:
            continue
        vol_today = vol20_series[i]
        if vol_today is None:
            continue
        window_start = max(0, i - _VOL_PERCENTILE_WINDOW_DAYS + 1)
        vol_population = [v for v in vol20_series[window_start: i + 1] if v is not None]
        vol_pct = _percentile_rank(vol_today, vol_population)

        sma50, sma200 = sma50_series[i], sma200_series[i]
        dev_pct = dev_pct_series[i]

        # Chop streak: consecutive sessions ending today (inclusive) with
        # dev_pct strictly below the threshold.
        streak = 0
        for j in range(i, -1, -1):
            d = dev_pct_series[j]
            if d is not None and d < _CHOP_DEV_THRESHOLD_PCT:
                streak += 1
            else:
                break

        # Rules, top-down, first match wins (CRISIS checked unconditionally
        # first -- it wins over EVERY other rule, not just TREND_UP).
        if vol_pct >= _CRISIS_VOL_PERCENTILE:
            regime = "CRISIS"
        elif streak >= _CHOP_MIN_CONSECUTIVE_SESSIONS:
            regime = "CHOP"
        elif sma50 is not None and sma200 is not None and closes[i] > sma50 > sma200:
            regime = "TREND_UP"
        elif sma50 is not None and sma200 is not None and closes[i] < sma50 < sma200:
            regime = "TREND_DN"
        else:
            regime = "CHOP"

        out.append({
            "date": dates[i],
            "regime": regime,
            "rules_version": REGIME_RULES_V1,
            "spy_close": closes[i],
            "sma_50": sma50,
            "sma_200": sma200,
            "realized_vol_20d": vol_today,
            "vol_percentile_1y": vol_pct,
            "dev_from_sma50_pct": dev_pct,
            "chop_streak_days": streak,
        })
    return out
