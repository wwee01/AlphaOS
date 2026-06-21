"""Exit classification and the same-day-exit rule.

Every exit is classified into exactly one of the six categories. A same-day exit
(intended hold was 1-5 days but the position closes the same market date) is
allowed for the documented reasons; the reason determines the classification.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from alphaos.constants import ExitClassification
from alphaos.util import timeutils

# Map a free-form exit reason to one of the six required classifications.
_REASON_MAP = {
    "stop": ExitClassification.RISK_CONTROL,
    "stop_loss": ExitClassification.RISK_CONTROL,
    "risk_off": ExitClassification.RISK_CONTROL,
    "regime_shift": ExitClassification.RISK_CONTROL,
    "target": ExitClassification.PROFIT_TAKING,
    "take_profit": ExitClassification.PROFIT_TAKING,
    "profit": ExitClassification.PROFIT_TAKING,
    "thesis_invalidation": ExitClassification.THESIS_INVALIDATION,
    "thesis": ExitClassification.THESIS_INVALIDATION,
    "manual": ExitClassification.MANUAL_USER,
    "manual_user": ExitClassification.MANUAL_USER,
    "user": ExitClassification.MANUAL_USER,
    "daytrade": ExitClassification.EXPERIMENTAL_DAYTRADE,
    "experimental_daytrade": ExitClassification.EXPERIMENTAL_DAYTRADE,
    "data_quality": ExitClassification.ERROR_DATA_QUALITY,
    "error": ExitClassification.ERROR_DATA_QUALITY,
    "stale_data": ExitClassification.ERROR_DATA_QUALITY,
}


def classify_exit(reason: str, pnl: float = 0.0) -> ExitClassification:
    """Return the ExitClassification for an exit reason.

    Time-based expiry has no fixed category, so it is classified by outcome:
    profit-taking if non-negative, risk-control otherwise.
    """
    key = (reason or "").strip().lower()
    if key in _REASON_MAP:
        return _REASON_MAP[key]
    if key in ("time", "time_expiry", "time_stop", "expiry"):
        return ExitClassification.PROFIT_TAKING if pnl >= 0 else ExitClassification.RISK_CONTROL
    # Unknown reasons default to risk-control (conservative).
    return ExitClassification.RISK_CONTROL


def is_same_day_exit(opened_market_date: Optional[str], exit_dt=None) -> bool:
    """True if the position opened and closed on the same US-market date."""
    if not opened_market_date:
        return False
    exit_date = timeutils.market_date(exit_dt)
    try:
        opened = date.fromisoformat(opened_market_date)
    except (ValueError, TypeError):
        return False
    return opened == exit_date
