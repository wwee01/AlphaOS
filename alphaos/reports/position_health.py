"""PR11 Portfolio Health engine: per-open-position thesis validity and a
human-facing verdict. Pure read -- never touches orders/execution, never
auto-exits anything (EXIT_REVIEW is a flag for a human, not an action).

v1 is deliberately deterministic, matching the spec's own framing: BROKEN
currently triggers ONLY on an open protection incident. There is no live
invalidation-condition detector yet -- that would mean re-running news/
catalyst enrichment against the CURRENT market state, which is explicitly
out of scope (see the specs doc's PR10 non-goals: "no LLM-derived
invalidation text"; PR11 inherits the same constraint). When that lands,
it becomes a second BROKEN trigger alongside the incident check.
"""

from __future__ import annotations

from typing import Optional

from alphaos.data.freshness_guard import FreshnessGuard
from alphaos.execution import protection_watchdog
from alphaos.execution.position_manager import PositionManager
from alphaos.util import timeutils
from alphaos.util.market_calendar import trading_days_between

THESIS_INTACT = "INTACT"
THESIS_AT_RISK = "AT_RISK"
THESIS_BROKEN = "BROKEN"

VERDICT_HOLD = "HOLD"
VERDICT_ATTENTION = "ATTENTION"
VERDICT_EXIT_REVIEW = "EXIT_REVIEW"

# A position at or below -0.5R (half its planned risk, unrealized) is
# AT_RISK even with no other signal -- named directly in the spec.
AT_RISK_R_THRESHOLD = -0.5

_VERDICT_BY_THESIS = {
    THESIS_BROKEN: VERDICT_EXIT_REVIEW,
    THESIS_AT_RISK: VERDICT_ATTENTION,
    THESIS_INTACT: VERDICT_HOLD,
}


def _days_held(pos: dict, now=None) -> Optional[float]:
    """Calendar age of the position. SECONDARY detail as of HOLD-1 -- see
    ``_trading_days_held`` for the PRIMARY number, which is what
    ``max_holding_days`` now means."""
    opened = timeutils.parse_iso(pos.get("opened_at"))
    if opened is None:
        return None
    return round(((now or timeutils.now_utc()) - opened).total_seconds() / 86400.0, 3)


def _trading_days_held(pos: dict, now=None) -> Optional[int]:
    """Trading days elapsed since entry -- the PRIMARY "days held" number as
    of HOLD-1, since ``max_holding_days`` now means trading days (matching
    the replay engine). Uses the exact same ``trading_days_between()``
    convention as ``PositionManager._check_exit`` -- and the same
    ``_opened_et_date`` entry-date resolution -- so this dashboard number and
    the live enforcement decision never disagree."""
    opened_et_date = PositionManager._opened_et_date(pos)
    if opened_et_date is None:
        return None
    now_et_date = timeutils.to_et(now or timeutils.now_utc()).date()
    return trading_days_between(opened_et_date, now_et_date)


def _r_at_price(pos: dict, price) -> Optional[float]:
    """R-multiple the position would show AT ``price`` -- reuses
    PositionManager._unrealized_r for both the live price (giving current_r)
    and the stop/target prices (giving stop_r/target_r), rather than
    reimplementing the long/short-aware math a second time."""
    if price is None:
        return None
    try:
        _, r = PositionManager._unrealized_r(pos, float(price))
    except (TypeError, ValueError):
        return None
    return r


def _earnings_within_hold_window(journal, pos: dict) -> bool:
    """Reads the PR5 earnings-proximity flag stamped on the proposal that
    produced this position (falls back to the candidate row if the
    proposal_id somehow isn't set -- e.g. an old/demo position)."""
    row = None
    if pos.get("proposal_id"):
        row = journal.one(
            "SELECT earnings_within_hold_window FROM trade_proposals WHERE proposal_id = ?",
            (pos["proposal_id"],),
        )
    if row is None and pos.get("candidate_id"):
        row = journal.one(
            "SELECT earnings_within_hold_window FROM candidates WHERE candidate_id = ?",
            (pos["candidate_id"],),
        )
    return bool(row and row.get("earnings_within_hold_window"))


def _thesis_status(has_open_incident: bool, earnings_within_hold_window: bool,
                    current_r: Optional[float], no_risk_basis: bool) -> str:
    """``no_risk_basis`` is True when a live price WAS available but current_r
    still came back None -- only possible when stop_price is missing or equal
    to entry (a degenerate/garbage risk_per_share). Without this, such a
    position would silently read INTACT forever, since it can never reach the
    R<=-0.5 branch -- "we can't compute this position's risk" must never be
    treated as "this position is fine" (the same unknown-never-zero posture
    every other measurement layer in this codebase follows)."""
    if has_open_incident:
        return THESIS_BROKEN
    if earnings_within_hold_window or no_risk_basis or (
        current_r is not None and current_r <= AT_RISK_R_THRESHOLD
    ):
        return THESIS_AT_RISK
    return THESIS_INTACT


def assess_positions(journal, settings, market) -> list[dict]:
    """One dict per OPEN position. Never raises: a single position that
    can't be assessed (bad snapshot, missing price data) reports itself with
    ``current_r=None`` and a WARNING-worthy ``freshness_status`` rather than
    aborting the whole sweep -- an operator seeing 9 of 10 positions is far
    better served than one seeing none."""
    positions = journal.open_positions()
    open_incidents = protection_watchdog.status_report(journal).get("open_incidents", [])
    incident_position_ids = {inc.get("position_id") for inc in open_incidents if inc.get("position_id")}
    freshness = FreshnessGuard.from_settings(settings)

    results = []
    for pos in positions:
        position_id = pos.get("position_id")
        symbol = pos.get("symbol")

        snap = None
        price = None
        freshness_status = "no_snapshot"
        try:
            snap = market.get_snapshot(symbol)
            if snap:
                price = snap.get("last_price")
                freshness_status = freshness.assess(snap).freshness_status
        except Exception:  # noqa: BLE001 - never let one bad symbol abort the sweep
            pass

        current_r = _r_at_price(pos, price)
        stop_r = _r_at_price(pos, pos.get("stop_price"))
        target_r = _r_at_price(pos, pos.get("target_price"))
        distance_to_stop_r = (
            round(current_r - stop_r, 4) if current_r is not None and stop_r is not None else None
        )
        distance_to_target_r = (
            round(target_r - current_r, 4) if current_r is not None and target_r is not None else None
        )

        has_open_incident = position_id in incident_position_ids
        earnings_flag = _earnings_within_hold_window(journal, pos)
        no_risk_basis = price is not None and current_r is None
        thesis_status = _thesis_status(has_open_incident, earnings_flag, current_r, no_risk_basis)
        verdict = _VERDICT_BY_THESIS[thesis_status]

        results.append({
            "position_id": position_id,
            "symbol": symbol,
            "direction": pos.get("direction"),
            "current_r": current_r,
            "distance_to_stop_r": distance_to_stop_r,
            "distance_to_target_r": distance_to_target_r,
            "thesis_status": thesis_status,
            "verdict": verdict,
            "ttl_status": "n/a (already executed)",
            "protection_status": pos.get("protection_status") or "unknown",
            "freshness_status": freshness_status,
            "days_held": _days_held(pos),
            "trading_days_held": _trading_days_held(pos),
            "max_holding_days": pos.get("max_holding_days"),
            "earnings_within_hold_window": earnings_flag,
        })
    return results
