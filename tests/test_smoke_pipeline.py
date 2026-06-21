"""End-to-end smoke: the v1 pipeline runs in mock mode with no external keys."""

from __future__ import annotations

from alphaos.constants import ExecutionSource, ExitClassification
from conftest import make_settings
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator


def test_full_pipeline_runs_offline():
    orch = Orchestrator(settings=make_settings(), journal=JournalStore(":memory:"))
    checks = orch.startup()
    assert all(c.ok for c in checks if c.severity.value in ("error", "critical"))

    summary = orch.run_scan_once()
    assert summary.candidates > 0  # universe produces detected candidates

    # Demo trade exercises execution + journal end to end.
    demo = orch.seed_demo()
    assert demo["approved"] is True
    assert orch.journal.count_open_positions() == 1

    # Watchdog closes it on a forced target hit, classified profit-taking same-day.
    res = orch.run_monitor_once(price_overrides={"DEMO": 10_000_000})
    assert len(res["exits"]) == 1
    assert res["exits"][0]["classification"] == ExitClassification.PROFIT_TAKING.value

    report = orch.generate_daily_report()
    assert report["report_date"]
    assert "AlphaOS Daily Learning Report" in report["content_md"]

    # Order journal is consistent and labelled as internal simulation.
    orders = orch.journal.query("SELECT DISTINCT execution_source, execution_provider FROM paper_orders")
    assert all(o["execution_source"] == ExecutionSource.INTERNAL_SIM.value for o in orders)
    assert all(o["execution_provider"] == "simulated_internal" for o in orders)
    orch.close()


def test_kill_switch_blocks_execution(tmp_path, monkeypatch):
    from alphaos.safety import KillSwitch

    ks_path = tmp_path / "KILL_SWITCH"
    orch = Orchestrator(settings=make_settings(), journal=JournalStore(":memory:"))
    orch.kill_switch = KillSwitch(str(ks_path))
    orch.orders.kill_switch = orch.kill_switch
    orch.kill_switch.engage("test")

    from conftest import make_proposal

    result = orch.orders.execute_proposal(make_proposal())
    assert result.blocked is True
    assert result.block_reason == "KILL_SWITCH_ACTIVE"
    orch.close()
