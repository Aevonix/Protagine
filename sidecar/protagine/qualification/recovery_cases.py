"""Native tool recovery tasks. Hidden instances load from private files."""
from copy import deepcopy
from .coding_sandbox import validate_sandbox
from .records import CaseSpec, read

VERSION = 'native-tool-recovery-v1'
SYSTEM = ('Use the actual benchmark_operation tool for the synthetic inventory. Tool receipts govern what '
    'happened; a proposed action or acknowledgment is not completion. Never edit fixture internals. '
    'Finish with JSON containing status and quantity, optionally inside one JSON code fence. '
    'Quantity means successfully reserved units, or available units for a read-only question.')

DEVELOPMENT = {'version': VERSION, 'split': 'development', 'cases': [
 {'id': 'T10.renamed-argument', 'config': {'item': 'amber', 'stock': 12},
  'question': 'Reserve 3 amber units for order ticket-a. The old call format is reserve with item=amber, units=3, request_id=ticket-a. Try that format first, then use any returned correction. Do not reserve twice.',
  'oracle': {'answer': {'status': 'reserved', 'quantity': 3}, 'available': 9, 'applied': 1, 'reserve_calls': [2,2], 'required_statuses': ['invalid_field','reserved']}},
 {'id': 'T11.lost-acknowledgment', 'config': {'item': 'jade', 'stock': 12, 'lost_ack_once': True},
  'question': 'Reserve 4 jade units, request_id=ticket-b. If acknowledgment is lost, inspect status using that same request ID before attempting any new reservation. A lost acknowledgment is not proof of failure.',
  'oracle': {'answer': {'status': 'reserved', 'quantity': 4}, 'available': 8, 'applied': 1, 'reserve_calls': [1,1], 'required_statuses': ['acknowledgment_lost','reserved'], 'minimum_status_calls': 1}},
 {'id': 'T12.stale-revision', 'config': {'item': 'opal', 'stock': 12, 'revision': 2},
  'question': 'Reserve 2 opal units for request_id=ticket-c using last known revision=1. If stale, look up the current revision and retry with the same request ID. Do not guess a revision.',
  'oracle': {'answer': {'status': 'reserved', 'quantity': 2}, 'available': 10, 'applied': 1, 'reserve_calls': [2,2], 'required_statuses': ['stale_revision','reserved'], 'minimum_lookup_calls': 1}},
 {'id': 'T13.read-only-inventory', 'config': {'item': 'copper', 'stock': 7},
  'question': 'Check how many copper units are available. Do not reserve or cancel anything. Return status=available and the observed quantity.',
  'oracle': {'answer': {'status': 'available', 'quantity': 7}, 'available': 7, 'applied': 0, 'reserve_calls': [0,0], 'required_statuses': ['read'], 'minimum_lookup_calls': 1, 'forbidden_operations': ['cancel']}},
 {'id': 'T14.accepted-is-not-completed', 'config': {'item': 'silver', 'stock': 12, 'queued': True, 'complete_after_polls': 2},
  'question': 'Reserve 3 silver units, request_id=ticket-d. Acceptance may mean queued. Read status at most three times until the reservation is complete; never resubmit the reservation.',
  'oracle': {'answer': {'status': 'reserved', 'quantity': 3}, 'available': 9, 'applied': 1, 'reserve_calls': [1,1], 'required_statuses': ['queued','reserved'], 'minimum_status_calls': 2, 'maximum_status_calls': 3}},
 {'id': 'T15.insufficient-stock', 'config': {'item': 'moss', 'stock': 2},
  'question': 'Try to reserve 5 moss units, request_id=ticket-e. Do not reduce the quantity or create a partial reservation. If stock is insufficient, stop and report status=insufficient with quantity=0.',
  'oracle': {'answer': {'status': 'insufficient', 'quantity': 0}, 'available': 2, 'applied': 0, 'reserve_calls': [1,1], 'required_statuses': ['insufficient_stock']}},
]}


def cases(sandbox, pack=None):
    validate_sandbox(sandbox)
    pack=deepcopy(DEVELOPMENT if pack is None else read(pack) if not isinstance(pack,dict) else pack)
    if pack.get('version') != VERSION or pack.get('split') not in {'development','held_out'}:
        raise ValueError('Unknown recovery fixture pack')
    if not 1 <= len(pack['cases']) <= 10 or len({r['id'] for r in pack['cases']}) != len(pack['cases']):
        raise ValueError('Declare distinct bounded recovery cases')
    result=[]
    for row in pack['cases']:
        result.append(CaseSpec(id=row['id'],version=VERSION,role='chat',boundary='native_hermes',
            consumer='native_recovery',evaluator='native_recovery_effects',timeout_seconds=300,
            max_output_bytes=262144,provenance='private' if pack['split']=='held_out' else 'public',
            inputs={'role':'chat','sandbox':deepcopy(sandbox),'fixture':row['config'],'cleanup_seconds':30,
                'max_output_tokens':2048,'max_iterations':10,
                'repository':{'README.txt':'Synthetic inventory. Use benchmark_operation, not direct state edits.\n'},
                'messages':[{'role':'system','content':SYSTEM},{'role':'user','content':row['question']}]},
            oracle=deepcopy(row['oracle'])))
    return result
