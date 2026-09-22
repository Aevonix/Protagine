"""Controlled HTTP trials use the real router and owned fault proxies."""
import asyncio
from dataclasses import replace

import pytest

from protagine.qualification.records import read
from protagine.qualification.router_recovery import (
    CONSUMERS, EVALUATORS, RecoveryRouter, VERSION, cases, recipe_metadata,
)
from protagine.qualification.runner import evaluate
from test_function_routing import config, endpoint


@pytest.mark.parametrize('fault', ['service-error', 'recover', 'timeout', 'cooldown', 'both-unavailable'])
def test_actual_router_faults_and_recovery(tmp_path, fault):
    pack = {'version': VERSION, 'split': 'development',
        'scenarios': [{'id': 'controlled-'+fault, 'fault': fault, 'marker': 'indigo'}]}
    original = cases(pack)[0]
    case = replace(original, timeout_seconds=60, inputs={**original.inputs,
        'attempt_seconds': 10, 'deadline_seconds': 25, 'timeout_attempt_seconds': 2})
    with endpoint(content='indigo') as (primary, primary_calls), endpoint(content='indigo') as (fallback, fallback_calls):
        cfg = config(primary, fallback)
        recipe = {'binding': 'interactive', **recipe_metadata([case])}
        asyncio.run(evaluate(tmp_path/'run', recipe, [case], CONSUMERS, EVALUATORS,
            lambda _: RecoveryRouter(cfg, 'interactive', 'deliberate'), evidence_mode='controlled'))
    result = read(tmp_path/'run'/'attempts'/case.id/'result.json')
    assert result['outcome'] == 'pass', result
    assert result['primary_outcome'] != 'pass', result
    if fault == 'both-unavailable':
        assert not primary_calls and not fallback_calls
    elif fault == 'recover':
        assert len(primary_calls) == len(fallback_calls) == 1
        assert result['effects']['elapsed_wait_seconds'][0] >= 15
    else:
        assert not primary_calls
        assert len(fallback_calls) == (2 if fault == 'cooldown' else 1)
    assert all(row['authorization'] == 'Bearer neutral-key' for row in primary_calls+fallback_calls)


def test_fallback_must_not_alias_the_same_endpoint_model():
    cfg = config('http://127.0.0.1:1/v1', 'http://127.0.0.1:1/v1')
    cfg['modelPool']['deliberate']['model'] = cfg['modelPool']['interactive']['model']
    with pytest.raises(ValueError, match='independent'):
        RecoveryRouter(cfg, 'interactive', 'deliberate')


def test_primary_failure_cannot_pass_from_fallback_text_without_effects():
    case = cases()[0]
    checks = EVALUATORS['router_recovery_effects']({'output': {'completions': [
        {'output': 'ochre', 'selected_binding': 'fallback', 'returned_model': 'controlled'}]}}, case.oracle)
    assert checks['answer_preserved'] is True
    assert checks['observed_primary_fault'] is False
    assert checks['supporting_fallback_observed'] is False

