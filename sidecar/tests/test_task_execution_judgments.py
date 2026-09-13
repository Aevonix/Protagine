"""Controlled native task callbacks -> source API -> existing judgment worker.

No model or service participates. Native adapter/store and lifecycle observer
are real; the processor is an explicit deterministic fixture, not value proof.
"""
from contextlib import closing, contextmanager
import importlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from pacomind.api.routers.executions import ExecutionObservation
from pacomind.self_model.execution_outcomes import evidence_text
from pacomind.self_model.judgments import SelfJudgments
from pacomind.turns import TurnIdempotencyLedger
from pacomind.turns.executions import ExecutionRegistry
from pacomind.turns.idempotency import source_message_hash
from test_hermes_general_governance import _load_plugin
from test_self_judgments import Processor, Clock


@pytest.fixture
def task(tmp_path, monkeypatch):
    monkeypatch.setenv('PACOMIND_OWNER_CONTACT_ID', 'owner')
    monkeypatch.setenv('PACOMIND_SELF_JUDGMENTS_ENABLED', '1')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path/'hermes'))
    plugin = _load_plugin('task_experience_integration')
    try:
        native = importlib.import_module(plugin.__name__ + '.native_task_platform')
    except ModuleNotFoundError as error:
        if error.name == 'gateway':
            pytest.skip('Qualified Hermes required for native adapter lifecycle')
        raise
    from gateway.config import PlatformConfig
    controller_module = importlib.import_module(plugin.__name__ + '.task_controller')
    observer_module = importlib.import_module(plugin.__name__ + '.executions')
    ledger = TurnIdempotencyLedger(tmp_path/'source.db')
    clock = Clock()
    registry = ExecutionRegistry(ledger, clock=clock)
    message = {'role': 'user', 'content': 'Inspect the fan maintenance notes and retain a checklist.'}
    ledger.record_source('original-instruction', contact_id='owner', session_id='operator',
                         messages=[message], derive_claims=False)
    inputs = [{'source_id': 'original-instruction', 'input_message_hash': source_message_hash('operator', message)}]
    source = {'version': 1, 'principal': 'hermes:cli', 'source_session_id': 'operator',
        'contact_id': 'owner', 'watermark': 0, 'input_refs': inputs,
        'source_refs': ledger.source_references(['original-instruction'], contact_id='owner', session_id=''),
        'origin': {'platform': 'cli', 'authority_gateway': 'cli', 'sender_id': '',
                   'session_id': 'operator', 'turn_id': 'operator-turn'}}
    @contextmanager
    def database():
        with closing(sqlite3.connect(tmp_path/'tasks.db')) as db, db:
            db.row_factory = sqlite3.Row
            yield db
    # Canonical source identity is frozen independently of any incoming extra
    # marker. The injected resolver stands in for authenticated admission only.
    sources = SimpleNamespace(resolve_source=lambda value, dependencies=None: dict(source),
        resolve_owner=lambda value, require_task_grant: 'owner')
    controller = controller_module.NativeTasks(None, None, 'owner', database=database, sources=sources)
    adapter = native.NativeTaskAdapter(PlatformConfig(enabled=True), handoffs=controller.handoffs,
                                       platform_name='api_server')
    controller.adapter = adapter
    sent = []
    class Client:
        def post(self, path, *, json, **kwargs):
            assert path == '/v1/host/executions/observe'
            body = ExecutionObservation(**json)
            sent.append(body.model_dump())
            registry.observe(body.model_dump(), principal_id='registered-host', contact_id=body.contact_id)
            return SimpleNamespace(raise_for_status=lambda: None)
    observer = observer_module.ExecutionObserver(Client())
    def run(*, purpose='operational', outcome='completed', name='first', begin_only=False):
        row = controller.handoffs.admit(request_id=name, request='MODEL-GENERATED TASK WRAPPER',
            source_input=source, experience=purpose)
        fields = {'platform': 'api_server', 'sender_id': 'owner', 'session_id': 'native-'+name,
                  'task_id': 'native-task-'+name, 'turn_id': 'native-turn-'+name}
        active = {'adapter': adapter, 'handoffs': controller.handoffs, 'id': row['id'], 'supplied': None}
        token = native.ACTIVE.set(active)
        try:
            controller.bind_native_turn(**fields)
            selected = controller.execution_experience(**fields)
            observer.start(SimpleNamespace(valid_participant=True, contact_id='owner', platform='api_server'),
                input_refs=inputs, task_experience=selected, **fields)
            if begin_only:
                return row, fields
            clock.value += 1
            metadata = dict(api_request_id='request-'+name, model='requested-alias', provider='local-role',
                api_mode='chat_completions', api_call_count=1, retry_count=0, approx_input_tokens=512,
                tool_count=3, request={'body': {'max_tokens': 256}})
            observer.api('start', **fields, **metadata)
            clock.value += 9
            observer.api('error' if outcome == 'failed' else 'response', **fields,
                         **metadata, response_model='' if outcome == 'failed' else 'reported-processor')
            clock.value += 2
            terminal = {**fields, 'completed': outcome == 'completed', 'failed': outcome == 'failed',
                'interrupted': outcome == 'interrupted', 'assistant_response': 'DONE. ALL OUTPUTS ARE CORRECT.'}
            controller.finish_native_turn(**terminal)
            observer.end(**terminal)
            return row, fields
        finally:
            native.ACTIVE.reset(token)
    return SimpleNamespace(ledger=ledger, registry=registry, clock=clock, run=run, source=source,
        inputs=inputs, message=message, controller=controller, sent=sent, observer=observer, native=native)


def retained(task):
    with closing(task.ledger._connect()) as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM turn_sources WHERE turn_id LIKE 'task-execution:%'")]


def counts(task):
    with closing(task.ledger._connect()) as conn:
        return {name: conn.execute('SELECT count(*) FROM '+name).fetchone()[0]
                for name in ('self_judgment_runs', 'source_claim_jobs', 'appraisal_runs')}


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['completed', 'failed', 'interrupted', 'ended'])
async def test_native_terminal_keeps_actual_request_and_unknown_quality(task, outcome):
    row, fields = task.run(outcome=outcome)
    receipt = task.controller.handoffs.get(row['id'])['terminal']
    assert receipt['basis'] == 'native_on_session_end'
    assert receipt['completed'] is (outcome == 'completed')
    sources = retained(task)
    assert len(sources) == 1
    messages = json.loads(sources[0]['messages_json'])
    assert len(messages) == 1 and messages[0]['role'] == 'assistant'
    assert messages[0]['_supplied_inputs'] == task.inputs
    assert messages[0]['content'] == ''
    facts = messages[0]['_task_execution_facts']
    text = evidence_text(facts)
    assert facts['state'] == outcome and facts['duration_seconds'] == 12
    assert facts['request_input']['excerpt'] == task.message['content']
    assert facts['request_input']['source_id'] == 'original-instruction'
    assert facts['task'] == {'task_id': row['id'], 'purpose': 'operational', 'origin_platform': 'cli'}
    assert facts['output_quality'] == facts['owner_approval'] == 'unobserved'
    assert 'MODEL-GENERATED' not in text and 'ALL OUTPUTS ARE CORRECT' not in text
    assert facts['processor']['configuration']['requested_model'] == 'requested-alias'
    assert facts['processor']['served_model'] == (None if outcome == 'failed' else 'reported-processor')
    assert facts['processor']['error_request_count'] == int(outcome == 'failed')
    assert 'callback pairs only' in facts['processor']['coverage']
    from pacomind.turns.source_vectors import chunks
    with closing(task.ledger._connect()) as conn:
        assert conn.execute('SELECT count(*) FROM turn_source_search WHERE turn_id=?',
                            (sources[0]['turn_id'],)).fetchone()[0] == 0
        assert list(chunks(conn, sources[0])) == []
    from pacomind.turns.source_read import read
    ref = task.ledger.source_references([sources[0]['turn_id']], contact_id='owner', session_id='later')[0]
    opened = read(task.ledger, contact_id='owner', session_id='later', **ref)
    assert opened['complete']
    assert json.loads(opened['content'])['messages'][0]['_task_execution_facts'] == facts
    assert counts(task) == {'self_judgment_runs': 1, 'source_claim_jobs': 0, 'appraisal_runs': 0}
    # Abstention is a valid result. A successful turn does not force a stance.
    processor = Processor(decide=lambda _: {'action': 'abstain'})
    state = SelfJudgments(task.ledger, owner_id='owner', clock=task.clock)
    assert await state.process_one(processor)
    assert processor.requests[0]['evidence'][0]['text'] == text
    assert processor.requests[0]['evidence'][0]['attribution'] == 'runtime_recorded_execution_metadata_not_output_verification'
    assert not state.revisions() and not await state.process_one(processor)


@pytest.mark.parametrize('purpose', ['qualification', None])
def test_qualification_and_unclassified_tasks_do_not_enter_learning(task, purpose):
    row, _ = task.run(purpose=purpose)
    assert task.controller.handoffs.get(row['id'])['terminal']['completed']
    task.registry.view(contact_id='owner', owner=True)
    assert retained(task) == [] and counts(task)['self_judgment_runs'] == 0
    first = task.sent[0]
    assert (first['task_experience'] or {}).get('purpose') == purpose
    with pytest.raises(ValueError, match='cannot be rebound'):
        task.controller.handoffs.admit(request_id='first', request='MODEL-GENERATED TASK WRAPPER',
            source_input=task.source, experience='operational')


def test_replay_after_queue_failure_uses_durable_terminal_once(task, monkeypatch):
    from pacomind.self_model import judgments
    original = judgments.enqueue
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('Controlled queue transaction failure')
    monkeypatch.setattr(judgments, 'enqueue', fail)
    task.run(outcome='failed')
    assert retained(task) == [] and task.sent[-1]['state'] == 'failed'
    monkeypatch.setattr(judgments, 'enqueue', original)
    reopened = ExecutionRegistry(TurnIdempotencyLedger(task.ledger.db_path), clock=task.clock)
    reopened.view(contact_id='owner', owner=True)
    first = retained(task)
    assert len(first) == 1 and counts(task)['self_judgment_runs'] == 1
    # Crash after canonical source+queue commit but before the optimization.
    with closing(task.ledger._connect()) as conn, conn:
        conn.execute("UPDATE execution_runtime_observations SET metadata_json=json_remove(metadata_json,'$.judgment_outcome_settled')")
    reopened.view(contact_id='owner', owner=True)
    assert retained(task) == first and counts(task)['self_judgment_runs'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('when', ['before_worker', 'during_worker', 'after_worker'])
async def test_request_correction_withholds_runtime_derived_view(task, when):
    task.run(outcome='failed')
    state = SelfJudgments(task.ledger, owner_id='owner', clock=task.clock)
    def annotate():
        task.ledger.append_source_annotation(contact_id='owner', session_id='later',
            annotation_id='correction', **task.source['source_refs'][0], excerpt=task.message['content'],
            correction='That inspection request was withdrawn before the run.', author_principal='owner')
    async def correction_during(_):
        annotate()
    if when == 'before_worker':
        annotate()
    processor = Processor(before_return=correction_during if when == 'during_worker' else None)
    assert await state.process_one(processor)
    if when == 'after_worker':
        assert state.revisions()
        annotate()
    assert not state.revisions()
    if when == 'before_worker':
        assert not processor.requests
    # The dated source and correction remain readable; no canonical rewrite.
    assert len(retained(task)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('erase', ['request', 'runtime'])
async def test_runtime_judgment_erasure_and_owner_withdrawal_survive_replay(task, erase):
    task.run(outcome='failed')
    state = SelfJudgments(task.ledger, owner_id='owner', clock=task.clock)
    assert await state.process_one(Processor()) and state.revisions()
    revision = state.revisions()[0]
    assert revision['authority_changed'] is False
    state.correct(revision['id'], action='withdraw', correction_id='owner-correction',
                  reason='A single interrupted task does not justify a general recommendation.')
    assert state.revisions() == []
    target = 'original-instruction' if erase == 'request' else retained(task)[0]['turn_id']
    task.ledger.erase_sources(contact_id='owner', turn_ids=[target])
    with closing(task.ledger._connect()) as conn, conn:
        conn.execute("UPDATE execution_runtime_observations SET metadata_json=json_remove(metadata_json,'$.judgment_outcome_settled')")
    task.registry.view(contact_id='owner', owner=True)
    assert retained(task) == []
    assert SelfJudgments(task.ledger, owner_id='owner').revisions() == []
    assert not await state.process_one(Processor())


def test_experience_cannot_arrive_late_or_mutate_at_terminal(task):
    task.run(begin_only=True)
    first = task.sent[0]
    for change in ({'purpose': 'qualification'}, {'task_id': 'b'*64}):
        with pytest.raises(ValueError, match='experience_binding_conflict'):
            task.registry.observe({**first, 'sequence': 2, 'task_experience': {**first['task_experience'], **change}},
                                  principal_id='registered-host', contact_id='owner')
    task.run(purpose=None, name='old', begin_only=True)
    old = task.sent[-1]
    with pytest.raises(ValueError, match='experience_binding_conflict'):
        task.registry.observe({**old, 'sequence': 2, 'task_experience': first['task_experience']},
                              principal_id='registered-host', contact_id='owner')


def test_missing_api_callbacks_remain_missing_evidence(task):
    _, fields = task.run(begin_only=True)
    task.clock.value += 5
    task.observer.end(**fields, completed=True)
    facts = json.loads(retained(task)[0]['messages_json'])[0]['_task_execution_facts']
    assert facts['processor']['observed_request_count'] == 0
    assert not facts['processor']['complete_observed_pairs']
    assert facts['processor']['served_model'] is None
    assert facts['output_quality'] == 'unobserved'


def test_model_tool_cannot_classify_experience_and_local_scope_is_not_assumed_ordinary(task):
    controller = task.controller
    controller.sources.capture = lambda scope: dict(task.source)
    controller.sources.attested_system_platforms = {'cli', 'local-operator'}
    controller.adapter.loop = SimpleNamespace(is_running=lambda: True)
    controller._call = lambda *args: {'native_admitted': True}
    remote = SimpleNamespace(valid_participant=True, contact_id='owner',
                             authority_lane='owner', resolution_status='resolved', platform='sms')
    task.source['origin']['platform'] = task.source['origin']['authority_gateway'] = 'sms'
    task.source['principal'] = 'hermes:sms'
    rejected = json.loads(controller.handle({'operation': 'submit', 'request': 'Inspect notes',
                                            'experience': 'operational'}, remote))
    assert 'exact fields' in rejected['error'] and controller.handoffs.count() == 0
    ordinary = json.loads(controller.handle({'operation': 'submit', 'request': 'Inspect notes'}, remote))
    assert ordinary['accepted']
    assert controller.handoffs.get(ordinary['task_id'])['source']['task_experience'] == 'operational'
    local = SimpleNamespace(valid_participant=True, contact_id='owner',
                            authority_lane='system', resolution_status='attested_system', platform='cli')
    task.source['origin']['platform'] = task.source['origin']['authority_gateway'] = 'cli'
    task.source['principal'] = 'hermes:cli'
    operator = json.loads(controller.handle({'operation': 'submit', 'request': 'Inspect another note'}, local))
    assert operator['accepted']
    assert 'task_experience' not in controller.handoffs.get(operator['task_id'])['source']
    # Even a CLI scope with owner permissions is not proof of ordinary work.
    local.authority_lane, local.resolution_status = 'owner', 'resolved'
    operator = json.loads(controller.handle({'operation': 'submit', 'request': 'Inspect final note'}, local))
    assert operator['accepted']
    assert 'task_experience' not in controller.handoffs.get(operator['task_id'])['source']


def test_bound_native_task_is_required_for_experience_metadata(task):
    row, fields = task.run(begin_only=True)
    assert task.controller.execution_experience(**fields) is None  # Outside actual native handler.
    token = task.native.ACTIVE.set({'adapter': task.controller.adapter, 'handoffs': task.controller.handoffs,
        'id': row['id'], 'supplied': None,
        'native': {key: fields[key] for key in ('session_id', 'task_id', 'turn_id')}})
    try:
        for mismatch in ({'turn_id': 'other'}, {'parent_session_id': fields['session_id']}, {'platform': 'background_review'}):
            assert task.controller.execution_experience(**{**fields, **mismatch}) is None
    finally:
        task.native.ACTIVE.reset(token)
