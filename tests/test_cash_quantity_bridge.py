"""Independent examples for cash, holdings, transfers and retained receipts."""
from copy import deepcopy

import pytest

from alphaos.reports.cash_quantity_bridge import reconcile_cash_quantity_bridge
from alphaos.reports.common_valuation import reconcile_common_valuation
from test_common_valuation import FakeSourceTransport, capture


def endpoints(activities=(), cash="900", positions=None):
    start, _ = capture()
    fake = FakeSourceTransport(calendar=[{"date": "2026-09-14", "open": "09:30", "close": "16:00"},
                                        {"date": "2026-09-15", "open": "09:30", "close": "16:00"}],
                               start="2026-09-15T21:00:00+00:00")
    fake.account["cash"] = cash
    if positions is not None:
        fake.positions = positions
    fake.activities = list(activities)
    end, _ = capture(fake)
    return start, end


def fill(key, qty, price, side="sell"):
    return {"id": key, "activity_type": "FILL", "transaction_time": "2026-09-15T15:00:00Z",
            "order_id": key, "symbol": "XYZ", "qty": str(qty), "price": str(price),
            "side": side, "type": "partial_fill"}


def cash_event(key, kind, value):
    return {"id": key, "activity_type": kind, "transaction_time": "2026-09-15T16:00:00Z", "net_amount": str(value)}


def test_partial_sell_then_flat_books_costs_once_and_retains_exact_movements():
    # Start $900 cash + 2 shares marked at $100; sell at $105/$106,
    # pay $1 fee and receive $2 dividend: ending wealth is $1112.
    rows = [fill("one", 1, 105), fill("two", 1, 106), cash_event("fee", "FEE", -1), cash_event("div", "DIV", 2)]
    a, b = endpoints(rows, cash="1112", positions=[])
    originals = deepcopy((a, b))
    result = reconcile_cash_quantity_bridge(a, b)
    assert result["status"] == "returned_activities_reconciled"
    assert result["fill_cash"] == "211" and result["internal_cash"] == "1"
    assert float(result["wealth_change_less_external_flows"]) == 12
    assert result["quantity_residuals"] == {"XYZ": "0"}
    assert result["comparison_eligible"] is False
    assert (a, b) == originals


def test_offsetting_transfers_stay_visible_even_with_zero_net_cash():
    a, b = endpoints([cash_event("in", "CSD", 100), cash_event("out", "CSW", -100)])
    result = reconcile_cash_quantity_bridge(a, b)
    assert result["status"] == "returned_activities_reconciled"
    assert result["external_event_count"] == 2 and result["external_net_inflow"] == "0"
    assert "external_flows_require_subperiod_valuations" in result["strict_comparison_blockers"]


@pytest.mark.parametrize("activities,cash,positions,blocker", [
    ([], "901", None, "cash_does_not_reconcile"),
    ([fill("sell", 1, 100)], "1000", None, "holdings_do_not_reconcile"),
    ([cash_event("bad", "CSW", 100)], "1000", None, "external transfer direction"),
])
def test_missing_cash_or_quantity_evidence_is_not_certified(activities, cash, positions, blocker):
    result = reconcile_cash_quantity_bridge(*endpoints(activities, cash, positions))
    assert result["status"] == "blocked" and any(blocker in v for v in result["blockers"])
    assert "wealth_change_less_external_flows" not in result


def test_derived_endpoint_tampering_is_ignored_but_raw_receipt_tampering_refused():
    a, b = endpoints()
    b["reconstructed_nav"] = 999999
    result = reconcile_cash_quantity_bridge(a, b)
    assert float(result["wealth_change_less_external_flows"]) == 0
    b["raw_evidence"]["account_after"]["value"]["cash"] = "1000"
    assert reconcile_cash_quantity_bridge(a, b)["status"] == "blocked"


def test_late_old_period_activity_requires_review_even_if_offsetting():
    rows = [cash_event("in", "CSD", 100), cash_event("out", "CSW", -100)]
    for row in rows:
        row["transaction_time"] = "2026-09-14T16:00:00Z"
    result = reconcile_cash_quantity_bridge(*endpoints(rows))
    assert result["status"] == "blocked"
    assert "late prior-period activity" in result["blockers"][0]


@pytest.mark.parametrize("mutation", ["different_account", "different_feed", "missing_receipt", "late_start_capture"])
def test_endpoint_identity_and_prospective_receipts(mutation):
    a, b = endpoints()
    if mutation == "different_account":
        for key in ("account_before", "account_after"):
            b["raw_evidence"][key]["value"]["account_fingerprint"] = "other"
    elif mutation == "different_feed":
        b["raw_evidence"]["configured_feed"] = "sip"
        b["raw_evidence"]["bars"]["request"]["feed"] = "sip"
    elif mutation == "late_start_capture":
        a["raw_evidence"]["captured_at_utc"] = "2026-09-16T21:00:00Z"
        a = reconcile_common_valuation(a["raw_evidence"])
    b = reconcile_common_valuation(b["raw_evidence"])
    if mutation == "missing_receipt":
        b.pop("input_sha256")
    result = reconcile_cash_quantity_bridge(a, b)
    assert result["status"] == "blocked" and result["comparison_eligible"] is False


@pytest.mark.parametrize("mutation", ["changed", "missing"])
def test_older_transaction_times_cannot_hide_changed_or_missing_retained_receipts(mutation):
    a, b = endpoints()
    original = [cash_event("old-in", "CSD", 100), cash_event("old-out", "CSW", -100)]
    for row in original:
        row["transaction_time"] = "2026-09-01T16:00:00Z"
    newer = deepcopy(original)
    if mutation == "changed":
        newer[0]["net_amount"], newer[1]["net_amount"] = "999", "-999"
    else:
        newer = []
    for capture_result, rows in ((a, original), (b, newer)):
        for key in ("activities_before", "activities_after"):
            capture_result["raw_evidence"][key]["rows"] = deepcopy(rows)
    a = reconcile_common_valuation(a["raw_evidence"])
    b = reconcile_common_valuation(b["raw_evidence"])
    assert a["status"] == b["status"] == "reconstructed_candidate"
    result = reconcile_cash_quantity_bridge(a, b)
    assert result["status"] == "blocked"
    assert "previously observed activity" in result["blockers"][0]
