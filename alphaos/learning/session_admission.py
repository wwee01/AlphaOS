"""Prospective daily-session admission, separate from bracket arithmetic.

The provider supplies only day-labelled OHLCV, not a final-source receipt. A
request begun before 16:00 ET cannot certify that day's complete session even
if its response arrives after 16:00. Conservatively use that request-start
cutoff, including on early-close days. This guard does not certify historical
rows or repair cached bars; it has no persistence or network side effects.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from alphaos.util.market_calendar import is_trading_day, nth_trading_day_after

SESSION_ADMISSION_VERSION = 'daily_session_admission_v1'
_ET = ZoneInfo('America/New_York')


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError('daily session admission requires an aware observation time')
    return value.astimezone(timezone.utc)


def admit_completed_forward_bars(
    bars: list[dict], decision_date: str, *, requested_at: datetime, received_at: datetime,
) -> tuple[list[dict], dict]:
    """Admit the contiguous completed trading-session prefix after decision.

    Missing/duplicate/invalid intermediate sessions cannot shift a later bar
    into an earlier holding-day slot. Earlier barrier evidence is retained in
    that prefix; later barrier evidence waits until any earlier gap is filled.
    """
    cutoff, received = _aware(requested_at), _aware(received_at)
    if received < cutoff:
        raise ValueError('daily session response precedes request')
    decision = date.fromisoformat(decision_date)
    grouped: dict[date, list[dict]] = {}
    exclusions: Counter[str] = Counter()
    for bar in bars:
        value = bar.get('date')
        try:
            day = date.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            day = None
        if day is None or day.isoformat() != value:
            exclusions['invalid_date'] += 1
        elif day <= decision:
            exclusions['on_or_before_decision'] += 1
        elif not is_trading_day(day):
            exclusions['nontrading_session'] += 1
        elif datetime.combine(day, time(16), _ET).astimezone(timezone.utc) > cutoff:
            exclusions['unfinished_or_future_session'] += 1
        else:
            grouped.setdefault(day, []).append(bar)
    valid = {}
    for day, group in grouped.items():
        if len(group) == 1:
            valid[day] = group[0]
        else:
            exclusions['duplicate_session'] += len(group)
    accepted = []
    expected = nth_trading_day_after(decision, 1)
    missing = None
    for day in sorted(valid):
        if day != expected:
            missing = expected.isoformat()
            break
        accepted.append(valid[day])
        expected = nth_trading_day_after(day, 1)
    if missing:
        exclusions['after_missing_required_session'] += len(valid) - len(accepted)
    waiting = any(count for reason, count in exclusions.items() if reason != 'on_or_before_decision')
    receipt = {
        'method_version': SESSION_ADMISSION_VERSION,
        'requested_at_utc': cutoff.isoformat(), 'received_at_utc': received.isoformat(),
        'admission_cutoff_utc': cutoff.isoformat(), 'cutoff_basis': 'conservative_request_start',
        'input_bars': len(bars), 'admitted_bars': len(accepted),
        'excluded_bars': sum(exclusions.values()), 'exclusion_reasons': dict(exclusions),
        'first_missing_session': missing, 'waiting_for_admissible_sessions': waiting,
        'per_row_source_finality_certified': False,
    }
    return accepted, receipt


def record_admission_diagnostic(counts: dict, receipt: dict) -> None:
    """Bounded job-summary counters, never a per-row finality certification."""
    summary = counts.setdefault('session_admission', {
        'method_version': SESSION_ADMISSION_VERSION, 'fetches': 0,
        'input_bars': 0, 'admitted_bars': 0, 'excluded_bars': 0,
        'exclusion_reasons': {}, 'rows_waiting_for_admission': 0,
        'first_request_at_utc': receipt['requested_at_utc'],
        'last_received_at_utc': receipt['received_at_utc'],
        'cutoff_basis': 'conservative_request_start',
        'per_row_source_finality_certified': False,
    })
    summary['fetches'] += 1
    for key in ('input_bars', 'admitted_bars', 'excluded_bars'):
        summary[key] += receipt[key]
    summary['rows_waiting_for_admission'] += int(receipt['waiting_for_admissible_sessions'])
    summary['first_request_at_utc'] = min(summary['first_request_at_utc'], receipt['requested_at_utc'])
    summary['last_received_at_utc'] = max(summary['last_received_at_utc'], receipt['received_at_utc'])
    for reason, count in receipt['exclusion_reasons'].items():
        summary['exclusion_reasons'][reason] = summary['exclusion_reasons'].get(reason, 0) + count
