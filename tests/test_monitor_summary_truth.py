"""Short monitor event reports the same counters as its structured result."""
from copy import deepcopy

import pytest

from alphaos.execution import protection_watchdog
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from conftest import make_settings


@pytest.mark.parametrize("counts,error_count", [
    # Observed Sep 28 AMD time-exit pass: distinct counters overlap; do not sum.
    ({"checked": 3, "protected": 0, "degraded": 0, "unprotected": 0,
      "closed_mismatch": 0, "check_error": 0, "unverifiable": 3,
      "qty_mismatches": 3, "open_incident_count": 3}, 1),
    # Distinguish closed-position disagreement from quantity disagreement.
    ({"protected": 2, "degraded": 1, "unprotected": 4, "closed_mismatch": 5,
      "check_error": 6, "unverifiable": 7, "qty_mismatches": 8, "open_incident_count": 9}, 3),
    ({"protected": 2, "degraded": 0, "unprotected": 0, "closed_mismatch": 0,
      "check_error": 0, "unverifiable": 0, "qty_mismatches": 0, "open_incident_count": 0}, 0),
    ({}, 0),
])
def test_short_message_preserves_each_structured_counter_and_monitor_behavior(monkeypatch, counts, error_count):
    journal = JournalStore(":memory:")
    orchestrator = Orchestrator(settings=make_settings(), journal=journal)
    # Avoid startup providers; this is the real monitor orchestration with
    # offline dependencies, including the ordinary scheduler/event persistence.
    monkeypatch.setattr(orchestrator, "_ensure_startup", lambda: None)
    calls = []
    recon = {"reconciled": 4, "opened": ["position"],
             "exits": [{"exit_id": "protective_exit"}],
             "time_exits_closed": [{"exit": {"exit_id": "time_exit"}}],
             "errors": ["reconcile"] if error_count == 3 else [],
             "stale_errors": ["stale"] if error_count == 3 else [],
             "time_exits_errors": ["pending_close"] if error_count else []}

    def reconcile():
        calls.append("reconcile")
        return deepcopy(recon)

    def protection(*args, **kwargs):
        calls.append("protection")
        assert kwargs["scheduler_run_id"]
        return deepcopy(counts)

    def monitor(price_overrides=None):
        calls.append("local_monitor")
        assert price_overrides == {"XYZ": 123}
        return [{"exit_id": "simulated_exit"}]

    monkeypatch.setattr(orchestrator.orders, "reconcile", reconcile)
    monkeypatch.setattr(protection_watchdog, "run_watchdog_pass", protection)
    monkeypatch.setattr(orchestrator.positions, "monitor", monitor)
    result = orchestrator.run_monitor_once(price_overrides={"XYZ": 123})
    assert calls == ["reconcile", "protection", "local_monitor"]
    assert result["protection"] == counts
    assert result["errors"] == recon["errors"] + recon["stale_errors"] + recon["time_exits_errors"]
    assert result["exits"] == [{"exit_id": "protective_exit"}, {"exit_id": "time_exit"}, {"exit_id": "simulated_exit"}]
    run = journal.one("SELECT * FROM scheduler_runs WHERE scheduler_run_id=?", (result["scheduler_run_id"],))
    assert run["status"] == "completed" and run["error_count"] == error_count
    events = journal.query("SELECT * FROM system_events WHERE category='monitor'")
    assert len(events) == 1
    message = events[0]["message"]
    assert "3 exit(s); reconciled 4 alpaca_paper order(s), opened 1" in message
    for key, label in [("protected", "protected"), ("degraded", "degraded"),
                       ("unprotected", "unprotected"), ("unverifiable", "unverifiable"),
                       ("check_error", "check errors"), ("qty_mismatches", "quantity mismatches"),
                       ("closed_mismatch", "closed-position mismatches"),
                       ("open_incident_count", "open incidents")]:
        assert f"{counts.get(key, 0)} {label}" in message
    assert f"execution errors: {error_count}" in message
    journal.close()
