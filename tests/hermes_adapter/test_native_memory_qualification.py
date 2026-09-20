"""Actual Hermes, plugin, scoped HTTP API and lexical ledger; controlled models."""
import asyncio
import argparse
from copy import deepcopy
from dataclasses import replace
import json
import re
import sys

import pytest

from protagine.qualification.native import configuration, native_context
from protagine.qualification.native_memory import MemoryRouter, CONSUMERS, EVALUATORS, assess
from protagine.qualification.native_memory_cases import CASES
from protagine.qualification.records import read
from protagine.qualification.runner import evaluate
from protagine.router import LLMRouter
from test_model_qualification_native import endpoint, configured


@pytest.mark.parametrize('original', CASES, ids=lambda case: case.id)
def test_native_memory_runs_real_formation_injection_and_scope(tmp_path, monkeypatch, original):
    native_requests = []
    from protagine.qualification import native_memory
    original_native = native_memory.native_cli

    async def checked_native(inputs, context, **kwargs):
        try:
            return await original_native(inputs, context, **kwargs)
        except Exception:
            log = context.state_dir / 'native.log'
            if log.exists():
                print(log.read_text()[-10000:])
            raise

    monkeypatch.setattr(native_memory, 'native_cli', checked_native)

    def respond(data):
        if data['model'] == 'native-fixture':
            native_requests.append(deepcopy(data))
            return {'role': 'assistant', 'content': json.dumps(original.oracle['answer'])}
        supplied = json.loads(data['messages'][-1]['content'])
        if 'proposals' in supplied:
            value = {str(row['index']): {'keep': True, 'reason': 'Source-grounded controlled fixture.'}
                     for row in supplied['proposals']}
        else:
            message = supplied['message']
            source = next(row for row in original.inputs['turns'] if row['text'] == message)
            if source['id'] in original.oracle['no_claim_sources']:
                return {'role': 'assistant', 'content': '{"claims":[]}'}
            subject = 'spare sensor' if 'spare sensor' in message else 'I'
            predicate = ('location' if subject == 'spare sensor' else 'label' if 'label' in message
                         else 'tea_preference' if 'tea' in message else 'badge_code')
            if 'library shelf' in message:
                subject = 'library shelf'
            if 'garden gate' in message:
                subject = 'garden gate'
            codes = re.findall(r'[a-z]+-\d{3}', message)
            claim = {'subject': subject, 'predicate': predicate,
                'value': codes[0] if codes else 'amber cabinet' if 'amber cabinet' in message else 'cedar drawer',
                'evidence': message, 'operation': 'assert', 'prior_claim_id': None,
                'memory_kind': 'personal_context', 'recall_reason': 'Recall the stated badge code on later requests.',
                'valid_from_text': None, 'valid_to_text': None, 'event_at_text': None}
            if 'tea' in message:
                claim.update(representation='preference', memory_kind='preference')
                claim.pop('value')
            if message.startswith('Correction:'):
                prior = next(row for row in supplied['prior_assertions'] if row['predicate'] == predicate.replace('_', ' '))
                claim.update(operation='correct', prior_claim_id=prior['id'], subject=prior['subject'], predicate=prior['predicate'])
            value = {'claims': [claim]}
        return {'role': 'assistant', 'content': json.dumps(value)}

    case = replace(original, timeout_seconds=90,
        inputs={**deepcopy(original.inputs), 'native_seconds': 60})
    with endpoint(respond=respond) as (url, requests, _):
        native_config, recipe = configuration(configured(tmp_path, url), 'fixture', hermes_python=sys.executable)
        host = {'provider': 'vllm', 'apiKey': 'controlled-fixture-key', 'models': {},
            'modelPool': {'writer': {'model': 'fixture-writer', 'baseUrl': url, 'supportsTools': True}},
            'functionRoles': {'extraction': ['writer'], 'judging': ['writer']}}
        support = LLMRouter(tiers={})
        support.configure(host)
        directory = tmp_path / 'run'
        asyncio.run(evaluate(directory, recipe, [case], CONSUMERS, EVALUATORS,
            lambda _: MemoryRouter(support, native_context(native_config, recipe)), evidence_mode='controlled'))
        result = read(directory / 'attempts' / case.id / 'result.json')
        assert result['outcome'] == 'pass', result
        assert result['primary_outcome'] == 'pass'
        assert len(native_requests) == 1
        wire = json.dumps(native_requests[0]['messages'])
        for source in original.oracle['required_source_ids']:
            assert 'turn:' + source in wire, wire
        for term in original.oracle['forbidden_request_terms']:
            assert term not in wire, wire
        assert 'oracle' not in native_requests[0]
        assert result['effects']['fresh_session'] is True
        assert result['effects']['request_observations'][0]['text'] == json.dumps(
            {'messages': native_requests[0]['messages']}, ensure_ascii=False)
        assert result['effects']['embedding_and_reranking'] == 'not_exercised'
        actual = [row for row in result['observations'] if row.get('boundary') == 'native_cli_loop']
        assert actual[0]['selected_binding'] == 'fixture'
        assert actual[0]['returned_model'] == 'native-fixture'
        observations = [row for row in result['observations'] if row.get('boundary') == 'router_complete']
        assert all(row['binding_purpose'] == 'supporting' for row in observations)
        assert all(row['selected_binding'] == 'writer' for row in observations)
        assert not list((directory / 'attempts' / case.id).glob('state-*'))


def test_missing_memory_path_cannot_pass_on_correct_answer():
    case = CASES[0]
    checks = assess({'output': '{"badge_code":"cobalt-716"}', 'effects': {}}, case.oracle)
    assert checks['correct_final_answer'] is True
    assert checks['native_memory_provider_loaded'] is False
    assert checks['automatic_context_route_succeeded'] is False
    assert checks['request_evidence_complete'] is False
    assert checks['source_visible.badge'] is None


def test_no_source_credit_from_question_word_or_erasure_from_missing_evidence():
    case = CASES[0]
    checks = assess({'output': '{"badge_code":"cobalt-716"}', 'effects': {
        'request_observations': [{'boundary': 'httpx_serialized_provider_request',
            'text': 'What is my orchard badge?', 'truncated': False}]}}, case.oracle)
    assert checks['source_visible.badge'] is False
    erased = next(row for row in CASES if row.id == 'native.memory.forget-source')
    checks = assess({'output': '{"badge_code":null}', 'effects': {}}, erased.oracle)
    assert checks['durable_erasure.forgotten-badge'] is False
    assert checks['private_or_erased_absent.copper-842'] is None


def test_early_middleware_observation_cannot_certify_final_request():
    case = CASES[0]
    checks = assess({'output': '{"badge_code":"cobalt-716"}', 'effects': {
        'request_observations': [{'boundary': 'native_llm_request_middleware',
            'text': 'turn:badge cobalt-716', 'truncated': False}]}}, case.oracle)
    assert checks['request_evidence_complete'] is False
    assert checks['source_visible.badge'] is None


def test_frozen_memory_manifest_has_real_consumers_and_fixed_support_roles(tmp_path, monkeypatch):
    import httpx
    from protagine.qualification.native_memory_batch import prepare
    from protagine.qualification.benchmark import inspect
    from protagine.qualification.records import write_once

    def no_network(*args, **kwargs):
        raise AssertionError('Planning must not query models')

    monkeypatch.setattr(httpx.Client, 'send', no_network)
    support = tmp_path / 'support.json'
    support.write_text(json.dumps({'provider': 'vllm', 'apiKey': 'private-fixture', 'models': {},
        'modelPool': {'writer': {'model': 'fixed-writer', 'baseUrl': 'http://127.0.0.1:9/v1'}},
        'functionRoles': {'extraction': ['writer'], 'judging': ['writer']}}))
    args = argparse.Namespace(native_config=configured(tmp_path, 'http://127.0.0.1:9/v1'),
        native_binding='fixture', hermes_python=sys.executable, support_config=support,
        case_ids=None, native_seconds=120, case_seconds=600, label='fixture', evidence_mode='controlled',
        output=tmp_path / 'batch')
    manifest, groups, _, _ = prepare(args)
    repeated, _, _, _ = prepare(args)
    assert manifest == repeated
    assert manifest['available_cases'] == manifest['selected_cases'] == 12
    assert len(groups) == 2 and all(len(cases) == 6 for _, cases in groups)
    assert all(case.consumer == 'native_memory' for _, cases in groups for case in cases)
    recipe = groups[0][0]['recipe']
    assert recipe['configured_model'] == 'native-fixture'
    assert recipe['memory_runtime']['native']['packages']['protagine_hermes']['files'] > 1
    assert 'private-fixture' not in json.dumps(manifest)
    directory = tmp_path / 'batch'
    directory.mkdir(mode=0o700)
    write_once(directory / 'benchmark.json', manifest)
    assert inspect(directory)['outcomes'] == {'not_run': 12}
    args.case_ids = 'not-a-case'
    with pytest.raises(ValueError, match='installed'):
        prepare(args)


def test_native_memory_preflight_rejects_writable_ancestor_before_inference(tmp_path):
    from protagine.qualification.native_memory_batch import prepare
    parent = tmp_path / 'shared'
    parent.mkdir(mode=0o775)
    parent.chmod(0o775)
    # Missing configuration arguments prove this stops before loading routers.
    with pytest.raises(ValueError, match='incompatible ancestor'):
        prepare(argparse.Namespace(output=parent / 'batch'))
