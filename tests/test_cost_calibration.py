"""Cost-model calibration + broker-hygiene (Roadmap 1.5), all hermetic.

No SDK, no network, no live broker: calibration rows + fake fills are inserted
straight into an in-memory ledger, and the broker is a tiny fake. Proves the
calibration math, report generation, the paper-only flatten, and broker-vs-ledger
mismatch detection.
"""

from __future__ import annotations

from alphaos.broker.alpaca_client import AlpacaClient
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.reports.broker_recon import build_broker_ledger_report
from alphaos.reports.cost_calibration import (
    build_calibration_report,
    build_calibration_row,
    render_markdown,
)
from alphaos.util.ids import new_id
from conftest import make_proposal, make_settings, inject_pending_proposal


# ----------------------------------------------------------------- row builder
def test_build_calibration_row_fields():
    s = make_settings()  # cost_slippage_bps default 1.0
    prop = make_proposal(symbol="AAPL", entry=100.0, stop=97.0, target=106.0, qty=10)
    snap = {"bid": 99.98, "ask": 100.02, "last_price": 100.0, "spread_pct": 0.0004}
    order_row = {
        "order_id": "ord_x", "side": "buy", "limit_price": 100.0,
        "execution_provider": "simulated_internal", "execution_source": "internal_sim",
    }
    row = build_calibration_row(s, prop, snap, order_row)
    assert row["expected_entry"] == 100.0
    assert row["approval_bid"] == 99.98 and row["approval_ask"] == 100.02
    assert row["approval_mid"] == 100.0
    assert round(row["approval_spread"], 4) == 0.04
    assert row["submitted_limit_price"] == 100.0
    assert row["modeled_slippage_bps"] == 1.0
    # modeled cost = qty * entry * bps/10000 = 10 * 100 * 0.0001 = 0.1
    assert round(row["modeled_cost_estimate"], 4) == 0.1
    assert row["broker_managed"] == 0


# ------------------------------------------------------------- report math
def _seed_sample(journal, *, symbol, expected, fill, submitted_at, filled_at,
                 side="buy", broker_managed=False, slippage_bps_model=1.0):
    order_id = new_id("ord")
    src = "alpaca_paper" if broker_managed else "internal_sim"
    journal.insert("paper_orders", {
        "order_id": order_id, "proposal_id": new_id("prop"), "symbol": symbol, "side": side,
        "qty": 10, "state": "filled", "execution_source": src, "execution_provider": src,
        "limit_price": expected, "entry_price": fill, "submitted_at": submitted_at, "filled_at": filled_at,
    })
    tid = new_id("trade")
    journal.insert("paper_fills", {
        "fill_id": new_id("fill"), "order_id": order_id, "symbol": symbol, "side": side, "qty": 10,
        "price": fill, "filled_at": filled_at, "trade_id": tid,
        "execution_provider": src, "fill_source": src,
    })
    for prev, new in (("approved", "submitted"), ("submitted", "accepted"), ("accepted", "filled")):
        journal.insert("order_events", {
            "event_id": new_id("oev"), "order_id": order_id, "prev_state": prev, "new_state": new,
            "execution_source": src, "message": f"{prev}->{new}",
        })
    journal.insert("execution_calibration", {
        "calibration_id": new_id("cal"), "proposal_id": new_id("prop"), "trade_id": tid,
        "order_id": order_id, "symbol": symbol, "side": side,
        "execution_provider": src, "broker_managed": 1 if broker_managed else 0,
        "expected_entry": expected, "approval_bid": expected - 0.01, "approval_ask": expected + 0.01,
        "approval_mid": expected, "approval_spread": 0.02, "approval_spread_pct": 0.02 / expected,
        "submitted_limit_price": expected, "modeled_slippage_bps": slippage_bps_model,
        "modeled_cost_estimate": 0.1,
    })


def test_calibration_report_math():
    s = make_settings()  # modeled slippage 1.0 bps
    j = JournalStore(":memory:")
    base = "2026-06-23T13:00:0"
    # Three buys: 4, 6, 8 bps adverse slippage; fill delays 1, 2, 3s.
    _seed_sample(j, symbol="AAA", expected=100.0, fill=100.04, submitted_at=base + "0+00:00", filled_at=base + "1+00:00")
    _seed_sample(j, symbol="BBB", expected=100.0, fill=100.06, submitted_at=base + "0+00:00", filled_at=base + "2+00:00")
    _seed_sample(j, symbol="CCC", expected=100.0, fill=100.08, submitted_at=base + "0+00:00", filled_at=base + "3+00:00", broker_managed=True)

    rep = build_calibration_report(j, s)
    summ, obs, rec = rep["summary"], rep["observed"], rep["recommended_model"]
    assert summ["filled_samples"] == 3
    assert summ["broker_managed"] == 1
    assert summ["preliminary"] is True
    assert summ["remaining_sample_needed"] == 17  # MIN_CALIBRATION_SAMPLE(20) - 3
    assert obs["mean_slippage_bps"] == 6.0
    assert obs["median_slippage_bps"] == 6.0
    assert obs["p75_slippage_bps"] == 7.0
    assert obs["mean_fill_delay_seconds"] == 2.0
    # Conservative: worse of modeled(1.0) and observed p75(7.0).
    assert rec["slippage_bps"] == 7.0
    assert rec["conservative"] is True and rec["preliminary"] is True
    per = {r["symbol"]: r for r in rep["per_trade"]}
    assert per["AAA"]["realized_slippage_bps"] == 4.0
    assert per["CCC"]["broker_managed"] is True
    assert per["AAA"]["order_status_sequence"] == ["submitted", "accepted", "filled"]
    assert isinstance(render_markdown(rep), str) and "Calibration" in render_markdown(rep)
    j.close()


def test_calibration_report_empty_is_preliminary_and_keeps_assumption():
    s = make_settings()
    j = JournalStore(":memory:")
    rep = build_calibration_report(j, s)
    assert rep["summary"]["filled_samples"] == 0
    assert rep["summary"]["preliminary"] is True
    assert rep["summary"]["remaining_sample_needed"] == 20
    # No data -> recommendation stays at the current assumption.
    assert rep["recommended_model"]["slippage_bps"] == s.cost_slippage_bps
    j.close()


def test_calibration_captured_on_simulated_approval():
    s = make_settings(APPROVAL_MODE="manual")
    j = JournalStore(":memory:")
    orch = Orchestrator(settings=s, journal=j)
    pid, _ = inject_pending_proposal(orch)
    assert orch.approve_proposal(pid)[0]
    cals = j.query("SELECT * FROM execution_calibration")
    assert len(cals) == 1
    assert cals[0]["proposal_id"] == pid
    assert cals[0]["broker_managed"] == 0  # simulated_internal
    assert cals[0]["modeled_slippage_bps"] == s.cost_slippage_bps
    assert cals[0]["expected_entry"] is not None
    orch.close()


# ------------------------------------------------------------------- flatten
class _FakeTC:
    def __init__(self):
        self.cancelled = False
        self.closed = False
        self._orders = [object()]       # one open order
        self._positions = [object()]    # one open position

    def get_orders(self):
        return list(self._orders)

    def get_all_positions(self):
        return list(self._positions)

    def cancel_orders(self):
        self.cancelled = True
        self._orders = []

    def close_all_positions(self, cancel_orders=True):
        self.closed = True
        self._positions = []
        return [object()]


def _paper_settings(**over):
    cfg = {
        "ALPHAOS_MODE": "paper", "EXECUTION_PROVIDER": "alpaca_paper",
        "ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s", "ALPACA_PAPER": "true",
        "ALPACA_BASE_URL": "https://paper-api.alpaca.markets", "REAL_TRADING_ENABLED": "false",
    }
    cfg.update(over)
    return make_settings(**cfg)


def test_flatten_paper_cancels_orders_and_closes_positions():
    s = _paper_settings()
    fake = _FakeTC()
    ac = AlpacaClient(s, journal=None, trading_client=fake)
    summary = ac.flatten_paper()
    assert fake.cancelled is True and fake.closed is True
    assert summary["cancelled_orders"] == 1
    assert summary["closed_positions"] == 1


def test_flatten_refuses_when_real_trading_flag_not_false():
    s = _paper_settings(REAL_TRADING_ENABLED="true")
    ac = AlpacaClient(s, journal=None, trading_client=_FakeTC())
    import pytest

    from alphaos.broker.alpaca_client import AlpacaSafetyError
    with pytest.raises(AlpacaSafetyError):
        ac.flatten_paper()


# -------------------------------------------------------------- broker recon
class _FakeAlpaca:
    is_safe_paper = True

    def __init__(self, positions, orders):
        self._p, self._o = positions, orders

    def list_positions(self):
        return self._p

    def list_open_orders(self):
        return self._o


def _seed_ledger_position(journal, symbol):
    journal.insert("positions", {
        "position_id": new_id("pos"), "symbol": symbol, "direction": "long", "qty": 1,
        "status": "open", "execution_source": "alpaca_paper", "trade_id": new_id("trade"),
    })


def test_broker_ledger_report_detects_orphans():
    j = JournalStore(":memory:")
    _seed_ledger_position(j, "AAPL")  # ledger open AAPL, broker won't have it
    fake = _FakeAlpaca(
        positions=[{"symbol": "SPY", "qty": 2, "side": "long"}],          # orphan broker position
        orders=[{"symbol": "MSFT", "broker_order_id": "b1", "status": "new"}],  # orphan broker order
    )
    rep = build_broker_ledger_report(j, fake)
    assert rep["broker_available"] is True
    assert rep["in_sync"] is False
    assert rep["mismatch_count"] == 3
    assert [o["symbol"] for o in rep["orphan_ledger_positions"]] == ["AAPL"]
    assert [o["symbol"] for o in rep["orphan_broker_positions"]] == ["SPY"]
    assert [o["symbol"] for o in rep["orphan_broker_orders"]] == ["MSFT"]
    j.close()


def test_broker_ledger_report_in_sync():
    j = JournalStore(":memory:")
    _seed_ledger_position(j, "AAPL")
    fake = _FakeAlpaca(positions=[{"symbol": "AAPL", "qty": 1, "side": "long"}], orders=[])
    rep = build_broker_ledger_report(j, fake)
    assert rep["in_sync"] is True and rep["mismatch_count"] == 0
    j.close()


def test_broker_ledger_report_handles_no_broker():
    j = JournalStore(":memory:")
    rep = build_broker_ledger_report(j, None)
    assert rep["broker_available"] is False and rep["in_sync"] is False
    j.close()
