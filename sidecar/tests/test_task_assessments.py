"""Actual host admission -> scoped HTTP -> durable evidence -> judgment worker.

Artifacts, authority and review decisions are explicit controlled fixtures.
No model, live owner evidence or useful-quality result is claimed here.
"""
from contextlib import closing
import copy
import hashlib
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from pacomind.api.authority import RequestAuthority, required_scope
from pacomind.api.routers import executions
from pacomind.self_model.judgments import SelfJudgments
from pacomind.self_model.task_assessments import ATTRIBUTION
from test_self_judgments import Processor
from test_task_execution_judgments import task, retained, counts


def document(name, text):
    return {'name': name, 'content': text, 'sha256': hashlib.sha256(text.encode()).hexdigest()}


def packet(task, *, purpose='operational', begin_only=False):
    row, fields = task.run(purpose=purpose, begin_only=begin_only)
    event = task.sent[0]
    runtime = retained(task)
    runtime_ref = (task.ledger.source_references([runtime[0]['turn_id']], contact_id='owner',
        session_id='')[0] if runtime else {'source_id': 'task-execution:'+hashlib.sha256(
            event['execution_id'].encode()).hexdigest(), 'source_version': '0'*64})
    return {'execution_id': event['execution_id'], 'task_id': row['id'], 'contact_id': 'owner',
        'session_id': fields['session_id'], 'turn_id': fields['turn_id'], 'input_refs': task.inputs,
        'runtime_source_ref': runtime_ref, 'source_refs': task.source['source_refs'],
        'assessed_at': '2027-01-15T12:01:00+00:00',
        'reviewer_identity': 'controlled-independent-reviewer', 'reviewer_model': 'unknown',
        'artifact': document('checklist.md', 'Six of eight fans passed. All eight passed, so defer checks.'),
        'assessment': document('first-review.txt', 'The checklist contradicts its own six-of-eight count. '
            'The recommendation to defer checks is not supported by that count.'),
        'context_documents': [document('original-inspection.txt', 'Six of eight inspected fans passed.') ]}


def snapshot(task):
    with closing(task.ledger._connect()) as db:
        return {table: [tuple(row) for row in db.execute('SELECT * FROM '+table)] for table in
            ('execution_observations', 'execution_runtime_observations', 'self_judgment_runs')}


def assessment_sources(task):
    with closing(task.ledger._connect()) as db:
        return [dict(row) for row in db.execute("SELECT * FROM turn_sources WHERE turn_id LIKE 'task-artifact-assessment:%'")]


@pytest.fixture
def host(task, monkeypatch):
    authority = [RequestAuthority(principal_id='registered-host', credential_id='test',
        scopes=frozenset({'turns:write', 'context:read'}), viewer_person_id='owner',
        person_ids=frozenset({'owner'}), audiences=frozenset({'owner'}), authenticated=True)]
    app = FastAPI()
    @app.middleware('http')
    async def auth(request, call_next):
        request.state.pacomind_authority = authority[0]
        return await call_next(request)
    app.include_router(executions.router)
    monkeypatch.setattr(executions, 'registry', lambda: task.registry)
    with TestClient(app, raise_server_exceptions=False) as api:
        class Client:
            def post(self, path, *, json, **kwargs):
                assert path == '/v1/host/executions/assess'
                assert kwargs['timeout'] == 5 and kwargs['_deadline_monotonic'] > 0
                return api.post(path, json=json)
        caller = type(task.observer)(Client())
        yield SimpleNamespace(api=api, caller=caller, authority=authority)


@pytest.mark.asyncio
async def test_host_caller_admits_one_review_without_rewriting_execution_or_abstention(task, host):
    payload = packet(task)
    state = SelfJudgments(task.ledger, owner_id='owner', clock=task.clock)
    assert await state.process_one(Processor(decide=lambda _: {'action': 'abstain'}))
    before = snapshot(task)
    result = host.caller.assess(payload)
    assert result['created'] and result['attribution'] == ATTRIBUTION
    assert result['owner_approval'] == 'unobserved'
    source = assessment_sources(task)[0]
    message = json.loads(source['messages_json'])[0]
    assert message['role'] == 'assistant' and message['_supplied_inputs'] == task.inputs
    assert set((r['source_id'], r['source_version']) for r in message['_supplied_sources']) == {
        (r['source_id'], r['source_version']) for r in [*payload['source_refs'], payload['runtime_source_ref']]}
    for doc in [payload['artifact'], payload['assessment'], *payload['context_documents']]:
        assert doc['content'] in message['content'] and doc['sha256'] in message['content']
    assert 'not an owner statement' in message['content']
    assert not host.caller.assess(payload)['created']
    after = snapshot(task)
    assert after['execution_observations'] == before['execution_observations']
    assert after['execution_runtime_observations'] == before['execution_runtime_observations']
    assert after['self_judgment_runs'][0] == before['self_judgment_runs'][0]
    assert counts(task) == {'self_judgment_runs': 2, 'source_claim_jobs': 0, 'appraisal_runs': 0}
    processor = Processor()
    assert await state.process_one(processor)
    assert processor.requests[0]['evidence'][0]['attribution'] == ATTRIBUTION
    assert processor.requests[0]['evidence'][0]['text'] == message['content']
    assert state.revisions()[0]['premise_basis'] == ATTRIBUTION
    assert 'owner approval unobserved' in state.brief('local work checkpoints')
    from pacomind.turns.source_read import read
    opened = read(task.ledger, contact_id='owner', session_id='later',
        source_id=result['source_id'], source_version=result['source_version'])
    assert opened['complete'] and payload['assessment']['content'] in opened['content']
    assert not await state.process_one(processor)


@pytest.mark.parametrize('change', ['scope', 'guest', 'legacy', 'anonymous', 'different-principal'])
def test_assessment_admission_uses_existing_owner_and_writer_authority(task, host, change):
    payload = packet(task)
    values = vars(host.authority[0]).copy()
    if change == 'scope':
        values['scopes'] = frozenset({'context:read'})
    elif change == 'guest':
        values.update(viewer_person_id='guest', person_ids=frozenset({'guest'}))
    elif change == 'different-principal':
        values['principal_id'] = 'another-owner-host'
    else:
        values[change] = True
    host.authority[0] = RequestAuthority(**values)
    response = host.api.post('/v1/host/executions/assess', json=payload)
    assert response.status_code == (409 if change == 'different-principal' else 403)
    assert assessment_sources(task) == []
    assert required_scope('POST', '/v1/host/executions/assess') == 'turns:write'


@pytest.mark.parametrize('change', ['task_id', 'execution_id', 'session_id', 'turn_id',
    'input_refs', 'runtime_source_ref', 'artifact', 'assessment', 'source_refs'])
def test_exact_task_run_documents_and_sources_cannot_be_rebound(task, host, change):
    payload = copy.deepcopy(packet(task))
    if change in ('task_id', 'execution_id'):
        payload[change] = 'f'*64
    elif change in ('session_id', 'turn_id'):
        payload[change] = 'another-run'
    elif change == 'input_refs':
        payload[change][0]['input_message_hash'] = 'f'*64
    elif change == 'runtime_source_ref':
        payload[change]['source_id'] = 'unrelated-source'
    elif change == 'source_refs':
        payload[change][0]['source_version'] = 'f'*64
    else:
        payload[change]['content'] += ' unrecorded change'
    response = host.api.post('/v1/host/executions/assess', json=payload)
    assert response.status_code == 409, response.text
    assert assessment_sources(task) == []


@pytest.mark.parametrize('kind', ['qualification', 'unknown-purpose', 'running'])
def test_only_prospectively_operational_terminal_work_accepts_assessments(task, host, kind):
    payload = packet(task, purpose=None if kind == 'unknown-purpose' else
        'qualification' if kind == 'qualification' else 'operational', begin_only=kind == 'running')
    assert host.api.post('/v1/host/executions/assess', json=payload).status_code == 409
    assert assessment_sources(task) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('when', ['before-admit', 'before-worker', 'during-worker', 'after-worker'])
async def test_original_input_correction_withholds_machine_derived_judgment(task, host, when):
    payload = packet(task)
    state = SelfJudgments(task.ledger, owner_id='owner', clock=task.clock)
    assert await state.process_one(Processor(decide=lambda _: {'action': 'abstain'}))
    def correct():
        task.ledger.append_source_annotation(contact_id='owner', session_id='later',
            annotation_id='actual-owner-correction-fixture', **task.source['source_refs'][0],
            excerpt=task.message['content'], correction='That task referred to a different inspection.',
            author_principal='owner')
    if when == 'before-admit':
        correct()
        assert host.api.post('/v1/host/executions/assess', json=payload).status_code == 409
        assert assessment_sources(task) == []
        return
    host.caller.assess(payload)
    if when == 'before-worker':
        correct()
    async def during(_):
        correct()
    processor = Processor(before_return=during if when == 'during-worker' else None)
    assert await state.process_one(processor)
    if when == 'after-worker':
        assert state.revisions()
        correct()
    assert not state.revisions()
    if when == 'before-worker':
        assert not processor.requests
    assert host.api.post('/v1/host/executions/assess', json=payload).status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize('target', ['input', 'runtime', 'assessment', 'supplied-context'])
async def test_erasure_removes_assessment_and_derived_view_without_resurrection(task, host, target):
    payload = packet(task)
    task.ledger.record_source('context', contact_id='owner', session_id='context',
        messages=[{'role':'user', 'content':'Inspection context from the controlled fixture.'}], derive_claims=False)
    payload['source_refs'].extend(task.ledger.source_references(['context'], contact_id='owner', session_id=''))
    task.ledger.record_source('unrelated', contact_id='owner', session_id='unrelated',
        messages=[{'role':'user', 'content':'Keep the independent calendar request.'}], derive_claims=False)
    state = SelfJudgments(task.ledger, owner_id='owner', clock=task.clock)
    assert await state.process_one(Processor(decide=lambda _: {'action': 'abstain'}))
    receipt = host.caller.assess(payload)
    assert await state.process_one(Processor()) and state.revisions()
    erase_id = {'input':'original-instruction', 'runtime':payload['runtime_source_ref']['source_id'],
        'assessment':receipt['source_id'], 'supplied-context':'context'}[target]
    task.ledger.erase_sources(contact_id='owner', turn_ids=[erase_id])
    assert not state.revisions()
    assert not assessment_sources(task)
    assert host.api.post('/v1/host/executions/assess', json=payload).status_code == 409
    assert task.ledger.source_references(['unrelated'], contact_id='owner', session_id='')


@pytest.mark.asyncio
async def test_review_itself_can_be_corrected_and_owner_can_withdraw_its_view(task, host):
    payload = packet(task)
    state = SelfJudgments(task.ledger, owner_id='owner', clock=task.clock)
    assert await state.process_one(Processor(decide=lambda _: {'action': 'abstain'}))
    receipt = host.caller.assess(payload)
    assert await state.process_one(Processor())
    revision = state.revisions()[0]
    task.ledger.append_source_annotation(contact_id='owner', session_id='later',
        annotation_id='review-correction', source_id=receipt['source_id'], source_version=receipt['source_version'],
        excerpt=payload['assessment']['content'], correction='The reviewer omitted relevant context.',
        author_principal='owner')
    assert not state.revisions()
    state.correct(revision['id'], action='withdraw', correction_id='actual-control-fixture',
        reason='Do not apply this interpretation to later inspections.')
    assert state.revisions(history=True)[0]['status'] == 'withdrawn'
    assert not host.api.post('/v1/host/executions/assess', json=payload).is_success


def test_failed_source_queue_transaction_can_retry_without_a_duplicate_review(task, host, monkeypatch):
    import sqlite3
    from pacomind.self_model import judgments
    payload = packet(task)
    original = judgments.enqueue
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError('Controlled queue failure')
    monkeypatch.setattr(judgments, 'enqueue', unavailable)
    assert host.api.post('/v1/host/executions/assess', json=payload).status_code == 500
    assert assessment_sources(task) == []
    monkeypatch.setattr(judgments, 'enqueue', original)
    assert host.caller.assess(payload)['created']
    assert not host.caller.assess(payload)['created']
    assert len(assessment_sources(task)) == 1 and counts(task)['self_judgment_runs'] == 2


def test_assessment_identity_cannot_change_reviewer_or_drop_dependencies(task, host):
    payload = packet(task)
    host.caller.assess(payload)
    changed = {**payload, 'reviewer_model':'new-unobserved-identity'}
    assert host.api.post('/v1/host/executions/assess', json=changed).status_code == 409
    assert host.api.post('/v1/host/executions/assess', json={**payload, 'owner_approval':'approved'}).status_code == 422
    assert len(assessment_sources(task)) == 1
    from pacomind.api.schemas.host import TurnMessage
    forged = TurnMessage(role='assistant', content='A model claims it passed.',
        _task_artifact_assessment='task-artifact-assessment-v1').model_dump()
    assert '_task_artifact_assessment' not in forged


def test_oversized_complete_review_is_rejected_without_partial_evidence(task, host):
    payload = packet(task)
    payload['assessment'] = document('too-long.txt', 'x'*16000)
    response = host.api.post('/v1/host/executions/assess', json=payload)
    assert response.status_code == 409 and 'evidence_too_large' in response.text
    assert assessment_sources(task) == []
