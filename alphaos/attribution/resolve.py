"""Attribution v2 -- pure ΔR scenario formulas (PR8).

Attribution v2 measures decision-DIVERGENCE events in R units:

    delta_r = actual_path_r - alphaos_path_r

Positive delta_r means the deviation from AlphaOS's frozen path added value;
negative means AlphaOS's path would have been better. This is NOT a second
replay engine -- every function here consumes candidate_outcomes.replay_r /
trade_outcomes.realized_r exactly as the existing outcome ledger already
resolved them (alphaos/learning/outcomes_engine.py's replay_bracket()). One
replay engine, one truth.

UNKNOWN IS NEVER 0R. A 0.0 side appears in exactly two places, both directly
observed facts rather than assumptions: propose_user_rejected/propose_expired/
propose_blocked's actual_path_r (no position was opened -- the proposal's own
status already proves this), and user_override_trade's alphaos_path_r (AlphaOS's
own final decision was watch/reject -- discovery already established this via
alphaos_would_have_traded=0). Every other absence is a missing_reason + a
non-'resolved' status, never a fabricated number.

Everything in this module is a PURE function: no I/O, no DB access, no clock
reads, no RNG -- mirrors alphaos/tqs/scoring.py's compute_tqs() split (pure
compute here, DB reads in discovery.py/batch.py).
"""

from __future__ import annotations

from typing import Optional

from alphaos.constants import AttributionDataQuality, AttributionResolvedStatus

# ATTRIBUTION_VERSION: bump on ANY change to the ΔR formulas or resolution
# rules below. Rows are never retro-recomputed -- a version change only
# affects rows discovered/resolved going forward; existing rows keep the
# version they were computed under so cross-version comparison is never
# silently invalid (same rule as alphaos/tqs/scoring.py's TQS_VERSION).
ATTRIBUTION_VERSION = "2.0.0"

# candidate_outcomes.replay_result values that mean "a level was actually
# touched" (see alphaos/learning/outcomes_engine.py::replay_bracket()).
_REPLAY_TERMINAL_HIT = frozenset({"stop_hit", "target_hit"})


def _replay_state(co_row: Optional[dict]) -> dict:
    """Extract {replay_r, replay_status, resolved_status, missing_reason} from
    ONE candidate_outcomes row (or None if the measurement foundation hasn't
    seeded it yet). Pure -- applies no sign convention; callers combine
    replay_r with their own 0.0 side per the scenario's own formula.

    ``resolved_status`` here describes only whether THIS replay side is
    usable, not necessarily the whole attribution row's final status.
    Mirrors alphaos/learning/outcomes_tracker.py::update_pending_outcomes()'s
    own state machine exactly (pending/partial -> still pending; unavailable
    -> unresolvable; complete -> read replay_result) so a REPLAY side outcome
    is never second-guessed here, only relayed.
    """
    if co_row is None:
        return {"replay_r": None, "replay_status": None,
                "resolved_status": AttributionResolvedStatus.PENDING.value,
                "missing_reason": "candidate_outcome_not_yet_seeded"}

    status = co_row.get("outcome_status")
    if status in ("pending", "partial"):
        return {"replay_r": None, "replay_status": co_row.get("replay_result"),
                "resolved_status": AttributionResolvedStatus.PENDING.value, "missing_reason": None}
    if status == "unavailable":
        return {"replay_r": None, "replay_status": co_row.get("replay_result"),
                "resolved_status": AttributionResolvedStatus.UNRESOLVABLE.value,
                "missing_reason": "data_unavailable"}

    # status == "complete"
    replay_result = co_row.get("replay_result")
    replay_r = co_row.get("replay_r")
    if replay_result is None:
        # entry/stop/target were never all present -- replay_bracket() was
        # never even called for this row (see update_pending_outcomes: replay
        # only runs "if ref is not None and stop and target").
        return {"replay_r": None, "replay_status": None,
                "resolved_status": AttributionResolvedStatus.UNRESOLVABLE.value,
                "missing_reason": "invalid_levels"}
    if replay_result in _REPLAY_TERMINAL_HIT and replay_r is not None:
        return {"replay_r": replay_r, "replay_status": replay_result,
                "resolved_status": AttributionResolvedStatus.RESOLVED.value, "missing_reason": None}
    if replay_result == "ambiguous_same_bar":
        # Refuse-to-guess is the established, deliberate rule (outcomes_engine
        # daily-OHLC can't order same-day touches) -- inherit it, never assume
        # stop-first or target-first.
        return {"replay_r": None, "replay_status": replay_result,
                "resolved_status": AttributionResolvedStatus.UNRESOLVABLE.value,
                "missing_reason": "ambiguous_same_bar"}
    if replay_result == "neither":
        # Window exhausted with no level touched -- outcomes_engine's own
        # replay_r here is a mark-to-market fallback, which answers a DIFFERENT
        # question than "would the bracket have won". Do not substitute it.
        return {"replay_r": None, "replay_status": replay_result,
                "resolved_status": AttributionResolvedStatus.UNRESOLVABLE.value,
                "missing_reason": "window_exhausted_no_touch"}
    # replay_result == "unavailable" (no_levels_or_bars / no_risk_per_share / no_usable_bars)
    return {"replay_r": None, "replay_status": replay_result,
            "resolved_status": AttributionResolvedStatus.UNRESOLVABLE.value,
            "missing_reason": "invalid_levels"}


def resolve_zero_vs_replay(co_row: Optional[dict]) -> dict:
    """Shared formula for propose_user_rejected / propose_expired /
    propose_blocked: AlphaOS proposed a trade; no position was opened (a
    directly-observed 0.0 -- the proposal's own terminal status already
    proves this, independent of whether the replay resolves).

        alphaos_path_r = candidate_outcomes.replay_r (frozen proposal bracket)
        actual_path_r  = 0.0                          (always -- observed fact)
        delta_r        = 0.0 - alphaos_path_r

    Never uses forward mark-to-market R as a substitute for bracket replay
    (see _replay_state's 'neither' handling)."""
    rs = _replay_state(co_row)
    delta_r = round(0.0 - rs["replay_r"], 4) if rs["replay_r"] is not None else None
    return {
        "alphaos_path_r": rs["replay_r"],
        "actual_path_r": 0.0,
        "delta_r": delta_r,
        "execution_delta_r": None,
        "r_basis": "planned_frozen",
        "replay_status": rs["replay_status"],
        "resolved_status": rs["resolved_status"],
        "missing_reason": rs["missing_reason"],
    }


def resolve_user_override_trade(trade_outcome_row: Optional[dict],
                                override_co_row: Optional[dict]) -> dict:
    """user_override_trade: AlphaOS's path was no position (a directly-observed
    0.0 -- discovery already established alphaos_would_have_traded=0).

        alphaos_path_r = 0.0
        actual_path_r  = trade_outcomes.realized_r if a real trade closed,
                         else the override's own candidate_outcomes replay_r
        delta_r        = actual_path_r - 0.0

    'First resolution wins': once a real trade_outcomes row exists, it is
    authoritative even if the counterfactual replay resolved earlier or later
    -- but per the caller's own idempotency rule (batch.py never re-touches an
    already-RESOLVED row), whichever side resolves FIRST across successive
    calls is what the row keeps permanently."""
    if trade_outcome_row is not None and trade_outcome_row.get("realized_r") is not None:
        actual_r = trade_outcome_row["realized_r"]
        return {
            "alphaos_path_r": 0.0, "actual_path_r": actual_r,
            "delta_r": round(actual_r - 0.0, 4), "execution_delta_r": None,
            "r_basis": "realized_net", "replay_status": None,
            "resolved_status": AttributionResolvedStatus.RESOLVED.value, "missing_reason": None,
        }
    rs = _replay_state(override_co_row)
    delta_r = round(rs["replay_r"] - 0.0, 4) if rs["replay_r"] is not None else None
    return {
        "alphaos_path_r": 0.0,
        "actual_path_r": rs["replay_r"],
        "delta_r": delta_r,
        "execution_delta_r": None,
        "r_basis": "planned_frozen",
        "replay_status": rs["replay_status"],
        "resolved_status": rs["resolved_status"],
        "missing_reason": rs["missing_reason"],
    }


def resolve_propose_approved_executed(trade_outcome_row: Optional[dict],
                                      co_row: Optional[dict]) -> dict:
    """propose_approved_executed: both AlphaOS and the user agreed -- this
    measures the EXECUTION gap, never a decision divergence, so delta_r is
    ALWAYS None here.

        alphaos_path_r    = replay_r of the frozen proposal bracket
        actual_path_r     = trade_outcomes.realized_r
        execution_delta_r = realized_r - replay_r   (r_basis='net_vs_gross':
                             realized_r is net-of-estimated-costs, replay_r is
                             gross -- recorded, not "fixed")
        delta_r           = None always; agent='execution'

    If the trade hasn't closed yet: pending, regardless of replay state (this
    type's core question needs the executed side to exist at all). If the
    trade closed but the replay never resolves (still pending, or terminally
    unresolvable): store the known side, mark partial -- never invents the
    other side."""
    realized_r = trade_outcome_row.get("realized_r") if trade_outcome_row else None
    rs = _replay_state(co_row)
    replay_r = rs["replay_r"]

    if realized_r is None:
        return {
            "alphaos_path_r": replay_r, "actual_path_r": None, "delta_r": None,
            "execution_delta_r": None, "r_basis": None, "replay_status": rs["replay_status"],
            "resolved_status": AttributionResolvedStatus.PENDING.value,
            "missing_reason": "trade_not_yet_closed",
        }
    if replay_r is None:
        return {
            "alphaos_path_r": None, "actual_path_r": realized_r, "delta_r": None,
            "execution_delta_r": None, "r_basis": "realized_net", "replay_status": rs["replay_status"],
            "resolved_status": AttributionResolvedStatus.PARTIAL.value,
            "missing_reason": rs["missing_reason"] or "replay_pending",
        }
    return {
        "alphaos_path_r": replay_r, "actual_path_r": realized_r, "delta_r": None,
        "execution_delta_r": round(realized_r - replay_r, 4), "r_basis": "net_vs_gross",
        "replay_status": rs["replay_status"], "resolved_status": AttributionResolvedStatus.RESOLVED.value,
        "missing_reason": None,
    }


def compute_data_quality(is_mock: bool, resolved_status: str, degraded: bool = False) -> str:
    """Overall evidence-quality label for one attribution_records row -- same
    shape/precedence as alphaos/tqs/scoring.py's data_quality_status: 'mock'
    always wins (mock evidence must never look calibration-ready), then
    'unresolvable' when the row can never produce a delta, then 'degraded'
    for a known-but-imperfect signal (currently: missing legacy lineage),
    else 'ok'."""
    if is_mock:
        return AttributionDataQuality.MOCK.value
    if resolved_status == AttributionResolvedStatus.UNRESOLVABLE.value:
        return AttributionDataQuality.UNRESOLVABLE.value
    if degraded:
        return AttributionDataQuality.DEGRADED.value
    return AttributionDataQuality.OK.value
