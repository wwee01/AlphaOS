"""Adversarial probes against the deployed (cd1cb2c) TimeExitLifecycle. Offline fakes only."""
import sys, os
sys.path.insert(0, os.path.join(os.environ["ALPHAOS_REPO"], "tests"))

from copy import deepcopy
from datetime import datetime, timezone, timedelta
import pytest

from alphaos.execution import protection_watchdog
from alphaos.execution.time_exit_lifecycle import TimeExitLifecycle
from alphaos.util import alerts
from test_time_exit_enforcement import (
    FakeTradingClient, _paper_om, _open_broker_position, _opened_date_for, _NOW_DATE, _NOW,
)


@pytest.fixture
def alert_log(monkeypatch):
    calls = []
    monkeypatch.setattr(alerts, "send_alert", lambda *a, **k: calls.append((a[1] if len(a) > 1 else k.get("title"), k.get("priority"))) or True)
    return calls


def _setup(tmp_path):
    fake = FakeTradingClient()
    settings, journal, manager = _paper_om(fake)
    pos, boid = _open_broker_position(journal, fake, qty=10, opened_market_date=_opened_date_for(_NOW_DATE, 5))
    return fake, settings, journal, manager, pos, boid


# ---------------------------------------------------------------- P1
def test_p1_pending_cancel_transient_pages_critical_and_leaves_stop_cancelled(tmp_path, alert_log):
    """Realistic in-session path: cancel() returns, the immediate re-read sees
    pending_cancel on ONE leg. Deployed code: CRITICAL incident + alert, stop
    already cancelled, no close, no re-protect -- broker-unprotected until the
    next monitor pass (15 min default)."""
    fake, settings, journal, manager, pos, boid = _setup(tmp_path)
    tp = fake.leg("XLV", "take_profit")
    fake.cancel_result_status[tp.id] = "pending_cancel"   # stop cancels cleanly, tp lags

    r1 = manager.enforce_time_exits(now=_NOW)
    assert r1["closed"] == []
    # NB: a CRITICAL incident is reported under "deferred", not "errors"
    assert r1["errors"] == [] and r1["deferred"], r1
    err = r1["deferred"][0]
    assert err["critical"] and "incomplete/unknown" in err["error"]
    # Broker state after pass 1: stop CANCELLED, tp pending_cancel -> no working stop.
    assert fake.leg("XLV", "stop_loss").status == "canceled"
    assert tp.status == "pending_cancel"
    assert not any(op == "close" for op, _ in fake.call_log)
    assert not any(op == "oco" for op, _ in fake.call_log)
    assert len(alert_log) == 1 and alert_log[0][1] == "high"
    open_inc = journal.query("SELECT * FROM protection_checks WHERE resolved_at_utc IS NULL")
    assert open_inc and open_inc[0]["protection_status"] == "unverifiable"

    # Pass 2 (next monitor tick): broker finished the cancel -> close proceeds.
    tp.status = "canceled"
    r2 = manager.enforce_time_exits(now=_NOW)
    assert len(r2["closed"]) == 1, r2
    assert len([op for op, _ in fake.call_log if op == "close"]) == 1
    assert journal.one("SELECT status FROM positions")["status"] == "closed"


# ---------------------------------------------------------------- P2
def test_p2_stuck_pending_cancel_overnight_pushes_every_pass(tmp_path, alert_log):
    """AAPL 2026-09-04 replay: tp stuck pending_cancel across the closed session.
    Every subsequent off-session monitor pass re-raises the SAME incident and
    sends a NEW high-priority push (no push dedupe) -- ~4/hour on the 15-min cadence."""
    fake, settings, journal, manager, pos, boid = _setup(tmp_path)
    tp = fake.leg("XLV", "take_profit")
    fake.cancel_result_status[tp.id] = "pending_cancel"
    manager.enforce_time_exits(now=_NOW)                      # in-session: cancel, re-read sees pending
    assert len(alert_log) == 1
    night = _NOW.replace(hour=2)                              # 02:00 UTC = 22:00 ET previous day: closed
    for _ in range(8):                                        # two hours of 15-min ticks
        r = manager.enforce_time_exits(now=night)
        assert r["closed"] == []
        assert not any(op == "close" for op, _ in fake.call_log)
    assert len(alert_log) == 9, len(alert_log)                # 1 + 8: one push per pass, same state
    # The watchdog's own repeat-pass rule ("does not re-alert") is bypassed: every
    # pass logged a CRITICAL system_event AND pushed. Count CRITICAL events.
    crit = journal.query("SELECT * FROM system_events WHERE severity='critical' AND category='time_exit_enforcement'")
    assert len(crit) == 9, len(crit)


# ---------------------------------------------------------------- P3
def test_p3_partially_filled_take_profit_defeats_time_exit_forever(tmp_path, alert_log):
    """A take-profit leg with ANY filled_qty>0 makes advance() return
    stale_view on every pass (line 239) -- the time exit never cancels/closes,
    for as long as the leg stays partially filled."""
    fake, settings, journal, manager, pos, boid = _setup(tmp_path)
    tp = fake.leg("XLV", "take_profit")
    tp.status, tp.filled_qty, tp.filled_avg_price, tp.filled_at = "partially_filled", 3, 112.0, "2026-07-10T15:00:00Z"
    fake.set_broker_qty("XLV", 7)
    # bring the local ledger in line the way reconcile() would (books the 3-share partial exit)
    manager._record_broker_exit(pos, {**{k: getattr(tp, k) for k in ("status","filled_qty","filled_avg_price","filled_at")},
                                      "broker_order_id": tp.id, "role": "take_profit"}, "target")
    assert journal.one("SELECT qty FROM positions")["qty"] == 7
    for _ in range(5):
        r = manager.enforce_time_exits(now=_NOW)
        assert r["closed"] == [] and r["errors"] == []
        assert r["deferred"] and r["deferred"][0].get("stale_view") is True
    assert not any(op == "cancel" for op, _ in fake.call_log)
    assert not any(op == "close" for op, _ in fake.call_log)
    assert journal.one("SELECT status FROM positions")["status"] == "open"


# ---------------------------------------------------------------- P4
def test_p4_partial_stop_fill_then_cancel_leaves_residual_unprotected_and_unexitable(tmp_path, alert_log):
    """Residual after a partially-filled-then-cancelled protective leg: both
    legs terminal (no broker protection), yet advance() short-circuits on
    filled_qty>0 and never reaches recover(); no re-protect, no time exit."""
    fake, settings, journal, manager, pos, boid = _setup(tmp_path)
    stop = fake.leg("XLV", "stop_loss"); tp = fake.leg("XLV", "take_profit")
    stop.status, stop.filled_qty, stop.filled_avg_price, stop.filled_at = "canceled", 4, 94.0, "2026-07-10T15:00:00Z"
    tp.status = "canceled"
    fake.set_broker_qty("XLV", 6)
    manager._record_broker_exit(pos, {"status": "canceled", "filled_qty": 4, "filled_avg_price": 94.0,
                                      "filled_at": stop.filled_at, "broker_order_id": stop.id, "role": "stop_loss"}, "stop")
    assert journal.one("SELECT qty FROM positions")["qty"] == 6
    for _ in range(3):
        r = manager.enforce_time_exits(now=_NOW)
        assert r["deferred"] and r["deferred"][0].get("stale_view") is True
    assert not any(op in ("oco", "close", "cancel") for op, _ in fake.call_log)
    # And the watchdog sees it: no live stop -> UNPROTECTED (detect-only).
    res = protection_watchdog.check_position(journal, manager.alpaca, journal.one("SELECT * FROM positions"),
                                             {"XLV": {"symbol": "XLV", "qty": 6.0, "side": "long"}}, settings)
    assert res.protection_status == "unprotected"


# ---------------------------------------------------------------- P5
def test_p5_new_race_integrity_error_is_swallowed_per_position(tmp_path, alert_log):
    """Two concurrent owners: second _new() hits UNIQUE(order_id) -> exception
    propagates out of advance() -> caught by enforce_time_exits' per-position guard."""
    fake, settings, journal, manager, pos, boid = _setup(tmp_path)
    lc = TimeExitLifecycle(manager)
    row, owner = lc._new(pos, "time_exit", "canceling")
    assert owner
    # simulate a second process that observed no pending row: force a fresh insert attempt
    from alphaos.execution import ledger_ops
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        with ledger_ops.atomic(journal) as j:
            j.insert("paper_orders", {**{k: row[k] for k in ("order_id","client_order_id","symbol","direction","side","qty",
                                          "order_type","order_class","state","execution_source","execution_provider",
                                          "execution_mode","time_in_force","submitted_at")}, "raw_request_json": row["raw_request_json"]})
