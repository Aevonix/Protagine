"""Actual shared SQLite state, scoped HTTP reads, and native-adapter events."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from onekey import RequestAuthority, required_scope
from protagine.api.routers import executions
from protagine.turns import TurnIdempotencyLedger
from protagine.turns.executions import ExecutionRegistry, format_view


def observation(name="a", **changes):
    return {"execution_id": hashlib.sha256(name.encode()).hexdigest(),
            "contact_id": "contact-a", "session_id": "session-" + name,
            "turn_id": "turn-" + name, "parent_execution_id": "", "platform": "sms",
            "state": "observed", "phase": "turn", "tool_name": "", "sequence": 1,
            **changes}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path))
    return ExecutionRegistry(TurnIdempotencyLedger(tmp_path / "turn-idempotency.db"))


def test_two_sessions_share_durable_view_and_terminal_never_resurrects(store):
    def write(name):
        peer = ExecutionRegistry(TurnIdempotencyLedger(store.ledger.db_path))
        return peer.observe(observation(name), principal_id="host", contact_id="contact-a")
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(row["accepted"] for row in pool.map(write, ("sms", "voice")))
    reopened = ExecutionRegistry(TurnIdempotencyLedger(store.ledger.db_path))
    assert reopened.view(contact_id="owner", owner=True)["total"] == 2
    assert reopened.view(contact_id="contact-a", session_id="session-sms")["total"] == 1
    reopened.observe(observation("sms", sequence=3, state="interrupted", phase="ended"), principal_id="host", contact_id="contact-a")
    assert not reopened.observe(observation("sms", sequence=99, phase="model"), principal_id="host", contact_id="contact-a")["accepted"]
    assert reopened.view(contact_id="owner", owner=True)["total"] == 1
    # Existing source evidence can coexist without a new store or DB rewind.
    reopened.ledger.record_source("source-a", contact_id="contact-a", session_id="session-sms", messages=[{"role": "user", "content": "A remembered fact."}])
    assert reopened.ledger.search_sources("remembered", contact_id="contact-a", session_id="session-sms")


def test_lease_expiry_is_unknown_not_completion_and_out_of_order_does_not_refresh(store):
    now = [1000.0]
    store.clock = lambda: now[0]
    store.observe(observation(), principal_id="host", contact_id="contact-a")
    now[0] += 180
    stale = store.view(contact_id="owner", owner=True)
    assert stale["items"][0]["liveness"] == "unknown"
    assert stale["items"][0]["observation_age_seconds"] == 180
    assert "unknown" in format_view(stale)
    assert store.observe(observation(), principal_id="host", contact_id="contact-a")["duplicate"]
    assert store.view(contact_id="owner", owner=True)["items"][0]["liveness"] == "unknown"
    store.observe(observation(sequence=2, phase="model"), principal_id="host", contact_id="contact-a")
    assert store.view(contact_id="owner", owner=True)["items"][0]["liveness"] == "recently_observed"


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('state,phase', [('observed', 'between_calls'), ('completed', 'ended'),
                                      ('interrupted', 'ended')])
def test_lost_observation_reply_can_be_replayed_without_rewriting_or_reopening(store, legacy, state, phase):
    from protagine.turns.idempotency import source_message_hash
    message = {'role': 'user', 'content': 'Perform the requested task.'}
    store.ledger.record_source('input', contact_id='contact-a', session_id='session-a',
        messages=[message], derive_claims=False)
    refs = [{'source_id': 'input', 'input_message_hash': source_message_hash('session-a', message)}]
    experience = {'task_id': 'f' * 64, 'purpose': 'operational', 'origin_platform': 'sms'}
    start = observation(input_refs=refs, task_experience=experience)
    store.observe(start, principal_id='host', contact_id='contact-a')
    sent = {**start, 'sequence': 2, 'state': state, 'phase': phase}
    store.observe(sent, principal_id='host', contact_id='contact-a')  # The HTTP reply is lost.
    with closing(store.ledger._connect()) as db, db:
        if legacy:
            row = db.execute('SELECT metadata_json FROM execution_runtime_observations').fetchone()
            metadata = json.loads(row[0])
            metadata.pop('observation_hash')
            db.execute('UPDATE execution_runtime_observations SET metadata_json=?', (json.dumps(metadata),))
        before = tuple(db.execute('SELECT * FROM execution_observations').fetchone())
        before_metadata = db.execute('SELECT metadata_json FROM execution_runtime_observations').fetchone()[0]
    reopened = ExecutionRegistry(TurnIdempotencyLedger(store.ledger.db_path))
    assert reopened.observe(sent, principal_id='host', contact_id='contact-a') == {
        'accepted': True, 'duplicate': True}
    for changed in ({'phase': 'model'}, {'state': 'failed'}, {'tool_name': 'changed'},
                    {'input_refs': [{**refs[0], 'source_id': 'different'}]},
                    {'task_experience': {**experience, 'purpose': 'qualification'}}):
        assert not reopened.observe({**sent, **changed}, principal_id='host', contact_id='contact-a')['accepted']
    with pytest.raises(ValueError, match='execution_scope_conflict'):
        reopened.observe(sent, principal_id='other-host', contact_id='contact-a')
    with closing(store.ledger._connect()) as db:
        assert tuple(db.execute('SELECT * FROM execution_observations').fetchone()) == before
        assert db.execute('SELECT metadata_json FROM execution_runtime_observations').fetchone()[0] == before_metadata
    assert not reopened.observe(start, principal_id='host', contact_id='contact-a')['accepted']
    if state != 'observed':
        assert not reopened.observe({**start, 'sequence': 3}, principal_id='host', contact_id='contact-a')['accepted']


def test_runtime_replay_requires_exact_recorded_payload_and_never_guesses_legacy_events(store):
    store.observe(observation(), principal_id='host', contact_id='contact-a')
    sent = observation(sequence=2, phase='model', runtime={
        'event': 'start', 'request_id': 'request-one', 'requested_model': 'processor-one'})
    store.observe(sent, principal_id='host', contact_id='contact-a')
    assert store.observe(sent, principal_id='host', contact_id='contact-a')['duplicate']
    changed = {**sent, 'runtime': {**sent['runtime'], 'requested_model': 'processor-two'}}
    assert not store.observe(changed, principal_id='host', contact_id='contact-a')['accepted']
    with closing(store.ledger._connect()) as db, db:
        metadata = json.loads(db.execute('SELECT metadata_json FROM execution_runtime_observations').fetchone()[0])
        metadata.pop('observation_hash')
        db.execute('UPDATE execution_runtime_observations SET metadata_json=?', (json.dumps(metadata),))
    assert not store.observe(sent, principal_id='host', contact_id='contact-a')['accepted']
    assert not store.observe({**sent, 'runtime': None}, principal_id='host', contact_id='contact-a')['accepted']


def test_parent_scope_and_writer_are_immutable(store):
    parent = observation()
    store.observe(parent, principal_id="host", contact_id="contact-a")
    child = observation("child", parent_execution_id=parent["execution_id"], platform="subagent")
    with pytest.raises(ValueError, match="parent_scope"):
        store.observe(child, principal_id="other-host", contact_id="contact-a")
    with pytest.raises(ValueError, match="parent_scope"):
        store.observe(child, principal_id="host", contact_id="contact-b")
    store.observe(child, principal_id="host", contact_id="contact-a")
    with pytest.raises(ValueError, match="execution_scope_conflict"):
        store.observe({**child, "sequence": 2}, principal_id="host", contact_id="contact-b")
    assert store.view(contact_id="contact-b", session_id=child["session_id"])["total"] == 0












@pytest.mark.asyncio
async def test_owner_context_observes_other_sessions_but_guest_context_omits_them(store, monkeypatch):
    from protagine.api.routers import host
    from protagine.api.schemas.host import ContextAssembleRequest
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", "owner")
    monkeypatch.setattr(host, "_p8_runtime", None)
    # Other producer behavior is outside this focused read-view test.
    monkeypatch.setattr(host, "_require_scoped_context_runtime_for_guest", lambda *a: None)
    store.observe(observation("voice"), principal_id="host", contact_id="owner")
    store.observe(observation("cron", platform="cron"), principal_id="host", contact_id="owner")
    for person in ("owner", "contact-a"):
        authority = RequestAuthority(principal_id="host-" + person, credential_id="key", scopes=frozenset({"context:read"}), viewer_person_id=person, person_ids=frozenset({person}), audiences=frozenset({"viewer"}), authenticated=True)
        request = SimpleNamespace(state=SimpleNamespace(protagine_authority=authority))
        body = ContextAssembleRequest(identity={"host_id": "test"}, context={"contact_id": person, "session_id": "new-chat"}, incoming_message={"role": "user", "content": "What are you doing now?"})
        response = await host.context_assemble(body, request)
        sections = [s for s in response.sections if s.id == "protagine-executions"]
        if person == "owner":
            assert len(sections) == 1
            assert "session-voice" in sections[0].body and "session-cron" in sections[0].body
        else:
            assert not sections




@pytest.mark.asyncio
async def test_request_endpoint_fetches_quiet_parent_before_eight_row_projection(store, monkeypatch):
    import json
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", "owner")
    monkeypatch.setattr(executions, "registry", lambda: store)
    now = [1000.0]
    store.clock = lambda: now[0]
    parent = observation("quiet-parent", contact_id="owner")
    store.observe(parent, principal_id="host", contact_id="owner")
    now[0] += 180
    for n in range(30):
        store.observe(observation("sibling-"+str(n), contact_id="owner",
            parent_execution_id=parent["execution_id"], platform="subagent"),
            principal_id="host", contact_id="owner")
        now[0] += 1
    selected = store.view(contact_id="owner", owner=True, limit=8)
    assert parent["execution_id"] not in {r["execution_id"] for r in selected["items"]}
    app = FastAPI()
    @app.middleware("http")
    async def auth(request, call_next):
        request.state.protagine_authority = RequestAuthority(principal_id="owner-host", credential_id="key",
            scopes=frozenset({"context:read"}), viewer_person_id="owner", person_ids=frozenset({"owner"}),
            audiences=frozenset({"owner"}), authenticated=True)
        return await call_next(request)
    app.include_router(executions.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.get('/v1/host/executions', params={
            'contact_id': 'owner', 'session_id': 'session-sibling-29', 'projection': 'request'})
    assert result.status_code == 200
    packet = result.json()
    rows = [json.loads(line) for line in packet['text'].splitlines() if line.startswith('{')]
    native = {row['execution_id']: row for row in rows if row['source'] == 'execution'}
    assert parent['execution_id'] in native and observation('sibling-29')['execution_id'] in native
    assert native[parent['execution_id']]['liveness'] == 'unknown'
    assert all(not row.get('parent_execution_id') or row['parent_execution_id'] in native for row in native.values())
    assert len(rows) <= 8 and len(packet['text']) <= 4000
    assert packet['truncated'] and not packet['complete']
    assert packet['work_sources']['execution']['total'] == 31


def test_ancestry_lookup_keeps_guest_session_and_terminal_selection_unchanged(store):
    parent = observation('parent')
    store.observe(parent, principal_id='host', contact_id='contact-a')
    child = observation('child', parent_execution_id=parent['execution_id'])
    store.observe(child, principal_id='host', contact_id='contact-a')
    guest = store.view(contact_id='contact-a', session_id='session-child', limit=1, include_ancestors=True)
    assert [row['execution_id'] for row in guest['items']] == [child['execution_id']]
    store.observe({**parent, 'state': 'completed', 'phase': 'ended', 'sequence': 2}, principal_id='host', contact_id='contact-a')
    owner = store.view(contact_id='owner', owner=True, limit=1, include_ancestors=True)
    assert owner['total'] == 1
    assert [row['execution_id'] for row in owner['items']] == [child['execution_id']]
