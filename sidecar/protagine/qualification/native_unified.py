"""Qualification through the installed shared-work plugin and native gateway."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from .cases import _exact_value
from .native import native_cli


async def consume(inputs, context):
    attempts = context.state_dir.parent.parent
    if attempts.name != 'attempts':
        raise ValueError('Native unified work requires an owned qualification attempt')
    attempts.chmod(0o700)
    (context.state_dir/'memory-state').mkdir(mode=0o700)
    try:
        worker = 'native_unified_base_worker.py' if inputs.get('arm') == 'base_hermes' else 'native_unified_worker.py'
        result = await native_cli(deepcopy(inputs), context, worker=Path(__file__).with_name(worker))
        requests = result['effects'].get('request_observations', [])
        returned = {model for row in requests for model in row.get('returned_models', [])}
        if requests and len(returned) == 1 and all(row.get('selected_binding') == context.router.binding
                and row.get('returned_models') and not row.get('response_identity_truncated')
                and 200 <= row.get('status', 0) < 300 for row in requests):
            native = [row for row in context.observations if row.get('boundary') == 'native_cli_loop']
            if len(native) == 1 and native[0].get('outcome') == 'returned':
                native[0].update(selected_binding=context.router.binding, returned_model=next(iter(returned)),
                    prior_attempts=[], attribution_basis='serialized_request_and_returned_model',
                    observed_weight_revision=None)
        return result
    finally:
        try:
            closed = json.loads((context.state_dir/'unified-cleanup.json').read_text())
        except (ValueError, OSError):
            closed = {}
        context.state_cleanup_safe = context.state_cleanup_safe and closed.get('gateway_stopped') is True


def assess(observed, oracle):
    try:
        output = json.loads(observed.get('output', ''))
    except (ValueError, TypeError):
        output = None
    effect = observed.get('effects', {})
    if oracle.get('arm') == 'base_hermes':
        return {'grounded_unknown_answer': _exact_value(output, oracle['answer']),
            'different_native_sessions': effect.get('distinct_sessions') is True,
            'foreground_did_not_wait': effect.get('foreground_before_release') is True,
            'worker_completed': effect.get('worker_completed') is True,
            'worker_closed': effect.get('worker_closed') is True,
            'base_boundary_explicit': effect.get('shared_task_context_available') is False}
    requests = effect.get('request_observations', [])
    foreground = effect.get('foreground_requests', [])
    durable = effect.get('durable_after', [])
    checks = {'grounded_foreground_answer': _exact_value(output, oracle['answer']),
        'real_native_gateway_connected': effect.get('gateway_connected') is True,
        'different_native_sessions': effect.get('distinct_sessions') is True,
        'one_durable_native_task': len(durable) == 1,
        'foreground_finished_while_worker_waited': effect.get('foreground_before_release') is True,
        'shared_task_in_actual_request': bool(foreground) and any(
            row.get('task_id', '') and row['task_id'] in '\n'.join(foreground) for row in effect.get('durable_before', [])),
        'physical_request_evidence': bool(requests) and all(row.get('truncated') is False for row in requests),
        'commitment_still_pending': effect.get('commitment_status') == 'pending'}
    if oracle['scenario'] == 'shared':
        checks['shared_commitment_in_actual_requests'] = bool(effect.get('commitment_id')) and all(
            any(effect['commitment_id'] in text for text in group)
            for group in (foreground, effect.get('bootstrap_requests', [])))
    if oracle['scenario'] == 'steer':
        result = effect.get('worker_result')
        checks.update(registered_update=bool(effect.get('updates')),
            correction_visible_to_worker=any(oracle['revised_label'] in text for text in effect.get('worker_requests', [])),
            corrected_worker_result=_exact_value(result, {'label': oracle['revised_label'], 'units': oracle['worker_units']}))
    if oracle['scenario'] == 'stop':
        checks.update(stop_durably_finalized=effect.get('stop_status') == 'cancelled',
            no_retained_completion=effect.get('worker_response_absent') is True,
            duplicate_dispatch_did_not_start_model=effect.get('duplicate_suppressed') is True)
    return checks


CONSUMERS = {'native_unified': consume}
EVALUATORS = {'native_unified_outcomes': assess}


def implementation_identity():
    return {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ('native_unified.py', 'native_unified_cases.py', 'native_unified_worker.py',
                'native_unified_base_worker.py', 'native_unified_host.py', 'native_memory_worker.py')}
