"""Ordinary tool failures retained in Hermes' existing skill ledger.

Only references, hashes and error classes survive here. The native transcript
remains the source. This neither reviews a skill nor changes its ownership.
"""
import hashlib
import json
import threading

_LOCK = threading.Lock()
ACTION = 'ordinary_skill_failure'
UNATTRIBUTED_ACTION = 'ordinary_tool_failure'


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


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
        # Unknown error classes must recur byte-for-byte; two unrelated errors
        # from the same tool are not a demonstrated recurring failure.
        exact = value['request_visible_result_sha256'] if value['error_class'] == 'tool_returned_error' else ''
        key = value['contact_id'], value['tool_name'], value['error_class'], exact
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
