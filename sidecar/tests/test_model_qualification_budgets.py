"""Configured qualification recipes and actual controlled HTTP receipts."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from pacomind.qualification.cases import STANDARD, role_completion, json_fields
from pacomind.qualification.cli import run
from pacomind.qualification.records import read
from pacomind.qualification.report import compare, markdown
from pacomind.qualification.runner import evaluate, inspect_binding, materialize_role_cases, router_for
from pacomind.qualification.structured_cases import CASES
from test_function_routing import config, endpoint


def test_binding_budget_is_frozen_and_rebinding_versions_the_case_without_rewriting_it():
    cfg = config('http://127.0.0.1:9911/v1', 'http://127.0.0.1:9912/v1')
    cfg['modelPool']['interactive']['maxTokens'] = 4096
    cfg['modelPool']['deliberate']['maxTokens'] = 8192
    before = deepcopy(cfg)
    original = CASES[0]
    original_record = original.record()
    left, policy = materialize_role_cases(cfg, 'interactive', [original])
    right, _ = materialize_role_cases(cfg, 'deliberate', [original])
    assert left[0].inputs['max_output_tokens'] == 4096
    assert right[0].inputs['max_output_tokens'] == 8192
    assert left[0].version == right[0].version == '1-configured-output-v1'
    assert left[0].record()['sha256'] != right[0].record()['sha256']
    assert left[0].inputs['messages'] == right[0].inputs['messages'] == original.inputs['messages']
    assert left[0].oracle == right[0].oracle == original.oracle
    assert policy['cases'][original.id] == {'max_output_tokens': 4096,
        'request_timeout_seconds': 120, 'role_deadline_seconds': 180,
        'case_timeout_seconds': 185, 'case_overhead_seconds': 5}
    assert cfg == before and original.record() == original_record
    cfg['modelPool']['interactive']['maxTokens'] = 2048
    assert left[0].inputs['max_output_tokens'] == 4096
    assert original.inputs['max_output_tokens'] == 1024


def test_default_binding_allowance_does_not_fall_back_to_consumer_512():
    cfg = config('http://127.0.0.1:9911/v1', 'http://127.0.0.1:9912/v1')
    cases, policy = materialize_role_cases(cfg, 'interactive', [STANDARD[0]])
    assert 'max_output_tokens' not in STANDARD[0].inputs
    assert cases[0].inputs['max_output_tokens'] == 8192
    assert policy['cases'][STANDARD[0].id]['max_output_tokens'] == 8192


def test_domain_and_native_consumers_keep_their_original_budgets():
    from pacomind.qualification.memory_cases import CASES as MEMORY
    from pacomind.qualification.native import cases as native_cases
    cfg = config('http://127.0.0.1:9911/v1', 'http://127.0.0.1:9912/v1')
    originals = [MEMORY[0], *native_cases(['reasoning']), CASES[0]]
    records = [case.record() for case in originals]
    derived, policy = materialize_role_cases(cfg, 'interactive', originals)
    assert [case.record() for case in originals] == records
    assert derived[0] is originals[0] and derived[1] is originals[1]
    assert derived[1].inputs['max_output_tokens'] == 1024
    assert set(policy['cases']) == {CASES[0].id}
    assert derived[2].inputs['max_output_tokens'] == 8192


@pytest.mark.parametrize('key', ['max_tokens', 'max_completion_tokens', 'max_output_tokens'])
def test_conflicting_request_override_cannot_misrepresent_the_frozen_allowance(key):
    from pacomind.qualification.memory_cases import CASES as MEMORY
    cfg = config('http://127.0.0.1:9911/v1', 'http://127.0.0.1:9912/v1')
    cfg['modelPool']['interactive']['extraBody'] = {key: 512}
    with pytest.raises(ValueError, match='output override conflicts'):
        materialize_role_cases(cfg, 'interactive', [STANDARD[0]])
    unchanged, policy = materialize_role_cases(cfg, 'interactive', [MEMORY[0]])
    assert unchanged[0] is MEMORY[0] and policy['cases'] == {}


def test_role_envelope_outside_existing_case_bound_is_rejected_before_dispatch():
    cfg = config('http://127.0.0.1:9911/v1', 'http://127.0.0.1:9912/v1')
    cfg['functionRoles']['reasoning'] = {'candidates': ['interactive'], 'deadlineSeconds': 600}
    with pytest.raises(ValueError, match='Case deadline'):
        materialize_role_cases(cfg, 'interactive', [CASES[0]])


@pytest.mark.parametrize('binding,cap', [('interactive', 3072), ('deliberate', 6144)])
def test_cli_records_binding_recipe_before_dispatch_and_sends_its_exact_cap(tmp_path, capsys, binding, cap):
    output = tmp_path / 'run'

    def answer(payload):
        manifest = read(output / 'run.json')
        frozen = manifest['cases'][0]
        assert frozen['inputs']['max_output_tokens'] == payload['max_tokens'] == cap
        assert frozen['version'] == '1-configured-output-v1'
        policy = manifest['recipe']['qualification_output_policy']
        assert policy['binding'] == binding
        assert policy['cases'][frozen['id']]['max_output_tokens'] == cap
        return '{"blue":"drawer 4","silver":null}'

    with endpoint(content=answer) as (url, calls):
        cfg = config(url, url)
        cfg['modelPool'][binding]['maxTokens'] = cap
        cfg['functionRoles']['chat']['maxLatencyMs'] = 5000
        path = tmp_path / 'config.json'
        path.write_text(json.dumps(cfg))
        before = path.read_bytes()
        args = SimpleNamespace(models_command='evaluate', binding=binding, config=path,
            roles='chat', suite='standard', output=output, resume=False, evidence_mode='controlled')
        assert run(args) == 0
        result = read(output / 'attempts/chat.grounded-note/result.json')
        observed = result['observations'][0]
        assert observed['requested_max_output_tokens'] == observed['client_max_tokens'] == cap
        assert observed['finish_reason'] == 'stop' and observed['completion_truncated'] is False
        assert observed['completion_status'] == 'complete'
        assert observed['usage']['completion_tokens'] == 2
        assert observed['usage']['reasoning_tokens'] is None
        args.resume = True
        assert run(args) == 0
        assert len(calls) == 1 and path.read_bytes() == before
        assert result == read(output / 'attempts/chat.grounded-note/result.json')
        assert 'role_completion' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_length_terminated_response_is_bounded_partial_evidence_not_a_semantic_failure(tmp_path):
    partial = 'Partial answer ' * 1000
    choice = {'index': 0, 'finish_reason': 'length', 'message': {
        'role': 'assistant', 'content': partial, 'reasoning_content': 'private scratch must not be retained'}}
    with endpoint(choice=choice) as (url, calls):
        cfg = config(url, url)
        cfg['modelPool']['interactive']['maxTokens'] = 4096
        cases, policy = materialize_role_cases(cfg, 'interactive', [STANDARD[0]])
        case = replace(cases[0], max_output_bytes=256)
        recipe = {**inspect_binding(cfg, 'interactive'), 'qualification_output_policy': policy}
        await evaluate(tmp_path / 'run', recipe, [case], {'role_completion': role_completion},
            {'json_fields': json_fields}, lambda _: router_for(cfg, 'interactive', [case]))
        result = read(tmp_path / 'run/attempts/chat.grounded-note/result.json')
        assert len(calls) == 1 and calls[0]['payload']['max_tokens'] == 4096
        assert result['outcome'] == 'error' and result['failure_category'] == 'incomplete_final_answer'
        assert result['output'] is None and result['checks'] == {}
        call = result['observations'][0]
        assert call['outcome'] == 'error' and call['selected_binding'] is None
        observed = call['rejected_completion']
        assert observed['completion_status'] == 'incomplete'
        assert observed['provenance'] == 'router_rejected_completion'
        assert observed['selected_binding'] == 'interactive'
        assert observed['client_max_tokens'] == 4096
        assert observed['finish_reason'] == 'length' and observed['completion_truncated'] is True
        assert observed['usage']['completion_tokens'] == 2
        evidence = observed['completion_evidence']
        assert evidence['text'].startswith('Partial answer') and evidence['truncated'] is True
        assert evidence['retained_json_bytes'] <= 256
        assert 'private scratch' not in json.dumps(result)
        # Normal caller errors keep their existing fixed reason, not model text.
        selected = router_for(cfg, 'interactive', [case])
        with pytest.raises(RuntimeError) as raised:
            await selected.complete(case.inputs['messages'], context={'function_role': 'chat',
                'allow_fallback': False, 'max_output_tokens': 4096})
        assert 'Partial answer' not in str(raised.value) + repr(raised.value)
        assert 'private scratch' not in str(raised.value) + repr(raised.value)
        assert len(calls) == 2  # One request per two separately controlled callers; no retry.


@pytest.mark.asyncio
async def test_conflicting_client_limit_aliases_are_unknown_without_changing_normal_requests(tmp_path):
    with endpoint(content='{"blue":"drawer 4","silver":null}') as (url, calls):
        cfg = config(url, url)
        cfg['modelPool']['interactive']['extraBody'] = {'max_completion_tokens': 2048}
        case = replace(STANDARD[0], inputs={**STANDARD[0].inputs, 'max_output_tokens': 1024})
        await evaluate(tmp_path / 'run', inspect_binding(cfg, 'interactive'), [case],
            {'role_completion': role_completion}, {'json_fields': json_fields},
            lambda _: router_for(cfg, 'interactive', [case]))
        row = read(tmp_path / 'run/attempts/chat.grounded-note/result.json')
        assert row['outcome'] == 'pass' and len(calls) == 1
        assert calls[0]['payload']['max_tokens'] == 1024
        assert calls[0]['payload']['max_completion_tokens'] == 2048
        assert row['observations'][0]['client_max_tokens'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['budget', 'envelope', 'identical', 'question', 'oracle', 'version', 'output_bound',
                                    'runtime', 'evaluator', 'consumer', 'evidence_mode',
                                    'identical_runtime', 'identical_consumer'])
async def test_recipe_comparison_allows_only_declared_budget_changes_for_the_same_task(tmp_path, change):
    from test_model_qualification_runner import Router
    cfg = config('http://127.0.0.1:9911/v1', 'http://127.0.0.1:9912/v1')
    cfg['modelPool']['interactive']['maxTokens'] = 4096
    identical_cases = {'identical', 'identical_runtime', 'identical_consumer'}
    cfg['modelPool']['deliberate']['maxTokens'] = 4096 if change in identical_cases | {'envelope'} else 8192

    async def changed_consumer(inputs, context):
        return await role_completion(inputs, context)

    def changed_evaluator(observed, oracle):
        return json_fields(observed, oracle)

    for name, binding in [('before', 'interactive'), ('after', 'deliberate')]:
        original = STANDARD[0]
        if name == 'after':
            if change == 'question':
                original = replace(original, inputs={**original.inputs,
                    'messages': [{'role': 'user', 'content': 'A different question.'}]})
            elif change == 'oracle':
                original = replace(original, oracle={'fields': [{'name': 'changed',
                    'path': ['output', 'blue'], 'equals': 'drawer 5'}]})
            elif change == 'version':
                original = replace(original, version='2')
            elif change == 'output_bound':
                original = replace(original, max_output_bytes=original.max_output_bytes // 2)
            elif change == 'envelope':
                cfg['functionRoles']['chat'] = {'candidates': [binding],
                    'timeoutSeconds': 30, 'deadlineSeconds': 90}
        derived, policy = materialize_role_cases(cfg, binding, [original])
        recipe = {**inspect_binding(cfg, binding), 'qualification_output_policy': policy,
                  'runtime_version': 'controlled-runtime-1'}
        if name == 'after' and change in {'runtime', 'identical_runtime'}:
            recipe['runtime_version'] = 'controlled-runtime-2'
        await evaluate(tmp_path / name, recipe, derived,
            {'role_completion': changed_consumer if name == 'after'
                and change in {'consumer', 'identical_consumer'} else role_completion},
            {'json_fields': changed_evaluator if name == 'after' and change == 'evaluator' else json_fields},
            lambda _: Router('{"blue":"drawer 4","silver":null}', binding=binding),
            evidence_mode='actual_inference' if name == 'after' and change == 'evidence_mode' else 'controlled')

    report = compare(tmp_path / 'before', tmp_path / 'after')
    row = report['cases'][0]
    assert row['comparable'] is (change in identical_cases | {'budget', 'envelope'})
    assert report['system_implementation_changed'] is (change in {'consumer', 'identical_consumer', 'evaluator'})
    if change in {'budget', 'envelope'}:
        assert report['same_suite'] is False
        assert row['comparison_basis'] == 'same_task_different_budget_recipes'
        assert row['before_budget']['max_output_tokens'] == 4096
        assert row['after_budget']['max_output_tokens'] == (8192 if change == 'budget' else 4096)
        if change == 'envelope':
            assert row['after_budget']['case_timeout_seconds'] == 95
        assert row['elapsed_ms_delta'] is not None
        rendered = markdown(report)
        assert 'same_task_different_budget_recipes' in rendered
        assert '4096' in rendered and 'not a same-budget comparison' in rendered
    elif change in identical_cases:
        assert report['same_suite'] is True and row['comparison_basis'] == 'identical_case'
        assert row['elapsed_ms_delta'] is not None
    else:
        assert row['comparison_basis'] == 'incomparable' and row['elapsed_ms_delta'] is None
