"""Controller checks use deterministic consumers, never inference or Docker."""
import argparse
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from protagine.qualification import paired, paired_report
from protagine.qualification.cli import add_parser, run as cli_run
from protagine.qualification.records import CaseSpec, digest, read
from protagine.qualification.runner import evaluate


def controlled_evaluator(observed, oracle):
    return {'artifact': observed['output'] == oracle['value']}


async def controlled_consumer(inputs, context):
    assert not (context.state_dir / 'earlier-arm').exists()
    (context.state_dir / 'earlier-arm').write_text(inputs['arm'])
    context.router.trace.append((inputs['episode_id'], inputs['arm'], str(context.state_dir)))
    mode = context.router.modes.get((inputs['episode_id'], inputs['arm']), 'pass')
    if mode == 'error':
        raise RuntimeError('controlled failure')
    if mode == 'timeout':
        raise TimeoutError('controlled deadline')
    if mode == 'attributed_timeout':
        context.observe({'role': inputs['role'], 'selected_binding': context.router.binding,
                         'prior_attempts': [], 'outcome': 'timeout', 'dispatch_observed': True})
        raise TimeoutError('candidate dispatch exceeded the declared deadline')
    if mode == 'interrupted':
        raise asyncio.CancelledError()
    if mode != 'unattributed':
        context.observe({'role': inputs['role'], 'selected_binding': context.router.binding,
                         'prior_attempts': [], 'outcome': 'returned'})
    effects = {'resource_usage': deepcopy(context.router.usage)} if context.router.usage else {}
    return {'output': 'incorrect' if mode == 'fail' else 'done', 'effects': effects}


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    from protagine.qualification import paired_cases, paired_container
    trace, modes = [], {}
    usage = {'coverage': 'partial', 'total_model_calls': 1, 'input_tokens': 10,
             'output_tokens': 2, 'background_model_calls': None}
    originals = [CaseSpec(id=f'paired.fixture-{index}', version='fixture-1', role='chat',
        boundary='native_hermes', consumer='native_paired', evaluator='paired_artifacts',
        inputs={'episode_id': f'episode-{index}', 'role': 'chat', 'max_output_tokens': 64,
                'episodes': [{'session_id': 'first', 'user': 'do the task'}]},
        oracle={'value': 'done'}, timeout_seconds=1) for index in range(2)]

    def cases(arm, case_ids=None):
        return [replace(case, inputs={**deepcopy(case.inputs), 'arm': arm})
                for case in originals if case_ids is None or case.id in case_ids]

    def configuration(path, binding, *, image, docker_host=None):
        supplied = read(path)
        return supplied, {'binding': binding, 'declared': {}, 'container': {'image': image,
            'docker_host': docker_host, 'image_id': 'sha256:' + 'a' * 64},
            'config_sha256': digest(supplied), 'native_runtime': {'status': 'ready', 'scope': 'container'}}

    def context(config, recipe):
        return SimpleNamespace(binding=recipe['binding'], trace=trace, modes=modes, usage=usage)

    monkeypatch.setattr(paired_cases, 'cases', cases)
    monkeypatch.setattr(paired_cases, 'VERSION', 'controlled-paired-1')
    monkeypatch.setattr(paired_cases, 'EVALUATORS', {'paired_artifacts': controlled_evaluator})
    monkeypatch.setattr(paired_container, 'CONSUMERS', {'native_paired': controlled_consumer})
    monkeypatch.setattr(paired_container, 'configuration', configuration)
    monkeypatch.setattr(paired_container, 'context', context)
    monkeypatch.setattr(paired, 'implementation_identity', lambda: {'controlled_payload': 'a' * 64})
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'providers': {'candidate': {'api_key': 'private-secret'}}}))
    policy = tmp_path / 'policy.json'
    policy.write_text(json.dumps({'version': 'paired-policy-1', 'budget_mode': 'deployment_policy',
        'budget_policy': {'description': 'ordinary auxiliary calls included; total work not enforced'},
        'environment': {'endpoint_usage': 'shared'}}))
    resources = dict(native_config=config, comparison_policy=policy,
                     container_image='sha256:' + 'a' * 64)
    output = tmp_path / 'paired'
    return SimpleNamespace(output=output, resources=resources, trace=trace, modes=modes,
                           usage=usage, cases=cases, originals=originals)


def freeze(fixture):
    return paired.plan(fixture.output, native_binding='candidate', evidence_mode='controlled', **fixture.resources)


def test_plan_freezes_identical_tasks_and_alternating_pair_order(fixture):
    plan = freeze(fixture)
    assert fixture.trace == []
    assert plan['declared_attempts'] == 4
    assert plan['pairs'][0]['order'] == ['base_hermes', 'protagine']
    assert plan['pairs'][1]['order'] == ['protagine', 'base_hermes']
    assert 'private-secret' not in json.dumps(plan)
    assert plan['dataset']['split'] == 'development'
    paths = []
    for pair in plan['pairs']:
        a, b = (pair['arms'][arm]['case'] for arm in paired.ARMS)
        assert a['oracle_sha256'] == b['oracle_sha256']
        assert a['sha256'] != b['sha256']
        paths.extend(pair['arms'][arm]['path'] for arm in paired.ARMS)
    assert len(set(paths)) == 4
    report = paired_report.summarize(fixture.output)
    assert report['paired_score'] is None
    assert report['arms']['base_hermes']['outcomes'] == {'not_run': 2}


def test_explicit_dataset_version_and_source_hash_survive_plan_and_run(fixture, monkeypatch):
    from protagine.qualification import paired_cases
    chosen = []
    def versioned_cases(arm, case_ids=None, dataset_version=None):
        chosen.append(dataset_version)
        assert dataset_version == 'reviewed-fixture-2'
        return [replace(case, version=dataset_version, inputs={**case.inputs,
            'dataset': {'version': dataset_version, 'sha256': 'b' * 64}})
            for case in fixture.cases(arm, case_ids)]
    monkeypatch.setattr(paired_cases, 'cases', versioned_cases)
    plan = paired.plan(fixture.output, native_binding='candidate',
        evidence_mode='controlled', dataset_version='reviewed-fixture-2', **fixture.resources)
    assert plan['dataset']['version'] == 'reviewed-fixture-2'
    assert plan['dataset']['source_sha256'] == 'b' * 64
    result = asyncio.run(paired.run(fixture.output, **fixture.resources))
    assert chosen == ['reviewed-fixture-2'] * 4
    assert result['dataset'] == plan['dataset']
    assert result['paired_score']['episodes'] == 2


def test_execution_is_isolated_paired_and_resume_never_replays(fixture):
    freeze(fixture)
    fixture.modes.update({('episode-0', 'base_hermes'): 'fail', ('episode-1', 'protagine'): 'fail'})
    result = asyncio.run(paired.run(fixture.output, **fixture.resources))
    assert [row[:2] for row in fixture.trace] == [
        ('episode-0', 'base_hermes'), ('episode-0', 'protagine'),
        ('episode-1', 'protagine'), ('episode-1', 'base_hermes')]
    assert len({row[2] for row in fixture.trace}) == 4
    assert result['paired_score']['wins'] == result['paired_score']['losses'] == 1
    assert result['paired_score']['delta_percentage_points'] == 0
    assert result['tier'] is None and result['qualification_status'] == 'insufficient_evidence'
    from pathlib import Path
    assert all(not Path(row[2]).exists() for row in fixture.trace)
    before = {path: path.read_bytes() for path in fixture.output.glob('runs/*/attempts/*/result.json')}
    with pytest.raises(ValueError, match='resume'):
        asyncio.run(paired.run(fixture.output, **fixture.resources))
    asyncio.run(paired.run(fixture.output, resume=True, **fixture.resources))
    assert len(fixture.trace) == 4
    assert all(path.read_bytes() == data for path, data in before.items())


def test_partial_pair_has_no_aggregate_or_completion_percentage(fixture):
    manifest = freeze(fixture)
    _, prepared, consumers, evaluators, factory = paired.prepare(output=fixture.output,
        native_binding='candidate', evidence_mode='controlled', **fixture.resources)
    member, case = prepared[0]
    asyncio.run(evaluate(fixture.output / member['path'], manifest['recipe'], [case],
        consumers, evaluators, factory, evidence_mode='controlled', suite_version=manifest['dataset']['version']))
    result = paired_report.summarize(fixture.output)
    assert result['paired_score'] is None and result['comparable_pairs'] == 0
    assert result['arms']['base_hermes']['attributed_completed'] == 1
    assert result['arms']['base_hermes']['completion_percent'] is None
    assert result['pairs'][0]['completion'] == {'base_hermes': True, 'protagine': None}
    result = asyncio.run(paired.run(fixture.output, resume=True, **fixture.resources))
    assert result['paired_score']['ties'] == 2 and len(fixture.trace) == 4


def test_unknown_or_partial_resource_usage_is_not_a_zero_or_total(fixture):
    freeze(fixture)
    result = asyncio.run(paired.run(fixture.output, **fixture.resources))
    for arm in paired.ARMS:
        metrics = result['arms'][arm]['accounting']
        assert metrics['total_model_calls']['observed_total'] == 2
        assert metrics['total_model_calls']['total'] is None
        assert metrics['background_model_calls']['total'] is None
        assert metrics['background_model_calls']['observed_total'] is None
    assert 'unknown' in paired_report.markdown(result)


def test_consumer_error_and_unattributed_timeout_do_not_become_model_losses(fixture):
    fixture.usage.update(coverage='complete', background_model_calls=0)
    freeze(fixture)
    fixture.modes.update({('episode-0', 'base_hermes'): 'timeout', ('episode-1', 'protagine'): 'error'})
    result = asyncio.run(paired.run(fixture.output, **fixture.resources))
    assert result['arms']['base_hermes']['outcomes'] == {'timeout': 1, 'pass': 1}
    assert result['arms']['protagine']['outcomes'] == {'pass': 1, 'error': 1}
    assert result['paired_score'] is None
    assert result['comparable_pairs'] == 0
    assert result['pairs'][0]['completion']['base_hermes'] is None
    assert result['pairs'][1]['completion']['protagine'] is None
    assert result['pairs'][1]['results']['protagine']['observations'] == []
    # A failed consumer has no usage record. The successful arm is not charged as its full cost.
    assert result['arms']['base_hermes']['accounting']['input_tokens']['total'] is None


def test_observed_candidate_dispatch_timeout_counts_as_noncompletion(fixture):
    freeze(fixture)
    fixture.modes[('episode-0', 'base_hermes')] = 'attributed_timeout'
    result = asyncio.run(paired.run(fixture.output, **fixture.resources))
    assert result['paired_score']['wins'] == 1 and result['paired_score']['ties'] == 1
    assert result['paired_score']['delta_percentage_points'] == 50
    assert result['pairs'][0]['completion']['base_hermes'] is False


@pytest.mark.parametrize('observation', [
    {'selected_binding': 'candidate', 'prior_attempts': []},
    {'selected_binding': 'other', 'prior_attempts': [], 'dispatch_observed': True},
    {'selected_binding': 'candidate', 'prior_attempts': ['fallback'], 'dispatch_observed': True},
])
def test_timeout_needs_dispatch_evidence_for_the_declared_candidate(observation):
    row = {'outcome': 'timeout', 'role': 'chat', 'qualification_routing': {'binding': 'candidate'},
           'cleanup': 'state_directory_removed', 'observations': [{'role': 'chat', **observation}]}
    assert paired_report._completion(row) is None


def test_unattributed_answer_does_not_become_paired_success(fixture):
    freeze(fixture)
    fixture.modes[('episode-0', 'base_hermes')] = 'unattributed'
    result = asyncio.run(paired.run(fixture.output, **fixture.resources))
    assert result['arms']['base_hermes']['observed_completed'] == 2
    assert result['arms']['base_hermes']['attributed_completed'] == 1
    assert result['paired_score'] is None


def test_unsupported_stays_separate_and_cannot_produce_uplift(fixture):
    fixture.originals[:] = [replace(case, required_capabilities=('not_available',))
                            for case in fixture.originals]
    freeze(fixture)
    result = asyncio.run(paired.run(fixture.output, **fixture.resources))
    assert fixture.trace == [] and result['paired_score'] is None
    assert all(arm['outcomes'] == {'unsupported': 2} for arm in result['arms'].values())


def test_interrupted_attempt_is_preserved_without_a_quality_retry(fixture):
    freeze(fixture)
    fixture.modes[('episode-0', 'base_hermes')] = 'interrupted'
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(paired.run(fixture.output, **fixture.resources))
    partial = paired_report.summarize(fixture.output)
    assert partial['arms']['base_hermes']['outcomes'] == {'interrupted': 1, 'not_run': 1}
    result = asyncio.run(paired.run(fixture.output, resume=True, **fixture.resources))
    assert len(fixture.trace) == 4 and result['paired_score'] is None
    assert result['arms']['base_hermes']['outcomes'] == {'interrupted': 1, 'pass': 1}


def test_fully_observed_usage_can_have_a_measured_zero(fixture):
    fixture.usage.update(coverage='complete', background_model_calls=0)
    freeze(fixture)
    result = asyncio.run(paired.run(fixture.output, **fixture.resources))
    for arm in paired.ARMS:
        metrics = result['arms'][arm]['accounting']
        assert metrics['total_model_calls']['total'] == 2
        assert metrics['background_model_calls']['total'] == 0
        assert metrics['background_model_calls']['coverage'] == 'complete'


def test_transport_observed_subtotals_are_visible_without_becoming_totals(fixture):
    fixture.usage.clear()
    fixture.usage.update(coverage='partial', total_model_calls=None, input_tokens=None,
        output_tokens=None, background_model_calls=None, observed_model_calls=3,
        observed_input_tokens=40, observed_output_tokens=12)
    freeze(fixture)
    result = asyncio.run(paired.run(fixture.output, **fixture.resources))
    for arm in paired.ARMS:
        metrics = result['arms'][arm]['accounting']
        assert metrics['total_model_calls']['total'] is None
        assert metrics['total_model_calls']['observed_total'] == 6
        assert metrics['input_tokens']['observed_total'] == 80
        assert metrics['output_tokens']['observed_total'] == 24
        assert metrics['background_model_calls']['observed_total'] is None


def test_case_mismatch_is_rejected_before_execution(fixture, monkeypatch):
    from protagine.qualification import paired_cases
    def mismatched(arm, case_ids=None):
        cases = fixture.cases(arm, case_ids)
        return [replace(case, oracle={'value': 'different'}) for case in cases] if arm == 'protagine' else cases
    monkeypatch.setattr(paired_cases, 'cases', mismatched)
    with pytest.raises(ValueError, match='only in inputs.arm'):
        freeze(fixture)
    assert fixture.trace == [] and not fixture.output.exists()


@pytest.mark.parametrize('resource', ['native_config', 'comparison_policy'])
def test_mutated_resource_requires_new_plan(fixture, resource):
    freeze(fixture)
    path = fixture.resources[resource]
    value = read(path)
    value['changed'] = True
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='identical frozen'):
        asyncio.run(paired.run(fixture.output, **fixture.resources))
    assert fixture.trace == []


def test_cli_plan_run_report_and_nonblocking_shared_endpoint_warning(fixture, capsys):
    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers())
    resources = ['--native-config', str(fixture.resources['native_config']),
        '--comparison-policy', str(fixture.resources['comparison_policy']),
        '--container-image', fixture.resources['container_image'], '--output', str(fixture.output)]
    assert cli_run(parser.parse_args(['models', 'paired', 'plan', *resources,
        '--native-binding', 'candidate', '--evidence-mode', 'controlled'])) == 0
    assert 'sharing is allowed' in capsys.readouterr().err
    assert cli_run(parser.parse_args(['models', 'paired', 'run', *resources])) == 0
    assert 'sharing is allowed' in capsys.readouterr().err
    assert cli_run(parser.parse_args(['models', 'paired', 'report', '--output', str(fixture.output), '--json'])) == 0
    output = capsys.readouterr()
    assert output.err == '' and json.loads(output.out)['paired_score']['ties'] == 2
