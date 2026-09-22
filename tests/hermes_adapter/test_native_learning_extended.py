"""Controlled inference exercises real native capture, reload and optional swap."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import sys

import pytest

from protagine.qualification.native import configuration, native_context
from protagine.qualification.native_learning import LearningRouter
from protagine.qualification.native_learning_extended import (
    CONSUMERS, DEVELOPMENT, EVALUATORS, cases, metrics, semantic_answer,
)
from protagine.qualification.records import read
from protagine.qualification.runner import evaluate
from protagine.router import LLMRouter
from test_model_qualification_native import endpoint, configured


def development_probes():
    probes = cases(include_initial=False)[::3]
    first = probes[0]
    probes.append(replace(first, id='controlled.learning.two-feedback-turns',
        inputs={**deepcopy(first.inputs), 'feedback_messages': [first.inputs['feedback'],
            first.inputs['feedback']+' Please confirm this remains the policy.']},
        oracle={**deepcopy(first.oracle), 'expected_feedback_count': 2}))
    return probes


@pytest.mark.parametrize('original', development_probes(), ids=lambda case: case.id)
def test_new_development_learning_constraints_use_real_sources(tmp_path, monkeypatch, original):
    from protagine.qualification import native_learning
    original_native = native_learning.native_cli

    async def checked(inputs, context, **kwargs):
        try:
            return await original_native(inputs, context, **kwargs)
        except Exception:
            log = context.state_dir/'native.log'
            if log.exists():
                print(log.read_text()[-8000:])
            raise

    monkeypatch.setattr(native_learning, 'native_cli', checked)

    def respond(data):
        if data['model'] == 'native-fixture':
            current = next(row['content'] for row in reversed(data['messages']) if row['role'] == 'user')
            if current.startswith(original.inputs['feedback']):
                value = {'acknowledged': True}
            elif current.startswith(original.inputs['messages'][-1]['content']):
                value = original.oracle['answer']
            else:
                value = original.oracle['baseline_answer']
        else:
            supplied = json.loads(data['messages'][-1]['content'])
            if 'proposals' in supplied:
                value = {str(row['index']): {'keep': True, 'reason': 'Reusable scoped procedure.'} for row in supplied['proposals']}
            elif supplied['message'] == original.inputs['feedback']:
                value = {'claims': [{'representation': 'procedure', 'subject': original.oracle['lesson_terms'][0],
                    'predicate': 'procedure', 'memory_kind': 'procedure', 'evidence': supplied['message'],
                    'operation': 'assert', 'prior_claim_id': None, 'recall_reason': 'Apply this procedure in its stated domain.'}]}
            else:
                value = {'claims': []}
        return {'role': 'assistant', 'content': json.dumps(value)}

    case = replace(original, timeout_seconds=120, inputs={**deepcopy(original.inputs), 'native_seconds': 100})
    with endpoint(respond=respond) as (url, _, _):
        selected, recipe = configuration(configured(tmp_path, url), 'fixture', hermes_python=sys.executable)
        host = {'provider': 'vllm', 'apiKey': 'controlled-key', 'models': {},
            'modelPool': {'writer': {'model': 'support-fixture', 'baseUrl': url, 'supportsTools': True}},
            'functionRoles': {'extraction': ['writer'], 'judging': ['writer']}}
        support = LLMRouter(tiers={}); support.configure(host)
        output = tmp_path/'run'
        asyncio.run(evaluate(output, recipe, [case], CONSUMERS, EVALUATORS,
            lambda _: LearningRouter(support, native_context(selected, recipe), host), evidence_mode='controlled'))
        result = read(output/'attempts'/case.id/'result.json')
        if original.inputs['scenario'] == 'irrelevant-feedback':
            effects = result['effects']
            wire = json.dumps(effects['phases']['transfer']['request_observations'])
            injected = any('turn:'+source in wire for source in effects['feedback_source_ids'])
            assert result['checks']['irrelevant_feedback_not_injected'] is not injected
            assert all(value is True for name, value in result['checks'].items()
                       if name != 'irrelevant_feedback_not_injected'), result
            assert result['outcome'] == ('fail' if injected else 'pass')
        else:
            assert result['outcome'] == 'pass', result
        assert result['primary_outcome'] == result['outcome']
        assert result['effects']['training_host_closed_before_transfer'] is True
        assert metrics(result, case.oracle)['transfer_goal_semantics'] is True


def test_processor_swap_uses_different_physical_endpoints_and_returned_models(tmp_path):
    pack = deepcopy(DEVELOPMENT)
    pack['scenarios'] = pack['scenarios'][:1]
    pack['scenarios'][0]['processor_swap'] = True
    original = cases(pack, include_initial=False)[0]
    case = replace(original, timeout_seconds=120, inputs={**original.inputs, 'native_seconds': 100})
    teacher_requests, reader_requests = [], []

    def teacher(data):
        teacher_requests.append(deepcopy(data))
        current = next(row['content'] for row in reversed(data['messages']) if row['role'] == 'user')
        value = {'acknowledged': True} if current.startswith(case.inputs['feedback']) else case.oracle['baseline_answer']
        return {'role': 'assistant', 'content': json.dumps(value)}

    def reader(data):
        reader_requests.append(deepcopy(data))
        if data['model'] == 'native-fixture':
            value = case.oracle['answer']
        else:
            supplied = json.loads(data['messages'][-1]['content'])
            if 'proposals' in supplied:
                value = {str(row['index']): {'keep': True, 'reason': 'Approved procedure, quoted dissent distinguished.'} for row in supplied['proposals']}
            elif supplied['message'] == case.inputs['feedback']:
                value = {'claims': [{'representation': 'procedure', 'subject': case.oracle['lesson_terms'][0],
                    'predicate': 'procedure', 'memory_kind': 'procedure', 'evidence': supplied['message'],
                    'operation': 'assert', 'prior_claim_id': None, 'recall_reason': 'Use the approved procedure.'}]}
            else:
                value = {'claims': []}
        return {'role': 'assistant', 'content': json.dumps(value)}

    with endpoint(respond=reader) as (read_url, _, _), endpoint(respond=teacher, returned_model='teacher-fixture') as (write_url, _, _):
        selected, recipe = configuration(configured(tmp_path, read_url), 'fixture', hermes_python=sys.executable)
        training_config = deepcopy(selected)
        training_config['model']['default'] = 'teacher-fixture'
        training_config['providers']['fixture'].update(base_url=write_url, default_model='teacher-fixture')
        teacher_path = tmp_path/'teacher.json'; teacher_path.write_text(json.dumps(training_config))
        training, training_recipe = configuration(teacher_path, 'fixture', hermes_python=sys.executable)
        recipe['declared']['distinct_learning_processors'] = True
        host = {'provider': 'vllm', 'apiKey': 'controlled-key', 'models': {},
            'modelPool': {'writer': {'model': 'support-fixture', 'baseUrl': read_url, 'supportsTools': True}},
            'functionRoles': {'extraction': ['writer'], 'judging': ['writer']}}
        support = LLMRouter(tiers={}); support.configure(host)
        output = tmp_path/'run'
        asyncio.run(evaluate(output, recipe, [case], CONSUMERS, EVALUATORS,
            lambda _: LearningRouter(support, native_context(selected, recipe), host,
                training_native=native_context(training, training_recipe)), evidence_mode='controlled'))
        result = read(output/'attempts'/case.id/'result.json')
        assert result['outcome'] == 'pass', result
        assert result['checks']['different_returned_processor_identities'] is True
        assert result['primary_outcome'] == 'pass', result
        assert len(teacher_requests) == 2
        native_reads = [row for row in reader_requests if row['model'] == 'native-fixture']
        assert len(native_reads) == 1
        quotes = [json.loads(line[2:]) for row in native_reads[0]['messages']
                  for line in str(row.get('content', '')).splitlines() if line.startswith('- {')]
        assert any(row.get('quote') == case.inputs['feedback'] for row in quotes)


def test_semantic_diagnostics_do_not_reward_conflicting_or_additional_claims():
    assert semantic_answer('15 spare labels.', {'labels': 15}) is True
    assert semantic_answer('```json\n{"rack":"quarantine"}\n```', {'rack': 'quarantine'}) is True
    assert semantic_answer('quarantine', {'rack': 'quarantine'}) is True
    assert semantic_answer('not quarantine', {'rack': 'quarantine'}) is None
    assert semantic_answer('15 labels or possibly 19', {'labels': 15}) is None
    assert semantic_answer('{"labels":19}', {'labels': 15}) is False
    assert semantic_answer('15 labels. I saved it.', {'labels': 15}) is None


def test_missing_swap_capability_is_unsupported_without_any_endpoint(tmp_path):
    pack = deepcopy(DEVELOPMENT)
    pack['scenarios'] = pack['scenarios'][:1]
    pack['scenarios'][0]['processor_swap'] = True
    case = cases(pack, include_initial=False)[0]
    output = tmp_path/'run'
    def forbidden(_):
        raise AssertionError('Capability failure must precede worker creation')
    asyncio.run(evaluate(output, {'binding': 'unused', 'declared': {}}, [case], CONSUMERS, EVALUATORS,
                         forbidden, evidence_mode='controlled'))
    result = read(output/'attempts'/case.id/'result.json')
    assert result['outcome'] == 'unsupported'
    assert result['missing_capabilities'] == ['distinct_learning_processors']
