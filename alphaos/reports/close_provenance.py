"""Read-time validation of retained close receipts; no writes or fee certification.

This is separate from immutable factual_trade_review_v1. A missing old receipt
is evidence unavailable today, never permission to reconstruct historical data.
"""
from __future__ import annotations

import json
import math
from datetime import datetime

from alphaos.execution.costs import CostModel
from alphaos.util import timeutils

VERSION = 'close_receipt_validation_v1'


def _number(row, key):
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('close_receipt_invalid_number:' + key)
    return value


def _same(left, right):
    return math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-8)


def _time(value):
    if not isinstance(value, str):
        raise ValueError('close_receipt_invalid_timestamp')
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.utcoffset() is None:
        raise ValueError('close_receipt_naive_timestamp')
    return stamp


def validate_close_receipt(journal, outcome):
    result = {'version': VERSION, 'status': 'invalid', 'limitations': [],
              'broker_fees_verified': False}
    try:
        if not outcome:
            raise ValueError('close_receipt_outcome_missing')
        exits = journal.query('SELECT * FROM exits WHERE position_id=?', (outcome['position_id'],))
        if len(exits) != 1 or exits[0]['exit_id'] != outcome.get('exit_id'):
            raise ValueError('close_receipt_exit_identity_unverifiable')
        closure = exits[0]
        order = journal.order_by_id(closure.get('exit_order_id'))
        position = journal.position_by_id(outcome['position_id'])
        if not order or not position or position.get('status') != 'closed':
            raise ValueError('close_receipt_closed_position_order_missing')
        raw = order.get('raw_response_json')
        raw = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(raw, dict):
            raise ValueError('close_receipt_invalid_response')
        if 'accounting_receipt' not in raw:
            return {**result, 'status': 'missing', 'limitations': ['close_accounting_receipt_missing']}
        receipt = raw['accounting_receipt']
        if not isinstance(receipt, dict) or receipt.get('version') != 'trade_close_accounting_v1':
            raise ValueError('close_receipt_invalid_version')
        json.dumps(receipt, allow_nan=False)
        entry = journal.order_by_id(position.get('order_id'))
        if (not entry or receipt.get('position_id') != outcome['position_id']
                or receipt.get('exit_id') != closure['exit_id']
                or receipt.get('entry_order_id') != entry['order_id']
                or any(row.get('symbol') != outcome['symbol'] for row in (position, order, entry, closure))
                or position.get('direction') != outcome.get('direction')
                or outcome.get('direction') not in {'long', 'short'}):
            raise ValueError('close_receipt_foreign_identity')
        if closure.get('order_id') != entry['order_id']:
            raise ValueError('close_receipt_closure_entry_mismatch')
        for key in ('trade_id', 'candidate_id', 'proposal_id'):
            if any(row.get(key) != outcome.get(key) for row in (closure, position, entry, order)):
                raise ValueError('close_receipt_closure_origin_mismatch:' + key)
        for field, outcome_field in (('qty', 'qty'), ('exit_price', 'exit_price'),
                                     ('gross_pnl', 'gross_pnl'), ('estimated_costs', 'costs'),
                                     ('net_pnl', 'net_pnl'), ('realized_r', 'realized_r')):
            if not _same(_number(closure, field), _number(outcome, outcome_field)):
                raise ValueError('close_receipt_closure_terms_mismatch:' + field)
        fills = journal.query('SELECT * FROM paper_fills WHERE order_id=?', (order['order_id'],))
        side = 'buy_to_cover' if outcome['direction'] == 'short' else 'sell'
        if not fills or any(f.get('position_id') != position['position_id']
                            or f.get('symbol') != outcome['symbol'] or f.get('side') != side
                            or f.get('broker_order_id') != order.get('broker_order_id') for f in fills):
            raise ValueError('close_receipt_exit_fill_identity_unverifiable')
        for key, field in (('qty', 'qty'), ('entry_vwap', 'entry_price'), ('exit_vwap', 'exit_price'),
                           ('gross_pnl', 'gross_pnl'), ('modeled_costs', 'costs'), ('net_pnl', 'net_pnl'),
                           ('stored_realized_r', 'realized_r')):
            if not _same(_number(receipt, key), _number(outcome, field)):
                raise ValueError('close_receipt_outcome_terms_mismatch:' + key)
        qty = _number(receipt, 'qty')
        entry_price, exit_price = _number(receipt, 'entry_vwap'), _number(receipt, 'exit_vwap')
        if min(qty, entry_price, exit_price) <= 0 or not _same(qty, _number(position, 'qty')):
            raise ValueError('close_receipt_invalid_quantity_or_price')
        if not _same(entry_price, _number(position, 'avg_entry_price')):
            raise ValueError('close_receipt_entry_basis_mismatch')
        stop = _number(receipt, 'original_entry_order_stop')
        if (receipt.get('original_stop_matches') is not True or receipt.get('risk_basis') != 'position_stop_at_close'
                or not _same(stop, _number(entry, 'stop_loss_price'))
                or not _same(stop, _number(receipt, 'stop_price_at_close'))
                or not _same(stop, _number(position, 'stop_price')) or stop <= 0):
            raise ValueError('close_receipt_original_risk_mismatch')
        risk = abs(entry_price - stop) * qty
        gross = (exit_price - entry_price) * qty * (-1 if outcome['direction'] == 'short' else 1)
        if risk <= 0 or not _same(round(gross, 2), _number(receipt, 'gross_pnl')):
            raise ValueError('close_receipt_gross_arithmetic_mismatch')
        model = receipt.get('cost_model')
        if not isinstance(model, dict) or not isinstance(model.get('breakdown'), dict):
            raise ValueError('close_receipt_cost_model_missing')
        params = [_number(model, k) for k in ('commission_per_share', 'min_commission', 'slippage_bps_per_side')]
        if any(v < 0 for v in params):
            raise ValueError('close_receipt_negative_cost_parameter')
        costs = CostModel(*params).costs(qty, entry_price, exit_price).as_dict()
        if any(not _same(v, _number(model['breakdown'], k)) for k, v in costs.items()):
            raise ValueError('close_receipt_cost_breakdown_mismatch')
        net = _number(receipt, 'net_pnl')
        if (not _same(costs['total'], _number(receipt, 'modeled_costs'))
                or not _same(net, round(round(gross, 2) - costs['total'], 2))
                or not _same(_number(receipt, 'gross_r'), gross / risk)
                or not _same(_number(receipt, 'net_r'), net / risk)
                or not _same(_number(receipt, 'stored_realized_r'), round(gross / risk, 3))
                or receipt.get('stored_realized_r_basis') != 'gross'
                or receipt.get('broker_fees_verified') is not False):
            raise ValueError('close_receipt_cost_or_r_basis_mismatch')
        if any(_number(f, 'qty') <= 0 or _number(f, 'price') <= 0 for f in fills):
            raise ValueError('close_receipt_nonpositive_final_fills')
        if sum(_number(f, 'qty') for f in fills) > qty + 1e-8:
            raise ValueError('close_receipt_final_fill_quantity_exceeds_trade')
        effective = _time(receipt.get('close_effective_at'))
        recorded = _time(receipt.get('accounting_recorded_at_utc'))
        if (effective != _time(outcome.get('created_at_utc'))
                or effective != _time(closure.get('created_at_utc')) or recorded < effective
                or recorded > _time(timeutils.now_utc().isoformat())
                or receipt.get('close_timestamp_source') != 'broker'
                or raw.get('fill_timestamp_source') != 'broker'
                or _time(receipt.get('close_execution_at')) != effective
                or max(_time(f.get('filled_at')) for f in fills) != effective
                or max(_time(f.get('created_at_utc')) for f in fills) > recorded):
            raise ValueError('close_receipt_timestamp_provenance_unverifiable')
        return {**result, 'status': 'verified_local_close_receipt'}
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        # Keep stable limitation codes; never echo provider payloads or JSON.
        code = str(exc) if isinstance(exc, ValueError) and str(exc).startswith('close_receipt_') else 'close_receipt_malformed'
        return {**result, 'limitations': [code]}
