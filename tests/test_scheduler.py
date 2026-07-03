"""Scheduler v1.5 (PR3): cadence-layer wrappers around the existing scan/
monitor/outcomes orchestrator entry points, exercised hermetically (mock mode,
in-memory/temp-file SQLite, no network). Covers: no new order-submission path
is created, manual approval stays required, reconcile-then-watchdog ordering
is preserved, kill-switch/cost-cap gating only SKIPS (never bypasses a block),
job idempotency (duplicate-lock skip), failure visibility, restart-recovery
of protection incidents, and that real money stays unreachable via the
scheduler exactly as it is via the manual CLI path.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from alphaos.constants import ReasonCode
from alphaos.execution import protection_watchdog as pw
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.safety import KillSwitch
from alphaos.scheduler import JobRunner
from alphaos.scheduler.jobs import run_monitor_job, run_outcomes_job, run_scan_job
from alphaos.util.ids import new_id
from conftest import inject_pending_proposal, make_proposal, make_settings
from test_alpaca_paper_execution import FakeTradingClient, _paper_om, _seed_proposal
from test_protection_watchdog import _force_check_error, _open_protected_position


# --------------------------------------------------------------- scan safety
def test_scan_job_does_not_submit_orders():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)
    orch = Orchestrator(settings=s, journal=journal)
    orch.orders = om
    runner = JobRunner(orch)

    run_scan_job(orch, runner)

    assert fake.orders == {}  # scheduler-driven scan never reaches the broker


def test_scan_job_respects_manual_approval_boundary(orchestrator):
    # Inject the pending proposal BEFORE the scan runs, so the assertion is
    # meaningful: if the scan (or anything it triggers) auto-advanced a
    # proposal past manual approval, this pre-existing row would flip. (An
    # earlier version injected the proposal AFTER the scan, which made the
    # assertion tautological -- it would pass even if the scan auto-approved.)
    proposal_id, _ = inject_pending_proposal(orchestrator, symbol="AAPL")
    assert orchestrator.journal.proposal_by_id(proposal_id)["status"] == "pending_approval"

    runner = JobRunner(orchestrator)
    run_scan_job(orchestrator, runner)

    # The pre-existing proposal is untouched by the scan...
    assert orchestrator.journal.proposal_by_id(proposal_id)["status"] == "pending_approval"
    # ...and NOTHING anywhere reached approved/filled without an explicit
    # human approve_proposal() call (which this test never makes).
    advanced = orchestrator.journal.query(
        "SELECT proposal_id FROM trade_proposals WHERE status IN ('approved', 'filled')"
    )
    assert advanced == []


# ------------------------------------------------------------- monitor order
def test_monitor_job_calls_reconcile_then_watchdog_then_local_monitor_in_order(orchestrator, monkeypatch):
    calls = []

    original_reconcile = orchestrator.orders.reconcile
    original_monitor = orchestrator.positions.monitor

    def spy_reconcile(*args, **kwargs):
        calls.append("reconcile")
        return original_reconcile(*args, **kwargs)

    def spy_watchdog(*args, **kwargs):
        calls.append("run_watchdog_pass")
        return {"checked": 0, "protected": 0, "unprotected": 0, "degraded": 0,
                "closed_mismatch": 0, "check_error": 0, "unverifiable": 0,
                "qty_mismatches": 0, "dangling_orders": [], "new_incidents": []}

    def spy_monitor(*args, **kwargs):
        calls.append("monitor")
        return original_monitor(*args, **kwargs)

    monkeypatch.setattr(orchestrator.orders, "reconcile", spy_reconcile)
    monkeypatch.setattr("alphaos.orchestrator.protection_watchdog.run_watchdog_pass", spy_watchdog)
    monkeypatch.setattr(orchestrator.positions, "monitor", spy_monitor)

    runner = JobRunner(orchestrator)
    run_monitor_job(orchestrator, runner)

    assert calls == ["reconcile", "run_watchdog_pass", "monitor"]


# ------------------------------------------------ protection incident boundary
def test_protection_incident_blocks_new_entries_under_scheduler(orchestrator):
    from alphaos.util.ids import new_id

    orchestrator.journal.insert("protection_checks", {
        "check_id": new_id("pcheck"), "position_id": "pos_fake", "symbol": "META",
        "protection_status": "unprotected", "severity": "critical",
        "detail": "test-injected incident",
    })
    proposal_id, _ = inject_pending_proposal(orchestrator, symbol="AAPL")

    ok, msg = orchestrator.approve_proposal(proposal_id, approver="test")

    assert ok is False
    assert "protection incident" in msg


# ----------------------------------------------------------- outcomes safety
def test_outcomes_job_is_idempotent_under_scheduler(orchestrator):
    runner = JobRunner(orchestrator)
    run_scan_job(orchestrator, runner)  # seed some candidates to track

    result1 = run_outcomes_job(orchestrator, runner)
    count_after_first = orchestrator.journal.count_rows("candidate_outcomes")

    result2 = run_outcomes_job(orchestrator, runner)
    count_after_second = orchestrator.journal.count_rows("candidate_outcomes")

    assert result1["status"] == "completed"
    assert result2["status"] == "completed"
    assert result2["outcomes_result"]["seeded"] == {} or all(
        v == 0 for v in result2["outcomes_result"]["seeded"].values()
    )
    assert count_after_second == count_after_first  # no double-counting on rerun


# --------------------------------------------------------------- kill switch
def test_kill_switch_blocks_scan_but_allows_monitor_and_outcomes(orchestrator, tmp_path):
    ks_path = tmp_path / "KILL_SWITCH"
    orchestrator.kill_switch = KillSwitch(str(ks_path))
    orchestrator.orders.kill_switch = orchestrator.kill_switch
    orchestrator.kill_switch.engage("test")
    runner = JobRunner(orchestrator)

    proposals_before = orchestrator.journal.count_rows("trade_proposals")
    scan_result = run_scan_job(orchestrator, runner)
    proposals_after = orchestrator.journal.count_rows("trade_proposals")

    monitor_result = run_monitor_job(orchestrator, runner)
    outcomes_result = run_outcomes_job(orchestrator, runner)

    assert scan_result["status"] == "skipped"
    assert scan_result["kill_switch_engaged"] is True
    assert proposals_after == proposals_before  # no new trade_proposals row created

    assert monitor_result["status"] == "completed"
    assert outcomes_result["status"] == "completed"


# ------------------------------------------------------------- job failures
def test_job_failure_is_recorded_and_visible(orchestrator, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("simulated monitor failure")

    monkeypatch.setattr(orchestrator, "run_monitor_once", boom)
    runner = JobRunner(orchestrator)

    result = runner.run_job("monitor")  # must not raise

    assert result["status"] == "failed"
    row = orchestrator.journal.one(
        "SELECT * FROM job_runs WHERE job_type = 'monitor' ORDER BY id DESC LIMIT 1"
    )
    assert row["status"] == "failed"
    assert row["error"] is not None
    assert row["finished_at_utc"] is not None

    report = runner.status_report()
    failed_runs = [r for r in report["recent_by_job_type"]["monitor"] if r["status"] == "failed"]
    assert len(failed_runs) == 1


def test_unknown_job_type_fails_cleanly_without_orphaning_a_lock_row(orchestrator):
    runner = JobRunner(orchestrator)

    result = runner.run_job("not_a_real_job_type")  # must not raise

    assert result["status"] == "failed"
    assert "unknown job_type" in result["error"]
    # Must not have claimed a lock row it can never resolve to completed/failed.
    rows = orchestrator.journal.query(
        "SELECT * FROM job_runs WHERE job_type = 'not_a_real_job_type'"
    )
    assert rows == []


# --------------------------------------------------------------- duplicate lock
def test_duplicate_job_run_does_not_duplicate_dangerous_actions(orchestrator, monkeypatch):
    calls = []
    original = orchestrator.run_scan_once

    def spy(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(orchestrator, "run_scan_once", spy)
    runner = JobRunner(orchestrator)
    lock_key = "scan:fixed-test-lock-key"

    result1 = runner.run_job("scan", lock_key=lock_key)
    result2 = runner.run_job("scan", lock_key=lock_key)

    assert len(calls) == 1
    assert result1["status"] == "completed"
    assert result2["status"] == "skipped"
    assert result2["reason"] == "duplicate_lock"
    rows = orchestrator.journal.query("SELECT * FROM job_runs WHERE lock_key = ?", (lock_key,))
    assert len(rows) == 1


# ------------------------------------------------------- restart-recovery
def test_scheduler_restart_does_not_clear_protection_incidents():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = tmp.name
    try:
        from alphaos.util.ids import new_id

        s = make_settings(ALPHAOS_DB_PATH=db_path)

        # --- "session 1": open a protection incident + a job_runs row.
        journal1 = JournalStore(db_path)
        orch1 = Orchestrator(settings=s, journal=journal1)
        runner1 = JobRunner(orch1)
        journal1.insert("protection_checks", {
            "check_id": new_id("pcheck"), "position_id": "pos_fake", "symbol": "META",
            "protection_status": "unprotected", "severity": "critical",
            "detail": "test-injected incident (restart-recovery)",
        })
        runner1.run_job("outcomes_update")
        journal1.close()  # simulate process death

        # --- "session 2": brand-new connection, brand-new everything.
        journal2 = JournalStore(db_path)
        orch2 = Orchestrator(settings=s, journal=journal2)
        runner2 = JobRunner(orch2)

        report = runner2.status_report()

        assert report["protection_status"]["blocking"] is True
        incident = journal2.one(
            "SELECT * FROM protection_checks WHERE symbol = 'META' AND protection_status = 'unprotected' "
            "ORDER BY id DESC LIMIT 1"
        )
        assert incident is not None and incident["resolved_at_utc"] is None
        journal2.close()
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------- cost cap
def test_cost_cap_exceeded_is_visible_and_safe(orchestrator):
    from alphaos.util.ids import new_id

    orchestrator.settings = make_settings(SCHEDULER_AI_COST_CAP_CALLS_PER_30D=50)
    for _ in range(51):
        orchestrator.journal.insert("openai_evaluations", {
            "eval_id": new_id("eval"), "candidate_id": new_id("cand"), "symbol": "AAPL",
            "model": "mock", "direction": "long", "entry": 100.0, "stop": 97.0, "target": 106.0,
            "max_holding_days": 3, "expected_r": 2.0, "confidence": 0.8, "decision": "propose",
            "reasoning_summary": "test", "is_mock": 0,
        })
    runner = JobRunner(orchestrator)
    proposals_before = orchestrator.journal.count_rows("trade_proposals")

    result = runner.run_job("scan")

    assert result["status"] == "skipped"
    assert result["cost_cap_exceeded"] is True
    proposals_after = orchestrator.journal.count_rows("trade_proposals")
    assert proposals_after == proposals_before
    assert isinstance(result["reason"], str) and result["reason"]  # explains the skip

    # run_job's own job_runs row is the durable, queryable record of the skip
    # (cost_guard/jobs.py do not additionally log a system_events row for a
    # handled/expected skip -- only run_job's except-block does that for an
    # unexpected failure). Confirm the skip is visible via job_runs instead.
    row = orchestrator.journal.one(
        "SELECT * FROM job_runs WHERE job_type = 'scan' ORDER BY id DESC LIMIT 1"
    )
    assert row["cost_cap_exceeded"] == 1
    assert row["status"] == "skipped"


def test_cost_cap_check_fails_safe_on_a_db_error(orchestrator, monkeypatch):
    """check_scan_budget's own try/except must fail toward (False, ...) -- i.e.
    skip the scan -- if counting trailing-30-day OpenAI calls itself raises,
    rather than silently letting a DB hiccup be mistaken for "budget is fine"."""
    from alphaos.scheduler import cost_guard

    def boom(_journal):
        raise RuntimeError("simulated DB error while counting openai_evaluations")

    monkeypatch.setattr(cost_guard, "calls_in_last_30_days", boom)

    within_budget, detail = cost_guard.check_scan_budget(orchestrator.settings, orchestrator.journal)

    assert within_budget is False
    assert "error checking AI cost cap" in detail

    # And the same fail-safe behavior holds end-to-end through run_scan_job:
    # a raising cost check must SKIP the scan, never let it silently proceed.
    runner = JobRunner(orchestrator)
    proposals_before = orchestrator.journal.count_rows("trade_proposals")
    result = runner.run_job("scan")
    assert result["status"] == "skipped"
    assert result["cost_cap_exceeded"] is True
    assert orchestrator.journal.count_rows("trade_proposals") == proposals_before


# ------------------------------------------------------------ real-money scope
def test_real_money_remains_unreachable_via_scheduler(orchestrator):
    runner = JobRunner(orchestrator)
    for job_type in ("scan", "monitor", "outcomes_update", "daily_digest"):
        runner.run_job(job_type)

    assert orchestrator.settings.real_trading_enabled_raw == "false"

    import pathlib

    scheduler_dir = pathlib.Path(__file__).resolve().parents[1] / "alphaos" / "scheduler"
    for py_file in scheduler_dir.glob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("import alphaos.broker.alpaca_client") or \
                    stripped.startswith("from alphaos.broker.alpaca_client") or \
                    stripped.startswith("from alphaos.broker import alpaca_client"):
                assert False, f"{py_file.name} imports alpaca_client directly: {stripped!r}"
            assert "alpaca_client" not in stripped, f"{py_file.name}: {stripped!r}"


# --------------------------------------------------------- no live trading path
def test_no_live_trading_path_enabled_by_scheduler(orchestrator):
    mode_before = orchestrator.settings.mode
    approval_mode_before = orchestrator.settings.approval_mode

    runner = JobRunner(orchestrator)
    runner.run_due_jobs()

    assert orchestrator.settings.mode == mode_before
    assert orchestrator.settings.approval_mode == approval_mode_before

    approved_or_filled = orchestrator.journal.query(
        "SELECT * FROM trade_proposals WHERE status IN ('approved', 'filled')"
    )
    assert approved_or_filled == []  # no proposal reached approved/filled without an explicit approve_proposal() call


# ------------------------------------------- check_error escalation via scheduler
def test_check_error_escalation_accrues_across_scheduled_monitor_passes():
    """The consecutive broker-lookup-failure streak must accumulate to a
    blocking `unverifiable` incident when the watchdog is driven through the
    SCHEDULER's monitor job (run_monitor_job -> run_monitor_once -> watchdog),
    not only via a direct pw.run_watchdog_pass() call. This exercises the real
    reconcile-before-watchdog path per scheduled pass and proves the wrapper
    does not reset or short-circuit the escalation streak. (Specifically
    requested audit item; previously covered only via the direct watchdog
    path in test_protection_watchdog.py.)"""
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, PROTECTION_CHECK_ERROR_ESCALATION_THRESHOLD="3")
    _open_protected_position(fake, om, journal, "NVDA", 120.0, 110.0, 140.0, 3, max_holding_days=5)
    orch = Orchestrator(settings=s, journal=journal)
    orch.orders = om
    runner = JobRunner(orch)
    _force_check_error(om)  # every broker per-order lookup now fails

    # Three consecutive SCHEDULED monitor passes (via the job wrapper).
    r1 = run_monitor_job(orch, runner)
    r2 = run_monitor_job(orch, runner)
    r3 = run_monitor_job(orch, runner)

    assert r1["status"] == "completed" and r1["protection_blocking"] is False  # below threshold
    assert r2["protection_blocking"] is False
    assert r3["protection_blocking"] is True  # 3rd consecutive failure crosses the threshold via the scheduler path

    blocking = pw.has_blocking_incident(journal)
    assert blocking is not None
    assert blocking["protection_status"] == "unverifiable"
    assert blocking["severity"] == "critical"

    # And a new entry is now blocked at the execution gate (same proof style as
    # test_protection_watchdog.py's direct-path escalation test).
    new_prop = make_proposal(symbol="AAPL", entry=100.0, stop=97.0, target=106.0, qty=10)
    _seed_proposal(journal, new_prop)
    res = om.execute_proposal(new_prop)
    assert res.blocked is True
    assert res.block_reason == ReasonCode.PROTECTION_INTEGRITY_FAILURE.value
    assert not any(o.symbol == "AAPL" for o in fake.orders.values())  # never reached the broker


# ------------------------------------------------- failure-logging crash-proofness
def test_failure_logging_crash_does_not_lose_the_failure_record(orchestrator, monkeypatch):
    """If the best-effort audit log itself raises (e.g. the DB is momentarily
    locked by a concurrent writer) WHILE recording a job failure, run_job must
    still (a) not re-raise and (b) leave the durable job_runs failure record
    intact. Guards _log_failure_best_effort (job_runner.py)."""
    def boom_job(*args, **kwargs):
        raise RuntimeError("job blew up")

    def boom_log(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(orchestrator, "run_monitor_once", boom_job)
    monkeypatch.setattr(orchestrator.journal, "log_system_event", boom_log)
    runner = JobRunner(orchestrator)

    result = runner.run_job("monitor")  # must not raise despite BOTH the job AND the logger raising

    assert result["status"] == "failed"
    row = orchestrator.journal.one(
        "SELECT * FROM job_runs WHERE job_type = 'monitor' ORDER BY id DESC LIMIT 1"
    )
    assert row["status"] == "failed"  # durable failure record survived the logging crash
    assert row["error"] is not None
    assert row["finished_at_utc"] is not None


# ------------------------------------------------- cross-process lock race backstop
def test_partial_unique_index_blocks_two_active_locks_for_one_key(journal):
    """DB-level backstop for the check-then-insert race: two active
    (started/completed) job_runs rows for the same lock_key are impossible."""
    row = {
        "job_type": "scan", "lock_key": "scan:race-key",
        "started_at_utc": "2026-07-04T00:00:00+00:00",
        "started_at_sgt": "2026-07-04T08:00:00+08:00", "status": "started",
    }
    journal.insert("job_runs", {"job_run_id": new_id("jobrun"), **row})
    with pytest.raises(sqlite3.IntegrityError):
        journal.insert("job_runs", {"job_run_id": new_id("jobrun"), **row})


def test_acquire_converts_a_lost_race_to_false_not_an_exception(orchestrator, monkeypatch):
    """When two processes both pass acquire()'s SELECT before either INSERT
    commits, the loser's INSERT hits the partial unique index. acquire() must
    convert that sqlite3.IntegrityError into a clean False (== 'already
    locked'), never let it propagate."""
    runner = JobRunner(orchestrator)
    assert runner.acquire("monitor", "monitor:race-key") is True  # first caller wins, inserts 'started'

    # Simulate the TOCTOU: force the pre-INSERT SELECT to miss the existing row
    # so the second acquire proceeds to INSERT and hits the unique index.
    monkeypatch.setattr(orchestrator.journal, "one", lambda *args, **kwargs: None)
    assert runner.acquire("monitor", "monitor:race-key") is False  # IntegrityError -> False, no raise


# ------------------------------------------------------------- CLI invalid job
def test_cli_invalid_job_type_is_rejected_by_argparse():
    """scheduler_run_job restricts job_type via argparse choices, so an unknown
    job name fails at parse time (SystemExit) rather than reaching JobRunner."""
    from alphaos.__main__ import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["scheduler_run_job", "definitely_not_a_real_job"])
