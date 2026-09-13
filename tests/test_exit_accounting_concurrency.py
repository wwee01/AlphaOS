"""Deterministic two-connection interleaving at the exit write-lock boundary."""
from contextlib import contextmanager

import pytest

from alphaos.broker.alpaca_client import AlpacaClient
from alphaos.execution import ledger_ops
from alphaos.execution.order_manager import OrderManager
from alphaos.journal.journal_store import JournalStore
from alphaos.util import alerts
from alphaos.safety import KillSwitch
from test_execution_lifecycle_repair import setup


@pytest.mark.parametrize("direction", ["long", "short"])
@pytest.mark.parametrize("filled_qty", [6, 10])
def test_exit_refreshes_residual_and_closed_status_after_other_pass_commits(tmp_path, monkeypatch, direction, filled_qty):
    monkeypatch.setattr(alerts, "send_alert", lambda *a, **kw: None)
    fake, settings, journal, manager, pos, _ = setup(tmp_path, persistent=True, direction=direction)
    other_journal = JournalStore(journal.db_path)
    other = OrderManager(settings, other_journal,
        alpaca=AlpacaClient(settings, other_journal, trading_client=fake),
        kill_switch=KillSwitch(str(tmp_path / "KILL_SWITCH")))
    try:
        fake.set_broker_qty("XLV", 10 - filled_qty)
        norm = {"broker_order_id": "exit-overlap", "symbol": "XLV",
                "side": "buy" if direction == "short" else "sell", "qty": 10,
                "filled_qty": filled_qty, "filled_avg_price": 105,
                "filled_at": "2026-07-14T17:00:02Z",
                "state": "filled" if filled_qty == 10 else "partially_filled",
                "status": "filled" if filled_qty == 10 else "partially_filled"}
        atomic = ledger_ops.atomic
        committed = []
        outer_sql = []

        @contextmanager
        def another_pass_before_lock(j):
            if j is journal and not committed:
                # Pause this pass just before BEGIN IMMEDIATE. A second real
                # connection commits the same cumulative fill in that gap.
                committed.append(True)
                other._record_broker_exit(pos, norm, "time_expiry")
                expected_qty = 10 if filled_qty == 10 else 4
                assert other_journal.one("SELECT qty FROM positions")["qty"] == expected_qty
                journal.conn.set_trace_callback(outer_sql.append)
            with atomic(j) as scoped:
                yield scoped

        monkeypatch.setattr(ledger_ops, "atomic", another_pass_before_lock)
        manager._record_broker_exit(pos, norm, "time_expiry")
        current = journal.one("SELECT * FROM positions")
        assert current["qty"] == (10 if filled_qty == 10 else 4)
        if filled_qty == 10:
            assert not any(sql.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")) for sql in outer_sql)
        assert current["status"] == ("closed" if filled_qty == 10 else "open")
        assert [(f["qty"], f["price"]) for f in journal.query("SELECT * FROM paper_fills")] == [(filled_qty, 105)]
        # An additional identical pass remains idempotent, including closed rows.
        before = {t: journal.query(f"SELECT * FROM {t} ORDER BY id")
                  for t in ("positions", "paper_fills", "exits", "trade_outcomes")}
        manager._record_broker_exit(pos, norm, "time_expiry")
        assert {t: journal.query(f"SELECT * FROM {t} ORDER BY id") for t in before} == before
        if filled_qty == 6:
            fake.set_broker_qty("XLV", 0)
            final = {**norm, "filled_qty": 10, "filled_avg_price": 106,
                     "state": "filled", "status": "filled"}
            manager._record_broker_exit(pos, final, "time_expiry")
            assert [(f["qty"], f["price"]) for f in journal.query("SELECT * FROM paper_fills ORDER BY id")] == [(6, 105), (4, 107.5)]
        outcome = journal.one("SELECT * FROM trade_outcomes")
        assert outcome["qty"] == 10
        expected_pnl = 60 if filled_qty == 6 else 50
        assert outcome["gross_pnl"] == (expected_pnl if direction == "long" else -expected_pnl)
        assert journal.one("SELECT qty,status FROM positions") == {"qty": 10.0, "status": "closed"}
        assert len(journal.query("SELECT * FROM exits")) == 1
    finally:
        other_journal.close()
        journal.close()
