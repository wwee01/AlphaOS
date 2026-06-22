"""Real Alpaca PAPER execution lifecycle, exercised hermetically with a fake
TradingClient (no SDK/network). Covers: bracket submit -> entry-fill reconcile
-> TP-leg-fill close, honest alpaca_paper labelling, watchdog segregation, and
the safety gates. Real money stays unreachable."""

from __future__ import annotations

import uuid

import pytest

from alphaos.broker.alpaca_client import AlpacaClient
from alphaos.config.settings import SettingsError
from alphaos.constants import ExecutionProvider, ExecutionSource, OrderState, ReasonCode
from alphaos.execution.order_manager import OrderManager
from alphaos.journal.journal_store import JournalStore
from conftest import make_proposal, make_settings


# --------------------------------------------------------------------- fakes
class _FakeLeg:
    def __init__(self, role, limit_price=None, stop_price=None):
        self.id = uuid.uuid4().hex
        self.order_type = "limit" if role == "take_profit" else "stop"
        self.limit_price = limit_price
        self.stop_price = stop_price
        self.status = "new"
        self.filled_qty = 0
        self.filled_avg_price = None


class _FakeOrder:
    def __init__(self, spec):
        self.id = uuid.uuid4().hex
        self.client_order_id = spec["client_order_id"]
        self.symbol = spec["symbol"]
        self.side = spec["side"]
        self.qty = spec["qty"]
        self.order_class = "bracket"
        self.status = "accepted"
        self.filled_qty = 0
        self.filled_avg_price = None
        self.limit_price = spec["entry"]
        self.stop_price = None
        self.submitted_at = "2026-06-22T13:30:00Z"
        self.filled_at = None
        self.legs = [
            _FakeLeg("take_profit", limit_price=spec["target"]),
            _FakeLeg("stop_loss", stop_price=spec["stop"]),
        ]


class FakeTradingClient:
    FAKE = True

    def __init__(self):
        self.orders = {}

    # ---- SDK-agnostic interface used by AlpacaClient ----
    def submit(self, spec):
        o = _FakeOrder(spec)
        self.orders[o.id] = o
        return o

    def get_order_by_id(self, oid):
        return self.orders[oid]

    def get_all_positions(self):
        return []

    def cancel_order_by_id(self, oid):
        self.orders[oid].status = "canceled"

    # ---- test drivers ----
    def _find(self, symbol):
        return next(o for o in self.orders.values() if o.symbol == symbol)

    def fill_entry(self, symbol, price):
        o = self._find(symbol)
        o.status = "filled"
        o.filled_qty = o.qty
        o.filled_avg_price = price
        o.filled_at = "2026-06-22T14:00:00Z"

    def fill_leg(self, symbol, role, price):
        o = self._find(symbol)
        want = "limit" if role == "take_profit" else "stop"
        for leg in o.legs:
            if leg.order_type == want:
                leg.status = "filled"
                leg.filled_qty = o.qty
                leg.filled_avg_price = price
            else:
                leg.status = "canceled"  # OCO cancels the sibling


def _paper_om(fake, **over):
    cfg = {
        "ALPHAOS_MODE": "paper", "EXECUTION_PROVIDER": "alpaca_paper",
        "ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s", "ALPACA_PAPER": "true",
        "ALPACA_BASE_URL": "https://paper-api.alpaca.markets", "REAL_TRADING_ENABLED": "false",
    }
    cfg.update(over)
    s = make_settings(**cfg)
    journal = JournalStore(":memory:")
    alpaca = AlpacaClient(s, journal, trading_client=fake)
    om = OrderManager(s, journal, alpaca=alpaca)
    return s, journal, om


def _seed_proposal(journal, prop):
    journal.insert("trade_proposals", prop.to_row())


# --------------------------------------------------------------------- tests
def test_alpaca_paper_full_lifecycle():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)
    assert om.real_paper is True and om.broker_connected is True

    prop = make_proposal(symbol="AAPL", entry=100.0, stop=97.0, target=106.0, qty=10)
    _seed_proposal(journal, prop)

    # Submit -> accepted, not filled yet, no position.
    res = om.execute_proposal(prop)
    assert res.blocked is False
    assert res.state == OrderState.ACCEPTED.value
    assert res.position_id is None
    row = journal.one("SELECT * FROM paper_orders WHERE order_id = ?", (res.order["order_id"],))
    assert row["execution_source"] == ExecutionSource.ALPACA_PAPER.value
    assert row["execution_provider"] == ExecutionProvider.ALPACA_PAPER.value
    assert row["broker_order_id"]  # real broker id recorded
    assert journal.count_open_positions() == 0

    # Entry fills -> reconcile opens an alpaca_paper position.
    fake.fill_entry("AAPL", price=100.0)
    rec = om.reconcile()
    assert rec["reconciled"] >= 1 and len(rec["opened"]) == 1
    assert journal.count_open_positions() == 1
    pos = journal.open_positions()[0]
    assert pos["execution_source"] == ExecutionSource.ALPACA_PAPER.value

    # Watchdog must NOT touch a broker-managed position even at a stop price.
    exits = om.positions.monitor(price_overrides={"AAPL": 1.0})
    assert exits == []
    assert journal.count_open_positions() == 1

    # TP leg fills -> reconcile closes the position (profit-taking) via OCO.
    fake.fill_leg("AAPL", role="take_profit", price=106.0)
    rec2 = om.reconcile()
    assert len(rec2["exits"]) == 1
    assert rec2["exits"][0]["classification"] == "profit-taking"
    assert journal.count_open_positions() == 0

    outcome = journal.one("SELECT * FROM trade_outcomes WHERE position_id = ?", (pos["position_id"],))
    assert outcome["net_pnl"] == round((106.0 - 100.0) * 10, 2)
    # The exit is labelled as a real alpaca_paper fill, not internal_sim.
    exit_order = journal.one(
        "SELECT * FROM paper_orders WHERE side = 'sell' AND execution_source = ?",
        (ExecutionSource.ALPACA_PAPER.value,),
    )
    assert exit_order is not None


def test_stop_leg_fill_closes_as_risk_control():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)
    prop = make_proposal(symbol="MSFT", entry=200.0, stop=194.0, target=212.0, qty=5)
    _seed_proposal(journal, prop)
    om.execute_proposal(prop)
    fake.fill_entry("MSFT", price=200.0)
    om.reconcile()
    fake.fill_leg("MSFT", role="stop_loss", price=194.0)
    rec = om.reconcile()
    assert rec["exits"][0]["classification"] == "risk-control"
    out = journal.one("SELECT * FROM trade_outcomes WHERE symbol = 'MSFT'")
    assert out["net_pnl"] == round((194.0 - 200.0) * 5, 2)  # loss


def test_alpaca_paper_requires_paper_mode():
    # alpaca_paper execution in mock mode must fail fast.
    with pytest.raises(SettingsError):
        make_settings(EXECUTION_PROVIDER="alpaca_paper")  # default mode=mock


def test_real_trading_flag_blocks_even_paper_execution():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, REAL_TRADING_ENABLED="true")
    prop = make_proposal(symbol="AAPL")
    _seed_proposal(journal, prop)
    res = om.execute_proposal(prop)
    assert res.blocked is True
    assert res.block_reason == ReasonCode.REAL_TRADING_BLOCKED.value
    # Nothing was submitted to the broker.
    assert fake.orders == {}


def test_mock_mode_still_simulated_internal():
    # Default (mock) settings keep execution simulated, not alpaca_paper.
    s = make_settings()
    assert s.real_paper_execution is False
    assert s.execution_provider == "simulated_internal"
