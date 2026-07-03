"""Broker protection watchdog (docs/roadmap/protection-watchdog.md).

Exercised hermetically against the same FakeTradingClient used by
test_alpaca_paper_execution.py -- no SDK/network. Covers detection (missing
stop/target legs, TIF policy, local-open/broker-closed mismatch), blocking new
entries, human-confirmed resolution (never auto-close), restart-recovery, and
the reconcile-then-watchdog ordering invariant.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from alphaos.broker.alpaca_client import AlpacaClient
from alphaos.constants import ReasonCode
from alphaos.execution import protection_watchdog as pw
from alphaos.execution.order_manager import OrderManager
from alphaos.journal.journal_store import JournalStore
from alphaos.util.ids import new_id
from conftest import inject_pending_proposal, make_proposal, make_settings
from test_alpaca_paper_execution import FakeTradingClient, _paper_om, _seed_proposal


def _open_protected_position(fake, om, journal, symbol, entry, stop, target, qty, max_holding_days=3):
    """Submit + fill + reconcile a bracket, and register the matching broker-side
    position (set_position) so the watchdog sees a healthy, fully-consistent
    starting state. Returns (position_row, proposal)."""
    prop = make_proposal(symbol=symbol, entry=entry, stop=stop, target=target, qty=qty)
    prop.max_holding_days = max_holding_days
    _seed_proposal(journal, prop)
    om.execute_proposal(prop)
    fake.fill_entry(symbol, price=entry)
    om.reconcile()
    fake.set_position(symbol, qty=qty, side="long", avg_entry_price=entry)
    pos = journal.one(
        "SELECT * FROM positions WHERE symbol = ? AND status = 'open' ORDER BY id DESC LIMIT 1", (symbol,)
    )
    return pos, prop


def _insert_manual_incident(journal, position_id="pos_test", symbol="TEST"):
    """Directly insert an open, blocking protection_checks row -- used to test
    the BLOCKING WIRING independent of the detection logic."""
    journal.insert("protection_checks", {
        "check_id": new_id("pcheck"), "position_id": position_id, "symbol": symbol,
        "protection_status": "unprotected", "severity": "critical",
        "detail": "test-injected incident",
    })


# ------------------------------------------------------------------ detection
def test_protected_position_passes():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)
    pos, _ = _open_protected_position(fake, om, journal, "AAPL", 100.0, 97.0, 106.0, 10)

    result = pw.run_watchdog_pass(journal, om.alpaca, s)

    assert result["checked"] == 1
    assert result["protected"] == 1
    assert result["unprotected"] == 0
    assert result["new_incidents"] == []
    row = journal.one("SELECT protection_status FROM positions WHERE position_id = ?", (pos["position_id"],))
    assert row["protection_status"] == "protected"
    assert pw.has_blocking_incident(journal) is None


def test_missing_stop_leg_fails_unprotected_critical():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)
    pos, _ = _open_protected_position(fake, om, journal, "META", 618.78, 594.99, 666.45, 41, max_holding_days=5)
    fake.expire_leg("META", "stop_loss")

    result = pw.run_watchdog_pass(journal, om.alpaca, s)

    assert result["unprotected"] == 1
    assert len(result["new_incidents"]) == 1
    blocking = pw.has_blocking_incident(journal)
    assert blocking is not None
    assert blocking["protection_status"] == "unprotected"
    assert blocking["severity"] == "critical"
    events = journal.query(
        "SELECT * FROM system_events WHERE category = 'protection_watchdog' AND severity = 'critical'"
    )
    assert len(events) == 1


def test_missing_target_leg_only_is_degraded_warning_not_blocking():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)
    pos, _ = _open_protected_position(fake, om, journal, "MSFT", 400.0, 390.0, 420.0, 5)
    fake.cancel_leg("MSFT", "take_profit")

    result = pw.run_watchdog_pass(journal, om.alpaca, s)

    assert result["degraded"] == 1
    assert result["unprotected"] == 0
    assert result["new_incidents"] == []  # degraded never counts as a blocking incident
    assert pw.has_blocking_incident(journal) is None
    row = journal.one("SELECT protection_status FROM positions WHERE position_id = ?", (pos["position_id"],))
    assert row["protection_status"] == "degraded"
    events = journal.query("SELECT * FROM system_events WHERE category = 'protection_watchdog'")
    assert len(events) == 1 and events[0]["severity"] == "warning"


def test_repeated_pass_on_same_incident_does_not_spam_duplicate_alerts():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)
    _open_protected_position(fake, om, journal, "META", 618.78, 594.99, 666.45, 41, max_holding_days=5)
    fake.expire_leg("META", "stop_loss")

    r1 = pw.run_watchdog_pass(journal, om.alpaca, s)
    r2 = pw.run_watchdog_pass(journal, om.alpaca, s)
    r3 = pw.run_watchdog_pass(journal, om.alpaca, s)

    assert len(r1["new_incidents"]) == 1
    assert r2["new_incidents"] == []
    assert r3["new_incidents"] == []
    # Every pass still writes its own audit row (Part A's "every pass verifies")...
    audit_rows = journal.query("SELECT * FROM protection_checks WHERE symbol = 'META'")
    assert len(audit_rows) == 3
    # ...but only ONE critical alert fired, not three.
    critical_events = journal.query(
        "SELECT * FROM system_events WHERE severity = 'critical' AND category = 'protection_watchdog'"
    )
    assert len(critical_events) == 1


def test_both_legs_missing_is_unprotected_not_escalated():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)
    _open_protected_position(fake, om, journal, "META", 618.78, 594.99, 666.45, 41, max_holding_days=5)
    fake.expire_leg("META", "stop_loss")
    fake.expire_leg("META", "take_profit")

    result = pw.run_watchdog_pass(journal, om.alpaca, s)

    assert result["unprotected"] == 1  # same severity as stop-missing-only, not escalated further
    assert result["degraded"] == 0


# --------------------------------------------------------------- TIF policy
def test_multiday_position_uses_gtc_and_is_appropriate():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)
    pos, _ = _open_protected_position(fake, om, journal, "META", 618.78, 594.99, 666.45, 41, max_holding_days=5)

    pw.run_watchdog_pass(journal, om.alpaca, s)

    check = journal.one(
        "SELECT * FROM protection_checks WHERE position_id = ? ORDER BY id DESC LIMIT 1", (pos["position_id"],)
    )
    assert check["time_in_force"] == "gtc"
    assert check["tif_appropriate"] == 1


def test_legacy_day_tif_on_multiday_position_flagged_inappropriate():
    """Simulates a row submitted before the TIF fix existed (or with the
    explicit opt-out flag): day-TIF on a >1-day hold is a policy violation,
    even though the legs are still currently live (a forward-looking risk
    flag, not itself a live protection gap)."""
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)
    pos, _ = _open_protected_position(fake, om, journal, "META", 618.78, 594.99, 666.45, 41, max_holding_days=5)
    order = fake._find("META")
    order.time_in_force = "day"
    for leg in order.legs:
        leg.time_in_force = "day"

    pw.run_watchdog_pass(journal, om.alpaca, s)

    check = journal.one(
        "SELECT * FROM protection_checks WHERE position_id = ? ORDER BY id DESC LIMIT 1", (pos["position_id"],)
    )
    assert check["time_in_force"] == "day"
    assert check["tif_appropriate"] == 0
    assert check["protection_status"] == "protected"  # legs ARE still live -- not itself a live gap


def test_allow_day_tif_opt_out_flag_makes_day_tif_appropriate():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, ALLOW_DAY_TIF_FOR_MULTIDAY_POSITIONS="true")
    pos, _ = _open_protected_position(fake, om, journal, "META", 618.78, 594.99, 666.45, 41, max_holding_days=5)

    pw.run_watchdog_pass(journal, om.alpaca, s)

    check = journal.one(
        "SELECT * FROM protection_checks WHERE position_id = ? ORDER BY id DESC LIMIT 1", (pos["position_id"],)
    )
    assert check["time_in_force"] == "day"  # explicit opt-in -> old behavior
    assert check["tif_appropriate"] == 1  # acknowledged, explicit choice -- not flagged as a violation


# --------------------------------------------------------- blocks new entries
def test_unprotected_incident_blocks_new_entries():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)
    _open_protected_position(fake, om, journal, "META", 618.78, 594.99, 666.45, 41, max_holding_days=5)
    fake.expire_leg("META", "stop_loss")
    pw.run_watchdog_pass(journal, om.alpaca, s)

    new_prop = make_proposal(symbol="AAPL", entry=100.0, stop=97.0, target=106.0, qty=10)
    _seed_proposal(journal, new_prop)
    res = om.execute_proposal(new_prop)

    assert res.blocked is True
    assert res.block_reason == ReasonCode.PROTECTION_INTEGRITY_FAILURE.value
    assert not any(o.symbol == "AAPL" for o in fake.orders.values())  # never reached the broker


def test_closed_mismatch_incident_blocks_new_entries():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)
    _open_protected_position(fake, om, journal, "META", 618.78, 594.99, 666.45, 41, max_holding_days=5)
    fake.vanish_position("META")
    pw.run_watchdog_pass(journal, om.alpaca, s)

    new_prop = make_proposal(symbol="AAPL", entry=100.0, stop=97.0, target=106.0, qty=10)
    _seed_proposal(journal, new_prop)
    res = om.execute_proposal(new_prop)

    assert res.blocked is True
    assert res.block_reason == ReasonCode.PROTECTION_INTEGRITY_FAILURE.value


def test_manual_approval_boundary_unchanged_when_no_incident(orchestrator):
    proposal_id, _ = inject_pending_proposal(orchestrator, symbol="AAPL")
    ok, msg = orchestrator.approve_proposal(proposal_id, approver="test")
    assert ok is True  # unchanged: still approves normally when no incident exists


def test_approve_proposal_blocked_by_open_protection_incident(orchestrator):
    _insert_manual_incident(orchestrator.journal, position_id="pos_fake", symbol="META")
    proposal_id, _ = inject_pending_proposal(orchestrator, symbol="AAPL")

    ok, msg = orchestrator.approve_proposal(proposal_id, approver="test")

    assert ok is False
    assert "protection incident" in msg


# ------------------------------------------------------------- safety/scope
def test_real_money_stays_unreachable_even_with_protection_incident():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, REAL_TRADING_ENABLED="true")
    prop = make_proposal(symbol="AAPL")
    _seed_proposal(journal, prop)

    res = om.execute_proposal(prop)

    # real_trading_guard fires FIRST, unconditionally -- before the protection
    # check is even evaluated.
    assert res.blocked is True
    assert res.block_reason == ReasonCode.REAL_TRADING_BLOCKED.value
    assert fake.orders == {}


def test_run_watchdog_pass_works_standalone_no_scheduler():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)
    _open_protected_position(fake, om, journal, "AAPL", 100.0, 97.0, 106.0, 10)

    result = pw.run_watchdog_pass(journal, om.alpaca, s)  # no Orchestrator, no scheduler involved

    assert result["checked"] == 1
    assert journal.query("SELECT * FROM scheduler_runs") == []


# --------------------------------------------------------- reconciliation
def test_manual_external_close_detected_not_auto_closed_then_resolved():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)
    pos, _ = _open_protected_position(fake, om, journal, "META", 618.78, 594.99, 666.45, 41, max_holding_days=5)
    fake.vanish_position("META")

    result = pw.run_watchdog_pass(journal, om.alpaca, s)
    assert result["closed_mismatch"] == 1
    incident_id = result["new_incidents"][0]

    # Watchdog detected it but did NOT close it itself.
    still_open = journal.one("SELECT status FROM positions WHERE position_id = ?", (pos["position_id"],))
    assert still_open["status"] == "open"

    res = pw.resolve_incident(journal, om.positions, incident_id, exit_price=586.70, note="manual flatten confirmed")

    assert res["ok"] is True
    closed = journal.one("SELECT status FROM positions WHERE position_id = ?", (pos["position_id"],))
    assert closed["status"] == "closed"
    exit_row = journal.one("SELECT * FROM exits WHERE position_id = ?", (pos["position_id"],))
    assert exit_row["exit_reason"] == "broker_protection_incident_manual_resolve"
    assert exit_row["classification"] == "risk-control"
    incident_row = journal.one("SELECT * FROM protection_checks WHERE check_id = ?", (incident_id,))
    assert incident_row["resolved_at_utc"] is not None
    assert incident_row["resolution_exit_id"] == exit_row["exit_id"]


def test_resolve_incident_rejects_wrong_incident_type():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)
    _open_protected_position(fake, om, journal, "META", 618.78, 594.99, 666.45, 41, max_holding_days=5)
    fake.expire_leg("META", "stop_loss")
    result = pw.run_watchdog_pass(journal, om.alpaca, s)
    incident_id = result["new_incidents"][0]

    res = pw.resolve_incident(journal, om.positions, incident_id, exit_price=600.0, note="x")

    assert res["ok"] is False
    assert "protection_ack" in res["message"]


def test_protection_ack_unblocks_without_closing_position():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)
    pos, _ = _open_protected_position(fake, om, journal, "META", 618.78, 594.99, 666.45, 41, max_holding_days=5)
    fake.expire_leg("META", "stop_loss")
    result = pw.run_watchdog_pass(journal, om.alpaca, s)
    incident_id = result["new_incidents"][0]

    ack = pw.acknowledge_incident(journal, incident_id, note="aware, monitoring manually, accepting the risk")

    assert ack["ok"] is True
    assert pw.has_blocking_incident(journal) is None
    pos_row = journal.one("SELECT status FROM positions WHERE position_id = ?", (pos["position_id"],))
    assert pos_row["status"] == "open"  # never touched

    new_prop = make_proposal(symbol="AAPL", entry=100.0, stop=97.0, target=106.0, qty=10)
    _seed_proposal(journal, new_prop)
    res = om.execute_proposal(new_prop)
    assert res.blocked is False  # unblocked


def test_watchdog_reconfirms_protection_auto_resolves_incident():
    """Self-healing: if a human fixes protection directly at the broker, the
    next watchdog pass auto-resolves the incident (no manual ack required)."""
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)
    _open_protected_position(fake, om, journal, "META", 618.78, 594.99, 666.45, 41, max_holding_days=5)
    fake.expire_leg("META", "stop_loss")
    pw.run_watchdog_pass(journal, om.alpaca, s)
    assert pw.has_blocking_incident(journal) is not None

    # Human manually re-arms the stop directly at the broker.
    order = fake._find("META")
    for leg in order.legs:
        if leg.order_type == "stop":
            leg.status = "new"

    pw.run_watchdog_pass(journal, om.alpaca, s)

    assert pw.has_blocking_incident(journal) is None
    incidents = journal.query("SELECT * FROM protection_checks WHERE protection_status = 'unprotected'")
    assert all(i["resolved_at_utc"] is not None for i in incidents)
    assert any(i["resolved_by"] == "watchdog_reconfirmed" for i in incidents)


def test_watchdog_never_calls_close_position_on_detection(monkeypatch):
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)
    _open_protected_position(fake, om, journal, "META", 618.78, 594.99, 666.45, 41, max_holding_days=5)
    fake.vanish_position("META")
    _open_protected_position(fake, om, journal, "NVDA", 120.0, 110.0, 140.0, 3)
    fake.expire_leg("NVDA", "stop_loss")

    calls = []
    original = om.positions.close_position

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(om.positions, "close_position", spy)

    pw.run_watchdog_pass(journal, om.alpaca, s)

    assert calls == []


def test_dangling_orders_after_close_detected():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)
    _open_protected_position(fake, om, journal, "AAPL", 100.0, 97.0, 106.0, 10)
    fake.add_stray_order("TSLA")  # a symbol with NO open local position

    result = pw.run_watchdog_pass(journal, om.alpaca, s)

    assert len(result["dangling_orders"]) == 1
    assert result["dangling_orders"][0]["symbol"] == "TSLA"
    events = journal.query("SELECT * FROM system_events WHERE message LIKE '%dangling%'")
    assert len(events) == 1


# ------------------------------------------------------------------ ordering
def test_reconcile_before_watchdog_avoids_false_positive_mismatch():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)
    pos, _ = _open_protected_position(fake, om, journal, "MSFT", 400.0, 390.0, 420.0, 5)
    # TP leg fills THIS pass -- a completely healthy, normal close. The broker's
    # OWN position is also gone now, same as reality.
    fake.fill_leg("MSFT", role="take_profit", price=420.0)
    fake.remove_position("MSFT")
    still_open = journal.one("SELECT status FROM positions WHERE position_id = ?", (pos["position_id"],))
    assert still_open["status"] == "open"

    recon = om.reconcile()  # correct order: reconcile() first
    protection = pw.run_watchdog_pass(journal, om.alpaca, s)

    assert len(recon["exits"]) == 1
    assert protection["checked"] == 0  # nothing open left for the watchdog to (mis)check
    assert protection["closed_mismatch"] == 0


def test_watchdog_before_reconcile_would_false_positive_demonstrating_why_ordering_matters():
    """Illustrates WHY run_monitor_once() must call reconcile() before the
    watchdog pass -- the wrong order (never used in production code) produces
    exactly the false positive the real ordering avoids."""
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)
    _open_protected_position(fake, om, journal, "MSFT", 400.0, 390.0, 420.0, 5)
    fake.fill_leg("MSFT", role="take_profit", price=420.0)
    fake.remove_position("MSFT")

    protection = pw.run_watchdog_pass(journal, om.alpaca, s)  # wrong order, for illustration only

    assert protection["closed_mismatch"] == 1


# ------------------------------------------------------------ restart-recovery
def test_restart_recovery_rediscovers_true_state_from_db():
    """Nothing about protection state may be relied on in memory -- only the DB
    file may bridge across a restart. Uses a temp file-backed SQLite DB (not
    :memory:, which cannot prove anything about restart-recovery: a fresh
    connection to :memory: is just an empty new database)."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = tmp.name
    try:
        fake = FakeTradingClient()
        s = make_settings(
            ALPHAOS_MODE="paper", EXECUTION_PROVIDER="alpaca_paper",
            ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s", ALPACA_PAPER="true",
            ALPACA_BASE_URL="https://paper-api.alpaca.markets", REAL_TRADING_ENABLED="false",
            ALPHAOS_DB_PATH=db_path,
        )

        # --- "session 1": open a broker-managed position, then let it go unprotected.
        journal1 = JournalStore(db_path)
        alpaca1 = AlpacaClient(s, journal1, trading_client=fake)
        om1 = OrderManager(s, journal1, alpaca=alpaca1)
        pos, _ = _open_protected_position(fake, om1, journal1, "META", 618.78, 594.99, 666.45, 41, max_holding_days=5)
        fake.expire_leg("META", "stop_loss")
        fake.expire_leg("META", "take_profit")  # the exact META scenario: BOTH legs gone
        journal1.close()  # simulate process death: connection fully closed

        # --- "session 2": brand-new connection, brand-new everything. Only the
        # fake broker client is reused (it stands in for "the real external
        # broker still has this state" -- not for any AlphaOS-side memory; a
        # real restart would reconnect to the real Alpaca API and see the same
        # broker-side truth).
        journal2 = JournalStore(db_path)
        alpaca2 = AlpacaClient(s, journal2, trading_client=fake)
        om2 = OrderManager(s, journal2, alpaca=alpaca2)

        result = pw.run_watchdog_pass(journal2, om2.alpaca, s)

        assert result["unprotected"] == 1
        row = journal2.one("SELECT * FROM positions WHERE symbol = 'META'")
        assert row["protection_status"] == "unprotected"
        incident = journal2.one(
            "SELECT * FROM protection_checks WHERE symbol = 'META' AND protection_status = 'unprotected' "
            "ORDER BY id DESC LIMIT 1"
        )
        assert incident is not None and incident["resolved_at_utc"] is None
        journal2.close()
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------- reporting
def test_status_report_reflects_blocking_state():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)
    _open_protected_position(fake, om, journal, "META", 618.78, 594.99, 666.45, 41, max_holding_days=5)

    healthy = pw.status_report(journal)
    assert healthy["blocking"] is False
    assert healthy["summary_label"] == "no broker-managed positions"  # no watchdog pass has run yet

    pw.run_watchdog_pass(journal, om.alpaca, s)
    healthy2 = pw.status_report(journal)
    assert healthy2["protected"] == 1
    assert healthy2["blocking"] is False
    assert healthy2["summary_label"] == "all protected"

    fake.expire_leg("META", "stop_loss")
    pw.run_watchdog_pass(journal, om.alpaca, s)
    unhealthy = pw.status_report(journal)
    assert unhealthy["blocking"] is True
    assert unhealthy["open_incident_count"] == 1
    assert "BLOCKED" in unhealthy["summary_label"]
