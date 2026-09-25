"""Lifecycle probes use real subprocesses and synthetic state, never a model."""
from copy import deepcopy
from contextlib import contextmanager
import io
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

from protagine.qualification import paired_worker as worker
from protagine.qualification import paired_workflow_runtime as runtime


def episodes(count=4):
    return [{'session_id': 'session-' + str(index), 'user': 'Synthetic work ' + str(index)}
            for index in range(count)]


def request():
    return {'binding': 'candidate', 'config': {'model': {'default': 'test'}},
        'inputs': {'arm': 'base_hermes', 'episodes': episodes(), 'initial_files': {'source.txt': 'seed'},
            'workflow': {'restart_before': [2], 'snapshot_after': [1, 3], 'read_failures': [
                {'turn_index': 2, 'path': 'source.txt', 'error': 'Read timed out; retry once.'}]}}}


@pytest.mark.parametrize('workflow', [None, [], {'unexpected': True},
    {'restart_before': [0]}, {'restart_before': [4]}, {'restart_before': [True]},
    {'restart_before': [2, 1]}, {'restart_before': [2, 2]}, {'snapshot_after': [-1]},
    {'snapshot_after': [4]}, {'snapshot_after': [False]}, {'read_failures': {}},
    {'read_failures': [{'turn_index': 0, 'path': '../secret', 'error': 'timeout'}]},
    {'read_failures': [{'turn_index': 0, 'path': 'nested/source', 'error': 'timeout'}]},
    {'read_failures': [{'turn_index': True, 'path': 'source', 'error': 'timeout'}]},
    {'read_failures': [{'turn_index': 0, 'path': 'source', 'error': ''}]},
    {'read_failures': [{'turn_index': 0, 'path': 'source', 'error': 'timeout', 'answer': 'oracle'}]},
])
def test_workflow_rejects_invalid_or_unbounded_events(workflow):
    with pytest.raises(ValueError):
        runtime.validate_workflow(workflow, episodes())


def test_workflow_requires_new_sessions_after_restart_but_allows_same_phase_conversation():
    turns = episodes()
    turns[1]['session_id'] = turns[0]['session_id']
    assert runtime.validate_workflow({'restart_before': [2]}, turns)['restart_before'] == [2]
    turns[2]['session_id'] = turns[0]['session_id']
    with pytest.raises(ValueError, match='fresh session'):
        runtime.validate_workflow({'restart_before': [2]}, turns)
    with pytest.raises(ValueError):
        runtime.validate_workflow({}, episodes(25))


def test_one_shot_fault_only_affects_exact_turn_and_path_and_snapshot_is_detached(tmp_path, monkeypatch):
    faults = runtime.TurnObservations(runtime.validate_workflow({
        'snapshot_after': [1], 'read_failures': [
            {'turn_index': 1, 'path': 'source.txt', 'error': 'Temporary read failure'}]}, episodes()))
    calls = []
    handlers = {name: SimpleNamespace(handler=lambda args, *a, **k: calls.append(args) or 'native result')
                for name in ('read_file', 'write_file', 'patch', 'search_files')}
    monkeypatch.setitem(sys.modules, 'tools', SimpleNamespace(file_tools=SimpleNamespace()))
    monkeypatch.setitem(sys.modules, 'tools.file_tools', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'tools.registry', SimpleNamespace(
        registry=SimpleNamespace(get_entry=handlers.get)))
    (tmp_path / 'source.txt').write_text('before')
    with worker.workspace_tools(tmp_path, workflow_observations=faults):
        faults.turn_index = 0
        assert handlers['read_file'].handler({'path': 'source.txt'}) == 'native result'
        faults.turn_index = 1
        assert 'outside' in handlers['read_file'].handler({'path': '../source.txt'})
        assert handlers['write_file'].handler({'path': 'source.txt'}) == 'native result'
        assert handlers['read_file'].handler({'path': 'other.txt'}) == 'native result'
        assert json.loads(handlers['read_file'].handler({'path': str(tmp_path / 'source.txt')})) == {
            'error': 'Temporary read failure'}
        assert handlers['read_file'].handler({'path': 'source.txt'}) == 'native result'
        faults.after_turn(tmp_path, worker.snapshot_workspace)
        (tmp_path / 'source.txt').write_text('after')
        faults.turn_index = 2
        faults.after_turn(tmp_path, worker.snapshot_workspace)
    assert len(calls) == 4
    assert faults.consumed == [{'turn_index': 1, 'path': 'source.txt', 'error': 'Temporary read failure'}]
    assert faults.snapshots == {'1': {'source.txt': 'before'}}


@pytest.mark.parametrize('result', ['not json', '{}', '{"success":true}',
    '{"error":"still unavailable"}', '{"content":"unreliable", "error":"failed"}',
    '{"content":"unreliable", "success":false}', '{"content":"unreliable", "is_error":true}',
    '{"content":null}', '[]'])
def test_failure_or_claimed_success_cannot_create_read_recovery(result):
    fault = {'turn_index': 1, 'path': 'source.txt', 'error': 'timeout'}
    observer = runtime.TurnObservations({'read_failures': [fault], 'snapshot_after': []})
    observer.turn_index = 1
    assert observer.read_failure('source.txt') == 'timeout'
    observer.after_read('source.txt', result)
    assert observer.read_recoveries == []


def test_successful_content_receipt_requires_prior_fault_and_survives_restart_without_content_copy():
    fault = {'turn_index': 1, 'path': 'source.txt', 'error': 'timeout'}
    spec = {'read_failures': [fault], 'snapshot_after': []}
    observer = runtime.TurnObservations(spec)
    result = json.dumps({'content': 'The error rate is 0.2 percent. Private synthetic detail.',
                         'total_lines': 1, 'file_size': 64, 'truncated': False})
    observer.turn_index = 0
    observer.after_read('source.txt', result)
    assert observer.read_recoveries == []
    observer.turn_index = 1
    observer.read_failure('source.txt')
    observer.after_read('other.txt', result)
    assert observer.read_recoveries == []
    observer.after_read('source.txt', result)
    assert observer.read_recoveries == [{'turn_index': 1, 'path': 'source.txt'}]
    resumed = runtime.TurnObservations(spec, consumed_before=observer.consumed)
    resumed.turn_index = 2
    resumed.after_read('source.txt', result)
    resumed.after_read('source.txt', result)
    assert resumed.read_recoveries == [{'turn_index': 2, 'path': 'source.txt'}]
    assert 'Private' not in json.dumps(resumed.read_recoveries)


FAKE_NATIVE_WORKER = r'''
import hashlib, json, os, pathlib, sys
request = json.load(sys.stdin)
phase = request['_workflow_phase']
home, workspace = map(pathlib.Path, request['test_roots'])
if phase['index'] == 0:
    home.mkdir()
    workspace.mkdir()
    for name, value in request['inputs']['initial_files'].items():
        (workspace / name).write_text(value)
    (home / 'durable-memory.txt').write_text('Remembered through disk, not replayed prompt')
else:
    assert (home / 'durable-memory.txt').read_text() == 'Remembered through disk, not replayed prompt'
    assert (workspace / 'source.txt').read_text() == 'changed-in-turn-1'
    assert 'conversation_history' not in request
turns, snapshots, consumed, ended = [], {}, [], None
for offset, turn in enumerate(request['inputs']['episodes']):
    index = phase['start_turn'] + offset
    if request.get('stop_turn') == index:
        turns.append({'index': index, 'session_id': turn['session_id'], 'completed': False,
                      'final_response': ''})
        ended = index
        break
    if request.get('capped_turn') == index:   # the iteration cap: incomplete, but it answered
        (workspace / 'source.txt').write_text('changed-in-turn-' + str(index))
        turns.append({'index': index, 'session_id': turn['session_id'], 'completed': False,
                      'final_response': 'Summary after the iteration limit'})
        continue
    (workspace / 'source.txt').write_text('changed-in-turn-' + str(index))
    turns.append({'index': index, 'session_id': turn['session_id'], 'completed': True,
                  'final_response': 'Done'})
    if index in phase['workflow']['snapshot_after']:
        snapshots[str(index)] = {'source.txt': (workspace / 'source.txt').read_text()}
    consumed.extend(fault for fault in phase['workflow']['read_failures'] if fault['turn_index'] == index)
for kind in ('model_request', 'model_response'):
    print('PROTAGINE_PAIRED_DIAGNOSTIC:' + json.dumps({'protocol': 'paired-private-trace-1',
        'sequence': 1, 'thread': 'paired-source-worker' if phase['index'] else 'MainThread',
        'kind': kind, 'data': {'request_id': 1}}), file=sys.stderr)
result = {'stage': 'returned', 'worker_stopped': True, 'agent_close_returned': True,
    'workflow_phase': {'index': phase['index'], 'pid': os.getpid()},
    'private_diagnostics': {'dropped': 0, 'errors': 0, 'truncated': 0}, 'output': 'Done',
    'tool_evidence': {'turns': turns, 'turns_completed': sum(turn['completed'] for turn in turns),
        'model_requests': [{'trace_request_id': 1, 'usage': {'prompt_tokens': 10, 'completion_tokens': 2}}],
        'artifacts': {'source.txt': (workspace / 'source.txt').read_text()},
        'native_memory_enabled': True, 'session_search_enabled': True, 'treatment_loaded': False,
        'workflow_observations': {'snapshots': snapshots, 'read_failures_consumed': consumed},
        **({'ended_at': ended} if ended is not None else {})}}
print('PROTAGINE_PAIRED_RESULT:' + json.dumps(result), flush=True)
'''


def subprocess_fixture(tmp_path, payload):
    command = [sys.executable, '-I', '-c', FAKE_NATIVE_WORKER]
    home, workspace = tmp_path / 'home', tmp_path / 'workspace'
    payload['test_roots'] = [str(home), str(workspace)]
    lines = []
    trace = runtime.TraceForwarder(sink=lines.append)

    def launch(request, stop, recorder):
        return runtime.run_phase(request, stop, recorder, command=command)

    result = runtime.supervise(payload, home=home, workspace=workspace, runner=launch, trace=trace)
    return result, [json.loads(line) for line in lines]


def test_real_process_restart_preserves_disk_without_reseed_and_keeps_trace_ids_unique(tmp_path):
    payload = request()
    original = deepcopy(payload)
    result, trace = subprocess_fixture(tmp_path, payload)
    assert result['stage'] == 'returned' and result['agent_close_returned'] is True
    effects = result['tool_evidence']
    assert effects['declared_turns'] == effects['turns_completed'] == 4
    lifecycle = effects['workflow']
    first, second = lifecycle['phases']
    assert first['pid'] != second['pid']
    assert first['pid'] == first['worker_pid'] and second['pid'] == second['worker_pid']
    assert first['state_after'] == second['state_before']
    assert lifecycle['restarts_completed'] == 1 and lifecycle['state_preserved'] is True
    assert lifecycle['all_declared_turns_attempted'] and lifecycle['all_phases_closed']
    assert lifecycle['snapshots'] == {
        '1': {'source.txt': 'changed-in-turn-1'}, '3': {'source.txt': 'changed-in-turn-3'}}
    assert lifecycle['read_failures_consumed'] == original['inputs']['workflow']['read_failures']
    assert effects['artifacts'] == {'source.txt': 'changed-in-turn-3'}
    identities = [row['trace_request_id'] for row in effects['model_requests']]
    assert identities == [1, runtime.REQUEST_ID_STRIDE + 1]
    assert [event['data']['request_id'] for event in trace] == [identities[0]] * 2 + [identities[1]] * 2
    assert [event['thread'] for event in trace] == ['MainThread'] * 2 + ['paired-source-worker'] * 2
    assert [event['sequence'] for event in trace] == [1, 2, 3, 4]
    assert effects['resource_usage']['observed_input_tokens'] == 20
    assert payload['inputs'] == original['inputs']


def test_partial_native_turn_stops_workflow_without_inventing_later_observations(tmp_path):
    payload = request()
    payload['stop_turn'] = 1
    result, _ = subprocess_fixture(tmp_path, payload)
    assert result['stage'] == 'returned'
    effects = result['tool_evidence']
    assert effects['declared_turns'] == 4 and effects['turns_completed'] == 1
    assert len(effects['turns']) == 2
    assert len(effects['workflow']['phases']) == 1
    assert effects['workflow']['restarts_completed'] == 0
    assert effects['workflow']['all_declared_turns_attempted'] is False
    assert effects['workflow']['read_failures_consumed'] == []
    assert effects['workflow']['snapshots'] == {}


def test_a_capped_turn_that_answered_does_not_end_the_workflow(tmp_path):
    """A turn that spent Hermes' iteration budget still answered, and the worker goes on past it; the
    supervisor did not, so a cap at formation also erased the probe after the restart (the pilots' I3).
    The cap stays recorded: turns_completed counts it out."""
    payload = request()
    payload['capped_turn'] = 1
    result, _ = subprocess_fixture(tmp_path, payload)
    assert result['stage'] == 'returned'
    effects = result['tool_evidence']
    assert effects['declared_turns'] == 4 and effects['turns_completed'] == 3 and len(effects['turns']) == 4
    assert effects['turns'][1]['completed'] is False and effects['turns'][1]['final_response']
    assert len(effects['workflow']['phases']) == 2 and effects['workflow']['restarts_completed'] == 1
    assert effects['workflow']['all_declared_turns_attempted'] is True


def test_supervisor_refuses_preexisting_state(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    with pytest.raises(ValueError, match='initially fresh'):
        runtime.supervise(request(), home=home, workspace=tmp_path / 'workspace')


def test_child_exit_failure_cannot_become_successful_restart(tmp_path):
    payload = request()
    home, workspace = tmp_path / 'home', tmp_path / 'workspace'

    def failed_child(request, stop, trace):
        home.mkdir()
        workspace.mkdir()
        return {'pid': 1234, 'exit_code': 1, 'result': {'stage': 'error', 'workflow_phase': {
            'index': 0, 'pid': 1234}, 'worker_stopped': True, 'agent_close_returned': False,
            'error_type': 'ValueError', 'error_origin_stage': 'preparing',
            'private_error_traceback': 'synthetic-private-setup-detail'}}

    result = runtime.supervise(payload, home=home, workspace=workspace, runner=failed_child)
    assert result['stage'] == 'error' and result['worker_stopped'] is True
    assert result['tool_evidence']['workflow']['restarts_completed'] == 0
    assert result['tool_evidence']['workflow']['all_phases_closed'] is False
    assert result['tool_evidence']['workflow']['phases'][0]['error_type'] == 'ValueError'
    assert result['private_phase_errors'] == [{'phase': 0, 'error_type': 'ValueError',
        'error_origin_stage': 'preparing', 'private_error_traceback': 'synthetic-private-setup-detail'}]
    assert 'synthetic-private-setup-detail' not in json.dumps(result['tool_evidence'])


def test_missing_or_duplicate_result_is_not_accepted_as_completed_phase():
    payload = request()
    payload['_workflow_phase'] = {'index': 0}
    for script in ('pass', "print('PROTAGINE_PAIRED_RESULT:{}\\nPROTAGINE_PAIRED_RESULT:{}')"):
        with pytest.raises(ValueError, match='exactly one result'):
            runtime.run_phase(payload, threading.Event(), runtime.TraceForwarder(sink=lambda _: None),
                command=[sys.executable, '-I', '-c', script])


def test_fingerprint_detects_content_edits_without_copying_contents(tmp_path):
    home, workspace = tmp_path / 'home', tmp_path / 'workspace'
    home.mkdir()
    workspace.mkdir()
    secret = 'synthetic-private-canary'
    (home / 'memory').write_text(secret)
    before = runtime.state_fingerprint(home, workspace)
    assert secret not in json.dumps(before)
    assert before == runtime.state_fingerprint(home, workspace)
    (home / 'memory').write_text('replacement')
    assert before != runtime.state_fingerprint(home, workspace)


def test_trace_budget_is_global_and_ambiguous_request_ids_are_rejected(monkeypatch):
    lines = []
    trace = runtime.TraceForwarder(sink=lines.append)
    event = {'protocol': 'paired-private-trace-1', 'thread': 'MainThread',
             'kind': 'model_request', 'data': {'request_id': 1}}
    raw = runtime.TRACE_MARKER + json.dumps(event)
    monkeypatch.setattr(runtime, 'MAX_TRACE_BYTES', 350)
    for phase in range(8):
        assert trace.accept(raw, phase) is True
    assert trace.bytes <= 350 and trace.dropped > 0
    assert trace.accept('ordinary log', 0) is False
    event['data']['request_id'] = True
    trace.accept(runtime.TRACE_MARKER + json.dumps(event), 0)
    assert trace.errors == 1


@pytest.mark.parametrize('arm', ['base_hermes', 'protagine', 'full-x', 'base-y'])
def test_native_worker_phase_wiring_seeds_once_uses_global_turns_and_snapshots(tmp_path, monkeypatch, capsys, arm):
    """Exercise the real worker loop with stub native APIs, not its model calls."""
    from protagine.qualification import paired_transport

    real_path = Path
    monkeypatch.setattr(worker, 'Path', lambda name: real_path(
        str(name).replace('/state/', str(tmp_path) + '/', 1)))
    monkeypatch.setattr(worker.os, 'environ', dict(worker.os.environ))
    monkeypatch.setattr(worker.os, 'chdir', lambda _: None)
    monkeypatch.setattr(worker.signal, 'signal', lambda *_: None)
    current = {}
    calls = []
    handlers = {name: SimpleNamespace(handler=lambda args, *a, **k: json.dumps({
                    'content': real_path(args['path']).read_text(), 'total_lines': 1, 'file_size': 32}))
                for name in ('read_file', 'write_file', 'patch', 'search_files')}
    monkeypatch.setitem(sys.modules, 'tools', SimpleNamespace(file_tools=SimpleNamespace()))
    monkeypatch.setitem(sys.modules, 'tools.file_tools', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'tools.registry', SimpleNamespace(
        registry=SimpleNamespace(get_entry=handlers.get)))
    monkeypatch.setitem(sys.modules, 'hermes_cli', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'hermes_cli.config', SimpleNamespace(
        load_config=lambda: deepcopy(current['config'])))
    monkeypatch.setitem(sys.modules, 'hermes_cli.runtime_provider', SimpleNamespace(
        resolve_runtime_provider=lambda **_: {'base_url': 'http://model.invalid/v1'}))
    monkeypatch.setitem(sys.modules, 'hermes_constants', SimpleNamespace(resolve_reasoning_config=lambda *_: {}))
    monkeypatch.setitem(sys.modules, 'hermes_state', SimpleNamespace(SessionDB=lambda _: None))
    # The body clock and tick need real Hermes modules; this probe exercises the turn loop only.
    monkeypatch.setattr(worker.paired_body, 'install_clock', lambda offset: float(offset))
    monkeypatch.setattr(worker.paired_body, 'advance_clock', lambda seconds: float(seconds))
    monkeypatch.setattr(worker.paired_body, 'protagine_tick_entry', lambda: None)
    drained = []
    monkeypatch.setattr(worker, 'drain_background', lambda path, **kwargs: drained.append(path.name) or {
        'status': 'drained', 'waited_seconds': 0.0, 'left': {}})

    agent_kwargs = []

    class NativeAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs['session_id']
            agent_kwargs.append(kwargs)

        def run_conversation(self, user, *, system_message, conversation_history):
            assert conversation_history is None
            index = int(user.rsplit(' ', 1)[1])
            read = handlers['read_file'].handler({'path': 'source.txt'})
            if index == 2:
                assert json.loads(read)['error'] == 'Read timed out; retry once.'
                read = handlers['read_file'].handler({'path': 'source.txt'})
            assert json.loads(read)['content'] == ('seed' if index == 0 else 'changed-in-turn-' + str(index-1))
            (tmp_path / 'workspace' / 'source.txt').write_text('changed-in-turn-' + str(index))
            calls.append(index)
            return {'completed': True, 'messages': [], 'final_response': 'Done'}

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, 'run_agent', SimpleNamespace(AIAgent=NativeAgent))
    # The worker pins the frozen runtime and limits on cron's agent-construction seam.
    cron = SimpleNamespace(scheduler=SimpleNamespace(
        _construct_cron_agent=lambda AIAgent, job, config, setup, **kwargs: AIAgent(model='cron-model')))
    monkeypatch.setitem(sys.modules, 'cron', cron)
    monkeypatch.setitem(sys.modules, 'cron.scheduler', cron.scheduler)

    observed = []
    lifecycle = []

    @contextmanager
    def no_model_requests(*_, **kwargs):
        observed.clear()
        lifecycle.append('observe-start')
        try:
            yield observed
        finally:
            lifecycle.append('observe-end')

    overlays = []

    @contextmanager
    def provider_setup(*args, **kwargs):
        overlays.append(kwargs.get('overlay'))
        assert lifecycle[-1] == 'observe-start'
        observed.append({'usage': {'prompt_tokens': 11, 'completion_tokens': 3}})
        lifecycle.append('source-start')
        try:
            yield lambda *args: {'memory_provider_loaded': True}
        finally:
            observed.append({'usage': {'prompt_tokens': 19, 'completion_tokens': 7}})
            lifecycle.append('source-stop')

    monkeypatch.setitem(sys.modules, 'protagine.qualification.native_memory_worker',
                        SimpleNamespace(prepare=provider_setup))
    monkeypatch.setitem(sys.modules, 'toolsets', SimpleNamespace(create_custom_toolset=lambda *a, **k: None))
    monkeypatch.setattr(paired_transport, 'observe_requests', no_model_requests)
    payload = request()
    payload['inputs'].update(arm=arm, max_iterations=8, max_output_tokens=512, settle_seconds=0)
    # Labels beyond the built-in pair carry a frozen profile; one pins the temperature too.
    profiles = {'full-x': {'name': 'full-x', 'plugin': True, 'overlay': {'PROTAGINE_TEST_FACULTY': 'off'}},
                'base-y': {'name': 'base-y', 'plugin': False, 'overlay': {'PROTAGINE_TEST_FACULTY': 'on'}}}
    plugin = arm in {'protagine', 'full-x'}
    if arm in profiles:
        payload['inputs']['profile'] = profiles[arm]
    if arm == 'full-x':
        payload['temperature'] = 0.0
    for phase in range(2):
        chunk = deepcopy(payload)
        chunk['inputs']['episodes'] = chunk['inputs']['episodes'][phase*2:phase*2+2] + [{'advance_clock': 60}] * phase
        chunk['_workflow_phase'] = {'index': phase, 'start_turn': phase*2,
                                    'workflow': payload['inputs']['workflow']}
        current.update(chunk)
        monkeypatch.setattr(sys, 'argv', ['paired_worker', '--workflow-phase'])
        monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(chunk)))
        assert worker.main() == 0
        output = capsys.readouterr()
        result = json.loads(next(line[len(runtime.RESULT_MARKER):] for line in output.out.splitlines()
                                 if line.startswith(runtime.RESULT_MARKER)))
        assert [turn['index'] for turn in result['tool_evidence']['turns']] == [phase*2, phase*2+1, 4][:2 + phase]
        # The arm's queues drain before the declared restart that ends phase 0 and before the clock advance.
        assert [(row['index'], row['before']) for row in result['tool_evidence']['drains']] == (
            [(4, 'advance_clock')] if phase else [(2, 'restart')])
        assert result['tool_evidence']['arm_profile'] == (profiles[arm] if arm in profiles else worker.LEGACY_PROFILES[arm])
        assert result['tool_evidence']['temperature'] == (0.0 if arm == 'full-x' else None)
        if plugin:
            assert lifecycle[-4:] == ['observe-start', 'source-start', 'source-stop', 'observe-end']
            assert result['tool_evidence']['resource_usage']['observed_input_tokens'] == 30
            assert result['tool_evidence']['resource_usage']['observed_output_tokens'] == 10
            assert len(result['tool_evidence']['model_requests']) == 2
        observations = result['tool_evidence']['workflow_observations']
        assert observations['snapshots'] == {str(phase*2+1): {
            'source.txt': 'changed-in-turn-' + str(phase*2+1)}}
        assert len(observations['read_failures_consumed']) == phase
        assert observations['read_recoveries'] == ([] if phase == 0 else [
            {'turn_index': 2, 'path': 'source.txt'}, {'turn_index': 3, 'path': 'source.txt'}])
    assert calls == [0, 1, 2, 3] and drained == ['turn-idempotency.db'] * 2
    assert overlays == ([profiles[arm]['overlay']] * 2 if arm == 'full-x' else [{}] * 2 if plugin else [])
    if arm == 'base-y':
        assert worker.os.environ['PROTAGINE_TEST_FACULTY'] == 'on'
    expected = {'extra_body': {'temperature': 0.0}} if arm == 'full-x' else None
    assert all(kwargs.get('request_overrides') == expected for kwargs in agent_kwargs) and len(agent_kwargs) == 4


def test_candidate_compatibility_body_preserved_without_mutating_recipe():
    recipe = {'request_overrides': {'extra_body': {
        'chat_template_kwargs': {'enable_thinking': False}, 'reasoning_effort': 'low'}}}
    body = worker.candidate_extra_body(recipe)
    assert body == recipe['request_overrides']['extra_body']
    body['chat_template_kwargs']['enable_thinking'] = True
    assert recipe['request_overrides']['extra_body']['chat_template_kwargs']['enable_thinking'] is False


@pytest.mark.parametrize('recipe', [
    {'request_overrides': {'extra_body': {'max_tokens': 9999}}},
    {'request_overrides': {'extra_body': {'model': 'other'}}},
    {'request_overrides': {'extra_body': ['invalid']}},
    {'request_overrides': {'temperature': 0.5}},
    {'extra_headers': {'X-Custom': 'unsupported'}},
])
def test_candidate_compatibility_rejects_silently_dropped_or_budget_overriding_fields(recipe):
    with pytest.raises(ValueError):
        worker.candidate_extra_body(recipe)


def _ledger(path, rows):
    import sqlite3
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE source_claim_jobs (turn_id TEXT, status TEXT, next_attempt REAL)')
        conn.execute('CREATE TABLE opinion_jobs (ref TEXT, done_at REAL, next_attempt REAL)')
        for table, row in rows:
            conn.execute(f'INSERT INTO {table} VALUES (?,?,?)', row)


def _set(path, sql):
    import sqlite3
    with sqlite3.connect(path) as conn:
        conn.execute(sql)


def test_the_background_drain_waits_for_owed_work_and_records_what_it_left(tmp_path):
    """Before a restart or a clock jump the harness waits for the arm's ledger: work claimable now and work
    running. It never processes a job itself, never waits for one backing off, never for a queue nobody works,
    and never past its budget."""
    import time as clock

    path = tmp_path / 'turn-idempotency.db'
    assert worker.drain_background(path) == {'status': 'no_queue', 'waited_seconds': 0.0, 'left': {}}
    later = clock.time() + 3600
    _ledger(path, [('source_claim_jobs', ('t1', 'pending', 0)), ('opinion_jobs', ('turn:t1', None, 0)),
                   ('source_claim_jobs', ('t2', 'pending', later)), ('source_claim_jobs', ('t0', 'complete', 0))])
    steps = iter(["UPDATE source_claim_jobs SET status='running' WHERE turn_id='t1'",
                  "UPDATE source_claim_jobs SET status='complete' WHERE turn_id='t1'",
                  "UPDATE opinion_jobs SET done_at=1 WHERE ref='turn:t1'"])

    def worker_step(_seconds):   # the source worker, one step per poll
        step = next(steps, None)
        if step:
            _set(path, step)
    drained = worker.drain_background(path, seconds=30, idle=5, wait=worker_step)
    assert drained['status'] == 'drained'
    # Only the job backing off is left, and it is reported.
    assert drained['left'] == {'source_claim_jobs': {'owed': 0, 'running': 0, 'deferred': 1}}
    _set(path, "INSERT INTO opinion_jobs VALUES ('turn:t3', NULL, 0)")   # owed, and no lane works it
    assert worker.drain_background(path, seconds=30, idle=0.05, poll=0.01)['status'] == 'idle'
    _set(path, "INSERT INTO source_claim_jobs VALUES ('t4', 'running', 0)")   # a model call that never returns
    budget = worker.drain_background(path, seconds=0.05, idle=5, poll=0.01)
    assert budget['status'] == 'budget' and budget['left']['source_claim_jobs']['running'] == 1
    before = path.read_bytes()
    assert worker.drain_background(path, seconds=30, wait=lambda _: True)['status'] == 'stopped'
    assert path.read_bytes() == before   # it only reads


def test_shutdown_backlog_is_read_only_and_missing_is_not_empty(tmp_path):
    import sqlite3
    path = tmp_path / 'turn-idempotency.db'
    assert worker.source_job_counts(path) == {'status': 'unavailable'}
    assert not path.exists()
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE source_claim_jobs (status TEXT,lease_until REAL)')
        conn.executemany('INSERT INTO source_claim_jobs VALUES (?,?)',
                         [('running', 1234), ('pending', 0), ('complete', 0), ('complete', 0)])
    # The capture queue is its own table in the same ledger; without it nothing is observed.
    assert worker.source_job_counts(path) == {'status': 'unavailable'}
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE commitment_runs (turn_id TEXT PRIMARY KEY, status TEXT, lease_until REAL)')
        conn.executemany('INSERT INTO commitment_runs VALUES (?,?,?)',
                         [('t1', 'complete', 0), ('t2', 'running', 1234), ('t3', 'pending', 0), ('t4', 'pending', 0)])
    before = path.read_bytes()
    assert worker.source_job_counts(path) == {'status': 'observed',
        'counts': {'complete': 2, 'pending': 1, 'running': 1},
        'commitment_runs': {'complete': 1, 'pending': 2, 'running': 1}}
    assert path.read_bytes() == before


def test_arm_profiles_resolve_the_built_in_pair_and_reject_unknown_labels():
    assert worker.arm_profile({'arm': 'base_hermes'}) == worker.LEGACY_PROFILES['base_hermes']
    assert worker.arm_profile({'arm': 'protagine'})['plugin'] is True
    declared = {'name': 'full-x', 'plugin': True, 'overlay': {'PROTAGINE_TEST_FACULTY': 'off'}}
    assert worker.arm_profile({'arm': 'full-x', 'profile': declared}) == declared
    for inputs in ({'arm': 'full-x'}, {'arm': 'x', 'profile': {'plugin': 'yes', 'overlay': {}}},
                   {'arm': 'x', 'profile': {'plugin': True, 'overlay': {'HOME': '/x'}}},
                   {'arm': 'x', 'profile': {'plugin': True, 'overlay': {'PROTAGINE_FLAG': 1}}}):
        with pytest.raises(ValueError, match='Unknown experiment arm'):
            worker.arm_profile(inputs)


def test_pinned_temperature_keeps_the_candidate_compatibility_body():
    runtime = {'base_url': 'http://model.invalid/v1',
               'request_overrides': {'extra_body': {'chat_template_kwargs': {'enable_thinking': False}}}}
    assert worker.pinned_runtime(runtime, None) is runtime
    pinned = worker.pinned_runtime(runtime, 0.0)
    assert pinned['request_overrides'] == {'extra_body': {
        'chat_template_kwargs': {'enable_thinking': False}, 'temperature': 0.0}}
    assert runtime['request_overrides']['extra_body'] == {'chat_template_kwargs': {'enable_thinking': False}}
    assert worker.pinned_runtime({'base_url': 'x'}, 0.7)['request_overrides'] == {'extra_body': {'temperature': 0.7}}
    with pytest.raises(ValueError):
        worker.pinned_runtime({'request_overrides': {'extra_body': {'model': 'other'}}}, 0.0)
