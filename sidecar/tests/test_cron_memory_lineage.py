"""A native reminder consumed by a later turn remains source-dependent."""
import asyncio
import importlib
import json
from types import SimpleNamespace

import pytest

from test_native_owned_copies import native_runtime


def _begin(rt, *, owner='owner', binding_home=None, mirror=True):
    from cron.jobs import create_job, use_cron_store
    from gateway.mirror import mirror_to_session
    cron_memory = importlib.import_module(rt.plugin.__name__ + '.cron_memory')
    home = rt.db.db_path.parent
    with use_cron_store(home):
        job = create_job('', 'every 1h', script='reader.py', no_agent=True, deliver='local')
        cron_memory.retain(rt.outbox, home=binding_home or home, owner=owner, job=job,
            binding={'binding_id':'fixture-reminder'}, sources=[rt.ref])
    if mirror:
        assert mirror_to_session('cli', 'fixture', rt.fact, source_label='cron', role='user',
            session_id='reader', metadata={'cron_job_id':job['id'], 'cron_execution_id':'fixture-run'})
    else:
        rt.db.append_message('reader', 'user', rt.fact)
    current = {'role':'user', 'content':'Prepare the badge mentioned in the reminder.'}
    current['_row_id'] = rt.db.append_message('reader', 'user', current['content'])
    history = rt.db.get_messages_as_conversation('reader', repair_alternation=False, include_row_ids=True)
    rt.memory.observe_native_anchor(rt.scope, history, user_message=current['content'])
    rt.memory.observe(rt.scope, history, user_message=current['content'])
    return current, history


def test_mirrored_source_supplies_later_answer_and_erases_its_retained_copies(native_runtime):
    rt = native_runtime
    current, history = _begin(rt)
    request = {'messages':[{'role':row['role'], 'content':row['content']} for row in history]}
    checked = rt.memory(request, rt.scope)
    assert rt.fact in json.dumps(checked['request'])
    supplied = rt.memory.finish(task_id='reader', turn_id='read-turn', contact_id='owner')
    answer = {'role':'assistant', 'content':'Prepare the forgettoken violet badge.'}
    answer['_row_id'] = rt.db.append_message('reader', 'assistant', answer['content'])
    rt.ledger.record_source('later-answer', contact_id='owner', session_id='reader', messages=[
        {'role':'user', 'content':current['content']},
        {'role':'assistant', 'content':answer['content'], '_supplied_sources':supplied}], derive_claims=False)
    assert rt.owned.retain_origin(rt.scope, 'later-answer', messages=[current, answer])
    unrelated = rt.db.append_message('reader', 'user', 'Keep the independent calendar request.')
    before_unrelated = rt.db.get_messages('reader')[-1]
    rt.ledger.erase_sources(contact_id='owner', turn_ids=['original-source'])
    receipt = asyncio.run(rt.owned.reconcile(contact='owner'))
    after = {row['id']:dict(row) for row in rt.db._conn.execute('SELECT * FROM messages')}
    assert after[unrelated]['content'] == before_unrelated['content']
    observed = {'sources':supplied, 'reconciliation':receipt['status'],
                'answer_retained':'forgettoken' in after[answer['_row_id']]['content']}
    assert observed == {'sources':[rt.ref], 'reconciliation':'settled', 'answer_retained':False}


@pytest.mark.parametrize('in_place', [False, True], ids=['compression-child', 'in-place-compaction'])
def test_consuming_request_reserves_actual_native_compression_summary(native_runtime, in_place):
    rt = native_runtime
    untouched = rt.db.append_message('reader', 'user', 'Keep this independent source input.')
    current, history = _begin(rt)
    # The same physical-request checkpoint governs native summarizer calls.
    request = {'messages':[{'role':'system', 'content':'Summarize this conversation.'},
                          *[{'role':row['role'], 'content':row['content']} for row in history]]}
    assert rt.fact in json.dumps(rt.memory(request, rt.scope)['request'])
    assert rt.memory.supplied_snapshot(rt.scope) == [rt.ref]
    rt.db.append_message('reader', 'assistant', 'The forgettoken label is violet.')
    summary = {'role':'user', 'content':'Retained summary: forgettoken label is violet.',
               '_compressed_summary':True, 'display_kind':'hidden'}
    human = {'role':'user', 'content':'Keep the next independent calendar request.'}
    handoff = [summary, human]
    holder = 'fixture-native-summary'
    assert rt.db.try_acquire_compression_lock('reader', holder)
    try:
        if in_place:
            rt.db.archive_and_compact('reader', handoff, lock_holder=holder, tail_count=1)
        else:
            rt.db.publish_compression_child(parent_session_id='reader', child_session_id='compressed',
                source='cli', messages=handoff, compression_lock_holder=holder)
    finally:
        rt.db.release_compression_lock('reader', holder)
    session = 'reader' if in_place else 'compressed'
    marked = list(rt.db._conn.execute('SELECT id FROM messages WHERE session_id=? AND _compressed_summary=1', (session,)))
    assert len(marked) == 1
    continued_scope = SimpleNamespace(**(vars(rt.scope) | {'session_id':session,
        'task_id':'continued', 'turn_id':'continued-turn'}))
    continued = rt.db.get_messages_as_conversation(session, include_row_ids=True)
    rt.memory.observe_native_anchor(continued_scope, continued, user_message=human['content'])
    rt.memory.observe(continued_scope, continued, user_message=human['content'])
    continued_request = {'messages':[{'role':row['role'], 'content':row['content']} for row in continued]}
    assert 'forgettoken' in json.dumps(rt.memory(continued_request, continued_scope)['request'])
    assert rt.memory.supplied_snapshot(continued_scope) == [rt.ref]
    rt.db.append_message(session, 'assistant', 'The summary says forgettoken is violet.')
    before = {row['id']:dict(row) for row in rt.db._conn.execute('SELECT * FROM messages')}
    rt.ledger.erase_sources(contact_id='owner', turn_ids=['original-source'])
    receipt = asyncio.run(rt.owned.reconcile(contact='owner'))
    after = {row['id']:dict(row) for row in rt.db._conn.execute('SELECT * FROM messages')}
    assert receipt['status'] == 'settled', receipt
    assert 'forgettoken' not in json.dumps([row for row in after.values() if row['session_id'] != 'original'],
                                         default=lambda value: value.hex())
    assert after[untouched] == before[untouched]
    # This now-consumed turn receives the ordinary API-payload erasure receipt;
    # its original human input and native structural fields remain intact.
    for field in before[human['_row_id']].keys() - {'display_metadata', 'display_identity', 'display_order'}:
        assert after[human['_row_id']][field] == before[human['_row_id']][field]


@pytest.mark.parametrize('mismatch', ['contact', 'profile', 'unmarked-human', 'not-visible'])
def test_only_visible_native_rows_with_matching_durable_owner_supply_sources(native_runtime, mismatch):
    rt = native_runtime
    current, history = _begin(rt, owner='someone-else' if mismatch == 'contact' else 'owner',
        binding_home=rt.db.db_path.parent / 'other-profile' if mismatch == 'profile' else None,
        mirror=mismatch != 'unmarked-human')
    if mismatch == 'not-visible':
        history = [current]
    request = {'messages':[{'role':row['role'], 'content':row['content']} for row in history]}
    rt.memory(request, rt.scope)
    assert rt.memory.supplied_snapshot(rt.scope) == []


@pytest.mark.parametrize('merged', [False, True])
@pytest.mark.parametrize('unavailable', ['erased', 'offline'])
def test_stale_native_mirror_is_withheld_while_current_human_input_survives(native_runtime, monkeypatch, merged, unavailable):
    rt = native_runtime
    current, history = _begin(rt)
    if unavailable == 'erased':
        rt.ledger.erase_sources(contact_id='owner', turn_ids=['original-source'])
    else:
        def offline(*args, **kwargs):
            raise OSError('Fixture source freshness unavailable')
        monkeypatch.setattr(rt.client, 'post', offline)
    request = {'messages':([{'role':'user', 'content':'\n\n'.join(row['content'] for row in history)}]
        if merged else [{'role':row['role'], 'content':row['content']} for row in history])}
    checked = rt.memory(request, rt.scope)
    assert 'forgettoken' not in json.dumps(checked['request'])
    assert current['content'] in json.dumps(checked['request'])
    assert rt.memory.supplied_snapshot(rt.scope) is None


def test_unrelated_long_history_does_not_hide_one_owned_native_mirror(native_runtime):
    rt = native_runtime
    rt.db.replace_messages('reader', [{'role':'user' if index % 2 == 0 else 'assistant',
        'content':'Independent conversation row '+str(index)} for index in range(520)])
    _, history = _begin(rt)
    request = {'messages':[{'role':row['role'], 'content':row['content']} for row in history]}
    checked = rt.memory(request, rt.scope)
    assert rt.fact in json.dumps(checked['request'])
    assert rt.memory.supplied_snapshot(rt.scope) == [rt.ref]


def test_detached_review_rechecks_authentic_parent_mirror_lineage_after_parent_cleanup(native_runtime):
    rt = native_runtime
    _, history = _begin(rt)
    parent = rt.memory.snapshot_review_parent(rt.scope)
    rt.memory.finish(task_id='reader', turn_id='read-turn', contact_id='owner')
    review = SimpleNamespace(**(vars(rt.scope) | {'platform':'background_review',
        'task_id':'review-task', 'turn_id':'review-turn'}))
    assert rt.memory.observe_review(review, parent)
    request = {'messages':[{'role':row['role'], 'content':row['content']} for row in history]}
    assert rt.fact in json.dumps(rt.memory(request, review)['request'])
    assert rt.memory.supplied_snapshot(review) == [rt.ref]
    assert {row['metadata']['kind'] for row in rt.owned._rows()} == {'cron'}
    rt.ledger.erase_sources(contact_id='owner', turn_ids=['original-source'])
    assert 'forgettoken' not in json.dumps(rt.memory(request, review)['request'])
