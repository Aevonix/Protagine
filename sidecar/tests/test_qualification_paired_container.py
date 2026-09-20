"""Owned-process and transport checks; no Docker daemon or model is contacted."""
import asyncio
from copy import deepcopy
import json
import pathlib
from pathlib import Path
import runpy
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import httpx
import pytest

from protagine.qualification import native, paired_cases, paired_container, paired_transport, paired_worker
from protagine.qualification.runner import RunContext

IMAGE = 'sha256:' + 'a' * 64


@pytest.fixture
def selected_config(tmp_path, monkeypatch):
    monkeypatch.setenv('UNRELATED_OWNER_SECRET', 'must-not-cross')
    monkeypatch.setenv('BENCHMARK_KEY', 'explicit-fixture-key')
    monkeypatch.setenv('DOCKER_HOST', 'tcp://owner-daemon:2375')
    monkeypatch.setenv('HERMES_HOME', '/owner/live-home')
    supplied = {'providers': {
        'candidate': {'base_url': 'http://model.invalid/v1', 'default_model': 'candidate-model',
                      'key_env': 'BENCHMARK_KEY'},
        'owner': {'base_url': 'http://owner.invalid/v1', 'api_key': 'owner-private-key'}},
        'plugins': {'protagine': {'instance_dir': '/owner/live-instance'}},
        'memory': {'private': 'must-not-cross'}, 'agent': {'reasoning_effort': 'high'}}
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(supplied))
    monkeypatch.setattr(native, '_runtime', lambda *_: pytest.fail('Local Hermes inspection is forbidden'))
    return path


def image_inspector(monkeypatch, *, removal_error=False, volumes=None):
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        if 'inspect' in command and 'image' in command:
            stdout = json.dumps([{'Id': IMAGE, 'Architecture': 'arm64', 'Os': 'linux',
                                  'Config': {'Volumes': volumes}}])
        elif '--inspect' in command:
            stdout = json.dumps({'native_runtime': {'native_payload_sha256': 'b' * 64,
                                                   'python': '/usr/local/bin/python'},
                                 'worker_sha256': 'c' * 64})
        else:
            assert 'rm' in command and '--force' in command
            return subprocess.CompletedProcess(command, int(removal_error), stdout=b'',
                stderr=b'daemon unavailable' if removal_error else b'')
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr='')
    monkeypatch.setattr(paired_container.subprocess, 'run', run)
    return calls


def test_image_inspection_is_networkless_and_never_loads_owner_state(selected_config, monkeypatch):
    calls = image_inspector(monkeypatch)
    config, recipe = paired_container.configuration(selected_config, 'candidate', image=IMAGE)
    assert set(config['providers']) == {'candidate'}
    assert 'owner-private-key' not in json.dumps(config)
    assert 'live-instance' not in json.dumps(config)
    assert config['fallback_providers'] == []
    assert recipe['container']['image_id'] == IMAGE and recipe['container']['host_mounts'] == []
    inspection = next(command for command, _ in calls if '--inspect' in command)
    assert inspection[inspection.index('--network') + 1] == 'none'
    assert inspection[inspection.index('--runtime') + 1] == 'runc'
    assert 'NVIDIA_VISIBLE_DEVICES=void' in inspection and '--pull=never' in inspection
    assert not {'--mount', '--volume', '-v', '--gpus', '--privileged'} & set(inspection)
    name = inspection[inspection.index('--name') + 1]
    assert calls[-1][0][-3:] == ['rm', '--force', name]
    for _, options in calls:
        env = options['env']
        assert not {'UNRELATED_OWNER_SECRET', 'BENCHMARK_KEY', 'DOCKER_HOST', 'HERMES_HOME'} & env.keys()
        assert env['HOME'] != '/owner/live-home'


def test_inspector_does_not_hide_cleanup_failure(selected_config, monkeypatch):
    image_inspector(monkeypatch, removal_error=True)
    with pytest.raises(RuntimeError, match='cleanup is unconfirmed'):
        paired_container.configuration(selected_config, 'candidate', image=IMAGE)


def test_automatic_image_volumes_are_rejected_before_execution(selected_config, monkeypatch):
    calls = image_inspector(monkeypatch, volumes={'/owner': {}})
    with pytest.raises(ValueError, match='automatic volumes'):
        paired_container.configuration(selected_config, 'candidate', image=IMAGE)
    assert not any('--inspect' in command for command, _ in calls)


class Process:
    def __init__(self, action, options, records, *, entered=None, release=None, block=None, remove_ok=True):
        self.action, self.options, self.records = action, options, records
        self.entered, self.release, self.block = entered, release, block
        self.remove_ok, self.returncode = remove_ok, None

    async def communicate(self, payload=None):
        if self.block == self.action + '_communicate':
            self.entered.set()
            await self.release.wait()
        self.returncode = 0
        if self.action == 'create':
            return (b'd' * 64 + b'\n', b'')
        if self.action == 'start':
            self.records['payload'] = json.loads(payload)
            result = {'stage': 'returned', 'output': 'task complete', 'tool_evidence': {
                'model_requests': [{'model': 'candidate-model', 'status': 200,
                                    'returned_models': ['served-model']}],
                'artifacts': {'result.json': '{}'}, 'turns_completed': 1, 'declared_turns': 1}}
            self.options['stdout'].write((paired_container.RESULT_MARKER + json.dumps(result) + '\n').encode())
            self.options['stdout'].flush()
            return b'', b''
        self.returncode = 0 if self.remove_ok else 1
        return b'', b'' if self.remove_ok else b'daemon unavailable'

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self):
        self.returncode = -9


def process_fixture(monkeypatch, *, block=None, entered=None, release=None, remove_ok=True):
    records = {'commands': []}
    async def spawn(*command, **kwargs):
        action = command[1]
        assert action in {'create', 'start', 'rm'}
        records['commands'].append((list(command), kwargs))
        if block == action + '_spawn':
            entered.set()
            await release.wait()
        return Process(action, kwargs, records, entered=entered, release=release,
                       block=block, remove_ok=remove_ok)
    monkeypatch.setattr(paired_container.asyncio, 'create_subprocess_exec', spawn)
    return records


def run_context(tmp_path, selected_config):
    config, _ = native.configuration(selected_config, 'candidate', inspect_runtime=False)
    state = tmp_path / 'state'
    state.mkdir()
    context = RunContext(SimpleNamespace(binding='candidate', native_config=config,
        container_spec={'image': IMAGE, 'image_id': IMAGE, 'docker_host': None}), state, [])
    return context


def test_consumer_sends_inputs_without_oracle_and_removes_only_its_container(tmp_path, selected_config, monkeypatch):
    records = process_fixture(monkeypatch)
    context = run_context(tmp_path, selected_config)
    case = paired_cases.cases(arm='base_hermes')[0]
    before = deepcopy(case.inputs)
    result = asyncio.run(paired_container.consume(case.inputs, context))
    assert case.inputs == before and records['payload']['inputs'] == before
    assert set(records['payload']) == {'binding', 'config', 'provider_env', 'inputs'}
    assert 'oracle' not in records['payload']['inputs']
    assert records['payload']['provider_env'] == {'BENCHMARK_KEY': 'explicit-fixture-key'}
    commands = [command for command, _ in records['commands']]
    assert [command[1] for command in commands] == ['create', 'start', 'rm']
    name = commands[0][commands[0].index('--name') + 1]
    assert commands[1][-1] == commands[2][-1] == name
    assert name.startswith('protagine-paired-')
    assert context.state_cleanup_safe and result['effects']['container_removed']
    assert context.observations[0]['selected_binding'] == 'candidate'
    assert context.observations[0]['prior_attempts'] == []


@pytest.mark.parametrize('stage', ['create_spawn', 'create_communicate', 'start_spawn', 'start_communicate'])
def test_cancellation_settles_owned_creation_before_removal(tmp_path, selected_config, monkeypatch, stage):
    async def check():
        entered, release = asyncio.Event(), asyncio.Event()
        records = process_fixture(monkeypatch, block=stage, entered=entered, release=release)
        context = run_context(tmp_path, selected_config)
        task = asyncio.create_task(paired_container.consume(paired_cases.cases(arm='base_hermes')[0].inputs, context))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        commands = [command for command, _ in records['commands']]
        name = commands[0][commands[0].index('--name') + 1]
        assert commands[-1] == ['docker', 'rm', '--force', name]
        assert context.state_cleanup_safe and context.observations[0]['outcome'] == 'cancelled'
        assert not any(command[1] == 'start' for command in commands) if stage.startswith('create') else True
    asyncio.run(check())


def test_failed_container_removal_keeps_cleanup_unconfirmed(tmp_path, selected_config, monkeypatch):
    process_fixture(monkeypatch, remove_ok=False)
    context = run_context(tmp_path, selected_config)
    result = asyncio.run(paired_container.consume(paired_cases.cases(arm='base_hermes')[0].inputs, context))
    assert context.state_cleanup_safe is False
    assert result['effects']['container_removed'] is False


class SyncChunks(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
    def __iter__(self):
        yield from self.chunks


class AsyncChunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('sse', [False, True])
def test_transport_preserves_request_and_response_bytes(asynchronous, sse):
    answer = {'model': 'served-model', 'choices': [{'delta': {'content': 'hello'}}],
              'usage': {'prompt_tokens': 17, 'completion_tokens': 3}}
    raw = json.dumps(answer).encode()
    if sse:
        raw = b'data: ' + raw + b'\r\n\r\ndata: [DONE]\n\n'
    chunks = [raw[:7], raw[7:19], raw[19:]]
    sent = []
    def respond(request):
        sent.append(request.content)
        stream = AsyncChunks(chunks) if asynchronous else SyncChunks(chunks)
        return httpx.Response(200, headers={'content-type': 'text/event-stream' if sse else 'application/json'},
                              stream=stream)
    body = b'{"model":"candidate-model","messages":[{"role":"user","content":"task"}],"max_tokens":128}'
    with paired_transport.observe_requests('http://model.invalid/v1') as observations:
        if asynchronous:
            async def query():
                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                    async with client.stream('POST', 'http://model.invalid/v1/chat/completions', content=body) as response:
                        return await response.aread()
            actual = asyncio.run(query())
        else:
            with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                with client.stream('POST', 'http://model.invalid/v1/chat/completions', content=body) as response:
                    actual = response.read()
    assert actual == raw and sent == [body]
    assert len(observations) == 1
    assert observations[0]['returned_models'] == ['served-model']
    assert observations[0]['usage'] == answer['usage']
    assert observations[0]['requested_max_tokens'] == 128
    usage = paired_transport.usage_summary(observations)
    assert usage['observed_model_calls'] == 1 and usage['observed_input_tokens'] == 17
    assert usage['observed_output_tokens'] == 3
    assert usage['coverage'] == 'partial' and usage['total_model_calls'] is None
    assert usage['background_model_calls'] is None


def test_transport_ignores_other_endpoints_and_retains_unknown_usage():
    with paired_transport.observe_requests('http://model.invalid/v1') as observations:
        with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200,
                json={'model': 'served-model', 'choices': [{'message': {'content': 'done'}}]}))) as client:
            client.post('http://other.invalid/v1/chat/completions', json={'messages': [], 'model': 'candidate'})
            client.post('http://model.invalid/v1/chat/completions', json={'messages': [], 'model': 'candidate'})
    assert len(observations) == 1
    usage = paired_transport.usage_summary(observations)
    assert usage['observed_model_calls'] == 1
    assert usage['observed_input_tokens'] is None and usage['input_tokens'] is None


def test_workspace_seed_and_snapshot_reject_escape_without_copying_oracles(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    case = paired_cases.cases(arm='base_hermes')[0]
    paired_worker.seed_workspace(root, case.inputs['initial_files'])
    assert paired_worker.snapshot_workspace(root) == case.inputs['initial_files']
    for name in ('../outside', '/tmp/absolute-task-escape', 'a/../../outside'):
        with pytest.raises(ValueError, match='fixture path'):
            paired_worker.seed_workspace(root, {name: 'no'})
    assert not (tmp_path / 'outside').exists()
    (root / 'outside-link').symlink_to(tmp_path / 'not-a-fixture')
    with pytest.raises(ValueError, match='symlink'):
        paired_worker.snapshot_workspace(root)


def test_native_file_tools_cannot_follow_paths_outside_workspace(tmp_path, monkeypatch):
    root = tmp_path / 'workspace'
    root.mkdir()
    (root / 'escape').symlink_to(tmp_path, target_is_directory=True)
    seen = []
    def original(args, **kwargs):
        seen.append(args)
        return 'called'
    entries = {name: SimpleNamespace(handler=original) for name in ('read_file', 'write_file', 'patch', 'search_files')}
    registry_module = ModuleType('tools.registry')
    registry_module.registry = SimpleNamespace(get_entry=lambda name: entries[name])
    tools_module = ModuleType('tools')
    tools_module.__path__ = []
    monkeypatch.setitem(sys.modules, 'tools', tools_module)
    monkeypatch.setitem(sys.modules, 'tools.registry', registry_module)
    monkeypatch.setitem(sys.modules, 'tools.file_tools', ModuleType('tools.file_tools'))
    with paired_worker.workspace_tools(root):
        for entry in entries.values():
            for path in ('../owner', str(tmp_path / 'owner'), 'escape/owner'):
                assert 'outside' in json.loads(entry.handler({'path': path}))['error']
            assert entry.handler({'path': 'result.json'}) == 'called'
    assert len(seen) == 4 and all(value['path'] == str(root / 'result.json') for value in seen)
    assert all(entry.handler is original for entry in entries.values())


def test_runtime_image_pruning_removes_oracles_and_preserves_execution_helpers(tmp_path, monkeypatch):
    repo = tmp_path / 'protagine'
    qualification = repo / 'sidecar/protagine/qualification'
    preserved = ['paired_worker.py', 'paired_transport.py', 'native_memory_worker.py',
                 'native_identity.py', 'native_memory_identity.py', '__init__.py']
    removed = ['paired_cases.py', 'paired_report.py', 'fixtures/paired-agent-pilot-1/cases.json']
    for name in preserved + removed:
        path = qualification / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('controlled fixture')
    for directory in (repo / 'tests', repo / 'sidecar/tests', repo / 'benchmarks', tmp_path / 'hermes-tests'):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'answers.json').write_text('{}')
    script = Path(__file__).resolve().parents[2] / 'benchmarks/paired/runtime_prune.py'
    original_path = pathlib.Path
    def sandbox_path(*parts):
        if parts == ('/opt/protagine',):
            return repo
        if parts == ('/opt/hermes/tests',):
            return tmp_path / 'hermes-tests'
        return original_path(*parts)
    # Execute the real build helper while redirecting only its two absolute roots.
    with monkeypatch.context() as patch:
        patch.setattr(pathlib, 'Path', sandbox_path)
        runpy.run_path(str(script), run_name='__test__')
    assert all((qualification / name).exists() for name in preserved)
    assert all(not (qualification / name).exists() for name in removed)
    assert not (repo / 'tests').exists() and not (repo / 'sidecar/tests').exists()
    assert not (repo / 'benchmarks').exists() and not (tmp_path / 'hermes-tests').exists()
