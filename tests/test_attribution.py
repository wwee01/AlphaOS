"""Attribution v2 pure ΔR formulas (PR8): scenario math, missing-data policy
(never fabricate a delta from an unresolved/unavailable/ambiguous replay),
and data-quality precedence. Hermetic -- pure functions, no journal/DB/
orchestrator involved."""

from __future__ import annotations

from alphaos.attribution.resolve import (
    ATTRIBUTION_VERSION,
    compute_data_quality,
    resolve_propose_approved_executed,
    resolve_user_override_trade,
    resolve_zero_vs_replay,
)
from alphaos.constants import AttributionDataQuality, AttributionResolvedStatus


def _co(status="complete", replay_result=None, replay_r=None, **extra) -> dict:
    row = {"outcome_status": status, "replay_result": replay_result, "replay_r": replay_r}
    row.update(extra)
    return row


# --------------------------------------------------------- resolve_zero_vs_replay
# Shared by propose_user_rejected / propose_expired / propose_blocked.

def test_zero_vs_replay_no_co_row_is_pending():
    r = resolve_zero_vs_replay(None)
    assert r["resolved_status"] == AttributionResolvedStatus.PENDING.value
    assert r["actual_path_r"] == 0.0  # always a directly-observed fact
    assert r["alphaos_path_r"] is None
    assert r["delta_r"] is None
    assert r["missing_reason"] == "candidate_outcome_not_yet_seeded"


def test_zero_vs_replay_stop_hit_gives_positive_delta():
    """The proposal WOULD have lost (stop_hit, replay_r=-1.0) -- not trading
    it (or the gate blocking it, or it expiring) ADDED value: delta_r > 0."""
    r = resolve_zero_vs_replay(_co(replay_result="stop_hit", replay_r=-1.0))
    assert r["resolved_status"] == AttributionResolvedStatus.RESOLVED.value
    assert r["alphaos_path_r"] == -1.0
    assert r["actual_path_r"] == 0.0
    assert r["delta_r"] == 1.0
    assert r["delta_r"] > 0


def test_zero_vs_replay_target_hit_gives_negative_delta():
    """The proposal WOULD have won (target_hit, replay_r=+2.5) -- not trading
    it COST value: delta_r < 0."""
    r = resolve_zero_vs_replay(_co(replay_result="target_hit", replay_r=2.5))
    assert r["resolved_status"] == AttributionResolvedStatus.RESOLVED.value
    assert r["alphaos_path_r"] == 2.5
    assert r["delta_r"] == -2.5
    assert r["delta_r"] < 0


def test_zero_vs_replay_formula_is_direction_agnostic():
    """The formula itself (0.0 - replay_r) does not re-derive direction --
    outcomes_engine.replay_bracket() already folds long/short into the sign
    of replay_r before this module ever sees it. A short-implied win (replay_r
    positive from a short's perspective) and a long-implied win produce the
    identical delta_r math given the same replay_r value."""
    long_win = resolve_zero_vs_replay(_co(replay_result="target_hit", replay_r=1.8))
    short_win = resolve_zero_vs_replay(_co(replay_result="target_hit", replay_r=1.8))
    assert long_win["delta_r"] == short_win["delta_r"] == -1.8


def test_zero_vs_replay_pending_co_status_stays_pending():
    r = resolve_zero_vs_replay(_co(status="pending"))
    assert r["resolved_status"] == AttributionResolvedStatus.PENDING.value
    assert r["delta_r"] is None
    assert r["actual_path_r"] == 0.0  # still a known fact even while pending


def test_zero_vs_replay_partial_co_status_stays_pending():
    r = resolve_zero_vs_replay(_co(status="partial"))
    assert r["resolved_status"] == AttributionResolvedStatus.PENDING.value
    assert r["delta_r"] is None


def test_zero_vs_replay_unavailable_co_status_is_unresolvable():
    r = resolve_zero_vs_replay(_co(status="unavailable"))
    assert r["resolved_status"] == AttributionResolvedStatus.UNRESOLVABLE.value
    assert r["delta_r"] is None
    assert r["missing_reason"] == "data_unavailable"


def test_zero_vs_replay_ambiguous_same_bar_is_unresolvable_not_fabricated():
    r = resolve_zero_vs_replay(_co(replay_result="ambiguous_same_bar", replay_r=None))
    assert r["resolved_status"] == AttributionResolvedStatus.UNRESOLVABLE.value
    assert r["delta_r"] is None
    assert r["alphaos_path_r"] is None
    assert r["missing_reason"] == "ambiguous_same_bar"


def test_zero_vs_replay_neither_never_uses_mark_to_market_substitute():
    """outcomes_engine's own 'neither' result carries a mark-to-market
    replay_r as a courtesy field -- attribution must NEVER read it as if it
    were a real bracket resolution."""
    r = resolve_zero_vs_replay(_co(replay_result="neither", replay_r=0.35))
    assert r["resolved_status"] == AttributionResolvedStatus.UNRESOLVABLE.value
    assert r["delta_r"] is None
    assert r["alphaos_path_r"] is None
    assert r["missing_reason"] == "window_exhausted_no_touch"


def test_zero_vs_replay_no_stop_invalid_levels_is_unresolvable():
    """replay_result is None -- entry/stop/target were never all present, so
    replay_bracket() was never even called (see update_pending_outcomes)."""
    r = resolve_zero_vs_replay(_co(status="complete", replay_result=None, replay_r=None))
    assert r["resolved_status"] == AttributionResolvedStatus.UNRESOLVABLE.value
    assert r["delta_r"] is None
    assert r["missing_reason"] == "invalid_levels"


def test_zero_vs_replay_engine_unavailable_result_is_invalid_levels():
    r = resolve_zero_vs_replay(_co(replay_result="unavailable", replay_r=None))
    assert r["resolved_status"] == AttributionResolvedStatus.UNRESOLVABLE.value
    assert r["missing_reason"] == "invalid_levels"


def test_zero_vs_replay_r_basis_always_planned_frozen():
    for co in (None, _co(replay_result="stop_hit", replay_r=-1.0), _co(status="unavailable")):
        assert resolve_zero_vs_replay(co)["r_basis"] == "planned_frozen"


# ------------------------------------------------------ resolve_user_override_trade

def test_user_override_trade_real_closed_trade_wins():
    r = resolve_user_override_trade({"realized_r": 1.6}, None)
    assert r["resolved_status"] == AttributionResolvedStatus.RESOLVED.value
    assert r["alphaos_path_r"] == 0.0
    assert r["actual_path_r"] == 1.6
    assert r["delta_r"] == 1.6
    assert r["r_basis"] == "realized_net"


def test_user_override_trade_real_closed_trade_loses():
    r = resolve_user_override_trade({"realized_r": -0.9}, None)
    assert r["delta_r"] == -0.9
    assert r["resolved_status"] == AttributionResolvedStatus.RESOLVED.value


def test_user_override_trade_falls_back_to_replay_when_no_real_trade():
    co = _co(replay_result="target_hit", replay_r=2.0)
    r = resolve_user_override_trade(None, co)
    assert r["resolved_status"] == AttributionResolvedStatus.RESOLVED.value
    assert r["actual_path_r"] == 2.0
    assert r["delta_r"] == 2.0
    assert r["r_basis"] == "planned_frozen"


def test_user_override_trade_falls_back_when_trade_outcome_present_but_r_is_none():
    """A trade_outcomes row can exist with realized_r not yet populated in some
    intermediate state -- treat that the same as 'no real trade yet'."""
    co = _co(replay_result="stop_hit", replay_r=-1.0)
    r = resolve_user_override_trade({"realized_r": None}, co)
    assert r["actual_path_r"] == -1.0
    assert r["r_basis"] == "planned_frozen"


def test_user_override_trade_pending_when_neither_side_resolved():
    r = resolve_user_override_trade(None, None)
    assert r["resolved_status"] == AttributionResolvedStatus.PENDING.value
    assert r["delta_r"] is None
    assert r["alphaos_path_r"] == 0.0  # AlphaOS's own side is always a known fact


def test_user_override_trade_unresolvable_when_replay_ambiguous_and_no_trade():
    co = _co(replay_result="ambiguous_same_bar", replay_r=None)
    r = resolve_user_override_trade(None, co)
    assert r["resolved_status"] == AttributionResolvedStatus.UNRESOLVABLE.value
    assert r["delta_r"] is None


def test_user_override_trade_execution_delta_r_always_none():
    assert resolve_user_override_trade({"realized_r": 1.0}, None)["execution_delta_r"] is None
    assert resolve_user_override_trade(None, _co(replay_result="stop_hit", replay_r=-1.0))["execution_delta_r"] is None


# -------------------------------------------------- resolve_propose_approved_executed

def test_propose_approved_executed_pending_when_trade_not_closed():
    co = _co(replay_result="target_hit", replay_r=1.5)  # replay already resolved
    r = resolve_propose_approved_executed(None, co)
    assert r["resolved_status"] == AttributionResolvedStatus.PENDING.value
    assert r["execution_delta_r"] is None
    assert r["delta_r"] is None
    assert r["missing_reason"] == "trade_not_yet_closed"


def test_propose_approved_executed_partial_when_closed_but_replay_pending():
    r = resolve_propose_approved_executed({"realized_r": 1.2}, _co(status="pending"))
    assert r["resolved_status"] == AttributionResolvedStatus.PARTIAL.value
    assert r["actual_path_r"] == 1.2
    assert r["alphaos_path_r"] is None
    assert r["execution_delta_r"] is None
    assert r["r_basis"] == "realized_net"


def test_propose_approved_executed_partial_when_closed_but_replay_permanently_unresolvable():
    r = resolve_propose_approved_executed({"realized_r": 0.8}, _co(replay_result="ambiguous_same_bar", replay_r=None))
    assert r["resolved_status"] == AttributionResolvedStatus.PARTIAL.value
    assert r["actual_path_r"] == 0.8
    assert r["execution_delta_r"] is None


def test_propose_approved_executed_resolved_computes_execution_delta():
    r = resolve_propose_approved_executed({"realized_r": 1.1}, _co(replay_result="target_hit", replay_r=1.5))
    assert r["resolved_status"] == AttributionResolvedStatus.RESOLVED.value
    assert r["alphaos_path_r"] == 1.5
    assert r["actual_path_r"] == 1.1
    assert r["execution_delta_r"] == round(1.1 - 1.5, 4)
    assert r["r_basis"] == "net_vs_gross"


def test_propose_approved_executed_delta_r_is_always_none():
    """This type measures execution, never decision divergence."""
    cases = [
        resolve_propose_approved_executed(None, None),
        resolve_propose_approved_executed({"realized_r": 1.0}, None),
        resolve_propose_approved_executed({"realized_r": 1.0}, _co(replay_result="target_hit", replay_r=1.0)),
    ]
    assert all(c["delta_r"] is None for c in cases)


# ---------------------------------------------------------------- data quality

def test_compute_data_quality_mock_takes_precedence_over_everything():
    assert compute_data_quality(is_mock=True, resolved_status="resolved") == AttributionDataQuality.MOCK.value
    assert compute_data_quality(is_mock=True, resolved_status="unresolvable", degraded=True) == \
        AttributionDataQuality.MOCK.value


def test_compute_data_quality_unresolvable():
    assert compute_data_quality(is_mock=False, resolved_status="unresolvable") == \
        AttributionDataQuality.UNRESOLVABLE.value


def test_compute_data_quality_degraded():
    assert compute_data_quality(is_mock=False, resolved_status="resolved", degraded=True) == \
        AttributionDataQuality.DEGRADED.value


def test_compute_data_quality_ok():
    assert compute_data_quality(is_mock=False, resolved_status="resolved") == AttributionDataQuality.OK.value
    assert compute_data_quality(is_mock=False, resolved_status="partial") == AttributionDataQuality.OK.value


def test_attribution_version_is_a_stable_constant():
    assert ATTRIBUTION_VERSION == "2.0.0"
    assert isinstance(ATTRIBUTION_VERSION, str)
