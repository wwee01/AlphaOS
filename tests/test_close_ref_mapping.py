"""Approved mapping examples and synthetic source rows; no DB or provider work."""
import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from alphaos.learning import close_ref_engine as e
from alphaos.learning import close_ref_mapping as m
from alphaos.util.market_calendar import is_trading_day

COMMIT = 'a' * 40
ET = ZoneInfo('America/New_York')


def utc(text):
    return datetime.fromisoformat(text).replace(tzinfo=ET).astimezone(timezone.utc).isoformat()


def batch(index=1, day='2026-11-18', at='15:59:58', **extra):
    return dict(id=index, scan_batch_id=f'scan_{index}', status='completed',
                completed_at_utc=utc(f'{day}T16:06:07'),
                explore_routed_at_utc=utc(f'{day}T{at}'), **extra)


def candidate(index=1, scan=1, arm='A', symbol='ABC', **extra):
    return dict(id=index, candidate_id=f'cand_{index}', scan_batch_id=f'scan_{scan}',
                symbol=symbol, shadow_tier=0, direction='long', policy_version='explore_1', arm=arm, **extra)


def opinion(index=1, cand=1, day='2026-11-18', at='15:59:30', **extra):
    return dict(id=index, eval_id=f'eval_{index}', candidate_id=f'cand_{cand}', symbol='ABC',
                created_at_utc=utc(f'{day}T{at}'), direction='long', decision='watch',
                confidence=.6, is_mock=0, validation_status='passed', **extra)


def route(b=None, c=None, evaluations=(), proposals=(), source_commit=None):
    return m.routing_records(b or batch(), [c or candidate()], evaluations, proposals,
                             source_commit=source_commit)[0]


def dates(record):
    w = record.window if isinstance(record, m.Decision) else record
    return w.reference.date, w.endpoint_5.date, w.endpoint_10.date


@pytest.mark.parametrize('at,expected', [
    ('2026-11-18T16:01:00', ('2026-11-19','2026-11-27','2026-12-04')),
    ('2026-11-18T16:02:00', ('2026-11-19','2026-11-27','2026-12-04')),
    ('2026-11-18T15:59:30', ('2026-11-18','2026-11-25','2026-12-03')),
    ('2026-11-18T16:00:00', ('2026-11-19','2026-11-27','2026-12-04')),
    ('2026-11-18T15:59:59.999999', ('2026-11-18','2026-11-25','2026-12-03')),
    ('2026-11-18T15:59:58', ('2026-11-18','2026-11-25','2026-12-03')),
    ('2026-11-18T16:03:00', ('2026-11-19','2026-11-27','2026-12-04')),
    ('2026-11-27T12:59:59', ('2026-11-27','2026-12-04','2026-12-11')),
    ('2026-11-27T13:00:00', ('2026-11-30','2026-12-07','2026-12-14')),
    ('2026-11-27T15:58:00', ('2026-11-30','2026-12-07','2026-12-14')),
    ('2026-12-24T11:40:00', ('2026-12-24','2027-01-04','2027-01-11')),
    ('2026-12-24T15:50:00', ('2026-12-28','2027-01-05','2027-01-12')),
    ('2026-11-26T10:00:00', ('2026-11-27','2026-12-04','2026-12-11')),
    ('2026-11-21T11:00:00', ('2026-11-23','2026-12-01','2026-12-08')),
])
def test_all_exact_section6_examples(at, expected):
    assert dates(e.resolve_window(datetime.fromisoformat(utc(at)))) == expected


def test_calendar_bytes_dates_only_approved_early_closes_dst_and_bounds():
    calendar = e.pinned_calendar()
    raw = Path(e.__file__).with_name('close_ref_calendar.json').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == calendar.sha256 == e.PINNED_CALENDAR_SHA256
    expected = []
    day = date(2026,11,16)
    while day <= date(2027,6,30):
        if is_trading_day(day):
            expected.append(day.isoformat())
        day += timedelta(days=1)
    assert [s.date for s in calendar.sessions] == expected
    for s in calendar.sessions:
        hour = 13 if s.date in ('2026-11-27','2026-12-24') else 16
        assert s.close.astimezone(ET).time() == time(hour)
    rows = {s.date:s for s in calendar.sessions}
    assert rows['2027-03-12'].close.hour == 21
    assert rows['2027-03-15'].close.hour == 20
    with pytest.raises(e.CalendarPending):
        e.resolve_window(datetime.fromisoformat(utc('2027-07-01T09:35:00')))
    with pytest.raises(e.CalendarPending):
        e.resolve_window(datetime.fromisoformat(utc('2027-06-30T09:35:00')))


@pytest.mark.parametrize('scan,day,lo,hi,pool,arms', [
    ('scan_f2835c8b1a31','2026-11-16','09:44:20.247','09:44:21.895',1,['A','C']),
    ('scan_9c70b2f60385','2026-11-16','11:40:31.637','11:40:33.905',2,['A','C']),
    ('scan_5d35d2c9cc0e','2026-11-16','15:58:22.152','15:58:24.450',1,['C']),
    ('scan_6b9bcc405cb8','2026-11-17','09:41:13.789','09:41:13.918',0,[]),
    ('scan_083cfd2eb2f5','2026-11-17','11:37:57.272','11:37:57.662',0,[]),
    ('scan_3342d3946466','2026-11-17','15:50:03.929','15:50:04.156',0,[]),
])
def test_six_section4_intervals(scan, day, lo, hi, pool, arms):
    b = batch(day=day)
    b.update(scan_batch_id=scan, explore_routed_at_utc=None, completed_at_utc=utc(day+'T'+hi))
    cs = [dict(candidate(i+1,arm=arm,symbol=f'S{i}'),scan_batch_id=scan) for i,arm in enumerate(arms)]
    pool_candidates = [c for c in cs if c['arm']=='C']
    while len(pool_candidates) < pool:
        c = dict(candidate(len(cs)+1,arm=None,symbol=f'S{len(cs)}'),scan_batch_id=scan)
        cs.append(c)
        pool_candidates.append(c)
    if not cs:
        cs.append(dict(candidate(1,arm=None),scan_batch_id=scan))
    b['explore_draw_json'] = json.dumps(dict(scan_batch_id=scan,window_key='window',
        eligible_ids=[c['candidate_id'] for c in pool_candidates],
        chosen_id=next((c['candidate_id'] for c in pool_candidates if c['arm']=='C'),None)))
    evs = [dict(opinion(i+1,cand=i+1,day=day,at=lo),symbol=c['symbol']) for i,c in enumerate(cs)]
    records = m.routing_records(b,cs,evs,[],source_commit=COMMIT)
    assert len([r for r in records if r.population=='pool']) == pool
    assert [r.arm for r in records if r.population=='arm'] == arms
    assert all(r.status=='ready' and r.basis=='bounded_routing_v1' and
               r.decision_at_utc is None and r.window.decision_at_utc is None and
               r.window.reference.date==day for r in records)
    assert m._routing_bounds(b,cs,evs,[],COMMIT) == (utc(day+'T'+lo),utc(day+'T'+hi),None)


@pytest.mark.parametrize('day,lo,hi,status,reason', [
    ('2026-11-16','15:58:22.152','15:58:24.450','ready',None),
    ('2026-11-18','15:59:40','16:06:07','unavailable','session_unproven'),
    ('2026-11-16','09:29:50','09:30:10','unavailable','boundary_unproven'),
    ('2026-11-16','09:29:40','09:29:59','out_of_scope','before_campaign'),
    ('2026-11-18','15:59:50','15:59:40','unavailable','session_unproven'),
])
def test_bounded_section6_and_refusal_cases(day,lo,hi,status,reason):
    b = batch(day=day)
    b.update(explore_routed_at_utc=None, completed_at_utc=utc(day+'T'+hi))
    record = route(b,evaluations=[opinion(day=day,at=lo)],source_commit=COMMIT)
    assert (record.status,record.reason)==(status,reason)
    if status=='ready':
        assert dates(record)==('2026-11-16','2026-11-23','2026-12-01')


def test_preboundary_exact_and_no_outcome_or_processing_time_fallback():
    assert route(batch(day='2026-11-16',at='09:29:59')).status=='out_of_scope'
    b = batch()
    b['explore_routed_at_utc']=None
    b['decision_at_utc']=utc('2026-11-18T15:00:00')
    b['created_at_utc']=b['completed_at_utc']
    assert route(b).reason=='ordering_source_unproven'
    assert route(b,source_commit=COMMIT).reason=='timestamp_unavailable'


def test_exact_inside_bounds_required_and_only_same_scan_sources_used():
    b = batch(at='15:59:58')
    c = candidate()
    other = candidate(2,scan=2)
    evs = [opinion(),opinion(2,2,at='16:05:00')]
    props = [dict(candidate_id='cand_1',scan_batch_id='scan_1',policy_version='legacy',created_at_utc=utc('2026-11-18T15:00:00')),
             dict(candidate_id='cand_2',scan_batch_id='scan_2',policy_version='explore_1',created_at_utc=utc('2026-11-18T15:00:00'))]
    record = m.routing_records(b,[c,other],evs,props,source_commit=COMMIT)[0]
    assert record.status=='ready'
    props.append(dict(candidate_id='cand_1',scan_batch_id='scan_1',policy_version='explore_1',created_at_utc=utc('2026-11-18T15:59:50')))
    assert m.routing_records(b,[c,other],evs,props,source_commit=COMMIT)[0].reason=='identity_mismatch'


@pytest.mark.parametrize('stamp', ['2026-11-18T15:59:00','nonsense','',7])
def test_naive_invalid_timestamp_never_uses_interval(stamp):
    b=batch()
    b['explore_routed_at_utc']=stamp
    assert route(b,evaluations=[opinion()],source_commit=COMMIT).reason=='timestamp_unavailable'


def test_pool_owners_and_c_identity_a_plus_b_expansion():
    b=batch()
    cs=[candidate(1,arm='A+B'),candidate(2,arm='C',symbol='XYZ'),candidate(3,arm=None,symbol='DEF')]
    b['explore_draw_json']=dict(scan_batch_id='scan_1',window_key='w',eligible_ids=['cand_2','cand_3'],chosen_id='cand_2')
    rows=m.routing_records(b,cs,[],[])
    assert [(r.population,r.arm,r.candidate_id) for r in rows]==[
        ('arm','A','cand_1'),('arm','B','cand_1'),('arm','C','cand_2'),('pool','','cand_2'),('pool','','cand_3')]
    assert all(r.status=='ready' for r in rows)
    b['explore_draw_json']['scan_batch_id']='other'
    rows=m.routing_records(b,cs,[],[])
    assert len(rows)==3 and rows[2].reason=='identity_mismatch'
    assert rows[0].status==rows[1].status=='ready'
    b['explore_draw_json'].update(scan_batch_id='scan_1',window_key=None)
    assert len(m.routing_records(b,cs,[],[]))==3


def test_malformed_pool_references_remain_counted_unavailable():
    b=batch(explore_draw_json=dict(scan_batch_id='scan_1',window_key='w',
        eligible_ids=['cand_1','cand_1','missing'],chosen_id='missing'))
    rows=m.routing_records(b,[candidate()],[],[])
    assert len(rows)==3
    assert all(r.reason=='identity_mismatch' for r in rows if r.population=='pool')


def test_opinion_vs_route_straddles_close_and_earliest_original_only():
    b=batch(at='16:02:00')
    c=candidate()
    evs=[opinion(2,at='16:01:00'),opinion(1,at='15:59:30')]
    record=m.evaluator_records([c],[b],evs,non_degraded_scan_ids=['scan_1'])[0]
    assert record.source_id=='eval_1' and record.ranking_eligible
    assert record.window.reference.date=='2026-11-18'
    assert route(b,c).window.reference.date=='2026-11-19'
    evs[1]['is_mock']=1
    record=m.evaluator_records([c],[b],evs,non_degraded_scan_ids=['scan_1'])[0]
    assert record.source_id=='eval_1' and not record.ranking_eligible and record.status=='ready'


@pytest.mark.parametrize('field,value,reason', [
    ('direction','short','not_long'),('is_mock',1,'evaluation_not_nonmock'),
    ('validation_status','live_exception','evaluation_not_passed'),
    ('decision','other','invalid_opinion'),('confidence',float('nan'),'nonfinite_confidence'),
    ('confidence',True,'nonfinite_confidence'),
])
def test_ranking_exclusions_preserve_measurement(field,value,reason):
    ev=opinion()
    ev[field]=value
    record=m.evaluator_records([candidate()],[batch()],[ev],non_degraded_scan_ids=['scan_1'])[0]
    assert record.status=='ready' and not record.ranking_eligible and record.ranking_exclusion_reason==reason


def test_nonqualifying_original_cannot_suppress_later_qualifying_symbol():
    cs=[candidate(1),candidate(2,scan=2)]
    bs=[batch(),batch(2)]
    evs=[dict(opinion(),is_mock=1),opinion(2,2,at='15:59:40')]
    records=m.evaluator_records(cs,bs,evs,non_degraded_scan_ids=['scan_1','scan_2'])
    final=m.deduplicate(records)
    assert [r.status for r in final]==['ready','ready']
    assert [r.ranking_eligible for r in final]==[False,True]
    assert not m.evaluator_records([candidate()],[batch()],[opinion()])[0].ranking_eligible


def test_earliest_opinion_id_tie_and_bad_timestamp_cannot_be_skipped():
    evs=[opinion(2),opinion(1)]
    assert m.evaluator_records([candidate()],[batch()],evs)[0].source_id=='eval_1'
    evs[1]['created_at_utc']='naive'
    assert m.evaluator_records([candidate()],[batch()],evs)[0].reason=='timestamp_unavailable'


def test_original_shadow_only_copy_time_and_identity_checks():
    c=candidate()
    c.update(shadow_tier=1,label_frozen_at_utc=utc('2026-11-18T15:59:30'))
    original=dict(id=1,label_id='label_1',candidate_id='cand_1',scan_batch_id='scan_1',symbol='ABC',
        shadow_tier=1,relabel_of=None,label_frozen_at_utc=c['label_frozen_at_utc'])
    later=dict(original,id=2,label_id='label_2',relabel_of='label_1',label_frozen_at_utc=utc('2026-11-18T16:01:00'))
    row=m.shadow_records([c],[batch()],[later,original])[0]
    assert row.source_id=='label_1' and row.window.reference.date=='2026-11-18'
    c['label_frozen_at_utc']=later['label_frozen_at_utc']
    assert m.shadow_records([c],[batch()],[later,original])[0].reason=='identity_mismatch'
    assert m.shadow_records([c],[batch()],[later])==[]


def bounded(index,lo,hi):
    b=batch(index,at=lo)
    b.update(explore_routed_at_utc=None,completed_at_utc=utc('2026-11-18T'+hi))
    return route(b,candidate(index,scan=index),[opinion(index,index,at=lo)],source_commit=COMMIT)


def test_dedupe_d_key_exact_ties_same_scan_a_plus_b_and_cross_population():
    first=route(batch(2,day='2026-11-18',at='16:02:00'),candidate(2,scan=2))
    later=route(batch(1,day='2026-11-19',at='09:40:00'),candidate(1))
    out=m.deduplicate([later,first])
    assert [r.status for r in out]==['duplicate_in_session','ready']
    tied=replace(first,candidate_id='cand_3',candidate_row_id=3,scan_row_id=1,scan_batch_id='scan_1',source_id='scan_1')
    out=m.deduplicate([first,tied])
    assert out[1].status=='ready'
    assert m.deduplicate([first,replace(first,population='pool',arm='')])==[first,replace(first,population='pool',arm='')]
    rows=m.routing_records(batch(),[candidate(arm='A+B'),candidate(2,arm='A')],[],[])
    out=m.deduplicate(rows)
    assert [(r.arm,r.status) for r in out]==[('A','ready'),('B','ready'),('A','duplicate_in_session')]


def test_disjoint_bounds_order_and_overlap_whole_key_including_old_frozen():
    first=bounded(1,'10:00:00','10:00:02')
    second=bounded(2,'10:00:03','10:00:04')
    out=m.deduplicate([second,first])
    assert out[0].status=='duplicate_in_session' and out[1].status=='ready'
    overlap=bounded(3,'10:00:01','10:00:05')
    out=m.deduplicate([*out,overlap])
    assert all(r.reason=='order_unproven' and r.status=='unavailable' for r in out)
    assert all(r.status=='unavailable' for r in m.deduplicate([*out,first]))
    assert m.deduplicate([first,replace(first,status='unavailable',reason='identity_mismatch')])[0].status=='unavailable'


def test_exact_inside_other_interval_is_order_unproven_but_exact_equal_ties_known():
    first=bounded(1,'10:00:00','10:00:02')
    exact=route(batch(2,at='10:00:01'),candidate(2,scan=2))
    assert all(r.reason=='order_unproven' for r in m.deduplicate([first,exact]))


def test_compact_window_reload_checks_entire_pin_endpoints_and_ownership():
    original=route()
    body=m.decision_to_dict(original)
    assert m.decision_from_dict(body)==original
    assert len(json.dumps(body))<2048
    body['window']['endpoint_5']['date']='2026-11-26'
    with pytest.raises(ValueError):
        m.decision_from_dict(body)
    assert original.window.endpoint_5.date=='2026-11-25'
    with pytest.raises(FrozenInstanceError):
        original.reason='changed'
    interval=bounded(1,'10:00:00','10:00:02')
    assert m.decision_from_dict(m.decision_to_dict(interval))==interval


def test_changed_input_same_identity_refuses_instead_of_later_timestamp():
    first=route()
    changed=replace(first,decision_at_utc=utc('2026-11-18T15:59:59'))
    with pytest.raises(ValueError,match='identity_changed'):
        m.deduplicate([first,changed])


def test_scope_precedes_identity_error_when_valid_event_proves_before_start():
    c=candidate()
    c.update(shadow_tier=1,label_frozen_at_utc=utc('2026-11-16T09:29:58'))
    label=dict(id=1,label_id='label_1',candidate_id='cand_1',scan_batch_id='scan_1',
        symbol='OTHER',shadow_tier=1,relabel_of=None,label_frozen_at_utc=utc('2026-11-16T09:29:59'))
    assert m.shadow_records([c],[batch()],[label])[0].status=='out_of_scope'
    record=m._record('arm','C',candidate(),batch(),'scan_batches','scan_1','bounded_routing_v1',COMMIT,
        lower=utc('2026-11-16T09:29:40'),upper=utc('2026-11-16T09:29:59'),reason='identity_mismatch')
    assert record.status=='out_of_scope'


def test_upper_bound_requires_same_batch_and_can_use_proposal_before_completion():
    b=batch()
    b.update(explore_routed_at_utc=None,completed_at_utc=None)
    wrong=dict(candidate_id='cand_1',scan_batch_id='other',policy_version='explore_1',
        created_at_utc=utc('2026-11-18T15:59:45'))
    assert route(b,evaluations=[opinion()],proposals=[wrong],source_commit=COMMIT).reason=='timestamp_unavailable'
    correct=dict(wrong,scan_batch_id='scan_1')
    assert route(b,evaluations=[opinion()],proposals=[correct],source_commit=COMMIT).status=='ready'
