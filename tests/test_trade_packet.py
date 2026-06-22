"""Trade Packet v1 — end-to-end traceability + assembler.

These tests drive the real mock-mode pipeline (scan / seed_demo / monitor) and
assert the lifecycle is fully linked by the central ``trade_id`` correlation key,
that the assembler resolves a complete packet, and that no execution / risk /
freshness / AI-decision behavior or the real-money guard changed.
"""

from __future__ import annotations

from alphaos.ai.openai_client import OpenAIClient
from alphaos.constants import Decision, ReasonCode
from alphaos.reports.trade_packet import assemble_trade_packet
from conftest import make_settings


def _seed_and_exit_at_target(orch):
    """Seed a demo trade, then force a target exit via a monitor pass.

    Returns (proposal_row, position_row)."""
    d = orch.seed_demo()
    assert d["approved"], d["message"]
    prop = orch.journal.proposal_by_id(d["proposal_id"])
    pos = orch.journal.position_for_trade(prop["trade_id"])
    assert pos is not None
    # Push price beyond the target so the watchdog exits this pass.
    orch.run_monitor_once(price_overrides={pos["symbol"]: float(pos["target_price"]) + 1.0})
    return prop, pos


# --------------------------------------------------------------- lifecycle
def test_seed_demo_lifecycle_links(orchestrator):
    prop, pos = _seed_and_exit_at_target(orchestrator)
    j = orchestrator.journal

    # approval row links the proposal (seed_demo approves).
    appr = j.approval_for_proposal(prop["proposal_id"])
    assert appr is not None and appr["proposal_id"] == prop["proposal_id"]

    # approved proposal -> paper_order(s).
    orders = j.orders_for_proposal(prop["proposal_id"])
    assert orders, "approved proposal must have an entry order"

    # order -> fill, and fill links to the position.
    fills = j.fills_for_order(orders[0]["order_id"])
    assert fills, "filled order must have a fill"
    assert fills[0]["position_id"] == pos["position_id"]

    # position resolves from the entry order.
    pos_from_order = j.one(
        "SELECT * FROM positions WHERE order_id = ?", (orders[0]["order_id"],)
    )
    assert pos_from_order is not None
    assert pos_from_order["position_id"] == pos["position_id"]


def test_position_has_monitoring_snapshots(orchestrator):
    prop, pos = _seed_and_exit_at_target(orchestrator)
    snaps = orchestrator.journal.monitoring_snapshots_for_position(pos["position_id"])
    assert snaps, "a monitor pass must write at least one snapshot"
    assert snaps[-1]["trade_id"] == prop["trade_id"]
    # The exit pass must be flagged as an exit.
    assert any(s["action_taken"] == "exit_simulated" for s in snaps)


def test_exit_and_outcome_after_target(orchestrator):
    prop, pos = _seed_and_exit_at_target(orchestrator)
    j = orchestrator.journal
    exits = j.exits_for_position(pos["position_id"])
    assert exits, "a closed position must have an exit"
    outcome = j.outcome_for_position(pos["position_id"])
    assert outcome is not None
    assert outcome["exit_id"] == exits[-1]["exit_id"]
    assert outcome["outcome_classification"] in ("win", "loss", "breakeven")


def test_baseline_links_back_to_candidate(orchestrator):
    orchestrator.run_scan_once()
    c = orchestrator.journal.one("SELECT * FROM candidates ORDER BY id DESC LIMIT 1")
    base = orchestrator.journal.baseline_for_candidate(c["candidate_id"])
    assert base is not None
    assert base["candidate_id"] == c["candidate_id"]


def test_target_profile_default_on_proposal(orchestrator):
    prop, _ = _seed_and_exit_at_target(orchestrator)
    assert prop["target_profile"] == "configured_standard"


# ----------------------------------------------- the survival (trade_id) test
def test_trade_id_survives_the_whole_chain(orchestrator):
    prop, pos = _seed_and_exit_at_target(orchestrator)
    j = orchestrator.journal
    trade_id = prop["trade_id"]
    assert trade_id

    order = j.orders_for_proposal(prop["proposal_id"])[0]
    position = j.position_for_trade(trade_id)
    exit_row = j.exits_for_position(position["position_id"])[-1]
    outcome = j.outcome_for_trade(trade_id)

    assert order["trade_id"] == trade_id
    assert position["trade_id"] == trade_id
    assert exit_row["trade_id"] == trade_id
    assert outcome["trade_id"] == trade_id


# ----------------------------------------------------------- assembler test
def test_assemble_trade_packet_full_trade(orchestrator):
    prop, pos = _seed_and_exit_at_target(orchestrator)
    pkt = assemble_trade_packet(orchestrator.journal, candidate_id=prop["candidate_id"])
    ids = pkt["ids"]
    for key in ("candidate_id", "proposal_id", "trade_id", "position_id", "outcome_id"):
        assert ids[key], f"packet missing {key}"
    # The packet must be internally consistent on the trade_id.
    assert pkt["proposal"]["trade_id"] == ids["trade_id"]
    assert pkt["position"]["trade_id"] == ids["trade_id"]
    assert pkt["outcome"]["trade_id"] == ids["trade_id"]
    # Pure-read: collections present, missing links graceful.
    assert isinstance(pkt["orders"], list) and pkt["orders"]
    assert isinstance(pkt["monitoring_snapshots"], list) and pkt["monitoring_snapshots"]
    assert pkt["risk_check"] is not None


def test_assemble_trade_packet_by_trade_id(orchestrator):
    prop, _ = _seed_and_exit_at_target(orchestrator)
    pkt = assemble_trade_packet(orchestrator.journal, trade_id=prop["trade_id"])
    assert pkt["ids"]["candidate_id"] == prop["candidate_id"]
    assert pkt["ids"]["proposal_id"] == prop["proposal_id"]


def test_assemble_trade_packet_missing_anchor_is_graceful(orchestrator):
    pkt = assemble_trade_packet(orchestrator.journal, candidate_id="cand_does_not_exist")
    assert pkt["candidate"] is None
    assert pkt["orders"] == []
    assert pkt["position"] is None


# ----------------------------------------------- guard / safety invariants
def test_min_reward_risk_guard_still_blocks_low_rr():
    # Reuse the OpenAIClient guard pattern: a sub-floor reward:risk is rejected,
    # proving the AI-decision guard behavior is unchanged by this roadmap.
    eng = OpenAIClient(make_settings(TARGET_REWARD_RISK="1.0", MIN_REWARD_RISK="1.5"))
    ev = eng.evaluate({"symbol": "TEST", "direction": "long", "momentum_score": 0.8,
                       "candidate_id": "cand_x"}, {"last_price": 100.0})
    assert ev.decision == Decision.REJECT.value
    assert ReasonCode.REWARD_RISK_TOO_LOW.value in ev.risk_flags


def test_real_money_stays_unreachable(orchestrator):
    s = orchestrator.settings
    assert s.real_trading_enabled is False
    health = orchestrator.system_health()
    assert health["real_money_trading"] == "unreachable"
