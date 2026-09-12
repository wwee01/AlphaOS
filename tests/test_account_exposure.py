from types import SimpleNamespace

import pytest

from alphaos.constants import ReasonCode
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.risk.account_exposure import ExposureUnavailable, entry_submission_lock, summarize_exposure
from alphaos.risk.risk_engine import RiskEngine
from conftest import make_proposal, make_settings


def _account(**kwargs):
    return {"equity": 90000, "last_equity": 90500, "trading_blocked": False, **kwargs}


def _position(**kwargs):
    return {"symbol": "AAPL", "side": "long", "qty": 100, "market_value": 10000, **kwargs}


def _order(**kwargs):
    return {"broker_order_id": "entry1", "symbol": "MSFT", "side": "buy", "qty": 100,
            "filled_qty": 0, "limit_price": 100, **kwargs}


def test_pending_entries_reserve_notional_and_position_slots():
    state = summarize_exposure(_account(), [_position()], [_order()], {"entry1": {"symbol": "MSFT"}})
    assert state["equity"] == 90000
    assert state["gross_committed_notional"] == 20000
    assert state["open_positions"] == 2
    assert state["account_pnl_today"] == -500
    engine = RiskEngine(make_settings(MAX_RISK_PER_TRADE_PCT="0.01"))
    risk = engine.assess(direction="long", entry=100, stop=99, equity=state["equity"],
                         gross_committed_notional=state["gross_committed_notional"])
    assert risk.approved
    assert risk.sizing.risk_budget == 900
    assert risk.sizing.shares == 700  # $70k uncommitted, not full $100k config equity


def test_partial_entry_reserves_only_unfilled_quantity_and_no_duplicate_position_slot():
    state = summarize_exposure(_account(), [_position(qty=40, market_value=4000)],
                              [_order(symbol="AAPL", filled_qty=40)], {"entry1": {"symbol": "AAPL"}})
    assert state["gross_committed_notional"] == 10000
    assert state["open_positions"] == 1


def test_two_unfilled_orders_for_same_symbol_each_reserve_a_slot():
    state = summarize_exposure(_account(), [], [_order(), _order(broker_order_id="entry2")],
                              {"entry1": {"symbol": "MSFT"}, "entry2": {"symbol": "MSFT"}})
    assert state["open_positions"] == 2
    assert state["gross_committed_notional"] == 20000


@pytest.mark.parametrize("account,positions,orders,known", [
    (_account(equity=None), [], [], {}),
    (_account(last_equity=None), [], [], {}),
    (_account(trading_blocked=None), [], [], {}),
    (_account(), [_position(market_value=None)], [], {}),
    (_account(), [], [_order()], {}),
    (_account(), [_position()], [_order(symbol="AAPL", side="sell", qty=200)], {}),
    (_account(), [], [_order(filled_qty=101)], {"entry1": {"symbol": "MSFT"}}),
])
def test_unknown_or_contradictory_state_cannot_be_free_capacity(account, positions, orders, known):
    with pytest.raises(ExposureUnavailable):
        summarize_exposure(account, positions, orders, known)


def test_protective_orders_do_not_double_count_long_or_short_notional():
    state = summarize_exposure(_account(), [_position()],
                              [_order(symbol="AAPL", side="sell"), _order(broker_order_id="stop", symbol="AAPL", side="sell", limit_price=None, stop_price=95)], {}, {"entry1", "stop"})
    assert state["gross_committed_notional"] == 10000
    assert state["open_positions"] == 1
    state = summarize_exposure(_account(), [_position(side="short", qty=-100, market_value=-10000)],
                              [_order(symbol="AAPL", side="buy")], {}, {"entry1"})
    assert state["gross_committed_notional"] == 10000


def test_unrealized_account_loss_enforces_existing_daily_percentage():
    engine = RiskEngine(make_settings(MAX_DAILY_LOSS_PCT="0.03"))
    risk = engine.assess(direction="long", entry=100, stop=95, equity=90000, realized_pnl_today=0, account_pnl_today=-3000)
    assert not risk.approved
    assert risk.primary_reason == ReasonCode.DAILY_LOSS_LIMIT.value


def test_local_entry_lock_prevents_concurrent_checks_and_releases_on_error(tmp_path):
    journal = SimpleNamespace(db_path=str(tmp_path / "ledger.db"))
    with pytest.raises(RuntimeError, match="crash"):
        with entry_submission_lock(journal):
            with pytest.raises(ExposureUnavailable, match="in progress"):
                with entry_submission_lock(journal):
                    pytest.fail("second writer entered")
            raise RuntimeError("crash")
    with entry_submission_lock(journal):
        pass


def test_final_execution_blocks_stale_approved_quantity_before_any_broker_submission(tmp_path, monkeypatch):
    j = JournalStore(str(tmp_path / "ledger.db"))
    orch = Orchestrator(settings=make_settings(), journal=j)
    # Isolate the final shared funnel from market data and order implementation.
    orch.orders = SimpleNamespace(real_paper=True, alpaca=object())
    monkeypatch.setattr("alphaos.risk.account_exposure.read_exposure", lambda *a: {
        "equity": 10000, "gross_committed_notional": 9990, "open_positions": 1,
        "account_pnl_today": 0,
    })
    submitted = []
    monkeypatch.setattr(orch, "_execute_checked", lambda *a: submitted.append(a))
    result = orch._execute(make_proposal(qty=10))
    assert result.blocked
    assert submitted == []
    assert j.one("SELECT category FROM system_events WHERE category='entry_risk_snapshot'")
    j.close()


def test_external_reducing_looking_order_needs_known_protection_lineage():
    with pytest.raises(ExposureUnavailable, match="unrecognized"):
        summarize_exposure(_account(), [_position()], [_order(symbol="AAPL", side="sell")], {})


def test_a_fill_between_account_reads_blocks_instead_of_losing_reserved_exposure():
    from alphaos.risk.account_exposure import read_exposure
    class RacingBroker:
        def __init__(self):
            self.calls = 0
        def get_account(self):
            return _account()
        def list_positions(self):
            self.calls += 1
            return [] if self.calls == 1 else [_position()]
        def list_open_orders(self):
            return []  # entry just filled, no longer appears as pending
    with pytest.raises(ExposureUnavailable, match="changed during"):
        read_exposure(RacingBroker(), SimpleNamespace())


@pytest.mark.parametrize("status,filled", [("new", 40), ("pending_cancel", 0), ("unknown_broker_state", 0)])
def test_final_exposure_requires_remaining_protection_and_explicit_working_status(status, filled):
    from alphaos.risk.account_exposure import read_exposure
    position = _position()
    class Broker:
        def get_account(self): return _account()
        def list_positions(self): return [position]
        def list_open_orders(self): return []
        def get_order(self, parent_id):
            return {"broker_order_id": parent_id, "order_class": "bracket", "legs": [
                {"broker_order_id": "tp", "role": "take_profit", "side": "sell", "qty": 100, "filled_qty": 0, "status": "new", "state": "accepted"},
                {"broker_order_id": "sl", "role": "stop_loss", "side": "sell", "qty": 100, "filled_qty": filled, "status": status, "state": "submitted"},
            ]}
    journal = SimpleNamespace(open_positions=lambda: [{**position, "broker_order_id": "parent"}])
    with pytest.raises(ExposureUnavailable, match="protection is not verified"):
        read_exposure(Broker(), journal)
