"""Claude audit probes on 13b5645 (F1: same-pass fill booking). Offline: fake broker, temp ledgers, frozen clock.

Copy this file OUTSIDE the production tree before running it against another checkout: run in place, pytest
puts the production tree first on the import path and the guard below stops the run.

Run from a checkout of the commit under audit:
  ALPHAOS_REPO="$PWD" PYTHONPATH="$PWD" .venv/bin/python -m pytest -q -p no:cacheprovider --rootdir=<dir of this file> <this file>
"""
import os, sys, json, threading
sys.path.insert(0, os.path.join(os.environ["ALPHAOS_REPO"], "tests"))
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import alphaos
from alphaos.ai.openai_client import OpenAIEvaluation
from alphaos.broker.alpaca_client import AlpacaClient
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator, ScanSummary
from alphaos.scanner.scan_context import ScanContext
from alphaos.util import timeutils
from conftest import make_settings
from test_entry_submission_recovery import RecoverableBroker

assert os.environ["ALPHAOS_REPO"] in alphaos.__file__, alphaos.__file__


class Broker(RecoverableBroker):
    """POST answers "accepted"; with instant=True the entry has filled by the next account read
    (the production shape: fills 1-5 s after acceptance, local position written minutes later)."""
    def __init__(self, instant=True):
        super().__init__()
        self.instant, self.unfilled, self.calls, self.hold_fills = instant, [], [], False

    def get_account(self):
        self.calls.append("account")
        while self.unfilled and not self.hold_fills:
            self.fill(self.unfilled.pop(0))
        return {"equity": 95000.0, "last_equity": 95000.0, "cash": 95000.0, "trading_blocked": False, "status": "ACTIVE"}

    def get_all_positions(self):
        self.calls.append("positions"); return super().get_all_positions()

    def get_order_by_id(self, oid):
        self.calls.append("order"); return super().get_order_by_id(oid)

    def submit(self, spec):
        order = super().submit(spec)
        if self.instant:
            self.unfilled.append(order)
        return order

    def fill(self, order, qty=None):
        qty = order.qty if qty is None else qty
        order.status = "filled" if qty == order.qty else "partially_filled"
        order.filled_qty, order.filled_avg_price, order.filled_at = qty, order.limit_price, "2026-11-16T14:40:01Z"
        self.set_position(order.symbol, qty, avg_entry_price=order.limit_price)
        if qty == order.qty:
            order.legs[1].status = "held"
            self._stray_orders[order.legs[0].id] = order.legs[0]

    def get_orders(self):
        self.calls.append("open_orders")
        return list(self._stray_orders.values()) + [o for o in self.orders.values() if o.status in ("accepted", "partially_filled")]


START = datetime(2026, 11, 16, 14, 36, tzinfo=timezone.utc)   # 09:36 ET


def harness(tmp_path, monkeypatch, *, instant=True, name="audit", **over):
    clock = [START]
    monkeypatch.setattr(timeutils, "now_utc", lambda: clock[0])
    alerts = []
    monkeypatch.setattr("alphaos.util.alerts.send_alert", lambda *a, **k: alerts.append((a, k)))
    cfg = {"ALPHAOS_MODE": "paper", "EXECUTION_PROVIDER": "alpaca_paper", "ALPACA_API_KEY": "k",
           "ALPACA_SECRET_KEY": "s", "ALPACA_PAPER": "true", "ALPACA_BASE_URL": "https://paper-api.alpaca.markets",
           "REAL_TRADING_ENABLED": "false", "OFFLINE_MODE": "true", "EXPLORE_1_ENABLED": "true",
           "MAX_OPEN_POSITIONS": "20", "MAX_RISK_PER_TRADE_PCT": ".0025", "MAX_AUTO_APPROVALS_PER_DAY": "20",
           "MAX_UNATTENDED_APPROVALS_PER_DAY": "20", "MAX_PAPER_TRADES_PER_DAY": "20",
           "APPROVAL_MODE": "auto", "REQUIRE_MANUAL_APPROVAL": "false", "ACTIVE_CARD_ID": "catalyst_momentum_v3",
           "ENTRY_ORDER_STALENESS_ENABLED": "false", "TIME_EXIT_ENFORCEMENT_ENABLED": "false",
           "SCHEDULER_SCAN_WINDOWS": "09:35-09:50,11:30-12:00,15:45-16:00",
           "PROPOSAL_TTL_RTH_SECONDS": "1800", "MAX_PRICE_DRIFT_BPS_SINCE_PROPOSAL": "50"}
    cfg.update(over)
    settings = make_settings(**cfg)
    journal = JournalStore(str(tmp_path / f"{name}.db"))
    orch = Orchestrator(settings=settings, journal=journal)
    orch.startup()
    fake = Broker(instant)
    orch.orders.alpaca = AlpacaClient(settings, journal, trading_client=fake)
    assert orch.orders.real_paper and orch.orders.broker_connected
    quote = orch.market.get_snapshot
    monkeypatch.setattr(orch.market, "get_snapshot",
                        lambda symbol: {**quote(symbol), "last_price": 100.0, "bid": 99.99, "ask": 100.01})
    monitor_ok(journal, clock)
    return SimpleNamespace(orch=orch, journal=journal, fake=fake, clock=clock, alerts=alerts)


def monitor_ok(journal, clock, tag="initial"):
    journal.insert("job_runs", {
        "job_run_id": f"monitor-{tag}", "job_type": "monitor", "lock_key": tag, "status": "completed",
        "started_at_utc": clock[0].isoformat(), "started_at_sgt": timeutils.stamp(clock[0]).local_sgt,
        "finished_at_utc": clock[0].isoformat(), "result_summary_json": {"worker_role": "monitor"}})


def stage(journal, rows, batch="s"):
    if not journal.one("SELECT 1 FROM scan_batches WHERE scan_batch_id=?", (batch,)):
        journal.insert("scan_batches", {"scan_batch_id": batch, "status": "started"})
    triples = []
    for cid, symbol, a, b in rows:
        c = ScanContext({"candidate_id": cid, "symbol": symbol, "direction": "long", "strategy": "swing",
                         "status": "detected", "scan_id": "s", "scan_batch_id": batch},
                        snapshot={"last_price": 100.0, "market_session": "regular", "volume": 5_000_000,
                                  "bid": 99.99, "ask": 100.01})
        e = replace(OpenAIEvaluation("e" + cid, cid, symbol, "mock", "long", 100.0, 98.0, 110.0, 10, 5.0, 0.8, a,
                                     "fixture", is_mock=True))
        journal.insert("candidates", c.row)
        journal.insert("openai_evaluations", e.to_row())
        triples.append((c, e, SimpleNamespace(candidate_id=cid, symbol=symbol, label_decision=b, direction="long",
                                              validation_status="passed")))
    return triples


ARMS3 = [("c1", "AMZN", "watch", "watch"), ("a1", "AAPL", "propose", "watch"), ("b1", "MSFT", "watch", "propose")]


def run_pass(h, rows=ARMS3, batch="s", at=START):
    summary = ScanSummary(batch)
    h.orch._route_campaign(stage(h.journal, rows, batch), summary, batch, at.isoformat(), False)
    return summary


def by_symbol(journal, table, where=""):
    return {r["symbol"]: r for r in journal.query(f"SELECT * FROM {table} {where} ORDER BY id")}


def booked_events(journal):
    return journal.query("SELECT detail_json FROM system_events WHERE category='same_pass_entry_fill' ORDER BY id")


# ------------------------------------------------------------------ 1. the original failure is repaired
def test_all_three_arms_trade_in_one_pass_when_each_entry_fills_at_once(tmp_path, monkeypatch):
    h = harness(tmp_path, monkeypatch)
    summary = run_pass(h)
    assert h.fake.submits == 3 and summary.auto_submitted == 3 and summary.entry_deferred == 0
    props = by_symbol(h.journal, "trade_proposals")
    assert {s: p["arm"] for s, p in props.items()} == {"AMZN": "C", "AAPL": "A", "MSFT": "B"}
    # the first two fills were booked when the next entry needed a clean read; the last one waits for the monitor
    assert [props[s]["status"] for s in ("AMZN", "AAPL", "MSFT")] == ["filled", "filled", "submitted"]
    pos = by_symbol(h.journal, "positions")
    assert set(pos) == {"AMZN", "AAPL"} and len(booked_events(h.journal)) == 2
    assert {s: (p["arm"], p["policy_version"]) for s, p in pos.items()} == {"AMZN": ("C", "explore_1"), "AAPL": ("A", "explore_1")}
    assert not h.journal.query("SELECT 1 FROM system_events WHERE category='entry_deferred'")
    assert h.orch._pass_entry_order_ids is None and h.orch._pass_entry_thread_id is None


# ------------------------------------------------------------------ 2. books are the same as the monitor would write
VOLATILE = ("id", "_at", "utc", "sgt", "opened_market_date", "created", "raw_", "current_price", "unrealized")


def stable(row):
    return {k: v for k, v in row.items() if not any(m in k for m in VOLATILE)}


def ledger_view(journal):
    orders = by_symbol(journal, "paper_orders")
    out = {}
    for sym, order in orders.items():
        pos = journal.one("SELECT * FROM positions WHERE order_id=?", (order["order_id"],))
        fills = journal.query("SELECT side, qty, price, fill_source, fill_price_basis, execution_source FROM paper_fills WHERE order_id=? ORDER BY id", (order["order_id"],))
        events = journal.query("SELECT prev_state, new_state FROM order_events WHERE order_id=? ORDER BY id", (order["order_id"],))
        prop = journal.one("SELECT status, arm, policy_version FROM trade_proposals WHERE proposal_id=?", (order["proposal_id"],))
        meta = json.loads(order["raw_request_json"])
        out[sym] = {"order": stable(order), "phase": meta.get("phase"), "position": stable(pos) if pos else None,
                    "position_links": bool(pos and pos["order_id"] == order["order_id"] and pos["broker_order_id"] == order["broker_order_id"]
                                           and pos["proposal_id"] == order["proposal_id"]),
                    "fills": [dict(f) for f in fills], "events": [tuple(e.values()) for e in events], "proposal": dict(prop)}
    return out


def test_same_pass_booking_then_monitor_leaves_the_same_books_as_the_monitor_alone(tmp_path, monkeypatch):
    a = harness(tmp_path, monkeypatch, instant=True, name="same_pass")
    run_pass(a)
    a.fake.get_account()                                               # the last entry fills at the broker too
    a.clock[0] = START + timedelta(minutes=8)
    first = a.orch.orders.reconcile()
    assert not first["errors"] and len(first["opened"]) == 1          # only the last entry was left for the monitor
    alerts_a = len(a.alerts)

    b = harness(tmp_path, monkeypatch, instant=False, name="monitor_only")
    run_pass(b)
    assert b.fake.submits == 3 and not by_symbol(b.journal, "positions")
    for order in list(b.fake.orders.values()):
        b.fake.fill(order)
    b.clock[0] = START + timedelta(minutes=8)
    only = b.orch.orders.reconcile()
    assert not only["errors"] and len(only["opened"]) == 3

    va, vb = ledger_view(a.journal), ledger_view(b.journal)
    assert set(va) == set(vb) == {"AMZN", "AAPL", "MSFT"}
    for sym in va:
        for part in va[sym]:
            assert va[sym][part] == vb[sym][part], (sym, part, va[sym][part], vb[sym][part])
    assert alerts_a == len(b.alerts)                                   # no extra or missing operator alert


# ------------------------------------------------------------------ 3. later monitor passes do not double-book
def test_monitor_passes_after_a_same_pass_booking_add_nothing(tmp_path, monkeypatch):
    h = harness(tmp_path, monkeypatch)
    run_pass(h)
    h.fake.get_account()                                               # the last entry fills at the broker too
    h.clock[0] = START + timedelta(minutes=8)
    assert not h.orch.orders.reconcile()["errors"]
    snap = lambda: (h.journal.query("SELECT position_id, qty, avg_entry_price, status FROM positions ORDER BY id"),
                    h.journal.query("SELECT order_id, qty, price FROM paper_fills ORDER BY id"),
                    h.journal.query("SELECT proposal_id, status FROM trade_proposals ORDER BY id"))
    before = snap()
    assert len(before[0]) == 3 and len(before[1]) == 3
    for _ in range(3):
        again = h.orch.orders.reconcile()
        assert not again["errors"] and not again["opened"] and not again["exits"]
    assert snap() == before


# ------------------------------------------------------------------ 4. limits see the booked position
def test_open_position_limit_counts_the_position_booked_in_this_pass(tmp_path, monkeypatch):
    h = harness(tmp_path, monkeypatch, MAX_OPEN_POSITIONS="2")
    run_pass(h)
    props = by_symbol(h.journal, "trade_proposals")
    assert h.fake.submits == 2 and props["MSFT"]["status"] == "blocked"
    event = json.loads(h.journal.one("SELECT detail_json FROM system_events WHERE category='risk' "
                                     "AND message='Entry blocked by fresh account limits' ORDER BY id DESC")["detail_json"])
    assert event["proposal_id"] == props["MSFT"]["proposal_id"]
    assert event["reasons"] == [{"code": "TOO_MANY_POSITIONS", "detail": "open 2 >= max 2"}]   # both booked fills counted


def test_daily_entry_cap_is_not_widened_by_booking(tmp_path, monkeypatch):
    h = harness(tmp_path, monkeypatch, MAX_PAPER_TRADES_PER_DAY="2")
    run_pass(h)
    assert h.fake.submits == 2


def test_two_candidates_in_one_symbol_trade_once(tmp_path, monkeypatch):
    h = harness(tmp_path, monkeypatch)
    run_pass(h, [("a1", "AAPL", "propose", "watch"), ("b1", "AAPL", "watch", "propose")])
    statuses = sorted(r["status"] for r in h.journal.query("SELECT status FROM trade_proposals"))
    assert h.fake.submits == 1 and statuses == ["submitted", "superseded"], statuses


# ------------------------------------------------------------------ 5. authority is this pass only
def test_fill_from_an_earlier_pass_is_never_booked_by_a_later_pass(tmp_path, monkeypatch):
    h = harness(tmp_path, monkeypatch)
    run_pass(h, [("a1", "AAPL", "propose", "watch")])
    assert h.fake.submits == 1 and not by_symbol(h.journal, "positions")
    h.fake.get_account()                                               # the entry fills; the monitor has not run
    later = START + timedelta(minutes=5); h.clock[0] = later
    summary = run_pass(h, [("a2", "NVDA", "propose", "watch")], batch="s2", at=later)
    assert h.fake.submits == 1 and summary.entry_deferred == 1 and not booked_events(h.journal)
    assert by_symbol(h.journal, "trade_proposals")["NVDA"]["status"] == "pending_approval"
    assert not by_symbol(h.journal, "positions")                       # still the monitor's to book


def test_authority_is_cleared_when_the_pass_raises_and_is_not_visible_to_another_thread(tmp_path, monkeypatch):
    h = harness(tmp_path, monkeypatch)
    seen = {}
    def boom(proposal, summary, unattended):
        t = threading.Thread(target=lambda: seen.setdefault("other", h.orch._current_pass_entry_ids()))
        t.start(); t.join()
        seen["own"] = h.orch._current_pass_entry_ids()
        raise RuntimeError("dispatch failed")
    monkeypatch.setattr(h.orch, "_dispatch_proposal", boom)
    with pytest.raises(RuntimeError):
        run_pass(h)
    assert seen == {"other": None, "own": set()}
    assert h.orch._pass_entry_order_ids is None and h.orch._current_pass_entry_ids() is None


# ------------------------------------------------------------------ 6. anything short of a full, protected fill
def test_partial_fill_is_not_booked_and_stops_the_next_entry(tmp_path, monkeypatch):
    h = harness(tmp_path, monkeypatch, instant=False)
    orig = h.fake.get_account
    def partial_then_account():
        for order in list(h.fake.orders.values()):
            if order.status == "accepted":
                h.fake.fill(order, qty=order.qty // 2)
        return orig()
    monkeypatch.setattr(h.fake, "get_account", partial_then_account)
    # first entry is read on a flat account; it is then half-filled before the second entry's read
    first_read = [True]
    def gated():
        if first_read[0]:
            first_read[0] = False
            return orig()
        return partial_then_account()
    monkeypatch.setattr(h.fake, "get_account", gated)
    run_pass(h)
    assert h.fake.submits == 1 and not booked_events(h.journal) and not by_symbol(h.journal, "positions")
    statuses = {s: p["status"] for s, p in by_symbol(h.journal, "trade_proposals").items()}
    assert statuses["AMZN"] == "submitted" and set(statuses.values()) <= {"submitted", "blocked", "pending_approval"}
    assert h.fake.submits == 1


def test_broker_quantity_that_the_entry_does_not_explain_is_not_booked(tmp_path, monkeypatch):
    h = harness(tmp_path, monkeypatch)
    real_fill = h.fake.fill
    def fill_plus_foreign(order, qty=None):
        real_fill(order, qty)
        h.fake.set_position(order.symbol, order.qty + 3, avg_entry_price=order.limit_price)
    monkeypatch.setattr(h.fake, "fill", fill_plus_foreign)
    run_pass(h)
    assert h.fake.submits == 1 and not booked_events(h.journal) and not by_symbol(h.journal, "positions")


def test_failed_booking_rolls_back_whole_and_the_monitor_still_books_it_later(tmp_path, monkeypatch):
    h = harness(tmp_path, monkeypatch)
    real_open = h.orch.orders.__class__._open_real_position
    armed = [True]
    def failing_open(self, row, norm):
        if armed[0]:
            raise RuntimeError("disk full")
        return real_open(self, row, norm)
    monkeypatch.setattr(h.orch.orders.__class__, "_open_real_position", failing_open)
    before = h.journal.query("SELECT count(*) n FROM paper_fills")[0]["n"]
    run_pass(h)
    assert h.fake.submits == 1 and not booked_events(h.journal)
    assert not by_symbol(h.journal, "positions") and h.journal.query("SELECT count(*) n FROM paper_fills")[0]["n"] == before
    order = by_symbol(h.journal, "paper_orders")["AMZN"]
    assert order["state"] == "accepted"                                 # the observe() update was rolled back too
    assert by_symbol(h.journal, "trade_proposals")["AMZN"]["status"] == "submitted"
    armed[0] = False
    h.clock[0] = START + timedelta(minutes=8)
    done = h.orch.orders.reconcile()
    assert not done["errors"] and len(done["opened"]) == 1
    assert by_symbol(h.journal, "trade_proposals")["AMZN"]["status"] == "filled"


def test_read_failure_after_a_booking_sends_nothing_and_keeps_the_booking(tmp_path, monkeypatch):
    h = harness(tmp_path, monkeypatch)
    real = h.fake.get_all_positions
    def flaky():
        if booked_events(h.journal):
            raise ConnectionError("network")
        return real()
    monkeypatch.setattr(h.fake, "get_all_positions", flaky)
    run_pass(h)
    assert h.fake.submits == 1 and len(booked_events(h.journal)) == 1
    assert set(by_symbol(h.journal, "positions")) == {"AMZN"}


# ------------------------------------------------------------------ 7. two fills waiting at once
def test_two_owned_fills_waiting_at_once_are_both_booked_before_the_third_entry(tmp_path, monkeypatch):
    h = harness(tmp_path, monkeypatch)
    h.fake.hold_fills = True                                            # entries 1 and 2 stay working...
    real_submit = h.fake.submit
    def submit(spec):
        order = real_submit(spec)
        if h.fake.submits == 2:
            h.fake.hold_fills = False                                   # ...and both fill before entry 3 is checked
        return order
    monkeypatch.setattr(h.fake, "submit", submit)
    summary = run_pass(h)
    assert h.fake.submits == 3 and summary.entry_deferred == 0 and len(booked_events(h.journal)) == 2
    assert set(by_symbol(h.journal, "positions")) == {"AMZN", "AAPL"}


# ------------------------------------------------------------------ 8. broker read volume (measured, for the record)
def test_broker_reads_in_a_three_entry_pass(tmp_path, monkeypatch, capsys):
    h = harness(tmp_path, monkeypatch)
    h.fake.calls.clear()
    run_pass(h)
    counts = {k: h.fake.calls.count(k) for k in sorted(set(h.fake.calls))}
    with capsys.disabled():
        print("\nbroker GETs in a 3-entry same-pass-fill pass:", counts, "total", len(h.fake.calls))
    assert len(h.fake.calls) < 60
