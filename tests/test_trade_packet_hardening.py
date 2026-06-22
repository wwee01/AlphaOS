"""Hardening tests from the Trade Packet v1 adversarial review:

* a failing audit write (monitoring snapshot) must NEVER suppress a watchdog exit
  or abort the pass (the blocker the review found);
* the assembler must resolve the scan_batch / openai_evaluation / baseline
  branches (the demo-trade tests don't exercise these);
* trade_proposals.scan_batch_id round-trips (was silently dropped);
* manual approval backfills proposal.risk_check_id to the linked risk_check.
"""

from __future__ import annotations

from alphaos.orchestrator import Orchestrator
from alphaos.reports.trade_packet import assemble_trade_packet
from alphaos.strategy.proposal import TradeProposal
from conftest import make_settings


def _orch() -> Orchestrator:
    return Orchestrator(settings=make_settings(MAX_AUTO_APPROVALS_PER_DAY="5"))


def test_watchdog_exit_fires_even_if_snapshot_write_raises(monkeypatch):
    orch = _orch()
    try:
        orch.seed_demo()
        pos = orch.journal.one(
            "SELECT * FROM positions WHERE status='open' ORDER BY id DESC LIMIT 1"
        )
        assert pos is not None

        def boom(*a, **k):
            raise RuntimeError("simulated monitoring_snapshots write failure")

        monkeypatch.setattr(orch.positions, "_record_monitoring_snapshot", boom)
        # Price below the stop -> a stop exit is decided. The audit-write failure
        # must not pre-empt the exit nor raise out of the watchdog pass.
        res = orch.run_monitor_once(price_overrides={pos["symbol"]: float(pos["stop_price"]) - 1.0})
        assert orch.journal.count_open_positions() == 0, "audit failure suppressed a watchdog exit"
        assert res["exits"], "expected a stop exit"
    finally:
        orch.close()


def test_packet_resolves_scan_batch_eval_and_baseline_for_a_scan_candidate():
    orch = _orch()
    try:
        summ = orch.run_scan_once()
        cand = orch.journal.one(
            "SELECT * FROM candidates WHERE scan_batch_id IS NOT NULL ORDER BY id DESC LIMIT 1"
        )
        assert cand is not None, "scan produced no candidate carrying scan_batch_id"
        pkt = assemble_trade_packet(orch.journal, candidate_id=cand["candidate_id"])
        assert pkt["ids"]["scan_batch_id"] == summ.scan_batch_id
        assert pkt["ids"]["eval_id"] is not None
        assert pkt["ids"]["baseline_outcome_id"] is not None
        assert pkt["scan_batch"] is not None
        assert pkt["openai_evaluation"] is not None
        assert pkt["baseline"] is not None
    finally:
        orch.close()


def test_proposal_scan_batch_id_round_trips():
    p = TradeProposal(
        symbol="AAPL", direction="long", strategy="swing", entry=100.0, stop=97.0,
        target=104.5, max_holding_days=3, qty=10, risk_per_share=3.0, dollar_risk=30.0,
        expected_r=1.5, same_day_exit_eligible=True,
    )
    p.scan_batch_id = "scan_deadbeef0001"
    row = p.to_row()
    assert row["scan_batch_id"] == "scan_deadbeef0001"          # emitted by to_row
    assert TradeProposal.from_row({**row}).scan_batch_id == "scan_deadbeef0001"  # round-trips


def test_manual_approval_backfills_proposal_risk_check_id():
    orch = _orch()
    try:
        demo = orch.seed_demo()  # builds proposal then approves (manual re-check path)
        pid = demo["proposal_id"]
        prop = orch.journal.proposal_by_id(pid)
        rc = orch.journal.risk_check_for_proposal(pid)
        assert rc is not None
        assert prop["risk_check_id"] == rc["risk_check_id"]
    finally:
        orch.close()
