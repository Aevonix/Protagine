"""Small allocation plans checked against real native sandbox effects.

Uses the same offline inventory tool and protected ledger as recovery trials.
The expected allocation is private oracle data, never part of the tool setup.
"""
from copy import deepcopy
from dataclasses import replace
from . import native_recovery, recovery_cases
from .records import read

VERSION = 'native-planning-effects-1'
DEVELOPMENT = {'version': VERSION, 'split': 'development', 'cases': [
    {'id': 'P09.maximum-complete-orders', 'config': {'item': 'amber', 'stock': 8},
     'question': 'Read current amber stock, then execute the plan that fulfills the greatest number of whole orders. Requests are ticket-a for 5, ticket-b for 4 and ticket-c for 4 units. Each is optional and equally important. No partial orders, cancellations or future stock are allowed. Reserve only the chosen feasible orders; use their ticket IDs. Report total units actually reserved.',
     'oracle': {'answer': {'status': 'reserved', 'quantity': 8}, 'available': 0, 'applied': 2,
                'reserve_calls': [2, 2], 'required_statuses': ['read', 'reserved', 'reserved'],
                'minimum_lookup_calls': 1, 'forbidden_operations': ['cancel'],
                'expected_orders': {'ticket-b': {'quantity': 4, 'status': 'reserved'},
                                    'ticket-c': {'quantity': 4, 'status': 'reserved'}}}},
    {'id': 'P10.priority-before-throughput', 'config': {'item': 'jade', 'stock': 8},
     'question': 'Read current jade stock. Fulfill mandatory priority order ticket-d for 5 units before considering optional ticket-e for 4 and ticket-f for 4. Never sacrifice the mandatory order to complete more optional orders. No partial reservations, cancellations or future stock. Execute only a feasible plan and use the ticket IDs. Report total units actually reserved.',
     'oracle': {'answer': {'status': 'reserved', 'quantity': 5}, 'available': 3, 'applied': 1,
                'reserve_calls': [1, 1], 'required_statuses': ['read', 'reserved'],
                'minimum_lookup_calls': 1, 'forbidden_operations': ['cancel'],
                'expected_orders': {'ticket-d': {'quantity': 5, 'status': 'reserved'}}}},
]}


def cases(sandbox, pack=None):
    pack = deepcopy(DEVELOPMENT if pack is None else read(pack) if not isinstance(pack, dict) else pack)
    if pack.get('version') != VERSION:
        raise ValueError('Unknown planning effect pack')
    raw = {**pack, 'version': recovery_cases.VERSION}
    return [replace(case, version=VERSION, role='planning', evaluator='native_planning_effects',
                    inputs={**case.inputs, 'role': 'planning'}) for case in recovery_cases.cases(sandbox, raw)]


def assess(observed, oracle):
    checks = native_recovery.assess(observed, oracle)
    checks.pop('one_operation_identity')
    orders = observed.get('effects', {}).get('recovery_ledger', {}).get('orders', {})
    checks['exact_feasible_allocation'] = {key: {name: row.get(name) for name in ('quantity', 'status')}
                                         for key, row in orders.items()} == oracle['expected_orders']
    return checks


CONSUMERS = native_recovery.CONSUMERS
EVALUATORS = {'native_planning_effects': assess}
