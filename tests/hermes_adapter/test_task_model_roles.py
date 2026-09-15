"""Declared per-task roles reach the actual native session store before dispatch."""
import importlib.util
import os

import pytest

from conftest import run_python


PROBE = r'''
import asyncio, copy, json, socket, sys
from pathlib import Path
from types import SimpleNamespace
sys.path[:0] = [sys.argv[1], *([sys.argv[2]] if sys.argv[2] else [])]
if sys.argv[3]: sys.path.append(sys.argv[3])
def no_network(*a, **kw): raise AssertionError('Task role selection needs no network')
socket.socket.connect = no_network
socket.create_connection = no_network
from gateway.config import GatewayConfig, PlatformConfig
from gateway.session import SessionStore
from protagine_hermes.client import TurnOutbox
from protagine_hermes.task_controller import NativeTasks
from protagine_hermes.task_handoffs import TaskHandoffs, TaskHandoffError
from protagine_hermes.native_task_platform import NativeTaskAdapter, TASK_ROLE_METADATA
from protagine_hermes.task_model_roles import configured_task_model_roles, select_task_model_role

home = Path('profile').absolute(); home.mkdir(exist_ok=True)
providers = {
    'fast': {'base_url':'http://unused-fast/v1','api_key':'fixture-fast-key'},
    'deep': {'base_url':'http://unused-deep/v1','api_key':'fixture-deep-key'}}
outbox = TurnOutbox(Path('outbox.sqlite3').absolute()); outbox.prepare()
source = {'version':1,'principal':'hermes:cli','source_session_id':'owner-turn',
    'input_refs':[{'source_id':'input','input_message_hash':'a'*64}],
    'source_refs':[{'source_id':'input','source_version':'b'*64}],
    'contact_id':'owner','watermark':0}
controller = NativeTasks(None, outbox, 'owner', state_path=Path('tasks.sqlite3').absolute())
controller.sources = SimpleNamespace(capture=lambda scope: dict(source),
    authorize_control=lambda value, scope: None)
controller.handoffs = TaskHandoffs(controller.database,
    lambda value, dependencies=None: dict(value), lambda value, require_task_grant: 'owner')
coding = {'role':'coding','provider':'fast','model':'fast-one'}
reasoning = {'role':'reasoning','provider':'deep','model':'deep-one'}
extra = {'task_model_role':reasoning, 'task_model_roles':{'coding':coding,'reasoning':reasoning}}
def publish(selected):
    pending = home/'config.pending'
    pending.write_text(json.dumps({'providers': providers,
        'platforms': {'api_server': {'extra': selected}}}))
    pending.replace(home/'config.yaml')
publish(extra)
config = GatewayConfig(sessions_dir=home/'sessions')
store = SessionStore(config.sessions_dir, config)
scope = SimpleNamespace(valid_participant=True,contact_id='owner',authority_lane='owner')
events = []
def opened():
    adapter = NativeTaskAdapter(PlatformConfig(enabled=True,extra=extra),
        handoffs=controller.handoffs,platform_name='api_server')
    adapter.set_session_store(store)
    async def accepted(event):
        events.append(event); event._gateway_accepted = True
    adapter.handle_message = accepted
    return adapter

async def main():
    # A private transport uses the same current profile resolver without an adapter.
    assert configured_task_model_roles("api_server") == {"coding": coding, "reasoning": reasoning}
    assert select_task_model_role("coding", "api_server") == coding
    adapter = opened(); controller.adapter = adapter
    await adapter.connect()
    inventory = json.loads(await asyncio.to_thread(controller.handle, {'operation':'list'},scope))
    assert inventory['configured_model_roles'] == ['coding','reasoning'] and inventory['items'] == []
    # An unknown name is rejected before source capture, handoff or dispatch.
    bad = json.loads(await asyncio.to_thread(controller.handle,
        {'operation':'submit','request':'Check the supplied source','model_role':'imaginary'},scope))
    assert 'not configured' in bad['error'] and controller.handoffs.count() == 0 and not events
    result = json.loads(await asyncio.to_thread(controller.handle,
        {'operation':'submit','request':'Check the supplied source','model_role':'coding'},scope))
    assert result['accepted'] and result['model_role'] == coding and len(events) == 1, result
    key = adapter._event_session_key(events[-1])
    assert store.get_model_override(key) == {'provider':'fast','model':'fast-one'}
    assert store.get_session_metadata(key,TASK_ROLE_METADATA) == coding
    row = controller.handoffs.get(result['task_id'])
    assert row['model_role'] == coding and 'fixture-fast-key' not in json.dumps(row)
    # An on-disk profile change reaches this SAME adapter, not a mutated startup object.
    replacement = {'role':'coding','provider':'replacement','model':'deep-two'}
    providers['replacement'] = {'base_url':'http://unused-new/v1','api_key':'fixture-new-key'}
    changed = copy.deepcopy(extra)
    changed['task_model_roles']['coding'] = replacement
    changed['task_model_roles']['planning'] = {'role':'planning','provider':'deep','model':'plan-one'}
    changed['task_model_role'] = {'role':'reasoning','provider':'replacement','model':'default-two'}
    publish(changed)
    assert select_task_model_role("coding", "api_server") == replacement
    assert "planning" in configured_task_model_roles("api_server")
    assert adapter.config.extra['task_model_roles']['coding'] == coding
    inventory = json.loads(await asyncio.to_thread(controller.handle, {'operation':'list'},scope))
    assert inventory['configured_model_roles'] == ['coding','planning','reasoning'], inventory
    # Same captured request/name cannot silently become different work after a rebind.
    rebound = json.loads(await asyncio.to_thread(controller.handle,
        {'operation':'submit','request':'Check the supplied source','model_role':'coding'},scope))
    assert 'cannot be rebound' in rebound['error'] and len(events) == 1
    assert store.get_model_override(key) == {'provider':'fast','model':'fast-one'}
    # A different source/request gets the new mapping. Parallel tasks keep their own route.
    second = json.loads(await asyncio.to_thread(controller.handle,
        {'operation':'submit','request':'Check a different source','model_role':'coding'},scope))
    second_key = adapter._event_session_key(events[-1])
    assert second['accepted'] and second_key != key
    assert store.get_model_override(second_key) == {'provider':'replacement','model':'deep-two'}
    assert controller.handoffs.get(second['task_id'])['model_role'] == replacement
    assert controller.handoffs.get(result['task_id'])['model_role'] == coding
    # New callers without a role use the current profile default at native selection.
    default = json.loads(await asyncio.to_thread(controller.handle,
        {'operation':'submit','request':'Plan another task'},scope))
    assert default['accepted'] and 'model_role' not in default
    assert store.get_model_override(adapter._event_session_key(events[-1])) == {
        'provider':'replacement','model':'default-two'}
    # Removed or invalid explicit mappings cannot fall through to that default.
    unchanged_count, unchanged_events = controller.handoffs.count(), len(events)
    del changed['task_model_roles']['coding']
    publish(changed)
    inventory = json.loads(await asyncio.to_thread(controller.handle, {'operation':'list'},scope))
    assert inventory['configured_model_roles'] == ['planning','reasoning'], inventory
    for invalid in (changed['task_model_roles'], None, [],
                    {'coding': {'role':'coding','provider':'missing','model':'x'}},
                    {'coding': {'role':'different','provider':'deep','model':'x'}}):
        changed['task_model_roles'] = invalid
        publish(changed)
        failed = json.loads(await asyncio.to_thread(controller.handle,
            {'operation':'submit','request':'Do not use the default','model_role':'coding'},scope))
        assert 'error' in failed and controller.handoffs.count() == unchanged_count, failed
        assert len(events) == unchanged_events
    assert store.get_model_override(key) == {'provider':'fast','model':'fast-one'}
    # Removing the map and default does not resurrect their startup values.
    publish({})
    assert configured_task_model_roles("api_server") == {}
    try:
        select_task_model_role("coding", "api_server")
    except TaskHandoffError:
        pass
    else:
        raise AssertionError("A transport must not resurrect a removed role")
    inventory = json.loads(await asyncio.to_thread(controller.handle, {'operation':'list'},scope))
    assert inventory['configured_model_roles'] == [], inventory
    unconfigured = json.loads(await asyncio.to_thread(controller.handle,
        {'operation':'submit','request':'Use the ordinary native route'},scope))
    assert unconfigured['accepted']
    assert store.get_model_override(adapter._event_session_key(events[-1])) is None
    # Admission persisted before a native session existed. Reopening uses its snapshot.
    held = controller.handoffs.admit(request_id='held',request='Inspect another file',
        source_input=source,model_role=coding)
    reopened = opened()
    await reopened.connect()
    response = await reopened.dispatch_native_event({'handoff_id':held['id']})
    assert response['native_admitted']
    assert store.get_model_override(reopened._event_session_key(events[-1])) == {
        'provider':'fast','model':'fast-one'}
    # Explicit native owner selection still owns an existing session.
    held2 = controller.handoffs.admit(request_id='held2',request='Inspect final file',
        source_input=source,model_role=coding)
    src = reopened.build_source(chat_id=held2['id'],chat_type='dm',user_id='owner',message_id=held2['id'])
    entry = store.get_or_create_session(src,touch_activity=False)
    store.set_model_override(entry.session_key,{'provider':'deep','model':'owner-selected'})
    await reopened.dispatch_native_event({'handoff_id':held2['id']})
    assert store.get_model_override(entry.session_key)['model'] == 'owner-selected'
    await reopened.disconnect(); await adapter.disconnect()
asyncio.run(main())
print(json.dumps({'named_roles_reach_native_sessions':True,'existing_default_preserved':True}))
'''


def test_declared_roles_persist_before_native_execution(artifacts, tmp_path):
    native = os.environ.get('PROTAGINE_TEST_HERMES_PATH', '')
    if not native and importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes for native adapter integration')
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'profile'), HERMES_DISABLE_TELEMETRY='1',
        HERMES_DISABLE_LAZY_INSTALLS='1', PYTHON_DOTENV_DISABLED='1')
    result = run_python('-I', '-c', PROBE, artifacts[3], native,
        os.environ.get('PROTAGINE_TEST_DEPENDENCY_PATH', ''), cwd=tmp_path, env=env)
    assert '"named_roles_reach_native_sessions": true' in result.stdout
