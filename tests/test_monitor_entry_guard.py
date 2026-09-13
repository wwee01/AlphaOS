"""A long scan may not rely on an earlier healthy execution monitor."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.scheduler.monitor_health import monitor_health
from alphaos.util import timeutils
from conftest import make_proposal, make_settings


@pytest.mark.parametrize("failure", ["stale", "failed", "inflight"])
def test_monitor_becomes_unready_during_scan_blocks_at_actual_submission(failure, monkeypatch):
    start = datetime(2026, 9, 14, 14, 0, tzinfo=timezone.utc)
    now = [start]
    monkeypatch.setattr(timeutils, "now_utc", lambda: now[0])
    journal = JournalStore(":memory:")
    orch = Orchestrator(settings=make_settings(), journal=journal)
    submitted = []
    orch.orders = SimpleNamespace(real_paper=True, execute_proposal=lambda *a, **kw: submitted.append(a))
    journal.insert("job_runs", {
        "job_run_id": "healthy", "job_type": "monitor", "lock_key": "initial",
        "status": "completed", "started_at_utc": start.isoformat(),
        "started_at_sgt": timeutils.stamp(start).local_sgt,
        "finished_at_utc": start.isoformat(), "result_summary_json": {"worker_role": "monitor"},
    })
    try:
        assert monitor_health(journal, orch.settings)["entry_ready"]
        if failure == "stale":
            now[0] += timedelta(hours=1)
        else:
            journal.insert("job_runs", {
                "job_run_id": "later", "job_type": "monitor", "lock_key": "later",
                "status": "failed" if failure == "failed" else "started",
                "started_at_utc": start.isoformat(), "finished_at_utc": start.isoformat(),
                "started_at_sgt": timeutils.stamp(start).local_sgt,
            })
        proposal = make_proposal(invalidation_reason="stop invalidates thesis")
        proposal.proposal_expires_at_utc = (start + timedelta(days=1)).isoformat()
        result = orch._execute_checked(proposal)
        assert result.blocked
        assert "monitor not ready" in result.detail
        assert submitted == []
        assert journal.count_rows("paper_orders") == 0
    finally:
        orch.close()


def test_ready_monitor_preserves_normal_paper_submission_path(monkeypatch):
    now = datetime(2026, 9, 14, 14, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(timeutils, "now_utc", lambda: now)
    journal = JournalStore(":memory:")
    orch = Orchestrator(settings=make_settings(), journal=journal)
    sent = []
    def submit(proposal, **kwargs):
        sent.append(proposal.proposal_id)
        return "fake-submitted"
    orch.orders = SimpleNamespace(real_paper=True, execute_proposal=submit)
    journal.insert("job_runs", {
        "job_run_id": "healthy", "job_type": "monitor", "lock_key": "initial",
        "status": "completed", "started_at_utc": now.isoformat(),
        "started_at_sgt": timeutils.stamp(now).local_sgt,
        "finished_at_utc": now.isoformat(), "result_summary_json": {"worker_role": "monitor"},
    })
    try:
        proposal = make_proposal(invalidation_reason="stop invalidates thesis")
        proposal.proposal_expires_at_utc = (now + timedelta(days=1)).isoformat()
        assert orch._execute_checked(proposal) == "fake-submitted"
        assert sent == [proposal.proposal_id]
    finally:
        orch.close()


def test_entry_cannot_submit_while_independent_monitor_owns_account(monkeypatch, tmp_path):
    import threading

    from alphaos.scheduler.ownership import monitor_ownership

    journal = JournalStore(str(tmp_path / "ledger.db"))
    orch = Orchestrator(settings=make_settings(), journal=journal)
    orch.orders = SimpleNamespace(real_paper=True)
    acquired, release = threading.Event(), threading.Event()
    def monitor_thread():
        with monitor_ownership(journal) as owner:
            assert owner
            acquired.set()
            assert release.wait(10)
    monitor = threading.Thread(target=monitor_thread)
    monitor.start()
    try:
        assert acquired.wait(5)
        monkeypatch.setattr(orch, "_assess_current_risk", lambda **kw: pytest.fail("entry started during monitor ownership"))
        monkeypatch.setattr(orch, "_execute_checked", lambda *a: pytest.fail("broker submission during monitoring"))
        result = orch._execute(make_proposal())
        assert result.blocked and "monitor owns" in result.detail
        assert journal.count_rows("paper_orders") == 0
    finally:
        release.set()
        monitor.join(5)
        orch.close()
