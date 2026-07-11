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
import pathlib
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from alphaos.constants import ReasonCode
from alphaos.execution import protection_watchdog as pw
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.safety import KillSwitch
from alphaos.scheduler import JobRunner, cadence
from alphaos.scheduler.jobs import run_monitor_job, run_outcomes_job, run_scan_job
from alphaos.util import alerts, timeutils
from alphaos.util.ids import new_id
from conftest import inject_pending_proposal, make_proposal, make_settings
from test_alpaca_paper_execution import FakeTradingClient, _paper_om, _seed_proposal
from test_protection_watchdog import _force_check_error, _open_protected_position


def _insert_job_run(journal, job_type, status, lock_key=None, finished_at_utc=None):
    """PR9 test helper: directly construct a terminal job_runs row with a
    known status/timestamp -- never depend on what a live run happens to
    produce (the date-seeded-mock-data lesson applies just as much to
    job_runs history as to price data)."""
    st = timeutils.stamp()
    row = {
        "job_run_id": new_id("jobrun"),
        "job_type": job_type,
        "trigger_source": "scheduler",
        "lock_key": lock_key or new_id(f"{job_type}-lock"),
        "started_at_utc": st.utc,
        "started_at_sgt": st.local_sgt,
        "status": status,
    }
    if status != "started":
        row["finished_at_utc"] = finished_at_utc or st.utc
        row["finished_at_sgt"] = st.local_sgt
    journal.insert("job_runs", row)


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
    #
    # Symbol is deliberately OUTSIDE the scanner's fixed DEFAULT_UNIVERSE (see
    # candidate_scanner.py) so the mock scan can never independently mint its
    # own competing proposal for it on any date. "AAPL" (which IS in the
    # universe) intermittently collided with the mock scan's own date-seeded
    # AAPL proposal, tripping PR6's same-symbol supersession policy and
    # failing this test on some but not all dates.
    proposal_id, _ = inject_pending_proposal(orchestrator, symbol="ZTEST")
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


def test_cli_scheduler_health_is_registered():
    from alphaos.__main__ import build_parser

    args = build_parser().parse_args(["scheduler_health"])
    assert args.command == "scheduler_health"


# =============================================================================
# PR9: self-halt fuse (cadence.is_fused pure predicate)
# =============================================================================
def test_is_fused_pure_function_trips_after_threshold_consecutive_failures(journal):
    for _ in range(3):
        _insert_job_run(journal, "scan", "failed")

    fused, reason, streak = cadence.is_fused("scan", 3, journal)

    assert fused is True
    assert streak == 3
    assert "3 consecutive failed scan runs" in reason


def test_is_fused_pure_function_not_fused_below_threshold(journal):
    for _ in range(2):
        _insert_job_run(journal, "scan", "failed")

    fused, reason, streak = cadence.is_fused("scan", 3, journal)

    assert fused is False
    assert streak == 2


def test_is_fused_streak_is_broken_by_an_older_completed_row(journal):
    """Rows are inserted oldest-first: completed, failed, failed. The trailing
    2 failures don't reach threshold=3 because the streak stops counting the
    moment it hits the completed row -- older history beyond a success must
    never count toward the fuse."""
    _insert_job_run(journal, "scan", "completed")
    _insert_job_run(journal, "scan", "failed")
    _insert_job_run(journal, "scan", "failed")

    fused, reason, streak = cadence.is_fused("scan", 3, journal)

    assert fused is False
    assert streak == 2


def test_is_fused_streak_is_broken_by_a_skipped_row():
    """A kill-switch/cost-cap 'skipped' row is an expected state, not a
    failure -- it must break the streak exactly like a completed row."""
    journal = JournalStore(":memory:")
    _insert_job_run(journal, "scan", "failed")
    _insert_job_run(journal, "scan", "failed")
    _insert_job_run(journal, "scan", "skipped")
    _insert_job_run(journal, "scan", "failed")

    fused, reason, streak = cadence.is_fused("scan", 3, journal)

    assert fused is False
    assert streak == 1
    journal.close()


def test_is_fused_ignores_in_flight_started_rows(journal):
    """An in-flight 'started' row (e.g. a stale/crashed process) must not be
    read as part of the terminal failure streak."""
    for _ in range(3):
        _insert_job_run(journal, "scan", "failed")
    _insert_job_run(journal, "scan", "started")  # most recent, but never terminal

    fused, reason, streak = cadence.is_fused("scan", 3, journal)

    assert fused is True  # the 3 failed rows underneath still count
    assert streak == 3


def test_is_fused_job_types_are_independent(journal):
    for _ in range(3):
        _insert_job_run(journal, "scan", "failed")

    scan_fused, _, _ = cadence.is_fused("scan", 3, journal)
    monitor_fused, _, monitor_streak = cadence.is_fused("monitor", 3, journal)

    assert scan_fused is True
    assert monitor_fused is False
    assert monitor_streak == 0


def test_is_fused_fails_safe_toward_not_fused_on_a_db_error(journal, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("simulated DB error while reading job_runs")

    monkeypatch.setattr(journal, "query", boom)

    fused, reason, streak = cadence.is_fused("scan", 3, journal)

    assert fused is False
    assert "error checking fuse state" in reason
    assert streak == 0


# =============================================================================
# PR9: self-halt fuse end-to-end (JobRunner.run_due_jobs dispatch skip)
# =============================================================================
def test_run_due_jobs_skips_a_fused_job_type_without_a_new_job_runs_row(orchestrator):
    # monitor has no prior COMPLETED row, so cadence._interval_due reports it
    # due regardless of wall-clock -- exactly like a brand-new deployment.
    for _ in range(3):
        _insert_job_run(orchestrator.journal, "monitor", "failed")
    rows_before = orchestrator.journal.count_rows("job_runs", "job_type = 'monitor'")

    results = JobRunner(orchestrator).run_due_jobs()
    by_type = {r["job_type"]: r for r in results}

    assert by_type["monitor"]["status"] == "fused"
    assert "3 consecutive failed monitor runs" in by_type["monitor"]["reason"]
    rows_after = orchestrator.journal.count_rows("job_runs", "job_type = 'monitor'")
    assert rows_after == rows_before  # fused dispatch never claims a new lock row


def test_run_due_jobs_logs_scheduler_fused_system_event(orchestrator):
    for _ in range(3):
        _insert_job_run(orchestrator.journal, "monitor", "failed")

    JobRunner(orchestrator).run_due_jobs()

    row = orchestrator.journal.one(
        "SELECT * FROM system_events WHERE category = 'scheduler_fused' ORDER BY id DESC LIMIT 1"
    )
    assert row is not None
    assert row["severity"] == "error"
    assert "monitor" in row["message"]


def test_fuse_alert_and_log_are_deduped_across_two_consecutive_fused_ticks(orchestrator, monkeypatch):
    calls = []
    monkeypatch.setattr(alerts, "send_alert", lambda *a, **k: calls.append((a, k)) or True)
    # Force ONLY 'monitor' due so this alert-count assertion measures the fuse
    # alert alone -- PR11's daily_digest job now sends its own brief alert
    # whenever IT is due, and its real cadence (a wall-clock time-of-day check)
    # would otherwise leak a second, unrelated alert into this count depending
    # on when the suite happens to run (the §H.1 "never depend on organic
    # cadence" flake class -- this bit the merged-main run).
    monkeypatch.setattr(
        cadence, "is_due",
        lambda job_type, settings, journal, now=None: (
            (True, "forced for test") if job_type == cadence.JobType.MONITOR.value else (False, "not due")
        ),
    )

    for _ in range(3):
        _insert_job_run(orchestrator.journal, "monitor", "failed")

    runner = JobRunner(orchestrator)
    runner.run_due_jobs()
    runner.run_due_jobs()  # second consecutive fused tick -- must not re-alert

    assert len(calls) == 1
    fused_events = orchestrator.journal.count_rows("system_events", "category = 'scheduler_fused'")
    assert fused_events == 1


def test_fuse_dedupe_survives_a_null_finished_at_utc_on_the_last_completed_row(orchestrator, monkeypatch):
    """Audit finding (LOW): a 'completed' job_runs row with a NULL
    finished_at_utc must not silently defeat the dedupe watermark (binding
    SQL NULL into 'created_at_utc >= ?' never matches, which would otherwise
    make _handle_fuse re-alert on every single tick instead of once)."""
    calls = []
    monkeypatch.setattr(alerts, "send_alert", lambda *a, **k: calls.append((a, k)) or True)
    # Force ONLY 'monitor' due -- same reason as the sibling dedupe test above:
    # keep PR11's daily_digest brief alert out of this fuse-alert count.
    monkeypatch.setattr(
        cadence, "is_due",
        lambda job_type, settings, journal, now=None: (
            (True, "forced for test") if job_type == cadence.JobType.MONITOR.value else (False, "not due")
        ),
    )
    # A completed row with an explicit NULL finished_at_utc -- schema-legal
    # (the column is nullable), even though no current writer produces it.
    orchestrator.journal.insert("job_runs", {
        "job_run_id": new_id("jobrun"), "job_type": "monitor", "trigger_source": "scheduler",
        "lock_key": new_id("monitor-lock"), "started_at_utc": "2026-01-01T00:00:00+00:00",
        "started_at_sgt": "2026-01-01T08:00:00+08:00", "status": "completed",
        "finished_at_utc": None, "finished_at_sgt": None,
    })

    for _ in range(3):
        _insert_job_run(orchestrator.journal, "monitor", "failed")

    runner = JobRunner(orchestrator)
    runner.run_due_jobs()
    runner.run_due_jobs()  # second consecutive fused tick -- must still dedupe

    assert len(calls) == 1
    fused_events = orchestrator.journal.count_rows("system_events", "category = 'scheduler_fused'")
    assert fused_events == 1


def test_fuse_clears_after_a_manual_successful_run(orchestrator, monkeypatch):
    # Force "due" throughout -- isolates fuse-clear behavior from monitor's
    # own interval cadence (a monitor that JUST completed is legitimately
    # "not due" again for scheduler_monitor_interval_minutes, which is a
    # separate concern from whether the fuse itself is cleared).
    monkeypatch.setattr(cadence, "is_due", lambda job_type, settings, journal, now=None: (True, "forced for test"))
    for _ in range(3):
        _insert_job_run(orchestrator.journal, "monitor", "failed")
    runner = JobRunner(orchestrator)
    fused_before, _, _ = cadence.is_fused("monitor", orchestrator.settings.scheduler_max_consecutive_failures, orchestrator.journal)
    assert fused_before is True

    # A human running `scheduler_run_job monitor` calls run_job() DIRECTLY,
    # bypassing run_due_jobs' fuse check entirely -- exactly like the CLI path.
    result = runner.run_job("monitor")
    assert result["status"] == "completed"

    fused_after, _, streak_after = cadence.is_fused(
        "monitor", orchestrator.settings.scheduler_max_consecutive_failures, orchestrator.journal
    )
    assert fused_after is False
    assert streak_after == 0

    # And run_due_jobs no longer reports it fused (is_due is forced True, so
    # this genuinely exercises the fuse re-check, not just cadence timing).
    results = runner.run_due_jobs()
    by_type = {r["job_type"]: r for r in results}
    assert by_type["monitor"]["status"] != "fused"


def test_fuse_re_alerts_on_a_genuinely_new_episode_after_clearing(orchestrator, monkeypatch):
    """Two SEPARATE fuse episodes (cleared by a manual success in between)
    must each alert once -- the dedupe watermark must advance, not permanently
    suppress every future episode for the same job_type.

    Scoped to ONLY 'monitor' being due (not a blanket force-everything-due
    like sibling tests use) -- PR11's daily_digest job now sends its own
    unrelated alert whenever it runs, which would otherwise inflate this
    test's exact alert-count assertions with a call that has nothing to do
    with the fuse behavior actually under test."""
    monkeypatch.setattr(
        cadence, "is_due",
        lambda job_type, settings, journal, now=None: (
            (True, "forced for test") if job_type == cadence.JobType.MONITOR.value else (False, "not due")
        ),
    )
    calls = []
    monkeypatch.setattr(alerts, "send_alert", lambda *a, **k: calls.append((a, k)) or True)
    runner = JobRunner(orchestrator)

    for _ in range(3):
        _insert_job_run(orchestrator.journal, "monitor", "failed")
    runner.run_due_jobs()
    assert len(calls) == 1

    runner.run_job("monitor")  # manual success clears the fuse

    for _ in range(3):
        _insert_job_run(orchestrator.journal, "monitor", "failed")
    runner.run_due_jobs()

    assert len(calls) == 2  # the second, genuinely new episode re-alerted


# =============================================================================
# PR9: job-failure alert (JobRunner.run_job -> alerts.send_alert)
# =============================================================================
def test_job_failure_sends_a_high_priority_alert(orchestrator, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("simulated monitor failure")

    monkeypatch.setattr(orchestrator, "run_monitor_once", boom)
    calls = []
    monkeypatch.setattr(alerts, "send_alert", lambda *a, **k: calls.append((a, k)) or True)
    runner = JobRunner(orchestrator)

    result = runner.run_job("monitor")

    assert result["status"] == "failed"
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert kwargs.get("priority") == "high"
    assert "monitor" in kwargs.get("title", "")
    assert "simulated monitor failure" in kwargs.get("message", "")


def test_job_failure_alert_crashing_never_breaks_run_job(orchestrator, monkeypatch):
    """Suspenders: even if alerts.send_alert itself raises unexpectedly,
    run_job must still return its failure result cleanly."""
    def boom_job(*args, **kwargs):
        raise RuntimeError("job blew up")

    def boom_alert(*args, **kwargs):
        raise RuntimeError("ntfy client library exploded")

    monkeypatch.setattr(orchestrator, "run_monitor_once", boom_job)
    monkeypatch.setattr(alerts, "send_alert", boom_alert)
    runner = JobRunner(orchestrator)

    result = runner.run_job("monitor")  # must not raise

    assert result["status"] == "failed"
    row = orchestrator.journal.one(
        "SELECT * FROM job_runs WHERE job_type = 'monitor' ORDER BY id DESC LIMIT 1"
    )
    assert row["status"] == "failed"


def test_a_skipped_job_never_sends_a_failure_alert(orchestrator, tmp_path, monkeypatch):
    """Kill-switch/cost-cap skips are expected state, never a failure -- see
    9.2.5 ('When the kill switch blocks a scan tick: NO alert')."""
    calls = []
    monkeypatch.setattr(alerts, "send_alert", lambda *a, **k: calls.append((a, k)) or True)
    ks_path = tmp_path / "KILL_SWITCH"
    orchestrator.kill_switch = KillSwitch(str(ks_path))
    orchestrator.orders.kill_switch = orchestrator.kill_switch
    orchestrator.kill_switch.engage("test")
    runner = JobRunner(orchestrator)

    result = runner.run_job("scan")

    assert result["status"] == "skipped"
    assert calls == []


# =============================================================================
# PR9: kill-switch-at-job-entry regression via run_due_jobs (spec 9.2.6)
# =============================================================================
def test_kill_switch_blocks_scan_via_run_due_jobs_but_monitor_and_outcomes_still_run(
    orchestrator, tmp_path, monkeypatch
):
    ks_path = tmp_path / "KILL_SWITCH"
    orchestrator.kill_switch = KillSwitch(str(ks_path))
    orchestrator.orders.kill_switch = orchestrator.kill_switch
    orchestrator.kill_switch.engage("test")

    # Force every job type "due" regardless of wall-clock/scan-window timing --
    # isolates the kill-switch behavior itself from cadence timing.
    monkeypatch.setattr(cadence, "is_due", lambda job_type, settings, journal, now=None: (True, "forced for test"))

    proposals_before = orchestrator.journal.count_rows("trade_proposals")
    orders_before = orchestrator.journal.count_rows("paper_orders")

    results = JobRunner(orchestrator).run_due_jobs()
    by_type = {r["job_type"]: r for r in results}

    assert by_type["scan"]["status"] == "skipped"
    assert by_type["scan"]["kill_switch_engaged"] is True
    assert orchestrator.journal.count_rows("trade_proposals") == proposals_before
    assert orchestrator.journal.count_rows("paper_orders") == orders_before

    # PR2.5 doctrine: monitor/protection must keep running even when the kill
    # switch blocks new entries (detect+block only, never itself gated off).
    assert by_type["monitor"]["status"] == "completed"
    assert by_type["outcomes_update"]["status"] == "completed"


# =============================================================================
# PR9: dead-man heartbeat (JobRunner.heartbeat_check)
# =============================================================================
# Monday 2026-07-06, 10:30 ET -- REGULAR session (verified: weekday()==0).
_MARKET_HOURS_NOW = datetime(2026, 7, 6, 14, 30, tzinfo=timezone.utc)
# Saturday 2026-07-04, 10:30 ET -- CLOSED session (weekday()==5).
_WEEKEND_NOW = datetime(2026, 7, 4, 14, 30, tzinfo=timezone.utc)


def test_heartbeat_ok_when_a_completed_job_is_recent(orchestrator):
    fresh_ts = timeutils.to_iso(_MARKET_HOURS_NOW - timedelta(minutes=10))
    _insert_job_run(orchestrator.journal, "monitor", "completed", finished_at_utc=fresh_ts)

    result = JobRunner(orchestrator).heartbeat_check(now=_MARKET_HOURS_NOW)

    assert result["ok"] is True
    assert result["market_hours"] is True
    assert result["last_job_type"] == "monitor"


def test_heartbeat_stale_completed_job_is_not_ok_and_alerts(orchestrator, monkeypatch):
    calls = []
    monkeypatch.setattr(alerts, "send_alert", lambda *a, **k: calls.append((a, k)) or True)
    stale_ts = timeutils.to_iso(_MARKET_HOURS_NOW - timedelta(minutes=200))
    _insert_job_run(orchestrator.journal, "monitor", "completed", finished_at_utc=stale_ts)

    result = JobRunner(orchestrator).heartbeat_check(now=_MARKET_HOURS_NOW)

    assert result["ok"] is False
    assert result["market_hours"] is True
    assert "200.0m ago" in result["detail"]
    assert len(calls) == 1
    assert calls[0][1].get("priority") == "high"


def test_heartbeat_no_completed_row_at_all_is_not_ok(orchestrator, monkeypatch):
    monkeypatch.setattr(alerts, "send_alert", lambda *a, **k: True)

    result = JobRunner(orchestrator).heartbeat_check(now=_MARKET_HOURS_NOW)

    assert result["ok"] is False
    assert "no completed job_runs row found" in result["detail"]


def test_heartbeat_unparseable_timestamp_is_not_ok(orchestrator, monkeypatch):
    monkeypatch.setattr(alerts, "send_alert", lambda *a, **k: True)
    _insert_job_run(orchestrator.journal, "monitor", "completed", finished_at_utc="not-a-timestamp")

    result = JobRunner(orchestrator).heartbeat_check(now=_MARKET_HOURS_NOW)

    assert result["ok"] is False
    assert "unparseable timestamp" in result["detail"]


def test_heartbeat_outside_market_hours_is_always_ok_and_never_alerts(orchestrator, monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("must not alert outside market hours")

    monkeypatch.setattr(alerts, "send_alert", fail_if_called)
    # No completed rows at all -- would be "not ok" during market hours, but
    # must be unconditionally healthy on a weekend.

    result = JobRunner(orchestrator).heartbeat_check(now=_WEEKEND_NOW)

    assert result["ok"] is True
    assert result["market_hours"] is False


def test_heartbeat_never_raises_even_if_alert_send_crashes(orchestrator, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("ntfy client exploded")

    monkeypatch.setattr(alerts, "send_alert", boom)

    result = JobRunner(orchestrator).heartbeat_check(now=_MARKET_HOURS_NOW)  # must not raise

    assert result["ok"] is False


# =============================================================================
# PR9: config-hash perturbation (lineage category, PR4 pattern)
# =============================================================================
def test_scheduler_config_hash_changes_when_pr9_settings_change():
    from alphaos.lineage.config_snapshot import build_config_hashes

    hash_a = build_config_hashes(make_settings())["scheduler_config_hash"]
    hash_b = build_config_hashes(make_settings(SCHEDULER_MAX_CONSECUTIVE_FAILURES="7"))["scheduler_config_hash"]
    hash_c = build_config_hashes(make_settings(SCHEDULER_HEARTBEAT_STALE_MINUTES="30"))["scheduler_config_hash"]

    assert hash_a != hash_b
    assert hash_a != hash_c


# =============================================================================
# PR9: behavior-neutrality -- alerts module never touches a decision path
# =============================================================================
def test_alerts_module_never_imported_by_approval_or_risk_engine():
    """Same pattern as PR7/PR8's shadow-layer isolation grep: alerting is
    operator-visibility tooling, never a decision path. job_runner.py IS
    allowed to import it (that's the whole point) -- only the actual
    gate/approval logic is checked here."""
    import alphaos.approval as approval_mod
    import alphaos.risk.risk_engine as risk_mod

    for mod, name in ((approval_mod, "approval.py"), (risk_mod, "risk_engine.py")):
        text = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        assert "util.alerts" not in text and "import alerts" not in text, f"{name} references the alerts module"


def test_decision_functions_never_reference_alerts_or_fuse_state():
    """Structural complement to the grep above: the actual decision-making
    Orchestrator methods must not mention alerting/fuse concepts at all."""
    import inspect

    from alphaos.orchestrator import Orchestrator

    decision_functions = (
        "_handle_proposal", "_resolve_decision", "_combine_decision",
        "_real_decision_driver", "approve_proposal", "reject_proposal",
        "_label_candidate", "_freeze_label", "run_scan_once",
    )
    for fn_name in decision_functions:
        fn = getattr(Orchestrator, fn_name)
        source = inspect.getsource(fn)
        assert "alerts" not in source.lower(), f"Orchestrator.{fn_name} references alerts"
        assert "is_fused" not in source, f"Orchestrator.{fn_name} references is_fused"


def test_no_orders_approvals_fills_positions_created_by_pr9_code():
    """Structural grep, same pattern as the scheduler package's own existing
    real-money-scope test: the fuse/heartbeat/alert additions must not
    introduce any new path toward broker submission. Scoped to the files PR9
    actually added/touched (cadence.py, job_runner.py, alerts.py) -- the rest
    of the scheduler package (jobs.py etc.) is unmodified by PR9 and already
    covered by its own pre-existing tests; jobs.py's docstrings legitimately
    NAME these tokens as things it never calls, which would false-positive a
    whole-package scan."""
    pr9_files = (
        pathlib.Path(cadence.__file__),
        pathlib.Path(cadence.__file__).parent / "job_runner.py",
        pathlib.Path(alerts.__file__),
    )
    banned = ("execute_proposal", "approve_proposal", "close_position",
              "submit_bracket", "submit_order", "place_order")
    for py_file in pr9_files:
        text = py_file.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{py_file.name} references {token!r}"


# =============================================================================
# Trading-day awareness (operator request, 2026-07-11): a real Saturday scan
# window previously read as due, same as any weekday -- closing this is what
# actually protects the just-armed unattended-approval door from acting on a
# closed-market scan.
# =============================================================================
def test_scan_not_due_on_a_real_saturday_within_a_configured_window():
    """The exact bug the operator reported: 2026-07-11 is a Saturday, and
    09:35 ET falls inside the default first scan window."""
    from datetime import datetime, timezone

    s = make_settings()  # default SCHEDULER_SCAN_WINDOWS includes 09:35-09:50
    j = JournalStore(":memory:")
    saturday_in_window = datetime(2026, 7, 11, 13, 35, tzinfo=timezone.utc)  # 09:35 ET (EDT)

    due, reason = cadence.is_due(cadence.JobType.SCAN, s, j, now=saturday_in_window)

    assert due is False
    assert "not a trading day" in reason
    j.close()


def test_scan_not_due_on_a_weekday_nyse_holiday_within_a_configured_window():
    """Christmas Day 2026 is a Friday (a weekday) -- the case a weekday-only
    check would have missed."""
    from datetime import datetime, timezone

    s = make_settings()
    j = JournalStore(":memory:")
    christmas_in_window = datetime(2026, 12, 25, 14, 35, tzinfo=timezone.utc)  # 09:35 ET (EST)

    due, reason = cadence.is_due(cadence.JobType.SCAN, s, j, now=christmas_in_window)

    assert due is False
    assert "not a trading day" in reason
    j.close()


def test_scan_still_due_on_an_ordinary_weekday_within_a_configured_window():
    """Confirms the trading-day gate doesn't over-block a genuine trading
    day -- same window, same time-of-day, just a real Monday."""
    from datetime import datetime, timezone

    s = make_settings()
    j = JournalStore(":memory:")
    monday_in_window = datetime(2026, 7, 13, 13, 35, tzinfo=timezone.utc)  # 09:35 ET (EDT), Monday

    due, reason = cadence.is_due(cadence.JobType.SCAN, s, j, now=monday_in_window)

    assert due is True
    j.close()


# =============================================================================
# PR9.5: benchmark spine cadence + scheduler wiring
# =============================================================================
def test_benchmark_spine_not_due_before_its_configured_time():
    from datetime import datetime, timezone

    s = make_settings(SCHEDULER_BENCHMARK_SPINE_TIME="17:30")
    j = JournalStore(":memory:")
    # 09:00 SGT = 01:00 UTC -- before 17:30 SGT.
    before = datetime(2026, 7, 6, 1, 0, tzinfo=timezone.utc)

    due, reason = cadence.is_due(cadence.JobType.BENCHMARK_SPINE, s, j, now=before)

    assert due is False
    assert "before benchmark_spine time" in reason
    j.close()


def test_benchmark_spine_due_at_or_after_its_configured_time_and_not_yet_run():
    from datetime import datetime, timezone

    s = make_settings(SCHEDULER_BENCHMARK_SPINE_TIME="17:30")
    j = JournalStore(":memory:")
    # 18:00 SGT = 10:00 UTC -- after 17:30 SGT, no prior run today.
    after = datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc)

    due, reason = cadence.is_due(cadence.JobType.BENCHMARK_SPINE, s, j, now=after)

    assert due is True
    j.close()


def test_benchmark_spine_not_due_twice_same_sgt_day(orchestrator):
    from datetime import datetime, timezone

    orchestrator.settings = make_settings(SCHEDULER_BENCHMARK_SPINE_TIME="17:30")
    after = datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc)
    runner = JobRunner(orchestrator)
    runner.run_job(cadence.JobType.BENCHMARK_SPINE, lock_key=cadence.default_lock_key(
        cadence.JobType.BENCHMARK_SPINE, orchestrator.settings, now=after))

    due, reason = cadence.is_due(cadence.JobType.BENCHMARK_SPINE, orchestrator.settings,
                                 orchestrator.journal, now=after)

    assert due is False
    assert "already completed today" in reason


def test_run_due_jobs_includes_benchmark_spine_and_writes_a_real_row(orchestrator, monkeypatch):
    monkeypatch.setattr(cadence, "is_due", lambda job_type, settings, journal, now=None: (True, "forced for test"))

    results = JobRunner(orchestrator).run_due_jobs()

    by_type = {r["job_type"]: r for r in results}
    assert cadence.JobType.BENCHMARK_SPINE in by_type
    assert by_type[cadence.JobType.BENCHMARK_SPINE]["status"] == "completed"
    assert orchestrator.journal.count_rows("equity_snapshots") == 1


def test_scheduler_status_report_includes_benchmark_spine(orchestrator):
    report = JobRunner(orchestrator).status_report()

    assert "benchmark_spine" in report["recent_by_job_type"]


def test_benchmark_spine_config_hash_changes_with_its_own_setting():
    from alphaos.lineage.config_snapshot import build_config_hashes

    hash_a = build_config_hashes(make_settings())["scheduler_config_hash"]
    hash_b = build_config_hashes(make_settings(SCHEDULER_BENCHMARK_SPINE_TIME="09:00"))["scheduler_config_hash"]

    assert hash_a != hash_b


def test_cli_benchmark_spine_is_a_valid_scheduler_run_job_choice():
    from alphaos.__main__ import build_parser

    args = build_parser().parse_args(["scheduler_run_job", "benchmark_spine"])
    assert args.job_type == "benchmark_spine"


# =============================================================================
# TEXT-0: text archive pull cadence + scheduler wiring (mirrors benchmark
# spine's own cadence tests above exactly -- same _once_daily_due helper).
# =============================================================================
def test_text_archive_pull_not_due_before_its_configured_time():
    s = make_settings(SCHEDULER_TEXT_ARCHIVE_PULL_TIME="07:00")
    j = JournalStore(":memory:")
    # 06:00 SGT = 22:00 UTC (previous day) -- before 07:00 SGT.
    before = datetime(2026, 7, 5, 22, 0, tzinfo=timezone.utc)

    due, reason = cadence.is_due(cadence.JobType.TEXT_ARCHIVE_PULL, s, j, now=before)

    assert due is False
    assert "before text_archive_pull time" in reason
    j.close()


def test_text_archive_pull_due_at_or_after_its_configured_time_and_not_yet_run():
    s = make_settings(SCHEDULER_TEXT_ARCHIVE_PULL_TIME="07:00")
    j = JournalStore(":memory:")
    # 08:00 SGT = 00:00 UTC -- after 07:00 SGT, no prior run today.
    after = datetime(2026, 7, 6, 0, 0, tzinfo=timezone.utc)

    due, reason = cadence.is_due(cadence.JobType.TEXT_ARCHIVE_PULL, s, j, now=after)

    assert due is True
    j.close()


def test_text_archive_pull_not_due_twice_same_sgt_day(orchestrator):
    # TEXT_ARCHIVE_ENABLED=true so the dispatched job actually reaches
    # status='completed' -- disabled (the default) returns 'skipped', which
    # _once_daily_due deliberately does NOT treat as "already done today"
    # (see _once_daily_due's own "status = 'completed'" filter), so a
    # disabled job stays due all day, harmlessly re-skipping each tick.
    orchestrator.settings = make_settings(
        SCHEDULER_TEXT_ARCHIVE_PULL_TIME="07:00", TEXT_ARCHIVE_ENABLED="true",
    )
    after = datetime(2026, 7, 6, 0, 0, tzinfo=timezone.utc)
    runner = JobRunner(orchestrator)
    runner.run_job(cadence.JobType.TEXT_ARCHIVE_PULL, lock_key=cadence.default_lock_key(
        cadence.JobType.TEXT_ARCHIVE_PULL, orchestrator.settings, now=after))

    due, reason = cadence.is_due(cadence.JobType.TEXT_ARCHIVE_PULL, orchestrator.settings,
                                 orchestrator.journal, now=after)

    assert due is False
    assert "already completed today" in reason


def test_run_due_jobs_includes_text_archive_pull(orchestrator, monkeypatch):
    monkeypatch.setattr(cadence, "is_due", lambda job_type, settings, journal, now=None: (True, "forced for test"))

    results = JobRunner(orchestrator).run_due_jobs()

    by_type = {r["job_type"]: r for r in results}
    assert cadence.JobType.TEXT_ARCHIVE_PULL in by_type
    # TEXT_ARCHIVE_ENABLED defaults false -- dispatched, but the job itself no-ops.
    assert by_type[cadence.JobType.TEXT_ARCHIVE_PULL]["status"] == "skipped"


def test_scheduler_status_report_includes_text_archive_pull(orchestrator):
    report = JobRunner(orchestrator).status_report()

    assert "text_archive_pull" in report["recent_by_job_type"]


def test_run_due_jobs_includes_atr_update(orchestrator, monkeypatch):
    """INSTR-1: the exact regression class TEXT-0 self-caught -- wired into
    cadence.is_due but NOT into JobRunner's hardcoded dispatch tuple would
    mean this job silently NEVER runs in production."""
    monkeypatch.setattr(cadence, "is_due", lambda job_type, settings, journal, now=None: (True, "forced for test"))

    results = JobRunner(orchestrator).run_due_jobs()

    by_type = {r["job_type"]: r for r in results}
    assert cadence.JobType.ATR_UPDATE in by_type
    # Mock mode -- make_bars_provider() returns None, so the job completes
    # with zero rows written, never an error.
    assert by_type[cadence.JobType.ATR_UPDATE]["status"] == "completed"


def test_scheduler_status_report_includes_atr_update(orchestrator):
    report = JobRunner(orchestrator).status_report()

    assert "atr_update" in report["recent_by_job_type"]


def test_text_archive_pull_config_hash_changes_with_its_own_setting():
    """Unlike benchmark_spine's cadence time (folded into scheduler_config_
    hash), scheduler_text_archive_pull_time lives in its own
    text_archive_config_hash bucket alongside TEXT_ARCHIVE_ENABLED/
    SEC_EDGAR_CONTACT_EMAIL -- see TEXT_ARCHIVE_CONFIG_FIELDS."""
    from alphaos.lineage.config_snapshot import build_config_hashes

    hash_a = build_config_hashes(make_settings())["text_archive_config_hash"]
    hash_b = build_config_hashes(
        make_settings(SCHEDULER_TEXT_ARCHIVE_PULL_TIME="09:00")
    )["text_archive_config_hash"]

    assert hash_a != hash_b


def test_cli_text_archive_pull_is_a_valid_scheduler_run_job_choice():
    from alphaos.__main__ import build_parser

    args = build_parser().parse_args(["scheduler_run_job", "text_archive_pull"])
    assert args.job_type == "text_archive_pull"
