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
from alphaos.config.settings import SettingsError
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
def test_resolve_tif_boundary_only_pure_intraday_stays_day():
    """Opus audit HIGH-1: max_holding_days==0 (pure intraday, the daytrade
    experiment) is the ONLY case that keeps day-TIF by default. Any swing
    hold (>=1, may cross a session boundary) gets persistent (gtc) protection
    -- the original >1 boundary left 1-day swings exposed to the exact META
    failure mode (day-TIF legs expiring at session close while the position
    is still open overnight)."""
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)

    for max_holding_days, expected_tif in ((0, "day"), (1, "gtc"), (2, "gtc"), (5, "gtc")):
        prop = make_proposal(symbol="AAPL")
        prop.max_holding_days = max_holding_days
        assert om.alpaca._resolve_tif(prop) == expected_tif, f"max_holding_days={max_holding_days}"


def test_resolve_tif_opt_out_flag_allows_day_tif_for_any_holding_period():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, ALLOW_DAY_TIF_FOR_MULTIDAY_POSITIONS="true")

    for max_holding_days in (0, 1, 2, 5):
        prop = make_proposal(symbol="AAPL")
        prop.max_holding_days = max_holding_days
        assert om.alpaca._resolve_tif(prop) == "day", f"max_holding_days={max_holding_days}"


def test_one_day_swing_position_uses_gtc_and_is_appropriate():
    """The exact regression this fix closes: a 1-day swing (still eligible to
    cross a session boundary, unlike the 0-day daytrade experiment) must get
    persistent protection, not day-TIF."""
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)
    pos, _ = _open_protected_position(fake, om, journal, "AAPL", 100.0, 97.0, 106.0, 10, max_holding_days=1)

    pw.run_watchdog_pass(journal, om.alpaca, s)

    check = journal.one(
        "SELECT * FROM protection_checks WHERE position_id = ? ORDER BY id DESC LIMIT 1", (pos["position_id"],)
    )
    assert check["time_in_force"] == "gtc"
    assert check["tif_appropriate"] == 1


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
    explicit opt-out flag): day-TIF on a >=1-day swing hold is a policy
    violation, even though the legs are still currently live (a
    forward-looking risk flag, not itself a live protection gap)."""
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


# =============================================================================
# PR2.6 — Protection Watchdog Hardening
# =============================================================================
def _force_check_error(om, message="flaky broker lookup"):
    """Make every per-order broker lookup fail (simulates a persistent
    per-order API error, distinct from a whole-pass list_positions() outage)."""
    om.alpaca.get_order = lambda boid: (_ for _ in ()).throw(RuntimeError(message))


# --------------------------------------------------------- check_error escalation
def test_single_check_error_surfaces_clearly_but_does_not_block():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, PROTECTION_CHECK_ERROR_ESCALATION_THRESHOLD="3")
    _open_protected_position(fake, om, journal, "NVDA", 120.0, 110.0, 140.0, 3, max_holding_days=5)
    _force_check_error(om)

    result = pw.run_watchdog_pass(journal, om.alpaca, s)

    assert result["check_error"] == 1
    assert result["unverifiable"] == 0
    assert pw.has_blocking_incident(journal) is None  # below threshold -- not blocking
    events = journal.query(
        "SELECT * FROM system_events WHERE category = 'protection_watchdog' AND severity = 'warning'"
    )
    assert any("broker order lookup failed" in e["message"] for e in events)  # surfaced clearly, just not critical


def test_repeated_check_error_escalates_to_blocking_unverifiable():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, PROTECTION_CHECK_ERROR_ESCALATION_THRESHOLD="3")
    _open_protected_position(fake, om, journal, "NVDA", 120.0, 110.0, 140.0, 3, max_holding_days=5)
    _force_check_error(om)

    r1 = pw.run_watchdog_pass(journal, om.alpaca, s)
    r2 = pw.run_watchdog_pass(journal, om.alpaca, s)
    r3 = pw.run_watchdog_pass(journal, om.alpaca, s)

    assert r1["check_error"] == 1 and r1["unverifiable"] == 0
    assert r2["check_error"] == 1 and r2["unverifiable"] == 0
    assert r3["check_error"] == 0 and r3["unverifiable"] == 1  # 3rd consecutive failure crosses the threshold
    blocking = pw.has_blocking_incident(journal)
    assert blocking is not None
    assert blocking["protection_status"] == "unverifiable"
    assert blocking["severity"] == "critical"
    critical_events = journal.query(
        "SELECT * FROM system_events WHERE category = 'protection_watchdog' AND severity = 'critical'"
    )
    assert len(critical_events) == 1  # exactly one CRITICAL alert, fired at the escalation transition


def test_new_entries_blocked_once_protection_unverifiable_beyond_threshold():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, PROTECTION_CHECK_ERROR_ESCALATION_THRESHOLD="2")
    _open_protected_position(fake, om, journal, "NVDA", 120.0, 110.0, 140.0, 3, max_holding_days=5)
    _force_check_error(om)

    pw.run_watchdog_pass(journal, om.alpaca, s)
    pw.run_watchdog_pass(journal, om.alpaca, s)  # 2nd consecutive failure -> escalates

    new_prop = make_proposal(symbol="AAPL", entry=100.0, stop=97.0, target=106.0, qty=10)
    _seed_proposal(journal, new_prop)
    res = om.execute_proposal(new_prop)

    assert res.blocked is True
    assert res.block_reason == ReasonCode.PROTECTION_INTEGRITY_FAILURE.value
    assert not any(o.symbol == "AAPL" for o in fake.orders.values())  # never reached the broker


def test_check_error_streak_survives_the_escalation_status_change():
    """Once escalated, a CONTINUING broker failure must not reset the streak
    back to 1 just because the stored status changed from check_error to
    unverifiable -- it must keep counting and stay escalated."""
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, PROTECTION_CHECK_ERROR_ESCALATION_THRESHOLD="2")
    _open_protected_position(fake, om, journal, "NVDA", 120.0, 110.0, 140.0, 3, max_holding_days=5)
    _force_check_error(om)

    pw.run_watchdog_pass(journal, om.alpaca, s)
    pw.run_watchdog_pass(journal, om.alpaca, s)  # escalates
    r3 = pw.run_watchdog_pass(journal, om.alpaca, s)  # still failing

    assert r3["unverifiable"] == 1
    assert r3["check_error"] == 0
    assert pw.has_blocking_incident(journal) is not None


def test_check_error_recovery_auto_resolves_without_escalating():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, PROTECTION_CHECK_ERROR_ESCALATION_THRESHOLD="3")
    _open_protected_position(fake, om, journal, "NVDA", 120.0, 110.0, 140.0, 3, max_holding_days=5)
    _force_check_error(om)
    pw.run_watchdog_pass(journal, om.alpaca, s)  # 1 failure, below threshold

    del om.alpaca.get_order  # broker lookup recovers -- instance override removed, falls back to the real method

    result = pw.run_watchdog_pass(journal, om.alpaca, s)

    assert result["protected"] == 1
    assert result["unverifiable"] == 0
    assert pw.has_blocking_incident(journal) is None


def test_unverifiable_incident_can_be_acknowledged():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, PROTECTION_CHECK_ERROR_ESCALATION_THRESHOLD="2")
    _open_protected_position(fake, om, journal, "NVDA", 120.0, 110.0, 140.0, 3, max_holding_days=5)
    _force_check_error(om)
    pw.run_watchdog_pass(journal, om.alpaca, s)
    result = pw.run_watchdog_pass(journal, om.alpaca, s)
    incident_id = result["new_incidents"][0]

    ack = pw.acknowledge_incident(journal, incident_id, note="confirmed protected manually at the broker")

    assert ack["ok"] is True
    assert pw.has_blocking_incident(journal) is None


def test_stale_ack_on_unverifiable_does_not_durably_unblock():
    """The ack lifts the block immediately, but if the broker lookup is STILL
    failing, the next watchdog pass must re-escalate and re-block -- an ack is
    a point-in-time human decision, not a permanent override of an ongoing,
    unresolved problem."""
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, PROTECTION_CHECK_ERROR_ESCALATION_THRESHOLD="2")
    _open_protected_position(fake, om, journal, "NVDA", 120.0, 110.0, 140.0, 3, max_holding_days=5)
    _force_check_error(om)
    pw.run_watchdog_pass(journal, om.alpaca, s)
    result = pw.run_watchdog_pass(journal, om.alpaca, s)  # escalates
    incident_id = result["new_incidents"][0]

    ack = pw.acknowledge_incident(journal, incident_id, note="accepting risk for now")
    assert ack["ok"] is True
    assert pw.has_blocking_incident(journal) is None  # ack clears the block immediately

    # Broker lookup is STILL failing -- the underlying problem was never fixed.
    result2 = pw.run_watchdog_pass(journal, om.alpaca, s)

    assert result2["unverifiable"] == 1
    assert len(result2["new_incidents"]) == 1  # a FRESH incident, not a reopened stale one
    assert pw.has_blocking_incident(journal) is not None  # re-blocked


def test_incident_supersession_leaves_at_most_one_open_incident():
    """A position moving from unverifiable -> unprotected (the broker lookup
    recovers and reveals a genuinely missing stop) must supersede the older
    unverifiable incident, not leave it open alongside the new one -- and
    resolving only the newer incident must not be blocked by a stale
    reference to the superseded older one."""
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, PROTECTION_CHECK_ERROR_ESCALATION_THRESHOLD="2")
    pos, _ = _open_protected_position(fake, om, journal, "NVDA", 120.0, 110.0, 140.0, 3, max_holding_days=5)
    _force_check_error(om)
    pw.run_watchdog_pass(journal, om.alpaca, s)
    r_escalate = pw.run_watchdog_pass(journal, om.alpaca, s)  # escalates to unverifiable
    unverifiable_incident_id = r_escalate["new_incidents"][0]

    # Broker lookup recovers, and reveals the stop really is missing.
    del om.alpaca.get_order
    fake.expire_leg("NVDA", "stop_loss")
    r_reveal = pw.run_watchdog_pass(journal, om.alpaca, s)
    unprotected_incident_id = r_reveal["new_incidents"][0]

    assert unprotected_incident_id != unverifiable_incident_id

    rows = journal.query(
        "SELECT check_id, protection_status, resolved_at_utc, resolved_by FROM protection_checks "
        "WHERE position_id = ? ORDER BY id", (pos["position_id"],),
    )
    open_incident_rows = [
        r for r in rows if r["resolved_at_utc"] is None
        and r["protection_status"] in ("unprotected", "closed_mismatch", "unverifiable")
    ]
    assert len(open_incident_rows) == 1  # at most one open incident, ever
    assert open_incident_rows[0]["check_id"] == unprotected_incident_id

    # The older unverifiable incident is SUPERSEDED (resolved), not deleted --
    # it stays in the audit trail with an honest resolution reason.
    old = journal.one("SELECT * FROM protection_checks WHERE check_id = ?", (unverifiable_incident_id,))
    assert old["resolved_at_utc"] is not None
    assert old["resolved_by"] == "watchdog_superseded"

    # Resolving the NEWER incident must not be blocked by any stale reference
    # to the old, already-superseded one.
    ack = pw.acknowledge_incident(journal, unprotected_incident_id, note="manually confirmed protection restored")
    assert ack["ok"] is True
    assert pw.has_blocking_incident(journal) is None


# ------------------------------------------------------------- qty mismatch
def test_qty_mismatch_reported_in_summary_and_logged():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)
    pos, _ = _open_protected_position(fake, om, journal, "AAPL", 100.0, 97.0, 106.0, 10, max_holding_days=5)
    fake.set_position("AAPL", qty=7, avg_entry_price=100.0)  # broker now shows fewer shares than local

    result = pw.run_watchdog_pass(journal, om.alpaca, s)

    assert result["qty_mismatches"] == 1
    check = journal.one(
        "SELECT * FROM protection_checks WHERE position_id = ? ORDER BY id DESC LIMIT 1", (pos["position_id"],)
    )
    assert check["qty_match"] == 0
    assert check["local_qty"] == 10 and check["broker_qty"] == 7
    events = journal.query("SELECT * FROM system_events WHERE message LIKE '%quantity mismatch%'")
    assert len(events) == 1

    report = pw.status_report(journal)
    assert report["qty_mismatches"] == 1
    assert report["blocking"] is False  # visible, not blocking -- protection legs are still live


def test_qty_mismatch_does_not_spam_every_pass():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)
    _open_protected_position(fake, om, journal, "AAPL", 100.0, 97.0, 106.0, 10, max_holding_days=5)
    fake.set_position("AAPL", qty=7, avg_entry_price=100.0)

    pw.run_watchdog_pass(journal, om.alpaca, s)
    pw.run_watchdog_pass(journal, om.alpaca, s)
    pw.run_watchdog_pass(journal, om.alpaca, s)

    events = journal.query("SELECT * FROM system_events WHERE message LIKE '%quantity mismatch%'")
    assert len(events) == 1  # one alert on the transition, not one per pass


def test_short_position_qty_sign_handled_correctly():
    """Alpaca reports a NEGATIVE qty for short positions; the local ledger
    always stores a positive magnitude -- comparing signed values directly
    would falsely report a mismatch on every healthy short position."""
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)
    prop = make_proposal(symbol="TSLA", direction="short", entry=200.0, stop=210.0, target=180.0, qty=5)
    prop.max_holding_days = 5
    _seed_proposal(journal, prop)
    om.execute_proposal(prop)
    fake.fill_entry("TSLA", price=200.0)
    om.reconcile()
    fake.set_position("TSLA", qty=-5, side="short", avg_entry_price=200.0)  # Alpaca convention

    result = pw.run_watchdog_pass(journal, om.alpaca, s)

    assert result["qty_mismatches"] == 0
    check = journal.one("SELECT * FROM protection_checks WHERE symbol = 'TSLA' ORDER BY id DESC LIMIT 1")
    assert check["qty_match"] == 1


def test_short_position_genuine_qty_mismatch_still_detected():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)
    prop = make_proposal(symbol="TSLA", direction="short", entry=200.0, stop=210.0, target=180.0, qty=5)
    prop.max_holding_days = 5
    _seed_proposal(journal, prop)
    om.execute_proposal(prop)
    fake.fill_entry("TSLA", price=200.0)
    om.reconcile()
    fake.set_position("TSLA", qty=-3, side="short", avg_entry_price=200.0)  # broker shows fewer shares (magnitude 3 vs 5)

    result = pw.run_watchdog_pass(journal, om.alpaca, s)

    assert result["qty_mismatches"] == 1


# --------------------------------------------------------------- TIF hardening
def test_contradictory_tif_config_rejected_at_settings_load():
    with pytest.raises(SettingsError, match="PROTECTIVE_ORDER_TIME_IN_FORCE"):
        make_settings(
            ALPHAOS_MODE="paper", EXECUTION_PROVIDER="alpaca_paper",
            ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s", ALPACA_PAPER="true",
            ALPACA_BASE_URL="https://paper-api.alpaca.markets",
            PROTECTIVE_ORDER_TIME_IN_FORCE="day", ALLOW_DAY_TIF_FOR_MULTIDAY_POSITIONS="false",
        )


def test_tif_day_with_explicit_opt_out_still_allowed():
    s = make_settings(
        ALPHAOS_MODE="paper", EXECUTION_PROVIDER="alpaca_paper",
        ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s", ALPACA_PAPER="true",
        ALPACA_BASE_URL="https://paper-api.alpaca.markets",
        PROTECTIVE_ORDER_TIME_IN_FORCE="day", ALLOW_DAY_TIF_FOR_MULTIDAY_POSITIONS="true",
    )
    assert s.protective_order_time_in_force == "day"
    assert s.allow_day_tif_for_multiday_positions is True


def test_max_holding_days_none_defaults_to_persistent_tif():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)
    prop = make_proposal(symbol="AAPL")
    prop.max_holding_days = None

    assert om.alpaca._resolve_tif(prop) == "gtc"


def test_max_holding_days_none_even_with_opt_out_flag_stays_persistent():
    """The opt-out flag is an INFORMED choice about identified swing holds --
    an unknown holding period is a defensive/anomalous case, not an informed
    swing decision, so it must stay safe regardless of the flag."""
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, ALLOW_DAY_TIF_FOR_MULTIDAY_POSITIONS="true")
    prop = make_proposal(symbol="AAPL")
    prop.max_holding_days = None

    assert om.alpaca._resolve_tif(prop) == "gtc"


def test_max_holding_days_none_flagged_inappropriate_if_observed_day():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)
    pos, prop = _open_protected_position(fake, om, journal, "AAPL", 100.0, 97.0, 106.0, 10, max_holding_days=5)
    # Simulate an order whose recorded holding period is unknown (defensive
    # path) but whose broker legs are day-TIF -- should be flagged, not
    # silently treated as fine just because max_holding_days is missing.
    journal.conn.execute(
        "UPDATE positions SET max_holding_days = NULL WHERE position_id = ?", (pos["position_id"],)
    )
    journal.conn.commit()
    order = fake._find("AAPL")
    order.time_in_force = "day"
    for leg in order.legs:
        leg.time_in_force = "day"

    pw.run_watchdog_pass(journal, om.alpaca, s)

    check = journal.one(
        "SELECT * FROM protection_checks WHERE position_id = ? ORDER BY id DESC LIMIT 1", (pos["position_id"],)
    )
    assert check["tif_appropriate"] == 0


# ------------------------------------------------------------------ safety
def test_no_orders_submitted_during_check_error_escalation():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, PROTECTION_CHECK_ERROR_ESCALATION_THRESHOLD="1")
    _open_protected_position(fake, om, journal, "NVDA", 120.0, 110.0, 140.0, 3, max_holding_days=5)
    orders_before = len(fake.orders)
    _force_check_error(om)

    pw.run_watchdog_pass(journal, om.alpaca, s)  # escalates immediately (threshold=1)

    assert len(fake.orders) == orders_before  # the watchdog itself submitted nothing


def test_no_auto_close_during_check_error_escalation(monkeypatch):
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, PROTECTION_CHECK_ERROR_ESCALATION_THRESHOLD="1")
    _open_protected_position(fake, om, journal, "NVDA", 120.0, 110.0, 140.0, 3, max_holding_days=5)
    _force_check_error(om)
    calls = []
    monkeypatch.setattr(om.positions, "close_position", lambda *a, **kw: calls.append((a, kw)))

    pw.run_watchdog_pass(journal, om.alpaca, s)

    assert calls == []


def test_real_money_stays_unreachable_during_unverifiable_block():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, REAL_TRADING_ENABLED="true")
    # An open, escalated 'unverifiable' incident (injected directly -- opening a
    # real position isn't possible under REAL_TRADING_ENABLED=true in the first
    # place, which is itself part of the invariant being tested).
    journal.insert("protection_checks", {
        "check_id": new_id("pcheck"), "position_id": "pos_fake", "symbol": "NVDA",
        "protection_status": "unverifiable", "severity": "critical",
        "detail": "test-injected unverifiable incident",
    })
    prop = make_proposal(symbol="AAPL")
    _seed_proposal(journal, prop)

    res = om.execute_proposal(prop)

    # real_trading_guard fires FIRST, unconditionally -- before the protection
    # check is even evaluated, regardless of any watchdog state.
    assert res.blocked is True
    assert res.block_reason == ReasonCode.REAL_TRADING_BLOCKED.value


def test_manual_approval_boundary_unchanged_when_unverifiable(orchestrator):
    """Mirrors test_approve_proposal_blocked_by_open_protection_incident but
    for the new unverifiable incident type -- confirms the same early-check
    wiring covers it without any change to the freshness/risk re-check logic."""
    orchestrator.journal.insert("protection_checks", {
        "check_id": new_id("pcheck"), "position_id": "pos_fake", "symbol": "META",
        "protection_status": "unverifiable", "severity": "critical",
        "detail": "test-injected unverifiable incident",
    })
    proposal_id, _ = inject_pending_proposal(orchestrator, symbol="AAPL")

    ok, msg = orchestrator.approve_proposal(proposal_id, approver="test")

    assert ok is False
    assert "protection incident" in msg
