"""Service exit status distinguishes healthy deferral from execution faults."""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from alphaos.scheduler import worker
from conftest import make_settings


@pytest.fixture
def worker_fixture(monkeypatch):
    settings = make_settings()
    health = {"ok": True, "entry_ready": False}
    jobs = []
    closed = []
    orch = SimpleNamespace(journal=object(), close=lambda: closed.append(True))
    monkeypatch.setattr(worker, "worker_ownership", lambda *a: nullcontext(True))
    monkeypatch.setattr("alphaos.orchestrator.Orchestrator", lambda **k: orch)
    monkeypatch.setattr(worker, "JobRunner", lambda *a, **k: SimpleNamespace(run_due_jobs=lambda: jobs))
    monkeypatch.setattr(worker, "monitor_health", lambda *a: health)
    monkeypatch.setattr("alphaos.research.swing_audit_worker.maybe_run", lambda *a: None)
    return settings, health, jobs, closed


@pytest.mark.parametrize("status,reason,nested_ok,code", [
    ("blocked", "monitor_not_ready", True, 0),
    ("blocked", "monitor_not_ready", False, 1),
    ("blocked", "monitor_not_ready", None, 1),
    ("blocked", "unknown_block", True, 1),
    ("failed", "failure", True, 1),
    ("fused", "fuse", True, 1),
    ("skipped", "duplicate_lock", True, 0),
    ("not_due", "outside_window", True, 0),
])
def test_research_deferral_exit_keeps_result_and_entry_gate(worker_fixture, status, reason, nested_ok, code):
    settings, health, jobs, closed = worker_fixture
    jobs.append({"job_type": "scan", "status": status, "reason": reason,
                 "monitor_health": {"ok": nested_ok}})
    result, actual = worker.run_role("research", settings)
    assert actual == code and result["results"] == jobs
    assert result["entry_ready"] is False and result["monitor_health"] == health
    assert closed == [True]


@pytest.mark.parametrize("role", ["research", "monitor"])
def test_current_health_fault_cannot_be_masked_by_normal_skip(worker_fixture, role):
    settings, health, jobs, _ = worker_fixture
    health.update(ok=False, issue_codes=["monitor_stale"])
    jobs.append({"job_type": "scan", "status": "blocked", "reason": "monitor_not_ready",
                 "monitor_health": {"ok": True}})
    result, code = worker.run_role(role, settings)
    assert code == 1 and result["monitor_health"]["issue_codes"] == ["monitor_stale"]


def test_worker_exception_has_nonzero_exit_without_exception_text(monkeypatch, capsys):
    monkeypatch.setattr(worker, "load_settings", lambda: make_settings())
    def failed(*a):
        raise RuntimeError("sensitive")
    monkeypatch.setattr(worker, "run_role", failed)
    assert worker.main(["--role", "monitor"]) == 2
    assert "sensitive" not in capsys.readouterr().out
