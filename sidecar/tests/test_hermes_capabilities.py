"""Capability receipts fail closed and native checks cannot inherit a live profile."""
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from protagine import hermes_capabilities as capabilities


@pytest.mark.parametrize('report', [None, {}, {'schema': capabilities.SCHEMA, 'capabilities': []}])
def test_invalid_receipt_is_rejected(report):
    with pytest.raises(ValueError, match='Invalid Hermes capability receipt'):
        capabilities.require_capabilities(report)


@pytest.mark.parametrize('value', [None, False, 'available', {'available': 'true'}, {'available': 1}])
def test_partial_or_mistyped_core_check_cannot_admit_runtime(value):
    report = {'schema': capabilities.SCHEMA, 'capabilities': {'owned_payload_erasure': value}}
    assert 'owned_payload_erasure' in capabilities.missing_capabilities(report)
    with pytest.raises(ValueError, match='owned_payload_erasure'):
        capabilities.require_capabilities(report)


def test_native_process_is_offline_profile_isolated_and_receipt_does_not_leak_environment(monkeypatch, tmp_path):
    real_home = tmp_path/'personal'; real_home.mkdir()
    sentinel = real_home/'config.yaml'; sentinel.write_text('unchanged')
    monkeypatch.setenv('HERMES_HOME', str(real_home))
    monkeypatch.setenv('OPENAI_API_KEY', 'PRIVATE_PROBE_CANARY')
    monkeypatch.setenv('PYTHONPATH', '/unexpected/imports')
    temporary = []
    def run(command, **options):
        assert command[:3] == ['/selected/python', '-I', '-B']
        assert command[-1] == '--probe'
        environment = options['env']
        assert 'PRIVATE_PROBE_CANARY' not in json.dumps(environment)
        assert 'PYTHONPATH' not in environment
        assert environment['HERMES_ENABLE_PROJECT_PLUGINS'] == '0'
        root = Path(options['cwd']); temporary.append(root)
        assert Path(environment['HERMES_HOME']).is_relative_to(root)
        assert Path(environment['HOME']) == root
        assert environment['HERMES_HOME'] != str(real_home)
        assert options['timeout'] == 45
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            'schema': capabilities.SCHEMA, 'capabilities': {}}))
    monkeypatch.setattr(capabilities.subprocess, 'run', run)
    report = capabilities.probe_runtime('/selected/python')
    assert capabilities.missing_capabilities(report)
    assert sentinel.read_text() == 'unchanged'
    assert all(not path.exists() for path in temporary)


@pytest.mark.parametrize('failure', ['exit', 'timeout', 'malformed'])
def test_native_probe_failure_has_safe_actionable_diagnostic(monkeypatch, failure):
    def run(command, **options):
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(command, 45, stderr='PRIVATE_PROBE_CANARY')
        return SimpleNamespace(returncode=1 if failure == 'exit' else 0,
                               stdout='PRIVATE_PROBE_CANARY', stderr='PRIVATE_PROBE_CANARY')
    monkeypatch.setattr(capabilities.subprocess, 'run', run)
    with pytest.raises(ValueError, match='Select its Python') as error:
        capabilities.probe_runtime('/selected/python')
    assert 'PRIVATE_PROBE_CANARY' not in str(error.value)
