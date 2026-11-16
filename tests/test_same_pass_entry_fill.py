"""F1: real exposure reads with a fake paper broker and a temporary ledger.

The delayed-acceptance fixture follows Claude's retained November 15 probes.
No monitor, cancellation, exit or network operation is needed for the fix.
"""
from datetime import datetime, timedelta, timezone
import json
import socket
from concurrent.futures import ThreadPoolExecutor

import pytest

from alphaos.broker.alpaca_client import AlpacaClient
from alphaos.execution import ledger_ops
from alphaos.execution.entry_submission import EntrySubmission
from alphaos.execution.order_manager import OrderManager
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator, ScanSummary
from alphaos.risk.account_exposure import (
    ExposureUnavailable, ExposureUnreconciled, read_exposure, summarize_exposure,
)
from alphaos.util import timeutils
from conftest import make_settings
from test_entry_submission_recovery import RecoverableBroker
from test_entry_deferral import held_monitor
from test_explore_policy import candidate, evaluation, label


START = datetime(2026, 11, 16, 14, 36, tzinfo=timezone.utc)
ARMS = [("c", "AMZN", "watch", "watch"), ("a", "AAPL", "propose", "watch"),
        ("b", "MSFT", "watch", "propose")]


class DelayedFillBroker(RecoverableBroker):
    def __init__(self):
        super().__init__()
        self.instant, self.unfilled, self.after_fill = True, [], None

    def submit(self, spec):
        order = super().submit(spec)
        if self.instant:
            self.unfilled.append(order)
        return order

    def get_account(self):
        while self.unfilled:
            order = self.unfilled.pop(0)
            self.fill(order)
            if self.after_fill:
                self.after_fill(order)
        return {"equity": 95000.0, "last_equity": 95000.0, "cash": 95000.0,
                "trading_blocked": False, "status": "ACTIVE"}

    def fill(self, order, qty=None):
        qty = order.qty if qty is None else qty
        order.status = "filled" if qty == order.qty else "partially_filled"
        order.filled_qty, order.filled_avg_price = qty, order.limit_price
        order.filled_at = "2026-11-16T14:36:01Z"
        self.set_position(order.symbol, qty, avg_entry_price=order.limit_price)
        for leg in order.legs:
            leg.qty = qty
        order.legs[1].status = "held"
        self._stray_orders[order.legs[0].id] = order.legs[0]

    def get_orders(self):
        return list(self._stray_orders.values()) + [
            o for o in self.orders.values() if o.status in {"accepted", "partially_filled"}]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("offline fixture attempted network")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr("alphaos.util.alerts.send_alert", lambda *a, **k: None)
    clock = [START]
    monkeypatch.setattr(timeutils, "now_utc", lambda: clock[0])
    settings = make_settings(
        ALPHAOS_MODE="paper", EXECUTION_PROVIDER="alpaca_paper", ALPACA_API_KEY="k",
        ALPACA_SECRET_KEY="s", ALPACA_PAPER="true", REAL_TRADING_ENABLED="false",
        OFFLINE_MODE="true", EXPLORE_1_ENABLED="true", MAX_OPEN_POSITIONS="20",
        MAX_RISK_PER_TRADE_PCT=".0025", MAX_AUTO_APPROVALS_PER_DAY="20",
        MAX_UNATTENDED_APPROVALS_PER_DAY="20", MAX_PAPER_TRADES_PER_DAY="20",
        APPROVAL_MODE="auto", REQUIRE_MANUAL_APPROVAL="false", ACTIVE_CARD_ID="catalyst_momentum_v3",
        ENTRY_ORDER_STALENESS_ENABLED="false", TIME_EXIT_ENFORCEMENT_ENABLED="false",
        SCHEDULER_SCAN_WINDOWS="09:35-09:50,11:30-12:00,15:45-16:00",
        PROPOSAL_TTL_RTH_SECONDS="1800", MAX_PRICE_DRIFT_BPS_SINCE_PROPOSAL="50")
    journal = JournalStore(str(tmp_path / "f1.db"))
    orch = Orchestrator(settings=settings, journal=journal)
    orch.startup()
    fake = DelayedFillBroker()
    orch.orders.alpaca = AlpacaClient(settings, journal, trading_client=fake)
    quote = orch.market.get_snapshot
    monkeypatch.setattr(orch.market, "get_snapshot", lambda s: {
        **quote(s), "last_price": 100.0, "bid": 99.99, "ask": 100.01})
    journal.insert("job_runs", {"job_run_id": "monitor", "job_type": "monitor", "lock_key": "m",
                               "status": "completed", "started_at_utc": START.isoformat(),
                               "started_at_sgt": timeutils.stamp(START).local_sgt,
                               "finished_at_utc": START.isoformat(),
                               "result_summary_json": {"worker_role": "monitor"}})
    yield orch, journal, fake, clock
    journal.close()


def route(orch, rows=ARMS, batch="s"):
    j = orch.journal
    j.insert("scan_batches", {"scan_batch_id": batch, "status": "started"})
    triples = []
    for cid, symbol, a, b in rows:
        c, e = candidate(cid, symbol, scan_batch_id=batch), evaluation(cid, symbol, a)
        j.insert("candidates", c.row)
        j.insert("openai_evaluations", e.to_row())
        triples.append((c, e, label(b, candidate_id=cid, symbol=symbol)))
    summary = ScanSummary(batch)
    orch._route_campaign(triples, summary, batch, START.isoformat(), False)
    return summary


def test_verified_same_pass_fills_allow_all_staged_arms_without_monitor(setup, monkeypatch):
    orch, j, fake, _ = setup
    monkeypatch.setattr(orch.orders, "reconcile", lambda: pytest.fail("broad reconciliation"))
    summary = route(orch)
    assert (fake.submits, summary.auto_submitted, summary.entry_deferred) == (3, 3, 0)
    assert j.count_open_positions() == 2  # Last accepted response has not filled yet.
    assert j.count_rows("approvals") == 3
    assert j.count_rows("paper_fills") == 2
    assert {p["arm"] for p in j.open_positions()} == {"C", "A"}
    assert {p["policy_version"] for p in j.open_positions()} == {"explore_1"}
    assert orch._pass_entry_order_ids is None


def test_unfilled_working_entries_do_not_create_positions(setup):
    orch, j, fake, _ = setup
    fake.instant = False
    summary = route(orch)
    assert (fake.submits, summary.auto_submitted, summary.entry_deferred) == (3, 3, 0)
    assert j.count_rows("positions") == j.count_rows("paper_fills") == 0


@pytest.mark.parametrize("change", ["identity", "protection", "external", "unknown"])
def test_unproved_or_ambiguous_fill_never_books_or_permits_next_submit(setup, change):
    orch, j, fake, _ = setup
    def after(order):
        if change == "identity":
            order.client_order_id = "foreign"
        elif change == "protection":
            order.legs[1].status = "canceled"
        elif change == "external":
            fake.set_position("OTHER", 1)
        else:
            row = j.one("SELECT * FROM paper_orders")
            ledger_ops.update(j, "paper_orders", "order_id", row["order_id"], {
                "raw_request_json": {**ledger_ops.metadata(row), "phase": "unknown"}})
    fake.after_fill = after
    route(orch)
    assert fake.submits == 1 and j.count_rows("approvals") == 1
    # External exposure may appear after the own fill is booked, but never
    # authorizes another entry or creates an unrelated local origin.
    assert j.count_rows("positions") <= (1 if change == "external" else 0)
    assert not j.one("SELECT * FROM positions WHERE symbol='OTHER'")
    assert orch._pass_entry_order_ids is None


def test_partial_parent_is_not_promoted_to_verified_protection(setup, monkeypatch):
    orch, j, fake, _ = setup
    original = fake.fill
    monkeypatch.setattr(fake, "fill", lambda order: original(order, order.qty / 2))
    route(orch)
    assert fake.submits == 1 and j.count_rows("paper_fills") == 0
    with pytest.raises(ExposureUnavailable):
        read_exposure(orch.orders.alpaca, j)
    first = next(iter(fake.orders.values()))
    row = j.one("SELECT * FROM paper_orders WHERE broker_order_id=?", (first.id,))
    norm = orch.orders.alpaca.get_order(first.id)
    amounts = summarize_exposure(fake.get_account(), orch.orders.alpaca.list_positions(),
                                 [norm], {first.id: row})
    assert amounts["pending_entry_notional"] == first.qty / 2 * first.limit_price


def test_previously_booked_partial_advances_only_delta_once_fully_protected(setup):
    orch, j, fake, _ = setup
    def earlier_partial(order):
        if j.count_rows("positions") == 0:
            row = j.one("SELECT * FROM paper_orders WHERE broker_order_id=?", (order.id,))
            norm = orch.orders.alpaca.get_order(order.id)
            orch.orders._open_real_position(row, {**norm, "filled_qty": order.qty / 2})
    fake.after_fill = earlier_partial
    route(orch)
    assert fake.submits == 3 and j.count_rows("paper_fills") == 3
    first = next(iter(fake.orders.values()))
    row = j.one("SELECT * FROM paper_orders WHERE broker_order_id=?", (first.id,))
    norm = orch.orders.alpaca.get_order(first.id)
    orch.orders._open_real_position(row, norm)
    orch.orders._open_real_position(row, norm)
    fills = j.query("SELECT * FROM paper_fills WHERE order_id=? ORDER BY id", (row["order_id"],))
    assert [f["qty"] for f in fills] == [first.qty / 2, first.qty / 2]
    with pytest.raises(ValueError, match="backwards"):
        orch.orders._open_real_position(row, {**norm, "filled_qty": first.qty / 2})
    assert j.query("SELECT * FROM paper_fills WHERE order_id=? ORDER BY id", (row["order_id"],)) == fills


@pytest.mark.parametrize("mutation", ["closed", "intent", "proposal", "position"])
def test_local_change_before_atomic_booking_refuses_without_new_fill(setup, monkeypatch, mutation):
    orch, j, fake, _ = setup
    original = orch.orders._book_verified_entry_fill
    def book(proof):
        row = proof["order"]
        if mutation in {"closed", "position"}:
            orch.orders._open_real_position(row, proof["parent"])
            if mutation == "closed":
                j.conn.execute("UPDATE positions SET status='closed' WHERE order_id=?", (row["order_id"],))
                j.conn.commit()
        elif mutation == "intent":
            ledger_ops.update(j, "paper_orders", "order_id", row["order_id"], {
                "raw_request_json": {**ledger_ops.metadata(row), "phase": "unknown"}})
        else:
            j.conn.execute("UPDATE trade_proposals SET status='blocked' WHERE proposal_id=?", (row["proposal_id"],))
            j.conn.commit()
        before = (j.query("SELECT * FROM paper_fills"), j.query("SELECT * FROM positions"))
        with pytest.raises(ValueError):
            original(proof)
        assert (j.query("SELECT * FROM paper_fills"), j.query("SELECT * FROM positions")) == before
        raise ValueError("concurrent state change")
    monkeypatch.setattr(orch.orders, "_book_verified_entry_fill", book)
    route(orch, ARMS[:2])
    assert fake.submits == 1 and j.count_rows("approvals") == 1


def test_fresh_read_failure_after_booking_does_not_submit(setup, monkeypatch):
    orch, j, fake, _ = setup
    original = orch.orders._book_verified_entry_fill
    def book(proof):
        result = original(proof)
        monkeypatch.setattr(fake, "get_account", lambda: (_ for _ in ()).throw(ValueError("read failed")))
        return result
    monkeypatch.setattr(orch.orders, "_book_verified_entry_fill", book)
    route(orch)
    assert fake.submits == 1 and j.count_open_positions() == 1
    assert j.count_rows("paper_fills") == j.count_rows("approvals") == 1


def test_ttl_crossing_while_booking_does_not_grant_late_approval(setup, monkeypatch):
    orch, j, fake, clock = setup
    original = orch.orders._book_verified_entry_fill
    def book(proof):
        result = original(proof)
        clock[0] += timedelta(seconds=1801)
        return result
    monkeypatch.setattr(orch.orders, "_book_verified_entry_fill", book)
    route(orch)
    assert fake.submits == 1 and j.count_rows("approvals") == 1
    assert j.count_open_positions() == 1


def test_same_symbol_cap_remains_after_own_fill_booking(setup):
    orch, j, fake, _ = setup
    route(orch)
    fake.unfilled.clear()  # Leave the last entry accepted; AAPL is already booked.
    before = fake.submits
    summary = route(orch, [("a2", "AAPL", "propose", "watch")], batch="duplicate")
    assert fake.submits == before and summary.auto_submitted == 0
    reasons = [json.loads(r["block_reasons_json"]) for r in j.query("SELECT * FROM risk_checks")]
    assert any("per-symbol cap" in x["detail"] for group in reasons for x in group)


def test_only_current_pass_ids_can_book_and_scope_is_cleared(setup):
    orch, j, fake, _ = setup
    route(orch, ARMS[:1])
    assert fake.submits == 1 and orch._pass_entry_order_ids is None
    with pytest.raises(ExposureUnreconciled):
        read_exposure(orch.orders.alpaca, j)
    assert j.count_rows("positions") == 0
    # A later route must leave this prior-pass fill to ordinary reconciliation.
    summary = route(orch, ARMS[1:], batch="later")
    assert fake.submits == 1 and summary.entry_deferred == 2
    assert j.count_rows("positions") == 0 and orch._pass_entry_order_ids is None


def test_retry_submission_is_owned_before_fresh_staged_dispatch(setup):
    orch, j, fake, _ = setup
    # Initial lack of readiness leaves a documented auto-authorized pending row.
    j.conn.execute("DELETE FROM job_runs")
    j.conn.commit()
    route(orch, ARMS[:1], batch="old")
    assert fake.submits == 0
    j.insert("job_runs", {"job_run_id": "ready", "job_type": "monitor", "lock_key": "ready",
                         "status": "completed", "started_at_utc": START.isoformat(),
                         "started_at_sgt": timeutils.stamp(START).local_sgt,
                         "finished_at_utc": START.isoformat(),
                         "result_summary_json": {"worker_role": "monitor"}})
    summary = route(orch, ARMS[1:], batch="fresh")
    assert fake.submits == 3 and summary.auto_submitted == 3
    assert j.count_open_positions() == 2 and j.count_rows("approvals") == 3


def test_scope_clears_on_dispatch_exception_without_manual_authority(setup, monkeypatch):
    orch, j, fake, _ = setup
    monkeypatch.setattr(orch, "_dispatch_proposal", lambda *a: (_ for _ in ()).throw(RuntimeError("stop")))
    with pytest.raises(RuntimeError, match="stop"):
        route(orch)
    assert orch._pass_entry_order_ids is None and fake.submits == 0
    assert j.count_rows("approvals") == 0


@pytest.mark.parametrize("phase", ["observe", "open"])
def test_booking_transaction_rolls_back_partial_observe_or_fill_failure(setup, monkeypatch, phase):
    orch, j, fake, _ = setup
    owner, name = (EntrySubmission, "observe") if phase == "observe" else (OrderManager, "_open_real_position")
    original = getattr(owner, name)
    def fail_after(self, *args):
        original(self, *args)
        raise ValueError("injected booking failure")
    original_book = orch.orders._book_verified_entry_fill
    def book(proof):
        tables = ("paper_orders", "positions", "paper_fills", "trade_proposals")
        before = [j.query(f"SELECT * FROM {t}") for t in tables]
        with monkeypatch.context() as patch:
            patch.setattr(owner, name, fail_after)
            with pytest.raises(ValueError, match="injected"):
                original_book(proof)
        assert [j.query(f"SELECT * FROM {t}") for t in tables] == before
        raise ValueError("booking withheld")
    monkeypatch.setattr(orch.orders, "_book_verified_entry_fill", book)
    route(orch, ARMS[:2])
    assert fake.submits == 1 and j.count_rows("approvals") == 1
    assert j.count_rows("paper_fills") == j.count_rows("positions") == 0


def test_monitor_contention_prevents_fill_booking(setup, monkeypatch):
    orch, j, fake, _ = setup
    monkeypatch.setattr("alphaos.orchestrator.ENTRY_MONITOR_WAIT_SECONDS", 0.01)
    original = orch._dispatch_proposal
    def dispatch(*args):
        if fake.submits:
            with held_monitor(j):
                return original(*args)
        return original(*args)
    monkeypatch.setattr(orch, "_dispatch_proposal", dispatch)
    summary = route(orch, ARMS[:2])
    assert fake.submits == 1 and summary.entry_deferred == 1
    assert j.count_rows("positions") == 0 and j.count_rows("approvals") == 1


def test_other_thread_cannot_inherit_pass_fill_authority(setup, monkeypatch):
    orch, _, fake, _ = setup
    original = orch._dispatch_proposal
    def dispatch(*args):
        assert orch._current_pass_entry_ids() is not None
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(orch._current_pass_entry_ids).result() is None
        return original(*args)
    monkeypatch.setattr(orch, "_dispatch_proposal", dispatch)
    route(orch)
    assert fake.submits == 3 and orch._current_pass_entry_ids() is None
