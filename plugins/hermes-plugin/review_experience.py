"""Ordinary tool failures retained in Hermes' existing skill ledger.

Only references, hashes and error classes survive here. The native transcript
remains the source. This neither reviews a skill nor changes its ownership.
"""
from contextlib import closing
import hashlib
import json
import threading
import sqlite3
from types import SimpleNamespace

_LOCK = threading.Lock()
ACTION = 'ordinary_skill_failure'
UNATTRIBUTED_ACTION = 'ordinary_tool_failure'
TERMINAL_EXIT = 'terminal_nonzero_exit'


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def terminal_cancelled(result):
    return (result.get('status') in ('interrupted', 'cancelled', 'canceled')
            or result.get('exit_code') in (130, 137, 143))


def terminal_exit_failure(result):
    """A completed native process outcome, independent of printed prose."""
    return (isinstance(result, dict) and not terminal_cancelled(result)
            and type(result.get('exit_code')) is int and 0 < result['exit_code'] <= 255
            and isinstance(result.get('output'), str))


def failure_group(value):
    # Unknown failures and process exits must match their original bytes.
    # Different commands with the same exit status/output are not recurrence.
    classification = value['error_class']
    exact = value['request_visible_result_sha256'] if classification in {
        'tool_returned_error', TERMINAL_EXIT} else ''
    arguments = value['arguments_sha256'] if classification == TERMINAL_EXIT else ''
    return value['contact_id'], value['tool_name'], classification, exact, arguments


def observations(scope, messages, failures):
    # System/qualification turns are not ordinary owner experience. A task
    # inherits its real sender through the existing native task association.
    if (scope.authority_lane != 'owner' or scope.resolution_status != 'resolved'
            or not scope.contact_id or scope.platform in {'cli', 'cron', 'subagent', 'background_review'}):
        return []
    current_user = max((i for i, m in enumerate(messages) if isinstance(m, dict)
                        and m.get('role') == 'user'), default=-1)
    calls, viewed, result = {}, {}, []
    by_call = {row['tool_call_id']: row for row in failures}
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        for call in message.get('tool_calls') or []:
            if isinstance(call, dict) and isinstance(call.get('function'), dict):
                calls[call.get('id')] = call['function']
        if message.get('type') == 'function_call':
            calls[message.get('call_id')] = message
        call_id = message.get('tool_call_id') if message.get('role') == 'tool' else message.get('call_id') if message.get('type') == 'function_call_output' else None
        raw = message.get('content') if message.get('role') == 'tool' else message.get('output')
        call = calls.get(call_id, {})
        if not isinstance(raw, str):
            continue
        try:
            body = json.loads(raw)
            args = call.get('arguments') or {}
            args = json.loads(args) if isinstance(args, str) else args
        except (ValueError, TypeError):
            continue
        if not isinstance(body, dict) or not isinstance(args, dict):
            continue
        if (call.get('name') == 'skill_view' and body.get('success') is True
                and isinstance(args.get('name'), str) and body.get('name') == args['name']
                and isinstance(body.get('content'), str) and body['content']
                and not args.get('file_path')):
            # A skill used earlier in the same conversation can still govern
            # this turn. Retain the exact content observed, never a live-file guess.
            viewed[args['name']] = {'skill_call_id': call_id,
                'skill_sha256': hashlib.sha256(body['content'].encode()).hexdigest()}
        if index <= current_user or call_id not in by_call:
            continue
        error_class = by_call[call_id]['error_class']
        error = body.get('error')
        if isinstance(error, str):
            if "AttributeError: 'list' object has no attribute 'get'" in error:
                error_class = 'python_list_get_attribute_error'
            elif error.startswith('Cell timed out after ') and 'session kernel was killed' in error:
                error_class = 'kernel_timeout_state_lost'
        for name, view in (list(viewed.items())[-8:] or [(None, {})]):
            evidence = {'version': 1, 'source': 'ordinary_native_tool_failure',
                'contact_id': scope.contact_id, 'platform': scope.platform,
                'session_id': scope.session_id, 'turn_id': scope.turn_id,
                **view, **by_call[call_id], 'error_class': error_class}
            # The native result is one occurrence even if history is replayed
            # by another turn or the process restarts before its next request.
            keys = ('contact_id', 'session_id', 'tool_call_id', 'request_visible_result_sha256')
            if name is None:
                evidence['attribution'] = 'unattributed'
                # Some providers reuse call IDs on later turns. The actual
                # native turn distinguishes those occurrences from replay.
                keys += ('turn_id',)
            else:
                keys += ('skill_call_id', 'skill_sha256')
            if error_class == TERMINAL_EXIT:
                keys += ('turn_id', 'arguments_sha256')
            evidence['observation_id'] = fingerprint({key: evidence[key] for key in keys})
            result.append((name, evidence))
    return result


def retain(scope, messages, failures):
    from tools import skill_ledger
    rows = observations(scope, messages, failures)
    if not rows:
        return
    with _LOCK:
        known = {row.get('evidence', {}).get('observation_id')
                 for row in skill_ledger.list_entries()
                 if row.get('action') in {ACTION, UNATTRIBUTED_ACTION}}
        for skill, evidence in rows:
            if evidence['observation_id'] not in known:
                action = ACTION if skill is not None else UNATTRIBUTED_ACTION
                recorded = skill_ledger.append_entry(action, skill, actor='agent', evidence=evidence)
                if recorded:
                    known.add(evidence['observation_id'])


def next_batch(entries, skill=None, skill_sha256=None):
    """Two actual turns with the same tool failure justify one bounded review.

    The existing periodic consumer provides cadence. Receipts make selection
    survive restarts, with no per-session iteration counter or second queue.
    """
    unattributed = skill is None and skill_sha256 is None
    if (skill is None) != (skill_sha256 is None):
        raise ValueError('Select a skill and its observed content hash, or neither')
    action = UNATTRIBUTED_ACTION if unattributed else ACTION
    consumed = {identifier for row in entries if row.get('action') == 'ordinary_skill_review'
                for identifier in row.get('evidence', {}).get('observation_ids', [])}
    groups = {}
    for row in reversed(entries):  # Hermes returns newest entries first.
        value = row.get('evidence', {})
        if (row.get('action') != action or row.get('skill') != skill
                or value.get('skill_sha256') != skill_sha256
                or (unattributed and (value.get('attribution') != 'unattributed'
                                      or 'skill_call_id' in value or 'skill_sha256' in value))
                or value.get('observation_id') in consumed):
            continue
        key = failure_group(value)
        group = groups.setdefault(key, {})
        group[value['observation_id']] = value
    for values in groups.values():
        turns = {}
        for value in values.values():
            turns.setdefault((value['session_id'], value['turn_id']), value)
        selected = list(turns.values())[:16]
        if len(selected) < 2:
            continue
        selected_turns = {(row['session_id'], row['turn_id']) for row in selected}
        ids = sorted(row['observation_id'] for row in values.values()
                     if (row['session_id'], row['turn_id']) in selected_turns)
        return {'version': 1, 'source': 'ordinary_native_failure_batch', 'skill': skill,
                **({'attribution': 'unattributed'} if unattributed else {}),
                'skill_sha256': skill_sha256, 'observation_ids': ids,
                'failure_sha256': fingerprint(ids), 'observations': selected}
    return None


def next_tool_batch(entries):
    """Group recurring tool outcomes independently of previously viewed skills.

    A skill view is context, not evidence that it caused a later tool failure.
    Keep those references for diagnosis without choosing a skill to change.
    Existing ledger observations and claims remain the only durable records.
    """
    rows = [row for row in reversed(entries)
            if row.get('action') in {ACTION, UNATTRIBUTED_ACTION}]
    consumed = {identifier for row in entries if row.get('action') == 'ordinary_skill_review'
                for identifier in row.get('evidence', {}).get('observation_ids', [])}

    def occurrence(value):
        return tuple(value[key] for key in ('contact_id', 'session_id', 'turn_id',
                     'tool_call_id', 'request_visible_result_sha256'))

    # Several skill views can reference one execution. A prior claim of any
    # reference consumes that execution, including another view recorded later.
    consumed_calls = {occurrence(row['evidence']) for row in rows
                      if row['evidence'].get('observation_id') in consumed}
    groups = {}
    for row in rows:
        value = row['evidence']
        identity = occurrence(value)
        if identity in consumed_calls:
            continue
        key = failure_group(value)
        values = groups.setdefault(key, {})
        if identity not in values:
            observation = {k: v for k, v in value.items()
                           if k not in {'skill_call_id', 'skill_sha256', 'attribution'}}
            values[identity] = {'observation': {**observation, 'skill_views': []}, 'ids': set()}
        current = values[identity]
        current['ids'].add(value['observation_id'])
        if row.get('skill') is not None:
            view = {'skill': row['skill'], 'skill_call_id': value['skill_call_id'],
                    'skill_sha256': value['skill_sha256']}
            if view not in current['observation']['skill_views']:
                current['observation']['skill_views'].append(view)
    for values in groups.values():
        turns = {}
        for value in values.values():
            observation = value['observation']
            turns.setdefault((observation['session_id'], observation['turn_id']), observation)
        selected = list(turns.values())[:16]
        if len(selected) < 2:
            continue
        selected_turns = {(row['session_id'], row['turn_id']) for row in selected}
        identifiers = sorted({identifier for value in values.values()
            if (value['observation']['session_id'], value['observation']['turn_id']) in selected_turns
            for identifier in value['ids']})
        return {'version': 1, 'source': 'ordinary_native_failure_batch', 'skill': None,
                'attribution': 'unassigned', 'skill_sha256': None,
                'observation_ids': identifiers, 'failure_sha256': fingerprint(identifiers),
                'observations': selected}
    return None


def selected_pairs(evidence, native_home, owner):
    observations = evidence.get('observations', [])
    if (not owner or not 2 <= len(observations) <= 16
            or any(row.get('contact_id') != owner for row in observations)):
        raise ValueError('Current configured owner must own every review occurrence')
    pairs = []
    with closing(sqlite3.connect((native_home/'state.db').as_uri()+'?mode=ro', uri=True, timeout=.25)) as db:
        db.row_factory = sqlite3.Row
        for observation in observations:
            session, call_id = observation['session_id'], observation['tool_call_id']
            rows = db.execute("SELECT id,content,tool_name FROM messages WHERE session_id=? "
                "AND role='tool' AND tool_call_id=? ORDER BY id LIMIT 17", (session, call_id)).fetchall()
            matched = [row for row in rows if isinstance(row['content'], str)
                and hashlib.sha256(row['content'].encode()).hexdigest() == observation['request_visible_result_sha256']]
            if len(rows) > 16 or len(matched) != 1 or matched[0]['tool_name'] != observation['tool_name']:
                raise ValueError('Original native tool result is unavailable or ambiguous')
            row = matched[0]
            start = db.execute("SELECT MAX(id) FROM messages WHERE session_id=? AND role='user' AND id<?",
                               (session, row['id'])).fetchone()[0]
            if start is None:
                raise ValueError('Original native turn is unavailable')
            calls = []
            candidates = db.execute("SELECT id,tool_calls FROM messages WHERE session_id=? "
                "AND id>? AND id<? AND role='assistant' AND tool_calls IS NOT NULL "
                "ORDER BY id LIMIT 257", (session, start, row['id'])).fetchall()
            if len(candidates) > 256:
                raise ValueError('Original native call window is too large')
            for candidate in candidates:
                for call in json.loads(candidate['tool_calls']):
                    if call.get('id') == call_id and call.get('function', {}).get('name') == observation['tool_name']:
                        calls.append((candidate['id'], call['function']))
            if len(calls) != 1:
                raise ValueError('Original native tool call is unavailable or ambiguous')
            try:
                result = json.loads(row['content'])
            except ValueError:
                # Classification and bytes were attested by the original
                # producer and validated against the claimed ledger batch.
                # Native exception results need not use a JSON envelope.
                if not observation.get('error_class'):
                    raise ValueError('Native error classification is unavailable') from None
                error = row['content']
            else:
                if observation.get('error_class') == TERMINAL_EXIT:
                    arguments = calls[0][1].get('arguments', '{}')
                    arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
                    if (observation['tool_name'] != 'terminal' or not terminal_exit_failure(result)
                            or not isinstance(arguments, dict)
                            or fingerprint(arguments) != observation.get('arguments_sha256')):
                        raise ValueError('Original native process outcome or command no longer matches')
                    error = {'exit_code': result['exit_code'], 'output': result['output']}
                elif not isinstance(result, dict) or not result.get('error'):
                    raise ValueError('Original native result does not contain the retained failure')
                else:
                    error = result['error']
            pairs.append({'observation_id': observation['observation_id'],
                'session_id': session, 'turn_id': observation['turn_id'], 'tool_call_id': call_id,
                'tool_name': observation['tool_name'], 'native_message_id': row['id'],
                'native_call_message_id': calls[0][0], 'native_user_message_id': start,
                'request_visible_result_sha256': observation['request_visible_result_sha256'],
                'arguments': calls[0][1].get('arguments', '{}'), 'error': error})
    return pairs


def diagnostic_context(evidence, native_home, config, *, memory=None, connection=None):
    from agent.redact import redact_sensitive_text
    from protagine_hermes.client import ProtagineClient, TurnOutbox, turn_outbox_path
    from protagine_hermes.native_history import reconcile
    from hermes_state import _default_db_path

    if _default_db_path().resolve() != (native_home/'state.db').resolve():
        raise ValueError('Native history reader must use the selected runtime home')
    owner = str(config.get('owner_contact_id') or '').strip()
    pairs = selected_pairs(evidence, native_home, owner)
    # Resolve the exact selected result rows through the existing history read
    # path. It checks each originating native turn's current source ancestry.
    payload = {'success': True, 'mode': 'read', 'messages': [
        {'id': pair['native_message_id'], 'content': json.dumps(pair)} for pair in pairs]}
    lineage = []
    def retain_lineage(scope, call_id, text, source):
        lineage.append(source)
        return True
    if memory is None:
        memory = SimpleNamespace(client=connection or ProtagineClient(url=config.get('url'), api_key=config.get('api_key')),
            outbox=TurnOutbox(turn_outbox_path(config)), register_source_read=retain_lineage)
    scope = SimpleNamespace(contact_id=owner, session_id=pairs[0]['session_id'])
    checked = json.loads(reconcile({}, json.dumps(payload), scope,
        {'tool_call_id': 'ordinary-review:'+evidence['failure_sha256']}, memory))
    messages = checked.get('messages', [])
    if (checked.get('success') is not True or not checked.get('protagine_native_history_read_v1')
            or {row['id'] for row in messages} != {pair['native_message_id'] for pair in pairs}):
        raise ValueError('Current source erasure state excludes the original failure context')
    for pair in pairs:
        for field in ('arguments', 'error'):
            text = pair[field] if isinstance(pair[field], str) else json.dumps(pair[field])
            text = redact_sensitive_text(text, force=True)
            pair[field] = text[:4096]
            if len(text) > 4096:
                pair[field+'_truncated'] = True
    return {'source': 'current_native_tool_history', 'occurrences': pairs,
            'memory_erasure': checked['memory_erasure'],
            'source_refs': lineage[0]['source_refs'] if lineage else []}
