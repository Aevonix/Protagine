"""Effect-based grading for distinct native task transitions."""
import hashlib
import json
from pathlib import Path
from .cases import _exact_value
from .native_unified import consume as unified_consume


async def consume(inputs, context):
    return await unified_consume(inputs, context, worker=Path(__file__).with_name('native_interactive_worker.py'))


def decoded(text):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def assess(observed, oracle):
    effects = observed.get('effects', {})
    evidence = effects.get('interactive', {})
    before = {row['name']: row for row in evidence.get('before', [])}
    after = {row['name']: row for row in evidence.get('after', [])}
    turns = evidence.get('conversations', [])
    foreground = [row for row in turns if row['role'] == 'foreground']
    workers = [row for row in turns if row['role'] == 'worker']
    bootstrap = [row for row in turns if row['role'] == 'bootstrap']
    scenario = oracle['scenario']
    checks = {'native_turn_completed': effects.get('native_turn_complete') is True,
        'grounded_foreground_answer': _exact_value(decoded(observed.get('output')), oracle['answer']),
        'real_gateway_connected': evidence.get('gateway_connected') is True,
        'exact_durable_task_count': len(evidence.get('after', [])) == len(oracle['task_names'])
            and set(after) == set(oracle['task_names']),
        'foreground_conversation_observed': bool(foreground),
        'background_acceptance_observed': len(bootstrap) == len(oracle['task_names'])
            and all(row['completed'] for row in bootstrap),
        'private_provider_evidence_complete': bool(effects.get('request_observations'))
            and all(row.get('truncated') is False for row in effects.get('request_observations', [])),
        'canonical_owner_preserved': bool(after) and all(row['owner_matches'] for row in after.values()),
        'no_replacement_admissions': set(before) == set(after)
            and all(before[name]['id'] == after[name]['id'] for name in before),
        'original_task_sources_preserved': bool(before)
            and all(before[name]['source_sha256'] == after.get(name, {}).get('source_sha256') for name in before)}
    for name, answer in oracle['results'].items():
        response = after.get(name, {}).get('response')
        checks['correct_retained_result.'+name] = bool(response) and not response['truncated'] and _exact_value(decoded(response['text']), answer)
    for name in oracle['stopped']:
        record = after.get(name, {})
        checks['cancelled_without_retained_response.'+name] = (record.get('stop') or {}).get('status') == 'cancelled' and not record.get('response')
    for name, count in oracle['updates'].items():
        checks['exact_retained_updates.'+name] = len(after.get(name, {}).get('updates', [])) == count
    if scenario in {'short_question', 'two_roots', 'cancel_sibling', 'targeted_steer', 'ordered_steer',
                    'terminal_handoff', 'channel_steer', 'existing_handoff', 'duplicate_submit'}:
        checks['foreground_completed_before_worker_release'] = evidence.get('foreground_before_release') is True
    if scenario in {'two_roots', 'cancel_sibling', 'short_question', 'targeted_steer'}:
        checks['independent_native_sessions'] = len({row['native_session_id'] for row in after.values()}) == 2
        checks['independent_source_sessions'] = len({row['source_session_id'] for row in after.values()}) == 2
    if scenario == 'failed_resume':
        checks['real_worker_failure_observed'] = any(row['kind'] == 'declared_read_timeout' for row in evidence.get('injected_failures', []))
        checks['failed_terminal_before_resume'] = bool(before) and all((row.get('terminal') or {}).get('failed') is True for row in before.values())
        checks['same_native_session_resumed'] = bool(before) and all(
            before[name]['native_session_id'] == after.get(name, {}).get('native_session_id')
            and before[name]['native_turn_id'] != after.get(name, {}).get('native_turn_id') for name in before)
    if scenario in {'stale_resume', 'completed_resume'}:
        checks['no_extra_native_generation'] = bool(before) and all(
            before[name]['native_turn_id'] == after.get(name, {}).get('native_turn_id') for name in before)
        checks['resume_rejection_receipt_observed'] = any('"resume_requested": false' in item['text']
            for row in foreground for item in row['tool_results'])
    if scenario == 'source_withdrawal':
        checks['withdrawn_source_blocks_response'] = bool(after) and all(
            row.get('request_erased') is True and not row['response'] for row in after.values())
    if scenario == 'gateway_restart':
        checks['owned_gateway_restarted'] = evidence.get('gateway_restart_performed') is True
    if scenario == 'worker_identity':
        checks['worker_identity_bound_to_owner'] = bool(workers) and all(row['scope']['owner_matches'] and row['scope']['valid'] for row in workers)
    if scenario == 'channel_steer':
        checks['verified_distinct_channels'] = (bool(bootstrap) and all(row['platform'] == 'sms' and row['scope']['owner_matches'] for row in bootstrap)
            and bool(foreground) and all(row['platform'] == 'whatsapp' and row['scope']['owner_matches'] for row in foreground))
        checks['update_has_actual_channel_provenance'] = any(update['source'].get('origin', {}).get('platform') == 'whatsapp'
            for row in after.values() for update in row['updates'])
    if scenario == 'queued_cancel':
        checks['cancelled_before_any_worker_request'] = not evidence.get('worker_entries')
    if scenario == 'queued_steer':
        checks['update_before_native_admission'] = bool(before) and all(row['native_session_id'] is None for row in before.values())
    if scenario == 'failed_retention':
        checks['publication_failure_exercised'] = any(row['kind'] == 'declared_write_failure' for row in evidence.get('injected_failures', []))
        checks['computation_finished_without_delivery_claim'] = bool(after) and all(
            (row.get('terminal') or {}).get('completed') is True and row['response'] is None for row in after.values())
    for marker in oracle['observed_markers'][-1:]:
        checks['latest_correction_in_worker_request'] = any(marker in row['markers'] for row in evidence.get('requests', []) if row['role'] == 'worker')
    return checks


def implementation_identity():
    return {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
        for name in ('native_interactive.py', 'native_interactive_cases.py', 'native_interactive_worker.py',
                     'native_unified.py', 'native_unified_host.py', 'native_memory_worker.py', 'native.py', 'native_worker.py')}


CONSUMERS = {'native_interactive': consume}
EVALUATORS = {'native_interactive_outcomes': assess}
