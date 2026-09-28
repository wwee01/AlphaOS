"""Conservation, common endpoints, economic conventions and adverse receipts."""

from copy import deepcopy
from datetime import datetime

import pytest

from alphaos.reports.account_economics import measure_account_economics, operating_cost
from alphaos.reports.common_valuation import evidence_hash
from test_cash_quantity_bridge import endpoints, fill, cash_event


def inputs(rows=(), cash="900", positions=None):
    a, b = endpoints(rows, cash, positions)
    for key in ("activities_before", "activities_after"):
        b["raw_evidence"][key]["request"]["after"] = a["raw_evidence"][key]["request"][
            "after"
        ]
    for key in ("activities_before", "activities_after"):
        b["raw_evidence"][key]["pages"][0]["request"]["after"] = b["raw_evidence"][key][
            "request"
        ]["after"]
    b["input_sha256"] = evidence_hash(b["raw_evidence"])
    value = {
        "schema": "cash_basis_economic_inputs_v1",
        "start_capture": a,
        "end_capture": b,
        "corporate_actions": {
            "request": {
                "symbols": ["XYZ", "SPY"],
                "data_quality": "all",
                "start": "2025-09-13",
                "end": "2027-09-17",
            },
            "rows": {},
            "pagination_exhausted": True,
            "requested_at_utc": "2026-09-15T22:59:00Z",
            "received_at_utc": "2026-09-15T23:00:00Z",
        },
    }
    ca = value["corporate_actions"]
    ca["pages"] = [
        {
            "request": {**ca["request"], "symbols": ",".join(ca["request"]["symbols"])},
            "requested_at_utc": ca["requested_at_utc"],
            "received_at_utc": ca["received_at_utc"],
            "next_page_token": None,
        }
    ]
    return value


def resign(value, which="end_capture"):
    # Fixture mutations model the corresponding raw single-page response too.
    b = value[which]["raw_evidence"]
    for key in ("activities_before", "activities_after"):
        if len(b[key]["pages"]) == 1:
            b[key]["pages"][0]["row_count"] = len(b[key]["rows"])
            b[key]["pages"][0]["request"] = deepcopy(b[key]["request"])
    value[which]["input_sha256"] = evidence_hash(b)


def marks(value, symbol, price):
    for key in ("o", "h", "l", "c"):
        value["end_capture"]["raw_evidence"]["bars"]["rows"][symbol][0][key] = price
    resign(value)


def test_real_conservation_returns_use_cash_and_executions_not_double_modeled_cost():
    rows = [
        fill("one", 1, 105),
        fill("two", 1, 106),
        cash_event("fee", "FEE", -1),
        cash_event("div", "DIV", 2),
    ]
    value = inputs(rows, cash="1112", positions=[])
    marks(value, "SPY", 505)
    originals = deepcopy(value)
    result = measure_account_economics(value)
    assert (
        result["status"] == "reconciled_asof" and result["comparison_eligible"] is True
    )
    assert (
        result["account_gain_usd"] == "12"
    )  # $11 fill gain - $1 fee + $2 distribution
    assert float(result["benchmark_gain_usd"]) == 11
    assert float(result["excess_gain_usd"]) == 1
    assert result["reported_broker_fee_cash"] == "-1"
    assert result["modeled_trade_cost_deduction"] == "0"
    assert (
        result["research_activation"] is False
        and result["final_or_accrued_comparison_eligible"] is False
    )
    assert originals == value


def test_spy_cash_dividend_included_only_after_purchase_entitlement_and_payment():
    value = inputs()
    value["corporate_actions"]["rows"] = {
        "cash_dividends": [
            {
                "id": "d1",
                "symbol": "SPY",
                "rate": "2",
                "ex_date": "2026-09-15",
                "payable_date": "2026-09-15",
                "special": False,
                "foreign": False,
            }
        ]
    }
    marks(value, "SPY", 498)
    result = measure_account_economics(value)
    assert (
        result["comparison_eligible"] is True and float(result["benchmark_return"]) == 0
    )
    assert result["benchmark_cash_dividend_per_share"] == "2"
    # Buying on the ex-date does not acquire the older entitlement.
    value["corporate_actions"]["rows"]["cash_dividends"][0]["ex_date"] = "2026-09-14"
    result = measure_account_economics(value)
    assert result["benchmark_cash_dividend_per_share"] == "0"
    # An entitled but unpaid distribution is not cash-basis profit yet.
    value["corporate_actions"]["rows"]["cash_dividends"][0].update(
        ex_date="2026-09-15", payable_date="2026-09-17"
    )
    assert measure_account_economics(value)["benchmark_cash_dividend_per_share"] == "0"


@pytest.mark.parametrize(
    "rows,cash,blocker",
    [
        ([], "901", "cash_does_not_reconcile"),
        ([fill("one", 1, 100)], "1000", "holdings_do_not_reconcile"),
        ([cash_event("bad", "CSW", 1)], "901", "external transfer direction"),
        ([cash_event("bad", "UNKNOWN", 1)], "901", "unsupported economic activity"),
    ],
)
def test_missing_and_unknown_movements_refuse_economics(rows, cash, blocker):
    result = measure_account_economics(inputs(rows, cash))
    assert result["status"] == "blocked" and any(
        blocker in s for s in result["blockers"]
    )
    assert not result["comparison_eligible"]


def test_offsetting_external_flows_not_profit_and_not_fake_time_weighted_return():
    value = inputs([cash_event("in", "CSD", 100), cash_event("out", "CSW", -100)])
    result = measure_account_economics(value)
    assert result["status"] == "reconciled_asof" and result["account_gain_usd"] == "0"
    assert len(result["external_events"]) == 2
    assert not result["comparison_eligible"] and result["benchmark_return"] is None
    assert "account_return" not in result


def test_single_external_flow_is_conserved_dollar_gain_remains_usable():
    result = measure_account_economics(
        inputs([cash_event("in", "CSD", 100)], cash="1000")
    )
    assert result["external_net_inflow"] == "100" and result["account_gain_usd"] == "0"
    assert not result["comparison_eligible"]


def test_multisession_cash_basis_accepts_date_only_internal_events_wholly_inside_interval():
    value = inputs(
        [
            {
                "id": "fee",
                "activity_type": "FEE",
                "date": "2026-09-15",
                "net_amount": "-1",
            }
        ],
        cash="899",
    )
    b = value["end_capture"]["raw_evidence"]

    # Shift entire end receipt one day; the Sept15 fee falls wholly inside.
    def shift(obj):
        if isinstance(obj, str):
            return obj.replace("2026-09-15", "2026-09-16")
        if isinstance(obj, list):
            return [shift(v) for v in obj]
        if isinstance(obj, dict):
            return {k: shift(v) for k, v in obj.items()}
        return obj

    value["end_capture"]["raw_evidence"] = shift(b)
    for key in ("activities_before", "activities_after"):
        value["end_capture"]["raw_evidence"][key]["rows"][0]["date"] = "2026-09-15"
    value["corporate_actions"]["received_at_utc"] = "2026-09-17T23:00:00Z"
    value["corporate_actions"]["pages"][0]["received_at_utc"] = "2026-09-17T23:00:00Z"
    resign(value)
    result = measure_account_economics(value)
    assert result["status"] == "reconciled_asof" and result["account_gain_usd"] == "-1"
    assert (
        result["activities"][0]["time_basis"]
        == "broker_date_interval_occurrence_or_settlement"
    )


def test_date_only_fee_straddling_cutoff_is_not_given_fabricated_posting_time():
    value = inputs(
        [
            {
                "id": "fee",
                "activity_type": "FEE",
                "date": "2026-09-15",
                "net_amount": "-1",
                "created_at": "2026-09-15T19:00:00Z",
            }
        ],
        cash="899",
    )
    result = measure_account_economics(value)
    assert not result["comparison_eligible"] and "straddles" in result["blockers"][0]


def test_postcutoff_fill_reversal_recovers_endpoint_when_historical_mark_present():
    row = fill("late", 1, 101)
    row["transaction_time"] = "2026-09-15T20:30:00Z"
    value = inputs(
        [row],
        cash="1001",
        positions=[
            {"symbol": "XYZ", "qty": "1", "side": "long", "asset_class": "us_equity"}
        ],
    )
    result = measure_account_economics(value)
    assert result["status"] == "reconciled_asof"
    assert result["cash_end"] == "900" and result["end_quantities"] == {"XYZ": "2"}
    assert result["account_gain_usd"] == "0"
    assert result["endpoint_reversals"]["end"] == ["late"]


def test_postcutoff_flattening_requires_mark_for_preclose_holding():
    row = fill("late", 2, 101)
    row["transaction_time"] = "2026-09-15T20:30:00Z"
    result = measure_account_economics(inputs([row], cash="1102", positions=[]))
    assert result["status"] == "blocked" and "marks missing" in result["blockers"][0]


@pytest.mark.parametrize(
    "mutation",
    [
        "raw_hash",
        "changed_account",
        "changed_cash",
        "changed_holdings",
        "bar_time",
        "bar_feed",
        "bar_adjustment",
        "missing_bar",
        "duplicate_bar",
        "empty_activity",
        "duplicate_activity",
        "activity_pagination",
        "late_activity",
        "changed_retained",
        "missing_retained",
        "corporate_pagination",
        "corporate_symbols",
        "corporate_filtered",
        "corporate_unknown",
        "corporate_incomplete",
        "corporate_duplicate",
        "nan",
        "bool",
    ],
)
def test_adverse_receipts_block_without_mutation(mutation):
    value = inputs()
    b = value["end_capture"]["raw_evidence"]
    if mutation == "raw_hash":
        b["account_after"]["value"]["cash"] = "1000"
    elif mutation == "changed_account":
        b["account_after"]["value"]["account_fingerprint"] = "other"
    elif mutation == "changed_cash":
        b["account_after"]["value"]["cash"] = "1000"
    elif mutation == "changed_holdings":
        b["positions_after"]["value"][0]["qty"] = "3"
    elif mutation == "bar_time":
        b["bars"]["rows"]["SPY"][0]["t"] = "2026-09-15T19:58:00Z"
    elif mutation == "bar_feed":
        b["bars"]["request"]["feed"] = "sip"
    elif mutation == "bar_adjustment":
        b["bars"]["request"]["adjustment"] = "all"
    elif mutation == "missing_bar":
        del b["bars"]["rows"]["XYZ"]
    elif mutation == "duplicate_bar":
        b["bars"]["rows"]["XYZ"] *= 2
    elif mutation == "activity_pagination":
        b["activities_after"]["pagination_exhausted"] = False
    elif mutation in {"empty_activity", "duplicate_activity", "late_activity"}:
        row = cash_event("new", "INT", 0)
        if mutation == "empty_activity":
            row["id"] = ""
        elif mutation == "late_activity":
            row["transaction_time"] = "2026-09-14T16:00:00Z"
        for key in ("activities_before", "activities_after"):
            b[key]["rows"] = [row] * (2 if mutation == "duplicate_activity" else 1)
    elif mutation in {"changed_retained", "missing_retained"}:
        row = cash_event("old", "INT", 0)
        row["transaction_time"] = "2026-09-14T15:00:00Z"
        for key in ("activities_before", "activities_after"):
            value["start_capture"]["raw_evidence"][key]["rows"] = [row]
            b[key]["rows"] = (
                [] if mutation == "missing_retained" else [{**row, "net_amount": "1"}]
            )
        resign(value, "start_capture")
    elif mutation == "corporate_pagination":
        value["corporate_actions"]["pagination_exhausted"] = False
    elif mutation == "corporate_symbols":
        value["corporate_actions"]["request"]["symbols"] = ["SPY"]
    elif mutation == "corporate_filtered":
        value["corporate_actions"]["request"]["types"] = "cash_dividend"
    elif mutation in {
        "corporate_unknown",
        "corporate_incomplete",
        "corporate_duplicate",
    }:
        row = {"id": "split", "symbol": "XYZ", "ex_date": "2026-09-15"}
        if mutation == "corporate_incomplete":
            del row["ex_date"]
        value["corporate_actions"]["rows"] = {
            "forward_splits": [row] * (2 if mutation == "corporate_duplicate" else 1)
        }
    elif mutation in {"nan", "bool"}:
        b["account_after"]["value"]["cash"] = "NaN" if mutation == "nan" else True
    if mutation != "raw_hash":
        resign(value)
    original = deepcopy(value)
    result = measure_account_economics(value)
    assert (
        result["status"] == "blocked"
        and not result["comparison_eligible"]
        and result["blockers"]
    )
    assert value == original


def test_declared_monthly_overhead_separate_from_broker_fees_and_not_claimed_actual():
    value = inputs()
    value["operating_cost"] = {
        "basis": "declared_monthly_budget",
        "monthly_usd": "150",
        "timezone": "Asia/Singapore",
    }
    result = measure_account_economics(value)
    assert result["operating_cost_usd"] == "5"
    assert result["account_gain_after_declared_overhead_usd"] == "-5"
    assert (
        result["reported_broker_fee_cash"] == "0"
        and result["broker_fee_completeness"] == "not_certified"
    )
    # Cross-month allocation uses each month's actual length.
    cost = operating_cost(
        datetime.fromisoformat("2026-09-30T00:00:00+08:00"),
        datetime.fromisoformat("2026-10-02T00:00:00+08:00"),
        value["operating_cost"],
    )
    assert float(cost) == pytest.approx(5 + 150 / 31)


def test_retained_real_sep18_to_sep25_cash_and_60_shares_reconcile_exactly():
    import json
    from pathlib import Path

    value = json.loads(
        (
            Path(__file__).parent / "fixtures/economic_accounting/sep18_sep25_2026.json"
        ).read_text()
    )
    value["operating_cost"] = {
        "basis": "declared_monthly_budget",
        "monthly_usd": "150",
        "timezone": "Asia/Singapore",
    }
    r = measure_account_economics(value)
    assert r["comparison_eligible"] is True and r["status"] == "reconciled_asof"
    assert r["start_nav"] == "95582.59" and r["end_nav"] == "96321.75"
    assert r["fill_cash"] == "-37089.04" and len(r["activities"]) == 11
    assert r["end_quantities"] == {"AMD": "60"} and r["quantity_residuals"] == {
        "AMD": "0"
    }
    assert r["cash_residual"] == "0.00"
    assert r["account_gain_usd"] == "739.16"
    assert float(r["benchmark_gain_usd"]) == pytest.approx(1221.10580171214)
    assert float(r["excess_gain_usd"]) == pytest.approx(-481.94580171214)
    assert (
        r["operating_cost_usd"] == "35"
        and r["account_gain_after_declared_overhead_usd"] == "704.16"
    )
    assert (
        r["broker_fee_completeness"] == "not_certified"
        and r["reported_broker_fee_cash"] == "0"
    )
    amd = [row for row in r["reported_order_fills"] if row["symbol"] == "AMD"]
    assert sorted(row["reported_incremental_qty"] for row in amd) == ["18", "21", "21"]
    assert sorted(row["fill_count"] for row in amd) == [1, 4, 6]
    assert all(
        row["final_cumulative_qty"] == row["reported_incremental_qty"] for row in amd
    )


@pytest.mark.parametrize("qty", ["1", "4", "6", "19"])
def test_real_receipt_removed_or_changed_fill_never_reconciles(qty):
    import json
    from pathlib import Path

    value = json.loads(
        (
            Path(__file__).parent / "fixtures/economic_accounting/sep18_sep25_2026.json"
        ).read_text()
    )
    for key in ("activities_before", "activities_after"):
        value["end_capture"]["raw_evidence"][key]["rows"][-1]["qty"] = qty
    resign(value)
    result = measure_account_economics(value)
    assert result["status"] == "blocked" and not result["comparison_eligible"]


def test_incremental_partial_fills_are_not_summed_as_cumulative_and_bad_cumulative_refused():
    rows = [fill("a", 4, 100), fill("b", 6, 102)]
    for row in rows:
        row.update(order_id="same-order", side="buy")
    rows[0].update(cum_qty="4", transaction_time="2026-09-15T15:00:00Z")
    rows[1].update(cum_qty="10", transaction_time="2026-09-15T15:01:00Z")
    value = inputs(
        rows,
        cash="-112",
        positions=[
            {"symbol": "XYZ", "qty": "12", "side": "long", "asset_class": "us_equity"}
        ],
    )
    result = measure_account_economics(value)
    assert result["comparison_eligible"]
    assert result["reported_order_fills"][0]["reported_vwap"] == "101.2"
    assert result["account_gain_usd"] == "-12"
    for key in ("activities_before", "activities_after"):
        value["end_capture"]["raw_evidence"][key]["rows"][1]["cum_qty"] = "14"
    resign(value)
    assert "incremental/cumulative" in measure_account_economics(value)["blockers"][0]


@pytest.mark.parametrize(
    "bad", [None, [], {"schema": "cash_basis_economic_inputs_v1", "start_capture": []}]
)
def test_malformed_container_returns_blocked(bad):
    assert measure_account_economics(bad)["status"] == "blocked"


@pytest.mark.parametrize(
    "where,mutation",
    [
        (w, m)
        for w in ["activities_after", "bars", "corporate_actions"]
        for m in ["empty", "request", "bounds", "terminal"]
    ],
)
def test_contradictory_pagination_receipts_fail_even_with_exhausted_flag(
    where, mutation
):
    value = inputs()
    part = (
        value["corporate_actions"]
        if where == "corporate_actions"
        else value["end_capture"]["raw_evidence"][where]
    )
    if mutation == "empty":
        part["pages"] = []
    elif mutation == "request":
        part["pages"][0]["request"]["direction"] = "desc"
    elif mutation == "bounds":
        part["pages"][0]["received_at_utc"] = "2026-09-15T23:10:00Z"
    elif mutation == "terminal":
        if where == "activities_after":
            part["pages"][0]["row_count"] = 100
        else:
            part["pages"][0]["next_page_token"] = "not-exhausted"
    value["end_capture"]["input_sha256"] = evidence_hash(
        value["end_capture"]["raw_evidence"]
    )
    r = measure_account_economics(value)
    assert r["status"] == "blocked" and not r["comparison_eligible"] and r["blockers"]


def test_late_invalid_overhead_cannot_leave_reportable_returns_or_profits():
    value = inputs()
    value["operating_cost"] = {
        "basis": "declared_monthly_budget",
        "monthly_usd": "-150",
        "timezone": "Asia/Singapore",
    }
    r = measure_account_economics(value)
    assert r["status"] == "blocked" and "negative operating" in r["blockers"][0]
    assert not any(k.endswith("return") or "gain" in k for k in r)


@pytest.mark.parametrize("state", ["canceled", "unconfirmed", "pending"])
def test_unconfirmed_dividend_schedule_not_counted(state):
    value = inputs()
    value["corporate_actions"]["rows"] = {
        "cash_dividends": [
            {
                "id": "d",
                "symbol": "SPY",
                "rate": "1",
                "ex_date": "2026-09-15",
                "payable_date": "2026-09-15",
                "status": state,
            }
        ]
    }
    r = measure_account_economics(value)
    assert r["status"] == "blocked" and "unconfirmed" in r["blockers"][0]


def test_short_inventory_has_signed_price_exposure_and_correct_cover_cash():
    value = inputs([fill("cover", 2, 95, "buy")], cash="710", positions=[])
    for key in ("positions_before", "positions_after"):
        value["start_capture"]["raw_evidence"][key]["value"][0].update(
            qty="-2", side="short"
        )
    resign(value, "start_capture")
    r = measure_account_economics(value)
    assert (
        r["comparison_eligible"]
        and r["start_nav"] == "700"
        and r["account_gain_usd"] == "10"
    )


def test_cent_rounding_is_reported_residual_not_hidden_or_used_as_extra_fee():
    value = inputs(cash="900.01")
    r = measure_account_economics(value)
    assert r["comparison_eligible"] and r["cash_residual"] == "0.01"
    assert r["account_gain_usd"] == "0.01" and r["reported_broker_fee_cash"] == "0"
    value = inputs(cash="900.011")
    assert "cash_does_not_reconcile" in measure_account_economics(value)["blockers"]


def test_date_only_postcutoff_cash_is_not_reversed_as_known_posting():
    row = {
        "id": "later-fee",
        "activity_type": "FEE",
        "date": "2026-09-16",
        "net_amount": "-1",
    }
    value = inputs([row], cash="899")
    # The old capture occurs Sept15 21UTC; move it into the following day so
    # the row is not merely future data. No posting time is supplied.
    b = value["end_capture"]["raw_evidence"]

    def later(v):
        if isinstance(v, str) and v.startswith("2026-09-15T21:"):
            return v.replace("2026-09-15T21:", "2026-09-16T21:")
        if isinstance(v, list):
            return [later(x) for x in v]
        if isinstance(v, dict):
            return {k: later(x) for k, x in v.items()}
        return v

    value["end_capture"]["raw_evidence"] = later(b)
    resign(value)
    r = measure_account_economics(value)
    assert r["status"] == "blocked" and "known posting" in r["blockers"][0]
