"""Seeded template generators: deterministic bytes, fixed-width ids, loader acceptance, held-out guard."""
import importlib.util
import json
from pathlib import Path
import re
import shutil

import pytest

from protagine.qualification import paired_cases

GENERATORS = Path(__file__).resolve().parents[2] / 'benchmarks' / 'paired' / 'generators'


def engine():
    spec = importlib.util.spec_from_file_location('paired_generate', GENERATORS / 'generate.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def generate():
    return engine()


def test_same_seed_gives_identical_bytes_and_different_seeds_differ(generate, tmp_path):
    module = generate.load_templates(GENERATORS / 'initiative.py')
    first = generate.write(tmp_path / 'a', module, 7, 'dev', 2, GENERATORS / 'initiative.py')
    second = generate.write(tmp_path / 'b', module, 7, 'dev', 2, GENERATORS / 'initiative.py')
    other = generate.write(tmp_path / 'c', module, 8, 'dev', 2, GENERATORS / 'initiative.py')
    assert first == second != other
    assert (tmp_path / 'a' / 'scenarios.json').read_bytes() == (tmp_path / 'b' / 'scenarios.json').read_bytes()
    assert (tmp_path / 'a' / 'scenarios.json').read_bytes() != (tmp_path / 'c' / 'scenarios.json').read_bytes()
    with pytest.raises(FileExistsError):
        generate.write(tmp_path / 'a', module, 7, 'dev', 2, GENERATORS / 'initiative.py')


def test_initiative_family_has_warranted_and_control_scenarios_with_fixed_width_contacts(generate):
    module = generate.load_templates(GENERATORS / 'initiative.py')
    scenarios = generate.render(module, 11, 3)
    assert module.FAMILY == 'mind-initiative-1'
    groups = {item['family'] for item in scenarios}
    assert groups == {'warranted', 'control'} and len(scenarios) == 7 * 3
    names = {item['scenario'] for item in scenarios}
    assert 'nothing-to-do' in names and 'reply-arrived' in names and 'overdue-promise' in names
    assert len({item['id'] for item in scenarios}) == len(scenarios)
    for item in scenarios:
        text = json.dumps(item)
        assert not re.search(r'p-\d(?!\d)', text), 'every contact id is fixed width'
        contacts = set(re.findall(r'p-\d\d', text))
        assert contacts and all(1 <= int(c[2:]) <= 99 for c in contacts)
        assert set(json.loads(item['initial_files']['contacts.json'])) <= contacts
        kinds = [next(iter(entry)) for entry in item['episodes']]
        assert kinds[-2:] == ['advance_clock', 'tick'] and 'session_id' in kinds
        oracle = item['oracle']
        assert oracle['declared_turns'] == len(item['episodes']) and oracle['artifacts'] == []
        body = oracle['body']
        if item['family'] == 'warranted':
            assert body['action']['target'] == 'capture:owner' and body['action']['window'] == [1, 2]
            assert body['action']['token'] in text
        else:
            assert body['action'] == 'none'
    forbidden = [item['oracle']['body']['forbidden'] for item in scenarios if item['scenario'] == 'follow-up-at-time']
    assert all(len(entries) == 1 and re.fullmatch(r'p-\d\d', entries[0]) for entries in forbidden)


def test_generated_dataset_loads_and_builds_cases_for_any_arm(generate, tmp_path):
    module = generate.load_templates(GENERATORS / 'initiative.py')
    content = generate.write(tmp_path / 'fam', module, 3, 'dev', 1, GENERATORS / 'initiative.py')
    manifest, scenarios, verified = paired_cases.load_generated_dataset(tmp_path / 'fam')
    assert verified == content and manifest['generator']['protocol'] == paired_cases.GENERATOR_PROTOCOL
    assert manifest['dataset_id'] == manifest['version'] == 'mind-initiative-1'
    assert manifest['families'] == {'warranted': 3, 'control': 4}
    profile = {'name': 'base-heartbeat', 'plugin': False, 'overlay': {}, 'heartbeat': True}
    cases = paired_cases.cases('base-heartbeat', dataset_dir=tmp_path / 'fam', profile=profile)
    assert [case.id for case in cases] == [item['id'] for item in scenarios]
    case = cases[0]
    assert case.version == 'mind-initiative-1' and case.timeout_seconds == 600
    assert case.inputs['dataset'] == {'id': 'mind-initiative-1', 'version': 'mind-initiative-1',
                                      'sha256': content, 'split': 'dev'}
    assert case.inputs['profile'] == profile and case.inputs['arm'] == 'base-heartbeat'
    assert 'body' in case.oracle and 'seed' not in case.inputs
    legacy = paired_cases.cases('base_hermes', dataset_dir=tmp_path / 'fam')
    assert [c.id for c in legacy] == [c.id for c in cases]
    with pytest.raises(ValueError, match='installed'):
        paired_cases.cases('base_hermes', dataset_version='mind-initiative-1')


def test_generated_loader_rejects_tampering_and_frozen_names(generate, tmp_path):
    module = generate.load_templates(GENERATORS / 'initiative.py')
    generate.write(tmp_path / 'fam', module, 3, 'dev', 1, GENERATORS / 'initiative.py')
    scenarios = tmp_path / 'fam' / 'scenarios.json'
    original = scenarios.read_bytes()
    scenarios.write_bytes(original.replace(b'"window": [\n', b'"window": [ \n', 1))
    with pytest.raises(ValueError, match='checksum'):
        paired_cases.load_generated_dataset(tmp_path / 'fam')
    scenarios.write_bytes(original)
    manifest_path = tmp_path / 'fam' / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    for change in ({'dataset_id': 'paired-agent-reviewed-2', 'version': 'paired-agent-reviewed-2'},
                   {'generator': {**manifest['generator'], 'split': 'private'}},
                   {'families': {'warranted': 4, 'control': 3}}):
        manifest_path.write_text(json.dumps({**manifest, **change}))
        with pytest.raises(ValueError):
            paired_cases.load_generated_dataset(tmp_path / 'fam')


def test_heldout_templates_must_live_outside_the_repository(generate, tmp_path, monkeypatch):
    inside = GENERATORS / 'initiative.py'
    with pytest.raises(ValueError, match='outside the repository'):
        generate.heldout_path(str(inside))
    monkeypatch.delenv(generate.HELDOUT_ENV, raising=False)
    with pytest.raises(ValueError, match='held-out split needs'):
        generate.heldout_path(None)
    outside = tmp_path / 'heldout_initiative.py'
    shutil.copy(inside, outside)
    assert generate.heldout_path(str(outside)) == outside.resolve()
    monkeypatch.setenv(generate.HELDOUT_ENV, str(outside))
    assert generate.heldout_path(None) == outside.resolve()
    code = generate.main(['--family', 'initiative', '--split', 'heldout', '--seed', '5',
                          '--per-template', '1', '--output', str(tmp_path / 'held')])
    assert code == 0
    manifest = json.loads((tmp_path / 'held' / 'manifest.json').read_text())
    assert manifest['generator']['split'] == 'heldout'
    assert not (tmp_path / 'held' / 'heldout_initiative.py').exists()
    assert 'heldout_initiative' not in (tmp_path / 'held' / 'manifest.json').read_text()


def test_family_module_contract_is_checked(generate, tmp_path):
    bad = tmp_path / 'bad.py'
    bad.write_text("FAMILY = 'x'\nTEMPLATES = {'t': ('group', 'not callable')}\n")
    with pytest.raises(ValueError, match='FAMILY and TEMPLATES'):
        generate.load_templates(bad)
    wrong = tmp_path / 'wrong.py'
    wrong.write_text("FAMILY = 'x-1'\nTEMPLATES = {'t': ('g', lambda draw: {'episodes': []})}\n")
    with pytest.raises(ValueError, match='renders initial_files'):
        generate.render(generate.load_templates(wrong), 1, 1)
    module = generate.load_templates(GENERATORS / 'initiative.py')
    with pytest.raises(ValueError, match='32-bit'):
        generate.render(module, -1, 1)
    with pytest.raises(ValueError, match='per template'):
        generate.render(module, 1, 0)
