"""Immediate proposal/fill alerts (operator ask, 2026-07-11). Covers:
* _alert_new_pending_approvals() (alphaos/scheduler/jobs.py) -- fires once
  per scan batch listing every new pending_approval proposal, silent on a
  mock run or an empty batch.
* PositionManager.open_position()/close_position() -- fire an alert on
  every REAL entry/exit fill, silent on mock mode or a seed_demo fixture
  row, regardless of which of the fill paths (internal sim vs alpaca-paper
  sync/async) produced the call.

All offline, in-memory, mock mode by default (deliberately overridden to
non-mock settings where a test needs to prove the alert actually fires).
No real network call -- alerts.send_alert is monkeypatched throughout.
"""

from __future__ import annotations

from alphaos.execution.position_manager import PositionManager
from alphaos.scheduler.jobs import _alert_new_pending_approvals
from alphaos.util.ids import new_id
from conftest import make_settings


class _FakeOrch:
    def __init__(self, settings, journal):
        self.settings = settings
        self.journal = journal


def _insert_proposal(journal, scan_batch_id, symbol="AAPL", status="pending_approval"):
    proposal_id = new_id("prop")
    journal.insert("trade_proposals", {
        "proposal_id": proposal_id, "candidate_id": new_id("cand"), "symbol": symbol,
        "direction": "long", "strategy": "swing",
        "entry": 100.0, "stop": 98.0, "target": 106.0, "max_holding_days": 3, "qty": 10.0,
        "risk_per_share": 2.0, "dollar_risk": 20.0, "expected_r": 2.0,
        "same_day_exit_eligible": 0, "status": status, "scan_batch_id": scan_batch_id,
        "proposal_expires_at_utc": "2026-07-11T01:00:00+00:00",
    })
    return proposal_id


# --------------------------------------------------- _alert_new_pending_approvals
def test_alert_new_pending_approvals_fires_for_a_real_scan_batch(journal, monkeypatch):
    settings = make_settings(NTFY_TOPIC="test-topic", ALPHAOS_MODE="paper")
    orch = _FakeOrch(settings, journal)
    calls = []
    monkeypatch.setattr("alphaos.util.alerts.send_alert", lambda *a, **k: calls.append(k) or True)

    batch_id = new_id("scan")
    _insert_proposal(journal, batch_id, symbol="AAPL")
    _insert_proposal(journal, batch_id, symbol="MSFT")

    _alert_new_pending_approvals(orch, batch_id)

    assert len(calls) == 1
    assert "2 proposal(s)" in calls[0]["title"]
    assert "AAPL" in calls[0]["title"] and "MSFT" in calls[0]["title"]
    assert "AAPL" in calls[0]["message"] and "MSFT" in calls[0]["message"]
    assert calls[0]["priority"] == "high"


def test_alert_new_pending_approvals_silent_on_an_empty_batch(journal, monkeypatch):
    settings = make_settings(NTFY_TOPIC="test-topic", ALPHAOS_MODE="paper")
    orch = _FakeOrch(settings, journal)
    calls = []
    monkeypatch.setattr("alphaos.util.alerts.send_alert", lambda *a, **k: calls.append(k))

    _alert_new_pending_approvals(orch, new_id("scan"))

    assert calls == []


def test_alert_new_pending_approvals_silent_in_mock_mode(journal, monkeypatch):
    settings = make_settings(NTFY_TOPIC="test-topic", ALPHAOS_MODE="mock")
    orch = _FakeOrch(settings, journal)
    calls = []
    monkeypatch.setattr("alphaos.util.alerts.send_alert", lambda *a, **k: calls.append(k))

    batch_id = new_id("scan")
    _insert_proposal(journal, batch_id)

    _alert_new_pending_approvals(orch, batch_id)

    assert calls == []


def test_alert_new_pending_approvals_ignores_other_batches_and_other_statuses(journal, monkeypatch):
    """Only THIS scan's own pending_approval rows count -- a stale
    pending_approval from a PRIOR scan, or a row this same scan already
    risk-blocked/filled, must never inflate the count."""
    settings = make_settings(NTFY_TOPIC="test-topic", ALPHAOS_MODE="paper")
    orch = _FakeOrch(settings, journal)
    calls = []
    monkeypatch.setattr("alphaos.util.alerts.send_alert", lambda *a, **k: calls.append(k))

    batch_id = new_id("scan")
    _insert_proposal(journal, new_id("scan"), symbol="OLDSCAN")  # different batch
    _insert_proposal(journal, batch_id, symbol="BLOCKED", status="blocked")  # same batch, wrong status
    _insert_proposal(journal, batch_id, symbol="NVDA")  # the real one

    _alert_new_pending_approvals(orch, batch_id)

    assert len(calls) == 1
    assert "NVDA" in calls[0]["title"]
    assert "OLDSCAN" not in calls[0]["title"] and "BLOCKED" not in calls[0]["title"]


def test_alert_new_pending_approvals_noop_when_scan_batch_id_missing(journal, monkeypatch):
    settings = make_settings(NTFY_TOPIC="test-topic", ALPHAOS_MODE="paper")
    orch = _FakeOrch(settings, journal)
    calls = []
    monkeypatch.setattr("alphaos.util.alerts.send_alert", lambda *a, **k: calls.append(k))

    _alert_new_pending_approvals(orch, None)

    assert calls == []


# ------------------------------------------------------------------- entry fill
def _order_row(**overrides):
    row = {
        "order_id": new_id("ord"), "symbol": "AAPL", "direction": "long", "qty": 10.0,
        "strategy": "swing", "stop_loss_price": 98.0, "take_profit_price": 106.0,
        "execution_source": "simulated_internal", "is_short": 0, "is_demo": 0,
        "trade_id": new_id("trade"), "proposal_id": None,
    }
    row.update(overrides)
    return row


def test_open_position_alerts_on_a_real_entry_fill(journal, monkeypatch):
    settings = make_settings(NTFY_TOPIC="test-topic", ALPHAOS_MODE="paper")
    calls = []
    monkeypatch.setattr("alphaos.util.alerts.send_alert", lambda *a, **k: calls.append(k) or True)
    pm = PositionManager(settings, journal)

    pm.open_position(_order_row(symbol="AAPL"), 150.25)

    assert len(calls) == 1
    assert "AAPL" in calls[0]["title"]
    assert "BUY" in calls[0]["title"]
    assert calls[0]["priority"] == "default"


def test_open_position_alert_says_short_for_a_short_fill(journal, monkeypatch):
    settings = make_settings(NTFY_TOPIC="test-topic", ALPHAOS_MODE="paper")
    calls = []
    monkeypatch.setattr("alphaos.util.alerts.send_alert", lambda *a, **k: calls.append(k) or True)
    pm = PositionManager(settings, journal)

    pm.open_position(_order_row(symbol="TSLA", is_short=1), 250.0)

    assert "SHORT" in calls[0]["title"]


def test_open_position_silent_in_mock_mode(journal, monkeypatch):
    settings = make_settings(NTFY_TOPIC="test-topic", ALPHAOS_MODE="mock")
    calls = []
    monkeypatch.setattr("alphaos.util.alerts.send_alert", lambda *a, **k: calls.append(k))
    pm = PositionManager(settings, journal)

    pm.open_position(_order_row(), 150.0)

    assert calls == []


def test_open_position_silent_for_a_seed_demo_fixture_row(journal, monkeypatch):
    settings = make_settings(NTFY_TOPIC="test-topic", ALPHAOS_MODE="paper")
    calls = []
    monkeypatch.setattr("alphaos.util.alerts.send_alert", lambda *a, **k: calls.append(k))
    pm = PositionManager(settings, journal)

    pm.open_position(_order_row(is_demo=1), 150.0)

    assert calls == []


# -------------------------------------------------------------------- exit fill
def test_close_position_alerts_on_a_real_exit_fill(journal, monkeypatch):
    settings = make_settings(NTFY_TOPIC="test-topic", ALPHAOS_MODE="paper")
    calls = []
    monkeypatch.setattr("alphaos.util.alerts.send_alert", lambda *a, **k: calls.append(k) or True)
    pm = PositionManager(settings, journal)
    position_id = pm.open_position(_order_row(symbol="AAPL"), 150.0)
    calls.clear()  # discard the entry-fill alert; this test is about the exit

    pm.close_position(position_id, 160.0, "target_hit")

    assert len(calls) == 1
    assert "AAPL" in calls[0]["title"]
    assert "WIN" in calls[0]["title"]
    assert calls[0]["priority"] == "default"


def test_close_position_alert_says_loss_for_a_losing_exit(journal, monkeypatch):
    settings = make_settings(NTFY_TOPIC="test-topic", ALPHAOS_MODE="paper")
    calls = []
    monkeypatch.setattr("alphaos.util.alerts.send_alert", lambda *a, **k: calls.append(k) or True)
    pm = PositionManager(settings, journal)
    position_id = pm.open_position(_order_row(symbol="AAPL"), 150.0)
    calls.clear()

    pm.close_position(position_id, 140.0, "stop_hit")

    assert "LOSS" in calls[0]["title"]


def test_close_position_silent_in_mock_mode(journal, monkeypatch):
    settings = make_settings(NTFY_TOPIC="test-topic", ALPHAOS_MODE="mock")
    calls = []
    monkeypatch.setattr("alphaos.util.alerts.send_alert", lambda *a, **k: calls.append(k))
    pm = PositionManager(settings, journal)
    position_id = pm.open_position(_order_row(), 150.0)
    calls.clear()

    pm.close_position(position_id, 160.0, "target_hit")

    assert calls == []


def test_close_position_silent_for_a_seed_demo_fixture_row(journal, monkeypatch):
    settings = make_settings(NTFY_TOPIC="test-topic", ALPHAOS_MODE="paper")
    calls = []
    monkeypatch.setattr("alphaos.util.alerts.send_alert", lambda *a, **k: calls.append(k))
    pm = PositionManager(settings, journal)
    position_id = pm.open_position(_order_row(is_demo=1), 150.0)
    calls.clear()

    pm.close_position(position_id, 160.0, "target_hit")

    assert calls == []


# ---------------------------------------------- structural isolation (§H.6-style)
def test_decision_functions_still_never_reference_alerts():
    """Re-assert the PR9 isolation law this feature must not violate: the
    Orchestrator decision functions must still never mention alerting.
    (tests/test_scheduler.py already asserts this generally; repeated here
    scoped to this feature's own PR so a reviewer sees the guarantee holds
    without cross-referencing another test file.)"""
    import inspect

    from alphaos.orchestrator import Orchestrator

    for fn_name in ("_handle_proposal", "run_scan_once", "approve_proposal"):
        source = inspect.getsource(getattr(Orchestrator, fn_name))
        assert "alerts" not in source.lower(), f"Orchestrator.{fn_name} references alerts"
