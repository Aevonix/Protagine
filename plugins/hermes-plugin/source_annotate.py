"""Append attributed correction evidence for an actually supplied source revision."""
import hashlib
import json
import re


def handle(args, scope, client, request_memory, context=None):
    if (scope is None or not scope.valid_participant
            or scope.authority_lane not in {'owner', 'system'}
            or not scope.task_id or not scope.turn_id):
        return json.dumps({'error': 'An attested owner or system turn is required'})
    keys = {'source_id', 'source_version', 'excerpt', 'correction'}
    if (not isinstance(args, dict) or set(args) != keys
            or any(not isinstance(args[k], str) or not args[k].strip() for k in keys)
            or len(args['source_id']) > 256
            or not re.fullmatch(r'[0-9a-f]{64}', args['source_version'])
            or any(len(args[k]) > 4096 for k in ('excerpt', 'correction'))):
        return json.dumps({'error': 'Supply only an exact source ID/version, excerpt and correction'})
    ref = {k: args[k] for k in ('source_id', 'source_version')}
    if ref not in (request_memory.supplied_snapshot(scope) or []):
        return json.dumps({'error': 'The exact source revision must have been supplied to this turn'})
    identity = json.dumps([scope.contact_id, scope.session_id, scope.turn_id, args],
                          sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    annotation_id = 'native-annotation:' + hashlib.sha256(identity.encode()).hexdigest()
    ownership = request_memory.ownership
    try:
        from .tool_observations import native_input, _arguments_hash
        _, original = native_input(scope, (context or {}).get('tool_call_id'), {
            'name':'pacomind_memory_annotate', 'arguments_sha256':_arguments_hash(args)})
        # Keep the first creating call before a possibly lost acknowledgement.
        # Retries in this turn reuse the request ID and its durable anchor.
        if ownership is None or not ownership.retain_origin(scope, annotation_id, messages=[original]):
            raise ValueError('Native annotation origin unavailable')
    except Exception:
        return json.dumps({'accepted': False, 'error':
            'The exact native annotation call is unavailable, shares a row, or exceeds the 16 KiB retention limit; no annotation was submitted'})
    try:
        response = client.post('/v1/host/memory/sources/annotations', timeout=3,
            json={**args, 'annotation_id': annotation_id, 'contact_id': scope.contact_id,
                  'session_id': scope.session_id})
        if response.status_code in {403, 409, 422}:
            steps = {
                'source_excerpt_mismatch': 'Read the source and copy an exact contiguous excerpt; do not join separate passages.',
                'source_version_mismatch': 'Read the current source revision before submitting a revised annotation.',
                'source_not_found': 'Search the current scoped evidence; this source is unavailable.',
                'source_erased': 'This source was forgotten. Do not recreate it from the rejected annotation.',
                'annotation_id_conflict': 'Inspect the existing annotation before proposing a different correction.',
                'source_annotation_not_authorized': 'This caller cannot annotate the source.',
                'invalid_source_annotation': 'Check the annotation arguments against the tool schema.',
            }
            try:
                reason = response.json()['detail']['code']
                next_step = steps[reason]
            except (ValueError, KeyError, TypeError):
                reason = 'source_annotation_rejected'
                next_step = 'Inspect current scoped evidence before changing the request.'
            return json.dumps({'error': 'The source annotation was rejected; inspect current scoped evidence',
                               'accepted': False, 'status_code': response.status_code,
                               'reason': reason, 'retry_identical': False, 'next_step': next_step})
        response.raise_for_status()
        receipt = response.json()
        if (not isinstance(receipt, dict) or receipt.get('target') != ref
                or type(receipt.get('created')) is not bool
                or not isinstance(receipt.get('source_version'), str)
                or not re.fullmatch(r'[0-9a-f]{64}', receipt['source_version'])
                or not ownership.bind_annotation_origin(scope, annotation_id, receipt)):
            raise ValueError('Native annotation receipt binding unavailable')
        return json.dumps(receipt)
    except Exception:
        # The server may have committed before the acknowledgement was lost.
        # Retrying the identical arguments in this turn reuses the same ID.
        return json.dumps({'error': 'Source annotation is unconfirmed; retry identical arguments in this turn',
                           'annotation_id': annotation_id, 'confirmation': 'unknown'})
