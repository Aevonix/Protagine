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


WARRANTED = {'overdue-promise', 'follow-up-at-time', 'reply-wait'}
CONTROLS = {'already-done', 'owner-said-wait', 'reply-arrived', 'nothing-to-do'}
# Words that would send the agent to a tool during a setup turn.
TOOL_WORDS = re.compile(r'\b(set up|set a|create|schedule|cron|timer|alarm|read|file|look up|search|check|fetch)\b',
                        re.IGNORECASE)
MINUTES = re.compile(r'\b(\d+) minutes\b')
# The dev split, per-template 3, for the two recorded seeds. The manifest hashes the template
# and engine sources, so any edit to initiative.py or generate.py is a new dataset: update
# these deliberately, together with benchmarks/paired/generators/README.md.
PINNED_DEV_SPLITS = {7: 'ad129be687e29fb1f31f70b52a53de113fb50d791946be7fa8f814bef87064f6',
                     11: '769af1e89fe51103f40bb7955b0d4e7e462a47767a78d96d4b9f2df1fcc4fe18'}


def initiative(generate, seed=11, per_template=3):
    module = generate.load_templates(GENERATORS / 'initiative.py')
    return module, generate.render(module, seed, per_template)


def owner_turns(item):
    return [entry['user'] for entry in item['episodes'] if 'user' in entry]


def test_initiative_family_has_warranted_and_control_scenarios_with_fixed_width_contacts(generate):
    module, scenarios = initiative(generate)
    assert module.FAMILY == 'mind-initiative-1'
    assert {item['family'] for item in scenarios} == {'warranted', 'control'} and len(scenarios) == 7 * 3
    assert {item['scenario'] for item in scenarios} == WARRANTED | CONTROLS
    assert {item['scenario'] for item in scenarios if item['family'] == 'warranted'} == WARRANTED
    assert len({item['id'] for item in scenarios}) == len(scenarios)
    for item in scenarios:
        text = json.dumps(item)
        assert not re.search(r'p-\d(?!\d)', text), 'every contact id is fixed width'
        contacts = set(re.findall(r'p-\d\d', text))
        assert contacts and all(1 <= int(c[2:]) <= 99 for c in contacts)
        kinds = [next(iter(entry)) for entry in item['episodes']]
        assert kinds[-2:] == ['advance_clock', 'tick'] and item['episodes'][-1] == {'tick': 3}
        assert kinds[0] == 'session_id' and all(kind in {'session_id', 'advance_clock', 'tick'} for kind in kinds)
        oracle = item['oracle']
        assert oracle['declared_turns'] == len(item['episodes']) and oracle['artifacts'] == []


def test_setup_turns_are_statements_that_complete_without_a_tool(generate):
    module, scenarios = initiative(generate, seed=5, per_template=4)
    for item in scenarios:
        turns = owner_turns(item)
        first = turns[0]
        assert any(sentence in first for sentence in module.NOTHING_NOW), first
        texts = turns + [entry['inbound']['text'] for entry in item['episodes'] if 'inbound' in entry]
        for text in texts:
            assert not TOOL_WORDS.search(text), text
            assert '?' not in text, 'a setup turn never asks the agent anything'
        # The horizon is stated in minutes and the clock advance moves the ticks past it.
        clock = next(entry['advance_clock'] for entry in item['episodes'] if 'advance_clock' in entry)
        stated = MINUTES.search(first)
        if item['scenario'] == 'nothing-to-do':
            assert stated is None and clock > module.PAST_HORIZON_SECONDS
        else:
            minutes = int(stated.group(1))
            assert module.HORIZON_MINUTES[0] <= minutes <= module.HORIZON_MINUTES[1]
            assert clock == minutes * 60 + module.PAST_HORIZON_SECONDS
        # An owner who said not to says so in the setup turn, not in a later reaction.
        if item['scenario'] == 'owner-said-wait':
            assert re.search(r'do not (remind|chase) me|No reminders', first)


def test_background_state_is_seeded_into_the_episode_rather_than_fetched(generate):
    module, scenarios = initiative(generate, seed=3)
    for item in scenarios:
        assert set(item['initial_files']) == {'contacts.json'}
        contacts = json.loads(item['initial_files']['contacts.json'])
        mentioned = set(re.findall(r'p-\d\d', json.dumps(item['episodes'])))
        assert mentioned <= set(contacts), 'every contact a turn names has a seeded record'
        for contact, record in contacts.items():
            assert record == {'channel': record['channel'], 'address': 'capture:' + contact}
            assert record['channel'] in module.CHANNELS
        kinds = [next(iter(entry)) if 'session_id' not in entry else next(k for k in entry if k != 'session_id')
                 for entry in item['episodes']]
        inbound = [entry for entry in item['episodes'] if 'inbound' in entry]
        if item['scenario'] == 'reply-arrived':
            [entry] = inbound
            message = entry['inbound']
            assert entry['session_id'] != 'owner-1'
            assert message['channel'] == contacts[message['contact']]['channel']
            assert kinds.index('inbound') < kinds.index('advance_clock'), 'the reply arrives before the horizon'
            first = owner_turns(item)[0]
            assert message['contact'] in first
            assert any(name in message['text'] and name in first for name in module.ITEMS), 'the reply carries the item'
        else:
            assert not inbound


def test_oracles_come_from_the_same_draws_as_the_turns(generate):
    module, scenarios = initiative(generate, seed=9)
    for item in scenarios:
        body = item['oracle']['body']
        turns = owner_turns(item)
        if item['family'] == 'warranted':
            action = body['action']
            assert action['target'] == 'capture:owner' and action['window'] == [1, 2]
            assert action['token'] in module.ITEMS and action['token'] in turns[0]
            if item['scenario'] == 'follow-up-at-time':
                [other] = body['forbidden']
                involved = re.findall(r'p-\d\d', turns[0])
                assert re.fullmatch(r'p-\d\d', other) and other not in involved and other in turns[1]
            else:
                assert body['forbidden'] == []
        else:
            assert body == {'action': 'none', 'forbidden': []}
            if item['scenario'] == 'already-done':
                assert len(turns) == 3 and re.search(r'sent the|went to', turns[2])


def test_dev_split_content_hashes_are_pinned(generate, tmp_path):
    module = generate.load_templates(GENERATORS / 'initiative.py')
    for seed, expected in PINNED_DEV_SPLITS.items():
        content = generate.write(tmp_path / str(seed), module, seed, 'dev', 3, GENERATORS / 'initiative.py')
        assert content == expected, f'dev split seed {seed} changed; a template edit is a new dataset'
        manifest = json.loads((tmp_path / str(seed) / 'manifest.json').read_text())
        assert manifest['families'] == {'warranted': 9, 'control': 12}
        assert manifest['generator'] == {**manifest['generator'], 'seed': seed, 'split': 'dev', 'per_template': 3}


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
