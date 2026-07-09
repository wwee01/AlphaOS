"""INSTR-1 (part 1): time-of-day-normalized relative volume.

Replaces the structurally-dead "today's cumulative volume divided by
yesterday's FULL day volume" comparison -- reads 0.1-0.3 every morning by
construction, since a partial trading day is compared against a whole one
(exit review T3; the scanner's own core catalyst signal was silently
meaningless for the first half of every session) -- with a comparison
against what fraction of a NORMAL day's volume should typically have
already traded by this point in the session.

Per the operator/Fable design: NO NEW DATA PIPELINE. A true per-symbol
empirical curve would need 20 days of historical INTRADAY (minute-bar)
volume per symbol -- that was the original, deliberately-blocked design
(unclear cost/rate impact on the live data plan, a new caching/storage
decision, real edge cases like early closes). This module instead uses a
market-typical, generic intraday volume-shape curve, expressed as a single
versioned code constant -- exactly like REG-1's regime thresholds -- needing
no new data at all: only the previous FULL trading day's volume (already
fetched every scan; see ``alpaca_data.py``'s existing ``prevDailyBar``) and
the current time-of-day. This is a real, honest improvement over the
apples-to-oranges broken formula, not a claim of empirical per-symbol
precision -- see ``expected_cumulative_fraction()``'s own docstring.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from alphaos.util import timeutils

INTRADAY_VOLUME_CURVE_VERSION = "intraday_volume_curve_v1"

# US regular session: 9:30 -> 16:00 ET (390 minutes). Matches
# alphaos.util.timeutils's own _REG_OPEN/_REG_CLOSE constants.
_SESSION_OPEN_MINUTE = 9 * 60 + 30
_SESSION_MINUTES = 390.0

# (minutes since the open, cumulative fraction of the full day's volume
# TYPICALLY traded by that point) -- piecewise-linear breakpoints spanning
# the whole session. Reflects the well-documented U-shaped intraday volume
# profile (heavier at the open and close, lighter at midday) as a single
# MARKET-WIDE approximation -- not a per-symbol empirical fit. A future PR
# could replace this with a real empirical curve (per-symbol or per-sector)
# once that data exists (e.g. derived from TEXT-0/benchmark-spine-style
# archived history) without changing this module's shape, only its version.
_CURVE_BREAKPOINTS: list[tuple[float, float]] = [
    (0.0, 0.0),
    (30.0, 0.15),
    (60.0, 0.21),
    (90.0, 0.27),
    (120.0, 0.32),
    (150.0, 0.37),
    (180.0, 0.42),
    (210.0, 0.47),
    (240.0, 0.52),
    (270.0, 0.57),
    (300.0, 0.62),
    (330.0, 0.68),
    (360.0, 0.78),
    (390.0, 1.0),
]


def expected_cumulative_fraction(minutes_since_open: float) -> Optional[float]:
    """Piecewise-linear lookup on ``_CURVE_BREAKPOINTS``.

    ``None`` strictly before the open (no reading is meaningful pre-market --
    never fabricated). Clamped to ``1.0`` at/after the close (the whole
    session has traded, so a plain full-day-to-full-day comparison IS the
    correct one at that point -- the old formula was only wrong INTRADAY).
    """
    if minutes_since_open < 0:
        return None
    if minutes_since_open >= _SESSION_MINUTES:
        return 1.0
    for i in range(1, len(_CURVE_BREAKPOINTS)):
        x0, y0 = _CURVE_BREAKPOINTS[i - 1]
        x1, y1 = _CURVE_BREAKPOINTS[i]
        if minutes_since_open <= x1:
            if x1 == x0:
                return y1
            frac = (minutes_since_open - x0) / (x1 - x0)
            return y0 + frac * (y1 - y0)
    return 1.0  # pragma: no cover - unreachable, breakpoints span the full session


def minutes_since_market_open(dt: datetime) -> float:
    """Minutes elapsed since 9:30 ET on ``dt``'s own ET calendar day.
    Negative before the open; can exceed 390 after the close (the caller /
    ``expected_cumulative_fraction`` clamps as appropriate)."""
    et = timeutils.to_et(dt)
    return (et.hour * 60 + et.minute + et.second / 60.0) - _SESSION_OPEN_MINUTE


def compute_rel_volume_v2(
    volume_so_far: Optional[float],
    prev_day_full_volume: Optional[float],
    now: datetime,
) -> Optional[float]:
    """Cumulative-to-now volume, divided by (the previous FULL trading day's
    volume times the expected cumulative fraction by this time of day).

    Returns ``None`` (never a fabricated number) when: ``volume_so_far`` is
    missing, ``prev_day_full_volume`` is missing/non-positive, ``now`` is
    before the open (no reading yet), or the resulting denominator would be
    non-positive. Note the asymmetry is intentional: ``volume_so_far`` is
    cumulative traded volume (Alpaca's own ``dailyBar.v``), physically never
    negative, and ``0`` at/near the open is a genuinely valid reading (the
    session has barely started) -- correctly returns ``0.0``, not ``None``,
    unlike the old formula which treated ``0`` as falsy and silently
    produced ``None`` too (correctness-audit LOW-1).
    """
    if volume_so_far is None or prev_day_full_volume is None or prev_day_full_volume <= 0:
        return None
    fraction = expected_cumulative_fraction(minutes_since_market_open(now))
    if fraction is None or fraction <= 0:
        return None
    denominator = prev_day_full_volume * fraction
    if denominator <= 0:
        return None
    return volume_so_far / denominator
