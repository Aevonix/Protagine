"""Two native Hermes sessions race a real durable commitment over HTTP."""
import importlib.util
import json
import os
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest
from conftest import ROOT, run_python


PROBE = r'''
import json, os, sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from urllib.request import urlopen
sys.path.insert(0, sys.argv[1])
home = Path(os.environ['HERMES_HOME']); home.mkdir(mode=0o700)
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
(home / 'config.yaml').write_text(json.dumps({'plugins': {'enabled': ['protagine'], 'protagine': {
    'url': sys.argv[2], 'owner_contact_id': 'owner', 'attested_system_platforms': ['cli'],
    'turn_outbox_path': str(home / 'turns.sqlite3')}}}))
from hermes_cli.plugins import get_plugin_manager
from hermes_cli.lifecycle import invoke_hook
from hermes_cli.middleware import run_tool_execution_middleware
from model_tools import handle_function_call
manager = get_plugin_manager(); manager.discover_and_load()
assert manager._plugins['protagine'].enabled, manager._plugins['protagine'].error
assert 'protagine_commitment_work' in manager._plugins['protagine'].tools_registered
def start(session):
    invoke_hook('pre_llm_call', session_id=session, task_id=session, turn_id=session,
                platform='cli', sender_id='', user_message='Inspect the same obligation')
def work(session, operation, commitment_id=None):
    return json.loads(handle_function_call('protagine_commitment_work', {'commitment_id': commitment_id or sys.argv[3], 'operation': operation},
        session_id=session, task_id=session, turn_id=session, tool_call_id='call-' + session))
start('invalid-target')
rejected = work('invalid-target', 'claim', 'unlisted-obligation')
assert rejected['reason'] == 'unknown_commitment' and rejected['detached'] is True, rejected
assert rejected['effect_performed'] is False and rejected['effect_authorized'] is False, rejected
assert run_tool_execution_middleware('read_file', {}, lambda args: 'unrelated-read',
    session_id='invalid-target', task_id='invalid-target', turn_id='invalid-target') == 'unrelated-read'
for session in ('chat', 'voice'): start(session)
with ThreadPoolExecutor(max_workers=2) as pool:
    results = list(pool.map(lambda session: work(session, 'claim'), ['chat', 'voice']))
assert sorted(row['accepted'] for row in results) == [False, True], results
assert all('claim_id' not in row for row in results)
winner = next(row['session_id'] for row in results if row['accepted'])
loser = next(session for session in ('chat', 'voice') if session != winner)
assert work(loser, 'status')['session_id'] == winner
assert run_tool_execution_middleware('read_file', {}, lambda args: 'executed',
    session_id=winner, task_id=winner, turn_id=winner) == 'executed'
urlopen(sys.argv[2] + '/advance-clock').close()
start('recovered'); assert work('recovered', 'claim')['accepted']
called = []
stale = run_tool_execution_middleware('read_file', {}, lambda args: called.append(args),
    session_id=winner, task_id=winner, turn_id=winner)
assert json.loads(stale)['effect_performed'] is False and called == []
assert run_tool_execution_middleware('read_file', {}, lambda args: 'executed',
    session_id='recovered', task_id='recovered', turn_id='recovered') == 'executed'
invoke_hook('subagent_start', parent_session_id='recovered', parent_turn_id='recovered', child_session_id='child')
invoke_hook('pre_llm_call', session_id='child', task_id='child-task', turn_id='child-turn',
            parent_session_id='recovered', platform='subagent', user_message='Inspect one part')
invoke_hook('pre_api_request', session_id='child-rotated', task_id='child-task', turn_id='child-turn')
assert run_tool_execution_middleware('read_file', {}, lambda args: 'executed',
    session_id='child-rotated', task_id='child-task', turn_id='child-turn') == 'executed'
assert work('recovered', 'release')['accepted']
assert work('chat', 'status')['work_state'] == 'released'
child_result = run_tool_execution_middleware('read_file', {}, lambda args: 'must-not-run',
    session_id='child-rotated', task_id='child-task', turn_id='child-turn')
assert json.loads(child_result)['effect_performed'] is False
# Explicit stop is local after an authoritative stale response, including child rotation.
stopped = json.loads(handle_function_call('protagine_commitment_work', {'commitment_id': sys.argv[3], 'operation': 'release'},
    session_id='child-rotated', task_id='child-task', turn_id='child-turn'))
assert stopped['detached']
assert run_tool_execution_middleware('read_file', {}, lambda args: 'executed',
    session_id='child-rotated', task_id='child-task', turn_id='child-turn') == 'executed'
print(json.dumps({'native_race': True, 'recovery_fenced': True}))
'''


def test_native_sessions_share_one_undertaking(artifacts, tmp_path, monkeypatch):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes to exercise native coordination')
    # Only the test HTTP server imports the source store. The isolated native
    # process receives the wheel alone and has no sidecar on its import path.
    monkeypatch.syspath_prepend(str(ROOT / 'sidecar'))
    from protagine.commitments.store import CommitmentStore
    from protagine.commitments.work import CommitmentWork
    store = CommitmentStore(tmp_path / 'commitments.db')
    obligation = store.create('owner', 'Inspect one failing fixture')
    now = [1000.0]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            assert self.path == '/advance-clock'
            now[0] += 121
            self.send_response(200); self.end_headers()
        def do_POST(self):
            prefix = '/v1/host/commitments/'
            assert self.path.startswith(prefix) and self.path.endswith('/work')
            identifier = self.path[len(prefix):-len('/work')]
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert body['contact_id'] == 'owner'
            status = 200
            try:
                result = CommitmentWork(store, clock=lambda: now[0]).operate(identifier, principal_id='host', **body)
            except KeyError:
                # Same authoritative no-mutation response as the existing API route.
                status, result = 404, {'detail': 'unknown commitment'}
            encoded = json.dumps(result).encode()
            self.send_response(status); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded))); self.end_headers(); self.wfile.write(encoded)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    _, _, _, installed = artifacts
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path / 'profile'), HERMES_BUNDLED_PLUGINS=str(tmp_path / 'bundled'),
        PROTAGINE_GENERAL_PLUGIN_ACTIVE='1', PROTAGINE_MEMORY_WORKER_TOOLS='0', PROTAGINE_MEMORY_TURN_WRITER='disabled')
    try:
        result = run_python('-I', '-c', PROBE, installed, 'http://127.0.0.1:' + str(server.server_port), obligation['id'], cwd=tmp_path, env=env)
        assert json.loads(result.stdout.splitlines()[-1])['native_race'] is True
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


@pytest.fixture
def coordinator_class(artifacts):
    module_path = artifacts[3] / 'protagine_hermes' / 'commitment_work.py'
    spec = importlib.util.spec_from_file_location('isolated_commitment_coordinator', module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CommitmentCoordinator


def _reply(status, body):
    return httpx.Response(status, json=body,
        request=httpx.Request('POST', 'http://fixture.invalid/v1/host/commitments/obligation/work'))


_SCOPE = SimpleNamespace(valid_participant=True, contact_id='owner')
_CONTEXT = {'session_id': 'session', 'task_id': 'task', 'turn_id': 'turn', 'tool_name': 'read_file'}
_CLAIM = {'operation': 'claim', 'commitment_id': 'obligation'}


@pytest.mark.parametrize('failure', ['unknown', 'other404', 'invalid_json', 'unavailable', 'timeout', 'invalid_token', 'held_elsewhere'])
def test_claim_rejection_and_uncertainty(coordinator_class, failure):
    calls = []
    def post(*args, **kwargs):
        calls.append(kwargs['json'])
        if failure == 'timeout':
            raise httpx.ReadTimeout('Reply lost after possible admission')
        if failure == 'invalid_json':
            return httpx.Response(404, text='not JSON', request=httpx.Request('POST', 'http://fixture.invalid'))
        return {'unknown': _reply(404, {'detail': 'unknown commitment'}),
            'other404': _reply(404, {'detail': 'proxy route not found'}),
            'unavailable': _reply(503, {'detail': 'commitment store unavailable'}),
            'invalid_token': _reply(200, {'commitment_id': 'obligation', 'accepted': True}),
            'held_elsewhere': _reply(200, {'commitment_id': 'obligation', 'accepted': False,
                'reason': 'undertaken_elsewhere'})}[failure]
    coordinator = coordinator_class(SimpleNamespace(post=post))
    result = json.loads(coordinator.handle(_CLAIM, _SCOPE, _CONTEXT))
    fence = coordinator.before_tool(_CONTEXT)
    if failure == 'unknown':
        assert result['reason'] == 'unknown_commitment' and result['detached'] is True
        assert result['effect_performed'] is False and result['accepted'] is False
        assert fence is None
    else:
        assert json.loads(fence)['effect_performed'] is False
        if failure != 'held_elsewhere':
            assert result['outcome'] == 'unconfirmed' and 'effect_performed' not in result
        released = json.loads(coordinator.handle({**_CLAIM, 'operation': 'release'}, _SCOPE, _CONTEXT))
        assert released['detached'] and released['effect_authorized'] is False
        assert coordinator.before_tool(_CONTEXT) is None
    assert len(calls) == 1  # Local detach never retries or releases someone else's lease.


@pytest.mark.parametrize('replacement', ['rotation', 'confirmed', 'pending'])
def test_rejected_attempt_preserves_concurrent_claim(coordinator_class, replacement):
    coordinator = None
    calls = []
    rotated = {**_CONTEXT, 'session_id': 'rotated'}
    def post(*args, **kwargs):
        calls.append(kwargs['json'])
        if len(calls) == 1:
            if replacement == 'rotation':
                coordinator.bind_turn(**rotated)
            else:
                nested = json.loads(coordinator.handle(_CLAIM, _SCOPE, _CONTEXT))
                assert nested.get('accepted') is True if replacement == 'confirmed' else nested['outcome'] == 'unconfirmed'
            return _reply(404, {'detail': 'unknown commitment'})
        if replacement == 'pending':
            raise httpx.ReadTimeout('Concurrent reply lost')
        return _reply(200, {'commitment_id': 'obligation', 'accepted': True, 'claim_id': 'a' * 32})
    coordinator = coordinator_class(SimpleNamespace(post=post))
    rejected = json.loads(coordinator.handle(_CLAIM, _SCOPE, _CONTEXT))
    assert rejected['reason'] == 'unknown_commitment'
    if replacement == 'rotation':
        assert rejected['detached'] is True
        assert coordinator.before_tool(rotated) is None and coordinator.before_tool(_CONTEXT) is None
    else:
        assert rejected['detached'] is False
        held = coordinator._claim(_CONTEXT)
        assert held['claim_id'] == ('a' * 32 if replacement == 'confirmed' else '')
        result = coordinator.before_tool(_CONTEXT)
        assert result is None if replacement == 'confirmed' else json.loads(result)['effect_performed'] is False


@pytest.mark.parametrize('retry_status', [404, 503])
def test_failed_reclaim_keeps_confirmed_token(coordinator_class, retry_status):
    calls = []
    def post(*args, **kwargs):
        payload = kwargs['json']; calls.append(payload)
        if len(calls) == 2:
            return _reply(retry_status, {'detail': 'unknown commitment' if retry_status == 404 else 'unavailable'})
        if payload['operation'] == 'renew':
            assert payload['claim_id'] == 'a' * 32
        return _reply(200, {'commitment_id': 'obligation', 'accepted': True, 'claim_id': 'a' * 32})
    coordinator = coordinator_class(SimpleNamespace(post=post))
    assert json.loads(coordinator.handle(_CLAIM, _SCOPE, _CONTEXT))['accepted']
    retried = json.loads(coordinator.handle(_CLAIM, _SCOPE, _CONTEXT))
    assert 'claim_id' not in retried and not retried.get('detached')
    assert coordinator._claim(_CONTEXT)['claim_id'] == 'a' * 32
    assert coordinator.before_tool(_CONTEXT) is None  # Actual renewal, not the error, permits this tool.
