"""One protection proof across entry gating, recovery and the watchdog."""
from copy import deepcopy

import pytest

from alphaos.execution.protection_contract import verified_live_roles


def _bracket(exit_side='sell'):
    return {'broker_order_id':'entry', 'order_class':'bracket', 'symbol':'AAA',
            'side':'buy' if exit_side=='sell' else 'sell', 'status':'filled', 'qty':10, 'filled_qty':10,
            'legs':[
                {'broker_order_id':'tp','role':'take_profit','symbol':'AAA','side':exit_side,
                 'qty':10,'filled_qty':0,'status':'new','state':'accepted','limit_price':110},
                {'broker_order_id':'sl','role':'stop_loss','symbol':'AAA','side':exit_side,
                 'qty':10,'filled_qty':0,'status':'held','state':'accepted','stop_price':95},
            ]}


def _oco():
    row=_bracket()
    row.update(broker_order_id='tp', order_class='oco', side='sell', status='new', filled_qty=0)
    return row


@pytest.mark.parametrize('exit_side', ['sell','buy'])
def test_real_shaped_filled_bracket_working_target_and_held_stop(exit_side):
    assert verified_live_roles(_bracket(exit_side),10,'AAA',exit_side) == {'take_profit','stop_loss'}


def test_active_oco_can_prove_held_stop_with_working_parent_target():
    assert verified_live_roles(_oco(),10,'AAA','sell') == {'take_profit','stop_loss'}


@pytest.mark.parametrize('role', ['take_profit','stop_loss'])
@pytest.mark.parametrize('status', ['pending_new','pending_cancel','pending_replace','unrecognized',None])
def test_display_state_cannot_hide_unknown_or_pending_raw_status(role,status):
    parent=_bracket()
    next(leg for leg in parent['legs'] if leg['role']==role)['status']=status
    assert role not in verified_live_roles(parent,10,'AAA','sell')
    if role=='take_profit':
        assert 'stop_loss' not in verified_live_roles(parent,10,'AAA','sell')


def test_held_target_does_not_activate_held_stop():
    parent=_bracket()
    parent['legs'][0]['status']='held'
    assert verified_live_roles(parent,10,'AAA','sell') == set()


@pytest.mark.parametrize('updates', [
    {'status':'partially_filled','filled_qty':6}, {'status':'filled','filled_qty':6},
    {'status':'new'}, {'symbol':'OTHER'}, {'side':'sell'}, {'broker_order_id':None},
])
def test_incomplete_or_wrong_parent_is_not_protection(updates):
    parent=_bracket()
    parent.update(updates)
    assert verified_live_roles(parent,10,'AAA','sell') == set()


@pytest.mark.parametrize('updates', [
    {'qty':None}, {'qty':float('nan')}, {'qty':float('inf')}, {'filled_qty':None},
    {'filled_qty':-1}, {'filled_qty':11}, {'filled_qty':4}, {'symbol':None}, {'side':'buy'},
])
def test_remaining_protection_quantity_and_identity_must_match(updates):
    parent=_bracket()
    parent['legs'][1].update(updates)
    assert 'stop_loss' not in verified_live_roles(parent,10,'AAA','sell')


def test_partial_target_protects_exact_residual_only():
    parent=_oco()
    parent.update(status='partially_filled',filled_qty=4)
    parent['legs'][0].update(status='partially_filled',filled_qty=4)
    parent['legs'][1].update(qty=6)
    assert verified_live_roles(parent,6,'AAA','sell') == {'take_profit','stop_loss'}
    assert verified_live_roles(parent,10,'AAA','sell') == set()


def test_working_stop_can_remain_live_if_target_canceled():
    parent=_bracket()
    parent['legs'][0]['status']='canceled'
    parent['legs'][1]['status']='new'
    assert verified_live_roles(parent,10,'AAA','sell') == {'stop_loss'}


def test_duplicate_leg_identity_is_ambiguous():
    parent=_bracket()
    parent['legs'].append(deepcopy(parent['legs'][0]))
    assert verified_live_roles(parent,10,'AAA','sell') == set()


@pytest.mark.parametrize('status', ['pending_new','pending_cancel','held'])
def test_recovery_does_not_claim_all_pending_or_held_oco_is_protected(tmp_path,status):
    from test_execution_lifecycle_repair import setup
    from test_time_exit_enforcement import _NOW
    fake, _, journal, manager, _, _ = setup(tmp_path)
    original=fake.submit_oco
    def changed(spec):
        order=original(spec)
        order.status=status
        for leg in order.legs:
            leg.status=status
        return order
    fake.submit_oco=changed
    fake.raise_on_close_for.add('XLV')
    result=manager.enforce_time_exits(now=_NOW)
    decisions=result['deferred'] + result['errors']
    assert not any(item.get('reprotected') for item in decisions)
    assert any(item.get('critical') for item in decisions)
    journal.close()


def test_watchdog_held_stop_is_valid_but_pending_cancel_blocks(tmp_path):
    from test_execution_lifecycle_repair import setup
    from alphaos.execution import protection_watchdog
    fake, settings, journal, manager, _, _ = setup(tmp_path)
    fake.leg('XLV','stop_loss').status='held'
    result=protection_watchdog.run_watchdog_pass(journal,manager.alpaca,settings)
    assert result['protected']==1
    fake.leg('XLV','stop_loss').status='pending_cancel'
    result=protection_watchdog.run_watchdog_pass(journal,manager.alpaca,settings)
    assert result['unprotected']==1
    assert protection_watchdog.has_blocking_incident(journal)
    journal.close()


@pytest.mark.parametrize('role,field', [('stop_loss','stop_price'),('take_profit','limit_price')])
@pytest.mark.parametrize('value', [None,0,-1,float('nan'),float('inf')])
def test_unknown_or_invalid_protective_price_never_counts(role,field,value):
    parent=_bracket()
    next(leg for leg in parent['legs'] if leg['role']==role)[field]=value
    assert role not in verified_live_roles(parent,10,'AAA','sell')
