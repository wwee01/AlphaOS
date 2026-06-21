"""Mock and Alpaca-paper execution share ONE order schema (test #6)."""

from __future__ import annotations

from alphaos.constants import ExecutionSource
from alphaos.execution.order_schema import ORDER_ROW_FIELDS, build_order_row
from alphaos.execution.order_manager import OrderManager
from conftest import make_settings, make_proposal


def _row(source, proposal):
    return build_order_row(
        order_id="ord_x", proposal=proposal, side="buy", order_type="bracket",
        execution_source=source, protection_path="BROKER_NATIVE_BRACKET", state="filled",
        qty=10, entry_price=100.0, take_profit_price=106.0, stop_loss_price=97.0,
    )


def test_mock_and_alpaca_rows_have_identical_shape():
    # Same proposal so only the source label can legitimately differ.
    proposal = make_proposal()
    mock_row = _row(ExecutionSource.MOCK.value, proposal)
    alpaca_row = _row(ExecutionSource.ALPACA_PAPER.value, proposal)
    assert set(mock_row.keys()) == set(alpaca_row.keys()) == set(ORDER_ROW_FIELDS)
    # Only the source label differs in this construction.
    diffs = {k for k in mock_row if mock_row[k] != alpaca_row[k]}
    assert diffs == {"execution_source"}


def test_persisted_order_has_all_schema_fields(journal):
    s = make_settings()
    om = OrderManager(s, journal)
    result = om.execute_proposal(make_proposal())
    assert result.blocked is False
    row = journal.one("SELECT * FROM paper_orders WHERE order_id = ?", (result.order["order_id"],))
    for field in ORDER_ROW_FIELDS:
        assert field in row
