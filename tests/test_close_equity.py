"""Broker-shaped close valuation evidence; never real network or credentials."""
from datetime import datetime, timezone

import pytest

from alphaos.journal.journal_store import JournalStore
from alphaos.reports.close_equity import capture_close_equity, daily_close_evidence
from alphaos.reports.relative_performance import build_relative_performance_report
from conftest import make_settings


def _epoch(value):
    return datetime.fromisoformat(value).timestamp()


def _history():
    return {"timeframe": "1D", "timestamp": [_epoch("2026-09-10T09:30:00-04:00"), _epoch("2026-09-11T09:30:00-04:00")],
            "equity": [100000, 101000], "cashflow": {}}


def _calendar():
    return [{"date": "2026-09-10", "open": "09:30", "close": "16:00"},
            {"date": "2026-09-11", "open": "09:30", "close": "2026-09-11T16:00:00"}]


def test_premarket_capture_never_labels_current_equity_as_today_close():
    # Broker includes a mutable current-day point; only yesterday can land.
    rows = daily_close_evidence(_history(), _calendar(), datetime(2026, 9, 11, 14, tzinfo=timezone.utc))
    assert [r["market_date"] for r in rows] == ["2026-09-10"]
    assert rows[0]["valuation_at_utc"] == "2026-09-10T20:00:00+00:00"
    assert rows[0]["source_timestamp_utc"] == "2026-09-10T13:30:00+00:00"


def test_early_close_and_dst_use_calendar_time_not_fixed_utc_hour():
    hist = {"timeframe": "1D", "timestamp": [_epoch("2026-11-27T09:30:00-05:00")], "equity": [100], "cashflow": {}}
    calendar = [{"date": "2026-11-27", "close": "13:00"}]
    rows = daily_close_evidence(hist, calendar, datetime(2026, 11, 27, 18, 16, tzinfo=timezone.utc))
    assert len(rows) == 1
    assert rows[0]["valuation_at_utc"] == "2026-11-27T18:00:00+00:00"
    assert daily_close_evidence(hist, calendar, datetime(2026, 11, 27, 18, 2, tzinfo=timezone.utc)) == []


def test_missing_and_offsetting_cashflows_are_never_verified_zero():
    h = _history()
    del h["cashflow"]
    now = datetime(2026, 9, 12, tzinfo=timezone.utc)
    rows = daily_close_evidence(h, _calendar(), now)
    assert all(r["cashflow_status"] == "unknown" and r["cashflow_net"] is None for r in rows)
    h["cashflow"] = {"CSD": [100, 0], "CSW": [-100, 0]}
    rows = daily_close_evidence(h, _calendar(), now)
    assert rows[0]["cashflow_status"] == "present"
    assert rows[0]["cashflow_net"] == 0


def test_malformed_arrays_or_duplicate_dates_fail_before_writes():
    h = _history()
    h["equity"] = [100]
    with pytest.raises(ValueError, match="lengths"):
        daily_close_evidence(h, _calendar(), datetime.now(timezone.utc))
    h = _history()
    h["timestamp"][1] = h["timestamp"][0]
    with pytest.raises(ValueError, match="duplicate"):
        daily_close_evidence(h, _calendar(), datetime(2026, 9, 12, tzinfo=timezone.utc))


class _Broker:
    def get_calendar(self, **kwargs):
        return _calendar()

    def get_portfolio_history(self, **kwargs):
        assert kwargs["timeframe"] == "1D"
        assert kwargs["cashflow_types"] == "ALL"
        return _history()


def test_capture_preserves_legacy_and_is_idempotent_without_static_fallback():
    j = JournalStore(":memory:")
    settings = make_settings(ALPHAOS_MODE="paper", ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s", EXECUTION_PROVIDER="simulated_internal")
    j.insert("equity_snapshots", {"snapshot_id": "legacy", "market_date": "2026-09-10", "equity": 123,
                                  "equity_source": "live_broker", "is_mock": 0})
    now = datetime(2026, 9, 12, tzinfo=timezone.utc)
    assert capture_close_equity(j, settings, now, _Broker())["written"] == 2
    assert capture_close_equity(j, settings, now, _Broker())["written"] == 0
    assert j.one("SELECT equity FROM equity_snapshots")["equity"] == 123
    rep = build_relative_performance_report(j, settings)
    assert rep["legacy_snapshot_count"] == 1
    assert rep["equity_snapshot_count"] == 2
    assert rep["legacy_equity_curve"][0]["cum_return_pct"] == 0
    assert rep["equity_curve"][-1]["cum_return_pct"] == 1
    j.close()


def test_revised_broker_value_is_visible_and_does_not_overwrite_recorded_evidence():
    j = JournalStore(":memory:")
    settings = make_settings(ALPHAOS_MODE="paper", ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s", EXECUTION_PROVIDER="simulated_internal")
    now = datetime(2026, 9, 12, tzinfo=timezone.utc)
    capture_close_equity(j, settings, now, _Broker())
    class Revised(_Broker):
        def get_portfolio_history(self, **kwargs):
            h = _history()
            h["equity"][0] = 99999
            return h
    with pytest.raises(ValueError, match="revised recorded"):
        capture_close_equity(j, settings, now, Revised())
    assert j.one("SELECT equity FROM equity_close_snapshots ORDER BY market_date")["equity"] == 100000
    j.close()
