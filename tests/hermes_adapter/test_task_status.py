"""Status distinguishes dispatch, native turn markers, and retained reports."""
import importlib.util
import os

import pytest

from conftest import run_python


PROBE = r'''
import asyncio, json, socket, sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
sys.path[:0] = [sys.argv[1], *([sys.argv[2]] if sys.argv[2] else [])]
if sys.argv[3]: sys.path.append(sys.argv[3])
def no_network(*a, **kw): raise AssertionError('Status evidence has no network dependency')
socket.socket.connect = no_network
socket.create_connection = no_network
from gateway.config import GatewayConfig, PlatformConfig
from gateway.session import SessionStore
from pacomind_hermes.client import TurnOutbox
from pacomind_hermes.task_controller import NativeTasks
from pacomind_hermes.task_handoffs import TaskHandoffError
from pacomind_hermes.native_task_platform import NativeTaskAdapter

source = {'version':1, 'principal':'fixture', 'source_session_id':'source-session',
    'input_refs':[{'source_id':'source-input','input_message_hash':'a'*64}],
    'source_refs':[{'source_id':'source-input','source_version':'b'*64}],
    'contact_id':'owner', 'watermark':0}
class Sources:
    readable = True
    def resolve_source(self, value, dependencies=None):
        if not self.readable: raise TaskHandoffError('Source erased')
        return dict(value)
    def resolve_owner(self, value, *, require_task_grant): return 'owner'
    def authorize_control(self, value, scope): assert scope.contact_id == value['contact_id']
sources = Sources()
outbox = TurnOutbox(Path('outbox.sqlite3').absolute())
outbox.prepare()
controller = NativeTasks(None, outbox, 'owner', sources=sources)
row = controller.handoffs.admit(request_id='status-evidence', request='Inspect local notes', source_input=source)
adapter = NativeTaskAdapter(PlatformConfig(enabled=True), handoffs=controller.handoffs, platform_name='api_server')
adapter._task_model_role = lambda retained=None: None
store = SessionStore(Path('native-sessions'), GatewayConfig())
adapter.set_session_store(store)
controller.adapter = adapter
scope = SimpleNamespace(valid_participant=True, contact_id='owner', authority_lane='owner')
observations = []

async def expect(status, basis):
    before = datetime.now(timezone.utc)
    view = await adapter.status(row['id'])
    after = datetime.now(timezone.utc)
    assert (view['status'], view['status_basis']) == (status, basis), view
    assert before <= datetime.fromisoformat(view['status_observed_at_utc']) <= after, view
    observations.append(view)
    return view

async def main():
    await expect('queued', 'retained_admission')
    unknown = json.loads(controller.handle({'operation':'status', 'task_id':row['id']}, scope))
    assert unknown['status'] == 'unknown' and unknown['status_basis'] == 'native_liveness_unobserved', unknown
    assert unknown['status_observed_at_utc']
    started, create_session, created, finish = (asyncio.Event() for _ in range(4))
    events, entries = [], []
    async def held_admission(event):
        events.append(event)
        started.set()
        await create_session.wait()
        entries.append(store.get_or_create_session(event.source, touch_activity=False))
        created.set()
        await finish.wait()
    adapter.handle_message = held_admission
    dispatch = asyncio.create_task(adapter.dispatch_native_event({'handoff_id':row['id']}))
    await asyncio.wait_for(started.wait(), timeout=5)
    try:
        await expect('dispatching', 'adapter_dispatch_in_progress')
        replay = await adapter.dispatch_native_event({'handoff_id':row['id']})
        assert replay['replayed'] and len(events) == 1, replay
        create_session.set()
        await asyncio.wait_for(created.wait(), timeout=5)
        # Native admission may create an empty session before taking its turn.
        await expect('dispatching', 'adapter_dispatch_in_progress')
        entry = entries[0]
        token = store.mark_turn_active(entry.session_key)
        assert token
        await expect('running', 'native_active_turn_token')
        assert store.clear_turn_active(entry.session_key, token)
        await expect('dispatching', 'adapter_dispatch_in_progress')
    finally:
        create_session.set()
        finish.set()
        await dispatch
    await expect('unavailable', 'native_session_idle')
    assert store.mark_resume_pending(entry.session_key)
    await expect('interrupted', 'native_resume_pending')
    assert store.clear_resume_pending(entry.session_key)
    assert store.suspend_session(entry.session_key)
    await expect('interrupted', 'native_suspended')

    native = {'session_id':entry.session_id, 'task_id':'native-task', 'turn_id':'native-turn'}
    controller.handoffs.bind(row['id'], native)
    controller.handoffs.observe_terminal(row['id'], {**native, 'failed':True})
    failed = await expect('failed', 'native_on_session_end')
    assert failed['failure'] == controller.handoffs.get(row['id'])['terminal']
    controller.handoffs.bind(row['id'], {**native, 'turn_id':'next-turn'})
    dependencies = {**source, 'session_id':entry.session_id, 'turn_id':'next-turn'}
    controller.handoffs.complete_source(row['id'], dependencies)
    prose = 'The assistant reports that the requested external update was applied.'
    retained = controller.handoffs.retain_reply(row['id'], prose)
    online = await expect('done', 'retained_assistant_report')
    offline = json.loads(controller.handle({'operation':'status', 'task_id':row['id']}, scope))
    for view in (online, offline):
        assert view['result'] == prose and view['source_dependencies'] == dependencies, view
        assert view['status_basis'] == 'retained_assistant_report' and view['status_observed_at_utc'], view
        assert view['result_provenance'] == {'kind':'assistant_report', 'assertions':'unverified',
                                              'external_effects':'unobserved'}, view
    metadata = NativeTasks._metadata(controller.handoffs.get(row['id']))
    assert metadata['result_provenance'] == online['result_provenance'] and 'result' not in metadata
    # Inspection adds no receipt and never rewrites the retained assistant text.
    assert controller.handoffs.get(row['id'])['response'] == retained
    sources.readable = False
    hidden = await expect('unavailable', 'source_unavailable')
    assert 'result' not in hidden and 'source_dependencies' not in hidden
asyncio.run(main())
print(json.dumps({'status_evidence':True, 'duplicate_dispatch_prevented':True}))
'''


def test_native_status_evidence_and_report_provenance(artifacts, tmp_path):
    native = os.environ.get('PACOMIND_TEST_HERMES_PATH', '')
    if not native and importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes for native adapter integration')
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'profile'), HERMES_DISABLE_TELEMETRY='1',
        HERMES_DISABLE_LAZY_INSTALLS='1', PYTHON_DOTENV_DISABLED='1')
    result = run_python('-I', '-c', PROBE, artifacts[3], native,
        os.environ.get('PACOMIND_TEST_DEPENDENCY_PATH', ''), cwd=tmp_path, env=env)
    assert '"status_evidence": true' in result.stdout
