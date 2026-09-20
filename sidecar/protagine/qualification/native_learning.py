"""Ordinary native feedback capture and procedural transfer, without weight training."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path

from .cases import _exact_value
from .native import native_cli
from .native_memory import MemoryRouter
from .records import write_once


class LearningRouter(MemoryRouter):
    def __init__(self, router, native, support_config):
        super().__init__(router, native)
        self.support_config = support_config


async def consume(inputs, context):
    attempts = context.state_dir.parent.parent
    if attempts.name != 'attempts':
        raise ValueError('Native learning requires an owned qualification directory')
    attempts.chmod(0o700)
    from .native_memory_batch import preflight_output
    preflight_output(context.state_dir)
    write_once(context.state_dir / 'support-config.json', context.router.support_config)
    async with asyncio.timeout(inputs['native_seconds']):
        result = await native_cli(deepcopy(inputs), context,
            worker=Path(__file__).with_name('native_learning_worker.py'))
    effects = result['effects']
    for row in effects.get('supporting_observations', []):
        context.observe(row)
    requests = [request for phase in effects.get('phases', {}).values()
                for request in phase.get('request_observations', [])]
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


def parsed(value):
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return None


def assess(observed, oracle):
    effects = observed.get('effects', {})
    phases = effects.get('phases', {})
    baseline, final = phases.get('baseline', {}), phases.get('transfer', {})
    requests = final.get('request_observations', [])
    wire = '\n'.join(row.get('text', '') for row in requests)
    claims = effects.get('claims', [])
    feedback_ids = effects.get('feedback_source_ids', [])
    arm = oracle['arm']
    expected = oracle['answer'] if arm == 'protagine' else oracle['unknown_answer']
    checks = {'baseline_preserves_unknown_policy': _exact_value(parsed(baseline.get('output')), oracle['baseline_answer']),
        'transfer_answer': _exact_value(parsed(observed.get('output')), expected),
        'fresh_transfer_session': bool(final.get('session_id')) and final.get('session_id') != baseline.get('session_id'),
        'physical_transfer_request_observed': bool(requests) and all(row.get('truncated') is False
            and row.get('boundary') == 'httpx_serialized_provider_request' for row in requests),
        'native_cleanup_confirmed': all(effects.get(key) is True for key in
            ('process_exited', 'worker_stopped', 'agent_close_returned', 'training_closed'))}
    if arm != 'base_hermes':
        checks['ordinary_native_feedback_retained'] = bool(feedback_ids) and any(
            row.get('path', '').startswith('/v2/host/turns/') and row.get('status') in {200, 201}
            for row in effects.get('context_routes', []))
        checks['ordinary_projection_completed'] = bool(effects.get('projection_jobs')) and all(
            row.get('status') == 'complete' for row in effects.get('projection_jobs', []))
        checks['useful_procedure_formed'] = any(row.get('source_id') in feedback_ids
            and row.get('memory_quality', {}).get('memory_kind') == 'procedure'
            and all(term.casefold() in row.get('value', '').casefold() for term in oracle['lesson_terms'])
            for row in claims)
    if arm == 'protagine':
        checks['feedback_recollected_on_new_variant'] = any('turn:' + source in wire for source in feedback_ids) if requests else None
    else:
        checks['lesson_not_injected_into_control'] = not any(term.casefold() in wire.casefold()
            for term in oracle['lesson_terms'][1:]) if requests else None
    return checks


def transfer_metrics(observed, oracle):
    """Goal completion is separate from an arm correctly abstaining without memory."""
    effects = observed.get('effects', {})
    baseline = effects.get('phases', {}).get('baseline', {})
    return {'scenario': effects.get('scenario'), 'arm': oracle['arm'],
        'baseline_goal_completed': _exact_value(parsed(baseline.get('output')), oracle['baseline_goal_answer']),
        'transfer_goal_completed': _exact_value(parsed(observed.get('output')), oracle['answer']),
        'retention_verified': assess(observed, oracle).get('useful_procedure_formed'),
        'baseline_elapsed_ms': baseline.get('elapsed_ms'),
        'transfer_elapsed_ms': effects.get('phases', {}).get('transfer', {}).get('elapsed_ms'),
        'formation_elapsed_ms': effects.get('formation_elapsed_ms'),
        'scope': 'Four development scenarios; procedural feedback transfer, not recursive self-improvement.'}


CONSUMERS = {'native_learning': consume}
EVALUATORS = {'native_learning_outcomes': assess}
