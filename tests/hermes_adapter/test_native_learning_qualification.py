"""Controlled model, actual native feedback writer and fresh-session transfer."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import sys

import pytest

from protagine.qualification.native import configuration, native_context
from protagine.qualification.native_learning import LearningRouter, CONSUMERS, EVALUATORS, assess, transfer_metrics
from protagine.qualification.native_learning_cases import CASES
from protagine.qualification.records import read
from protagine.qualification.runner import evaluate
from protagine.router import LLMRouter
from test_model_qualification_native import endpoint, configured


@pytest.mark.parametrize('original', CASES, ids=lambda case: case.id)
def test_feedback_is_natively_captured_then_used_only_in_recollection_arm(tmp_path, monkeypatch, original):
    arm = original.inputs['arm']
    native_requests = []
    from protagine.qualification import native_learning
    original_native = native_learning.native_cli

    async def checked_native(inputs, context, **kwargs):
        try:
            return await original_native(inputs, context, **kwargs)
        except Exception:
            log = context.state_dir / 'native.log'
            if log.exists():
                print(log.read_text()[-10000:])
            raise

    monkeypatch.setattr(native_learning, 'native_cli', checked_native)

    def respond(data):
        if data['model'] == 'native-fixture':
            native_requests.append(deepcopy(data))
            current = next(row['content'] for row in reversed(data['messages']) if row['role'] == 'user')
            if current.startswith(original.inputs['feedback']):
                answer = {'acknowledged': True}
            elif current.startswith(original.inputs['messages'][-1]['content']) and original.inputs['feedback'] in json.dumps(data['messages']):
                answer = original.oracle['answer']
            else:
                answer = original.oracle['unknown_answer']
            return {'role': 'assistant', 'content': json.dumps(answer)}
        supplied = json.loads(data['messages'][-1]['content'])
        if 'proposals' in supplied:
            value = {str(row['index']): {'keep': True, 'reason': 'Reusable complete local routing procedure.'}
                     for row in supplied['proposals']}
        elif supplied['message'] == original.inputs['feedback']:
            subject = {'intake-procedure': 'workshop intake tickets', 'exception-precedence': 'archive review',
                'computed-transfer': 'lab parcel checklist', 'negative-transfer': 'greenhouse sensor alerts'}[original.inputs['scenario']]
            value = {'claims': [{'representation': 'procedure', 'subject': subject,
                'predicate': 'routing procedure', 'memory_kind': 'procedure',
                'evidence': supplied['message'], 'operation': 'assert', 'prior_claim_id': None,
                'recall_reason': 'Route future workshop intake tickets using the stated seal conditions.'}]}
        else:
            value = {'claims': []}
        return {'role': 'assistant', 'content': json.dumps(value)}

    case = replace(original, timeout_seconds=120,
        inputs={**deepcopy(original.inputs), 'native_seconds': 100})
    with endpoint(respond=respond) as (url, requests, _):
        native_config, recipe = configuration(configured(tmp_path, url), 'fixture', hermes_python=sys.executable)
        host = {'provider': 'vllm', 'apiKey': 'controlled-fixture-key', 'models': {},
            'modelPool': {'writer': {'model': 'fixture-writer', 'baseUrl': url, 'supportsTools': True}},
            'functionRoles': {'extraction': ['writer'], 'judging': ['writer']}}
        support = LLMRouter(tiers={})
        support.configure(host)
        directory = tmp_path / 'run'
        asyncio.run(evaluate(directory, recipe, [case], CONSUMERS, EVALUATORS,
            lambda _: LearningRouter(support, native_context(native_config, recipe), host), evidence_mode='controlled'))
        result = read(directory / 'attempts' / case.id / 'result.json')
        assert result['outcome'] == 'pass', result
        assert result['primary_outcome'] == 'pass'
        assert len(native_requests) == 3
        effects = result['effects']
        assert effects['phases']['baseline']['session_id'] == effects['phases']['feedback']['session_id']
        assert effects['phases']['transfer']['session_id'] != effects['phases']['feedback']['session_id']
        assert bool(effects['feedback_source_ids']) is (arm != 'base_hermes')
        assert (original.inputs['feedback'] in json.dumps(native_requests[-1])) is (arm == 'protagine')
        assert all(row['role'] in {'extraction', 'judging'} and row['binding_purpose'] == 'supporting'
                   for row in effects['supporting_observations'])
        assert not list((directory / 'attempts' / case.id).glob('state-*'))
        metrics = transfer_metrics(result, original.oracle)
        assert metrics['baseline_goal_completed'] is False
        assert metrics['transfer_goal_completed'] is (arm == 'protagine' or original.inputs['scenario'] == 'negative-transfer')


def test_correct_transfer_answer_without_native_feedback_or_fresh_session_does_not_pass():
    checks = assess({'output': '{"tray":"amber"}', 'effects': {}}, CASES[0].oracle)
    assert checks['transfer_answer'] is True
    assert checks['ordinary_native_feedback_retained'] is False
    assert checks['fresh_transfer_session'] is False
    assert checks['useful_procedure_formed'] is False
