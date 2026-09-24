"""Owner-reported outcomes in the existing appraisal call, the affect read API, the claim status
and erasure as deletes (build plan M6; architecture 4.3 "owner correction" and "existing appraisal
records"). Outcomes are counted occurrences, not votes: "the export failed twice" is two entries."""
import json
from datetime import datetime, timezone

import pytest

from protagine.self_model import appraisals as module
from protagine.turns import TurnIdempotencyLedger
from test_source_appraisals import Processor, observation, resolved, source, state, view  # noqa: F401


def reported(payload, event='failed', topic='quarterly figures', approach='the archive export'):
    evidence = next(e for e in payload['evidence'] if e['current'])
    return {'event': event, 'topic': topic, 'approach': approach,
            'support': [{'handle': evidence['handle'], 'quote': evidence['text']}]}


def answer(*outcomes, observations=()):
    """A processor answer carrying ``outcomes`` (each a function of the payload)."""
    def decide(payload):
        return {'observations': [o(payload) for o in observations],
                'incident_decisions': [{'record_id': i, 'outcome': 'unchanged'} for i in payload['incident_ids']],
                'outcomes': [o(payload) for o in outcomes]}
    return decide


def outcomes_of(store):
    with store.ledger._connect() as conn:
        return [dict(r) for r in conn.execute('SELECT * FROM appraisal_outcomes ORDER BY occurred_at,id')]


def run_of(store, turn_id):
    with store.ledger._connect() as conn:
        row = conn.execute('SELECT * FROM appraisal_runs WHERE turn_id=?', (turn_id,)).fetchone()
    return dict(row) if row else None


def owner_turn(store, name, text):
    source(store, name, text, contact='owner')


# -- the schema and the prompt ------------------------------------------------------------

def test_schema_requires_a_bounded_outcomes_array_for_strict_routers():
    schema = module.RESPONSE_SCHEMA['schema']
    assert schema['required'] == ['observations', 'incident_decisions', 'outcomes']
    outcomes = schema['properties']['outcomes']
    assert outcomes['type'] == 'array' and outcomes['maxItems'] == 4
    item = outcomes['items']
    assert item['additionalProperties'] is False
    assert item['required'] == ['event', 'topic', 'approach', 'support']
    assert item['properties']['event']['enum'] == list(module.OUTCOME_EVENTS) == [
        'failed', 'succeeded', 'dismissed', 'corrected']
    assert (item['properties']['topic']['minLength'], item['properties']['topic']['maxLength']) == (1, 80)
    assert item['properties']['approach']['maxLength'] == 80 and 'minLength' not in item['properties']['approach']
    support = item['properties']['support']
    assert (support['minItems'], support['maxItems']) == (1, 2)


def test_prompt_counts_occurrences_not_restatements():
    system = ' '.join(module.SYSTEM.split())
    assert 'observations, incident_decisions and outcomes' in system
    assert 'warrant no observation' in system and 'warrant no social record' not in system
    for phrase in ('outcomes lists how pieces of work went', 'two nudges waved off are two entries',
                   '"that is twice now" adds only the new one', 'even on the topic of a supplied incident',
                   'reuse a topic from recent_outcomes', 'Never record plans, predictions or hypotheticals'):
        assert phrase in system, phrase


# -- validation ----------------------------------------------------------------------------

def prepared(store, name='report', text='The archive export gave stale quarterly figures again.'):
    owner_turn(store, name, text)
    job = store._claim()
    return job, store._prepare(job)[1]


def test_validate_returns_items_and_outcomes_and_accepts_a_missing_key(state):
    _, payload = prepared(state)
    raw = json.dumps({'observations': [observation(payload)], 'incident_decisions': [],
                      'outcomes': [reported(payload), reported(payload)]})
    items, outcomes = state._validate(raw, payload)
    assert len(items) == 1 and len(outcomes) == 2
    first, deps = outcomes[0]
    assert first == {'event': 'failed', 'topic': 'quarterly figures', 'approach': 'the archive export'}
    assert deps[0]['message_hash'] == payload['evidence'][0]['handle']
    items, outcomes = state._validate(json.dumps({'observations': [], 'incident_decisions': []}), payload)
    assert items == [] and outcomes == []


@pytest.mark.parametrize('change', ['unknown_event', 'extra_key', 'missing_key', 'empty_topic', 'long_topic',
                                    'long_approach', 'no_support', 'three_citations', 'fabricated_quote',
                                    'prior_citation', 'too_many', 'not_a_list', 'unknown_top_level'])
def test_validate_rejects_malformed_outcomes(state, change):
    _, first = prepared(state, 'first', 'The export stalled at validation.')
    state._commit({'turn_id': 'first'}, state._prepare({'turn_id': 'first'})[0], *_one_incident(state, first))
    _, payload = prepared(state)
    prior = next((e for e in payload['evidence'] if not e['current']), None)
    item = reported(payload)
    value = {'observations': [], 'incident_decisions': [
        {'record_id': i, 'outcome': 'unchanged'} for i in payload['incident_ids']], 'outcomes': [item]}
    if change == 'unknown_event':
        item['event'] = 'annoyed'
    elif change == 'extra_key':
        item['reason'] = 'it broke'
    elif change == 'missing_key':
        del item['approach']
    elif change == 'empty_topic':
        item['topic'] = '  '
    elif change == 'long_topic':
        item['topic'] = 'x' * 81
    elif change == 'long_approach':
        item['approach'] = 'y' * 81
    elif change == 'no_support':
        item['support'] = []
    elif change == 'three_citations':
        item['support'] = item['support'] * 3
    elif change == 'fabricated_quote':
        item['support'][0]['quote'] = 'The owner said the export always fails.'
    elif change == 'prior_citation':
        assert prior is not None
        item['support'] = [{'handle': prior['handle'], 'quote': prior['quotes'][0]}]
    elif change == 'too_many':
        value['outcomes'] = [item] * 5
    elif change == 'not_a_list':
        value['outcomes'] = item
    else:
        value['mood'] = []
    with pytest.raises(ValueError):
        state._validate(json.dumps(value), payload)


def _one_incident(store, payload):
    """(items, outcomes, heads, processor) for a direct ``_commit`` of one frustration record."""
    items, _ = store._validate(json.dumps({'observations': [observation(payload)], 'incident_decisions': []}), payload)
    return items, [], {}, {'model_id': 'fixture', 'task': 'source_appraisal'}


# -- storage: owner only, once per turn ---------------------------------------------------

@pytest.mark.asyncio
async def test_owner_outcomes_are_stored_once_per_turn_with_their_source_time(state):
    owner_turn(state, 'report', 'The archive export gave stale quarterly figures twice today.')
    processor = Processor(answer(reported, reported))
    assert await state.process_one(processor)
    rows = outcomes_of(state)
    assert len(rows) == 2 and len({r['id'] for r in rows}) == 2
    assert all(r['id'].startswith('outcome:') for r in rows)
    handle = processor.requests[0]['evidence'][0]['handle']
    for row in rows:
        assert {k: row[k] for k in ('owner_id', 'subject_id', 'turn_id', 'message_hash', 'event', 'topic',
                                    'approach')} == {
            'owner_id': 'owner', 'subject_id': 'owner', 'turn_id': 'report', 'message_hash': handle,
            'event': 'failed', 'topic': 'quarterly figures', 'approach': 'the archive export'}
        assert row['occurred_at'] == state.test_clock.value and row['created_at'] == state.clock()
    assert run_of(state, 'report')['disposition'] == 'abstained'
    # A retry of the same turn (whatever it returns) never adds a second set.
    with state.ledger._connect() as conn, conn:
        conn.execute("UPDATE appraisal_runs SET status='pending' WHERE turn_id='report'")
    assert await state.process_one(Processor(answer(reported, reported, reported)))
    assert len(outcomes_of(state)) == 2


@pytest.mark.asyncio
async def test_a_contacts_turn_moves_no_outcome(state):
    source(state, 'contact-report', 'The archive export gave stale figures again.', contact='person')
    assert await state.process_one(Processor(answer(reported)))
    assert outcomes_of(state) == []
    assert state.affect_events(since=0) == []


@pytest.mark.asyncio
async def test_outcomes_land_even_when_observations_stay_empty_and_topics_are_normalized(state):
    owner_turn(state, 'report', 'I waved off the stretching reminder again.')
    assert await state.process_one(Processor(answer(
        lambda p: reported(p, event='dismissed', topic=' stretching_reminder ', approach='  a  nudge '))))
    row, = outcomes_of(state)
    assert (row['event'], row['topic'], row['approach']) == ('dismissed', 'stretching reminder', 'a nudge')


# -- the affect read API --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_affect_events_merge_outcomes_and_records_with_the_agreed_keys(state):
    owner_turn(state, 'incident', 'The export stalled at the validation step.')
    await state.process_one(Processor(answer(reported, observations=[
        lambda p: observation(p, topic='export task', dimension='frustration')])))
    state.test_clock.value += 60
    owner_turn(state, 'repair', 'The new diagnostic fixed the export and its output opened.')
    await state.process_one(Processor(lambda p: {'observations': [], 'outcomes': [
        reported(p, event='succeeded', topic='export task', approach='the new diagnostic')],
        'incident_decisions': [resolved(p, i) for i in p['incident_ids']]}))
    state.test_clock.value += 60
    source(state, 'other', 'The garden plan has a new planting date.', contact='person')
    await state.process_one(Processor(answer(reported)))
    events = state.affect_events(since=0)
    keys = {'ref', 'kind', 'topic', 'approach', 'dimension', 'intensity', 'turn_id', 'occurred_at', 'created_at'}
    assert all(set(e) == keys for e in events)
    assert [(e['occurred_at'], e['ref']) for e in events] == sorted((e['occurred_at'], e['ref']) for e in events)
    by_kind = {e['kind']: e for e in events}
    assert set(by_kind) == {'failed', 'appraisal', 'succeeded', 'resolved'}
    assert by_kind['failed'] == {**by_kind['failed'], 'topic': 'quarterly figures', 'approach': 'the archive export',
                                 'dimension': '', 'intensity': '', 'turn_id': 'incident'}
    assert by_kind['failed']['ref'].startswith('outcome:')
    appraisal = by_kind['appraisal']
    assert appraisal['ref'].startswith('appraisal:') and appraisal['topic'] == 'export task'
    assert (appraisal['dimension'], appraisal['intensity'], appraisal['approach']) == ('frustration', 'moderate', '')
    assert by_kind['resolved']['turn_id'] == 'repair' and by_kind['resolved']['dimension'] == 'satisfaction'
    assert all(e['turn_id'] != 'other' for e in events)                     # owner subject only
    assert state.affect_events(since=state.test_clock.value - 60) == [
        e for e in events if e['occurred_at'] >= state.test_clock.value - 60]
    assert state.affect_events(since=0, limit=2) == events[:2]
    # A withdrawn record is not the agent's appraisal any more.
    state.correct(appraisal['ref'], action='withdraw', correction_id='wrong', reason='Not my frustration.',
                  actor_id='owner')
    assert appraisal['ref'] not in {e['ref'] for e in state.affect_events(since=0)}


def test_affect_events_without_an_owner_are_empty(tmp_path):
    store = module.AppraisalStore(TurnIdempotencyLedger(tmp_path / 'sources.db'), owner_id='')
    assert store.affect_events(since=0) == [] and store.pending_jobs() == {'pending': 0, 'running': 0}


def test_pending_jobs_count_due_pending_and_running_per_contact(state):
    owner_turn(state, 'a', 'The export failed.')
    owner_turn(state, 'b', 'The export failed once more.')
    source(state, 'c', 'The garden plan changed.', contact='person')
    assert state.pending_jobs() == {'pending': 3, 'running': 0}
    assert state.pending_jobs(contact_id='owner') == {'pending': 2, 'running': 0}
    assert state._claim()['turn_id'] == 'a'
    assert state.pending_jobs(contact_id='owner') == {'pending': 1, 'running': 1}
    assert state.pending_jobs(contact_id='person') == {'pending': 1, 'running': 0}
    with state.ledger._connect() as conn, conn:
        conn.execute("UPDATE appraisal_runs SET next_attempt=? WHERE turn_id='b'", (state.clock() + 60,))
    assert state.pending_jobs(contact_id='owner') == {'pending': 0, 'running': 1}


@pytest.mark.asyncio
async def test_recent_outcomes_reach_the_next_owner_payload_only(state):
    for index in range(10):
        owner_turn(state, f'report-{index}', f'The export attempt {index} failed.')
        await state.process_one(Processor(answer(lambda p, i=index: reported(p, topic=f'export {i}'))))
        state.test_clock.value += 60
    owner_turn(state, 'next', 'The export failed again.')
    processor = Processor(answer())
    await state.process_one(processor)
    recent = processor.requests[0]['recent_outcomes']
    assert len(recent) == 8 and recent[0] == {'event': 'failed', 'topic': 'export 9', 'approach': 'the archive export'}
    assert all(set(item) == {'event', 'topic', 'approach'} for item in recent)
    state.test_clock.value += 86400 + 1
    owner_turn(state, 'tomorrow', 'The export failed on a new day.')
    later = Processor(answer())
    await state.process_one(later)
    assert later.requests[0]['recent_outcomes'] == []
    source(state, 'contact', 'The export failed for me as well.', contact='person')
    other = Processor(answer())
    state.test_clock.value -= 86400
    await state.process_one(other)
    assert other.requests[0]['recent_outcomes'] == []


# -- erasure and attribution delete ---------------------------------------------------------

@pytest.mark.asyncio
async def test_erasing_the_owner_turn_deletes_its_outcomes(state):
    owner_turn(state, 'report', 'The archive export gave stale figures again.')
    await state.process_one(Processor(answer(reported)))
    owner_turn(state, 'kept', 'The archive export failed on the next try too.')
    await state.process_one(Processor(answer(reported)))
    assert len(outcomes_of(state)) == 2
    state.ledger.erase_sources(contact_id='owner', turn_ids=['report'])
    assert [r['turn_id'] for r in outcomes_of(state)] == ['kept']
    assert [e['turn_id'] for e in state.affect_events(since=0)] == ['kept']


@pytest.mark.asyncio
async def test_attribution_change_deletes_outcomes_and_records_and_completes_the_run(state):
    owner_turn(state, 'report', 'The archive export gave stale figures again.')
    await state.process_one(Processor(answer(reported, observations=[lambda p: observation(p)])))
    owner_turn(state, 'pending', 'The export failed again.')
    with state.ledger._connect() as conn, conn:
        assert module.invalidate_source_attribution(conn, ['report', 'pending'], 'owner', 'person') == 1
        assert conn.execute('SELECT count(*) FROM appraisal_records').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM appraisal_heads').fetchone()[0] == 0
    assert outcomes_of(state) == []
    assert (run_of(state, 'pending')['status'], run_of(state, 'pending')['disposition']) == (
        'complete', 'attribution_changed')


@pytest.mark.asyncio
async def test_erasure_deletes_records_heads_and_corrections_instead_of_tombstones(state):
    source(state, 'first', 'The export has failed again.')
    await state.process_one(Processor())
    record = view(state)['records'][0]
    state.correct(record['id'], action='reconsider', correction_id='look-again', reason='Check the cause.',
                  actor_id='owner')
    state.ledger.erase_sources(contact_id='person', turn_ids=['first'])
    with state.ledger._connect() as conn:
        for table in ('appraisal_records', 'appraisal_heads', 'appraisal_corrections'):
            assert conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0, table


def test_the_shell_methods_are_gone():
    assert not hasattr(module.AppraisalStore, 'purge_erased_sources')
    assert not hasattr(module.AppraisalStore, 'invalidate_subject')


# -- migration and the claim status -----------------------------------------------------------

@pytest.mark.asyncio
async def test_initialize_removes_old_tombstones_with_their_heads_and_corrections(state):
    source(state, 'first', 'The export has failed again.')
    await state.process_one(Processor())
    live = view(state)['records'][0]['id']
    with state.ledger._connect() as conn, conn:
        for identifier, status in (('appraisal:old-erased', 'erased'), ('appraisal:old-invalidated', 'invalidated')):
            conn.execute('INSERT INTO appraisal_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         (identifier, 'owner', 'person', 'key-' + status, 'appraisal', '{}', '[]', '{}', 'gone',
                          'v', 1.0, None, status, None))
            conn.execute('INSERT INTO appraisal_heads VALUES (?,?,?,?)', ('owner', 'person', 'key-' + status, identifier))
            conn.execute('INSERT INTO appraisal_corrections VALUES (?,?,?,?)', ('fix-' + status, identifier, '{}', 1.0))
    for _ in range(2):   # idempotent
        module.AppraisalStore(TurnIdempotencyLedger(state.ledger.db_path), owner_id='owner', clock=state.clock)
        with state.ledger._connect() as conn:
            assert [r[0] for r in conn.execute('SELECT id FROM appraisal_records')] == [live]
            assert [r[0] for r in conn.execute('SELECT record_id FROM appraisal_heads')] == [live]
            assert conn.execute('SELECT count(*) FROM appraisal_corrections').fetchone()[0] == 0


def test_new_runs_table_has_no_lease_columns(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    module.AppraisalStore(ledger, owner_id='owner')
    with ledger._connect() as conn:
        columns = {row[1] for row in conn.execute('PRAGMA table_info(appraisal_runs)')}
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
    assert columns == {'turn_id', 'status', 'attempts', 'next_attempt', 'disposition', 'error'}
    assert {'appraisal_outcomes', 'appraisal_outcome_subject', 'appraisal_outcome_turn'} <= tables


def test_claim_is_a_plain_atomic_status_and_a_dead_processes_job_is_reset_once(state, monkeypatch):
    owner_turn(state, 'a', 'The export failed.')
    owner_turn(state, 'b', 'The export failed again.')
    first = state._claim()
    competitor = module.AppraisalStore(TurnIdempotencyLedger(state.ledger.db_path), owner_id='owner',
                                       clock=state.clock)
    second = competitor._claim()
    assert (first['turn_id'], first['attempts'], second['turn_id']) == ('a', 1, 'b')
    state.test_clock.value += 10 * 86400
    assert competitor._claim() is None                     # a running job is never reclaimed by time
    assert run_of(state, 'a')['status'] == 'running'
    # A new process: the first claim on this ledger resets what a dead process left running.
    monkeypatch.setattr(module, '_RECOVERED', set())
    again = module.AppraisalStore(TurnIdempotencyLedger(state.ledger.db_path), owner_id='owner',
                                  clock=state.clock)._claim()
    assert (again['turn_id'], again['attempts']) == ('a', 2)
    assert run_of(state, 'b')['status'] == 'pending'
    assert competitor._claim()['turn_id'] == 'b'


@pytest.mark.asyncio
async def test_a_cancelled_call_puts_its_job_back(state):
    import asyncio
    owner_turn(state, 'report', 'The export failed.')

    async def cancel(_):
        raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await state.process_one(Processor(pause=cancel))
    assert run_of(state, 'report')['status'] == 'pending'


@pytest.mark.asyncio
async def test_outcome_topic_is_not_blocked_by_a_supplied_incident(state):
    owner_turn(state, 'incident', 'The archive export stalled.')
    await state.process_one(Processor(answer(observations=[lambda p: observation(p, topic='quarterly figures')])))
    owner_turn(state, 'again', 'The archive export gave stale quarterly figures again.')
    processor = Processor(answer(reported))
    await state.process_one(processor)
    assert processor.requests[0]['incident_ids']
    assert [r['turn_id'] for r in outcomes_of(state)] == ['again']


def test_occurred_at_is_the_turn_time_not_the_processing_time(state):
    owner_turn(state, 'report', 'The export failed.')
    stamp = datetime.fromtimestamp(state.test_clock.value, timezone.utc)
    job = state._claim()
    source_row, payload, heads = state._prepare(job)
    state.test_clock.value += 3600
    _, outcomes = state._validate(json.dumps({'observations': [], 'incident_decisions': [],
                                              'outcomes': [reported(payload)]}), payload)
    state._commit(job, source_row, [], outcomes, heads, {'model_id': 'fixture'})
    row, = outcomes_of(state)
    assert row['occurred_at'] == stamp.timestamp() and row['created_at'] == state.test_clock.value
