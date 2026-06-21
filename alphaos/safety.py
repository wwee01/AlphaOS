"""Central runtime safety guards.

All of v1's hard "no" lives here so it is auditable in one file:

* ``assert_no_real_trading`` — orders are impossible unless REAL_TRADING_ENABLED
  is exactly 'false'. There is no v1 path that flips this on.
* ``KillSwitch`` — a file-backed, restart-surviving emergency stop. When engaged
  no new orders may be placed.

These guards are intentionally independent of the order manager so that no
single bug can quietly bypass them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from alphaos.config.settings import Settings
from alphaos.constants import REAL_TRADING_REQUIRED_VALUE, RuntimeMode


class RealTradingBlocked(Exception):
    """Raised if anything attempts to place a real-money order. Should be
    unreachable in v1, but we fail loudly rather than silently."""


class KillSwitchEngaged(Exception):
    """Raised when an order is attempted while the kill switch is engaged."""


@dataclass(frozen=True)
class SafetyVerdict:
    allowed: bool
    reason: str


def real_trading_guard(settings: Settings) -> SafetyVerdict:
    """Return whether real trading would be permitted. Always denies in v1."""
    if settings.real_trading_enabled_raw != REAL_TRADING_REQUIRED_VALUE:
        return SafetyVerdict(
            False,
            f"REAL_TRADING_ENABLED={settings.real_trading_enabled_raw!r} is not 'false'.",
        )
    # Even when the value is correct, v1 has no live broker path at all.
    return SafetyVerdict(True, "real trading disabled (paper/mock only in v1)")


def assert_paper_or_mock(settings: Settings) -> None:
    """Hard assertion that we are in a non-real mode. Live is unreachable."""
    if settings.mode not in (RuntimeMode.MOCK, RuntimeMode.PAPER):
        raise RealTradingBlocked(
            f"mode {settings.mode!r} is not an executable v1 mode (mock|paper only)."
        )


class KillSwitch:
    """File-backed emergency stop.

    The switch is a marker file. Its presence means 'engaged' (block all new
    orders). Using a file makes the state survive restarts and lets the
    dashboard, CLI, and any watchdog agree without a shared process.
    """

    def __init__(self, path: str = "data/KILL_SWITCH"):
        self.path = path

    def is_engaged(self) -> bool:
        return os.path.exists(self.path)

    def engage(self, reason: str = "manual") -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(reason)

    def release(self) -> None:
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass

    def reason(self) -> Optional[str]:
        if not self.is_engaged():
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return fh.read().strip() or "engaged"
        except OSError:  # pragma: no cover
            return "engaged"
