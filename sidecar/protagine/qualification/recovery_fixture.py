"""Small deterministic inventory fixture, executed only in an owned container.

The model cannot read or write this helper or its ledger. The trusted native
tool handler supplies JSON operations; no generated code runs as container root.
"""
import json
from pathlib import Path
import sys


def apply(state, operation, payload):
    config = state['config']
    events, orders = state['events'], state['orders']
    event = {'operation': operation, 'payload': payload, 'applied': False}
    events.append(event)
    writes = [row for row in events if row['operation'] == 'reserve']
    key = payload.get('request_id')
    if operation == 'lookup':
        lookups = sum(row['operation'] == 'lookup' for row in events)
        if config.get('malformed_lookup_once') and lookups == 1:
            result = {'error': 'malformed_fixture_reply'}
            event['status'] = 'malformed'
            return 'not valid JSON { incomplete'
        result = {'item': config['item'], 'available': state['available'], 'revision': config.get('revision', 1)}
    elif operation == 'reserve':
        if 'units' in payload:
            result = {'error': 'invalid_field', 'message': 'Use quantity instead of units. No reservation occurred.'}
        elif payload.get('item') != config['item'] or type(payload.get('quantity')) is not int or payload.get('quantity', 0) < 1 or not isinstance(key, str) or not 1 <= len(key) <= 80:
            result = {'error': 'invalid_arguments', 'message': 'Supply item, positive integer quantity and request_id.'}
        elif key in orders:
            result = dict(orders[key])
        elif len(writes) <= config.get('unavailable_attempts', 0):
            result = {'error': 'temporarily_unavailable', 'effect_performed': False}
        elif payload.get('revision', 1) != config.get('revision', 1):
            result = {'error': 'stale_revision', 'current_revision': config['revision'], 'effect_performed': False}
        elif payload['quantity'] > state['available']:
            result = {'error': 'insufficient_stock', 'available': state['available'], 'effect_performed': False}
        else:
            queued = config.get('queued', False)
            orders[key] = {'request_id': key, 'quantity': payload['quantity'], 'status': 'queued' if queued else 'reserved'}
            if not queued:
                state['available'] -= payload['quantity']
                event['applied'] = True
            result = dict(orders[key])
            if config.get('lost_ack_once'):
                result = {'error': 'acknowledgment_lost', 'outcome': 'unknown',
                          'message': 'Check status with the same request_id before issuing another reservation.'}
    elif operation == 'status':
        if key not in orders:
            result = {'request_id': key, 'status': 'unknown', 'quantity': 0}
        else:
            order = orders[key]
            polls = sum(row['operation'] == 'status' and row['payload'].get('request_id') == key for row in events)
            if order['status'] == 'queued' and polls >= config.get('complete_after_polls', 999):
                order['status'] = 'reserved'
                state['available'] -= order['quantity']
                event['applied'] = True
            result = dict(order)
    elif operation == 'cancel':
        order = orders.get(key)
        if order and order['status'] == 'queued':
            order['status'] = 'cancelled'
            result = dict(order)
        else:
            result = {'error': 'not_queued', 'effect_performed': False}
    else:
        result = {'error': 'unknown_operation', 'effect_performed': False}
    event['status'] = result.get('error', result.get('status', 'read'))
    return result


def main():
    root = Path(sys.argv[1])
    command = json.loads(sys.stdin.read())
    state_path = root/'ledger.json'
    state = json.loads(state_path.read_text())
    if command['operation'] == '__snapshot':
        print(json.dumps(state)); return
    result = apply(state, command['operation'], command.get('payload', {}))
    temporary = root/'ledger.next'
    temporary.write_text(json.dumps(state))
    temporary.replace(state_path)
    print(json.dumps({'reply': result}))


if __name__ == '__main__':
    main()
