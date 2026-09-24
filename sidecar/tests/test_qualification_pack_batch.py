"""Portable CLI orchestration; controlled local HTTP only, no candidate calls."""
import argparse
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

from protagine.qualification import benchmark, pack_batch
from protagine.qualification.cli import add_parser, run as cli_run
from protagine.qualification.pack_registry import PACKS
from protagine.qualification.records import digest, read
from test_function_routing import config, endpoint


def write_json(path, value):
    path.write_text(json.dumps(value))
    return path


def host_config(first, second):
    value = config(first, second, timeoutSeconds=10, deadlineSeconds=20)
    value['functionRoles']['judging'] = ['deliberate']
    return value


@pytest.fixture
def resources(tmp_path, monkeypatch):
    """Mock resource inventory only; factories, configuration and hashes are real."""
    monkeypatch.setattr('protagine.qualification.native._runtime', lambda *_: {
        'status': 'ready', 'python': sys.executable, 'native_payload_sha256': 'a'*64})
    monkeypatch.setattr(pack_batch, '_native_packages', lambda _: {
        'packages': {'protagine_hermes': {'sha256': 'b'*64}},
        'distribution_versions': {'lancedb': 'fixture', 'pyarrow': 'fixture'}})
    monkeypatch.setattr(pack_batch, '_sandbox_resource', lambda _: {'installed_image_id': 'sha256:'+'c'*64})
    values = {
        'config': host_config('http://127.0.0.1:9987/v1', 'http://127.0.0.1:9988/v1'),
        'native_config': {'providers': {'reader': {'base_url': 'http://127.0.0.1:9989/v1',
            'default_model': 'fixed-native', 'api_key': 'native-private-secret'}}},
        'support_config': host_config('http://127.0.0.1:9990/v1', 'http://127.0.0.1:9991/v1'),
        'retrieval_config': {'embedding': {'base_url': 'http://127.0.0.1:9992/v1',
            'model': 'fixture-embed', 'dimensions': 4},
            'reranker': {'base_url': 'http://127.0.0.1:9993/v1', 'model': 'fixture-rerank', 'cutoff': -1}},
        'sandbox_config': {'image': 'sha256:'+'c'*64, 'docker_host': None},
    }
    return {name: write_json(tmp_path / (name+'.json'), value) for name, value in values.items()}


def options(pack, resources):
    spec = PACKS[pack]
    result = {'pack': pack, 'evidence_mode': 'controlled'}
    if spec.mode in {'host', 'perspective', 'router'}:
        result.update(config=resources['config'], binding='interactive')
    if spec.mode in {'native', 'perspective', 'semantic'}:
        result.update(native_config=resources['native_config'], native_binding='reader', hermes_python=Path(sys.executable))
    if spec.sandbox:
        result['sandbox_config'] = resources['sandbox_config']
    if spec.mode == 'semantic':
        result.update(support_config=resources['support_config'], retrieval_config=resources['retrieval_config'])
    if spec.mode == 'router':
        result['fallback_binding'] = 'deliberate'
    return result


@pytest.mark.parametrize('name,count', [
    ('formation', 4), ('perspective', 2), ('planning', 2), ('recovery', 6), ('semantic', 6),
    ('authority', 4), ('identity-audience', 5), ('interactive', 11), ('unified', 6),
    ('router-recovery', 2), ('evidence', 15), ('interaction', 5),
])
def test_all_registered_development_packs_preserve_cases_and_boundaries(tmp_path, resources, name, count):
    output = tmp_path / name
    manifest = pack_batch.plan(output, **options(name, resources))
    assert manifest['selected_cases'] == manifest['available_cases'] == count
    assert manifest['stage'] == 'development' and manifest['fixture'] is None
    assert len({case['case_id'] for case in manifest['cases']}) == count
    assert all(case['split'] == 'development' and case['provenance'] == 'public' for case in manifest['cases'])
    assert all(shard['declared_seconds'] <= 3600 and shard['declared_cases'] <= 128 for shard in manifest['shards'])
    assert sum(shard['declared_seconds'] for shard in manifest['shards']) == manifest['declared_seconds']
    assert benchmark.inspect(output)['outcomes'] == {'not_run': count}
    assert 'native-private-secret' not in json.dumps(manifest)
    if name == 'unified':
        assert [case['comparison_arm'] for case in manifest['cases']].count('base_hermes') == 2


def test_real_cli_runs_existing_evaluator_then_resumes_without_replaying(tmp_path, capsys):
    from protagine.qualification.evidence_cases import GROUNDING
    case = GROUNDING[0]
    answer = {'combined_completed': 99, 'combined_attempts': 120, 'combined_completion_fraction': 0.825,
              'faster_service': None, 'lowest_energy_service': None}
    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers())
    with endpoint(content=json.dumps(answer)) as (url, calls):
        host = write_json(tmp_path/'host.json', host_config(url, url))
        batch = tmp_path/'batch'
        common = ['--config', str(host), '--output', str(batch)]
        args = parser.parse_args(['models', 'packs', 'plan', *common, '--pack', 'evidence',
            '--binding', 'interactive', '--case-ids', case.id, '--evidence-mode', 'controlled'])
        assert cli_run(args) == 0 and calls == []
        inspect_args = parser.parse_args(['models', 'packs', 'inspect', '--output', str(batch)])
        assert cli_run(inspect_args) == 0 and calls == []
        args = parser.parse_args(['models', 'packs', 'run', *common])
        assert cli_run(args) == 0 and len(calls) == 1
        receipt = batch / 'runs/evidence-001/attempts' / case.id / 'result.json'
        original = receipt.read_bytes()
        assert read(receipt)['primary_outcome'] == 'pass'
        assert calls[0]['payload']['messages'] == case.inputs['messages']
        with pytest.raises(ValueError, match='resume'):
            cli_run(args)
        args.resume = True
        assert cli_run(args) == 0 and len(calls) == 1 and receipt.read_bytes() == original
        assert len(list(batch.glob('report-*.json'))) == 2
        assert read(batch/'benchmark.json')['options']['case_ids'] == [case.id]
    capsys.readouterr()


def test_formation_executes_real_ledger_with_independent_supporting_reviewer(tmp_path):
    from test_qualification_formation_extended import assertion
    def extract(payload):
        source = json.loads(payload['messages'][-1]['content'])['message']
        return json.dumps([assertion(source, 'I', 'observatory badge code', 'moss-738')])
    def review(payload):
        proposals = json.loads(payload['messages'][-1]['content'])['proposals']
        return json.dumps({str(row['index']): {'keep': True, 'reason': 'Source-grounded synthetic detail.'} for row in proposals})
    with endpoint(content=extract) as (candidate, candidate_calls), endpoint(content=review) as (judge, judge_calls):
        cfg = host_config(candidate, judge)
        cfg['taskRoles'] = {'source_claim_extraction': 'reasoning'}
        path = write_json(tmp_path/'host.json', cfg)
        batch = tmp_path/'batch'
        case_id = 'formation.duplicate-delivery-idempotence'
        pack_batch.plan(batch, pack='formation', config=path, binding='interactive',
                        case_ids=[case_id], evidence_mode='controlled')
        result = asyncio.run(pack_batch.run(batch, config=path))
        assert result['outcomes'] == {'pass': 1}
        receipt = read(batch/'runs/formation-001/attempts'/case_id/'result.json')
        assert receipt['primary_outcome'] == 'pass'
        assert receipt['effects']['replay']['extra_model_calls'] == 0
        assert [(row['role'], row['binding_purpose'], row['selected_binding'])
                for row in receipt['observations']] == [
            ('extraction', 'target', 'interactive'), ('judging', 'supporting', 'deliberate')]
        assert len(candidate_calls) == len(judge_calls) == 1
        assert read(path) == cfg


def test_router_fallback_remains_system_success_not_primary_pass(tmp_path):
    with endpoint(content='ochre') as (primary, primary_calls), endpoint(content='ochre') as (fallback, fallback_calls):
        path = write_json(tmp_path/'host.json', host_config(primary, fallback))
        batch = tmp_path/'batch'
        case_id = 'router.recovery.service-error-fallback'
        frozen = pack_batch.plan(batch, pack='router-recovery', config=path, binding='interactive',
            fallback_binding='deliberate', case_ids=[case_id], evidence_mode='controlled')
        result = asyncio.run(pack_batch.run(batch, config=path))
        assert result['outcomes'] == {'pass': 1} and result['runs'][0]['primary_passes'] == 0
        assert frozen['shards'][0]['recipe']['supporting_fallback_recipe']['binding'] == 'deliberate'
        receipt = read(batch/'runs/router-recovery-001/attempts'/case_id/'result.json')
        assert receipt['primary_outcome'] == 'fail'
        assert not primary_calls and len(fallback_calls) == 1


def test_changed_config_and_implementation_rejected_before_inference(tmp_path, monkeypatch):
    with endpoint(content='{}') as (url, calls):
        cfg = host_config(url, url)
        host = write_json(tmp_path/'host.json', cfg)
        batch = tmp_path/'batch'
        pack_batch.plan(batch, pack='evidence', config=host, binding='interactive', case_ids=['G08.unequal-denominators'])
        changed = deepcopy(cfg)
        changed['modelPool']['interactive']['maxTokens'] = 2048
        write_json(host, changed)
        with pytest.raises(ValueError, match='identical frozen'):
            asyncio.run(pack_batch.run(batch, config=host))
        write_json(host, cfg)
        identity = pack_batch.implementation_identity()
        monkeypatch.setattr(pack_batch, 'implementation_identity', lambda: {**identity, 'changed': 'oracle'})
        with pytest.raises(ValueError, match='identical frozen'):
            asyncio.run(pack_batch.run(batch, config=host))
        assert calls == [] and not (batch/'runs').exists()


def test_fixture_is_explicit_hash_checked_private_and_not_autodiscovered(tmp_path, resources):
    from protagine.qualification.evidence_cases import VERSION
    document = {'version': VERSION, 'fixtures': [{'identity': 'heldout.synthetic-evidence', 'role': 'reasoning',
        'document': 'The synthetic marker is pewter.', 'question': 'Return marker.', 'expected': {'marker': 'pewter'}}]}
    path = write_json(tmp_path/'fixture.json', document)
    opts = options('evidence', resources)
    development, *_ = pack_batch.prepare(output=tmp_path/'dev', **opts)
    assert all(case['provenance'] == 'public' for case in development['cases'])
    with pytest.raises(ValueError, match='fixture-sha256'):
        pack_batch.plan(tmp_path/'missing', fixture_pack=path, **opts)
    with pytest.raises(ValueError, match='canonical JSON hash'):
        pack_batch.plan(tmp_path/'wrong', fixture_pack=path, fixture_sha256='0'*64, **opts)
    held = tmp_path/'held'
    frozen = pack_batch.plan(held, fixture_pack=path, fixture_sha256=digest(document), **opts)
    assert frozen['stage'] == 'held_out' and frozen['selected_cases'] == 1
    assert frozen['cases'][0]['provenance'] == 'private'
    assert 'pewter' not in json.dumps(frozen)
    with pytest.raises(ValueError, match='explicit --fixture-pack'):
        asyncio.run(pack_batch.run(held, config=resources['config']))
    document['fixtures'][0]['expected']['marker'] = 'silver'
    write_json(path, document)
    with pytest.raises(ValueError, match='canonical JSON hash'):
        asyncio.run(pack_batch.run(held, config=resources['config'], fixture_pack=path))
    assert not (held/'runs').exists()


def test_supporting_routes_and_native_reader_stay_independent(tmp_path, resources):
    cfg = read(resources['config'])
    cfg['taskRoles'] = {'source_claim_extraction': 'extraction', 'source_claim_review': 'judging'}
    write_json(resources['config'], cfg)
    before = resources['config'].read_bytes()
    manifest, groups, _, _, factory = pack_batch.prepare(output=tmp_path/'perspective', **options('perspective', resources))
    shard, cases = groups[0]
    context = factory(shard, cases[0])
    assert context.binding == 'reader' and context.native_config['model']['default'] == 'fixed-native'
    status = context.routing_status()
    assert status['roles']['reasoning'] == ['interactive']
    assert status['roles']['extraction'] == ['interactive', 'deliberate']
    assert status['roles']['judging'] == ['deliberate']
    assert status['task_roles']['source_claim_extraction'] == 'extraction'
    assert status['task_roles']['source_claim_review'] == 'judging'
    assert status['task_roles']['source_appraisal'] == 'reasoning'
    recipe = manifest['shards'][0]['recipe']
    assert recipe['binding'] == 'interactive' and recipe['fixed_reader_recipe']['binding'] == 'reader'
    assert resources['config'].read_bytes() == before
    _, groups, _, _, factory = pack_batch.prepare(output=tmp_path/'semantic', **options('semantic', resources))
    context = factory(groups[0][0], groups[0][1][0])
    assert context.binding == 'reader' and context.routing_status()['roles']['judging'] == ['deliberate']
    cfg['taskRoles']['source_claim_extraction'] = 'reasoning'
    write_json(resources['config'], cfg)
    with pytest.raises(ValueError, match='shares the pinned candidate role'):
        pack_batch.prepare(output=tmp_path/'confounded', **options('perspective', resources))


@pytest.mark.parametrize('missing', ['native_config', 'support_config', 'retrieval_config'])
def test_missing_semantic_resource_fails_before_output_or_inference(tmp_path, resources, missing):
    opts = options('semantic', resources)
    del opts[missing]
    with pytest.raises(ValueError, match='requires'):
        pack_batch.plan(tmp_path/'batch', **opts)
    assert not (tmp_path/'batch').exists()


def test_unavailable_runtime_support_or_sandbox_fail_before_attempts(tmp_path, resources, monkeypatch):
    monkeypatch.setattr('protagine.qualification.native._runtime', lambda *_: {'status': 'unavailable'})
    with pytest.raises(ValueError, match='interpreter is unavailable'):
        pack_batch.plan(tmp_path/'native', **options('interactive', resources))
    cfg = read(resources['config']); cfg['functionRoles']['judging'] = []
    write_json(resources['config'], cfg)
    with pytest.raises(ValueError, match='supporting role'):
        pack_batch.plan(tmp_path/'formation', **options('formation', resources))
    assert not (tmp_path/'native').exists() and not (tmp_path/'formation').exists()


def test_docker_preflight_never_pulls_or_creates(tmp_path, monkeypatch):
    observed = []
    image = 'sha256:'+'7'*64
    def execute(command, **kwargs):
        observed.append(command)
        assert 'DOCKER_CONTEXT' not in kwargs['env']
        assert kwargs['env']['DOCKER_HOST'] == 'unix:///synthetic/docker.sock'
        return type('Result', (), {'stdout': image+'\n'})()
    monkeypatch.setattr(pack_batch.subprocess, 'run', execute)
    assert pack_batch._sandbox_resource({'image': image, 'docker_host': 'unix:///synthetic/docker.sock'}) == {
        'installed_image_id': image}
    assert observed == [['docker', 'image', 'inspect', image, '--format', '{{.Id}}']]
    def missing(*_, **__):
        raise FileNotFoundError('synthetic missing docker')
    monkeypatch.setattr(pack_batch.subprocess, 'run', missing)
    with pytest.raises(ValueError, match='already installed image'):
        pack_batch._sandbox_resource({'image': image, 'docker_host': None})


def test_identity_covers_new_oracles_workers_and_image_payloads(monkeypatch):
    before = pack_batch.implementation_identity()
    original = Path.read_bytes
    for target in ('native_identity_cases.py', 'native_interactive_worker.py', 'interaction-frame-a.png'):
        monkeypatch.setattr(Path, 'read_bytes', lambda path, target=target:
            original(path) + (b'changed' if path.name == target else b''))
        assert pack_batch.implementation_identity()['qualification_payload_sha256'] != before['qualification_payload_sha256']
    monkeypatch.setattr(Path, 'read_bytes', original)


def test_bad_case_ids_and_unused_resources_are_not_silently_ignored(tmp_path, resources):
    opts = options('evidence', resources)
    for identifiers in ([], ['missing'], ['G08.unequal-denominators']*2):
        with pytest.raises(ValueError, match='case IDs|Case IDs'):
            pack_batch.prepare(output=tmp_path/'batch', case_ids=identifiers, **opts)
    with pytest.raises(ValueError, match='does not use --support-config'):
        pack_batch.prepare(output=tmp_path/'batch', support_config=resources['support_config'], **opts)
    with pytest.raises(ValueError, match='fallback-binding'):
        pack_batch.prepare(output=tmp_path/'batch', fallback_binding='deliberate', **opts)
