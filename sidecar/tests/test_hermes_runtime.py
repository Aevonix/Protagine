"""Runtime preparation cannot select a failed or unrelated candidate."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from protagine import hermes_runtime as runtime
from protagine import setup_hermes
from protagine.hermes_capabilities import FEATURES, SCHEMA


@pytest.fixture
def preparation(tmp_path, monkeypatch):
    root = tmp_path/'new-parent'/'candidate'
    source = tmp_path/'official'
    source.mkdir()
    manifest = runtime.describe_patchset()
    calls = []
    def stage(original, destination, **kwargs):
        assert original == source
        assert destination.parent.is_dir()
        destination.mkdir()
        calls.append('stage')
    def command(args, **kwargs):
        calls.append([str(value) for value in args])
        return '[]' if '--format=json' in args else ''
    report = {'schema': SCHEMA, 'capabilities': {
        name: {'available': True} for names in FEATURES.values() for name in names}}
    monkeypatch.setattr(runtime, 'stage_runtime', stage)
    monkeypatch.setattr(runtime, '_run', command)
    monkeypatch.setattr(runtime, 'source_for_interpreter', lambda _: root)
    monkeypatch.setattr(runtime, 'probe_runtime', lambda _: report)
    return SimpleNamespace(root=root, source=source, report=report, calls=calls, manifest=manifest)


def test_prepare_qualifies_separate_runtime_and_rechecks_reuse(preparation, monkeypatch):
    p = preparation
    selected = runtime.prepare_runtime(source=p.source, destination=p.root)
    assert selected == p.root/'.venv/bin/python'
    receipt = json.loads((p.root/'.protagine-runtime.json').read_text())
    assert receipt['activation_state'] == 'not_activated'
    assert receipt['official_revision'] == p.manifest['official_revision']
    assert list(p.source.iterdir()) == []
    monkeypatch.setattr(runtime, 'inspect_runtime', lambda *a, **k: {'status': 'patched'})
    p.calls.clear()
    runtime.prepare_runtime(destination=p.root)
    assert not any('install' in row or 'venv' in row for row in p.calls)
    assert any('check' in row for row in p.calls)


def test_failed_behavior_does_not_write_success_receipt(preparation):
    p = preparation
    p.report['capabilities']['overlapping_callbacks']['available'] = False
    with pytest.raises(ValueError, match='overlapping_callbacks'):
        runtime.prepare_runtime(source=p.source, destination=p.root)
    assert not (p.root/'.protagine-runtime.json').exists()


def test_wrong_import_root_is_not_qualified(preparation, monkeypatch):
    p = preparation
    monkeypatch.setattr(runtime, 'source_for_interpreter', lambda _: p.source)
    with pytest.raises(ValueError, match='another Hermes source'):
        runtime.prepare_runtime(source=p.source, destination=p.root)
    assert not (p.root/'.protagine-runtime.json').exists()


def test_reuse_cannot_mask_an_unsupported_update(preparation, monkeypatch):
    p = preparation
    runtime.prepare_runtime(source=p.source, destination=p.root)
    before = (p.root/'.protagine-runtime.json').read_bytes()
    monkeypatch.setattr(runtime, '_run', lambda *a, **k: 'new-unsupported-revision')
    with pytest.raises(ValueError, match='Unsupported Hermes update'):
        runtime.prepare_runtime(source=p.source, destination=p.root)
    assert (p.root/'.protagine-runtime.json').read_bytes() == before


def test_reuse_cannot_import_another_interpreter(preparation, monkeypatch):
    p = preparation
    runtime.prepare_runtime(source=p.source, destination=p.root)
    monkeypatch.setattr(runtime, 'inspect_runtime', lambda *a, **k: {'status': 'patched'})
    path = p.root/'.protagine-runtime.json'
    receipt = json.loads(path.read_text())
    receipt['python'] = '/another/runtime/bin/python'
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match='different interpreter'):
        runtime.prepare_runtime(destination=p.root)


def test_guided_setup_prepares_official_runtime_when_contract_is_missing(monkeypatch):
    original, prepared = Path('/official/.venv/bin/python'), Path('/prepared/.venv/bin/python')
    def missing(_):
        raise ValueError('missing owned_payload_erasure')
    monkeypatch.setattr(setup_hermes, '_interpreter', missing)
    monkeypatch.setattr(runtime, 'source_for_interpreter', lambda value: Path('/official'))
    calls = []
    monkeypatch.setattr(runtime, 'prepare_runtime', lambda **values: calls.append(values) or prepared)
    args = SimpleNamespace(hermes_python=str(original))
    assert setup_hermes._installation_interpreter(args) == prepared
    assert setup_hermes._installation_interpreter(args) == prepared
    assert len(calls) == 1 and calls[0]['source'] == Path('/official')
    assert args.hermes_python == str(prepared)


def test_explicit_prepare_does_not_require_an_existing_hermes(monkeypatch):
    calls = []
    monkeypatch.setattr(runtime, 'prepare_runtime', lambda **values: calls.append(values) or Path('/new/python'))
    args = SimpleNamespace(prepare_hermes=True)
    assert setup_hermes._installation_interpreter(args) == Path('/new/python')
    assert calls[0]['source'] is None
