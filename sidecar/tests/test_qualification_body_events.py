"""Episode events, their ordering across restarts and the worker loop, with no model."""
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from protagine.qualification import paired_body
from protagine.qualification import paired_worker as worker
from protagine.qualification import paired_workflow_runtime as runtime

USER = {'session_id': 'owner-1', 'user': 'Remind me about the invoice tomorrow.'}
INBOUND = {'session_id': 'contact-1', 'inbound': {'contact': 'p-03', 'channel': 'chat', 'text': 'Any news?'}}
REACTION = {'session_id': 'owner-2', 'owner_reaction': {'text': 'Please do not remind me again.'}}
TICK = {'tick': 2}
CLOCK = {'advance_clock': 3600}


@pytest.mark.parametrize('entry, kind', [(USER, 'user'), (INBOUND, 'inbound'),
                                         (REACTION, 'owner_reaction'), (TICK, 'tick'), (CLOCK, 'advance_clock')])
def test_declared_event_kinds_are_recognized(entry, kind):
    assert runtime.episode_kind(entry) == kind


@pytest.mark.parametrize('entry', [
    None, [], {}, {'tick': 0}, {'tick': 17}, {'tick': True}, {'tick': 1, 'user': 'x'},
    {'advance_clock': 0}, {'advance_clock': 1.5}, {'advance_clock': 400 * 86400},
    {'session_id': 'a', 'user': ''}, {'session_id': '', 'user': 'x'}, {'session_id': 'a'},
    {'session_id': 'a', 'user': 'x', 'inbound': {}},
    {'session_id': 'a', 'inbound': {'contact': 'p-3', 'channel': 'chat', 'text': 'hi'}},
    {'session_id': 'a', 'inbound': {'contact': 'p-03', 'channel': 'chat'}},
    {'session_id': 'a', 'inbound': {'contact': 'p-03', 'channel': 'chat', 'text': 'hi', 'extra': 1}},
    {'session_id': 'a', 'inbound': {'contact': 'p-03', 'channel': 'bad channel', 'text': 'hi'}},
    {'session_id': 'a', 'owner_reaction': 'text'}, {'session_id': 'a', 'owner_reaction': {'text': ' '}},
    {'session_id': 'a', 'unknown': 'x'},
])
def test_malformed_entries_are_rejected(entry):
    with pytest.raises(ValueError):
        runtime.episode_kind(entry)


def test_episodes_need_at_least_one_agent_turn_and_report_kinds():
    assert runtime.validate_episodes([USER, TICK, CLOCK, TICK, INBOUND]) == [
        'user', 'tick', 'advance_clock', 'tick', 'inbound']
    for episodes in ([], [TICK], [TICK, CLOCK], 'user'):
        with pytest.raises(ValueError):
            runtime.validate_episodes(episodes)


def test_body_before_accumulates_clock_and_ticks_in_order():
    episodes = [USER, TICK, CLOCK, {'tick': 1}, {'advance_clock': 60}, REACTION]
    assert runtime.body_before(episodes, 0) == {'clock_offset_seconds': 0, 'ticks_completed': 0}
    assert runtime.body_before(episodes, 3) == {'clock_offset_seconds': 3600, 'ticks_completed': 2}
    assert runtime.body_before(episodes, 6) == {'clock_offset_seconds': 3660, 'ticks_completed': 3}


def test_workflow_restart_freshness_ignores_events_and_allows_event_boundaries():
    episodes = [USER, TICK, CLOCK, {'session_id': 'owner-3', 'user': 'later'}, TICK]
    contract = runtime.validate_workflow({'restart_before': [2], 'snapshot_after': [1, 4]}, episodes)
    assert contract['restart_before'] == [2] and contract['snapshot_after'] == [1, 4]
    reused = [USER, TICK, {'session_id': 'owner-1', 'user': 'again'}]
    with pytest.raises(ValueError, match='fresh session'):
        runtime.validate_workflow({'restart_before': [1]}, reused)
    with pytest.raises(ValueError):
        runtime.validate_workflow({}, [TICK, CLOCK])


FAKE_BODY_WORKER = r'''
import json, os, pathlib, sys
request = json.load(sys.stdin)
phase = request['_workflow_phase']
home, workspace = map(pathlib.Path, request['test_roots'])
outbox = home.parent / 'outbox.json'
if phase['index'] == 0:
    home.mkdir(); workspace.mkdir()
before = phase['body_before']
rows, ticks, tick = [], [], before['ticks_completed']
offset = before['clock_offset_seconds']
for offset_index, entry in enumerate(request['inputs']['episodes']):
    index = phase['start_turn'] + offset_index
    if 'tick' in entry:
        for _ in range(entry['tick']):
            tick += 1
            ticks.append({'index': index, 'tick': tick, 'outbox_before': tick - 1, 'outbox_after': tick,
                          'created_task_ids': [], 'kanban': [], 'cron_jobs_run': 1})
            sent = json.loads(outbox.read_text()) if outbox.exists() else []
            sent.append({'target': 'capture:owner', 'text': 'tick %d at offset %d' % (tick, offset), 'at': 'x', 'via': 'platform'})
            outbox.write_text(json.dumps(sent))
        rows.append({'index': index, 'event': 'tick', 'completed': True})
    elif 'advance_clock' in entry:
        offset += entry['advance_clock']
        rows.append({'index': index, 'event': 'advance_clock', 'completed': True, 'clock_offset_seconds': offset})
    else:
        rows.append({'index': index, 'session_id': entry['session_id'], 'completed': True, 'final_response': 'Done'})
result = {'stage': 'returned', 'worker_stopped': True, 'agent_close_returned': True,
    'workflow_phase': {'index': phase['index'], 'pid': os.getpid()},
    'private_diagnostics': {'dropped': 0, 'errors': 0, 'truncated': 0}, 'output': 'Done',
    'tool_evidence': {'turns': rows, 'turns_completed': len(rows), 'model_requests': [], 'artifacts': {},
        'body': {'protocol': 'paired-body-tick-1', 'ticks': ticks, 'clock_offset_seconds': offset,
                 'outbox': json.loads(outbox.read_text()) if outbox.exists() else []},
        'workflow_observations': {'snapshots': {}, 'read_failures_consumed': []}}}
print('PROTAGINE_PAIRED_RESULT:' + json.dumps(result), flush=True)
'''


def test_supervisor_carries_clock_and_tick_counts_across_restarts_and_merges_ticks(tmp_path):
    episodes = [USER, TICK, CLOCK, {'session_id': 'owner-3', 'user': 'later'}, {'tick': 1}]
    payload = {'binding': 'candidate', 'config': {}, 'inputs': {'arm': 'base_hermes', 'episodes': episodes,
        'initial_files': {}, 'workflow': {'restart_before': [3]}}}
    home, workspace = tmp_path / 'home', tmp_path / 'workspace'
    payload['test_roots'] = [str(home), str(workspace)]
    seen = []

    def launch(request, stop, recorder):
        seen.append(deepcopy(request['_workflow_phase']['body_before']))
        return runtime.run_phase(request, stop, recorder,
                                 command=[sys.executable, '-I', '-c', FAKE_BODY_WORKER])

    result = runtime.supervise(payload, home=home, workspace=workspace, runner=launch,
                               trace=runtime.TraceForwarder(sink=lambda _: None))
    assert result['stage'] == 'returned'
    assert seen == [{'clock_offset_seconds': 0, 'ticks_completed': 0},
                    {'clock_offset_seconds': 3600, 'ticks_completed': 2}]
    effects = result['tool_evidence']
    assert effects['declared_turns'] == effects['turns_completed'] == 5
    body = effects['body']
    assert body['protocol'] == 'paired-body-tick-1' and body['clock_offset_seconds'] == 3600
    assert [row['tick'] for row in body['ticks']] == [1, 2, 3]
    assert [row['index'] for row in body['ticks']] == [1, 1, 4]
    assert [entry['text'] for entry in body['outbox']] == [
        'tick 1 at offset 0', 'tick 2 at offset 0', 'tick 3 at offset 3600']


@pytest.fixture
def stubbed_hermes(tmp_path, monkeypatch):
    """Stub native APIs so the real worker loop runs its turns and events without a model."""
    from protagine.qualification import paired_transport
    real_path = Path
    monkeypatch.setattr(worker, 'Path', lambda name: real_path(
        str(name).replace('/state/', str(tmp_path) + '/', 1)))
    monkeypatch.setattr(worker.os, 'environ', dict(worker.os.environ))
    monkeypatch.setattr(worker.os, 'chdir', lambda _: None)
    monkeypatch.setattr(worker.signal, 'signal', lambda *_: None)
    handlers = {name: SimpleNamespace(handler=lambda args, *a, **k: 'native result')
                for name in ('read_file', 'write_file', 'patch', 'search_files')}
    monkeypatch.setitem(sys.modules, 'tools', SimpleNamespace(file_tools=SimpleNamespace()))
    monkeypatch.setitem(sys.modules, 'tools.file_tools', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'tools.registry', SimpleNamespace(
        registry=SimpleNamespace(get_entry=handlers.get)))
    monkeypatch.setitem(sys.modules, 'hermes_cli', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'hermes_cli.config', SimpleNamespace(
        load_config=lambda: json.loads((tmp_path / 'home' / 'config.yaml').read_text())))
    monkeypatch.setitem(sys.modules, 'hermes_cli.runtime_provider', SimpleNamespace(
        resolve_runtime_provider=lambda **_: {'base_url': 'http://model.invalid/v1'}))
    monkeypatch.setitem(sys.modules, 'hermes_constants', SimpleNamespace(resolve_reasoning_config=lambda *_: {}))
    monkeypatch.setitem(sys.modules, 'hermes_state', SimpleNamespace(SessionDB=lambda _: None))
    monkeypatch.setitem(sys.modules, 'hermes_time', SimpleNamespace(now=lambda: datetime(2027, 3, 4, 9)))
    calls, systems = [], []

    class NativeAgent:
        def __init__(self, **kwargs):
            self.session_id, self.platform = kwargs['session_id'], kwargs['platform']

        def run_conversation(self, user, *, system_message, conversation_history):
            calls.append((self.session_id, self.platform, user))
            systems.append(system_message)
            return {'completed': True, 'messages': [], 'final_response': 'Reply to ' + user.splitlines()[-1]}

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, 'run_agent', SimpleNamespace(AIAgent=NativeAgent))

    class StubCronAgent(dict):
        """What cron constructs: its keyword arguments, plus the run the scheduler calls."""
        def run_conversation(self, prompt, **kwargs):
            cron_prompts.append(prompt)
            cron_systems.append(kwargs.get('system_message'))
            return {'completed': True}

    # Cron builds its agent through this seam; the worker pins the frozen runtime and limits on it.
    cron = SimpleNamespace(scheduler=SimpleNamespace(
        _construct_cron_agent=lambda AIAgent, job, config, setup, **kwargs: AIAgent(model='cron-model')))
    monkeypatch.setitem(sys.modules, 'cron', cron)
    monkeypatch.setitem(sys.modules, 'cron.scheduler', cron.scheduler)

    @contextmanager
    def no_model_requests(*_, **kwargs):
        yield []

    monkeypatch.setattr(paired_transport, 'observe_requests', no_model_requests)
    events, cron_agents, cron_prompts, cron_systems = [], [], [], []
    offset = [0.0]
    monkeypatch.setattr(paired_body, 'install_clock', lambda seconds: events.append(('install', seconds)))
    monkeypatch.setattr(paired_body, 'clock_offset', lambda: offset[0])
    monkeypatch.setattr(paired_body, 'protagine_tick_entry', lambda: None)

    def advance(seconds):
        offset[0] += seconds
        events.append(('advance', seconds))
        return offset[0]

    def run_tick(*, outbox, arm_tick, run_task, wait_seconds):
        events.append(('tick', wait_seconds, arm_tick, callable(run_task)))
        # What a cron job firing in this tick would construct its agent with.
        setup = SimpleNamespace(runtime={'base_url': 'http://model.invalid/v1'}, max_iterations=None)
        agent = sys.modules['cron.scheduler']._construct_cron_agent(
            StubCronAgent, {}, {}, setup, workdir=None, session_id='cron', session_db=None)
        cron_agents.append({'runtime': setup.runtime, 'max_iterations': setup.max_iterations, 'agent': agent})
        # The prompt a cron run would hand its agent, as the worker's seam presents it.
        agent.run_conversation('[Heartbeat]\nCheck.')
        recorded = paired_body.read_outbox(outbox) or []
        return {'outbox_before': len(recorded), 'outbox_after': len(recorded), 'cron_jobs_run': 0,
                'dispatch': {'spawned': 0}, 'workers': [], 'kanban': [], 'created_task_ids': [], 'arm_tick': None}

    monkeypatch.setattr(paired_body, 'advance_clock', advance)
    monkeypatch.setattr(paired_body, 'run_tick', run_tick)
    return SimpleNamespace(calls=calls, systems=systems, events=events, cron_agents=cron_agents,
                           cron_prompts=cron_prompts, cron_systems=cron_systems, root=tmp_path)


def run_worker(monkeypatch, capsys, request):
    monkeypatch.setattr(sys, 'argv', ['paired_worker'])
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(request)))
    code = worker.main()
    output = capsys.readouterr().out
    result = json.loads(next(line[len(worker.RESULT_MARKER):] for line in output.splitlines()
                             if line.startswith(worker.RESULT_MARKER)))
    return code, result


def test_worker_runs_turns_and_events_in_order_and_records_inbound_replies(stubbed_hermes, monkeypatch, capsys):
    request = {'binding': 'candidate', 'config': {'model': {'default': 'test'}},
        'inputs': {'arm': 'base_hermes', 'initial_files': {}, 'max_iterations': 4, 'max_output_tokens': 64,
                   'settle_seconds': 0, 'worker_wait_seconds': 7,
                   'episodes': [USER, TICK, CLOCK, INBOUND, {'tick': 1}, REACTION]}}
    code, result = run_worker(monkeypatch, capsys, request)
    assert code == 0 and result['stage'] == 'returned', result.get('private_error_traceback')
    assert stubbed_hermes.calls == [
        ('owner-1', 'cli', USER['user']),
        ('contact-1', 'capture', '[Message from contact p-03 on chat]\nAny news?'),
        ('owner-2', 'cli', REACTION['owner_reaction']['text'])]
    assert stubbed_hermes.events == [('install', 0), ('tick', 7, None, True), ('tick', 7, None, True),
                                     ('advance', 3600), ('tick', 7, None, True)]
    effects = result['tool_evidence']
    assert effects['declared_turns'] == effects['turns_completed'] == 6
    assert [row.get('kind', row.get('event')) for row in effects['turns']] == [
        'user', 'tick', 'advance_clock', 'inbound', 'tick', 'owner_reaction']
    assert effects['turns'][2]['clock_offset_seconds'] == 3600
    body = effects['body']
    assert [row['tick'] for row in body['ticks']] == [1, 2, 3]
    assert [row['index'] for row in body['ticks']] == [1, 1, 4]
    assert body['clock_offset_seconds'] == 3600 and body['protocol'] == paired_body.PROTOCOL
    # Only the contact's reply is an outbound message; owner turns are not deliveries.
    assert body['outbox'] == [{'target': 'capture:p-03', 'text': 'Reply to Any news?',
                               'at': '2027-03-04T09:00:00', 'via': 'reply'}]
    assert body['ticks'][2]['outbox_before'] == 1
    assert result['output'] == 'Reply to ' + REACTION['owner_reaction']['text']
    config = json.loads((stubbed_hermes.root / 'home' / 'config.yaml').read_text())
    assert config['plugins']['enabled'] == ['capture'] and config['platforms'] == {'capture': {'enabled': True}}
    assert config['kanban'] == {'default_assignee': 'default'} and config['cron'] == {'wrap_response': False}
    assert (stubbed_hermes.root / 'home' / 'plugins' / 'capture' / 'plugin.yaml').exists()
    assert worker.os.environ['CAPTURE_OUTBOX'] == str(stubbed_hermes.root / 'outbox.json')
    # Every cron agent of the episode carries the frozen limits; no temperature keeps the runtime as resolved.
    assert stubbed_hermes.cron_agents == [{'runtime': {'base_url': 'http://model.invalid/v1'}, 'max_iterations': 4,
                                          'agent': {'model': 'cron-model', 'max_tokens': 64}}] * 3


def test_worker_pins_the_plan_temperature_on_cron_agents_too(stubbed_hermes, monkeypatch, capsys):
    request = {'binding': 'candidate', 'config': {'model': {'default': 'test'}}, 'temperature': 0.3,
        'inputs': {'arm': 'base_hermes', 'initial_files': {}, 'max_iterations': 8, 'max_output_tokens': 4096,
                   'settle_seconds': 0, 'episodes': [USER, {'tick': 1}]}}
    code, result = run_worker(monkeypatch, capsys, request)
    assert code == 0 and result['stage'] == 'returned', result.get('private_error_traceback')
    assert stubbed_hermes.cron_agents == [{
        'runtime': {'base_url': 'http://model.invalid/v1', 'request_overrides': {'extra_body': {'temperature': 0.3}}},
        'max_iterations': 8, 'agent': {'model': 'cron-model', 'max_tokens': 4096}}]


def test_worker_resumes_tick_numbering_and_clock_from_the_phase_contract(stubbed_hermes, monkeypatch, capsys):
    (stubbed_hermes.root / 'home').mkdir()
    (stubbed_hermes.root / 'workspace').mkdir()
    earlier = [{'target': 'capture:owner', 'text': 'from phase 0', 'at': '2027-03-04T09:00:00', 'via': 'platform'}]
    (stubbed_hermes.root / 'outbox.json').write_text(json.dumps(earlier))
    request = {'binding': 'candidate', 'config': {'model': {'default': 'test'}},
        'inputs': {'arm': 'base_hermes', 'initial_files': {}, 'max_iterations': 4, 'max_output_tokens': 64,
                   'settle_seconds': 0, 'episodes': [{'tick': 1}, {'advance_clock': 5}],
                   'workflow': {'restart_before': [3]}},
        '_workflow_phase': {'index': 1, 'start_turn': 3, 'workflow': {'restart_before': [3],
            'snapshot_after': [], 'read_failures': []}, 'body_before': {'clock_offset_seconds': 900, 'ticks_completed': 4}}}
    monkeypatch.setattr(sys, 'argv', ['paired_worker', '--workflow-phase'])
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(request)))
    assert worker.main() == 0
    output = capsys.readouterr().out
    result = json.loads(next(line[len(worker.RESULT_MARKER):] for line in output.splitlines()
                             if line.startswith(worker.RESULT_MARKER)))
    assert stubbed_hermes.events[0] == ('install', 900)
    # An events-only phase after a restart runs without any agent turn and keeps the earlier deliveries.
    assert result['tool_evidence']['body']['outbox'] == earlier
    assert [row['tick'] for row in result['tool_evidence']['body']['ticks']] == [5]
    assert result['tool_evidence']['turns'][0] == {'event': 'tick', 'completed': True, 'index': 3}
    assert result['tool_evidence']['turns_completed'] == 2 and result['output'] is None
    assert stubbed_hermes.calls == []


def test_worker_rejects_malformed_events_before_any_turn(stubbed_hermes, monkeypatch, capsys):
    request = {'binding': 'candidate', 'config': {'model': {'default': 'test'}},
        'inputs': {'arm': 'base_hermes', 'initial_files': {}, 'max_iterations': 4, 'max_output_tokens': 64,
                   'episodes': [USER, {'tick': 0}]}}
    with pytest.raises(ValueError):
        run_worker(monkeypatch, capsys, request)
    assert stubbed_hermes.calls == []


def test_worker_with_no_delivery_reports_an_empty_outbox_that_grades_as_silence(stubbed_hermes, monkeypatch, capsys):
    from protagine.qualification.paired_body_grading import assess_body
    request = {'binding': 'candidate', 'config': {'model': {'default': 'test'}},
        'inputs': {'arm': 'base_hermes', 'initial_files': {}, 'max_iterations': 4, 'max_output_tokens': 64,
                   'settle_seconds': 0, 'episodes': [USER, {'tick': 1}]}}
    code, result = run_worker(monkeypatch, capsys, request)
    assert code == 0 and result['stage'] == 'returned', result.get('private_error_traceback')
    body = result['tool_evidence']['body']
    assert body['outbox'] == [] and body['ticks'][0]['outbox_before'] == body['ticks'][0]['outbox_after'] == 0
    assert (stubbed_hermes.root / 'outbox.json').read_text() == '[]'
    assert assess_body({'body': body}, {'action': 'none', 'forbidden': ['p-01']}) == {
        'body:observed': True, 'body:forbidden': True, 'body:action': True}


def test_capture_writer_appends_one_json_array_with_the_hermes_clock(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, 'hermes_time', SimpleNamespace(now=lambda: datetime(2027, 3, 4, 9, 30)))
    capture = paired_body.capture_module()
    path = tmp_path / 'outbox.json'
    capture.record('capture:owner', 'first', path=path)
    capture.record('capture:p-02', 'second', via=capture.VIA_REPLY, path=path)
    assert paired_body.read_outbox(path) == [
        {'target': 'capture:owner', 'text': 'first', 'at': '2027-03-04T09:30:00', 'via': 'platform'},
        {'target': 'capture:p-02', 'text': 'second', 'at': '2027-03-04T09:30:00', 'via': 'reply'}]
    assert capture.parse_target(' p-07 ') == ('p-07', None) and capture.parse_target('') is None
    assert paired_body.read_outbox(tmp_path / 'missing.json') is None
    path.write_text('{}')
    assert paired_body.read_outbox(path) is None


def test_concurrent_capture_writers_lose_no_delivery(tmp_path, monkeypatch):
    import threading
    monkeypatch.setitem(sys.modules, 'hermes_time', SimpleNamespace(now=lambda: datetime(2027, 3, 4, 9, 30)))
    # Cron delivery and the harness's own reply recorder hold separate module instances of the plugin.
    writers = [paired_body.capture_module(), paired_body.capture_module()]
    path = tmp_path / 'outbox.json'
    count = 24
    start = threading.Barrier(count)

    def deliver(index):
        start.wait()
        writers[index % 2].record(f'capture:p-{index:02d}', f'delivery {index}', path=path)

    threads = [threading.Thread(target=deliver, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    rows = paired_body.read_outbox(path)
    assert sorted(row['text'] for row in rows) == sorted(f'delivery {index}' for index in range(count))
    assert not (tmp_path / 'outbox.json.tmp').exists()
