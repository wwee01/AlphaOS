"""Common marks preserve source timing and never manufacture economic proof."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
import stat

import pytest

from alphaos.journal.journal_store import JournalStore
from alphaos.reports.common_valuation import collect_common_valuation, reconcile_common_valuation, record_common_valuation
from alphaos.reports.common_valuation_source import CommonValuationSource
from alphaos.reports.relative_performance import build_relative_performance_report, render_markdown
from conftest import make_settings


class FakeSourceTransport:
    def __init__(self, *, calendar=None, start="2026-09-14T21:00:00+00:00"):
        self.clock = datetime.fromisoformat(start)
        self.calls = []
        self.account_reads = 0
        self.position_reads = 0
        self.activity_reads = 0
        self.calendar = calendar or [{"date": "2026-09-11", "open": "09:30", "close": "16:00"},
                                     {"date": "2026-09-14", "open": "09:30", "close": "16:00"}]
        self.account = {"id": "private-account-id", "cash": "900", "currency": "USD", "equity": "1099.21"}
        self.positions = [{"symbol": "XYZ", "qty": "2", "side": "long", "asset_class": "us_equity"}]
        self.activities = []
        self.mutate = None

    def now(self):
        self.clock += timedelta(seconds=1)
        return self.clock

    def get(self, base, path, params):
        self.calls.append((base, path, deepcopy(params)))
        if self.mutate:
            changed = self.mutate(self, path, params)
            if changed is not None:
                return changed
        if path == "/v2/calendar":
            return deepcopy(self.calendar)
        if path == "/v2/account":
            self.account_reads += 1
            return deepcopy(self.account)
        if path == "/v2/positions":
            self.position_reads += 1
            return deepcopy(self.positions)
        if path == "/v2/account/activities":
            self.activity_reads += 1
            return deepcopy(self.activities)
        if path == "/v2/stocks/bars":
            return {"bars": {symbol: [{"t": params["start"], "o": price, "h": price, "l": price, "c": price, "v": 10}]
                             for symbol, price in [(s, 500 if s == "SPY" else 100) for s in params["symbols"].split(",")]},
                    "next_page_token": None}
        raise AssertionError(path)


def source(fake):
    settings = make_settings(ALPHAOS_MODE="paper", OFFLINE_MODE="false", ALPACA_API_KEY="secret-key", ALPACA_SECRET_KEY="secret-secret")
    return CommonValuationSource(settings, transport=fake.get, clock=fake.now)


def capture(fake=None):
    fake = fake or FakeSourceTransport()
    return collect_common_valuation(source(fake), now=fake.clock), fake


def test_same_window_nav_is_independently_computed_and_not_broker_equity():
    result, fake = capture()
    assert result["status"] == "reconstructed_candidate"
    assert result["reconstructed_nav"] == 900 + 2 * 100 == 1100
    assert result["reconstructed_nav"] != float(fake.account["equity"])
    assert result["benchmark_price"] == 500
    assert result["comparison_eligible"] is False
    assert "economic_activity_completeness_unverified" in result["strict_comparison_blockers"]
    assert result["source_interval_start_utc"] == "2026-09-14T19:59:00+00:00"
    assert result["source_interval_end_exclusive_utc"] == "2026-09-14T20:00:00+00:00"
    assert result["valuation_cutoff_utc"] == "2026-09-14T20:00:00+00:00"
    assert len(fake.calls) == 8
    assert fake.account_reads == fake.position_reads == fake.activity_reads == 2
    assert "secret-key" not in str(result) and "private-account-id" not in str(result)
    assert {base for base, _, _ in fake.calls} == {"https://paper-api.alpaca.markets", "https://data.alpaca.markets"}


def test_short_and_flat_cash_books_use_signed_quantities_and_always_fetch_spy():
    fake = FakeSourceTransport()
    fake.positions[0].update(qty="-2", side="short")
    result, _ = capture(fake)
    assert result["reconstructed_nav"] == 700
    fake = FakeSourceTransport()
    fake.positions = []
    result, _ = capture(fake)
    assert result["reconstructed_nav"] == 900 and result["marks"] == {"SPY": 500}


@pytest.mark.parametrize("calendar,start,expected", [
    ([{"date": "2026-11-25", "open": "09:30", "close": "16:00"},
      {"date": "2026-11-27", "open": "09:30", "close": "13:00"}], "2026-11-27T19:00:00+00:00", "2026-11-27T17:59:00+00:00"),
    ([{"date": "2026-09-04", "open": "09:30", "close": "16:00"},
      {"date": "2026-09-08", "open": "09:30", "close": "16:00"}], "2026-09-08T21:00:00+00:00", "2026-09-08T19:59:00+00:00"),
])
def test_calendar_cutoff_covers_holiday_weekends_dst_and_early_close(calendar, start, expected):
    result, _ = capture(FakeSourceTransport(calendar=calendar, start=start))
    assert result["status"] == "reconstructed_candidate"
    assert result["source_interval_start_utc"] == expected
    request = result["raw_evidence"]["activities_after"]["request"]
    assert datetime.fromisoformat(request["after"]).date() < datetime.fromisoformat(calendar[0]["date"]).date()


@pytest.mark.parametrize("cause", ["missing_spy", "missing_holding", "nearby_minute", "duplicate_bar", "mixed_feed", "adjusted",
                                  "unknown_pagination", "changed_cash", "changed_qty", "changed_account", "wrong_currency",
                                  "non_equity", "wrong_direction", "wrong_sequence", "truncated_activities", "activity_filter",
                                  "activity_coverage", "changed_activities", "duplicate_activity"])
def test_unverifiable_common_evidence_never_yields_nav(cause):
    result, _ = capture()
    bundle = deepcopy(result["raw_evidence"])
    if cause == "missing_spy":
        del bundle["bars"]["rows"]["SPY"]
    elif cause == "missing_holding":
        del bundle["bars"]["rows"]["XYZ"]
    elif cause == "nearby_minute":
        bundle["bars"]["rows"]["XYZ"][0]["t"] = "2026-09-14T19:58:00+00:00"
    elif cause == "duplicate_bar":
        bundle["bars"]["rows"]["XYZ"] *= 2
    elif cause in {"mixed_feed", "adjusted"}:
        bundle["bars"]["request"]["feed" if cause == "mixed_feed" else "adjustment"] = "unknown"
    elif cause == "unknown_pagination":
        bundle["bars"]["pagination_exhausted"] = False
    elif cause == "changed_cash":
        bundle["account_after"]["value"]["cash"] = "1000"
    elif cause == "changed_qty":
        bundle["positions_after"]["value"][0]["qty"] = "3"
    elif cause == "changed_account":
        bundle["account_after"]["value"]["account_fingerprint"] = "other"
    elif cause == "wrong_currency":
        bundle["account_before"]["value"]["currency"] = "SGD"
    elif cause == "non_equity":
        bundle["positions_before"]["value"][0]["asset_class"] = "crypto"
    elif cause == "wrong_direction":
        bundle["positions_before"]["value"][0]["qty"] = "-2"
    elif cause == "wrong_sequence":
        bundle["bars"]["requested_at_utc"] = "2026-09-14T19:00:00+00:00"
    elif cause == "truncated_activities":
        bundle["activities_after"]["pagination_exhausted"] = False
    elif cause == "activity_filter":
        bundle["activities_after"]["request"]["activity_types"] = "FILL"
    elif cause == "activity_coverage":
        bundle["activities_after"]["request"]["until"] = "2026-09-14T20:30:00+00:00"
    elif cause == "changed_activities":
        bundle["activities_after"]["rows"] = [{"id": "late"}]
    elif cause == "duplicate_activity":
        bundle["activities_after"]["rows"] = [{"id": "same"}, {"id": "same"}]
    blocked = reconcile_common_valuation(bundle)
    assert blocked["status"] == "blocked" and blocked["reconstructed_nav"] is None
    assert blocked["comparison_eligible"] is False and blocked["blockers"]


def fill(when="2026-09-14T15:00:00Z"):
    return {"id": "fill1", "activity_type": "FILL", "transaction_time": when, "order_id": "order", "symbol": "XYZ",
            "qty": "2", "price": "90", "side": "buy", "type": "fill"}


@pytest.mark.parametrize("activity", [fill("2026-09-14T20:00:00Z"), fill("2026-09-14T20:01:00Z"),
                                     {"id": "fee", "activity_type": "FEE", "net_amount": "-.01", "date": "2026-09-14"},
                                     {"id": "split", "activity_type": "SSP", "qty": "2", "date": "2026-09-14"},
                                     {"id": "deposit", "activity_type": "CSD", "net_amount": "100", "transaction_time": "2026-09-14T20:30:00Z"}])
def test_postcutoff_fills_or_date_only_corporate_cash_events_block_reconstruction(activity):
    fake = FakeSourceTransport()
    fake.activities = [activity]
    result, _ = capture(fake)
    assert result["status"] == "blocked" and result["comparison_eligible"] is False


def test_confirmed_intraday_flows_are_retained_and_not_net_zero_away():
    fake = FakeSourceTransport()
    fake.activities = [fill(),
        {"id": "deposit", "activity_type": "CSD", "net_amount": "100", "transaction_time": "2026-09-14T14:00:00Z"},
        {"id": "withdrawal", "activity_type": "CSW", "net_amount": "-100", "transaction_time": "2026-09-14T16:00:00Z"},
        {"id": "fee", "activity_type": "FEE", "net_amount": "-.01", "transaction_time": "2026-09-14T15:00:00Z"}]
    result, _ = capture(fake)
    assert result["status"] == "reconstructed_candidate"
    assert result["activity_summary"]["external_cash"] == 0
    assert result["activity_summary"]["external_event_count"] == 2
    assert result["activity_summary"]["internal_cash"] == -.01
    assert result["reconstructed_nav"] == 1100  # no fee add-back or guessed TWR adjustment
    assert "external_flows_require_subperiod_valuations" in result["strict_comparison_blockers"]


def test_activity_pagination_is_exhausted_without_treating_first_full_page_as_complete():
    fake = FakeSourceTransport()
    def pages(current, path, params):
        if path == "/v2/account/activities":
            return [dict(fill(), id=str(i)) for i in range(100)] if "page_token" not in params else []
    fake.mutate = pages
    result, _ = capture(fake)
    assert result["status"] == "reconstructed_candidate"
    assert len(result["raw_evidence"]["activities_before"]["pages"]) == 2
    assert result["raw_evidence"]["activities_before"]["pages"][1]["request"]["page_token"] == "99"


@pytest.mark.parametrize("cause", ["repeated_ids", "never_exhausted", "unavailable_bars", "call_cap", "bad_endpoint"])
def test_source_refuses_incomplete_provider_responses(cause):
    fake = FakeSourceTransport()
    def response(current, path, params):
        if path == "/v2/account/activities":
            offset = 0 if cause == "repeated_ids" else len(current.calls) * 100
            return [dict(fill(), id=str(i + offset)) for i in range(100)]
        if path == "/v2/stocks/bars" and cause == "unavailable_bars":
            return {"bars": None}
    fake.mutate = response
    src = source(fake)
    if cause == "call_cap":
        src.calls = src.MAX_CALLS
    with pytest.raises(ValueError):
        if cause == "bad_endpoint":
            src._get("/v2/orders")
        else:
            collect_common_valuation(src, fake.clock)


def test_candidate_persistence_is_explicit_append_only_and_cannot_enable_report_returns():
    journal = JournalStore(":memory:")
    original = journal.query("SELECT * FROM equity_close_snapshots")
    result, _ = capture()
    assert journal.query("SELECT * FROM system_events") == []
    result["comparison_eligible"] = True  # a stored flag cannot promote the method
    record_common_valuation(journal, result)
    record_common_valuation(journal, result)
    assert len(journal.query("SELECT * FROM system_events")) == 2
    assert journal.query("SELECT * FROM equity_close_snapshots") == original
    report = build_relative_performance_report(journal, make_settings())
    assert report["common_valuation"]["comparison_eligible"] is False
    assert report["common_valuation"]["reconstructed_nav"] == 1100
    assert "raw_evidence" not in report["common_valuation"]
    assert report["excess_return_pct"] is report["rolling_beta"] is None
    assert "Separate reconstructed NAV candidate" in render_markdown(report)
    journal.close()


def test_missing_calendar_session_cannot_hide_a_weekday_gap():
    fake = FakeSourceTransport()
    fake.calendar[0]["date"] = "2026-09-10"  # Friday is missing, not a weekend
    result, _ = capture(fake)
    assert result["status"] == "blocked" and "adjacent trading sessions" in result["blockers"][0]


def test_no_silent_change_to_a_different_valid_feed():
    result, _ = capture()
    bundle = result["raw_evidence"]
    bundle["bars"]["request"]["feed"] = "sip"
    assert reconcile_common_valuation(bundle)["status"] == "blocked"


def test_existing_provisional_source_time_is_not_reported_as_verified_cutoff():
    from alphaos.reports.close_equity import VALUATION_METHOD
    journal = JournalStore(":memory:")
    journal.insert("equity_close_snapshots", {"snapshot_id": "old", "market_date": "2026-09-11", "equity": 95629.17,
        "equity_source": "broker_portfolio_history_intraday", "valuation_method_version": VALUATION_METHOD,
        "valuation_at_utc": "2026-09-11T20:00:00+00:00", "source_timestamp_utc": "2026-09-11T19:59:00+00:00",
        "captured_at_utc": "2026-09-12T12:34:42+00:00", "cashflow_status": "none_reported", "raw_evidence_json": {}})
    before = journal.query("SELECT * FROM equity_close_snapshots")
    point = build_relative_performance_report(journal, make_settings())["provisional_account_value_curve"][0]
    assert point["valuation_at_utc"] is None
    assert point["source_timestamp_utc"] == "2026-09-11T19:59:00+00:00"
    assert point["recorded_documented_window_end_utc"] == "2026-09-11T20:00:00+00:00"
    assert journal.query("SELECT * FROM equity_close_snapshots") == before
    journal.close()


def test_manual_offline_tool_writes_new_private_evidence_without_broker_calls(tmp_path, monkeypatch):
    from alphaos.reports.common_valuation_capture import main
    result, _ = capture()
    incoming, outgoing = tmp_path / "input.json", tmp_path / "output.json"
    incoming.write_text(json.dumps(result))
    monkeypatch.setattr(CommonValuationSource, "__init__", lambda *a, **k: pytest.fail("offline path touched broker source"))
    assert main(["--input", str(incoming), "--output", str(outgoing)]) == 0
    assert stat.S_IMODE(outgoing.stat().st_mode) == 0o600
    saved = json.loads(outgoing.read_text())
    assert saved["input_sha256"] == result["input_sha256"] and saved["comparison_eligible"] is False
    original = outgoing.read_bytes()
    with pytest.raises(SystemExit):
        main(["--input", str(incoming), "--output", str(outgoing)])
    assert outgoing.read_bytes() == original


def test_manual_tool_failure_is_nonzero_and_does_not_expose_exception_secrets(tmp_path, monkeypatch):
    from alphaos.reports.common_valuation_capture import main
    outgoing = tmp_path / "failure.json"
    def failure(*a, **k):
        raise ValueError("credential-bearing provider response secret-key")
    monkeypatch.setattr(CommonValuationSource, "__init__", failure)
    assert main(["--collect", "--output", str(outgoing)]) == 2
    saved = json.loads(outgoing.read_text())
    assert saved["status"] == "capture_failed" and "secret-key" not in str(saved)


@pytest.mark.parametrize("nav", [None, "1100", "invalid", True, float("nan"), float("inf"), 10**1000, -1, {"nav": 1100}])
def test_malformed_candidate_dict_cannot_crash_report_render(nav):
    journal = JournalStore(":memory:")
    result, _ = capture()
    result["reconstructed_nav"] = nav
    record_common_valuation(journal, result)
    report = build_relative_performance_report(journal, make_settings())
    assert report["common_valuation"]["status"] == "evidence_unreadable"
    assert report["excess_return_pct"] is None
    render_markdown(report)
    journal.close()


@pytest.mark.parametrize("detail", [{"status": "reconstructed_candidate"}, [], "invalid json", {"status": "blocked"}])
def test_missing_schema_or_invalid_json_is_explicitly_unreadable(detail):
    journal = JournalStore(":memory:")
    from alphaos.reports.common_valuation import CATEGORY
    journal.log_system_event("warning", CATEGORY, "invalid", detail)
    report = build_relative_performance_report(journal, make_settings())
    assert report["common_valuation"]["status"] == "evidence_unreadable"
    render_markdown(report)
    journal.close()
