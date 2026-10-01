"""Coverage denominators must include missing reviews, not just displayed rows."""
from alphaos.journal.journal_store import JournalStore
from alphaos.reports.trade_learning import VERSION, record_pending_trade_reviews, trade_learning_report


def test_unrecorded_outcome_is_visible_before_research_catchup():
    j = JournalStore(':memory:')
    j.insert('trade_outcomes', {'outcome_id': 'new', 'position_id': 'p', 'symbol': 'AMD'})
    before = j.conn.total_changes
    report = trade_learning_report(j)
    coverage = report['coverage']
    assert coverage['eligible_outcomes'] == 1
    assert coverage['missing_review_count'] == 1
    assert coverage['recorded_outcomes'] == 0
    assert coverage['current_audited_outcomes'] == 0
    assert coverage['current_unaudited_outcomes'] == 1
    assert coverage['complete_local_chain_count'] == 0
    assert coverage['complete_local_chain_count_basis'] == 'audited_records_only'
    assert coverage['missing_reviews'][0]['outcome_id'] == 'new'
    assert coverage['status'] == 'incomplete'
    assert j.conn.total_changes == before
    record_pending_trade_reviews(j)
    after = trade_learning_report(j)['coverage']
    assert after['missing_review_count'] == 0
    assert after['recorded_outcomes'] == after['current_audited_outcomes'] == 1
    assert after['complete_local_chain_count'] == 0
    assert after['status'] == 'incomplete'


def test_pagination_remainder_does_not_hide_pending_outcomes_or_certify_them():
    j = JournalStore(':memory:')
    for n in range(4):
        j.insert('trade_outcomes', {'outcome_id': f'o{n}', 'position_id': f'p{n}', 'symbol': 'AMD'})
    record_pending_trade_reviews(j, limit=2)
    c = trade_learning_report(j, limit=1)['coverage']
    assert c['eligible_outcomes'] == 4
    assert c['recorded_outcomes'] == c['missing_review_count'] == 2
    assert c['current_audited_outcomes'] == 1
    assert c['recorded_outcomes_not_audited'] == 1
    assert c['current_unaudited_outcomes'] == 3
    assert len(c['missing_reviews']) == 1 and c['missing_reviews_truncated'] is True


def test_empty_and_uninstalled_are_not_successful_observations():
    j = JournalStore(':memory:')
    assert trade_learning_report(j)['coverage']['status'] == 'not_observed'
    j.insert('trade_outcomes', {'outcome_id': 'o', 'position_id': 'p', 'symbol': 'AMD'})
    j.conn.execute('DROP TABLE trade_learning_records')
    before = j.conn.total_changes
    c = trade_learning_report(j)['coverage']
    assert c['status'] == 'not_installed'
    assert c['missing_review_count'] == c['current_unaudited_outcomes'] == 1
    assert j.conn.total_changes == before


def test_orphan_review_and_closed_position_without_outcome_are_explicit():
    j = JournalStore(':memory:')
    j.insert('trade_outcomes', {'outcome_id': 'o', 'position_id': 'p', 'symbol': 'AMD'})
    record_pending_trade_reviews(j)
    j.conn.execute('DELETE FROM trade_outcomes')
    j.insert('positions', {'position_id': 'closed', 'status': 'closed', 'symbol': 'AMD'})
    c = trade_learning_report(j)['coverage']
    assert c['eligible_outcomes'] == c['recorded_outcomes'] == 0
    assert c['orphan_review_count'] == 1
    assert c['closed_positions_without_outcome_count'] == 1
    assert c['current_audited_outcomes'] == 0
    assert c['status'] == 'incomplete'


def test_other_method_does_not_satisfy_current_recording():
    j = JournalStore(':memory:')
    j.insert('trade_outcomes', {'outcome_id': 'o', 'position_id': 'p', 'symbol': 'AMD'})
    record_pending_trade_reviews(j)
    j.conn.execute('UPDATE trade_learning_records SET method_version=?', (VERSION + '_other',))
    c = trade_learning_report(j)['coverage']
    assert c['missing_review_count'] == 1 and c['recorded_outcomes'] == 0


def test_population_stays_consistent_when_research_writer_commits_mid_report(tmp_path):
    import sqlite3
    path = tmp_path / 'ledger.db'
    j = JournalStore(str(path))
    j.conn.execute('PRAGMA journal_mode=WAL')
    j.insert('trade_outcomes', {'outcome_id': 'o', 'position_id': 'p', 'symbol': 'AMD'})
    writer = sqlite3.connect(path)
    original = j.one
    fired = []
    def one(sql, params=()):
        row = original(sql, params)
        if sql == 'SELECT COUNT(*) n FROM trade_outcomes' and not fired:
            fired.append(True)
            writer.execute("INSERT INTO trade_outcomes(outcome_id,position_id,symbol,created_at_utc,created_at_sgt) VALUES('later','q','AMD','2026-10-01T00:00:00+00:00','2026-10-01T08:00:00+08:00')")
            writer.commit()
        return row
    j.one = one
    c = trade_learning_report(j)['coverage']
    assert c['eligible_outcomes'] == c['missing_review_count'] == 1
    assert c['recorded_outcomes'] == 0
    assert not j.conn.in_transaction
    assert original('SELECT COUNT(*) n FROM trade_outcomes')['n'] == 2
    writer.close()


def test_report_does_not_commit_or_rollback_callers_transaction():
    j = JournalStore(':memory:')
    j.conn.execute("INSERT INTO trade_outcomes(outcome_id,position_id,symbol,created_at_utc,created_at_sgt) VALUES('o','p','AMD','2026-10-01T00:00:00+00:00','2026-10-01T08:00:00+08:00')")
    assert j.conn.in_transaction
    assert trade_learning_report(j)['coverage']['eligible_outcomes'] == 1
    assert j.conn.in_transaction
    j.conn.rollback()
    assert j.one('SELECT COUNT(*) n FROM trade_outcomes')['n'] == 0
