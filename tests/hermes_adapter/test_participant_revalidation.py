"""Fresh participant authority tolerates service delay without failing open."""
from pathlib import Path

from conftest import run_python


PROBE = r'''
import importlib.util, json, sys, threading, time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

adapter = sys.argv[1]
spec = importlib.util.spec_from_file_location(
    'protagine_hermes', adapter + '/__init__.py', submodule_search_locations=[adapter])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

reply = {'delay': 0.65, 'status': 200, 'contact_id': 'fixture-owner'}
requests = []
class Resolver(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        current = dict(reply)
        requests.append(self.path)
        assert urlsplit(self.path).path == '/v1/host/contacts/resolve'
        assert parse_qs(urlsplit(self.path).query) == {
            'gateway': ['sms'], 'address': ['fixture-sender'], 'create': ['false']}
        time.sleep(current['delay'])
        body = json.dumps({'contact_id': current['contact_id']}).encode()
        try:
            self.send_response(current['status'])
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

server = ThreadingHTTPServer(('127.0.0.1', 0), Resolver)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
client = module.ProtagineClient(url='http://127.0.0.1:' + str(server.server_port))
parent = module._TransportScope(
    'fixture-session', 'fixture-task', 'fixture-turn', 'sms', 'fixture-sender',
    'fixture-owner', 'owner', 'resolved', authority_gateway='sms')
child = replace(parent, session_id='child-session', task_id='child-task',
    turn_id='child-turn', platform='subagent', parent_session_id=parent.session_id)
for scope in (parent, child): module._TRANSPORT_SCOPES.put(scope)
executed = []
def dispatch(scope, tool):
    return module._tool_execution_middleware(
        tool_name=tool, args={}, next_call=lambda args: executed.append(tool) or 'executed',
        session_id=scope.session_id, task_id=scope.task_id, turn_id=scope.turn_id,
        revalidate_participant=lambda current: module._current_participant(client, current))

try:
    # A valid response arriving after the former half-second cutoff still
    # authorizes this exact participant, including inherited external handles.
    assert dispatch(child, 'read_file') == 'executed'
    reply['delay'] = 0
    assert dispatch(parent, 'protagine_memory_retain_observation') == 'executed'
    assert len(requests) == 2

    # Every subsequent native or governed call fetches current authority.
    # A prior success cannot authorize a corrected identity or a revoked grant.
    for status, contact, reason in [
        (200, 'fixture-corrected', 'participant_identity_changed'),
        (401, 'fixture-owner', 'participant_authority_revoked'),
        (403, 'fixture-owner', 'participant_authority_revoked'),
        (503, 'fixture-owner', 'participant_revalidation_unavailable'),
    ]:
        reply.update(status=status, contact_id=contact)
        for tool in ('read_file', 'protagine_memory_retain_observation'):
            before = len(requests)
            result = json.loads(dispatch(child, tool))
            assert result['reason'] == reason, result
            assert result['effect_performed'] is False and result['approval_created'] is False
            assert len(requests) == before + 1
            assert len(executed) == 2

    # Shorten only the test budget: a stalled resolver remains bounded and
    # cannot reach the handler or retry using the earlier successful response.
    reply.update(delay=0.4, status=200, contact_id='fixture-owner')
    with patch.object(module, '_PARTICIPANT_RESOLUTION_TIMEOUT_SECONDS', 0.1):
        before = len(requests)
        began = time.monotonic()
        result = json.loads(dispatch(child, 'read_file'))
        elapsed = time.monotonic() - began
        assert result['reason'] == 'participant_revalidation_unavailable', result
        assert result['effect_performed'] is False and len(executed) == 2
        assert 0.07 <= elapsed < 0.35, elapsed
        assert len(requests) == before + 1
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
print(json.dumps({'delayed_authority': True, 'fresh_and_fail_closed': True}))
'''


def test_delayed_participant_resolution_remains_fresh_and_bounded(tmp_path):
    adapter = Path(__file__).resolve().parents[2] / 'plugins' / 'hermes-plugin'
    result = run_python('-I', '-B', '-c', PROBE, adapter, cwd=tmp_path)
    assert '"fresh_and_fail_closed": true' in result.stdout
