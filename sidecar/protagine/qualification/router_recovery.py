"""Real function-router failure handling with independently observed faults."""
import asyncio
from contextlib import AsyncExitStack
from copy import deepcopy
import hashlib
from pathlib import Path
import time

from protagine.router import LLMRouter
from .records import CaseSpec, digest, read
from .router_recovery_proxy import fault_proxy
from .runner import router_failure

VERSION = 'function-router-recovery-1'
DEVELOPMENT = {'version': VERSION, 'split': 'development', 'scenarios': [
    {'id': 'service-error-fallback', 'fault': 'service-error', 'marker': 'ochre'},
    {'id': 'elapsed-cooldown-recovery', 'fault': 'recover', 'marker': 'violet'},
]}


def cases(pack=None):
    pack = deepcopy(DEVELOPMENT if pack is None else read(pack) if not isinstance(pack, dict) else pack)
    if pack.get('version') != VERSION or pack.get('split') not in {'development', 'held_out'}:
        raise ValueError('Unknown router-recovery pack')
    rows = pack.get('scenarios', [])
    if not 1 <= len(rows) <= 5 or len({row['id'] for row in rows}) != len(rows):
        raise ValueError('Declare bounded distinct recovery scenarios')
    result = []
    for row in rows:
        if row['fault'] not in {'service-error', 'recover', 'timeout', 'cooldown', 'both-unavailable'}:
            raise ValueError('Unsupported fault scenario')
        if not isinstance(row['marker'], str) or not row['marker'].isalpha() or len(row['marker']) > 24:
            raise ValueError('Marker must be a short word')
        result.append(CaseSpec(id='router.recovery.'+row['id'], version='1', role='chat',
            boundary='role_completion', consumer='router_recovery', evaluator='router_recovery_effects',
            timeout_seconds=360, max_output_bytes=262144,
            provenance='private' if pack['split'] == 'held_out' else 'public',
            inputs={**row, 'attempt_seconds': 90, 'deadline_seconds': 190,
                'timeout_attempt_seconds': 25, 'max_output_tokens': 512,
                'messages': [{'role': 'system', 'content': 'Return the requested word only.'},
                    {'role': 'user', 'content': 'Return the word '+row['marker']+'.'}]},
            oracle={'fault': row['fault'], 'marker': row['marker']}))
    return result


class RecoveryRouter:
    """Delegate completions to the existing router, retaining private config."""
    def __init__(self, config, binding, fallback_binding):
        self.config, self.binding, self.fallback_binding = deepcopy(config), binding, fallback_binding
        self.actual = LLMRouter(tiers={})
        self.actual.configure(self.config)
        snapshot = self.actual._snapshot
        if binding == fallback_binding or any(name not in snapshot.bindings for name in (binding, fallback_binding)):
            raise ValueError('Choose distinct existing primary and supporting fallback bindings')
        first, second = (snapshot.bindings[name].config for name in (binding, fallback_binding))
        if (first.base_url, first.model_id) == (second.base_url, second.model_id):
            raise ValueError('Fallback must have an independent endpoint or model identity')

    def __getattr__(self, name):
        return getattr(self.actual, name)


async def consume(inputs, context):
    cfg, binding, fallback = deepcopy(context.router.config), context.router.binding, context.router.fallback_binding
    snapshot, actual = context.router._snapshot, context.router.actual
    fault = inputs['fault']
    primary_mode = 'recover' if fault == 'recover' else 'stall' if fault == 'timeout' else 'unavailable'
    fallback_mode = 'unavailable' if fault == 'both-unavailable' else 'healthy'
    calls, exceptions, waits = [], [], []
    async with AsyncExitStack() as resources:
        primary_url, primary_rows = await resources.enter_async_context(fault_proxy(actual, snapshot, snapshot.bindings[binding], primary_mode))
        fallback_url, fallback_rows = await resources.enter_async_context(fault_proxy(actual, snapshot, snapshot.bindings[fallback], fallback_mode))
        for name, url in ((binding, primary_url), (fallback, fallback_url)):
            if name in cfg.get('modelPool', {}):
                cfg['modelPool'][name]['baseUrl'] = url
            elif name in cfg.get('models', {}):
                cfg['models'][name]['baseUrl'] = url
            else:
                raise ValueError('Binding is not explicitly materialized in the supplied config')
        timeout = inputs['timeout_attempt_seconds'] if fault == 'timeout' else inputs['attempt_seconds']
        cfg.setdefault('functionRoles', {})['chat'] = {'candidates': [binding, fallback],
            'timeoutSeconds': timeout, 'deadlineSeconds': inputs['deadline_seconds']}
        context.router.configure(cfg)

        async def call():
            try:
                response = await context.router.complete(inputs['messages'], context={
                    'function_role': 'chat', 'allow_fallback': True, 'max_output_tokens': inputs['max_output_tokens']})
                row = {'selected_binding': response.binding, 'returned_model': getattr(response.raw, 'model', None),
                    'output': response.content, 'prior_attempts': response.prior_attempts}
                calls.append(row)
            except RuntimeError as error:
                evidence = router_failure(error)
                if evidence is None:
                    raise
                exceptions.append(evidence)

        if fault == 'cooldown':
            first = asyncio.create_task(call())
            try:
                async with asyncio.timeout(10):
                    while not actual._endpoints._calls:
                        if first.done():
                            first.result()
                            raise RuntimeError('No observed failure state before cooldown trial')
                        await asyncio.sleep(.01)
                # First fallback may still be running. Exercise the second
                # selection while the real primary cooldown is active.
                await call()
                await first
            finally:
                if not first.done():
                    first.cancel()
                    await asyncio.gather(first, return_exceptions=True)
        else:
            await call()
            if fault == 'recover':
                started = time.monotonic()
                await asyncio.sleep(actual._endpoints.cooldown + .05)
                waits.append(time.monotonic()-started)
                await call()
    return {'output': {'completions': calls, 'unavailable': exceptions}, 'effects': {
        'primary_binding': binding, 'fallback_binding': fallback,
        'primary_fault_requests': primary_rows, 'fallback_proxy_requests': fallback_rows,
        'elapsed_wait_seconds': waits, 'real_cooldown_seconds': actual._endpoints.cooldown,
        'proxies_closed': True, 'boundary': 'actual_function_router',
        'native_gateway_recovery': 'not_exercised', 'remote_gpu_cancellation': 'not_observed'}}


def assess(observed, oracle):
    output, effects = observed.get('output', {}), observed.get('effects', {})
    calls, errors = output.get('completions', []), output.get('unavailable', [])
    primary, fallback = effects.get('primary_fault_requests', []), effects.get('fallback_proxy_requests', [])
    fault = oracle['fault']
    checks = {'proxy_cleanup': effects.get('proxies_closed') is True,
        'actual_router_boundary': effects.get('boundary') == 'actual_function_router',
        'observed_primary_fault': bool(primary) and primary[0].get('injected') == (
            'withheld_response' if fault == 'timeout' else 'http_503')}
    if fault == 'both-unavailable':
        checks.update(no_fabricated_completion=not calls, bounded_failure=len(errors) == 1,
            both_endpoints_observed_unavailable=len(primary) == len(fallback) == 1
                and fallback[0].get('injected') == 'http_503',
            no_real_endpoint_request=not any(row.get('forwarded') for row in primary+fallback))
        return checks
    count = 2 if fault in {'cooldown', 'recover'} else 1
    checks.update(expected_completion_count=len(calls) == count and not errors,
        answer_preserved=bool(calls) and all(oracle['marker'].casefold() in str(row.get('output', '')).casefold() for row in calls),
        returned_identity_observed=bool(calls) and all(bool(row.get('returned_model')) for row in calls),
        supporting_fallback_observed=bool(fallback) and all(row.get('forwarded') and row.get('status') == 200 for row in fallback))
    if fault == 'recover':
        checks.update(fallback_then_primary=[row.get('selected_binding') for row in calls] == [effects.get('fallback_binding'), effects.get('primary_binding')],
            initial_failure_attributed=bool(calls) and bool(calls[0].get('prior_attempts')),
            recovered_primary_has_no_prior_failure=len(calls) == 2 and calls[1].get('prior_attempts') == [],
            actual_primary_relay=len(primary) == 2 and primary[1].get('forwarded') is True and primary[1].get('status') == 200,
            real_cooldown_elapsed=any(value >= effects.get('real_cooldown_seconds', 15) for value in effects.get('elapsed_wait_seconds', [])))
    else:
        checks['fallback_not_credited_as_primary'] = bool(calls) and all(row.get('selected_binding') == effects.get('fallback_binding') and row.get('prior_attempts') for row in calls)
        checks['primary_not_forwarded'] = len(primary) == 1 and primary[0].get('forwarded') is False
    if fault == 'cooldown':
        checks['cooldown_skip_observed'] = any(any(attempt.get('reason') == 'EndpointCoolingDown' for attempt in row.get('prior_attempts', [])) for row in calls)
    if fault == 'timeout':
        checks['actual_router_deadline_observed'] = any(any(attempt.get('reason') == 'RequestBudgetExceeded' for attempt in row.get('prior_attempts', [])) for row in calls)
    return checks


def recipe_metadata(suite):
    return {'router_recovery_cases_sha256': digest([case.record() for case in suite]),
        'router_recovery_implementation': {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ('router_recovery.py', 'router_recovery_proxy.py')},
        'quality_scope': 'Router effects with real responses; fallback success is not primary model quality.'}


CONSUMERS = {'router_recovery': consume}
EVALUATORS = {'router_recovery_effects': assess}
