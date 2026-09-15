"""Native observer to scoped HTTP, SQLite, owner readout and CLI formatting."""
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from protagine.api.authority import RequestAuthority, required_scope
from protagine.api.routers import executions
from protagine.qualification.diagnostics import render, diagnose
from protagine.turns import TurnIdempotencyLedger
from protagine.turns.executions import ExecutionRegistry


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv('PROTAGINE_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('PROTAGINE_OWNER_CONTACT_ID', 'owner')
    store = ExecutionRegistry(TurnIdempotencyLedger(tmp_path / 'turn-idempotency.db'))
    monkeypatch.setattr(executions, 'registry', lambda: store)
    principal = [RequestAuthority(principal_id='host', credential_id='key',
        scopes=frozenset({'context:read', 'turns:write'}), viewer_person_id='owner',
        person_ids=frozenset({'owner'}), audiences=frozenset({'owner'}), authenticated=True)]
    app = FastAPI()
    @app.middleware('http')
    async def authority(request, call_next):
        request.state.protagine_authority = principal[0]
        return await call_next(request)
    app.include_router(executions.router)
    with TestClient(app) as api:
        path = Path(__file__).resolve().parents[2] / 'plugins/hermes-plugin/executions.py'
        spec = importlib.util.spec_from_file_location('diagnostic_observer', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        class Connection:
            def post(self, path, *, json, **kwargs):
                result = api.post(path, json=json)
                assert result.status_code == 200, result.text
                return result
        observer = module.ExecutionObserver(Connection())
        observer.start(SimpleNamespace(valid_participant=True, contact_id='owner', platform='whatsapp'),
                       session_id='session', turn_id='turn')
        identifier = hashlib.sha256(f'{observer.instance}:session:turn'.encode()).hexdigest()
        yield SimpleNamespace(api=api, observer=observer, store=store, principal=principal,
                              execution_id=identifier, tmp=tmp_path)


def test_readout_retains_attempts_and_missing_evidence_without_grading_quality(harness):
    h = harness
    for index, model in enumerate(['fast', 'fast', 'bulk'], start=1):
        shared = dict(session_id='session', turn_id='turn', api_request_id=f'call-{index}',
            api_call_count=index, model=model, provider='local-' + model,
            api_mode='chat_completions', started_at=1000 + index * 10)
        h.observer.api('start', **shared)
        # Older callbacks lack shape fields. They must remain unknown, not empty.
        shape = {} if index == 1 else dict(finish_reason='stop', assistant_content_chars=0 if index == 2 else 12,
            assistant_tool_call_count=0, api_duration=3.5, first_chunk_at=1000 + index * 10 + .1,
            assistant_message=SimpleNamespace(content='PRIVATE TEXT', reasoning='PRIVATE REASON',
                tool_calls=[]), response={'PRIVATE': 'RAW RESPONSE'}, base_url='https://private.invalid')
        h.observer.api('response', **shared, response_model=model + '-served',
                       ended_at=shared['started_at'] + 3.5, **shape)
    h.observer.end(session_id='session', turn_id='turn', completed=True)
    params = {'contact_id': 'owner', 'execution_id': h.execution_id}
    report = h.api.get('/v1/host/executions/model-calls', params=params).json()
    assert report['state'] == 'completed' and report['total_requests'] == 3
    assert report['complete_observed_pairs'] is True
    assert [row['request_id'] for row in report['calls']] == ['call-1', 'call-2', 'call-3']
    assert report['calls'][0]['response']['assistant_content_chars'] is None
    assert report['calls'][0]['observations'] == []
    assert report['calls'][1]['observations'] == ['no_visible_text_or_tool_calls']
    assert report['calls'][2]['response']['response_model'] == 'bulk-served'
    assert report['calls'][1]['response']['assistant_reasoning_chars'] == len('PRIVATE REASON')
    assert 'PRIVATE' not in json.dumps(report) and 'private.invalid' not in json.dumps(report)
    text = render(report)
    assert 'text_chars=unknown' in text and 'text_chars=0' in text
    assert 'fast-served' in text and 'bulk-served' in text and 'parser loss' in text
    page = h.api.get('/v1/host/executions/model-calls', params={**params, 'offset': 1, 'limit': 1}).json()
    assert page['total_requests'] == 3 and page['more'] and len(page['calls']) == 1
    assert 'use --offset 2' in render(page)
    h.store.clock = lambda: report['last_observed_at'] + 7 * 86400 + 1
    assert h.api.get('/v1/host/executions/model-calls', params=params).status_code == 404


def test_known_api_error_is_visible_and_conflicting_terminal_callbacks_stay_incomplete(harness):
    h = harness
    shared = dict(session_id='session', turn_id='turn', api_request_id='failed-call',
                  model='fast', provider='local', started_at=1000)
    h.observer.api('start', **shared)
    h.observer.api('error', **shared, ended_at=1001, error='PRIVATE EXCEPTION')
    params = {'contact_id': 'owner', 'execution_id': h.execution_id}
    report = h.api.get('/v1/host/executions/model-calls', params=params).json()
    assert report['complete_observed_pairs'] is True
    assert report['calls'][0]['observations'] == ['request_error']
    assert 'request_error' in render(report) and 'PRIVATE' not in json.dumps(report)
    h.observer.api('response', **shared, ended_at=1002)
    report = h.api.get('/v1/host/executions/model-calls', params=params).json()
    assert report['complete_observed_pairs'] is False
    assert 'incomplete_or_conflicting_callbacks' in report['calls'][0]['observations']


def test_diagnostics_require_owner_exact_subject_and_existing_read_scope(harness):
    h = harness
    path = '/v1/host/executions/model-calls'
    params = {'contact_id': 'owner', 'execution_id': h.execution_id}
    assert required_scope('GET', path) == 'context:read'
    assert h.api.get(path, params={**params, 'execution_id': 'not-an-id'}).status_code == 422
    assert h.api.get(path, params={**params, 'limit': 129}).status_code == 422
    assert h.api.get(path, params={**params, 'execution_id': 'f' * 64}).status_code == 404
    h.principal[0] = RequestAuthority(principal_id='guest-host', credential_id='guest-key',
        scopes=frozenset({'context:read'}), viewer_person_id='guest', person_ids=frozenset({'guest'}),
        audiences=frozenset({'viewer'}), authenticated=True)
    assert h.api.get(path, params=params).status_code == 403
    assert h.api.get(path, params={**params, 'contact_id': 'guest'}).status_code == 403


def test_cli_fetches_scoped_read_and_does_not_echo_credentials(harness, monkeypatch, capsys):
    import httpx
    h = harness
    real_client = httpx.Client
    credential = h.tmp / 'credential.json'
    credential.write_text(json.dumps({'principal': 'host', 'secret': 'DO-NOT-PRINT'}))
    def request(req):
        assert req.method == 'GET' and req.headers['Authorization'] == 'Bearer DO-NOT-PRINT'
        assert req.headers['X-Protagine-Principal'] == 'host'
        response = h.api.get(req.url.path, params=dict(req.url.params))
        return httpx.Response(response.status_code, json=response.json())
    def client(**kwargs):
        assert kwargs['follow_redirects'] is False and kwargs['trust_env'] is False
        return real_client(transport=httpx.MockTransport(request), **kwargs)
    monkeypatch.setattr(httpx, 'Client', client)
    args = SimpleNamespace(execution_id=h.execution_id, contact_id='owner',
        credential_file=credential, url='http://sidecar.test', offset=0, limit=20, json=False)
    assert diagnose(args) == 0
    output = capsys.readouterr().out
    assert 'DO-NOT-PRINT' not in output and h.execution_id in output
    assert 'Retained requests: 0' in output and 'complete callback pairs: False' in output
