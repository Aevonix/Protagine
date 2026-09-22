"""Declared contracts travel over the real router; no model oracles in requests."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json

import jsonschema
import pytest

from protagine.qualification import benchmark
from protagine.qualification.benchmark_cases import screen_cases
from protagine.qualification.records import read
from protagine.qualification.screen_contracts import SCHEMAS, VERSION, apply_contracts, assess
from test_function_routing import config, endpoint


def _answer(case):
    answer = {}
    for field in case.oracle['fields']:
        path = field['path']
        assert path[0] == 'output'
        if len(path) == 1:
            return deepcopy(field['equals'])
        current = answer
        for name in path[1:-1]:
            current = current.setdefault(name, {})
        current[path[-1]] = deepcopy(field['equals'])
    return answer


def test_contracts_cover_all_direct_tasks_without_changing_semantic_oracles_or_budgets():
    original = screen_cases()
    before = [case.record() for case in original]
    derived = apply_contracts(original)
    assert len(SCHEMAS) == 16
    for prior, current in zip(original, derived):
        assert prior.id == current.id
        assert prior.oracle == current.oracle
        assert prior.record()['oracle_sha256'] == current.record()['oracle_sha256']
        if 'messages' in prior.inputs:
            assert prior.inputs['messages'] == current.inputs['messages']
        assert prior.timeout_seconds == current.timeout_seconds
        if prior.boundary != 'role_completion':
            assert current.record() == prior.record()
            continue
        if 'max_output_tokens' in prior.inputs:
            assert current.inputs['max_output_tokens'] == prior.inputs['max_output_tokens']
        schema = current.inputs['response_schema']['schema']
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(_answer(prior), schema)
        # Changing expected values cannot change model inputs or schema types.
        alternate = replace(prior, oracle={'fields': [{'path': ['output'], 'equals': 'unrelated'}]})
        assert apply_contracts([alternate])[0].inputs == current.inputs
    assert [case.record() for case in original] == before


def test_schema_accepts_wrong_answers_without_telling_model_missing_values_or_correct_results():
    samples = {
        'chat.grounded-note': {'blue': None, 'silver': 'invented'},
        'screen.grounding.correction-scope': {'day': 'invented', 'time': 'invented', 'room': 'invented',
                                            'attendance_confirmed': True},
        'screen.grounding.unresolved-conflict': {'location': 'invented', 'needs_clarification': False,
                                               'conflicting_record_ids': []},
        'screen.planning.resource-and-dependency': {'validation_start': -9, 'validation_finish': 0,
            'publication_start': 100, 'finish_if_authorized_at_12': 50},
    }
    cases = {case.id: case for case in screen_cases()}
    for identity, answer in samples.items():
        schema = SCHEMAS[identity]
        jsonschema.validate(answer, schema)
        effects = {'output_schema': schema, 'serialized_requests': [{'response_format_matches': True}]}
        checks = assess({'output': json.dumps(answer), 'effects': effects}, cases[identity].oracle)
        assert checks['output_satisfies_schema']
        assert not all(checks.values())


@pytest.mark.parametrize('output', ['```json\n{"blue":"drawer 4","silver":null}\n```',
    'Answer: {"blue":"drawer 4","silver":null}', '{"blue":"drawer 4","silver":null}{}'])
def test_no_fence_stripping_or_substring_salvage(output):
    case = screen_cases()[0]
    effects = {'output_schema': SCHEMAS[case.id], 'serialized_requests': [{'response_format_matches': True}]}
    checks = assess({'output': output, 'effects': effects}, case.oracle)
    assert checks['output_is_json'] is False
    assert checks['output_satisfies_schema'] is False


def _host(tmp_path, url, *, capable=True):
    cfg = config(url, url, timeoutSeconds=10, deadlineSeconds=20)
    cfg['modelPool']['interactive'].update(supportsJsonSchema=capable, maxTokens=4096)
    cfg['functionRoles']['judging'] = ['deliberate']
    cfg['functionRoles']['chat'] = {'candidates': ['interactive'], 'timeoutSeconds': 10, 'deadlineSeconds': 20}
    path = tmp_path / 'host.json'
    path.write_text(json.dumps(cfg))
    return path


def test_new_protocol_identity_and_unchanged_full_standard_selection(tmp_path):
    path = _host(tmp_path, 'http://127.0.0.1:9988/v1')
    options = dict(config_path=path, binding='interactive', boundaries=['role_completion', 'cognition_consumer'])
    prompt, before, *_ = benchmark.prepare(**options)
    structured, after, *_ = benchmark.prepare(**options, output_contract='json_schema')
    assert prompt['suite_version'] == 'agent-screen-1'
    assert structured['suite_version'] == VERSION
    assert prompt['selected_cases'] == structured['selected_cases'] == 18
    assert prompt['sha256'] != structured['sha256']
    for a, b in zip(before[0][1], after[0][1]):
        assert a.id == b.id and a.oracle == b.oracle
        assert a.inputs.get('max_output_tokens') == b.inputs.get('max_output_tokens')
        assert a.timeout_seconds == b.timeout_seconds
    assert all(case.inputs['max_output_tokens'] == 4096 for case in after[0][1]
               if case.boundary == 'role_completion')


def test_real_http_contract_all_three_chat_cases_one_attempt_and_resume_is_inert(tmp_path):
    cases = [case for case in screen_cases() if case.role == 'chat' and case.boundary == 'role_completion']
    answers = {json.dumps(case.inputs['messages'], sort_keys=True): _answer(case) for case in cases}

    def response(body):
        return json.dumps(answers[json.dumps(body['messages'], sort_keys=True)])

    with endpoint(content=response) as (url, requests):
        path = _host(tmp_path, url)
        directory = tmp_path / 'batch'
        benchmark.plan(directory, config_path=path, binding='interactive', roles=['chat'],
            boundaries=['role_completion'], evidence_mode='controlled', output_contract='json_schema')
        result = asyncio.run(benchmark.run(directory, config_path=path))
        assert result['outcomes'] == {'pass': 3}
        assert result['runs'][0]['primary_passes'] == 3
        assert len(requests) == 3
        for case, request in zip(cases, requests):
            body = request['payload']
            assert body['messages'] == case.inputs['messages']
            assert body['max_tokens'] == 4096
            assert body['response_format'] == {'type': 'json_schema', 'json_schema': {
                'name': 'screen_answer', 'schema': SCHEMAS[case.id], 'strict': True}}
        asyncio.run(benchmark.run(directory, config_path=path, resume=True))
        assert len(requests) == 3
        manifest = read(directory / 'benchmark.json')
        assert 'screen_contracts.py' in manifest['implementation']
        assert 'schema_cases.py' in manifest['implementation']


def test_unavailable_schema_support_is_reported_without_silent_prompt_fallback(tmp_path):
    with endpoint(content='{}') as (url, requests):
        path = _host(tmp_path, url, capable=False)
        directory = tmp_path / 'batch'
        benchmark.plan(directory, config_path=path, binding='interactive', roles=['chat'],
            boundaries=['role_completion'], evidence_mode='controlled', output_contract='json_schema')
        result = asyncio.run(benchmark.run(directory, config_path=path))
        assert result['outcomes'] == {'unsupported': 3}
        assert requests == []


def test_no_wire_witness_cannot_pass_even_with_a_correct_answer():
    case = screen_cases()[0]
    checks = assess({'output': json.dumps(_answer(case)),
                     'effects': {'output_schema': SCHEMAS[case.id]}}, case.oracle)
    assert checks['output_satisfies_schema'] is True
    assert checks['schema_in_serialized_request'] is False
