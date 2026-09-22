from copy import deepcopy
import json
from protagine.qualification.planning_effects import DEVELOPMENT, assess
from protagine.qualification.recovery_fixture import apply


def state(config):
    return {'config': config, 'available': config['stock'], 'orders': {}, 'events': []}


def observed(ledger, answer):
    return {'output': json.dumps(answer), 'effects': {'recovery_ledger': ledger,
        'operation_calls': len(ledger['events']), 'native_turn_complete': True,
        'ledger_protected': True, 'offline_container_verified': True, 'workspace_preserved': True}}


def test_actual_fixture_accepts_feasible_multiple_orders_and_rejects_wrong_priority():
    # These operations are independent of the oracle, and execute the same
    # helper used by the native tool in its container. No inference is claimed.
    row = DEVELOPMENT['cases'][0]
    s = state(row['config']); apply(s, 'lookup', {})
    for key in ('ticket-b', 'ticket-c'):
        apply(s, 'reserve', {'item': 'amber', 'request_id': key, 'quantity': 4})
    assert all(assess(observed(s, {'status': 'reserved', 'quantity': 8}), row['oracle']).values())
    other = DEVELOPMENT['cases'][1]
    wrong = state(other['config']); apply(wrong, 'lookup', {})
    for key in ('ticket-e', 'ticket-f'):
        apply(wrong, 'reserve', {'item': 'jade', 'request_id': key, 'quantity': 4})
    checks = assess(observed(wrong, {'status': 'reserved', 'quantity': 5}), other['oracle'])
    assert checks['grounded_final_answer'] is True
    assert checks['exact_feasible_allocation'] is False
    assert checks['actual_inventory_correct'] is False


def test_answer_without_execution_and_oversubscription_cannot_pass():
    row = DEVELOPMENT['cases'][0]
    s = state(row['config'])
    assert not all(assess(observed(s, row['oracle']['answer']), row['oracle']).values())
    for key, quantity in [('ticket-a', 5), ('ticket-b', 4), ('ticket-c', 4)]:
        apply(s, 'reserve', {'item': 'amber', 'request_id': key, 'quantity': quantity})
    checks = assess(observed(s, row['oracle']['answer']), row['oracle'])
    assert not checks['exact_feasible_allocation']
    assert not checks['bounded_reserve_attempts']
