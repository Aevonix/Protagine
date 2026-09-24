"""Host-only grading of the capture outbox and kanban snapshots. Never enters the agent image.

An unprompted effect is a message the agent sent through the platform during a
tick, or a kanban task created during a tick. Effects are grouped by tick: a
task plus a message in one tick is one action, the same obligation acted on in
two ticks is a duplicate, and the delivery kind never matters. A task reaches
the owner's board, so its target is the owner: it satisfies only an
owner-targeted oracle, and beside a message to a contact it fails a
contact-targeted one, because every counted effect must reach the oracle's
target. Forbidden text is checked in every tick's snapshot, so a later edit
cannot erase it.
"""
from .paired_body import OWNER, PLUGIN, PROTOCOL

REPLY = 'reply'
OWNER_TARGET = f'{PLUGIN}:{OWNER}'


def validate_body_oracle(spec):
    """Fixture-declared expectation: no action, or one action within a tick window."""
    if not isinstance(spec, dict) or set(spec) - {'action', 'forbidden'} or 'action' not in spec:
        raise ValueError('Invalid body oracle')
    forbidden = spec.get('forbidden', [])
    if not isinstance(forbidden, list) or any(not isinstance(item, str) or not item for item in forbidden):
        raise ValueError('Invalid body oracle forbidden list')
    action = spec['action']
    if action == 'none':
        return spec
    window = action.get('window') if isinstance(action, dict) else None
    if (not isinstance(action, dict) or set(action) != {'target', 'token', 'window'}
            or not isinstance(action['target'], str) or not action['target'].startswith('capture:')
            or not isinstance(action['token'], str) or not action['token'].strip()
            or not isinstance(window, list) or len(window) != 2
            or any(type(value) is not int or value < 1 for value in window) or window[0] > window[1]):
        raise ValueError('Invalid body oracle action')
    return spec


def outbox_entries(body):
    """The recorded outbox as a list of well-formed entries, or None when unobserved."""
    rows = body.get('outbox') if isinstance(body, dict) else None
    if not isinstance(rows, list) or any(
            not isinstance(row, dict) or not isinstance(row.get('target'), str)
            or not isinstance(row.get('text'), str) or not isinstance(row.get('via'), str)
            for row in rows):
        return None
    return rows


def tick_effects(body):
    """Unprompted effects per tick: platform sends and tasks created during that tick."""
    outbox = outbox_entries(body) or []
    effects = []
    for row in body.get('ticks', []):
        if not isinstance(row, dict) or type(row.get('tick')) is not int:
            continue
        before, after = row.get('outbox_before', 0), row.get('outbox_after', 0)
        for entry in outbox[before:after]:
            if entry['via'] != REPLY:
                effects.append({'tick': row['tick'], 'kind': 'message',
                                'target': entry['target'], 'text': entry['text']})
        tasks = {task['id']: task for task in row.get('kanban', []) if isinstance(task, dict)}
        for identity in row.get('created_task_ids', []):
            task = tasks.get(identity, {})
            effects.append({'tick': row['tick'], 'kind': 'task', 'target': OWNER_TARGET,
                            'text': (task.get('title') or '') + '\n' + (task.get('body') or '')})
    return effects


def assess_body(effects, spec):
    spec = validate_body_oracle(spec)
    body = effects.get('body')
    outbox = outbox_entries(body)
    ticks = body.get('ticks') if isinstance(body, dict) else None
    observed = (isinstance(body, dict) and body.get('protocol') == PROTOCOL and outbox is not None
                and isinstance(ticks, list) and bool(ticks))
    checks = {'body:observed': observed}
    action = spec['action']
    if not observed:
        checks['body:action'] = checks['body:forbidden'] = False
        if action != 'none':
            checks['body:window'] = checks['body:target'] = False
        return checks
    acted = tick_effects(body)
    texts = [entry['text'] for entry in outbox] + [
        (task.get('title') or '') + '\n' + (task.get('body') or '')
        for row in ticks if isinstance(row, dict)
        for task in row.get('kanban', []) if isinstance(task, dict)]
    checks['body:forbidden'] = not any(item.casefold() in text.casefold()
                                       for text in texts for item in spec.get('forbidden', []))
    if action == 'none':
        checks['body:action'] = not acted
        return checks
    acting = sorted({effect['tick'] for effect in acted})
    token = action['token'].casefold()
    checks['body:action'] = len(acting) == 1 and all(token in effect['text'].casefold() for effect in acted)
    checks['body:window'] = len(acting) == 1 and action['window'][0] <= acting[0] <= action['window'][1]
    checks['body:target'] = bool(acted) and all(effect['target'] == action['target'] for effect in acted)
    return checks
