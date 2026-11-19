"""Campaign writer integration: population evidence, freezing and no legacy writes."""
from datetime import datetime
from types import SimpleNamespace
import json
import sqlite3
import hashlib
import subprocess

import pytest

from alphaos.journal.journal_store import JournalStore
from alphaos.learning import close_ref_tracker as tracker
from alphaos.learning import close_ref_lineage as lineage
from alphaos.learning.close_ref_sources import CloseRefSources
from alphaos.util import timeutils
from conftest import make_settings


def dt(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


@pytest.fixture
def j(tmp_path, monkeypatch):
    monkeypatch.setattr(timeutils, 'now_utc', lambda: dt('2026-11-19T03:30:00Z'))
    monkeypatch.setattr(lineage, 'committed_ordering', lambda sha: {'verified': True, 'commit': sha})
    journal = JournalStore(str(tmp_path / 'ledger.db'))
    journal.insert('lineage_snapshots', {'lineage_id': 'lineage', 'git_commit_sha': 'a'*40, 'git_dirty': 0})
    yield journal
    journal.close()


def scan(j, scan_id='scan', symbol='XYZ', arm='A', instant='2026-11-16T15:00:00Z', *, exact=True, shadow=False):
    cid = 'cand_' + scan_id
    j.insert('scan_batches', {'scan_batch_id': scan_id, 'started_at_utc': instant,
             'completed_at_utc': instant, 'status': 'completed',
             'explore_draw_json': {'scan_batch_id': scan_id, 'window_key': 'window_'+scan_id,
                                    'eligible_ids': [cid], 'chosen_id': cid},
             'explore_routed_at_utc': instant if exact else None})
    j.insert('candidates', {'candidate_id': cid, 'symbol': symbol, 'scan_batch_id': scan_id,
             'policy_version': 'explore_1', 'arm': arm, 'direction': 'long',
             'lineage_id': 'lineage', 'shadow_tier': int(shadow),
             'label_frozen_at_utc': instant if shadow else None})
    if not shadow:
        j.insert('openai_evaluations', {'eval_id': 'eval_'+scan_id, 'candidate_id': cid,
                 'symbol': symbol, 'created_at_utc': instant, 'direction': 'long',
                 'decision': 'watch', 'confidence': .5, 'validation_status': 'passed',
                 'is_mock': 0, 'lineage_id': 'lineage'})
    else:
        j.insert('candidate_labels', {'label_id': 'label_'+scan_id, 'candidate_id': cid,
                 'symbol': symbol, 'scan_batch_id': scan_id, 'shadow_tier': 1,
                 'label_frozen_at_utc': instant, 'relabel_of': None})
    return cid


def enroll(j, **kw):
    return tracker.enroll(j, provider='alpaca', feed='iex', **kw)


class Bars:
    def __init__(self):
        self.missing = set()
        self.calls = []
        self.ratio = 1.1
        self.settings = SimpleNamespace(market_data_feed='iex')

    def get_daily_bars(self, symbol, *, start, end, limit, adjustment):
        assert adjustment == 'split'
        self.calls.append(symbol)
        if symbol in self.missing:
            return []
        return [{'date': start, 'close': 100.}, {'date': '2026-11-23', 'close': 100*self.ratio},
                {'date': '2026-12-01', 'close': 100*self.ratio}]


def sources(bars, now='2026-11-23T21:00:02Z', **kw):
    return CloseRefSources(None, bars, provider='alpaca', feed='iex', now=lambda: dt(now), **kw)


def test_actual_enrollment_all_populations_and_legacy_unchanged(j):
    cid = scan(j, arm='A+B')
    scan(j, 'shadow', symbol='SHD', arm=None, shadow=True)
    j.insert('candidate_outcomes', {'outcome_id': 'legacy', 'candidate_id': cid, 'symbol': 'XYZ',
             'candidate_type': 'watch', 'decision_at_utc': '2026-11-16T01:00:00Z',
             'market_adjusted_return_5d_pct': 99})
    before = {t: j.query(f'SELECT * FROM {t}') for t in ('candidate_outcomes','candidates','scan_batches','openai_evaluations','candidate_labels')}
    result = enroll(j)
    rows = j.query('SELECT * FROM explore_close_outcomes')
    assert {r['population'] for r in rows} == {'arm','pool','evaluator','shadow'}
    assert {(r['population'],r['arm']) for r in rows if r['candidate_id']==cid} == {
        ('arm','A'),('arm','B'),('pool',''),('evaluator','')}
    assert all(r['reference_session']=='2026-11-16' for r in rows if r['enrollment_status']=='ready')
    assert all(before[t] == j.query(f'SELECT * FROM {t}') for t in before)
    assert enroll(j)['inserted'] == 0
    assert result['inserted'] == len(rows)


def test_candidate_leg_freezes_while_benchmark_retries_independently(j):
    scan(j)
    enroll(j)
    bars = Bars()
    bars.missing = {'SPY'}
    tracker.update(j, sources(bars))
    before = j.query("SELECT * FROM explore_close_outcomes WHERE enrollment_status='ready'")
    assert before and all(r['status_5d']=='pending' and r['r_5d']==pytest.approx(.1) and r['b_5d'] is None for r in before)
    assert all(json.loads(r['candidate_5d_json'])['leg']['return_fraction']==pytest.approx(.1) for r in before)
    bars.missing.clear()
    bars.ratio = 1.2
    bars.calls.clear()
    tracker.update(j, sources(bars, '2026-11-24T21:00:00Z'))
    after = j.query("SELECT * FROM explore_close_outcomes WHERE enrollment_status='ready'")
    assert all(r['status_5d']=='complete' and r['r_5d']==pytest.approx(.1) and r['b_5d']==pytest.approx(.2) for r in after)
    assert set(bars.calls)=={'SPY'}
    assert [r['candidate_5d_json'] for r in before]==[r['candidate_5d_json'] for r in after]
    tracker.update(j, sources(bars, '2026-12-01T21:00:02Z'))
    assert all(r['status_10d']=='complete' for r in j.query("SELECT * FROM explore_close_outcomes WHERE enrollment_status='ready'"))


def test_missing_after_deadline_is_final_and_never_null_complete(j):
    scan(j)
    enroll(j)
    bars = Bars()
    bars.missing = {'XYZ','SPY'}
    tracker.update(j, sources(bars, '2026-12-04T21:00:00Z'))
    before = j.query('SELECT candidate_5d_json,benchmark_5d_json,status_5d FROM explore_close_outcomes')
    assert all(r['status_5d']=='unavailable' for r in before)
    bars.missing.clear()
    tracker.update(j, sources(bars, '2026-12-05T21:00:00Z'))
    assert before==j.query('SELECT candidate_5d_json,benchmark_5d_json,status_5d FROM explore_close_outcomes')
    with pytest.raises(sqlite3.IntegrityError):
        j.conn.execute("UPDATE explore_close_outcomes SET status_5d='complete',r_5d=NULL,b_5d=NULL")
    j.conn.rollback()


def test_yield_and_restart_does_not_refetch_frozen_leg(j):
    scan(j)
    enroll(j)
    bars = Bars()
    src = sources(bars, should_yield=lambda: bool(bars.calls))
    result = tracker.update(j, src, should_yield=lambda: bool(bars.calls))
    assert result['yielded'] and len(bars.calls)==1
    first = j.one("SELECT * FROM explore_close_outcomes WHERE candidate_5d_json IS NOT NULL")
    assert first
    path = j.db_path
    j.close()
    j2 = JournalStore(path)
    bars.calls.clear()
    tracker.update(j2, sources(bars))
    assert j2.one('SELECT candidate_5d_json FROM explore_close_outcomes WHERE id=?',(first['id'],))['candidate_5d_json']==first['candidate_5d_json']
    j.conn = j2.conn


def test_exclusion_permanent_after_later_source_correction(j):
    cid = scan(j, exact=False)
    j.conn.execute("UPDATE scan_batches SET completed_at_utc='2026-11-16T22:00:00Z'")
    j.conn.commit()
    enroll(j)
    before = j.query("SELECT * FROM explore_close_outcomes WHERE population='arm'")
    assert before[0]['unavailable_reason']=='session_unproven'
    j.conn.execute("UPDATE scan_batches SET completed_at_utc='2026-11-16T15:00:00Z'")
    j.conn.commit()
    enroll(j)
    assert before==j.query("SELECT * FROM explore_close_outcomes WHERE population='arm'")
    assert cid==before[0]['candidate_id']


def test_cross_run_dedupe_uses_D_not_wall_date(j):
    scan(j, instant='2026-11-16T21:00:00Z')
    enroll(j)
    scan(j, 'later', instant='2026-11-17T15:00:00Z')
    enroll(j)
    rows = j.query("SELECT * FROM explore_close_outcomes WHERE population='arm' ORDER BY id")
    assert len(rows)==2 and [r['enrollment_status'] for r in rows]==['ready','duplicate_in_session']
    assert {r['reference_session'] for r in rows}=={'2026-11-17'}


def test_open_scan_is_deferred_without_permanent_unavailable(j):
    scan(j)
    j.conn.execute('UPDATE scan_batches SET completed_at_utc=NULL')
    j.conn.commit()
    result = enroll(j)
    assert result['open_scans_deferred']==1 and result['inserted']==0
    j.conn.execute("UPDATE scan_batches SET completed_at_utc='2026-11-16T15:00:00Z'")
    j.conn.commit()
    assert enroll(j)['inserted']>0


def test_normal_cooperative_entrypoint_includes_measurement_without_registry_calls(j, monkeypatch):
    from alphaos.learning.cooperative_outcomes import run_cooperative_outcomes
    scan(j)
    settings = make_settings(ALPHAOS_DB_PATH=j.db_path, EXPLORE_1_ENABLED=True)
    orch = SimpleNamespace(journal=j, settings=settings)
    before = j.query('SELECT * FROM preregistrations')
    result = run_cooperative_outcomes(orch, limit=20, should_yield=lambda: False)
    assert result['close_ref']['seeded']['inserted']>0
    assert 'close_ref' not in result['stages_not_started']
    assert before==j.query('SELECT * FROM preregistrations')


def test_later_overlap_marks_whole_key_including_prior_duplicates(j):
    scan(j, 'first', instant='2026-11-16T15:00:00Z', exact=False)
    scan(j, 'second', instant='2026-11-16T16:00:00Z', exact=False)
    enroll(j)
    assert j.count_rows('explore_close_outcomes', "population='arm' AND enrollment_status='duplicate_in_session'")==1
    scan(j, 'overlap', instant='2026-11-16T15:00:00Z', exact=False)
    j.conn.execute("UPDATE scan_batches SET completed_at_utc='2026-11-16T15:00:01Z' WHERE scan_batch_id='overlap'")
    j.conn.commit()
    enroll(j)
    rows = j.query("SELECT * FROM explore_close_outcomes WHERE population='arm'")
    assert len(rows)==3
    assert all(r['enrollment_status']=='unavailable' and r['unavailable_reason']=='order_unproven' for r in rows)


def test_source_ordering_checked_without_git_timestamp_in_bounds(j, monkeypatch):
    scan(j, exact=False)
    monkeypatch.setattr(lineage, 'committed_ordering', lambda sha: {
        'verified': False, 'commit': sha, 'reason': 'source_ordering_unreviewed'})
    enroll(j)
    row = j.one("SELECT * FROM explore_close_outcomes WHERE population='arm'")
    assert row['enrollment_status']=='unavailable'
    assert row['unavailable_reason']=='ordering_source_unproven'


def test_production_producer_pin_matches_candidate_ordering_bytes():
    import hashlib
    from pathlib import Path
    module = Path(tracker.__file__).resolve().parents[1]/'orchestrator.py'
    assert hashlib.sha256(module.read_bytes()).hexdigest() in lineage._APPROVED_PRODUCERS


def test_crash_after_leg_commit_keeps_scalar_atomic(j):
    scan(j)
    enroll(j)
    base = j.conn

    class CommitCrash:
        def __getattr__(self, name):
            return getattr(base, name)

        def commit(self):
            base.commit()
            row = base.execute("SELECT candidate_5d_json FROM explore_close_outcomes ORDER BY id LIMIT 1").fetchone()
            if row[0] and json.loads(row[0])["leg"]["status"] == "complete":
                raise RuntimeError("simulated process exit after durable leg")

    j.conn = CommitCrash()
    try:
        with pytest.raises(RuntimeError, match="simulated process exit"):
            tracker.update(j, sources(Bars()))
    finally:
        j.conn = base
    row = j.one("SELECT * FROM explore_close_outcomes ORDER BY id LIMIT 1")
    assert row["r_5d"] == json.loads(row["candidate_5d_json"])["leg"]["return_fraction"]
    assert row["status_5d"] == "pending" and row["b_5d"] is None


def all_populations_scan(j, scan_id='scan', *, exact=True):
    scan(j, scan_id, arm='A+B', exact=exact)
    cid = 'shadow_' + scan_id
    j.insert('candidates', {'candidate_id': cid, 'symbol': 'SHD', 'scan_batch_id': scan_id,
             'policy_version': 'explore_1', 'direction': 'long', 'lineage_id': 'lineage',
             'shadow_tier': 1, 'label_frozen_at_utc': '2026-11-16T15:00:00Z'})
    j.insert('candidate_labels', {'label_id': 'label_'+cid, 'candidate_id': cid,
             'symbol': 'SHD', 'scan_batch_id': scan_id, 'shadow_tier': 1,
             'label_frozen_at_utc': '2026-11-16T15:00:00Z', 'relabel_of': None})


def test_unavailable_proof_defers_whole_scan_then_healthy_retry(j, monkeypatch):
    all_populations_scan(j)
    healthy = lineage.committed_ordering
    monkeypatch.setattr(lineage, 'committed_ordering', lambda sha: {
        'verified': False, 'commit': sha, 'reason': 'source_commit_unavailable'})
    first = enroll(j)
    assert first['inserted'] == j.count_rows('explore_close_outcomes') == 0
    assert first['source_proof_scans_deferred'] == 1 and first['open_scans_deferred'] == 0
    assert first['source_proof_deferred_reasons'] == {'source_commit_unavailable': 1}
    assert first['source_proof_deferred']['scan']['deferred'] is True
    monkeypatch.setattr(lineage, 'committed_ordering', healthy)
    second = enroll(j)
    assert second['inserted'] == 5 and second['source_proof_scans_deferred'] == 0
    rows = j.query('SELECT * FROM explore_close_outcomes')
    assert {r['population'] for r in rows} == {'arm', 'pool', 'evaluator', 'shadow'}
    assert all(r['enrollment_status'] == 'ready' for r in rows)
    assert enroll(j)['inserted'] == 0
    assert rows == j.query('SELECT * FROM explore_close_outcomes')


@pytest.mark.parametrize('error', [OSError, RuntimeError, ValueError, TypeError, KeyError])
def test_any_proof_exception_defers_all_populations_without_leaking_error(j, monkeypatch, error):
    all_populations_scan(j)
    healthy = tracker.scan_proof

    def broken(*args):
        raise error('sensitive diagnostic must not be retained')

    monkeypatch.setattr(tracker, 'scan_proof', broken)
    first = enroll(j)
    assert first['inserted'] == j.count_rows('explore_close_outcomes') == 0
    assert first['source_proof_deferred_reasons'] == {'source_proof_exception': 1}
    assert first['source_proof_deferred']['scan']['error_type'] == error.__name__
    assert 'sensitive diagnostic' not in json.dumps(first)
    monkeypatch.setattr(tracker, 'scan_proof', healthy)
    assert enroll(j)['inserted'] == 5


@pytest.mark.parametrize('proof', [None, {}, {'verified': 1},
                                    {'verified': True, 'reason': 'source_commit_unavailable'},
                                    {'verified': False, 'reason': 'unexpected_failure'}])
def test_unknown_or_malformed_proof_cannot_freeze_exclusions(j, monkeypatch, proof):
    all_populations_scan(j)
    monkeypatch.setattr(tracker, 'scan_proof', lambda *args: proof)
    result = enroll(j)
    assert result['inserted'] == j.count_rows('explore_close_outcomes') == 0
    assert result['source_proof_scans_deferred'] == 1


def test_deferred_scan_does_not_block_healthy_scan_or_rewrite_saved_rows(j, monkeypatch):
    scan(j, 'healthy', symbol='OK')
    enroll(j)
    before = j.query('SELECT * FROM explore_close_outcomes')
    all_populations_scan(j, 'deferred')
    healthy = tracker.scan_proof

    def proof(candidates, *args):
        if any(c['scan_batch_id'] == 'deferred' for c in candidates):
            raise RuntimeError('temporarily unavailable')
        return healthy(candidates, *args)

    monkeypatch.setattr(tracker, 'scan_proof', proof)
    first = enroll(j)
    assert first['source_proof_scans_deferred'] == 1 and first['inserted'] == 0
    assert before == j.query('SELECT * FROM explore_close_outcomes')
    scan(j, 'new_healthy', symbol='NEW')
    assert enroll(j)['inserted'] == 3
    assert before == j.query("SELECT * FROM explore_close_outcomes WHERE scan_batch_id='healthy'")
    assert not j.query("SELECT * FROM explore_close_outcomes WHERE scan_batch_id='deferred'")
    monkeypatch.setattr(tracker, 'scan_proof', healthy)
    assert enroll(j)['inserted'] == 5


@pytest.mark.parametrize('failure', ['digest', 'missing_lineage', 'disagreeing_lineage', 'missing_commit'])
def test_definitive_source_failures_stay_permanent_after_healthy_retry(j, monkeypatch, failure):
    all_populations_scan(j, exact=False)
    healthy = lineage.committed_ordering
    if failure == 'digest':
        monkeypatch.setattr(lineage, 'committed_ordering', lambda sha: {
            'verified': False, 'commit': sha, 'reason': 'source_ordering_unreviewed',
            'orchestrator_sha256': 'f'*64})
    elif failure == 'missing_lineage':
        j.conn.execute("UPDATE candidates SET lineage_id='missing' WHERE candidate_id='cand_scan'")
    elif failure == 'disagreeing_lineage':
        j.insert('lineage_snapshots', {'lineage_id': 'other', 'git_commit_sha': 'b'*40, 'git_dirty': 0})
        j.conn.execute("UPDATE openai_evaluations SET lineage_id='other'")
    else:
        j.conn.execute("UPDATE lineage_snapshots SET git_commit_sha=NULL")
        monkeypatch.setattr(lineage, 'committed_ordering', lambda sha: {
            'verified': False, 'reason': 'source_commit_missing'})
    j.conn.commit()
    first = enroll(j)
    assert first['inserted'] == 5 and first['source_proof_scans_deferred'] == 0
    before = j.query('SELECT * FROM explore_close_outcomes')
    assert all(r['enrollment_status'] == 'unavailable' for r in before if r['population'] in ('arm', 'pool'))
    evaluator = next(r for r in before if r['population'] == 'evaluator')
    assert evaluator['enrollment_status'] == 'ready'
    assert not json.loads(evaluator['decision_json'])['ranking_eligible']
    j.conn.execute("UPDATE candidates SET lineage_id='lineage'")
    j.conn.execute("UPDATE openai_evaluations SET lineage_id='lineage'")
    j.conn.execute("UPDATE lineage_snapshots SET git_commit_sha=? WHERE lineage_id='lineage'", ('a'*40,))
    j.conn.commit()
    monkeypatch.setattr(lineage, 'committed_ordering', healthy)
    assert enroll(j)['inserted'] == 0
    assert before == j.query('SELECT * FROM explore_close_outcomes')


@pytest.mark.parametrize('failure', ['timeout', 'oserror', 'nonzero', 'unexpected'])
def test_actual_git_lookup_transients_are_not_cached(monkeypatch, failure):
    lineage._committed_ordering.cache_clear()
    body = b'reviewed committed producer bytes'
    monkeypatch.setattr(lineage, '_APPROVED_PRODUCERS', {hashlib.sha256(body).hexdigest()})
    calls = []

    def lookup(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            if failure == 'timeout':
                raise subprocess.TimeoutExpired('git', 2)
            if failure == 'oserror':
                raise OSError('temporary')
            if failure == 'unexpected':
                raise RuntimeError('unexpected proof exception')
            return SimpleNamespace(returncode=128, stdout=b'')
        return SimpleNamespace(returncode=0, stdout=body)

    monkeypatch.setattr(lineage.subprocess, 'run', lookup)
    try:
        if failure == 'unexpected':
            with pytest.raises(RuntimeError):
                lineage.committed_ordering('a'*40)
        else:
            assert lineage.committed_ordering('a'*40)['reason'] == 'source_commit_unavailable'
        assert lineage.committed_ordering('a'*40)['verified'] is True
        assert lineage.committed_ordering('a'*40)['verified'] is True
        assert len(calls) == 2  # No operator/cache clear between either pass.
    finally:
        lineage._committed_ordering.cache_clear()


def test_verified_digest_mismatch_is_cached_as_definitive(monkeypatch):
    lineage._committed_ordering.cache_clear()
    calls = []

    def lookup(*args, **kwargs):
        calls.append(1)
        return SimpleNamespace(returncode=0, stdout=b'unreviewed producer')

    monkeypatch.setattr(lineage.subprocess, 'run', lookup)
    try:
        first = lineage.committed_ordering('b'*40)
        assert first['reason'] == 'source_ordering_unreviewed' and first['verified'] is False
        first['verified'] = True
        assert lineage.committed_ordering('b'*40)['verified'] is False
        assert len(calls) == 1
    finally:
        lineage._committed_ordering.cache_clear()


def test_summary_separates_measured_failsafes_and_ranking_duplicates(j):
    scan(j, 'fallback')
    j.conn.execute("UPDATE openai_evaluations SET risk_flags_json='[\"OPENAI_REJECT\"]',decision='reject',confidence=0")
    j.conn.commit()
    scan(j, 'genuine', instant='2026-11-16T15:01:00Z')
    scan(j, 'duplicate', instant='2026-11-16T15:02:00Z')
    before = j.query('SELECT * FROM openai_evaluations')
    result = enroll(j)
    s = result['summary']
    assert s['populations']['evaluator:']['eligible'] == 2  # Fallback and genuine record both measured.
    assert s['evaluator_ranking'] == {
        'total': 3, 'eligible': 1, 'excluded': 2,
        'exclusion_reasons': {'evaluator_failsafe': 1, 'duplicate_in_session': 1},
    }
    tracker.update(j, sources(Bars()))
    fallback = j.one("SELECT * FROM explore_close_outcomes WHERE population='evaluator' AND scan_batch_id='fallback'")
    assert fallback['status_5d'] == 'complete' and fallback['r_5d'] == pytest.approx(.1)
    assert before == j.query('SELECT * FROM openai_evaluations')
    assert tracker.summary(j)['evaluator_ranking'] == s['evaluator_ranking']


def test_enrollment_requires_split_adjustment(j):
    scan(j)
    with pytest.raises(ValueError, match='invalid close_ref enrollment request'):
        enroll(j, adjustment='raw')
    assert j.count_rows('explore_close_outcomes') == 0
    assert enroll(j)['inserted'] == 3
    assert {r['adjustment'] for r in j.query('SELECT adjustment FROM explore_close_outcomes')} == {'split'}


def test_transient_proof_after_enrollment_preserves_all_frozen_records(j, monkeypatch):
    all_populations_scan(j)
    enroll(j)
    before = j.query('SELECT * FROM explore_close_outcomes')
    healthy = lineage.committed_ordering
    monkeypatch.setattr(lineage, 'committed_ordering', lambda sha: {
        'verified': False, 'commit': sha, 'reason': 'source_commit_unavailable'})
    result = enroll(j)
    assert result['inserted'] == result['newly_excluded'] == 0
    assert result['source_proof_scans_deferred'] == 1
    assert before == j.query('SELECT * FROM explore_close_outcomes')
    monkeypatch.setattr(lineage, 'committed_ordering', healthy)
    assert enroll(j)['inserted'] == 0
    assert before == j.query('SELECT * FROM explore_close_outcomes')
