"""Disposable real gateway, canonical ASGI routes and controlled SDK HTTP only."""
import asyncio
import itertools
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
from unittest.mock import AsyncMock

installed, sidecar, native, dependencies = sys.argv[1:]
sys.path[:0] = [installed, sidecar, *([native] if native else [])]
if dependencies:
    sys.path.append(dependencies)

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pacomind.api.middleware import ApiKeyMiddleware
from pacomind.api.routers import executions, host
from pacomind.contacts.config import ContactsConfig
from pacomind.contacts.store import SQLiteContactStore
from pacomind.turns import get_turn_idempotency_ledger

home = Path(os.environ['HERMES_HOME'])
home.mkdir(mode=0o700)
artifact = home/'task-artifact.md'
artifact.write_text('The completed task artifact.\n')
beta_reply = f'TASK_BETA saved its result at {artifact}, with its own retained source.'
alpha_reply = 'TASK_ALPHA finished the original violet-note comparison after the foreground handoff.'
state = Path(os.environ['PACOMIND_STATE_DIR'])
state.mkdir(mode=0o700)
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
contacts = SQLiteContactStore(ContactsConfig(sqlite_path=str(state/'contacts.db')))


async def seed():
    await contacts.connect()
    person = await contacts.create(display_name='Native task fixture owner', trust_tier='inner_circle')
    await contacts.add_handle(person.contact_id, 'api_server', 'owner-api', verified=True)
    await contacts.add_handle(person.contact_id, 'whatsapp', '15550003@s.whatsapp.net', verified=True)
    return person.contact_id


owner = asyncio.run(seed())
host._contacts_store = contacts
os.environ['PACOMIND_OWNER_CONTACT_ID'] = owner
secret = 'isolated-native-task-fixture-key'
tool_form = os.environ.get('PACOMIND_TEST_TASK_TOOL_FORM', 'deferred')
keyring = home/'keys.json'
keyring.write_text(json.dumps({'version': 1, 'principals': [{
    'principal': 'native-task-fixture', 'status': 'active', 'viewer_person_id': owner,
    'person_ids': [owner], 'audiences': ['viewer'],
    'scopes': ['api:access', 'turns:resolve-sender', 'context:read', 'memory:read', 'turns:write'],
    'credentials': [{'id': 'fixture', 'secret': secret, 'status': 'active'}]}]}))
keyring.chmod(0o600)
(home/'config.yaml').write_text(json.dumps({
    'model': {'provider': 'custom', 'default': 'fixture-model', 'base_url': 'http://model.fixture/v1'},
    'providers': {'task-interactive': {
        'base_url': 'http://model.fixture/v1', 'api_key': 'fixture-task-model-key'}},
    'platforms': {'pacomind_task': {'extra': {'task_model_roles': {
        'coding': {'role': 'coding', 'provider': 'task-interactive', 'model': 'fixture-coding-model'}}}}},
    'auxiliary': {'title_generation': {'enabled': False}},
    'terminal': {'cwd': str(home)}, 'agent': {'max_turns': 4, 'api_max_retries': 0}, 'toolsets': ['pacomind'],
    'tools': {'tool_search': {'eager': ['pacomind_task'] if tool_form.endswith('direct') else []}},
    'display': {'platforms': {'pacomind_task': {'streaming': False, 'tool_progress': 'off'}}},
    'memory': {'provider': 'pacomind-memory', 'config': {
        'contact_id': owner, 'url': 'http://fixture', 'api_key': secret}},
    'plugins': {'enabled': ['pacomind'], 'pacomind': {
        'owner_contact_id': owner, 'url': 'http://fixture', 'api_key': secret,
        'turn_outbox_path': str(home/'outbox.db'), 'execution_registry_enabled': True,
        'native_tasks': {'enabled': True, 'state_path': str(home/'native-tasks.db')}}}}))

app = FastAPI()
app.add_middleware(ApiKeyMiddleware, keyring_path=str(keyring))
for router in (host.router, host.v2_router, executions.router):
    app.include_router(router)
api = TestClient(app)
api.__enter__()
ledger = get_turn_idempotency_ledger(state)
wire, generation, foreground_calls, tool_results = [], {'alpha': [], 'beta': [], 'failure': []}, {}, {}
context_reads = []
held = {name: threading.Event() for name in (
    'alpha', 'beta', 'alpha_next', 'failure_next', 'failure_terminal')}
release = {name: threading.Event() for name in held}
task_ids = {}
expected_failure_turn = None
source_parents, status_views, tool_ids = {}, {}, itertools.count()
tearing_down = False
steer_delivery = os.environ.get('PACOMIND_TEST_STEER_DELIVERY', 'tool_batch')
submission = os.environ.get('PACOMIND_TEST_TASK_SUBMISSION', 'submit')
existing_handoff = submission == 'existing_handoff'
mixed_handoff = tool_form.startswith('mixed_')
accepted_before_handoff, admissions, dispatches, controllers = {}, [], [], {}
update_text = 'Keep the alpha comparison scoped to the violet notes and label the result ORANGE-472.'


def message_response(body, message, finish='stop'):
    if body.get('stream'):
        delta = dict(message)
        if delta.get('tool_calls'):
            delta['tool_calls'] = [{**value, 'index': index}
                for index, value in enumerate(delta['tool_calls'])]
        chunk = {'id': 'fixture', 'object': 'chat.completion.chunk', 'created': 1,
            'model': body['model'], 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]}
        end = {**chunk, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}]}
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=(
            'data: ' + json.dumps(chunk) + '\n\ndata: ' + json.dumps(end) + '\n\ndata: [DONE]\n\n').encode())
    return httpx.Response(200, json={'id': 'fixture', 'object': 'chat.completion', 'created': 1,
        'model': body['model'], 'choices': [{'index': 0, 'message': message, 'finish_reason': finish}],
        'usage': {'prompt_tokens': 20, 'completion_tokens': 10, 'total_tokens': 30}})


def answer(body, text):
    return message_response(body, {'role': 'assistant', 'content': text})


def tool(body, name, arguments):
    if name not in {value.get('function', {}).get('name') for value in body.get('tools', [])}:
        name, arguments = 'tool_call', {'name': name, 'arguments': arguments}
    return message_response(body, {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': 'fixture-tool-' + str(next(tool_ids)), 'type': 'function', 'function': {'name': name,
        'arguments': json.dumps(arguments)}}]}, 'tool_calls')


def existing_task_handoff(body, identity):
    calls = [{'name': 'pacomind_task', 'arguments': {'operation': 'handoff', 'task_id': identity}}]
    if mixed_handoff:
        calls.append({'name': 'pacomind_task', 'arguments': {'operation': 'status', 'task_id': identity}})
    names = {value.get('function', {}).get('name') for value in body['tools']}
    direct = tool_form.endswith('direct')
    assert ('pacomind_task' in names) == direct, (tool_form, names)
    if not direct:
        calls = [{'name': 'tool_call', 'arguments': {'calls': calls}}]
    return message_response(body, {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': 'fixture-tool-' + str(next(tool_ids)), 'type': 'function',
         'function': {'name': call['name'], 'arguments': json.dumps(call['arguments'])}}
        for call in calls]}, 'tool_calls')


def respond(request):
    if request.url.host == 'fixture':
        response = api.request(request.method, request.url.path, params=request.url.params,
            headers=dict(request.headers), content=request.content)
        wire.append({'path': request.url.path, 'status': response.status_code})
        if request.url.path == '/v1/host/context/assemble':
            context_reads.append({'context': json.loads(request.content)['context'],
                'status': response.status_code})
        return httpx.Response(response.status_code, content=response.content, headers=response.headers)
    assert request.url.host == 'model.fixture', str(request.url)
    if request.method == 'GET' and request.url.path == '/v1/models':
        return httpx.Response(200, json={'data': [{'id': 'fixture-model', 'context_length': 32768}]})
    if request.url.path == '/api/show':
        return httpx.Response(404, json={'error': 'Not Ollama'})
    assert request.url.path == '/v1/chat/completions', str(request.url)
    body = json.loads(request.content)
    if tearing_down:
        # Release controlled provider work during cleanup without replacing an
        # earlier test failure with an assertion from a cancelled request.
        return answer(body, 'Fixture cleanup after qualification ended.')
    from pacomind_hermes.native_task_platform import ACTIVE
    from pacomind_hermes.input_provenance import current
    active = ACTIVE.get()
    if active is not None:
        if existing_handoff:
            assert 'pacomind_task' not in json.dumps(body['tools']), (
                'A task worker was offered foreground-only task control in its schema or catalog')
            worker_text = json.dumps(body['messages'])
            assert 'Actual acceptance completes this foreground request' not in worker_text
            assert 'When the user asks for background work and a prompt return' not in worker_text
        row = adapter.handoffs.get(active['id'])
        if 'TASK_FAILURE' in row['request']:
            generation['failure'].append(body)
            step = len(generation['failure'])
            assert step <= 3, 'Exhausted task was dispatched again'
            assert active['native']['session_id'] == row['native_session_id']
            if step == 1:
                return tool(body, 'write_file', {'path': str(home/'retained-effect.txt'),
                                               'content': 'Completed before the route failed.'})
            assert (home/'retained-effect.txt').read_text() == 'Completed before the route failed.'
            if step == 2:
                raise httpx.ReadTimeout('Controlled unavailable model route', request=request)
            assert row['native_turn_id'] != expected_failure_turn and row['terminal'] is None
            assert 'Completed before the route failed.' in json.dumps(body['messages'])
            assert 'The owner requested continuation of this same task' in json.dumps(body['messages'])
            held['failure_next'].set()
            assert release['failure_next'].wait(35), 'Resumed task was never released'
            return answer(body, 'TASK_FAILURE continued from its retained tool result.')
        name = 'alpha' if 'TASK_ALPHA' in row['request'] else 'beta'
        assert 'TASK_' + name.upper() in row['request'], row
        assert body['model'] == ('fixture-coding-model' if name == 'alpha' else 'fixture-model')
        generation[name].append(body)
        step = len(generation[name])
        assert step <= (2 if name == 'alpha' else 1), (name, step)
        assert row['native_session_id'] and row['native_task_id'] and row['native_turn_id'], row
        assert active['native']['session_id'] == row['native_session_id']
        supplied = current()
        assert supplied is not None and supplied.session_id == row['native_session_id']
        assert supplied.memory_contact(row['native_session_id']) == owner, {
            'task': name, 'step': step, 'native': active.get('native'),
            'closed': supplied._closed, 'blocked': supplied._blocked,
            'failure': supplied.failure, 'bound': list(supplied._bound),
            'memory_sessions': list(supplied._memory_sessions), 'messages': body['messages']}
        if step == 1:
            assert any(read['status'] == 200 and read['context'] == {
                'session_id': row['native_session_id'], 'contact_id': owner}
                for read in context_reads), {'task': name, 'context_reads': context_reads}
            assert supplied.parents()[0] == row['source']['input_refs']
            source_parents[name] = row['source']['input_refs']
            held[name].set()
            assert release[name].wait(35), 'Held ' + name + ' request was never released'
            if name == 'alpha':
                if existing_handoff:
                    return answer(body, alpha_reply)
                if steer_delivery == 'next_turn':
                    return answer(body, 'Initial alpha answer completed before the queued correction.')
                return tool(body, 'pacomind_memory_read_source', row['source']['source_refs'][0])
            return answer(body, beta_reply)
        text = json.dumps(body['messages'])
        assert update_text in text and 'pacomind-task-update-v1' in text, text
        updates = adapter.handoffs.updates(row['id'])
        from agent import relay_runtime
        turn = relay_runtime.current_turn()
        assert len(updates) == 1 and updates[0]['observations'].get('native_request_visible'), {
            'updates': updates, 'managed_callback_depth': relay_runtime._MANAGED_CALLBACK_DEPTH.get(),
            'relay_active': relay_runtime.active_turn(row['native_session_id']) is turn,
            'relay_enabled': getattr(turn, 'relay_enabled', None),
            'logical_llm_calls': list(getattr(turn, 'logical_llm_calls', {})),
            'native_turn': active.get('native')}
        assert turn.logical_llm_calls, 'The independent task bypassed native Relay execution'
        assert updates[0]['source']['input_refs'][0] in supplied.parents()[0]
        if steer_delivery == 'next_turn':
            assert len(supplied._bound) == 2, 'Late /steer did not enter a new native turn'
        assert updates[0]['source']['input_refs'][0] in row['dependencies']['input_refs']
        held['alpha_next'].set()
        assert release['alpha_next'].wait(35), 'The stopped alpha provider request was never released'
        return answer(body, 'LATE_ALPHA_RESULT_MUST_NOT_BE_RETAINED')

    assert current() is None, 'An ordinary conversation inherited a task source context'
    assert body['model'] == 'fixture-model', 'Task model selection escaped into foreground work'
    latest = next(row.get('content') for row in reversed(body['messages']) if row.get('role') == 'user')
    latest = latest if isinstance(latest, str) else json.dumps(latest)
    tag = next((name for name in ('SUBMIT_ALPHA', 'SUBMIT_BETA', 'SUBMIT_FAILURE', 'RESUME_FAILURE',
                                 'RESUME_FAILURE_DUPLICATE', 'ORDINARY', 'STEER_ALPHA', 'STOP_ALPHA',
                                 'STATUS_QUEUED', 'STATUS_VISIBLE', 'STATUS_ERASED', 'HANDOFF_REJECTED',
                                 'HANDOFF_PRIOR_TURN')
                if latest.startswith('FG_' + name + ':')), None)
    assert tag is not None, latest
    foreground_calls[tag] = foreground_calls.get(tag, 0) + 1
    step = foreground_calls[tag]
    if tag == 'HANDOFF_PRIOR_TURN':
        assert step <= 2
        if step == 1:
            return tool(body, 'pacomind_task', {'operation': 'handoff', 'task_id': task_ids['alpha']})
        result = json.loads(next(row['content'] for row in reversed(body['messages']) if row['role'] == 'tool'))
        assert result.get('error') and not result.get('accepted'), result
        return answer(body, 'A different foreground turn cannot finish through the prior task admission.')
    if tag == 'SUBMIT_ALPHA' and existing_handoff and step > 1:
        results = [json.loads(row['content']) for row in body['messages'] if row['role'] == 'tool']
        if step == 2:
            accepted = results[-1]
            assert accepted.get('accepted') is True, accepted
            accepted_before_handoff.update(adapter.handoffs.get(accepted['task_id']))
            return existing_task_handoff(body, accepted['task_id'])
        assert mixed_handoff and step == 3, (
            'An existing-task handoff must finish without another foreground provider response', results)
        if tool_form == 'mixed_deferred':
            assert len(results) == 2 and results[-1].get('error') == (
                'Local tools require one entry per tool_call; mixed and multi-local batches are not supported.'), results
        else:
            assert any(value.get('accepted') and value.get('task_id') == accepted_before_handoff['id']
                       for value in results[1:]), results
        return answer(body, 'The mixed tool batch completed without terminating the foreground turn.')
    if tag == 'HANDOFF_REJECTED':
        assert step <= 2
        if step == 1:
            return tool(body, 'pacomind_task', {'operation': 'handoff', 'request': ' '})
        result = json.loads(next(row['content'] for row in reversed(body['messages']) if row.get('role') == 'tool'))
        assert result['error'] and result['outcome'] == 'unconfirmed' and 'task_id' not in result, result
        return answer(body, 'The invalid handoff was rejected; the conversation can continue.')
    if tag == 'SUBMIT_ALPHA' and submission == 'handoff':
        assert step == 1, 'Successful terminal handoff requested another foreground provider response'
    if tag == 'ORDINARY':
        assert step == 1
        return answer(body, 'The ordinary conversation completed while both tasks stayed active.')
    if tag.startswith('STATUS_'):
        assert step <= (3 if tag == 'STATUS_QUEUED' else 2)
        if step == 1:
            return tool(body, 'pacomind_task', {'operation': 'status', 'task_id': task_ids['alpha']})
        results = [row['content'] for row in body['messages'] if row.get('role') == 'tool']
        result = json.loads(results[-1])
        if step == 2:
            status_views[tag] = result
            assert 'error' not in result, result
            if tag == 'STATUS_ERASED':
                assert not result.get('source_refs') and not result.get('updates'), result
            else:
                from datetime import datetime, timezone
                retained = adapter.handoffs.get(task_ids['alpha'])
                original = retained['source']['source_refs']
                assert result['accepted_at'] == retained['created']
                assert result['accepted_at_utc'] == datetime.fromtimestamp(retained['created'], timezone.utc).isoformat()
                assert 0 <= result['age_since_acceptance_seconds'] <= time.time()-retained['created']+.001
                update = adapter.handoffs.updates(task_ids['alpha'])[0]
                assert result['input_source_refs'] == original, result
                assert result['updates_complete'] and len(result['updates']) == 1, result
                observed = result['updates'][0]
                assert observed['accepted_at'] == update['created']
                assert observed['accepted_at_utc'] == datetime.fromtimestamp(update['created'], timezone.utc).isoformat()
                ack = observed['observations']['native_control_acknowledged']
                assert ack['observed_at'] == update['observations']['native_control_acknowledged']['observed_at']
                assert ack['observed_at_utc'] == datetime.fromtimestamp(ack['observed_at'], timezone.utc).isoformat()
                assert observed['source_refs'] == update['source']['source_refs']
                assert observed['accepted'] and observed['native_control_acknowledged']
                assert observed['provider_delivery'] == observed['behavior_applied'] == 'unobserved'
                assert observed['middleware_visible'] == observed['native_request_visible'] == (tag == 'STATUS_VISIBLE')
                assert 'instruction' not in observed
                if tag == 'STATUS_QUEUED':
                    return tool(body, 'pacomind_memory_read_source', observed['source_refs'][0])
        else:
            assert result['pacomind_source_read_v1'] and update_text in result['content'], result
        return answer(body, 'FG_' + tag + '_ACK')
    assert step <= 2, (tag, body)
    if step == 1:
        if tag.startswith('RESUME_'):
            return tool(body, 'pacomind_task', {'operation': 'resume',
                'task_id': task_ids['failure'], 'expected_turn_id': expected_failure_turn})
        if tag.startswith('SUBMIT_'):
            name = tag.removeprefix('SUBMIT_')
            return tool(body, 'pacomind_task', {'operation': submission if name == 'ALPHA' and not existing_handoff else 'submit',
                'request': 'TASK_' + name + ': Compare my violet calibration notes and retain the result.',
                **({'model_role': 'coding'} if name == 'ALPHA' else {})})
        return tool(body, 'pacomind_task', {'operation': 'steer' if tag == 'STEER_ALPHA' else 'stop',
            'task_id': task_ids['alpha'], **({'request': update_text} if tag == 'STEER_ALPHA' else {})})
    results = [row['content'] for row in body['messages'] if row.get('role') == 'tool']
    tool_results[tag] = results
    assert results and '"error"' not in results[-1], (tag, results)
    if tag.startswith('RESUME_'):
        assert isinstance(json.loads(results[-1])['native_observation']['resume_requested'], bool), results
    elif tag.startswith('SUBMIT_') or tag == 'STEER_ALPHA':
        assert '"accepted": true' in results[-1], (tag, results)
    else:
        assert '"stop_requested": true' in results[-1], results
    return answer(body, 'FG_' + tag + '_ACK')


def controlled(self, request):
    try:
        return respond(request)
    except httpx.ReadTimeout:
        raise  # Exercise actual SDK classification and native retry exhaustion.
    except BaseException:
        import traceback
        traceback.print_exc()
        print(json.dumps({'wire': wire, 'foreground': foreground_calls}), file=sys.stderr)
        sys.stderr.flush()
        os._exit(70)


httpx.HTTPTransport.handle_request = controlled


def no_network(*args, **kwargs):
    raise AssertionError('Native task qualification cannot access the network')


socket.socket.connect = no_network
socket.create_connection = no_network
from hermes_cli.plugins import get_plugin_manager
from pacomind_hermes.task_handoffs import TaskHandoffs
original_observe_terminal = TaskHandoffs.observe_terminal


def held_failure_terminal(self, identity, native, *, basis='native_on_session_end'):
    if (basis == 'native_on_native_turn_settled'
            and self.get(identity)['request'].startswith('TASK_FAILURE:')):
        # A provider notice can precede native settlement. Make that real
        # interleaving deterministic without replacing the terminal observation.
        held['failure_terminal'].set()
        assert release['failure_terminal'].wait(35), 'Failure settlement was never released'
    return original_observe_terminal(self, identity, native, basis=basis)


TaskHandoffs.observe_terminal = held_failure_terminal
if existing_handoff:
    from pacomind_hermes.task_controller import NativeTasks
    original_admit, original_call = TaskHandoffs.admit, NativeTasks._call
    def counted_admit(self, **kwargs):
        admissions.append(kwargs.copy())
        return original_admit(self, **kwargs)
    def counted_call(self, action, identity, **kwargs):
        dispatches.append((action, identity))
        controllers[identity] = self
        return original_call(self, action, identity, **kwargs)
    TaskHandoffs.admit, NativeTasks._call = counted_admit, counted_call
manager = get_plugin_manager()
manager.discover_and_load()
assert manager._plugins['pacomind'].enabled, manager._plugins['pacomind'].error
from pacomind_hermes.task_controller import FinishTurn, TOOL_SCHEMA
assert ('handoff' in TOOL_SCHEMA['parameters']['properties']['operation']['enum']) == (FinishTurn is not None)
if submission == 'handoff' or existing_handoff:
    assert FinishTurn is not None, 'Terminal handoff requires the native post_tool_batch contract'
from gateway.config import GatewayConfig, PlatformConfig, Platform
from gateway.platform_registry import platform_registry
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.platforms.event import MessageEvent, MessageType

# Disable only the unrelated optional scanner bootstrap that installs a binary.
import tools.tirith_security
tools.tirith_security.ensure_installed = lambda **kwargs: False
config = GatewayConfig(sessions_dir=home/'sessions', loop_watchdog=False)
platform_config = PlatformConfig(enabled=True, typing_indicator=False, gateway_restart_notification=False)
config.platforms = {Platform('pacomind_task'): platform_config}
runner = GatewayRunner(config)
adapter = platform_registry.create_adapter('pacomind_task', platform_config)
assert adapter is not None
adapter.send_document = AsyncMock(wraps=adapter.send_document)
runner.adapters[adapter.platform] = adapter
runner.delivery_router.adapters = runner.adapters
runner._wire_adapter_handlers(adapter)


def event(tag, *, whatsapp=False):
    return MessageEvent(text='FG_' + tag + ': ' + (update_text if tag == 'STEER_ALPHA' else
        'Perform the requested local calibration comparison.' if tag.startswith('SUBMIT_') else
        'Stop the alpha task.' if tag == 'STOP_ALPHA' else 'Answer an unrelated short question.'),
        source=SessionSource(platform=Platform.WHATSAPP if whatsapp else Platform.API_SERVER,
            chat_id='15550003@s.whatsapp.net' if whatsapp else 'thread-' + tag.lower(),
            user_id='15550003@s.whatsapp.net' if whatsapp else 'owner-api'),
        message_id='message-' + tag.lower(), message_type=MessageType.TEXT, allow_gateway_control=False)


async def wait_for(predicate, label, timeout=15):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        await asyncio.sleep(.025)
    assert predicate(), {'waiting_for': label, 'wire': wire, 'foreground': foreground_calls,
        'tasks': adapter.handoffs.recent(), 'tool_results': tool_results}


async def exercise():
    global tearing_down, expected_failure_turn
    runner._running = True
    runner._gateway_loop = asyncio.get_running_loop()
    try:
        assert await adapter.connect()
        assert adapter.verify_http_event_request('Bearer untrusted-caller')[0] is False
        if submission == 'handoff':
            rejected = await asyncio.wait_for(runner._handle_message(event('HANDOFF_REJECTED')), 12)
            assert rejected == 'The invalid handoff was rejected; the conversation can continue.', rejected
            assert foreground_calls['HANDOFF_REJECTED'] == 2 and adapter.handoffs.count() == 0
        # Actual foreground native turns call the registered tool. Neither the
        # test nor the model can supply contact, principal, origin or input refs.
        alpha_event = event('SUBMIT_ALPHA')
        answers = await asyncio.wait_for(asyncio.gather(
            runner._handle_message(alpha_event),
            runner._handle_message(event('SUBMIT_BETA'))), 20)
        assert answers[1] == 'FG_SUBMIT_BETA_ACK' and foreground_calls['SUBMIT_BETA'] == 2, answers
        if submission == 'submit':
            assert answers[0] == 'FG_SUBMIT_ALPHA_ACK' and foreground_calls['SUBMIT_ALPHA'] == 2, answers
        await wait_for(lambda: held['alpha'].is_set() and held['beta'].is_set(), 'both native roots at SDK')
        rows = adapter.handoffs.recent()
        assert len(rows) == 2, rows
        for row in rows:
            name = 'alpha' if 'TASK_ALPHA' in row['request'] else 'beta'
            task_ids[name] = row['id']
            assert row['source']['origin']['platform'] == 'api_server'
            assert row['source']['source_session_id'] != row['native_session_id']
            assert row['response'] is None and row['stop'] is None
        if submission == 'handoff' or existing_handoff:
            assert foreground_calls['SUBMIT_ALPHA'] == (3 if mixed_handoff else 2 if existing_handoff else 1)
            if mixed_handoff:
                assert answers[0] == 'The mixed tool batch completed without terminating the foreground turn.', answers
            else:
                guidance = ('Inspect this task ID for its current status.' if existing_handoff else
                            'You can inspect or steer it using this task ID.')
                assert answers[0] == (f"Accepted task `{task_ids['alpha']}`. "
                    + guidance), answers
            entry = runner.session_store.get_or_create_session(alpha_event.source, touch_activity=False)
            messages = runner._session_db._db.get_messages(entry.session_id)
            results = [json.loads(row['content']) for row in messages if row['role'] == 'tool']
            expected_results = 3 if tool_form == 'mixed_direct' else 2 if existing_handoff else 1
            assert len(results) == expected_results, results
            assert results[0]['accepted'] is True, results
            assert results[0]['task_id'] == task_ids['alpha'] and results[0]['delivery'] == 'unobserved'
            if existing_handoff and tool_form != 'mixed_deferred':
                accepted = next(value for value in results[1:] if value.get('accepted'))
                assert accepted['existing_task'] is True and accepted['task_id'] == results[0]['task_id'], accepted
                assert accepted['model_role'] == results[0]['model_role'], accepted
                assert accepted['status'] not in {'queued', 'running'}, accepted
            receipts = [row for row in messages if row['role'] == 'assistant' and row['content']]
            assert len(receipts) == 1 and receipts[0]['content'] == answers[0], receipts
            receipt = receipts[0]
            if mixed_handoff:
                assert receipt['display_kind'] != 'runtime_handoff', receipt
            else:
                assert receipt['display_kind'] == 'runtime_handoff'
                provenance = receipt['display_metadata']
                assert provenance['response_origin'] == 'runtime' and provenance['type'] == 'tool_handoff'
                assert provenance['session_id'] == entry.session_id and provenance['api_request_id']
                tool_row = [row for row in messages if row['role'] == 'tool'][-1]
                assert provenance['tool_call_id'] == tool_row['tool_call_id']
                assert provenance['tool_name'] == ('pacomind_task' if tool_form.endswith('direct') else 'tool_call')
                assert tool_row['tool_name'] == 'pacomind_task'
            # Finishing this foreground turn cannot interrupt its independently owned task.
            assert held['alpha'].is_set() and not release['alpha'].is_set()
        if existing_handoff:
            from types import SimpleNamespace
            retained = adapter.handoffs.get(task_ids['alpha'])
            for key in ('id', 'created', 'request', 'source', 'model_role'):
                assert retained[key] == accepted_before_handoff[key], (key, retained, accepted_before_handoff)
            assert len(admissions) == 2 and sum('TASK_ALPHA' in item['request'] for item in admissions) == 1
            assert dispatches.count(('submit', task_ids['alpha'])) == 1, dispatches
            assert len(generation['alpha']) == 1, generation['alpha']
            controller = controllers[task_ids['alpha']]
            scope = dict(valid_participant=True, contact_id=owner, authority_lane='owner',
                resolution_status='resolved', **retained['source']['origin'])
            arguments = {'operation': 'handoff', 'task_id': task_ids['alpha']}
            exact_scope = SimpleNamespace(**scope)
            for extra in ({'request': retained['request']}, {'model_role': 'coding'},
                          {'expected_turn_id': retained['native_turn_id']}):
                rejected = json.loads(await asyncio.to_thread(controller.handle, {**arguments, **extra}, exact_scope))
                assert rejected.get('error') and not rejected.get('accepted'), rejected
            for changed in ({'turn_id': 'another-turn'}, {'session_id': 'another-session'},
                            {'platform': 'whatsapp'}, {'contact_id': 'another-owner'}):
                other = SimpleNamespace(**{**scope, **changed})
                rejected = json.loads(await asyncio.to_thread(controller.handle, arguments, other))
                assert rejected.get('error') and not rejected.get('accepted'), (changed, rejected)
                if not mixed_handoff:
                    assert controller.finish_handoff(scope=other, tool_name='pacomind_task',
                        tool_call_id='foreign-handoff', tool_arguments=json.dumps(arguments),
                        tool_result=json.dumps(results[1])) is None, changed
            denied = await asyncio.wait_for(runner._handle_message(event('HANDOFF_PRIOR_TURN')), 12)
            assert denied == 'A different foreground turn cannot finish through the prior task admission.', denied
            assert adapter.handoffs.count() == 2 and len(admissions) == 2
            assert dispatches.count(('submit', task_ids['alpha'])) == 1, dispatches
            release['alpha'].set()
            await wait_for(lambda: bool(adapter.handoffs.get(task_ids['alpha'])['response']),
                           'original task result after foreground handoff')
            completed = adapter.handoffs.get(task_ids['alpha'])
            assert completed['response']['text'] == alpha_reply and completed['terminal']['completed']
            assert completed['native_session_id'] == retained['native_session_id']
            assert completed['native_turn_id'] == retained['native_turn_id']
            assert completed['dependencies']['input_refs'] == retained['source']['input_refs']
            for key in ('request', 'source', 'model_role'):
                assert completed[key] == retained[key], key
            rejected = json.loads(await asyncio.to_thread(controller.handle, arguments, exact_scope))
            assert rejected.get('error') and not rejected.get('accepted'), rejected
            beta = adapter.handoffs.get(task_ids['beta'])
            beta_scope = SimpleNamespace(**{**scope, **beta['source']['origin']})
            stopped = json.loads(await asyncio.to_thread(controller.handle,
                {'operation': 'stop', 'task_id': beta['id']}, beta_scope))
            assert stopped['stop_requested'], stopped
            rejected = json.loads(await asyncio.to_thread(controller.handle,
                {'operation': 'handoff', 'task_id': beta['id']}, beta_scope))
            assert rejected.get('error') and not rejected.get('accepted'), rejected
            ledger.erase_sources(contact_id=owner,
                turn_ids=[retained['source']['input_refs'][0]['source_id']])
            rejected = json.loads(await asyncio.to_thread(controller.handle, arguments, exact_scope))
            assert rejected.get('error') == (
                'Task sources are unavailable or changed; inspect source state before continuing'), rejected
            assert not rejected.get('accepted'), rejected
            assert len(admissions) == 2 and dispatches.count(('submit', task_ids['alpha'])) == 1, dispatches
            print(json.dumps({'cross_channel_native_tasks': True, 'existing_task_handoff': True,
                'tool_form': tool_form, 'same_admission_and_source': True,
                'native_finish_preserves_background_task': not mixed_handoff,
                'mixed_batch_does_not_finish': mixed_handoff, 'unrelated_turn_rejected': True,
                'original_task_result_retained': True, 'ended_stopped_erased_tasks_not_reaccepted': True,
                'worker_task_control_and_foreground_guidance_absent': True}))
            return
        assert len({tuple(row[key] for key in ('native_session_id', 'native_task_id', 'native_turn_id'))
                    for row in rows}) == 2
        assert source_parents['alpha'] != source_parents['beta']
        ordinary = await asyncio.wait_for(runner._handle_message(event('ORDINARY')), 12)
        assert ordinary == 'The ordinary conversation completed while both tasks stayed active.', ordinary
        assert not release['alpha'].is_set() and not release['beta'].is_set()
        steered = await asyncio.wait_for(runner._handle_message(event('STEER_ALPHA', whatsapp=True)), 12)
        assert steered == 'FG_STEER_ALPHA_ACK', steered
        updates = adapter.handoffs.updates(task_ids['alpha'])
        assert len(updates) == 1 and updates[0]['source']['origin']['platform'] == 'whatsapp'
        assert updates[0]['source']['origin']['sender_id'] == '15550003@s.whatsapp.net'
        assert not adapter.handoffs.updates(task_ids['beta'])
        assert not updates[0]['observations'].get('native_request_visible')
        inspected = await asyncio.wait_for(runner._handle_message(event('STATUS_QUEUED')), 12)
        assert inspected == 'FG_STATUS_QUEUED_ACK', inspected
        assert not release['alpha'].is_set(), 'Inspection released or readmitted the held task'
        release['alpha'].set()
        await wait_for(held['alpha_next'].is_set, 'steered next native SDK request')
        inspected = await asyncio.wait_for(runner._handle_message(event('STATUS_VISIBLE')), 12)
        assert inspected == 'FG_STATUS_VISIBLE_ACK', inspected
        stopped = await asyncio.wait_for(runner._handle_message(event('STOP_ALPHA', whatsapp=True)), 12)
        assert stopped == 'FG_STOP_ALPHA_ACK', stopped
        assert adapter.handoffs.get(task_ids['alpha'])['stop']['native_control_acknowledged']
        assert not adapter.handoffs.get(task_ids['beta'])['stop']
        release['alpha_next'].set()
        await wait_for(lambda: bool(adapter.handoffs.get(task_ids['alpha'])['terminal']), 'alpha terminal receipt')
        alpha = adapter.handoffs.get(task_ids['alpha'])
        assert alpha['terminal']['interrupted'] and alpha['response'] is None, alpha
        assert adapter.handoffs.stop_view(alpha)['status'] == 'cancelled'
        assert not adapter.handoffs.get(task_ids['beta'])['response']
        release['beta'].set()
        await wait_for(lambda: bool(adapter.handoffs.get(task_ids['beta'])['response']), 'unrelated task retained result')
        beta = adapter.handoffs.get(task_ids['beta'])
        # Actual native response delivery must retain an existing bare artifact
        # locator: this adapter stores text and cannot upload inferred documents.
        assert beta['response']['text'] == beta_reply, beta['response']
        assert beta['response']['source_dependencies']['input_refs'] == source_parents['beta']
        retained = beta['response']['source_dependencies']
        with ledger._connect() as db:
            canonical = db.execute('SELECT contact_id, messages_json FROM turn_sources WHERE turn_id = ?',
                (retained['turn_id'],)).fetchone()
        assert canonical is not None and canonical['contact_id'] == owner, canonical
        assert beta['response']['text'] in canonical['messages_json'], canonical['messages_json']
        people = await contacts.list()
        assert {person.contact_id for person in people} == {owner}, [
            {'contact_id': person.contact_id, 'name': person.display_name,
             'handles': [(handle.gateway, handle.address)
                for handle in await contacts.get_handles(person.contact_id)]}
            for person in people]
        assert beta['stop'] is None
        assert len(generation['alpha']) == 2 and len(generation['beta']) == 1
        await wait_for(lambda: not adapter._session_tasks, 'native task delivery cleanup')
        failed_ack = await asyncio.wait_for(runner._handle_message(event('SUBMIT_FAILURE')), 12)
        assert failed_ack == 'FG_SUBMIT_FAILURE_ACK', failed_ack
        await wait_for(lambda: any('TASK_FAILURE' in row['request'] and row['notice_json']
            for row in adapter.handoffs.recent()), 'failed task notice')
        failed = next(row for row in adapter.handoffs.recent() if 'TASK_FAILURE' in row['request'])
        assert failed['terminal'] is None and adapter._session_tasks, failed
        release['failure_terminal'].set()
        await wait_for(lambda: not adapter._session_tasks, 'failed task delivery cleanup')
        assert held['failure_terminal'].is_set(), 'The native failure settlement hook was not reached'
        # The earlier notice row is a snapshot, not a live task view.
        failed = adapter.handoffs.get(failed['id'])
        assert failed['terminal'] and failed['terminal']['failed'], failed
        assert failed['terminal']['failure_reason'] == 'timeout', failed
        assert failed['terminal']['failure_retryable'] is True, failed
        assert failed['terminal']['basis'] == 'native_on_native_turn_settled', failed
        assert failed['response'] is None and failed['dependencies'] is None, failed
        notice = json.loads(failed['notice_json'])['text']
        assert '/reset' not in notice and 'source receipt' not in notice
        status = await adapter.status(failed['id'])
        assert status['status'] == 'failed' and status['failure'] == failed['terminal'], status
        assert status['input_source_refs'] == failed['source']['source_refs']
        # Reopen the retained database as a restarted consumer, without a live
        # native session store. A failed result remains distinct from an answer.
        from pacomind_hermes.task_controller import NativeTasks
        from pacomind_hermes.task_handoffs import TaskHandoffs
        reopened = TaskHandoffs(adapter.handoffs._database, adapter.handoffs._resolve_source,
                               adapter.handoffs._resolve_owner)
        saved = reopened.get(failed['id'])
        assert saved['source'] == failed['source'] and saved['terminal'] == failed['terminal']
        assert NativeTasks._metadata(saved)['status'] == 'failed'
        assert reopened.pending() == [] and len(generation['failure']) == 2
        # Targeted owner control resumes exactly this task/session. The first
        # turn already wrote a file; its durable tool result must remain in
        # history and that operation must never be dispatched again.
        task_ids['failure'] = failed['id']
        expected_failure_turn = failed['native_turn_id']
        original_effect = (home/'retained-effect.txt').stat().st_mtime_ns
        stale = await adapter.dispatch_native_event({'handoff_id': failed['id'], 'action': 'resume',
                                                    'expected_turn_id': 'wrong-generation'})
        assert not stale['resume_requested'] and len(generation['failure']) == 2
        resumed = await asyncio.wait_for(asyncio.gather(
            runner._handle_message(event('RESUME_FAILURE', whatsapp=True)),
            runner._handle_message(event('RESUME_FAILURE_DUPLICATE'))), 15)
        assert resumed == ['FG_RESUME_FAILURE_ACK', 'FG_RESUME_FAILURE_DUPLICATE_ACK'], resumed
        assert sum(json.loads(tool_results[tag][-1])['native_observation']['resume_requested']
                   for tag in ('RESUME_FAILURE', 'RESUME_FAILURE_DUPLICATE')) == 1
        await wait_for(held['failure_next'].is_set, 'same task resumed at SDK')
        duplicate = await adapter.dispatch_native_event({'handoff_id': failed['id'], 'action': 'resume',
                                                        'expected_turn_id': expected_failure_turn})
        assert not duplicate['resume_requested'] and duplicate['replayed'], duplicate
        assert len(generation['failure']) == 3
        continued = adapter.handoffs.get(failed['id'])
        assert continued['source'] == failed['source']
        assert continued['native_session_id'] == failed['native_session_id']
        assert continued['native_task_id'] == failed['native_task_id']
        release['failure_next'].set()
        await wait_for(lambda: bool(adapter.handoffs.get(failed['id'])['response']), 'resumed retained result')
        assert (await adapter.status(failed['id']))['status'] == 'done'
        assert (home/'retained-effect.txt').stat().st_mtime_ns == original_effect
        for terminal_id in (failed['id'], alpha['id']):
            terminal = adapter.handoffs.get(terminal_id)
            declined = await adapter.dispatch_native_event({'handoff_id': terminal_id, 'action': 'resume',
                'expected_turn_id': terminal['native_turn_id']})
            assert not declined['resume_requested'] and declined['reason'] == 'task_stopped_or_completed'
        assert len(generation['failure']) == 3
        # Exercise the real installed correlated handler, through the native
        # adapter intake, with a mismatched retained owner. This is not an
        # external callback: that boundary is disabled above.
        wrong = MessageEvent(text='WRONG_OWNER_MUST_NOT_RUN',
            source=adapter.build_source(chat_id=beta['id'], chat_type='dm',
                user_id='unrelated-actor', message_id='wrong-owner'),
            message_id='wrong-owner', message_type=MessageType.TEXT, allow_gateway_control=False)
        await adapter.handle_message(wrong)
        wrong_task = adapter._session_tasks.get(adapter._event_session_key(wrong))
        if wrong_task is not None:
            await asyncio.wait_for(asyncio.shield(wrong_task), 5)
        unchanged = adapter.handoffs.get(beta['id'])
        assert all(unchanged[key] == beta[key] for key in (
            'native_session_id', 'native_task_id', 'native_turn_id', 'response', 'stop'))
        assert len(generation['alpha']) == 2 and len(generation['beta']) == 1
        assert 'Native task participant does not match its owner' in (home/'logs'/'errors.log').read_text()
        assert all(handle.gateway != 'pacomind_task' for handle in await contacts.get_handles(owner))
        ledger.erase_sources(contact_id=owner, turn_ids=[updates[0]['source']['source_refs'][0]['source_id']])
        inspected = await asyncio.wait_for(runner._handle_message(event('STATUS_ERASED')), 12)
        assert inspected == 'FG_STATUS_ERASED_ACK', inspected
        assert adapter.handoffs.get(task_ids['beta'])['response'] == beta['response']
        adapter.send_document.assert_not_awaited()
        print(json.dumps({'cross_channel_native_tasks': True, 'steer_delivery': steer_delivery, 'separate_native_roots': 2,
            'submission': submission, 'explicit_handoff_ends_without_another_provider_call': submission == 'handoff',
            'queued_update_source_read_in_another_owner_conversation': True,
            'status_flags_track_native_request_visibility': True, 'erased_status_refs_withheld': True,
            'foreground_completed_while_tasks_held': True, 'steering_in_actual_sdk_request': True,
            'matching_native_stop_terminal': True, 'late_reply_suppressed': True,
            'retry_exhaustion_retains_typed_failure_without_answer_receipt': True,
            'failure_notice_precedes_settlement_and_cleanup_refreshes_exact_task': True,
            'failed_status_survives_database_reopen_without_redispatch': True,
            'owner_resume_preserves_same_task_session_and_completed_file_effect': True,
            'stale_duplicate_stopped_completed_resume_does_not_execute': True,
            'native_recollection_uses_canonical_owner': True,
            'external_task_callback_disabled': True, 'wrong_owner_native_entry_rejected': True,
            'unrelated_task_completed': True, 'physical_channels_exercised': False,
            'model_quality_measured': False}))
    finally:
        tearing_down = True
        for gate in release.values():
            gate.set()
        await adapter.disconnect()
        runner._running = False
        for value in runner._agent_cache.values():
            value[0].close()
        if runner._executor:
            runner._executor.shutdown(wait=True, cancel_futures=True)


try:
    asyncio.run(exercise())
    if steer_delivery == 'next_turn':
        from types import SimpleNamespace
        from hermes_state import SessionDB
        from pacomind_hermes.client import TurnOutbox, PacoMindClient
        from pacomind_hermes.native_owned_copies import NativeOwnedCopies
        session = adapter.handoffs.get(task_ids['alpha'])['native_session_id']
        with SessionDB(home/'state.db') as native:
            before = {row['id']:dict(row) for row in native._conn.execute(
                'SELECT * FROM messages WHERE session_id=? ORDER BY id', (session,))}
            carrier_id = next(key for key,row in before.items()
                if row['role'] == 'user' and update_text in row['content'])
            assert before[carrier_id]['display_kind'] is None, 'Fixture used mid-tool steering'
            unrelated_id = native.append_message(session, 'user', 'An unrelated later task remains intact.')
            unrelated = dict(native._conn.execute('SELECT * FROM messages WHERE id=?', (unrelated_id,)).fetchone())
        owned = NativeOwnedCopies(SimpleNamespace(outbox=TurnOutbox(home/'outbox.db'),
            client=PacoMindClient('http://fixture', secret)), None)
        reservations = owned._rows(owner)
        copies = [row for row in reservations if row['metadata'].get('anchor_id') == carrier_id]
        updates = [row for row in copies if row['metadata'].get('update_carrier_hash')]
        assert len(updates) == 1 and updates[0]['metadata']['payload_anchor'], copies
        assert all(not row['metadata'].get('payload_anchor') for row in copies
            if not row['metadata'].get('update_carrier_hash')), 'Recall and steering ownership merged'
        erased = asyncio.run(owned.reconcile(contact=owner, gateway=runner))
        with SessionDB(home/'state.db') as native:
            after = {row['id']:dict(row) for row in native._conn.execute(
                'SELECT * FROM messages WHERE session_id=? ORDER BY id', (session,))}
        assert after[carrier_id]['content'] == '[Content removed.]', erased
        assert all(after[key] == row for key,row in before.items() if key < carrier_id)
        assert after[unrelated_id] == unrelated
        print(json.dumps({'late_steer_exact_payload_erased': True,
                          'earlier_task_and_unrelated_later_input_retained': True}))
finally:
    api.__exit__(None, None, None)
    asyncio.run(contacts.close())
