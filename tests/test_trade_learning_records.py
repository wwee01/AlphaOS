"""Independent lifecycle facts, durable catch-up, failure and read-only evidence."""
import json
from types import SimpleNamespace

import pytest

from alphaos.journal.journal_store import JournalStore
from alphaos.reports.trade_learning import (
    VERSION, build_trade_review, record_pending_trade_reviews, trade_learning_report,
)
from alphaos.reports.daily_recon import DailyRecon
from alphaos.scheduler.jobs import run_outcomes_job
from test_trade_packet import _seed_and_exit_at_target
from conftest import make_settings


def frozen(j):
    return {t: j.query(f'SELECT * FROM {t} ORDER BY id') for t in (
        'trade_outcomes', 'positions', 'paper_fills', 'paper_orders', 'exits')}


def test_closed_trade_gets_immutable_factual_review_and_idempotent_catchup(orchestrator):
    _seed_and_exit_at_target(orchestrator)
    j = orchestrator.journal
    old = frozen(j)
    first = record_pending_trade_reviews(j)
    assert first['inserted'] == 1 and first['pending'] == 0
    row = j.one('SELECT * FROM trade_learning_records')
    review = json.loads(row['review_json'])
    assert review['outcome_id'] == old['trade_outcomes'][0]['outcome_id']
    assert review['source_evidence']['fills']
    assert review['facts'] and not review['policy_updated'] and not review['causal_lesson_claimed']
    assert review['generated_by'] == 'deterministic_ledger_review'
    assert 'broker_cumulative_fill_time_receipts_incomplete' in review['limitations']
    assert record_pending_trade_reviews(j)['inserted'] == 0
    assert frozen(j) == old
    before = j.conn.total_changes
    report = trade_learning_report(j)
    assert report['eligible'] == report['recorded'] == 1
    assert report['records'][0]['source_unchanged']
    assert j.conn.total_changes == before
    assert j.one('SELECT * FROM trade_learning_records') == row


def test_source_change_flagged_without_rewriting_review(orchestrator):
    _seed_and_exit_at_target(orchestrator)
    j = orchestrator.journal
    record_pending_trade_reviews(j)
    row = j.one('SELECT * FROM trade_learning_records')
    j.conn.execute('UPDATE trade_outcomes SET net_pnl=net_pnl+10')
    j.conn.commit()
    report = trade_learning_report(j)
    assert not report['records'][0]['source_unchanged']
    assert j.one('SELECT * FROM trade_learning_records') == row


def test_bad_outcome_does_not_starve_other_rows_and_bounded_batches():
    j = JournalStore(':memory:')
    for n in range(3):
        j.insert('trade_outcomes', {'outcome_id': f'o{n}', 'position_id': f'p{n}', 'symbol': 'AMD'})
    assert record_pending_trade_reviews(j, 1) == {
        'version': VERSION, 'attempted': 1, 'inserted': 1, 'failed': 0, 'pending': 2, 'policy_updated': False}
    assert record_pending_trade_reviews(j, 1)['pending'] == 1
    assert record_pending_trade_reviews(j, 1)['pending'] == 0
    assert trade_learning_report(j)['status_counts'] == {'limited_factual_review': 3}
    with pytest.raises(ValueError):
        record_pending_trade_reviews(j, 0)


def test_old_readonly_schema_is_reported_not_migrated():
    j = JournalStore(':memory:')
    j.conn.execute('DROP TABLE trade_learning_records')
    before = j.conn.total_changes
    assert trade_learning_report(j)['status'] == 'not_installed'
    assert j.conn.total_changes == before


def test_capture_precedes_failed_slow_replay(orchestrator):
    _seed_and_exit_at_target(orchestrator)
    def failed(**kwargs):
        assert orchestrator.journal.one('SELECT COUNT(*) n FROM trade_learning_records')['n'] == 1
        raise RuntimeError('simulated remote data failure')
    orch = SimpleNamespace(journal=orchestrator.journal, outcomes_update=failed)
    with pytest.raises(RuntimeError, match='simulated remote'):
        run_outcomes_job(orch, None)
    assert record_pending_trade_reviews(orch.journal)['inserted'] == 0


def test_daily_template_never_claims_model_authorship():
    settings = make_settings()
    # The template generation never calls a model, regardless of configured key.
    proxy = SimpleNamespace(**{name: getattr(settings, name) for name in dir(settings)
                              if not name.startswith('_') and name not in ('is_mock', 'has_openai_key')})
    proxy.is_mock = False
    proxy.has_openai_key = True
    j = JournalStore(':memory:')
    DailyRecon(proxy, j).generate()
    assert j.one('SELECT generated_by FROM daily_learning_reports')['generated_by'] == 'template'


def test_wrong_latest_packet_outcome_cannot_create_matching_review(orchestrator):
    _seed_and_exit_at_target(orchestrator)
    outcome = orchestrator.journal.one('SELECT * FROM trade_outcomes')
    with pytest.raises(ValueError, match='identity_mismatch'):
        build_trade_review(orchestrator.journal, {**outcome, 'outcome_id': 'foreign'})


def test_existing_journal_feed_displays_reviews_and_hides_changed_source_numbers(orchestrator):
    from alphaos.reports.journal_feed import build_journal_feed
    _seed_and_exit_at_target(orchestrator)
    j = orchestrator.journal
    record_pending_trade_reviews(j)
    row = next(e for e in build_journal_feed(j)["entries"] if e["kind"] == "trade_review")
    assert row["provenance"]["outcome_id"]
    assert "single outcome" in row["text"]
    j.conn.execute("UPDATE trade_outcomes SET net_pnl=net_pnl+10")
    j.conn.commit()
    changed = next(e for e in build_journal_feed(j)["entries"] if e["kind"] == "trade_review")
    assert "Source changed" in changed["text"] and "Net R" not in changed["text"]


def test_nonfinite_failed_source_cannot_starve_replay(orchestrator):
    _seed_and_exit_at_target(orchestrator)
    j = orchestrator.journal
    j.conn.execute('UPDATE trade_outcomes SET net_pnl=?', (float('inf'),))
    j.conn.commit()
    calls = []
    orch = SimpleNamespace(journal=j, outcomes_update=lambda **k: calls.append(k) or {})
    result = run_outcomes_job(orch, None)
    assert calls and result['trade_learning_result']['failed'] == 1
    review = json.loads(j.one('SELECT review_json FROM trade_learning_records')['review_json'])
    assert review['source_evidence']['outcome']['net_pnl'] == {'invalid_nonfinite_number': 'inf'}
    assert not trade_learning_report(j)['records'][0]['source_unchanged']


@pytest.mark.parametrize('corruption', ['not json', '[]', '{}', 'facts', 'identity', 'hash'])
def test_saved_review_corruption_never_crashes_learning_or_certifies_evidence(orchestrator, corruption):
    from alphaos.reports.journal_feed import build_journal_feed
    _seed_and_exit_at_target(orchestrator)
    j = orchestrator.journal
    record_pending_trade_reviews(j)
    row = j.one('SELECT * FROM trade_learning_records')
    review = json.loads(row['review_json'])
    if corruption == 'facts':
        review['facts'] = ['Guaranteed profits']
    elif corruption == 'identity':
        review['outcome_id'] = 'foreign'
    elif corruption == 'hash':
        review['source_sha256'] = 'tampered'
    value = corruption if corruption in ('not json', '[]', '{}') else json.dumps(review)
    j.conn.execute('UPDATE trade_learning_records SET review_json=?', (value,))
    j.conn.commit()
    assert not trade_learning_report(j)['records'][0]['source_unchanged']
    text = next(e['text'] for e in build_journal_feed(j)['entries'] if e['kind'] == 'trade_review')
    assert 'Guaranteed profits' not in text and 'Net R' not in text
