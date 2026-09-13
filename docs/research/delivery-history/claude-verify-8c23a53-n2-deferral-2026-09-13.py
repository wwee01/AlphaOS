"""N2 approval preservation on 8c23a53 (reuses Codex's broker-free fixture; independent assertions)."""
import sys, os, threading, time, fcntl
sys.path.insert(0, os.path.join(os.environ["ALPHAOS_REPO"], "tests"))
from pathlib import Path
import pytest
from conftest import inject_pending_proposal
from test_entry_deferral import paper_entry, held_monitor   # noqa: F401  (fixture + helper)


def test_n2a_four_second_hold_auto_path_submits_once_with_one_receipt(paper_entry, monkeypatch):
    orch, clock, exposure, attempted = paper_entry
    pid, proposal = inject_pending_proposal(orch)
    t0 = time.monotonic()
    with held_monitor(orch.journal, duration=4):
        ok, msg = orch.approve_proposal(pid, approver="explicit user")
    assert ok, msg
    assert 3.5 <= time.monotonic() - t0 < 10
    assert attempted == [pid]
    assert orch.journal.count_rows("approvals") == 1
    assert orch.journal.proposal_by_id(pid)["status"] in ("filled", "submitted")


def test_n2b_timeout_leaves_proposal_pending_with_no_receipt_no_attempt(paper_entry, monkeypatch):
    orch, clock, exposure, attempted = paper_entry
    monkeypatch.setattr("alphaos.orchestrator.ENTRY_MONITOR_WAIT_SECONDS", 0.3)
    pid, _ = inject_pending_proposal(orch)
    with held_monitor(orch.journal):                                # held for the whole call
        ok, msg = orch.approve_proposal(pid, approver="explicit user")
    assert not ok and "deferred" in msg
    assert attempted == [] and orch.journal.count_rows("approvals") == 0
    assert orch.journal.count_auto_approvals_today() == 0
    assert orch.journal.proposal_by_id(pid)["status"] == "pending_approval"
    ev = orch.journal.query("SELECT * FROM system_events WHERE category='entry_deferred'")
    assert len(ev) == 1
    # fresh manual retry after release works exactly once
    ok, msg = orch.approve_proposal(pid, approver="explicit user")
    assert ok, msg
    assert attempted == [pid] and orch.journal.count_rows("approvals") == 1


def test_n2c_entry_lock_held_by_another_process_defers_without_receipt(paper_entry, monkeypatch):
    orch, clock, exposure, attempted = paper_entry
    pid, _ = inject_pending_proposal(orch)
    lock_path = Path(orch.journal.db_path).resolve().with_suffix(".entry.lock")
    handle = open(lock_path, "a"); fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)   # foreign owner
    try:
        ok, msg = orch.approve_proposal(pid, approver="explicit user")
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN); handle.close()
    assert not ok and "deferred" in msg
    assert attempted == [] and orch.journal.count_rows("approvals") == 0
    assert orch.journal.proposal_by_id(pid)["status"] == "pending_approval"


def test_n2d_observation_unhealthy_monitor_still_blocks_permanently_not_defers(paper_entry, monkeypatch):
    """Not in N2's scope but the same lost-proposal class: an execution error in the latest
    completed monitor pass makes entry_ready False -> BLOCKED (terminal), not deferred."""
    orch, clock, exposure, attempted = paper_entry
    pid, _ = inject_pending_proposal(orch)
    orch.journal.conn.execute("UPDATE job_runs SET result_summary_json=? WHERE job_run_id='monitor-ready'",
                              ('{"worker_role": "monitor", "monitor_result": {"errors": [{"error": "get_order failed: transient"}]}}',))
    orch.journal.conn.commit()
    ok, msg = orch.approve_proposal(pid, approver="explicit user")
    assert not ok and "blocked" in msg
    assert attempted == [] and orch.journal.proposal_by_id(pid)["status"] == "blocked"
