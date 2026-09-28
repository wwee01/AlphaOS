"""Finished-session admission through real forward, HOLD10 and baseline writers."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from alphaos.baseline.tracker import resolve_pending_baseline_decisions
from alphaos.journal.journal_store import JournalStore
from alphaos.learning.outcomes_engine import REPLAY_METHOD_VERSION
from alphaos.learning.outcomes_tracker import update_pending_outcomes
from alphaos.util import timeutils
from alphaos.util.market_calendar import nth_trading_day_after

UTC = timezone.utc
ET = ZoneInfo('America/New_York')
DECISION = date(2026, 9, 14)


class Bars:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get_daily_bars(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        return self.rows


def bars(n):
    return [{'date': nth_trading_day_after(DECISION, i).isoformat(),
             'open': 100., 'high': 102., 'low': 98., 'close': 101., 'volume': 1000}
            for i in range(1, n + 1)]


def seed_ai(j, *, horizon=5, forward_complete=False):
    j.insert('candidate_outcomes', {
        'outcome_id': 'ai', 'candidate_id': 'candidate', 'symbol': 'AAPL',
        'candidate_type': 'candidate', 'decision_at_utc': '2026-09-14T15:00:00+00:00',
        'entry_reference_price': 100., 'stop_price': 90., 'target_price': 120.,
        'direction_hint': 'long', 'outcome_status': 'complete' if forward_complete else 'pending',
        'replay_window_days': horizon, 'replay_status': 'pending',
        'replay_method_version': REPLAY_METHOD_VERSION,
    })


def seed_baseline(j, horizon):
    j.insert('shadow_baseline_decisions', {
        'baseline_decision_id': 'baseline', 'candidate_id': 'candidate', 'symbol': 'AAPL',
        'rule_version': 'threshold_v1_hold10' if horizon == 10 else 'threshold_v1',
        'decision': 'propose', 'input_sha': 'input', 'direction': 'long',
        'entry': 100., 'stop': 90., 'target': 120., 'max_holding_days': horizon,
        'decision_at_utc': '2026-09-14T15:00:00+00:00', 'replay_status': 'pending',
    })


def clock(monkeypatch, day, hour=15, minute=59):
    instant = datetime.combine(day, datetime.min.time(), ET).replace(hour=hour, minute=minute)
    monkeypatch.setattr(timeutils, 'now_utc', lambda: instant.astimezone(UTC))


@pytest.mark.parametrize('horizon', [5, 10])
def test_ai_unfinished_last_session_waits_and_then_uses_revised_close(monkeypatch, horizon):
    with JournalStore(':memory:') as j:
        seed_ai(j, horizon=horizon, forward_complete=horizon == 10)
        source = Bars(bars(horizon))
        last = date.fromisoformat(source.rows[-1]['date'])
        clock(monkeypatch, last)
        update_pending_outcomes(j, source, limit=1)
        pending = j.one("SELECT * FROM candidate_outcomes WHERE outcome_id='ai'")
        assert pending['replay_status'] == 'pending'
        if horizon == 5:
            assert pending['outcome_status'] == 'partial'
        else:
            assert pending['outcome_status_10d'] != 'complete'
        source.rows[-1]['close'] = 103.
        clock(monkeypatch, last, 16, 0)
        update_pending_outcomes(j, source, limit=1)
        complete = j.one("SELECT * FROM candidate_outcomes WHERE outcome_id='ai'")
        assert complete['replay_status'] == 'complete'
        assert complete['replay_r'] == .3
        if horizon == 10:
            assert complete['outcome_status_10d'] == 'complete'
            assert complete['forward_10d_r'] == .3


@pytest.mark.parametrize('horizon', [3, 10])
def test_baseline_unfinished_last_session_waits_and_revised_close_is_used(monkeypatch, horizon):
    with JournalStore(':memory:') as j:
        seed_baseline(j, horizon)
        source = Bars(bars(horizon))
        last = date.fromisoformat(source.rows[-1]['date'])
        clock(monkeypatch, last)
        resolve_pending_baseline_decisions(j, source)
        assert j.one('SELECT * FROM shadow_baseline_decisions')['replay_status'] == 'pending'
        source.rows[-1]['close'] = 103.
        clock(monkeypatch, last, 16, 0)
        resolve_pending_baseline_decisions(j, source)
        complete = j.one('SELECT * FROM shadow_baseline_decisions')
        assert complete['replay_status'] == 'complete'
        assert complete['replay_r'] == .3


@pytest.mark.parametrize('barrier,expected', [('high', 'target_hit'), ('low', 'stop_hit')])
def test_day_six_barrier_waits_for_close_then_resolves_ten_day_replay(monkeypatch, barrier, expected):
    with JournalStore(':memory:') as j:
        seed_ai(j, horizon=10, forward_complete=True)
        source = Bars(bars(6))
        source.rows[-1][barrier] = 121. if barrier == 'high' else 89.
        last = date.fromisoformat(source.rows[-1]['date'])
        clock(monkeypatch, last)
        update_pending_outcomes(j, source, limit=1)
        assert j.one('SELECT * FROM candidate_outcomes')['replay_status'] == 'pending'
        clock(monkeypatch, last, 16, 0)
        update_pending_outcomes(j, source, limit=1)
        row = j.one('SELECT * FROM candidate_outcomes')
        assert row['replay_result'] == expected
        assert row['replay_status'] == 'complete'
        assert row['outcome_status_10d'] == 'partial'
        assert row['replay_method_version'] == REPLAY_METHOD_VERSION


def test_missing_intermediate_session_cannot_shift_later_bar_into_maturity(monkeypatch):
    with JournalStore(':memory:') as j:
        seed_ai(j)
        source = Bars(bars(6))
        omitted = source.rows.pop(2)
        clock(monkeypatch, date.fromisoformat(source.rows[-1]['date']), 16, 0)
        result = update_pending_outcomes(j, source, limit=1)
        row = j.one('SELECT * FROM candidate_outcomes')
        assert row['outcome_status'] == 'partial'
        assert row['replay_bars_used'] == 2
        assert result['session_admission']['exclusion_reasons']['after_missing_required_session'] == 3
        source.rows.insert(2, omitted)
        update_pending_outcomes(j, source, limit=1)
        assert j.one('SELECT * FROM candidate_outcomes')['outcome_status'] == 'complete'


def test_completed_legacy_rows_remain_byte_for_byte_unchanged(monkeypatch):
    with JournalStore(':memory:') as j:
        seed_ai(j, forward_complete=True)
        j.conn.execute("UPDATE candidate_outcomes SET replay_status='complete', replay_method_version=NULL, replay_result='neither', replay_r=.1,outcome_status_10d='complete'")
        j.conn.commit()
        old = j.one('SELECT * FROM candidate_outcomes')
        source = Bars(bars(10))
        source.rows[0]['high'] = 200.
        clock(monkeypatch, date(2026, 10, 1), 16, 0)
        update_pending_outcomes(j, source, limit=1)
        assert j.one('SELECT * FROM candidate_outcomes') == old
        assert source.calls == []


def admit(rows, decision, requested, received=None):
    from alphaos.learning.session_admission import admit_completed_forward_bars
    return admit_completed_forward_bars(rows, decision,
                                        requested_at=requested, received_at=received or requested)


def bar(day):
    return {'date': day, 'open': 100., 'high': 102., 'low': 98., 'close': 101.}


@pytest.mark.parametrize('zone,hour,expected', [
    (ET, 15, 0), (UTC, 19, 0), (ZoneInfo('Asia/Singapore'), 3, 0),
    (ET, 16, 1), (UTC, 20, 1), (ZoneInfo('Asia/Singapore'), 4, 1),
])
def test_aware_equivalent_timezones_and_equal_close(zone, hour, expected):
    day = 22 if getattr(zone, 'key', None) == 'Asia/Singapore' else 21
    # timezone.utc has no .key; the date conversion is explicit below.
    instant = datetime(2026, 9, day, hour, tzinfo=zone)
    accepted, _ = admit([bar('2026-09-21')], '2026-09-18', instant)
    assert len(accepted) == expected


def test_request_before_close_receiving_after_close_is_not_complete():
    accepted, receipt = admit([bar('2026-09-21')], '2026-09-18',
                             datetime(2026, 9, 21, 19, 59, tzinfo=UTC),
                             datetime(2026, 9, 21, 20, 1, tzinfo=UTC))
    assert accepted == []
    assert receipt['cutoff_basis'] == 'conservative_request_start'
    assert not receipt['per_row_source_finality_certified']


@pytest.mark.parametrize('instant', [datetime(2026, 11, 27, 13, 1, tzinfo=ET),
                                    datetime(2026, 11, 27, 15, 59, tzinfo=ET)])
def test_known_early_close_still_waits_conservatively_until_sixteen(instant):
    accepted, _ = admit([bar('2026-11-27')], '2026-11-25', instant)
    assert accepted == []


def test_holiday_weekend_future_duplicate_and_bad_dates_cannot_count():
    rows = [bar(d) for d in ['2026-09-05', '2026-09-07', '2026-09-08',
                            '2026-09-08', '2026-09-09', 'bad-date', '2026-09-10']]
    accepted, receipt = admit(rows, '2026-09-04', datetime(2026, 9, 9, 21, tzinfo=UTC))
    assert accepted == []
    assert receipt['first_missing_session'] == '2026-09-08'
    assert receipt['exclusion_reasons'] == {'nontrading_session': 2, 'invalid_date': 1,
            'unfinished_or_future_session': 1, 'duplicate_session': 2, 'after_missing_required_session': 1}


def test_holiday_is_not_a_required_missing_session():
    accepted, _ = admit([bar('2026-09-08')], '2026-09-04', datetime(2026, 9, 8, 20, tzinfo=UTC))
    assert [b['date'] for b in accepted] == ['2026-09-08']


def test_naive_or_reversed_observation_times_are_refused():
    with pytest.raises(ValueError, match='aware'):
        admit([bar('2026-09-21')], '2026-09-18', datetime(2026, 9, 21, 20))
    with pytest.raises(ValueError, match='precedes'):
        admit([bar('2026-09-21')], '2026-09-18', datetime(2026, 9, 21, 20, tzinfo=UTC),
              datetime(2026, 9, 21, 19, tzinfo=UTC))


def test_readonly_report_does_not_credit_arithmetic_stamp_as_finality():
    from alphaos.reports.baseline_report import BASELINE_CAVEAT, build_baseline_report
    with JournalStore(':memory:') as j:
        report = build_baseline_report(j, None)
    assert report['validity']['session_finality_status'] == 'unverified_without_independent_per_row_receipts'
    assert report['validity']['arithmetic_method_is_source_finality_proof'] is False
    assert 'not retrospectively certify' in BASELINE_CAVEAT
