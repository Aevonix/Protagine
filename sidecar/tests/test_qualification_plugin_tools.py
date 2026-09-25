"""A family's plugin tools are fixed for its comparison series.

The plugin arms' model tools once changed for every family at once (protagine_self was added so the
memory family's self-report could be read), and the initiative family's re-pilot then ran with a tool
its own dev and held-out runs never had: owner turns that wrote Hermes memory fell from 27/38 to 15/38
and a contact turn capped. Every generated family now declares its plugin tool set, the plan records it
in the comparison (so a series is never compared across a change of it), and the worker gives the plugin
arms exactly that set.
"""

from __future__ import annotations

import pytest

from protagine.qualification import paired, paired_cases, paired_worker
from test_qualification_paired_campaign import CASES, GENERATORS, engine, write_dataset
from test_qualification_paired_runner import fixture  # noqa: F401  (pytest fixture)


def _plan(fixture, monkeypatch, tmp_path, family_file, dataset_id, arms, name=None):
    generate = engine()
    rendered = generate.render(generate.load_templates(GENERATORS / family_file), 7, 1)
    monkeypatch.setattr(paired_cases, 'cases', CASES)
    directory = tmp_path / ('data-' + (name or dataset_id))
    if not directory.exists():
        write_dataset(directory, rendered, dataset_id=dataset_id)
    fixture.output.mkdir(mode=0o700, exist_ok=True)
    return paired.plan(fixture.output / (name or dataset_id), native_binding='candidate', evidence_mode='controlled',
                       dataset_dir=directory, arms=list(arms), reference_arm=arms[0], **fixture.resources)


def test_the_worker_gives_the_plugin_arms_the_declared_set():
    assert paired_worker.plugin_tools('memory') == ['protagine_memory_search', 'protagine_memory_forget']
    assert paired_worker.plugin_tools('memory_self') == [*paired_worker.plugin_tools('memory'), 'protagine_self']
    assert paired_worker.plugin_tools(None) == paired_worker.PLUGIN_TOOLS       # a frozen dataset declares none
    with pytest.raises(ValueError):
        paired_worker.plugin_tools('everything')


def test_the_initiative_series_keeps_the_tools_of_its_dev_and_held_out_runs(fixture, monkeypatch, tmp_path):
    manifest = _plan(fixture, monkeypatch, tmp_path, 'initiative.py', 'mind-initiative-1', ('base-heartbeat', 'full'))
    assert manifest['comparison']['plugin_tools'] == {
        'protocol': paired_worker.PLUGIN_TOOLS_PROTOCOL, 'mode': 'memory',
        'tools': ['protagine_memory_search', 'protagine_memory_forget']}
    assert all(pair['arms'][arm]['case']['inputs']['plugin_tools'] == 'memory'
               for pair in manifest['pairs'] for arm in ('base-heartbeat', 'full'))


def test_the_memory_family_keeps_the_self_tool_its_probe_reads(fixture, monkeypatch, tmp_path):
    manifest = _plan(fixture, monkeypatch, tmp_path, 'memory.py', 'mind-memory-1', ('base_hermes', 'full'))
    assert manifest['comparison']['plugin_tools']['mode'] == 'memory_self'
    assert 'protagine_self' in manifest['comparison']['plugin_tools']['tools']


def test_a_declared_set_needs_an_image_whose_worker_applies_it(fixture, monkeypatch, tmp_path):
    from protagine.qualification import paired_container
    original = paired_container.configuration

    def old_image(*args, **kwargs):
        supplied, recipe = original(*args, **kwargs)
        recipe['container_payload'] = {k: v for k, v in recipe['container_payload'].items() if k != 'plugin_tools'}
        return supplied, recipe
    monkeypatch.setattr(paired_container, 'configuration', old_image)
    with pytest.raises(ValueError, match='plugin tool'):
        _plan(fixture, monkeypatch, tmp_path, 'initiative.py', 'mind-initiative-1', ('base-heartbeat', 'full'),
              name='old-image')
