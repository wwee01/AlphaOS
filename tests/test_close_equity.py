"""Broker-shaped provisional valuation evidence; no network or credentials."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from alphaos.journal.journal_store import JournalStore
from alphaos.reports.close_equity import TIMING_STATUS, VALUATION_METHOD, capture_close_equity, daily_close_evidence
from conftest import make_settings


def _epoch(value):
    return datetime.fromisoformat(value).timestamp()


def _calendar():
    return [{"date": "2026-09-10", "open": "09:30", "close": "16:00"},
            {"date": "2026-09-11", "open": "09:30", "close": "2026-09-11T16:00:00"}]


def _history(calendar=None, values=None, offset="-04:00"):
    calendar = calendar or _calendar()
    values = values or [100000, 101000]
    stamps, equities = [], []
    for day, value in zip(calendar, values):
        start = datetime.fromisoformat(day['date'] + 'T' + day['open'] + offset)
        end = datetime.fromisoformat(day['date'] + 'T' + day['close'].split('T')[-1] + offset)
        for i in range(int((end - start).total_seconds() // 60) + 1):
            stamps.append((start + timedelta(minutes=i)).timestamp())
            equities.append(value)
    return {"timeframe": "1Min", "timestamp": stamps, "equity": equities, "cashflow": {}}


def test_exact_documented_closing_window_remains_provisional():
    h = _history()
    # Distinguish 15:58, 15:59 and 16:00; none can silently substitute another.
    h['equity'][388:391] = [99998, 100000, 100002]
    rows = daily_close_evidence(h, _calendar(), datetime(2026, 9, 11, 14, tzinfo=timezone.utc))
    assert [r["market_date"] for r in rows] == ["2026-09-10"]
    assert rows[0]["equity"] == 100000
    assert rows[0]["valuation_at_utc"] == "2026-09-10T20:00:00+00:00"
    assert rows[0]["source_timestamp_utc"] == "2026-09-10T19:59:00+00:00"
    assert rows[0]["comparison_eligible"] is False
    raw = json.loads(rows[0]['raw_evidence_json'])
    assert raw['timing_status'] == TIMING_STATUS
    assert raw['comparison_eligible'] is False
    assert raw['regular_session_minutes_complete'] is True


def test_early_close_and_dst_use_calendar_not_fixed_utc_hour():
    calendar = [{"date": "2026-11-27", "open": "09:30", "close": "13:00"}]
    hist = _history(calendar, [100], '-05:00')
    rows = daily_close_evidence(hist, calendar, datetime(2026, 11, 27, 18, 16, tzinfo=timezone.utc))
    assert rows[0]["valuation_at_utc"] == "2026-11-27T18:00:00+00:00"
    assert rows[0]["source_timestamp_utc"] == "2026-11-27T17:59:00+00:00"
    assert daily_close_evidence(hist, calendar, datetime(2026, 11, 27, 18, 2, tzinfo=timezone.utc)) == []


def test_missing_and_offsetting_external_cashflows_never_become_verified_zero():
    h = _history()
    del h["cashflow"]
    now = datetime(2026, 9, 12, tzinfo=timezone.utc)
    rows = daily_close_evidence(h, _calendar(), now)
    assert all(r["cashflow_status"] == "unknown" and r["cashflow_net"] is None for r in rows)
    csd, csw = [0] * len(h['timestamp']), [0] * len(h['timestamp'])
    csd[0], csw[100] = 100, -100
    h["cashflow"] = {"CSD": csd, "CSW": csw}
    rows = daily_close_evidence(h, _calendar(), now)
    assert rows[0]["cashflow_status"] == "present"
    assert rows[0]["cashflow_net"] == 0
    assert len(json.loads(rows[0]['raw_evidence_json'])['cashflow_events']) == 2


def test_internal_fees_and_unknown_activity_types_are_distinguished():
    h = _history()
    fees = [0] * len(h['timestamp'])
    fees[0] = -.01
    h['cashflow'] = {'FEE': fees}
    now = datetime(2026, 9, 12, tzinfo=timezone.utc)
    row = daily_close_evidence(h, _calendar(), now)[0]
    assert row['cashflow_status'] == 'internal_only'
    assert row['cashflow_net'] == 0
    assert row['equity'] == 100000  # never add a fee back to broker equity
    assert row['comparison_eligible'] is False
    h['cashflow']['UNFAMILIAR_ACTIVITY'] = fees
    row = daily_close_evidence(h, _calendar(), now)[0]
    assert row['cashflow_status'] == 'unknown'
    assert row['cashflow_net'] is None
    assert 'UNFAMILIAR_ACTIVITY' in json.loads(row['raw_evidence_json'])['unknown_nonzero_activity_types']


@pytest.mark.parametrize('missing_index', [0, 100, 389])
def test_partial_session_or_missing_closing_minute_has_no_fallback(missing_index):
    h = _history()
    del h['timestamp'][missing_index]
    del h['equity'][missing_index]
    rows = daily_close_evidence(h, _calendar(), datetime(2026, 9, 12, tzinfo=timezone.utc))
    assert [r['market_date'] for r in rows] == ['2026-09-11']


def test_malformed_arrays_duplicate_timestamps_or_daily_values_fail_before_writes():
    now = datetime(2026, 9, 12, tzinfo=timezone.utc)
    h = _history()
    h['equity'] = [100]
    with pytest.raises(ValueError, match='lengths'):
        daily_close_evidence(h, _calendar(), now)
    h = _history()
    h['timestamp'][1] = h['timestamp'][0]
    with pytest.raises(ValueError, match='duplicate'):
        daily_close_evidence(h, _calendar(), now)
    h = {'timeframe': '1D', 'timestamp': [_epoch('2026-09-11T00:00:00+00:00')], 'equity': [95474.08]}
    with pytest.raises(ValueError, match='timeframe=1Min'):
        daily_close_evidence(h, _calendar(), now)


class _Broker:
    def get_calendar(self, **kwargs):
        return _calendar()

    def get_portfolio_history(self, **kwargs):
        assert kwargs['period'] == '7D'
        assert kwargs['timeframe'] == '1Min'
        assert kwargs['intraday_reporting'] == 'market_hours'
        assert kwargs['cashflow_types'] == 'ALL'
        return _history()


def test_capture_preserves_legacy_and_is_idempotent_with_zero_eligible_observations():
    j = JournalStore(':memory:')
    settings = make_settings(ALPHAOS_MODE='paper', ALPACA_API_KEY='k', ALPACA_SECRET_KEY='s', EXECUTION_PROVIDER='simulated_internal')
    j.insert('equity_snapshots', {'snapshot_id': 'legacy', 'market_date': '2026-09-10', 'equity': 123,
                                'equity_source': 'live_broker', 'is_mock': 0})
    now = datetime(2026, 9, 12, tzinfo=timezone.utc)
    result = capture_close_equity(j, settings, now, _Broker())
    assert result['written'] == 2 and result['status'] == 'provisional'
    assert result['eligible_observations'] == 0
    assert result['valuation_method'] == VALUATION_METHOD
    assert capture_close_equity(j, settings, now, _Broker())['written'] == 0
    assert j.one('SELECT equity FROM equity_snapshots')['equity'] == 123
    rows = j.query('SELECT * FROM equity_close_snapshots')
    assert len(rows) == 2
    assert all(json.loads(r['raw_evidence_json'])['comparison_eligible'] is False for r in rows)
    j.close()


def test_revised_evidence_is_visible_without_overwriting_original():
    j = JournalStore(':memory:')
    settings = make_settings(ALPHAOS_MODE='paper', ALPACA_API_KEY='k', ALPACA_SECRET_KEY='s', EXECUTION_PROVIDER='simulated_internal')
    now = datetime(2026, 9, 12, tzinfo=timezone.utc)
    capture_close_equity(j, settings, now, _Broker())
    class Revised(_Broker):
        def get_portfolio_history(self, **kwargs):
            h = _history()
            h['equity'][389] = 99999
            return h
    with pytest.raises(ValueError, match='revised recorded'):
        capture_close_equity(j, settings, now, Revised())
    assert j.one('SELECT equity FROM equity_close_snapshots ORDER BY market_date')['equity'] == 100000
    j.close()


def test_real_shaped_closing_window_is_not_relabeled_to_match_daily_eod():
    # Sanitized observation values from the Sep12 read-only broker capture.
    cal = [{'date': '2026-09-11', 'open': '09:30', 'close': '16:00'}]
    h = _history(cal, [95629.17])
    h['equity'][-1] = 95617.38
    row = daily_close_evidence(h, cal, datetime(2026, 9, 12, tzinfo=timezone.utc))[0]
    assert row['equity'] == 95629.17
    assert row['equity'] != 95622.51  # separately observed daily EOD/account mark
    assert row['comparison_eligible'] is False


def test_deposit_on_missing_earlier_equity_is_retained_for_closing_window():
    h = _history()
    h['equity'][100] = None
    deposits = [0] * len(h['timestamp'])
    deposits[100] = 100
    h['cashflow'] = {'CSD': deposits}
    row = daily_close_evidence(h, _calendar(), datetime(2026, 9, 12, tzinfo=timezone.utc))[0]
    assert row['cashflow_status'] == 'present'
    assert row['cashflow_net'] == 100
    assert row['comparison_eligible'] is False


@pytest.mark.parametrize('value', [None, 0, float('nan'), float('inf')])
def test_invalid_closing_value_is_not_replaced_by_nearby_observation(value):
    h = _history()
    h['equity'][389] = value
    rows = daily_close_evidence(h, _calendar(), datetime(2026, 9, 12, tzinfo=timezone.utc))
    assert [r['market_date'] for r in rows] == ['2026-09-11']
