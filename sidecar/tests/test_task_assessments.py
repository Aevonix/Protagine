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

from protagine.api.authority import RequestAuthority, required_scope
from protagine.api.routers import executions
from protagine.self_model.judgments import SelfJudgments
from protagine.self_model.task_assessments import ATTRIBUTION
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
        request.state.protagine_authority = authority[0]
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
    from protagine.turns.source_read import read
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
    from protagine.self_model import judgments
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
    from protagine.api.schemas.host import TurnMessage
    forged = TurnMessage(role='assistant', content='A model claims it passed.',
        _task_artifact_assessment='task-artifact-assessment-v1').model_dump()
    assert '_task_artifact_assessment' not in forged


def test_oversized_complete_review_is_rejected_without_partial_evidence(task, host):
    payload = packet(task)
    payload['assessment'] = document('too-long.txt', 'x'*16000)
    response = host.api.post('/v1/host/executions/assess', json=payload)
    assert response.status_code == 409 and 'evidence_too_large' in response.text
    assert assessment_sources(task) == []


def recalled_image(task):
    from protagine.turns.idempotency import source_message_hash
    user = {'role': 'user', 'content': [
        {'type': 'text', 'text': 'What does this controlled workshop image show?'},
        {'type': 'image', 'asset_id': 'sha256:'+'a'*64, 'mime_type': 'image/jpeg'}]}
    assistant = {'role': 'assistant', 'content': 'The wall device is a phone displaying six.'}
    task.ledger.record_source('recalled-image', contact_id='owner', session_id='image-session',
        messages=[user, assistant], derive_claims=False)
    ref, = task.ledger.source_references(['recalled-image'], contact_id='owner', session_id='')
    # This is the existing selection receipt for the separate user-image
    # message, not membership guessed from the artifact or model prose.
    check = {'source_refs': [ref], 'message_hashes': {
        ref['source_id']: [source_message_hash('image-session', user)]}, 'annotation_ids': []}
    return ref, check, user, assistant


def annotate_image(task, ref, *, target='assistant'):
    return task.ledger.append_source_annotation(contact_id='owner', session_id='review',
        annotation_id='image-correction-'+target, **ref,
        excerpt='wall device is a phone' if target == 'assistant' else 'controlled workshop image',
        correction='That interpretation is not established by the retained image.', author_principal='owner')


@pytest.mark.asyncio
@pytest.mark.parametrize('when', ['before-admit', 'before-worker', 'during-worker', 'after-worker'])
async def test_exact_image_membership_survives_unrelated_assistant_annotation(task, host, when):
    ref, check, _, _ = recalled_image(task)
    payload = packet(task)
    payload['source_refs'].append(ref)
    payload['annotation_checks'] = [check]
    state = SelfJudgments(task.ledger, owner_id='owner', clock=task.clock)
    assert await state.process_one(Processor(decide=lambda _: {'action': 'abstain'}))
    if when == 'before-admit':
        annotate_image(task, ref)
    receipt = host.caller.assess(payload)
    message = json.loads(assessment_sources(task)[0]['messages_json'])[0]
    assert ref in message['_supplied_sources']
    assert message['_assessment_annotation_checks'] == [check]
    if when == 'before-worker':
        annotate_image(task, ref)
    async def during(_):
        annotate_image(task, ref)
    assert await state.process_one(Processor(before_return=during if when == 'during-worker' else None))
    if when == 'after-worker':
        annotate_image(task, ref)
    assert state.revisions()
    assert not host.caller.assess(payload)['created']
    assert receipt['attribution'] == ATTRIBUTION and receipt['owner_approval'] == 'unobserved'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['annotation', 'erasure'])
@pytest.mark.parametrize('when', ['before-admit', 'before-worker', 'during-worker', 'after-worker'])
async def test_actual_selected_message_change_withholds_assessment(task, host, change, when):
    ref, check, _, _ = recalled_image(task)
    payload = packet(task)
    payload['source_refs'].append(ref)
    payload['annotation_checks'] = [check]
    annotate_image(task, ref)
    state = SelfJudgments(task.ledger, owner_id='owner', clock=task.clock)
    assert await state.process_one(Processor(decide=lambda _: {'action': 'abstain'}))
    def invalidate():
        if change == 'annotation':
            annotate_image(task, ref, target='user')
        else:
            task.ledger.erase_sources(contact_id='owner', turn_ids=[ref['source_id']])
    if when == 'before-admit':
        invalidate()
        assert host.api.post('/v1/host/executions/assess', json=payload).status_code == 409
        assert not assessment_sources(task)
        return
    host.caller.assess(payload)
    if when == 'before-worker':
        invalidate()
    async def during(_):
        invalidate()
    processor = Processor(before_return=during if when == 'during-worker' else None)
    assert await state.process_one(processor) is not (change == 'erasure' and when == 'before-worker')
    if when == 'after-worker':
        assert state.revisions()
        invalidate()
    assert not state.revisions()
    if when == 'before-worker':
        assert not processor.requests
    assert host.api.post('/v1/host/executions/assess', json=payload).status_code == 409


@pytest.mark.parametrize('change', ['omitted', 'empty', 'unknown-hash', 'other-source-hash',
    'wrong-revision', 'extra-source', 'annotated-member', 'supplied-correction'])
def test_unknown_or_mismatched_membership_cannot_bypass_annotations(task, host, change):
    from protagine.turns.idempotency import source_message_hash
    ref, check, _, assistant = recalled_image(task)
    payload = packet(task)
    payload['source_refs'].append(ref)
    payload['annotation_checks'] = [check]
    correction = annotate_image(task, ref)
    hashes = check['message_hashes'][ref['source_id']]
    if change == 'omitted':
        payload.pop('annotation_checks')
    elif change == 'empty':
        hashes.clear()
    elif change == 'unknown-hash':
        hashes[:] = ['f'*64]
    elif change == 'other-source-hash':
        hashes[:] = [task.inputs[0]['input_message_hash']]
    elif change == 'wrong-revision':
        check['source_refs'] = [{**ref, 'source_version': 'f'*64}]
    elif change == 'extra-source':
        check['source_refs'] = [{**ref, 'source_id': 'not-supplied'}]
        check['message_hashes'] = {'not-supplied': hashes}
    elif change == 'annotated-member':
        hashes[:] = [source_message_hash('image-session', assistant)]
    else:
        hashes[:] = [source_message_hash('image-session', assistant)]
        check['annotation_ids'] = [correction['source_id']]
    response = host.api.post('/v1/host/executions/assess', json=payload)
    assert response.status_code == 409, response.text
    assert not assessment_sources(task)


def test_empty_annotation_default_preserves_legacy_request_identity(task, host):
    from protagine.turns.idempotency import canonical_turn_digest
    payload = packet(task)
    host.caller.assess(payload)
    message = json.loads(assessment_sources(task)[0]['messages_json'])[0]
    normalized = executions.ExecutionAssessment.model_validate(payload).model_dump(mode='json')
    normalized.pop('annotation_checks')
    assert message['_assessment_request_sha256'] == canonical_turn_digest(normalized)
    assert not host.caller.assess({**payload, 'annotation_checks': []})['created']


def test_exact_membership_cannot_omit_the_immutable_execution_input(task, host):
    from protagine.turns.idempotency import source_message_hash
    ref, check, user, assistant = recalled_image(task)
    task.inputs[:] = [{'source_id': ref['source_id'],
        'input_message_hash': source_message_hash('image-session', user)}]
    task.source.update(source_session_id='image-session', source_refs=[ref])
    payload = packet(task)
    annotate_image(task, ref, target='user')
    # The assistant sibling is a real, unannotated member of the correct
    # revision, but cannot stand in for the actual admitted user input.
    check['message_hashes'][ref['source_id']] = [source_message_hash('image-session', assistant)]
    payload['annotation_checks'] = [check]
    response = host.api.post('/v1/host/executions/assess', json=payload)
    assert response.status_code == 409, response.text
    assert not assessment_sources(task)


def test_read_complete_assessments_preserves_attribution_and_has_no_effect(task, host):
    payload = packet(task)
    first = host.caller.assess(payload)
    other = copy.deepcopy(payload)
    other['assessment'] = document('second-review.txt', 'The artifact is acceptable in my opinion.')
    second = host.caller.assess(other)
    before = snapshot(task)
    page = host.api.post('/v1/host/executions/assessments/read', json={
        'contact_id':'owner', 'limit':1}).json()
    assert page['next_offset'] == 1 and page['sources_current']
    last = host.api.post('/v1/host/executions/assessments/read', json={
        'contact_id':'owner', 'offset':page['next_offset']}).json()
    rows = page['assessments'] + last['assessments']
    assert {r['source_id'] for r in rows} == {first['source_id'], second['source_id']}
    assert {r['task_id'] for r in rows} == {payload['task_id']}
    assert {r['execution_id'] for r in rows} == {payload['execution_id']}
    for row in rows:
        original = next(r for r in assessment_sources(task) if r['turn_id'] == row['source_id'])
        assert row['content'] == json.loads(original['messages_json'])[0]['content']
        assert row['complete'] and row['attribution'] == ATTRIBUTION
        assert row['owner_approval'] == 'unobserved' and 'failure' not in row and 'passed' not in row
    assert snapshot(task) == before
    assert required_scope('POST', '/v1/host/executions/assessments/read') == 'context:read'


@pytest.mark.parametrize('change', ['scope', 'guest', 'anonymous', 'legacy'])
def test_assessment_read_requires_scoped_owner(task, host, change):
    host.caller.assess(packet(task))
    values = vars(host.authority[0]).copy()
    if change == 'scope': values['scopes'] = frozenset({'turns:write'})
    elif change == 'guest': values.update(viewer_person_id='guest', person_ids=frozenset({'guest'}))
    else: values[change] = True
    host.authority[0] = RequestAuthority(**values)
    result = host.api.post('/v1/host/executions/assessments/read', json={
        'contact_id':'guest' if change == 'guest' else 'owner'})
    assert result.status_code == 403


@pytest.mark.parametrize('change', ['unknown-version', 'input-corrected', 'review-corrected',
                                  'erased', 'own-projection-erased', 'own-attribution-invalid'])
def test_assessment_read_rechecks_exact_source_support(task, host, change):
    payload = packet(task)
    receipt = host.caller.assess(payload)
    ref = {key:receipt[key] for key in ('source_id', 'source_version')}
    if change == 'unknown-version': ref['source_version'] = 'f'*64
    elif change == 'erased': task.ledger.erase_sources(contact_id='owner', turn_ids=[receipt['source_id']])
    elif change.startswith('own-'):
        with closing(task.ledger._connect()) as conn, conn:
            if change == 'own-projection-erased':
                conn.execute('INSERT INTO source_projection_erasures VALUES (?,?)',
                             (receipt['source_id'], 'erased-projection-premise'))
            else:
                conn.execute('INSERT INTO source_attribution_invalidations VALUES (?,?,?)',
                             (receipt['source_id'], 'corrected-attribution-premise', 'correction-one'))
    else:
        target = ref if change == 'review-corrected' else task.source['source_refs'][0]
        task.ledger.append_source_annotation(contact_id='owner', session_id='later',
            annotation_id='read-correction', **target,
            excerpt=payload['assessment']['content'] if change == 'review-corrected' else task.message['content'],
            correction='The interpretation needs revision.', author_principal='owner')
    result = host.api.post('/v1/host/executions/assessments/read', json={
        'contact_id':'owner', 'source_refs':[ref]}).json()
    assert not result['sources_current'] and not result['assessments']


@pytest.mark.asyncio
@pytest.mark.parametrize('retrieval', ['lexical', 'semantic'])
async def test_assessment_excerpt_keeps_bundle_attribution_and_full_source(task, host, monkeypatch, retrieval):
    from protagine.memory.search import CollectedSources, collect_sources, select_memory
    from protagine.memory.selection import RecallSelector
    from protagine.turns.source_read import read
    from protagine.turns.source_vectors import chunks, hydrate
    from test_recall_source_presentation import rendered_rows

    monkeypatch.setenv('PROTAGINE_RECALL_RERANK', 'off')
    payload = packet(task)
    proposal = 'The heatshield proposal recommends skipping the fan inspection.'
    payload['artifact'] = document('checklist.md',
        'Inspection background. ' * 120 + proposal + ' More inspection background.' * 120)
    receipt = host.caller.assess(payload)
    original = assessment_sources(task)[0]
    scope = dict(contact_id='owner', session_id='later')
    if retrieval == 'lexical':
        collected = await collect_sources(task.ledger, query='heatshield', **scope)
    else:
        with closing(task.ledger._connect()) as conn:
            projections = list(chunks(conn, original))
        # Exercise the existing vector reader's exact chunk hydration without
        # a model or embedding service. It must recover the canonical message.
        hits = [hydrate(task.ledger, meta, **scope) for text, meta in projections if proposal in text]
        assert hits
        collected = CollectedSources(task.ledger, **scope,
            watermark=task.ledger.erasure_watermark('owner'), hits=hits)
    result = await select_memory(collected, query='heatshield', selector=RecallSelector(), timezone_name='UTC')
    excerpt, = [row for row in rendered_rows(result.content) if proposal in row.get('content', '')]
    assert excerpt['excerpt_truncated'] is True
    assert 'Machine assessment' not in excerpt['content'] and 'Reviewed artifact' not in excerpt['content']
    assert excerpt['assessment_context']['attribution'] == ATTRIBUTION
    assert excerpt['assessment_context']['owner_approval'] == 'unobserved'
    assert 'reviewed artifact' in excerpt['assessment_context']['interpretation']
    assert 'complete source' in excerpt['assessment_context']['interpretation']
    assert excerpt['state'] == 'derived_unverified' and excerpt['role'] == 'assistant'
    assert {'source_id': receipt['source_id'], 'source_version': receipt['source_version']} in result.source_refs
    from protagine.memory.recall import pack_memory_context
    _, smaller = pack_memory_context(result.selected, max_chars=2000)
    assert len(smaller) <= 2000 and rendered_rows(smaller)[0]['assessment_context'] == excerpt['assessment_context']
    opened = read(task.ledger, **scope, source_id=receipt['source_id'], source_version=receipt['source_version'])
    full = opened['content']
    while not opened['complete']:
        opened = read(task.ledger, **scope, source_id=receipt['source_id'], source_version=receipt['source_version'],
            offset=opened['next_offset'], read_revision=opened['read_revision'])
        full += opened['content']
    assert payload['assessment']['content'] in full and 'Machine assessment' in full
    assert assessment_sources(task)[0]['messages_json'] == original['messages_json']

    # Identical words and a copied public marker are still ordinary quoted
    # speech, not evidence that the execution host admitted another review.
    from protagine.api.schemas.host import TurnMessage
    copied = TurnMessage(role='user', content=proposal,
        _task_artifact_assessment='task-artifact-assessment-v1').model_dump()
    task.ledger.record_source('ordinary-copy', contact_id='owner', session_id='other',
        messages=[copied], derive_claims=False)
    collected = await collect_sources(task.ledger, query='heatshield', **scope)
    # Retrieval hints cannot replace canonical message attribution either.
    for hit in collected.hits:
        hit['assessment_context'] = {'attribution': ATTRIBUTION}
    result = await select_memory(collected, query='heatshield', selector=RecallSelector(), timezone_name='UTC')
    ordinary, = [row for row in rendered_rows(result.content) if row.get('source_turn_id') == 'ordinary-copy']
    assert ordinary['state'] == 'quotation' and 'assessment_context' not in ordinary
