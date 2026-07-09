"""INSTR-1 (part 2): alphaos.data.atr -- deterministic, direct-construction
fixtures only.
"""

from __future__ import annotations

from alphaos.data.atr import ATR_PERIOD, compute_atr, true_range


# --------------------------------------------------------------- true_range
def test_true_range_no_prior_close_is_just_the_bar_range():
    assert true_range(high=105.0, low=95.0, prev_close=None) == 10.0


def test_true_range_uses_the_widest_of_the_three_components():
    # Gap up: prior close far below today's low -> |low - prev_close| dominates.
    assert true_range(high=105.0, low=103.0, prev_close=95.0) == 10.0
    # Gap down: prior close far above today's high -> |high - prev_close| dominates.
    assert true_range(high=95.0, low=93.0, prev_close=105.0) == 12.0
    # No gap: plain high-low range dominates.
    assert true_range(high=105.0, low=95.0, prev_close=100.0) == 10.0


# --------------------------------------------------------------- compute_atr
def _bars(n, high, low, close):
    return [{"high": high, "low": low, "close": close} for _ in range(n)]


def test_insufficient_bars_returns_none():
    assert compute_atr(_bars(ATR_PERIOD, 101.0, 99.0, 100.0)) is None  # need period+1
    assert compute_atr([]) is None


def test_atr_of_a_perfectly_uniform_series_is_exact():
    """Every bar identical (high=101, low=99, close=100) -> every True Range
    after the first bar is max(2, 1, 1) = 2.0 exactly, so ATR(14) = 2.0."""
    bars = _bars(ATR_PERIOD + 1, 101.0, 99.0, 100.0)
    result = compute_atr(bars)
    assert result == 2.0


def test_atr_uses_only_the_trailing_window_not_the_whole_history():
    """A long-ago volatility spike outside the trailing 14-period window
    must not affect the result -- confirms the [-period:] slicing."""
    quiet = _bars(ATR_PERIOD, 101.0, 99.0, 100.0)          # 14 quiet bars
    spike = [{"high": 200.0, "low": 50.0, "close": 100.0}]  # 1 wild bar, oldest
    bars = spike + quiet  # 15 bars total: spike is bar 0 (supplies only a prev_close)
    result = compute_atr(bars)
    assert result == 2.0  # spike bar itself is never a TR observation, only a prev_close


def test_atr_reflects_genuinely_higher_volatility():
    calm = compute_atr(_bars(ATR_PERIOD + 1, 101.0, 99.0, 100.0))
    volatile = compute_atr(_bars(ATR_PERIOD + 1, 110.0, 90.0, 100.0))
    assert volatile > calm


def test_atr_skips_bars_with_missing_high_or_low():
    bars = _bars(ATR_PERIOD, 101.0, 99.0, 100.0) + [{"high": None, "low": 99.0, "close": 100.0}]
    # 15 bars, but one has no high -> only 13 valid TRs producible from 14 transitions -> insufficient.
    assert compute_atr(bars) is None


def test_atr_is_a_simple_moving_average_hand_verified():
    """Two distinct TR values alternating -> exact hand-computed average."""
    bars = [{"high": 100.0, "low": 100.0, "close": 100.0}]  # seed close only
    for i in range(ATR_PERIOD):
        if i % 2 == 0:
            bars.append({"high": 104.0, "low": 100.0, "close": 100.0})  # TR = 4
        else:
            bars.append({"high": 102.0, "low": 100.0, "close": 100.0})  # TR = 2
    # 7 TRs of 4.0, 7 TRs of 2.0 -> mean = 3.0
    result = compute_atr(bars)
    assert result == 3.0
