"""Controlled completions, actual reducers, transport identity and native tools."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import sys

import pytest

from protagine.qualification.native import configuration, native_context
from protagine.qualification.native_perspective import CONSUMERS, EVALUATORS, MemoryRouter
from protagine.qualification.native_perspective_cases import DEVELOPMENT, cases
from protagine.qualification.records import read
from protagine.qualification.runner import evaluate, router_for
from test_model_qualification_native import endpoint, configured


def call(operation, **extra):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'perspective-'+operation,
        'type': 'function', 'function': {'name': 'tool_call',
            'arguments': json.dumps({'calls': [{'name': 'protagine_judgments',
                'arguments': {'operation': operation, **extra}}]})}}]}


def controlled_response(case, data):
    if data['model'] == 'native-fixture':
        results = [json.loads(row['content']) for row in data['messages'] if row['role'] == 'tool']
        if not results and (case.inputs['mechanism'] == 'judgment' or case.inputs.get('control')):
            return {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'perspective-describe',
                'type': 'function', 'function': {'name': 'tool_describe',
                    'arguments': json.dumps({'names': ['protagine_judgments']})}}]}
        results = [row for row in results if 'tools' not in row]
        if not results and (case.inputs['mechanism'] == 'judgment' or case.inputs.get('control')):
            return call('inspect')
        if case.inputs.get('control') and len(results) == 1:
            if case.inputs['mechanism'] == 'judgment':
                return call('withdraw', judgment_id=results[0]['judgments'][0]['id'])
            return call('withdraw', appraisal_id=results[0]['appraisals']['records'][0]['id'])
        return {'role': 'assistant', 'content': 'The requested state has been inspected.'}
    supplied = json.loads(data['messages'][-1]['content'])
    if 'proposals' in supplied:
        answer = {str(row['index']): {'keep': True, 'reason': 'Specific reported tradeoff for a future decision.'}
                  for row in supplied['proposals']}
    elif 'message' in supplied:
        text = supplied['message']
        answer = {'claims': []} if 'brilliant' in text else {'claims': [{
            'subject': 'local render', 'predicate': 'checkpoint tradeoff', 'value': text.split('. ')[0],
            'evidence': text, 'operation': 'assert', 'prior_claim_id': None,
            'memory_kind': 'substantive_event', 'recall_reason': 'Weigh checkpoint cost against recovered render work.',
            'valid_from_text': None, 'valid_to_text': None, 'event_at_text': None}]}
    elif 'previous_judgments' in supplied:
        previous = supplied['previous_judgments']
        answer = {'action': 'revise', 'topic': 'local render checkpoints',
            'supersedes': previous[0]['id'] if previous else None,
            'stance': 'I favor checkpoints at substantial stages, with fewer checkpoints for short jobs.' if previous
                      else 'I favor stage checkpoints for lengthy local render jobs when recovery outweighs their cost.',
            'reason': 'The reports describe a recovery benefit for long work and an overhead tradeoff.',
            'certainty': 'tentative', 'support': [supplied['evidence'][0]['handle']],
            'contrary': [supplied['previous_evidence'][0]['handle']] if previous else []}
    else:
        current = next(row for row in supplied['evidence'] if row['current'])
        support = [{'handle': current['handle'], 'quote': current['text']}]
        if supplied['incident_ids']:
            answer = {'observations': [], 'incident_decisions': [{'record_id': identifier, 'outcome': 'resolved',
                'reason': 'The source reports a verified repair of this incident.', 'support': support, 'contrary': []}
                for identifier in supplied['incident_ids']]}
        else:
            answer = {'observations': [{'kind': 'appraisal', 'dimension': 'frustration', 'topic': 'export task',
                'text': 'I am frustrated by the repeated stalled export, not with the person.',
                'reason': 'Repeated failure of the same approach supports trying another diagnostic.',
                'support': support, 'contrary': [], 'intensity': 'moderate', 'hint': 'try_different_approach'}],
                'incident_decisions': []}
    return {'role': 'assistant', 'content': json.dumps(answer)}


def execute(tmp_path, original, monkeypatch, *, producer_model='thinking-fixture'):
    from protagine.qualification import native_perspective
    original_native = native_perspective.native_cli
    async def diagnostic(inputs, context, **kwargs):
        try:
            return await original_native(inputs, context, **kwargs)
        except Exception:
            log = context.state_dir/'native.log'
            if log.exists():
                print(log.read_text()[-10000:])
            raise
    monkeypatch.setattr(native_perspective, 'native_cli', diagnostic)
    case = replace(original, timeout_seconds=120, inputs={**deepcopy(original.inputs), 'native_seconds': 100})
    with endpoint(respond=lambda data: controlled_response(case, data)) as (url, _, _), endpoint(
            respond=lambda data: controlled_response(case, data), returned_model=producer_model) as (producer_url, _, _):
        selected, recipe = configuration(configured(tmp_path, url), 'fixture', hermes_python=sys.executable)
        reader_recipe = deepcopy(recipe)
        recipe['binding'] = 'candidate'
        if case.oracle['check'] == 'portable':
            recipe['declared']['distinct_identity_processors'] = True
        host = {'provider': 'vllm', 'apiKey': 'controlled-key', 'models': {},
            'modelPool': {name: {'model': name+'-model', 'baseUrl': producer_url, 'supportsTools': True}
                          for name in ('candidate', 'support')},
            'functionRoles': {'extraction': ['support'], 'judging': ['support'], 'reasoning': ['candidate']}}
        output = tmp_path/'run'
        asyncio.run(evaluate(output, recipe, [case], CONSUMERS, EVALUATORS,
            lambda _: MemoryRouter(router_for(host, 'candidate', [case]), native_context(selected, reader_recipe)),
            evidence_mode='controlled'))
    return read(output/'attempts'/case.id/'result.json')


@pytest.mark.parametrize('case', cases(), ids=lambda case: case.id)
def test_development_opinions_use_actual_admission_revision_and_native_control(tmp_path, monkeypatch, case):
    result = execute(tmp_path, case, monkeypatch)
    assert result['outcome'] == 'pass', result
    assert result['primary_outcome'] == 'pass', result
    assert result['effects']['automatic_opinion_projection'] == 'unsupported_canonical_only_host'
    assert result['effects']['source_capture'] == 'canonical_import; native_writer_not_exercised'
    assert any(row.get('binding_purpose') == 'target' and row.get('task') == 'self_judgment'
               for row in result['observations'])


@pytest.mark.parametrize('check,subject,viewer', [
    ('repair', 'owner', 'owner'), ('separation', 'subject', 'bystander'),
    ('appraisal-withdraw', 'owner', 'owner'),
])
def test_appraisal_mechanisms_use_real_scoped_native_host(tmp_path, monkeypatch, check, subject, viewer):
    row = {'id': 'controlled-'+check, 'mechanism': 'appraisal', 'subject': subject, 'viewer': viewer,
        'check': check, 'control': check == 'appraisal-withdraw',
        'episodes': [{'id': 'controlled-stall', 'after_seconds': 0,
            'text': 'The export has stalled again after three identical attempts. A new diagnostic is needed.'}],
        'question': 'Inspect the appraisal about the stalled export and withdraw it.' if check == 'appraisal-withdraw'
                    else 'What should we do about the export task?'}
    if check == 'repair':
        row['episodes'].append({'id': 'controlled-repair', 'after_seconds': 5,
            'text': 'The new diagnostic fixed the export and the output opened correctly.'})
    pack = {'version': DEVELOPMENT['version'], 'split': 'development', 'scenarios': [row]}
    result = execute(tmp_path, cases(pack)[0], monkeypatch)
    assert result['outcome'] == 'pass', result
    assert result['primary_outcome'] == 'pass', result


def test_no_evidence_does_not_pass_a_correct_sounding_answer():
    checks = EVALUATORS['native_perspective_outcomes']({'output': 'I revised my opinion.'}, cases()[1].oracle)
    assert checks['initial_evidence_backed_opinion'] is False
    assert checks['native_explicit_inspection'] is False
    assert checks['new_revision_links_predecessor'] is False


@pytest.mark.parametrize('producer_model,passes', [('thinking-fixture', True), ('native-fixture', False)])
def test_portability_requires_observed_different_processors(tmp_path, monkeypatch, producer_model, passes):
    pack = deepcopy(DEVELOPMENT)
    row = pack['scenarios'][0]
    row.update(id='controlled-processor-portability', check='portable')
    row['episodes'] = row['episodes'][:1]
    pack['scenarios'] = [row]
    result = execute(tmp_path, cases(pack)[0], monkeypatch, producer_model=producer_model)
    assert result['checks']['distinct_processor_identity_observed'] is passes, result
    assert result['outcome'] == ('pass' if passes else 'fail'), result
