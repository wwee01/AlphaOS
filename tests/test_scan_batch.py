"""Scan-batch + scheduler-run + risk-check persistence (Trade Packet v1).

Drives a real mock-mode scan via ``orch.run_scan_once()`` and asserts the new
audit/traceability rows are written without changing any trading behavior.
"""

from __future__ import annotations

from alphaos.constants import ReasonCode, RunStatus, SchedulerRunType, TriggerSource
from conftest import make_settings


def test_scan_once_creates_completed_scan_batch(orchestrator):
    summary = orchestrator.run_scan_once()
    assert summary.scan_batch_id is not None
    batch = orchestrator.journal.scan_batch_by_id(summary.scan_batch_id)
    assert batch is not None
    assert batch["status"] == RunStatus.COMPLETED.value
    assert batch["completed_at_utc"]
    assert batch["candidates_found"] == summary.candidates


def test_candidates_carry_scan_batch_id(orchestrator):
    summary = orchestrator.run_scan_once()
    cands = orchestrator.journal.query(
        "SELECT * FROM candidates WHERE scan_batch_id = ?", (summary.scan_batch_id,)
    )
    assert cands, "expected candidates linked to the scan batch"
    for c in cands:
        assert c["scan_batch_id"] == summary.scan_batch_id
        assert c["scan_id"] == summary.scan_batch_id  # scan_id == batch id
        assert c["playbook_name"]
        assert c["setup_classification"] == "momentum_continuation"


def test_candidate_to_evaluation_exists(orchestrator):
    orchestrator.run_scan_once()
    c = orchestrator.journal.one("SELECT * FROM candidates ORDER BY id DESC LIMIT 1")
    ev = orchestrator.journal.evaluation_for_candidate(c["candidate_id"])
    assert ev is not None
    assert ev["candidate_id"] == c["candidate_id"]


def test_proposal_links_candidate_and_eval(orchestrator):
    orchestrator.run_scan_once()
    prop = orchestrator.journal.one(
        "SELECT * FROM trade_proposals WHERE status='pending_approval' ORDER BY id DESC LIMIT 1"
    )
    assert prop is not None
    assert prop["candidate_id"]
    assert prop["eval_id"]
    ev = orchestrator.journal.evaluation_for_candidate(prop["candidate_id"])
    assert ev is not None and ev["eval_id"] == prop["eval_id"]


def test_risk_check_recorded_and_linked(orchestrator):
    orchestrator.run_scan_once()
    prop = orchestrator.journal.one(
        "SELECT * FROM trade_proposals WHERE status='pending_approval' ORDER BY id DESC LIMIT 1"
    )
    assert prop["risk_check_id"]
    rc = orchestrator.journal.risk_check_for_proposal(prop["proposal_id"])
    assert rc is not None
    assert rc["risk_check_id"] == prop["risk_check_id"]
    assert rc["trade_id"] == prop["trade_id"]
    assert rc["result"] in ("pass", "fail")


def test_configured_thresholds_persisted_on_risk_check(orchestrator):
    s = orchestrator.settings
    orchestrator.run_scan_once()
    rc = orchestrator.journal.one("SELECT * FROM risk_checks ORDER BY id DESC LIMIT 1")
    assert rc["stop_loss_pct"] == s.stop_loss_pct
    assert rc["target_reward_risk"] == s.target_reward_risk
    assert rc["min_reward_risk"] == s.min_reward_risk
    assert rc["target_profile"] == "configured_standard"


def test_target_profile_defaults_configured_standard(orchestrator):
    orchestrator.run_scan_once()
    prop = orchestrator.journal.one(
        "SELECT * FROM trade_proposals WHERE status='pending_approval' ORDER BY id DESC LIMIT 1"
    )
    assert prop["target_profile"] == "configured_standard"


def test_rejected_candidates_persist_with_reasons():
    # Force every proposal to be downgraded to a reject via an unreachable
    # min-reward-risk floor; each must persist a rejected_candidates row.
    s = make_settings(TARGET_REWARD_RISK="1.0", MIN_REWARD_RISK="5.0")
    from alphaos.journal.journal_store import JournalStore
    from alphaos.orchestrator import Orchestrator

    j = JournalStore(":memory:")
    orch = Orchestrator(settings=s, journal=j)
    try:
        summary = orch.run_scan_once()
        assert summary.rejected > 0
        rejs = j.query("SELECT * FROM rejected_candidates WHERE reason_code IS NOT NULL")
        assert rejs, "rejected candidates must persist with a reason_code"
        # The reward:risk floor downgrades the evaluation to an OpenAI reject.
        assert any(r["reason_code"] == ReasonCode.OPENAI_REJECT.value for r in rejs)
        assert all(r["stage"] == "openai" for r in rejs)
    finally:
        j.close()


def test_blocked_candidate_writes_rejection_and_blocked_proposal():
    # Force every proposal to be risk-blocked by capping open positions at 0.
    s = make_settings(MAX_OPEN_POSITIONS="0")
    from alphaos.journal.journal_store import JournalStore
    from alphaos.orchestrator import Orchestrator

    j = JournalStore(":memory:")
    orch = Orchestrator(settings=s, journal=j)
    try:
        orch.run_scan_once()
        blocked = j.query("SELECT * FROM trade_proposals WHERE status='blocked'")
        assert blocked, "expected at least one risk-blocked proposal"
        rejs = j.query(
            "SELECT * FROM rejected_candidates WHERE stage='risk' AND reason_code=?",
            (ReasonCode.TOO_MANY_POSITIONS.value,),
        )
        assert rejs, "risk-block must also write a rejection row"
    finally:
        j.close()


def test_scheduler_run_recorded_without_a_scheduler(orchestrator):
    summary = orchestrator.run_scan_once()
    runs = orchestrator.journal.recent_scheduler_runs(10)
    assert runs, "a scheduler_runs row must exist after a scan"
    run = orchestrator.journal.one(
        "SELECT * FROM scheduler_runs WHERE scheduler_run_id = ?", (summary.scheduler_run_id,)
    )
    assert run["run_type"] == SchedulerRunType.SCAN.value
    assert run["trigger_source"] == TriggerSource.MANUAL_CLI.value
    assert run["status"] == RunStatus.COMPLETED.value
    assert run["scan_batch_id"] == summary.scan_batch_id


def test_monitor_records_a_scheduler_run(orchestrator):
    orchestrator.seed_demo()
    res = orchestrator.run_monitor_once()
    run = orchestrator.journal.one(
        "SELECT * FROM scheduler_runs WHERE scheduler_run_id = ?", (res["scheduler_run_id"],)
    )
    assert run["run_type"] == SchedulerRunType.MONITOR.value
    assert run["status"] == RunStatus.COMPLETED.value
