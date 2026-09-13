"""New-surface probes on 742312f (entry intent recovery, lifecycle edge cases)."""
import sys, os
sys.path.insert(0, os.path.join(os.environ["ALPHAOS_REPO"], "tests"))
from datetime import timedelta
import pytest
from requests import ConnectionError as ReqConnectionError

from alphaos.execution import ledger_ops, protection_watchdog
from alphaos.execution import time_exit_lifecycle as tel
from alphaos.execution.entry_submission import pending_entry_intent
from alphaos.risk.account_exposure import read_exposure, ExposureUnavailable
from alphaos.journal.journal_store import JournalStore
from alphaos.safety import KillSwitch
from alphaos.util import alerts, timeutils
from conftest import make_proposal
from test_entry_submission_recovery import setup as entry_setup, NotFound, paper_row
from test_alpaca_paper_execution import _seed_proposal
from test_time_exit_enforcement import (
    FakeTradingClient, _paper_om, _open_broker_position, _opened_date_for, _NOW_DATE, _NOW,
)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(tel, "CANCEL_VERIFY_DELAY_SECONDS", 0)


# ---------------------------------------------------------------- N7/N10: entry intent that never reached the broker
def test_n7_post_never_reached_broker_halts_all_entries_with_no_operator_exit(tmp_path, monkeypatch):
    fake, settings, journal, manager, proposal = entry_setup(tmp_path, monkeypatch)
    # connection refused BEFORE the request leaves; broker genuinely never saw it -> 404 forever
    def refuse(spec):
        raise ReqConnectionError("connection refused")
    fake.before_post = refuse
    r = manager.execute_proposal(proposal)
    assert r.submission_uncertain and not r.blocked
    assert fake.submits == 1
    # 20 reconcile passes (5 hours of monitor ticks): still pending, entries still blocked
    for _ in range(20):
        assert manager.reconcile()["errors"]
    assert pending_entry_intent(journal) is not None
    assert ledger_ops.metadata(paper_row(journal))["phase"] == "unknown"
    # a NEW proposal is blocked at the funnel...
    other = make_proposal(symbol="ABC", entry=50, stop=48, target=54, qty=10)
    _seed_proposal(journal, other)
    r2 = manager.execute_proposal(other)
    assert r2.blocked and "unverified" in (r2.detail or "")
    assert fake.submits == 1
    # ...and at the fresh-exposure gate used by proposal generation
    with pytest.raises(ExposureUnavailable):
        read_exposure(manager.alpaca, journal)
    # Operator resolves the protection incident the documented way -- the gate is unaffected
    inc = journal.one("SELECT check_id FROM protection_checks WHERE resolved_at_utc IS NULL ORDER BY id DESC")
    protection_watchdog._mark_resolved(journal, inc["check_id"], resolved_by="operator", note="confirmed never sent")
    assert pending_entry_intent(journal) is not None          # still halted
    r3 = manager.execute_proposal(other)
    assert r3.blocked and fake.submits == 1


# ---------------------------------------------------------------- N5: tp fully fills while we are cancelling
def test_n5_full_fill_during_cancellation_is_booked_as_target_not_time_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(alerts, "send_alert", lambda *a, **k: True)
    fake = FakeTradingClient(); settings, journal, manager = _paper_om(fake)
    pos, boid = _open_broker_position(journal, fake, qty=10, opened_market_date=_opened_date_for(_NOW_DATE, 5))
    tp = fake.leg("XLV", "take_profit")
    orig = fake.cancel_order_by_id
    def cancel_then_fill(oid):
        if oid == tp.id:
            tp.status, tp.filled_qty, tp.filled_avg_price, tp.filled_at = "filled", 10, 112.0, "2026-07-14T14:00:00Z"
            fake.set_broker_qty("XLV", 0)
            return
        orig(oid)
    fake.cancel_order_by_id = cancel_then_fill
    r = manager.enforce_time_exits(now=_NOW)
    assert len(r["closed"]) == 1 and r["errors"] == []
    assert not any(op == "close" for op, _ in fake.call_log)
    assert journal.one("SELECT exit_reason FROM exits")["exit_reason"] == "target"
    assert journal.one("SELECT status FROM positions")["status"] == "closed"
    row = journal.one("SELECT raw_request_json FROM paper_orders WHERE order_type='market'")
    assert ledger_ops.metadata(row)["phase"] == "terminal"


# ---------------------------------------------------------------- N11: monitor owns the account at submission time
def test_n11_entry_during_monitor_ownership_is_permanently_blocked_not_deferred(tmp_path, monkeypatch):
    """Orchestrator._execute returns blocked when the monitor lock is held; the auto path then
    marks the proposal BLOCKED (no retry). Window ~4 s per 15 min today."""
    from alphaos.orchestrator import Orchestrator
    from alphaos.scheduler.ownership import worker_ownership
    from conftest import make_settings
    j = JournalStore(str(tmp_path / "ledger.db"))
    orch = Orchestrator(settings=make_settings(), journal=j)
    from types import SimpleNamespace
    orch.orders = SimpleNamespace(real_paper=True, alpaca=object(), _entry_result=lambda row: None)
    import threading
    held = threading.Event(); release = threading.Event()
    def hold():
        with worker_ownership(SimpleNamespace(db_path=j.db_path), "monitor"):
            held.set(); release.wait(5)
    t = threading.Thread(target=hold); t.start(); held.wait(5)
    try:
        res = orch._execute(make_proposal(qty=10))
    finally:
        release.set(); t.join()
    assert res.blocked and "monitor owns" in res.detail
    j.close()
