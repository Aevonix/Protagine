"""Comparator profiles and generated families in the plan; deterministic consumers, no model."""
import asyncio
import hashlib
import importlib.util
from pathlib import Path

import pytest

from protagine.qualification import paired, paired_arms
from protagine.qualification.paired_cases import cases as real_cases
from test_qualification_paired_runner import fixture  # noqa: F401  (pytest fixture)

GENERATORS = Path(__file__).resolve().parents[2] / 'benchmarks' / 'paired' / 'generators'


def test_built_in_comparator_profiles_carry_their_switches_and_the_heartbeat_identity(fixture):
    arms = ['base-heartbeat', 'protagine', 'base-curator']
    manifest = paired.plan(fixture.output, native_binding='candidate', evidence_mode='controlled',
                           arms=arms, **fixture.resources)
    profiles = manifest['comparison']['profiles']
    assert profiles['base-heartbeat'] == {'name': 'base-heartbeat', 'plugin': False, 'overlay': {}, 'heartbeat': True}
    assert profiles['base-curator'] == {'name': 'base-curator', 'plugin': False, 'overlay': {}, 'curator': True}
    assert profiles['protagine'] == {'name': 'protagine', 'plugin': True, 'overlay': {}}
    assert manifest['comparison']['heartbeat'] == paired.HEARTBEAT
    assert paired.HEARTBEAT['prompt_sha256'] == paired_arms.HEARTBEAT_PROMPT_SHA256
    assert paired.HEARTBEAT['extra_toolsets'] == ['kanban', 'cronjob'] and paired.HEARTBEAT['deliver'] == 'capture:owner'
    case = manifest['pairs'][0]['arms']['base-heartbeat']['case']
    assert case['inputs']['profile']['heartbeat'] is True and case['inputs']['arm'] == 'base-heartbeat'
    report = asyncio.run(paired.run(fixture.output, **fixture.resources))
    assert report['reference_arm'] == 'base-heartbeat' and set(report['arms']) == set(arms)
    assert [(item['treatment'], item['comparator']) for item in report['statistics']['contrasts']] == [
        ('protagine', 'base-heartbeat'), ('base-curator', 'base-heartbeat')]


def test_custom_profiles_turn_switches_on_only_with_booleans(fixture):
    manifest = paired.plan(fixture.output, native_binding='candidate', evidence_mode='controlled',
                           arms=['base_hermes', 'full-hb'],
                           profiles={'full-hb': {'plugin': True, 'heartbeat': True, 'curator': False}},
                           **fixture.resources)
    assert manifest['comparison']['profiles']['full-hb'] == {'name': 'full-hb', 'plugin': True, 'overlay': {},
                                                             'heartbeat': True}


@pytest.mark.parametrize('profile', [{'heartbeat': 'yes'}, {'curator': 1}, {'toolsets': ['kanban']}, {'heartbeat': None}])
def test_non_boolean_or_unknown_switches_are_rejected(fixture, profile):
    with pytest.raises(ValueError, match='switches'):
        paired.plan(fixture.output, native_binding='candidate', evidence_mode='controlled',
                    arms=['base_hermes', 'x'], profiles={'x': {'plugin': False, **profile}}, **fixture.resources)
    assert not fixture.output.exists()


def test_heartbeat_arm_needs_an_image_carrying_the_same_prompt(fixture, monkeypatch):
    from protagine.qualification import paired_container
    original = paired_container.configuration

    def other_prompt(*args, **kwargs):
        supplied, recipe = original(*args, **kwargs)
        recipe['container_payload'] = {**recipe['container_payload'], 'heartbeat_prompt_sha256': 'f' * 64}
        return supplied, recipe
    monkeypatch.setattr(paired_container, 'configuration', other_prompt)
    with pytest.raises(ValueError, match='heartbeat prompt'):
        paired.plan(fixture.output, native_binding='candidate', evidence_mode='controlled',
                    arms=['base-heartbeat', 'protagine'], **fixture.resources)
    # Arms without the heartbeat do not care which prompt the image carries.
    assert paired.plan(fixture.output, native_binding='candidate', evidence_mode='controlled',
                       arms=['base-curator', 'protagine'], **fixture.resources)['declared_attempts'] == 4


def generated_family(tmp_path):
    spec = importlib.util.spec_from_file_location('paired_generate_runner', GENERATORS / 'generate.py')
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    module = engine.load_templates(GENERATORS / 'initiative.py')
    content = engine.write(tmp_path / 'family', module, 21, 'dev', 1, GENERATORS / 'initiative.py')
    return tmp_path / 'family', content


def test_generated_family_directory_is_frozen_into_the_plan(fixture, monkeypatch, tmp_path):
    from protagine.qualification import paired_cases
    monkeypatch.setattr(paired_cases, 'cases', real_cases)
    directory, content = generated_family(tmp_path)
    manifest = paired.plan(fixture.output, native_binding='candidate', evidence_mode='controlled',
                           arms=['base-heartbeat', 'protagine'], dataset_dir=directory, **fixture.resources)
    dataset = manifest['dataset']
    assert dataset['version'] == 'mind-initiative-1' and dataset['source_sha256'] == content
    assert dataset['split'] == 'dev' and len(dataset['episode_ids']) == 28
    assert manifest['options']['dataset_dir'] == str(directory.resolve())
    assert manifest['options']['dataset_version'] is None
    assert 'Generated family' in manifest['coverage']
    case = manifest['pairs'][0]['arms']['protagine']['case']
    assert 'body' in case['oracle'] and case['inputs']['dataset']['split'] == 'dev'
    # Re-preparing from the frozen options reproduces the plan exactly, as run() requires.
    again, *_ = paired.prepare(output=fixture.output, **manifest['options'], label=manifest['label'],
                               evidence_mode=manifest['evidence_mode'], **fixture.resources)
    assert again == manifest
    (directory / 'scenarios.json').write_bytes((directory / 'scenarios.json').read_bytes() + b'\n')
    with pytest.raises(ValueError, match='checksum'):
        paired.prepare(output=fixture.output, **manifest['options'], label=manifest['label'],
                       evidence_mode=manifest['evidence_mode'], **fixture.resources)


def test_generated_family_plans_record_eager_tool_loading_and_need_an_image_that_applies_it(fixture, monkeypatch, tmp_path):
    from protagine.qualification import paired_cases, paired_container, paired_worker
    monkeypatch.setattr(paired_cases, 'cases', real_cases)
    directory, _ = generated_family(tmp_path)
    manifest = paired.plan(fixture.output, native_binding='candidate', evidence_mode='controlled',
                           arms=['base-heartbeat', 'protagine'], dataset_dir=directory, **fixture.resources)
    assert manifest['comparison']['tool_loading'] == {
        'protocol': 'paired-tool-loading-1', 'mode': 'eager',
        'config': {'tools': {'tool_search': {'enabled': 'off'}}}}
    assert manifest['comparison']['tool_loading']['config']['tools'] == paired_worker.EAGER_TOOLS_CONFIG
    # Every model-facing turn carries the body clock in the stock gateway format, in every arm.
    assert manifest['comparison']['message_timestamps'] == {
        'protocol': 'paired-message-timestamps-1', 'mode': 'gateway', 'format': '[%a %Y-%m-%d %H:%M:%S %Z]'}
    # And the same description of the body in every turn's system message and cron run.
    note = manifest['comparison']['environment_note']
    assert note['protocol'] == 'paired-environment-note-1' and note['mode'] == 'messaging'
    assert note['text'] == paired_worker.ENVIRONMENT_NOTES['messaging']
    assert note['text_sha256'] == hashlib.sha256(note['text'].encode()).hexdigest()
    for pair in manifest['pairs']:
        for arm in ('base-heartbeat', 'protagine'):
            assert pair['arms'][arm]['case']['inputs']['tool_loading'] == 'eager'
            assert pair['arms'][arm]['case']['inputs']['message_timestamps'] == 'gateway'
            assert pair['arms'][arm]['case']['inputs']['environment_note'] == 'messaging'
    original = paired_container.configuration

    def image_without(key):
        def older_image(*args, **kwargs):
            supplied, recipe = original(*args, **kwargs)
            recipe['container_payload'] = {k: v for k, v in recipe['container_payload'].items() if k != key}
            return supplied, recipe
        return older_image
    for key, message in (('tool_loading', 'Eager tool loading'), ('message_timestamps', 'Message timestamps'),
                         ('environment_note', 'environment note')):
        monkeypatch.setattr(paired_container, 'configuration', image_without(key))
        with pytest.raises(ValueError, match=message):
            paired.plan(tmp_path / 'again', native_binding='candidate', evidence_mode='controlled',
                        arms=['base-heartbeat', 'protagine'], dataset_dir=directory, **fixture.resources)
        assert not (tmp_path / 'again').exists()
    # Frozen datasets declare no tool loading, so the same older image still plans them.
    frozen = paired.plan(tmp_path / 'frozen', native_binding='candidate', evidence_mode='controlled',
                         dataset_version=paired_cases.BASELINE_VERSION,
                         case_ids=[paired_cases.cases('base_hermes', dataset_version=paired_cases.BASELINE_VERSION)[0].id],
                         **fixture.resources)
    for key in ('tool_loading', 'message_timestamps', 'environment_note'):
        assert key not in frozen['comparison']
        assert key not in frozen['pairs'][0]['arms']['base_hermes']['case']['inputs']


def test_generated_family_plans_pin_the_clock_start_in_every_arm_and_need_an_image_that_does(fixture, monkeypatch, tmp_path):
    from protagine.qualification import paired_body, paired_cases, paired_container
    monkeypatch.setattr(paired_cases, 'cases', real_cases)
    directory, _ = generated_family(tmp_path)
    manifest = paired.plan(fixture.output, native_binding='candidate', evidence_mode='controlled',
                           arms=['base-heartbeat', 'protagine'], dataset_dir=directory, **fixture.resources)
    assert manifest['comparison']['clock_start'] == {'protocol': 'paired-clock-start-1', 'utc': '12:00'}
    assert paired_body.CLOCK_START_PROTOCOL == 'paired-clock-start-1'
    for pair in manifest['pairs']:
        for arm in ('base-heartbeat', 'protagine'):
            assert pair['arms'][arm]['case']['inputs']['clock_start'] == '12:00'
    original = paired_container.configuration

    def older_image(*args, **kwargs):
        supplied, recipe = original(*args, **kwargs)
        recipe['container_payload'] = {k: v for k, v in recipe['container_payload'].items() if k != 'clock_start'}
        return supplied, recipe
    monkeypatch.setattr(paired_container, 'configuration', older_image)
    with pytest.raises(ValueError, match='clock start'):
        paired.plan(tmp_path / 'again', native_binding='candidate', evidence_mode='controlled',
                    arms=['base-heartbeat', 'protagine'], dataset_dir=directory, **fixture.resources)
    # Frozen datasets declare no pinned start, so the older image still plans them.
    frozen = paired.plan(tmp_path / 'frozen', native_binding='candidate', evidence_mode='controlled',
                         dataset_version=paired_cases.BASELINE_VERSION,
                         case_ids=[paired_cases.cases('base_hermes', dataset_version=paired_cases.BASELINE_VERSION)[0].id],
                         **fixture.resources)
    assert 'clock_start' not in frozen['comparison']
    assert 'clock_start' not in frozen['pairs'][0]['arms']['base_hermes']['case']['inputs']


def test_generated_family_needs_a_body_capable_image_and_one_dataset_selector(fixture, monkeypatch, tmp_path):
    from protagine.qualification import paired_cases, paired_container
    monkeypatch.setattr(paired_cases, 'cases', real_cases)
    directory, _ = generated_family(tmp_path)
    with pytest.raises(ValueError, match='not both'):
        paired.plan(fixture.output, native_binding='candidate', evidence_mode='controlled',
                    dataset_dir=directory, dataset_version='paired-agent-reviewed-2', **fixture.resources)
    with pytest.raises(ValueError, match='must exist'):
        paired.plan(fixture.output, native_binding='candidate', evidence_mode='controlled',
                    dataset_dir=tmp_path / 'missing', **fixture.resources)
    original = paired_container.configuration

    def no_body(*args, **kwargs):
        supplied, recipe = original(*args, **kwargs)
        recipe['container_payload'] = {k: v for k, v in recipe['container_payload'].items() if k != 'body_protocol'}
        return supplied, recipe
    monkeypatch.setattr(paired_container, 'configuration', no_body)
    with pytest.raises(ValueError, match='body tick'):
        paired.plan(fixture.output, native_binding='candidate', evidence_mode='controlled',
                    dataset_dir=directory, **fixture.resources)
    assert not fixture.output.exists()
