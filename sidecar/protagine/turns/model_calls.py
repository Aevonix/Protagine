"""Read retained model-call measurements without replaying or grading a turn."""
from contextlib import closing
import json


REQUEST_FIELDS = ('requested_model', 'provider', 'api_mode', 'runtime_kind',
    'profile_id', 'api_call_count', 'retry_count', 'approx_input_tokens',
    'tool_count', 'max_tokens', 'output_limit_kind', 'started_at')
RESPONSE_FIELDS = ('response_model', 'finish_reason', 'assistant_content_chars',
    'assistant_tool_call_count', 'assistant_reasoning_chars',
    'assistant_reasoning_content_chars', 'assistant_reasoning_details_count',
    'assistant_invalid_tool_name_count', 'api_duration', 'first_chunk_at',
    'started_at', 'ended_at')


def read_model_calls(registry, *, contact_id, execution_id, offset=0, limit=20):
    """Exact subject and the existing seven-day operational retention apply."""
    with closing(registry.ledger._connect()) as db:
        row = db.execute(
            'SELECT e.execution_id,e.state,e.platform,e.last_observed_at,r.metadata_json '
            'FROM execution_observations e JOIN execution_runtime_observations r '
            'USING(execution_id) WHERE e.execution_id=? AND e.contact_id=? '
            'AND e.last_observed_at>=?',
            (execution_id, contact_id, registry.clock() - 7 * 86400)).fetchone()
    if row is None:
        return None
    data = json.loads(row['metadata_json'])
    pairs = data.get('requests', {})
    calls = []
    complete = bool(pairs) and not data.get('incomplete', False)
    for request_id, pair in pairs.items():
        start, response, error = (pair.get(key) for key in ('start', 'response', 'error'))
        complete_pair = bool(start and (response is not None) != (error is not None))
        complete = complete and complete_pair
        call = {'request_id': request_id, 'start_observed': start is not None,
            'response_observed': response is not None, 'error_observed': error is not None,
            'complete_pair': complete_pair,
            'request': {key: (start or {}).get(key) for key in REQUEST_FIELDS},
            'response': {key: (response or {}).get(key) for key in RESPONSE_FIELDS}}
        measured = call['response']
        notes = []
        if error is not None:
            notes.append('request_error')
        if measured['assistant_content_chars'] == 0 and measured['assistant_tool_call_count'] == 0:
            notes.append('no_visible_text_or_tool_calls')
        if (measured['assistant_invalid_tool_name_count'] or 0) > 0:
            notes.append('invalid_tool_names')
        if measured['finish_reason'] == 'length':
            notes.append('output_length_stop')
        if not complete_pair:
            notes.append('incomplete_or_conflicting_callbacks')
        call['observations'] = notes
        calls.append(call)
    return {'execution_id': execution_id, 'state': row['state'], 'platform': row['platform'],
        'last_observed_at': row['last_observed_at'],
        'complete_observed_pairs': bool(complete), 'total_requests': len(calls),
        'offset': offset, 'more': offset + limit < len(calls), 'calls': calls[offset:offset + limit],
        'evidence_boundary': 'Normalized Hermes callbacks, not raw provider output. Missing values are unknown. '
            'Reasoning fields may overlap and must not be added. These measurements do not grade answer quality '
            'or distinguish provider generation from parser loss. Auxiliary or dropped callbacks may be absent.'}
