"""Real SDK request construction with a non-network recorder, not the FAKE path."""
from datetime import date, datetime, timezone
from types import SimpleNamespace as Object

import pytest

from alphaos.broker.alpaca_client import AlpacaClient, AlpacaSafetyError
from conftest import make_settings


@pytest.fixture
def paper_settings():
    return make_settings(ALPHAOS_MODE="paper", EXECUTION_PROVIDER="alpaca_paper",
                         ALPACA_API_KEY="test-key", ALPACA_SECRET_KEY="test-secret", ALPACA_PAPER="true")


def oco():
    stop = Object(id="stop", symbol="XLV", side="sell", order_type="stop", status="new", qty=10,
                  stop_price=94, limit_price=None, filled_qty=0, filled_avg_price=None, time_in_force="gtc")
    return Object(id="target", symbol="XLV", side="sell", order_class="oco", order_type="limit",
                  status="new", qty=10, limit_price=112, stop_price=None, filled_qty=0,
                  filled_avg_price=None, client_order_id="stable", time_in_force="gtc", legs=[stop])


def test_real_oco_request_and_response_contract(paper_settings):
    class Recorder:
        def submit_order(self, request):
            self.request = request.model_dump(exclude_none=True)
            return oco()
    recorder = Recorder()
    client = AlpacaClient(paper_settings, trading_client=recorder)
    result = client.submit_protective_oco("XLV", 10, "long", 94, 112, "gtc", "stable")
    assert recorder.request["take_profit"] == {"limit_price": 112}
    assert recorder.request["stop_loss"] == {"stop_price": 94}
    assert recorder.request["client_order_id"] == "stable"
    assert [(leg["broker_order_id"], leg["role"], leg["qty"], leg["side"], leg["symbol"])
            for leg in result["legs"]] == [("target", "take_profit", 10, "sell", "XLV"),
                                           ("stop", "stop_loss", 10, "sell", "XLV")]


def test_stable_id_exact_quantity_reduction(paper_settings):
    class Recorder:
        def submit_order(self, request):
            self.request = request.model_dump(exclude_none=True)
            return oco()
    recorder = Recorder()
    AlpacaClient(paper_settings, trading_client=recorder).submit_position_reduction("XLV", 4, "short", "reduce-1")
    assert recorder.request["qty"] == 4
    assert recorder.request["side"] == "buy"
    assert recorder.request["client_order_id"] == "reduce-1"


def test_order_reads_are_nested_and_fail_closed_at_page_limit(paper_settings):
    class Recorder:
        def get_orders(self, request):
            assert request.limit == 500 and request.nested is True and request.status == "open"
            return [oco()]
        def get_order_by_id(self, order_id, request):
            assert request.nested is True
            return oco()
    recorder = Recorder()
    client = AlpacaClient(paper_settings, trading_client=recorder)
    assert len(client.list_open_orders()[0]["legs"]) == 2
    assert len(client.get_order("target")["legs"]) == 2
    recorder.get_orders = lambda request: [oco()] * 500
    with pytest.raises(AlpacaSafetyError):
        client.list_open_orders()


def test_lookup_only_404_means_absent(paper_settings):
    class Recorder:
        status = 404
        def get_order_by_client_id(self, client_id):
            error = RuntimeError("lookup failed")
            error.status_code = self.status
            raise error
    recorder = Recorder()
    client = AlpacaClient(paper_settings, trading_client=recorder)
    assert client.get_order_by_client_id("stable") is None
    recorder.status = 503
    with pytest.raises(RuntimeError):
        client.get_order_by_client_id("stable")


def test_read_only_account_history_calendar_preserve_unknown_cashflow(paper_settings):
    from pydantic import BaseModel, Field
    class History(BaseModel):
        equity: list[float]
        cashflow: dict = Field(default_factory=dict)
    class Recorder:
        def get_account(self):
            return {"equity": "100", "last_equity": "99"}
        def get_portfolio_history(self, request):
            return History(equity=[100])
        def get_calendar(self, request):
            return [Object(date=date(2026, 7, 14), open=datetime(2026, 7, 14, 9, 30), close=datetime(2026, 7, 14, 16))]
    client = AlpacaClient(paper_settings, trading_client=Recorder())
    account = client.get_account()
    assert account["equity"] == 100 and account["last_equity"] == 99 and account["cash"] is None
    result = client.get_portfolio_history(period="7D", timeframe="1D", cashflow_types="ALL",
                                         end=datetime(2026, 7, 14, tzinfo=timezone.utc))
    assert "cashflow" not in result
    assert client.get_calendar(date(2026, 7, 14), date(2026, 7, 14))[0]["open"] == "2026-07-14T09:30:00"
