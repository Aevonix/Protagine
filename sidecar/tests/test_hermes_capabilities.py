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


@pytest.mark.parametrize('behavior', ['serialized', 'parallel', 'drop', 'wrong_context', 'wrong_result'])
def test_concurrent_callback_context_checks_admission_and_isolation(monkeypatch, tmp_path, behavior):
    import contextvars
    import sys
    import threading
    from types import ModuleType
    class Manager:
        def __init__(self, **kwargs):
            self._hook_timeout_running_cond = threading.Condition()
            self.running = False
        def invoke_hook(self, name, **kwargs):
            condition = self._hook_timeout_running_cond
            with condition:
                if self.running and behavior == 'drop':
                    return []
                while self.running and behavior != 'parallel':
                    condition.wait()
                self.running = True
            try:
                result = (contextvars.Context().run(self.callback, **kwargs)
                          if behavior == 'wrong_context' else self.callback(**kwargs))
                return ['first' if behavior == 'wrong_result' else result]
            finally:
                with condition:
                    self.running = False
                    condition.notify_all()
    class Context:
        def __init__(self, manifest, manager):
            self.manager = manager
        def register_hook(self, name, callback):
            self.manager.callback = callback
            return 'registered'
    plugins = ModuleType('hermes_cli.plugins')
    plugins.PluginManager, plugins.PluginContext = Manager, Context
    manifests = ModuleType('hermes_cli.plugins_manifest')
    manifests.PluginManifest = SimpleNamespace
    monkeypatch.setitem(sys.modules, 'hermes_cli', ModuleType('hermes_cli'))
    monkeypatch.setitem(sys.modules, 'hermes_cli.plugins', plugins)
    monkeypatch.setitem(sys.modules, 'hermes_cli.plugins_manifest', manifests)
    if behavior in {'serialized', 'parallel'}:
        capabilities._check_callbacks(tmp_path)
        capabilities._check_callbacks(tmp_path, overlap=True)
    else:
        with pytest.raises(AssertionError):
            capabilities._check_callbacks(tmp_path, overlap=True)
