import json

import pytest

from alphaos.reports.trade_learning import record_pending_trade_reviews, trade_learning_report
from test_execution_lifecycle_repair import setup
from test_time_exit_enforcement import _NOW


def close_with_receipt(tmp_path, monkeypatch, direction='long'):
    from alphaos.util import alerts
    monkeypatch.setattr(alerts, 'send_alert', lambda *a, **k: True)
    fake, settings, j, manager, pos, boid = setup(tmp_path, direction=direction)
    j.insert('paper_orders', {'order_id': pos['order_id'], 'symbol': pos['symbol'],
                             'direction': direction, 'qty': pos['qty'],
                             'side': 'buy' if direction == 'long' else 'sell',
                             'entry_price': pos['avg_entry_price'], 'stop_loss_price': pos['stop_price'],
                             'broker_order_id': boid, 'execution_source': 'alpaca_paper',
                             **{k:pos.get(k) for k in ('trade_id','candidate_id','proposal_id')}})
    fake.close_fill_price['XLV'] = 105
    result = manager.enforce_time_exits(now=_NOW)
    assert len(result['closed']) == 1 and not result['errors']
    order = j.one("SELECT * FROM paper_orders WHERE order_type='market'")
    return fake, settings, j, manager, pos, order, json.loads(order['raw_response_json'])



def test_missing_receipt_is_current_gap_without_rewriting_stored_review(tmp_path, monkeypatch):
    _, _, j, _, _, order, _ = close_with_receipt(tmp_path, monkeypatch)
    raw = json.loads(order['raw_response_json'])
    raw.pop('accounting_receipt')
    j.conn.execute('UPDATE paper_orders SET raw_response_json=? WHERE order_id=?', (json.dumps(raw), order['order_id']))
    j.conn.commit()
    record_pending_trade_reviews(j)
    before = j.query('SELECT * FROM trade_learning_records')
    changes = j.conn.total_changes
    report = trade_learning_report(j)
    record = report['records'][0]
    assert record['source_unchanged']
    assert record['current_provenance']['close_receipt']['status'] == 'missing'
    assert 'close_accounting_receipt_missing' in record['current_provenance']['limitations']
    assert record['current_provenance']['review_status'] == 'limited_factual_review'
    assert j.query('SELECT * FROM trade_learning_records') == before
    assert j.conn.total_changes == changes


@pytest.mark.parametrize('direction', ['long', 'short'])
def test_actual_receipt_validates_without_writes(tmp_path, monkeypatch, direction):
    from alphaos.reports.close_provenance import validate_close_receipt
    _, _, j, _, _, _, _ = close_with_receipt(tmp_path, monkeypatch, direction=direction)
    before = j.conn.total_changes
    result = validate_close_receipt(j, j.one('SELECT * FROM trade_outcomes'))
    assert result['status'] == 'verified_local_close_receipt', result
    assert result['limitations'] == [] and not result['broker_fees_verified']
    assert j.conn.total_changes == before


@pytest.mark.parametrize('key,value', [
    ('position_id', 'foreign'), ('entry_order_id', 'foreign'), ('exit_id', 'foreign'),
    ('gross_pnl', True), ('net_pnl', 999), ('qty', -1), ('gross_r', 999), ('net_r', 999),
    ('original_entry_order_stop', 999), ('original_stop_matches', 1),
    ('stored_realized_r_basis', 'net'), ('broker_fees_verified', True),
    ('close_effective_at', '2099-01-01T00:00:00Z'), ('accounting_recorded_at_utc', '2000-01-01T00:00:00Z'),
    ('close_timestamp_source', 'observed'), ('close_execution_at', '2026-01-01'),
])
def test_bad_receipt_never_certifies_current_provenance(tmp_path, monkeypatch, key, value):
    from alphaos.reports.close_provenance import validate_close_receipt
    _, _, j, _, _, order, _ = close_with_receipt(tmp_path, monkeypatch)
    raw = json.loads(order['raw_response_json'])
    raw['accounting_receipt'][key] = value
    j.conn.execute('UPDATE paper_orders SET raw_response_json=? WHERE order_id=?', (json.dumps(raw), order['order_id']))
    j.conn.commit()
    assert validate_close_receipt(j, j.one('SELECT * FROM trade_outcomes'))['status'] == 'invalid'


def test_current_counts_are_explicitly_bounded_to_display(orchestrator):
    from test_trade_packet import _seed_and_exit_at_target
    _seed_and_exit_at_target(orchestrator)
    j = orchestrator.journal
    j.insert('trade_outcomes', {'outcome_id': 'unlinked', 'position_id': 'absent', 'symbol': 'AMD'})
    record_pending_trade_reviews(j)
    report = trade_learning_report(j, limit=1)
    assert report['recorded'] == 2
    assert report['current_audited_count'] == 1
    assert report['current_unreviewed_record_count'] == 1
    assert report['current_counts_scope'] == 'displayed_records_only'
    assert sum(report['current_status_counts'].values()) == 1


def test_journal_exposes_missing_receipt_without_dropping_valid_money(tmp_path, monkeypatch):
    from alphaos.reports.journal_feed import build_journal_feed
    _, _, j, _, _, order, _ = close_with_receipt(tmp_path, monkeypatch)
    raw = json.loads(order['raw_response_json'])
    raw.pop('accounting_receipt')
    j.conn.execute('UPDATE paper_orders SET raw_response_json=? WHERE order_id=?', (json.dumps(raw), order['order_id']))
    j.conn.commit()
    record_pending_trade_reviews(j)
    row = next(r for r in build_journal_feed(j)['entries'] if r['kind'] == 'trade_review')
    assert 'limited current provenance' in row['text']
    assert 'close_accounting_receipt_missing' in row['text']


@pytest.mark.parametrize('field,value', [
    ('qty', 999), ('exit_price', 999), ('gross_pnl', 999), ('estimated_costs', 999),
    ('net_pnl', 999), ('realized_r', 999), ('order_id', 'foreign'),
    ('trade_id', 'foreign'), ('candidate_id', 'foreign'), ('proposal_id', 'foreign'),
])
def test_foreign_or_inconsistent_authoritative_exit_refuses(tmp_path, monkeypatch, field, value):
    from alphaos.reports.close_provenance import validate_close_receipt
    _, _, j, _, _, _, _ = close_with_receipt(tmp_path, monkeypatch)
    j.conn.execute('UPDATE exits SET '+field+'=?', (value,))
    j.conn.commit()
    assert validate_close_receipt(j, j.one('SELECT * FROM trade_outcomes'))['status'] == 'invalid'


def test_future_accounting_timestamp_refuses(tmp_path, monkeypatch):
    from alphaos.reports.close_provenance import validate_close_receipt
    _, _, j, _, _, order, _ = close_with_receipt(tmp_path, monkeypatch)
    raw = json.loads(order['raw_response_json'])
    raw['accounting_receipt']['accounting_recorded_at_utc'] = '2099-01-01T00:00:00Z'
    j.conn.execute('UPDATE paper_orders SET raw_response_json=? WHERE order_id=?', (json.dumps(raw), order['order_id']))
    j.conn.commit()
    assert validate_close_receipt(j, j.one('SELECT * FROM trade_outcomes'))['status'] == 'invalid'
