"""Mixed adapter hooks must not adopt another transport's active handoff."""
import importlib.util
import os

import pytest

from conftest import run_python


PROBE = r'''
import asyncio, json, socket, sys
from pathlib import Path
from types import SimpleNamespace
sys.path[:0] = [sys.argv[1], *([sys.argv[2]] if sys.argv[2] else [])]
if sys.argv[3]: sys.path.append(sys.argv[3])
def no_network(*a, **kw): raise AssertionError('Hook ownership has no network dependency')
socket.socket.connect = no_network
socket.create_connection = no_network
from gateway.config import PlatformConfig
from pacomind_hermes.client import TurnOutbox
from pacomind_hermes.task_controller import NativeTasks
from pacomind_hermes.task_handoffs import TaskHandoffs, TaskHandoffError
from pacomind_hermes.native_task_platform import ACTIVE, NativeTaskAdapter, bind_native_turn, finish_native_turn
from pacomind_hermes.input_provenance import current
from gateway.platforms.event import MessageEvent

outbox = TurnOutbox(Path('outbox.sqlite3').absolute())
outbox.prepare()
controller = NativeTasks(None, outbox, 'owner', state_path=Path('tasks.sqlite3').absolute())
source = {'version':1, 'principal':'hermes:cli', 'source_session_id':'ordinary',
    'input_refs':[{'source_id':'instruction','input_message_hash':'a'*64}],
    'source_refs':[{'source_id':'instruction','source_version':'b'*64}],
    'contact_id':'owner', 'watermark':0,
    'origin':{'platform':'cli','authority_gateway':'cli','sender_id':'local',
              'session_id':'ordinary','turn_id':'original-turn'}}
readable, granted = True, True
def resolve_source(value, dependencies=None):
    if not readable: raise TaskHandoffError('Source erased')
    return dict(value)
def resolve_owner(value, require_task_grant):
    if require_task_grant and not granted: raise TaskHandoffError('Task grant revoked')
    return 'owner'
controller.handoffs = TaskHandoffs(controller.database, resolve_source, resolve_owner)
row = controller.handoffs.admit(request_id='original', request='Inspect local notes', source_input=source)
config = PlatformConfig(enabled=True)
# Built-in platform keeps this focused check independent of plugin discovery.
# Two actual adapter instances deliberately share a platform and store, so a
# platform-string-only ownership check would still adopt the wrong callback.
owned = NativeTaskAdapter(config, handoffs=controller.handoffs, platform_name='api_server')
foreign = NativeTaskAdapter(config, handoffs=controller.handoffs, platform_name='api_server')
controller.adapter = owned
native = {'platform':'api_server','sender_id':'owner','session_id':'native',
          'task_id':'task','turn_id':'turn'}
active = {'adapter':foreign, 'handoffs':controller.handoffs, 'id':row['id'], 'supplied':None}
token = ACTIVE.set(active)
try:
    assert controller.bind_native_turn(**native) is None
    assert controller.finish_native_turn(**native, interrupted=True) is None
    assert controller.settle_native_turn(**native, outcome={'failed': True}) is None
    assert controller.handoffs.get(row['id'])['native_session_id'] is None
    assert controller.handoffs.get(row['id'])['terminal'] is None
    # The foreign transport can still call the generic exports itself.
    assert bind_native_turn(**native)['context'] == foreign.delivery_context
    assert controller.finish_native_turn(**native, interrupted=True) is None
    assert controller.settle_native_turn(**native, outcome={'failed': True}) is None
    assert controller.handoffs.get(row['id'])['terminal'] is None
    finish_native_turn(**native, interrupted=True)
    assert controller.handoffs.get(row['id'])['terminal']['turn_id'] == 'turn'
finally: ACTIVE.reset(token)
successor = {**native, 'turn_id':'successor'}
active = {'adapter':owned, 'handoffs':controller.handoffs, 'id':row['id'], 'supplied':None}
token = ACTIVE.set(active)
try:
    assert controller.bind_native_turn(**successor)['context'] == owned.delivery_context
    assert controller.handoffs.get(row['id'])['terminal'] is None
    fields = controller.native_scope_fields(**successor)
    assert fields == {'sender_id':'local','contact_id':'owner','authority_lane':'system',
                      'resolution_status':'attested_system','authority_gateway':'cli'}
    for mismatch in ({'sender_id':'stranger'}, {'turn_id':'prior'}, {'parent_session_id':'native'}):
        try: controller.native_scope_fields(**{**successor, **mismatch})
        except TaskHandoffError: pass
        else: raise AssertionError('A different native origin acquired this task source')
    # An old runtime has no outcome argument; it cannot create a failure
    # receipt. A late failure from the prior generation is also ignored.
    controller.settle_native_turn(**successor)
    controller.settle_native_turn(**native, outcome={'failed': True, 'failure_reason': 'timeout'})
    assert controller.handoffs.get(row['id'])['terminal'] is None
    controller.settle_native_turn(**successor, outcome={
        'completed': False, 'failed': True, 'failure_reason': 'timeout', 'failure_retryable': True})
    failed = controller.handoffs.get(row['id'])
    assert NativeTasks._metadata(failed)['status'] == 'failed'
    assert failed['terminal']['turn_id'] == 'successor' and failed['response'] is None
    # The next real native binding clears the failed receipt, preserving the
    # same original source while admitting a new turn identity.
    restarted = {**successor, 'turn_id': 'resumed-turn'}
    controller.bind_native_turn(**restarted)
    assert controller.handoffs.get(row['id'])['terminal'] is None
    controller.settle_native_turn(**successor, outcome={'failed': True})
    assert controller.handoffs.get(row['id'])['terminal'] is None
    controller.finish_native_turn(**restarted, interrupted=True)
    assert controller.handoffs.get(row['id'])['terminal']['turn_id'] == 'resumed-turn'
finally: ACTIVE.reset(token)
before = controller.handoffs.get(row['id'])
for reason in ('Source erased', 'Task grant revoked'):
    readable, granted = reason != 'Source erased', reason != 'Task grant revoked'
    try: asyncio.run(owned.resume(row['id'], before['native_turn_id']))
    except TaskHandoffError as error: assert str(error) == reason, str(error)
    else: raise AssertionError('Resume ignored current source/owner policy')
    assert controller.handoffs.get(row['id']) == before
readable = granted = True

async def source_outcome(kind):
    task = controller.handoffs.admit(request_id='source-outcome-' + kind,
        request='Inspect local notes', source_input=source)
    fields = {**native, 'session_id':'source-session-' + kind,
              'task_id':'source-task-' + kind, 'turn_id':'source-turn-' + kind}
    prose = 'The native model returned a final answer.'

    async def native_result(event):
        bind_native_turn(**fields)
        supplied = current()
        scope = SimpleNamespace(**{key:fields[key] for key in ('session_id', 'task_id', 'turn_id')},
            contact_id='owner', platform='api_server', authority_lane='system', valid_participant=True)
        supplied.bind(scope)
        if kind == 'ownership':
            supplied.block_update_ownership()
        elif kind == 'freshness':
            assert not supplied.allowed(scope, fresh=False, rules=[], freshness_retryable=True)
        else:
            assert supplied.allowed(scope, fresh=True, rules=[])
            supplied.completed(scope, fields['turn_id'], source['source_refs'])
        finish_native_turn(**fields, completed=True, failed=False, interrupted=False,
                           turn_exit_reason='text_response(finish_reason=stop)')
        return prose

    owned.set_message_handler(native_result)
    event = MessageEvent(text=task['request'], source=owned.build_source(
        chat_id=task['id'], chat_type='dm', user_id='owner', message_id=task['id']))
    response = await owned._message_handler(event)
    retained = controller.handoffs.get(task['id'])
    terminal = retained['terminal']
    assert terminal['turn_id'] == fields['turn_id']
    assert terminal['turn_exit_reason'] == 'text_response(finish_reason=stop)'
    assert terminal['interrupted'] is False and retained['stop'] is None
    assert retained['source'] == task['source'] and retained['request'] == task['request']
    if kind == 'healthy':
        assert response == prose and terminal['completed'] and not terminal['failed']
        assert 'failure_reason' not in terminal and 'failure_retryable' not in terminal
        assert (await owned.send(task['id'], response, metadata={'notify':True})).success
        assert NativeTasks._metadata(controller.handoffs.get(task['id']))['status'] == 'done'
    else:
        reason = 'source_update_ownership_unavailable' if kind == 'ownership' else 'source_freshness_unavailable'
        assert response is None and retained['response'] is None
        assert terminal['failed'] and not terminal['completed']
        assert terminal['failure_reason'] == reason
        assert terminal['failure_retryable'] is (kind == 'freshness')
        assert NativeTasks._metadata(retained)['status'] == 'failed'
        assert json.loads(retained['notice_json'])['text'] == 'The task turn failed. Its original request and conversation are retained.'
        assert prose not in json.dumps(retained)

for kind in ('healthy', 'ownership', 'freshness'):
    asyncio.run(source_outcome(kind))
assert owned.authorization_is_upstream is False and foreign.authorization_is_upstream is False
assert owned.verify_http_event_request('Bearer anything')[0] is False
print(json.dumps({'mixed_adapter_hooks':True}))
'''


def test_native_controller_hooks_preserve_transport_and_generation(artifacts, tmp_path):
    native = os.environ.get('PACOMIND_TEST_HERMES_PATH', '')
    if not native and importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes for native adapter integration')
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'profile'), HERMES_DISABLE_TELEMETRY='1',
        HERMES_DISABLE_LAZY_INSTALLS='1', PYTHON_DOTENV_DISABLED='1')
    result = run_python('-I', '-c', PROBE, artifacts[3], native,
        os.environ.get('PACOMIND_TEST_DEPENDENCY_PATH', ''), cwd=tmp_path, env=env)
    assert '"mixed_adapter_hooks": true' in result.stdout
