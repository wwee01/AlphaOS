"""INSTR-1 (part 1): alphaos.data.intraday_volume_curve -- deterministic,
direct-construction tests (explicit datetimes throughout, never wall-clock).
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from alphaos.data.intraday_volume_curve import (
    _CURVE_BREAKPOINTS,
    _SESSION_MINUTES,
    compute_rel_volume_v2,
    expected_cumulative_fraction,
    minutes_since_market_open,
)

_ET = ZoneInfo("America/New_York")


def _et(y, m, d, h, mi, s=0):
    return datetime(y, m, d, h, mi, s, tzinfo=_ET)


# --------------------------------------------------------- expected_cumulative_fraction
def test_fraction_is_none_before_the_open():
    assert expected_cumulative_fraction(-1.0) is None
    assert expected_cumulative_fraction(-0.001) is None


def test_fraction_is_zero_exactly_at_the_open():
    assert expected_cumulative_fraction(0.0) == 0.0


def test_fraction_is_one_at_and_after_the_close():
    assert expected_cumulative_fraction(_SESSION_MINUTES) == 1.0
    assert expected_cumulative_fraction(_SESSION_MINUTES + 60) == 1.0


def test_fraction_is_monotonically_non_decreasing_across_the_session():
    prev = 0.0
    m = 0.0
    while m <= _SESSION_MINUTES:
        f = expected_cumulative_fraction(m)
        assert f >= prev
        prev = f
        m += 5.0


def test_fraction_at_each_breakpoint_matches_the_table_exactly():
    for minutes, fraction in _CURVE_BREAKPOINTS:
        assert expected_cumulative_fraction(minutes) == fraction


def test_fraction_interpolates_linearly_between_breakpoints():
    # Breakpoints (0, 0.0) -> (30, 0.15): midpoint at 15 minutes -> 0.075.
    assert abs(expected_cumulative_fraction(15.0) - 0.075) < 1e-9


def test_fraction_reflects_the_u_shape_heavier_at_open_and_close():
    """The first 30 minutes and last 30 minutes should each carry a bigger
    slice of cumulative volume than a mid-session 30-minute window -- this
    is the whole point of curve-normalizing instead of a flat pro-rata."""
    open_slice = expected_cumulative_fraction(30.0) - expected_cumulative_fraction(0.0)
    midday_slice = expected_cumulative_fraction(210.0) - expected_cumulative_fraction(180.0)
    close_slice = expected_cumulative_fraction(390.0) - expected_cumulative_fraction(360.0)
    assert open_slice > midday_slice
    assert close_slice > midday_slice


# --------------------------------------------------------- minutes_since_market_open
def test_minutes_since_open_at_the_open_is_zero():
    assert minutes_since_market_open(_et(2026, 7, 9, 9, 30, 0)) == 0.0


def test_minutes_since_open_one_hour_in():
    assert minutes_since_market_open(_et(2026, 7, 9, 10, 30, 0)) == 60.0


def test_minutes_since_open_before_the_open_is_negative():
    assert minutes_since_market_open(_et(2026, 7, 9, 9, 0, 0)) == -30.0


def test_minutes_since_open_at_the_close():
    assert minutes_since_market_open(_et(2026, 7, 9, 16, 0, 0)) == _SESSION_MINUTES


def test_minutes_since_open_converts_from_utc():
    # 14:30 UTC == 10:30 ET during EDT (UTC-4) -- 60 minutes after the open.
    dt_utc = datetime(2026, 7, 9, 14, 30, 0, tzinfo=timezone.utc)
    assert minutes_since_market_open(dt_utc) == 60.0


def test_minutes_since_open_handles_naive_datetime_as_utc():
    dt_naive = datetime(2026, 7, 9, 14, 30, 0)  # no tzinfo
    assert minutes_since_market_open(dt_naive) == 60.0


# --------------------------------------------------------- compute_rel_volume_v2
def test_rel_volume_v2_none_when_volume_missing():
    assert compute_rel_volume_v2(None, 1_000_000, _et(2026, 7, 9, 10, 30)) is None


def test_rel_volume_v2_none_when_prev_volume_missing_or_zero():
    assert compute_rel_volume_v2(500_000, None, _et(2026, 7, 9, 10, 30)) is None
    assert compute_rel_volume_v2(500_000, 0, _et(2026, 7, 9, 10, 30)) is None


def test_rel_volume_v2_none_before_the_open():
    assert compute_rel_volume_v2(500_000, 1_000_000, _et(2026, 7, 9, 9, 0)) is None


def test_rel_volume_v2_known_value_one_hour_in():
    """Hand-verified: 60 minutes in, expected fraction = 0.21 (breakpoint
    table). volume=210_000, prev_day_volume=1_000_000 ->
    210_000 / (1_000_000 * 0.21) = 1.0 (exactly typical volume so far)."""
    result = compute_rel_volume_v2(210_000, 1_000_000, _et(2026, 7, 9, 10, 30))
    assert abs(result - 1.0) < 1e-9


def test_rel_volume_v2_reads_meaningfully_high_early_on_a_hot_day():
    """The whole point of the fix: on a day with genuinely unusual early
    volume, the v2 formula reads a real signal instead of the structurally
    tiny number the old cumulative-vs-full-day formula always produced.
    30 minutes in, double the typical early pace."""
    result = compute_rel_volume_v2(300_000, 1_000_000, _et(2026, 7, 9, 10, 0))
    # expected fraction at 30 min = 0.15 -> denominator = 150_000 -> 2.0x
    assert abs(result - 2.0) < 1e-9


def test_rel_volume_v2_full_day_after_close_is_plain_ratio():
    """After the close, the whole session has traded -- fraction clamps to
    1.0, so this degenerates to the old (correct, in THIS one case)
    full-day-vs-full-day comparison."""
    result = compute_rel_volume_v2(1_200_000, 1_000_000, _et(2026, 7, 9, 16, 30))
    assert abs(result - 1.2) < 1e-9


def test_rel_volume_v2_old_formula_would_have_read_far_lower_mid_morning():
    """Direct comparison against the OLD (broken) formula on identical
    inputs: at 10:00am (30 min in) with genuinely average volume so far
    (150_000, exactly the expected 15% slice of a 1M prev-day volume), the
    old cumulative/full-day formula reads 0.15 (looks dead), while v2
    correctly reads 1.0 (exactly typical)."""
    volume_so_far, prev_day_volume = 150_000, 1_000_000
    old_formula = volume_so_far / prev_day_volume
    new_formula = compute_rel_volume_v2(volume_so_far, prev_day_volume, _et(2026, 7, 9, 10, 0))
    assert abs(old_formula - 0.15) < 1e-9
    assert abs(new_formula - 1.0) < 1e-9
    assert new_formula > old_formula * 5  # the old number understated reality by 6.6x here
