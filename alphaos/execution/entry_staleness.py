"""Entry-order staleness watchdog (ENTRY-TTL-1).

Extends the existing fail-safe TTL philosophy (see ``alphaos/proposals/ttl.py``,
PR6) past the broker boundary: an unfilled entry order is automatically
cancelled when its thesis has aged out (TTL leg) or the market has moved
decisively past it (drift leg). Either trigger fires -> cancel. Cancellation
only ever *reduces* prospective exposure -- it never re-prices, never closes a
position, never touches the protective legs of a filled position.

This module holds the PURE decision logic only (mirrors ``ttl.py``'s own
precedent of pure functions with no journal/broker/side-effect access) --
unit-testable with an injected clock and no database. The side-effecting
cancel flow (broker call, journal writes, alert) lives in
``OrderManager._cancel_stale_entries`` / ``OrderManager._cancel_order_row``.

Fail-safe split, deliberate (spec 3.1): the TTL leg depends ONLY on time and
must work even when market data is unavailable/stale -- a data outage must
never let an order live forever. The drift leg is skipped, never guessed,
when no usable fresh price snapshot exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from alphaos.constants import TradeDirection
from alphaos.util import timeutils
from alphaos.util.market_calendar import trading_days_between


@dataclass(frozen=True)
class StalenessDecision:
    """The outcome of evaluating one unfilled entry order for staleness."""

    should_cancel: bool
    trigger: Optional[str]  # "ttl" | "drift" | None
    age_trading_days: Optional[int]
    drift_pct: Optional[float]

    def as_detail(self) -> dict:
        """The ``order_events``/``system_events`` audit detail payload (spec
        3.4: "a detail payload naming which trigger fired (ttl / drift), the
        age, and the drift %")."""
        return {
            "trigger": self.trigger,
            "age_trading_days": self.age_trading_days,
            "drift_pct": self.drift_pct,
        }


def trading_day_age(submitted_at: Optional[str], now: datetime) -> Optional[int]:
    """Trading days elapsed since ``submitted_at``, using the SAME
    ``trading_days_between()`` convention HOLD-1 established for
    ``PositionManager._check_exit`` / ``close_position`` (the day after
    submission is trading day 1; weekends/holidays never count). Both
    endpoints are converted to US-Eastern calendar dates first, matching
    every other trading-day computation in this codebase (one market-calendar
    module, one convention -- see ``trading-systems-reference``'s HOLD-1
    case study on why two subsystems counting "days" in different units is a
    real, previously-shipped bug class).

    None when ``submitted_at`` is missing/unparseable -- callers decide the
    fail-safe interpretation (see ``evaluate_ttl``)."""
    submitted_dt = timeutils.parse_iso(submitted_at) if submitted_at else None
    if submitted_dt is None:
        return None
    submitted_et_date = timeutils.to_et(submitted_dt).date()
    now_et_date = timeutils.to_et(now).date()
    return trading_days_between(submitted_et_date, now_et_date)


def evaluate_ttl(
    submitted_at: Optional[str], now: datetime, ttl_trading_days: int
) -> tuple[bool, Optional[int]]:
    """(fires, age_trading_days). ``ttl_trading_days <= 0`` deliberately
    disables this leg (never fires -- a valid "TTL leg off" configuration,
    not an error).

    A missing/unparseable ``submitted_at`` fails SAFE toward cancellation --
    mirrors ``proposals/ttl.py``'s own "an unrecognized/unparseable time
    context is always treated as the MORE conservative case" rule: an entry
    order whose own submission time cannot be established must not be
    allowed to sit at the broker forever just because of a data/journal
    defect. ``age_trading_days`` is reported as None in that case (the age
    itself is genuinely unknown, never fabricated as a number)."""
    if ttl_trading_days <= 0:
        return False, None
    age = trading_day_age(submitted_at, now)
    if age is None:
        return True, None
    return age >= ttl_trading_days, age


def evaluate_drift(
    direction: str,
    intended_entry_price: Optional[float],
    last_price: Optional[float],
    max_adverse_drift_pct: float,
) -> tuple[bool, Optional[float]]:
    """(fires, drift_pct). ``max_adverse_drift_pct <= 0`` deliberately
    disables this leg. Never guesses: a missing/non-positive
    ``intended_entry_price`` or a missing ``last_price`` means "not
    evaluable" (fires=False, drift_pct=None), not "fires" -- the caller is
    responsible for only passing a ``last_price`` that came from a
    freshness-guard-clean snapshot (spec 3.1: the drift leg is skipped,
    never guessed, when no usable fresh snapshot exists).

    Boundary is INCLUSIVE both directions (spec 3.1, pinned by test 2):
      long:  last_price >= intended_entry_price * (1 + X/100)
      short: last_price <= intended_entry_price * (1 - X/100)
    ``drift_pct`` is signed toward the adverse direction (positive = moved
    against the position) for both directions, so the audit detail payload
    reads the same way regardless of long/short."""
    if max_adverse_drift_pct <= 0:
        return False, None
    # Audit MINOR-3 hardening: a non-positive entry price is garbage data,
    # not a real order -- refuse to evaluate rather than produce a nonsense
    # percentage (upstream never writes one today; this is defense against
    # a corrupted/hand-edited row).
    if intended_entry_price is None or intended_entry_price <= 0 or last_price is None:
        return False, None
    if direction == TradeDirection.SHORT.value:
        drift_pct = (intended_entry_price - last_price) / intended_entry_price * 100.0
        fires = last_price <= intended_entry_price * (1 - max_adverse_drift_pct / 100.0)
    elif direction == TradeDirection.LONG.value:
        drift_pct = (last_price - intended_entry_price) / intended_entry_price * 100.0
        fires = last_price >= intended_entry_price * (1 + max_adverse_drift_pct / 100.0)
    else:
        # Audit MINOR-3: an unrecognized direction must never silently fall
        # through to the long branch -- not evaluable, never "fires".
        return False, None
    return fires, round(drift_pct, 4)


def evaluate(
    *,
    direction: str,
    submitted_at: Optional[str],
    intended_entry_price: Optional[float],
    now: datetime,
    last_price: Optional[float],
    price_usable: bool,
    ttl_trading_days: int,
    max_adverse_drift_pct: float,
) -> StalenessDecision:
    """Pure decision function for ONE unfilled entry order. No journal/broker
    access, no side effects -- the caller (``OrderManager._cancel_stale_entries``)
    owns fetching ``last_price``/``price_usable`` (a FreshnessGuard-assessed
    snapshot) and applying the resulting decision.

    Either trigger fires -> cancel (spec 3.1). TTL is evaluated first and, if
    it fires, is reported as the trigger even when drift ALSO would have
    fired that pass -- a deterministic tie-break (both are true statements
    about the order; TTL is picked because it is the leg with the fail-safe
    "must work with zero market data" guarantee, so it is the one that can
    always explain a cancel).  When usable, the drift percentage is still
    computed and carried in the detail payload even on a TTL-triggered
    cancel, purely for audit context -- it never changes ``trigger``."""
    ttl_fires, age = evaluate_ttl(submitted_at, now, ttl_trading_days)
    _, drift_pct = evaluate_drift(
        direction, intended_entry_price, last_price if price_usable else None, max_adverse_drift_pct
    )
    if ttl_fires:
        return StalenessDecision(True, "ttl", age, drift_pct)
    if price_usable:
        drift_fires, drift_pct = evaluate_drift(
            direction, intended_entry_price, last_price, max_adverse_drift_pct
        )
        if drift_fires:
            return StalenessDecision(True, "drift", age, drift_pct)
    return StalenessDecision(False, None, age, drift_pct)
