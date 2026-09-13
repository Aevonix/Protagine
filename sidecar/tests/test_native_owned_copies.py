"""Real native SQLite/FTS erasure, driven by canonical lineage and settled hooks."""
import asyncio
from contextlib import closing
import importlib
import json
from types import SimpleNamespace

import httpx
import pytest

from test_hermes_turn_outbox import _load_plugin, _Context, _Client
from test_native_request_erasure import freshness_response
from pacomind.turns import TurnIdempotencyLedger


@pytest.fixture
def native_runtime(tmp_path, monkeypatch):
    state = pytest.importorskip('hermes_state')
    if not hasattr(state.SessionDB, 'redact_message_payloads'):
        pytest.skip('selected native runtime lacks the owned-copy writer extension')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'hermes'))
    plugin = _load_plugin('pacomind_owned_copy_fixture')
    module = importlib.import_module(plugin.__name__ + '.native_owned_copies')
    requests = importlib.import_module(plugin.__name__ + '.request_memory')
    outbox = plugin.TurnOutbox(tmp_path / 'outbox.db')
    outbox.prepare()
    ledger = TurnIdempotencyLedger(tmp_path / 'canonical.db')
    def get(path, **kwargs):
        assert path == '/v1/host/memory/sources/erasures'
        return httpx.Response(200, json=ledger.erasure_feed(kwargs['params']['contact_id'],
            kwargs['params']['after']), request=httpx.Request('GET', 'http://fixture' + path))
    def post(path, **kwargs):
        return freshness_response(ledger, path, kwargs['json'])
    client = SimpleNamespace(get=get, post=post)
    memory = requests.RequestMemory(client, outbox)
    scope = SimpleNamespace(contact_id='owner', session_id='reader', task_id='reader',
                            turn_id='read-turn', valid_participant=True, platform='cli')
    scopes = SimpleNamespace(for_execution=lambda **kw: scope if kw == {
        'session_id':'reader', 'task_id':'reader', 'turn_id':'read-turn'} else None)
    owned = module.NativeOwnedCopies(memory, scopes)
    memory.ownership = owned
    db = state.SessionDB(module.NativeOwnedCopies._path())
    db.create_session('original', source='cli')
    db.create_session('reader', source='cli')
    fact = 'Orchard forgettoken badge is violet.'
    original = db.append_message('original', 'user', fact)
    ledger.record_source('original-source', contact_id='owner', session_id='original',
        messages=[{'role':'user', 'content':fact}], derive_claims=False)
    ref = ledger.source_references(['original-source'], contact_id='owner', session_id='reader')[0]
    yield SimpleNamespace(**locals())
    db.close()


def begin_read(rt, *, recall=False, admitted_input=True):
    text = 'Use the orchard record to prepare a label.'
    current = {'role':'user', 'content':text}
    rt.memory.observe_native_anchor(rt.scope, [current], user_message=text)
    rt.memory.observe(rt.scope, [current], user_message=text if admitted_input else None)
    if recall:
        marker = '[pacomind-recall-v1 ' + json.dumps({'contact_id':'owner', 'watermark':0,
                 'sources':[rt.ref]}) + ']\n' + rt.fact + '\n[/pacomind-recall-v1]'
        current['api_content'] = text + '\n\n' + marker
    current['_row_id'] = rt.db.append_message('reader', 'user', text, api_content=current.get('api_content'))
    rt.memory({'messages':[{'role':'user', 'content':current.get('api_content', text)}]}, rt.scope)
    if not recall:
        rt.db.append_message('reader', 'assistant', None, tool_calls=[{'id':'source-read',
            'type':'function', 'function':{'name':'pacomind_source_read', 'arguments':'{}'}}])
        assert rt.memory.register_source_read(rt.scope, 'source-read', rt.fact,
            {'source_refs':[rt.ref], 'watermark':0})
        rt.db.append_message('reader', 'tool', rt.fact, tool_call_id='source-read')
    return current


def rows(rt):
    with closing(rt.owned._native_read()) as db:
        return {row['id']:dict(row) for row in db.execute('SELECT * FROM messages ORDER BY id')}


def erase(rt):
    rt.ledger.erase_sources(contact_id='owner', turn_ids=['original-source'])


def settle(rt):
    return rt.owned.native_settled(session_id='reader', task_id='reader', turn_id='read-turn', platform='cli')


@pytest.mark.parametrize('storage_available', [True, False])
def test_post_hook_publishes_only_after_exact_native_origin_retention(native_runtime, monkeypatch, storage_available):
    rt = native_runtime
    monkeypatch.setattr(rt.plugin, 'PacoMindClient', _Client)
    monkeypatch.setenv('PACOMIND_GENERAL_PLUGIN_ACTIVE', '1')
    monkeypatch.setenv('PACOMIND_MEMORY_WORKER_TOOLS', '0')
    monkeypatch.setenv('PACOMIND_MEMORY_TURN_WRITER', 'disabled')
    context = _Context(rt.outbox.path)
    rt.plugin.register(context)
    client = _Client.instances[-1]
    current = {'role':'user', 'content':'Keep the maintenance receipt.'}
    kwargs = dict(session_id='reader', task_id='capture-task', turn_id='capture-turn',
                  platform='sms', sender_id='fixture', user_message=current['content'])
    context.hooks['pre_llm_call'](**kwargs, conversation_history=[current])
    current['_row_id'] = rt.db.append_message('reader', 'user', current['content'])
    answer = {'role':'assistant', 'content':'The maintenance receipt is ready.'}
    answer['_row_id'] = rt.db.append_message('reader', 'assistant', answer['content'])
    before = rt.db.get_messages('reader')
    if not storage_available:
        def unavailable(*args, **kwargs):
            raise OSError('Fixture native ownership storage unavailable')
        monkeypatch.setattr(rt.module.NativeOwnedCopies, '_native_read', unavailable)
    assert context.hooks['post_llm_call'](**kwargs, conversation_history=[current, answer],
        assistant_response=answer['content'], model='fixture') is None
    # Observation cannot suppress or rewrite the already persisted safe reply.
    assert rt.db.get_messages('reader') == before
    assert len(client.synced) == int(storage_available)
    captured = rt.outbox.snapshot()
    assert len(captured) == int(storage_available)
    if storage_available:
        assert captured[0]['payload']['assistant_message'] == answer['content']
        with closing(rt.outbox._connect()) as db:
            metadata = json.loads(db.execute("SELECT metadata_json FROM native_source_ownership "
                "WHERE ownership_id=?", ('origin:' + captured[0]['turn_id'],)).fetchone()[0])
        assert set(metadata['anchors']) == {str(current['_row_id']), str(answer['_row_id'])}


def test_unavailable_origin_leaves_no_empty_binding_before_exact_retry(native_runtime):
    rt = native_runtime
    assert not rt.owned.retain_origin(rt.scope, 'capture-source', messages=[])
    assert not rt.owned._rows()
    current = {'role':'user', 'content':'Keep the exact maintenance input.'}
    current['_row_id'] = rt.db.append_message('reader', 'user', current['content'])
    assert rt.owned.retain_origin(rt.scope, 'capture-source', messages=[current])
    row, = rt.owned._rows()
    assert set(row['metadata']['anchors']) == {str(current['_row_id'])}


def test_explicit_unavailable_native_descriptor_cannot_reuse_old_anchor(native_runtime):
    rt = native_runtime
    begin_read(rt, recall=True)
    assert rt.memory.native_anchor(rt.scope) is not None
    rt.memory.observe_native_message(rt.scope, None)
    assert rt.memory.native_anchor(rt.scope) is None
    assert not rt.owned.retain(rt.scope, [rt.ref])
    assert len(rt.owned._rows()) == 1  # Existing ownership is still required.


@pytest.mark.parametrize('in_place', [False, True])
def test_native_compaction_keeps_old_and_new_exact_supplied_anchors(native_runtime, in_place):
    rt = native_runtime
    current = begin_read(rt, recall=True)
    handoff = rt.db.get_messages_as_conversation('reader')
    holder = 'fixture-native-compaction'
    assert rt.db.try_acquire_compression_lock('reader', holder)
    try:
        if in_place:
            rt.db.archive_and_compact('reader', handoff, lock_holder=holder, tail_count=len(handoff))
        else:
            rt.db.publish_compression_child(parent_session_id='reader', child_session_id='continuation',
                source='cli', messages=handoff, compression_lock_holder=holder)
    finally:
        rt.db.release_compression_lock('reader', holder)
    session = 'reader' if in_place else 'continuation'
    scope = SimpleNamespace(**{**vars(rt.scope), 'session_id':session})
    clone = handoff[-1]
    assert clone['_row_id'] != current['_row_id']
    rt.memory.observe_native_message(scope, clone)
    assert rt.owned.retain(scope, [rt.ref])
    retained = rt.owned._rows()
    assert {row['metadata']['anchor_id'] for row in retained} == {current['_row_id'], clone['_row_id']}
    rt.db.append_message(session, 'assistant', 'The forgettoken label is violet.')
    erase(rt)
    assert asyncio.run(rt.owned.reconcile(contact='owner'))['status'] == 'settled'
    after = rows(rt)
    for row_id in (current['_row_id'], clone['_row_id']):
        assert after[row_id]['content'] == current['content']
        assert after[row_id]['api_content'] is None
    assert not rt.db.search_messages('forgettoken', include_inactive=True)


@pytest.mark.parametrize('changed_native', [False, True])
def test_partial_canonical_input_erasure_selects_exact_native_wrapper(native_runtime, changed_native):
    rt = native_runtime
    original = 'Use the exact maintenance receipt I supplied.'
    current = {'role':'user', 'content':'Execute the admitted maintenance task wrapper.'}
    current['_row_id'] = rt.db.append_message('reader', 'user', current['content'])
    answer = {'role':'assistant', 'content':'The receipt was inspected.'}
    answer['_row_id'] = rt.db.append_message('reader', 'assistant', answer['content'])
    for source_id, messages in (
        ('admitted-input', [{'role':'user','content':original}]),
        ('captured-input', [{'role':'user','content':original}, {'role':'assistant','content':answer['content']}]),
    ):
        rt.ledger.record_source(source_id, contact_id='owner', session_id='reader', messages=messages, derive_claims=False)
        assert rt.owned.retain_origin(rt.scope, source_id,
            messages=[current] if source_id=='admitted-input' else [current, answer], canonical_user_message=original)
    unrelated = rt.db.append_message('reader', 'user', 'Keep the unrelated calendar request.')
    before = rows(rt)[unrelated]
    if changed_native:
        rt.db._execute_write(lambda db: db.execute('UPDATE messages SET content=? WHERE id=?',
            ('New unrelated native content.', current['_row_id'])))
    rt.ledger.erase_sources(contact_id='owner', turn_ids=['admitted-input'])
    partial = next(rule for rule in rt.ledger.erasure_feed('owner')['events']
                   if rule['source_turn_id']=='captured-input')
    assert not partial['whole_source']
    assert settle(rt)['status'] == ('pending' if changed_native else 'settled')
    assert rows(rt)[current['_row_id']]['content'] == (
        'New unrelated native content.' if changed_native else '[Content removed.]')
    assert rows(rt)[unrelated] == before


@pytest.mark.parametrize('capture', [False, True])
def test_source_read_and_linked_native_answer_are_physically_erased(native_runtime, capture):
    rt = native_runtime
    unrelated = [rt.db.append_message('reader', 'user', 'Keep the useful calendar item.'),
                 rt.db.append_message('reader', 'assistant', 'The meeting remains at nine.')]
    current = begin_read(rt)
    answer = rt.db.append_message('reader', 'assistant', 'The label reads forgettoken violet.',
                                  reasoning='forgettoken reasoning')
    if capture:
        rt.ledger.record_source('captured-reader', contact_id='owner', session_id='reader',
            messages=[{'role':'user','content':current['content']},
                      {'role':'assistant','content':'The label reads forgettoken violet.', '_supplied_sources':[rt.ref]}],
            derive_claims=False)
    # Ordinary capture is not a prerequisite: ownership survives turn cleanup
    # and a new adapter instance, with no fabricated canonical reader source.
    rt.memory.finish(task_id='reader', turn_id='read-turn', contact_id='owner')
    rt.owned = rt.module.NativeOwnedCopies(rt.memory, rt.scopes)
    before = rows(rt)
    assert rt.db.search_messages('forgettoken', include_inactive=True)
    erase(rt)
    result = settle(rt)
    after = rows(rt)
    assert result['status'] == 'settled', result
    assert result['redacted_rows'] >= 4
    assert set(after) == set(before)
    assert [after[key] for key in unrelated] == [before[key] for key in unrelated]
    assert after[current['_row_id']]['content'] == before[current['_row_id']]['content']
    assert 'forgettoken' not in json.dumps(after, default=lambda value: value.hex())
    assert not rt.db.search_messages('forgettoken', include_inactive=True)
    assert not rt.owned._rows()
    assert settle(rt)['redacted_rows'] == 0
    with closing(rt.outbox._connect()) as db:
        assert 'forgettoken' not in '\n'.join(row[0] for row in db.execute('SELECT rules_json FROM turn_erasures'))


def test_recall_api_copy_erased_but_original_human_input_kept(native_runtime):
    rt = native_runtime
    current = begin_read(rt, recall=True)
    rt.db.append_message('reader', 'assistant', 'Use forgettoken on the label.')
    assert len(rt.owned._rows()) == 1
    before = rows(rt)[current['_row_id']]
    assert rt.fact in before['api_content']
    erase(rt)
    assert settle(rt)['status'] == 'settled'
    after = rows(rt)[current['_row_id']]
    assert after['content'] == before['content']
    assert after['api_content'] is None
    assert not rt.db.search_messages('forgettoken', include_inactive=True)


def test_active_turn_retries_include_answer_written_after_first_attempt(native_runtime):
    rt = native_runtime
    begin_read(rt)
    assert rt.db.try_acquire_session_turn_lease('reader', 'native-owner')
    erase(rt)
    first = settle(rt)
    assert first['status'] == 'pending'
    assert any(row['metadata']['kind']=='supplied' for row in rt.owned._rows())
    answer = rt.db.append_message('reader', 'assistant', 'Late forgettoken final answer.',
                                  turn_lease_holder='native-owner')
    rt.db.release_session_turn_lease('reader', 'native-owner')
    assert settle(rt)['status'] == 'settled'
    assert 'forgettoken' not in rows(rt)[answer]['content']
    assert not rt.db.search_messages('forgettoken', include_inactive=True)
    assert not rt.owned._rows()


def test_pending_original_anchor_survives_partial_completion_and_reopen(native_runtime, monkeypatch):
    rt = native_runtime
    rt.db.append_message('original', 'assistant', 'Original forgettoken answer.')
    erase(rt)
    real_remove = rt.owned._remove
    monkeypatch.setattr(rt.owned, '_remove', lambda row: (_ for _ in ()).throw(OSError('receipt interrupted')))
    first = settle(rt)
    assert first['status'] == 'pending'
    assert not rt.db.search_messages('forgettoken', include_inactive=True)
    rt.owned = rt.module.NativeOwnedCopies(rt.memory, rt.scopes)
    assert settle(rt)['status'] == 'settled'
    assert not rt.owned._rows()


def test_unaffected_ownership_does_not_hide_later_pending_erasure(native_runtime):
    rt = native_runtime
    with closing(rt.outbox._connect()) as db, db:
        for index in range(40):
            db.execute('INSERT INTO native_source_ownership VALUES (?,?,?,?,?)',
                (f'aaa:{index:03}', 'owner', 'reader', f'turn-{index}',
                 json.dumps({'kind':'supplied','anchor_id':1,'sources':[
                     {'source_id':'unrelated','source_version':'a'*64}]})))
    begin_read(rt)
    rt.db.append_message('reader', 'assistant', 'Forgettoken reply.')
    erase(rt)
    assert settle(rt)['status'] == 'settled'
    assert not rt.db.search_messages('forgettoken', include_inactive=True)
    assert len(rt.owned._rows()) == 40


def test_shared_outbox_uses_origin_and_reader_profile_locations(native_runtime, tmp_path, monkeypatch):
    rt = native_runtime
    assert rt.owned.retain_origin(rt.scope.__class__(**{**vars(rt.scope), 'session_id':'original'}),
        'original-source', messages=[{'role':'user', 'content':rt.fact, '_row_id':rt.original}])
    root_path = rt.db.db_path
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'helper-profile'))
    helper = rt.state.SessionDB(rt.owned._path())
    helper.create_session('reader', source='cli')
    rt.db = helper
    current = begin_read(rt)
    answer = helper.append_message('reader', 'assistant', 'Helper forgettoken answer.')
    helper_path = helper.db_path
    # Reconcile from a third independent profile using only previously bound
    # paths in the same outbox. Never confuse identical display text with origin.
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'reconciler-profile'))
    other = rt.state.SessionDB(rt.owned._path())
    other.create_session('unrelated', source='cli')
    kept = other.append_message('unrelated', 'user', 'Keep this independent profile input.')
    try:
        erase(rt)
        assert settle(rt)['status'] == 'settled'
        assert helper.get_messages('reader')[0]['content'] == current['content']
        assert not helper.search_messages('forgettoken', include_inactive=True)
        with rt.state.SessionDB(root_path) as root:
            assert not root.search_messages('forgettoken', include_inactive=True)
        assert other.get_messages('unrelated')[0]['id'] == kept
        assert not rt.owned._rows()
    finally:
        helper.close()
        other.close()


def test_version_two_upgrade_retains_rules_and_stages_native_erasure(native_runtime):
    rt = native_runtime
    rt.outbox.enqueue('pending-existing', {'turn_id':'pending-existing', 'contact_id':'owner',
        'session_id':'unrelated', 'user_message':'Preserve this queued request.', 'assistant_message':'Queued.'})
    erase(rt)
    rt.outbox.apply_erasure_page('owner', rt.ledger.erasure_feed('owner'))
    with closing(rt.outbox._connect()) as db, db:
        queued = tuple(db.execute('SELECT * FROM turn_outbox').fetchone())
        erasures = tuple(db.execute('SELECT * FROM turn_erasures').fetchone())
        db.execute('DROP TABLE native_source_ownership')
        db.execute('PRAGMA user_version=2')
    migrated = type(rt.outbox)(rt.outbox.path)
    assert migrated.prepare()['user_version'] == 3
    rt.outbox = rt.memory.outbox = rt.owned.outbox = migrated
    with closing(migrated._connect()) as db:
        assert tuple(db.execute('SELECT * FROM turn_outbox').fetchone()) == queued
        assert tuple(db.execute('SELECT * FROM turn_erasures').fetchone()) == erasures
        assert db.execute('SELECT count(*) FROM native_source_ownership').fetchone()[0] == 1
    assert settle(rt)['status'] == 'settled'
    assert not rt.db.search_messages('forgettoken', include_inactive=True)


def test_retained_erasures_finish_while_new_feed_is_unavailable(native_runtime, monkeypatch):
    rt = native_runtime
    begin_read(rt)
    rt.db.append_message('reader', 'assistant', 'Retained forgettoken answer.')
    erase(rt)
    rt.outbox.apply_erasure_page('owner', rt.ledger.erasure_feed('owner'))
    monkeypatch.setattr(rt.owned, '_feed', lambda owner: (_ for _ in ()).throw(OSError('offline')))
    receipt = settle(rt)
    assert receipt['status'] == 'pending'  # Newer erasures could not be checked.
    assert receipt['redacted_rows'] >= 4
    assert not rt.db.search_messages('forgettoken', include_inactive=True)
    assert not rt.owned._rows()


def test_actual_gateway_settled_callback_erases_and_evicts_owned_cache(native_runtime, tmp_path):
    rt = native_runtime
    from gateway.config import GatewayConfig
    from gateway.run_agent_cache import GatewayAgentCacheMixin
    from gateway.session import SessionStore
    from gateway.turn_lease import SessionTurnLeaseRegistry
    import threading
    class Runner(GatewayAgentCacheMixin):
        def _running_agent_ids(self):
            return set()
        def _peek_session_state(self, key):
            return None
        def _spawn_release_thread(self, target, args, name, *, inline_fallback):
            target(*args)
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    store._db = rt.db
    gateway = Runner()
    gateway.session_store = store
    gateway._agent_cache_lock = threading.Lock()
    gateway._turn_leases = SessionTurnLeaseRegistry()
    current = begin_read(rt)
    rt.db.append_message('reader', 'assistant', 'Cached forgettoken answer.')
    # The real gateway appends this housekeeping row after a fresh turn. It
    # belongs to transcript bookkeeping, not the source-derived answer span.
    store.append_to_transcript('reader', {'role':'session_meta', 'tools':[],
        'model':'fixture', 'platform':'pacomind_task', 'timestamp':123.0})
    rt.db.get_messages('reader', include_compacted=True)
    metadata_before, = [row for row in rows(rt).values() if row['role'] == 'session_meta']
    assert metadata_before['content'] is None
    assert metadata_before['display_identity'] and metadata_before['display_order']
    agent = SimpleNamespace(session_id='reader', _session_messages=[{'content':'Cached forgettoken'}],
                            _db_flush_scan_prefix=[{'content':'Cached forgettoken'}], release_clients=lambda: None)
    gateway._agent_cache = {'route:reader':(agent, 'signature', 1, 'reader')}
    store._entries['route:reader'] = SimpleNamespace(session_id='reader')
    rt.db.save_gateway_routing_entry('route:reader', json.dumps({'session_key':'route:reader','session_id':'reader'}))
    rt.owned.observe_gateway(gateway=gateway, session_store=store)
    erase(rt)
    # A CLI process sharing this outbox cannot bypass the actual gateway's
    # dirty-buffer serialization or cache boundary for the routed reader.
    first_standalone = asyncio.run(rt.owned.reconcile())
    assert first_standalone['status'] == 'pending'
    assert gateway._agent_cache and rt.db.search_messages('forgettoken', include_inactive=True)
    assert any(row['metadata'].get('pending_reason') == 'native_gateway_reconciliation_required'
               for row in rt.owned._rows())
    async def scenario():
        token = await gateway._turn_leases.acquire('reader', owner_key='current-turn', generation=1)
        first = await rt.owned.reconcile(gateway=gateway)
        assert first['status'] == 'pending'
        assert gateway._agent_cache
        gateway._turn_leases.release(token)
        rt.owned.gateway_settled(gateway=gateway, session_id='reader', session_key='route:reader', run_generation=1)
        # Await this callback's actual scheduled operation, with a finite bound.
        tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    asyncio.run(scenario())
    assert not gateway._agent_cache
    assert agent._session_messages == [] and agent._db_flush_scan_prefix is None
    assert rows(rt)[metadata_before['id']] == metadata_before
    assert not rt.db.search_messages('forgettoken', include_inactive=True)
    assert rt.db.get_messages('reader')[0]['content'] == current['content']
    assert not rt.owned._rows()


@pytest.mark.parametrize('source', ['supplied', 'original'])
def test_changed_anchor_is_pending_without_erasing_new_unrelated_content(native_runtime, source):
    rt = native_runtime
    current = begin_read(rt)
    if source == 'supplied':
        session, anchor = 'reader', current['_row_id']
    else:
        session, anchor = 'original', rt.original
    assert rt.db.try_acquire_session_turn_lease(session, 'active-writer')
    erase(rt)
    assert settle(rt)['status'] == 'pending'
    rt.db.release_session_turn_lease(session, 'active-writer')
    # An editor changed the meaning while preserving the native row identity.
    rt.db._execute_write(lambda db: db.execute('UPDATE messages SET content=? WHERE id=?',
                                               ('A newly unrelated human request.', anchor)))
    after_edit = rows(rt)[anchor]
    result = settle(rt)
    assert result['status'] == 'pending'
    assert rows(rt)[anchor] == after_edit
    assert any(row['session_id']==session and row['metadata'].get('pending_reason') ==
               'native_source_anchor_changed' for row in rt.owned._rows())


def test_unknown_historical_location_remains_observably_pending(native_runtime, caplog):
    rt = native_runtime
    rt.ledger.record_source('unknown-native-source', contact_id='owner', session_id='old-helper-session',
        messages=[{'role':'user','content':'A source from an unobserved historical helper.'}], derive_claims=False)
    rt.ledger.erase_sources(contact_id='owner', turn_ids=['unknown-native-source'])
    result = settle(rt)
    assert result['status'] == 'pending' and result['pending'] == 1
    assert any(row['session_id']=='old-helper-session' and row['metadata'].get('pending_reason') ==
               'native_source_location_unobserved' for row in rt.owned._rows())
    assert settle(rt)['pending'] == 1
    notices = [record for record in caplog.records
               if record.getMessage() == 'Native owned-copy erasure pending (native_source_location_unobserved)']
    assert len(notices) == 1


@pytest.mark.parametrize('platform', ['subagent', 'cron'])
def test_persistent_child_and_cron_reads_use_storage_anchor_without_owner_admission(native_runtime, platform):
    rt = native_runtime
    rt.scope.platform = platform
    rt.db._execute_write(lambda db: db.execute('UPDATE sessions SET source=? WHERE id=?', (platform, 'reader')))
    current = begin_read(rt, admitted_input=False)
    assert rt.memory._aliases[('owner','reader','read-turn')][1] is None
    assert rt.memory.native_anchor(rt.scope)['_row_id'] == current['_row_id']
    rt.db.append_message('reader', 'assistant', 'Derived forgettoken answer.')
    erase(rt)
    assert settle(rt)['status'] == 'settled'
    assert not rt.db.search_messages('forgettoken', include_inactive=True)
    assert rt.db.get_messages('reader')[0]['content'] == current['content']
    assert not rt.owned._rows() and not rt.memory._native_anchors


@pytest.mark.parametrize('prospective', [True, False])
def test_identical_later_input_is_not_an_origin_identity(native_runtime, prospective):
    rt = native_runtime
    first_answer = rt.db.append_message('original', 'assistant', 'The original label is forgettoken violet.')
    repeated = rt.db.append_message('original', 'user', rt.fact)
    other_answer = rt.db.append_message('original', 'assistant', 'Keep this separate later decision.')
    if prospective:
        scope = SimpleNamespace(**{**vars(rt.scope), 'session_id':'original'})
        assert rt.owned.retain_origin(scope, 'original-source', messages=[
            {'role':'user', 'content':rt.fact, '_row_id':rt.original}])
    before = rows(rt)
    erase(rt)
    result = settle(rt)
    after = rows(rt)
    if prospective:
        assert result['status'] == 'settled', result
        assert after[rt.original]['content'] == '[Content removed.]'
        assert after[first_answer]['content'] == '[Content removed.]'
    else:
        assert result['status'] == 'pending'
        assert after == before
        assert rt.owned._rows()[0]['metadata']['pending_reason'] == 'native_source_origin_ambiguous'
    assert after[repeated] == before[repeated]
    assert after[other_answer] == before[other_answer]


def test_partial_canonical_answer_erasure_preserves_its_exact_user_origin(native_runtime):
    rt = native_runtime
    current = {'role':'user', 'content':'Keep the independent calendar instruction.'}
    current['_row_id'] = rt.db.append_message('reader', 'user', current['content'])
    answer = {'role':'assistant', 'content':'The label is forgettoken violet.'}
    answer['_row_id'] = rt.db.append_message('reader', 'assistant', answer['content'])
    assert rt.owned.retain_origin(rt.scope, 'reader-source', messages=[current, answer])
    rt.ledger.record_source('reader-source', contact_id='owner', session_id='reader', messages=[
        {'role':'user','content':current['content']},
        {'role':'assistant','content':answer['content'],'_supplied_sources':[rt.ref]}], derive_claims=False)
    before = rows(rt)[current['_row_id']]
    erase(rt)
    assert any(not rule['whole_source'] for rule in rt.ledger.erasure_feed('owner')['events'])
    assert settle(rt)['status'] == 'settled'
    assert rows(rt)[current['_row_id']] == before
    assert rows(rt)[answer['_row_id']]['content'] == '[Content removed.]'
    assert not rt.db.search_messages('forgettoken', include_inactive=True)


@pytest.mark.parametrize('prior_erasures', [0, 2])
def test_partial_and_whole_erasure_share_origin_until_both_settle(native_runtime, prior_erasures):
    rt = native_runtime
    # Two prior real events make the hashed pending-row order place the later
    # whole erasure before the partial one, without changing processing order.
    for index in range(prior_erasures):
        text = 'Previous removed item ' + str(index)
        rt.db.append_message('original', 'user', text)
        rt.ledger.record_source('previous-' + str(index), contact_id='owner', session_id='original',
            messages=[{'role':'user', 'content':text}], derive_claims=False)
        rt.ledger.erase_sources(contact_id='owner', turn_ids=['previous-' + str(index)])
    assert settle(rt)['status'] == 'settled'
    current = {'role':'user', 'content':'Use the exact orchard source.'}
    current['_row_id'] = rt.db.append_message('reader', 'user', current['content'])
    answer = {'role':'assistant', 'content':'The orchard badge is forgettoken violet.'}
    answer['_row_id'] = rt.db.append_message('reader', 'assistant', answer['content'])
    assert rt.owned.retain_origin(rt.scope, 'overlapping-reader', messages=[current, answer])
    rt.ledger.record_source('overlapping-reader', contact_id='owner', session_id='reader', messages=[
        {'role':'user', 'content':current['content']},
        {'role':'assistant', 'content':answer['content'], '_supplied_sources':[rt.ref]}], derive_claims=False)
    kept = rt.db.append_message('reader', 'user', 'Keep this separate calendar request.')
    before = rows(rt)[kept]
    erase(rt)
    rt.ledger.erase_sources(contact_id='owner', turn_ids=['overlapping-reader'])
    rt.owned._feed('owner')
    _, rules = rt.outbox.erasure_state('owner')
    paired = {r['sequence']:r for r in rules if r['source_turn_id']=='overlapping-reader'}
    ordered = [paired[row['metadata']['sequence']]['whole_source'] for row in rt.owned._rows(actionable=True)
               if row['metadata'].get('sequence') in paired]
    assert ordered == ([False, True] if prior_erasures == 0 else [True, False])
    assert settle(rt)['status'] == 'settled'
    rt.owned = rt.module.NativeOwnedCopies(rt.memory, rt.scopes)
    assert settle(rt)['status'] == 'settled'
    assert not rt.owned._rows()
    after = rows(rt)
    assert after[current['_row_id']]['content'] == '[Content removed.]'
    assert after[answer['_row_id']]['content'] == '[Content removed.]'
    assert after[kept] == before


def test_cascaded_tool_observation_erases_its_exact_call_input(native_runtime):
    rt = native_runtime
    user = rt.db.append_message('reader', 'user', 'Inspect the orchard observation; keep this question.')
    call = {'id':'orchard-call', 'type':'function',
            'function':{'name':'inspect_record', 'arguments':'{"label":"forgettoken input"}'}}
    input_id = rt.db.append_message('reader', 'assistant', None, tool_calls=[call])
    tool = {'role':'tool', 'content':'The inspected orchard badge is violet.'}
    tool['_row_id'] = rt.db.append_message('reader', 'tool', tool['content'], tool_call_id='orchard-call')
    expected = rt.db.get_message_redaction_snapshot('reader', [input_id])[0]
    input_row = {'role':'assistant', 'content':None, '_row_id':input_id,
                 '_native_payload_sha256':expected['sha256']}
    assert rt.owned.retain_origin(rt.scope, 'observation-with-input', messages=[tool, input_row],
                                 row_only_ids=[input_id])
    rt.ledger.record_source('observation-with-input', contact_id='owner', session_id='reader', messages=[
        {'role':'tool', 'content':tool['content'], '_native_tool_observation':'native-tool-observation-v1',
         '_observation_sources':[rt.ref]}], derive_claims=False)
    answer = rt.db.append_message('reader', 'assistant', 'The observation says violet.')
    kept = rt.db.append_message('reader', 'user', 'Keep the unrelated meeting task.')
    before = rows(rt)
    erase(rt)
    partial = next(r for r in rt.ledger.erasure_feed('owner')['events']
                   if r['source_turn_id']=='observation-with-input')
    assert not partial['whole_source']
    assert settle(rt)['status'] == 'settled'
    after = rows(rt)
    assert after[user] == before[user] and after[kept] == before[kept]
    assert after[tool['_row_id']]['content'] == '[Content removed.]'
    assert after[answer]['content'] == '[Content removed.]'
    assert json.loads(after[input_id]['tool_calls'])[0]['function']['arguments'] == '{}'
    assert not rt.db.search_messages('forgettoken', include_inactive=True)


def test_incomplete_feed_cannot_report_settled_before_next_existing_callback(native_runtime, monkeypatch):
    rt = native_runtime
    other = rt.db.append_message('reader', 'user', 'Second purgefixture record.')
    rt.ledger.record_source('second-source', contact_id='owner', session_id='reader',
        messages=[{'role':'user','content':'Second purgefixture record.'}], derive_claims=False)
    rt.ledger.erase_sources(contact_id='owner', turn_ids=['original-source','second-source'])
    def page(path, **kwargs):
        return httpx.Response(200, request=httpx.Request('GET','http://fixture'+path),
            json=rt.ledger.erasure_feed('owner', kwargs['params']['after'], limit=1))
    monkeypatch.setattr(rt.client, 'get', page)
    assert settle(rt)['status'] == 'pending'
    assert rows(rt)[other]['content'] == 'Second purgefixture record.'
    assert settle(rt)['status'] == 'settled'
    assert not rt.db.search_messages('purgefixture', include_inactive=True)


def test_failed_ownership_write_withholds_actual_source_read_output(native_runtime, monkeypatch):
    rt = native_runtime
    begin_read(rt, recall=True)
    handler = importlib.import_module(rt.plugin.__name__ + '.source_read').handle
    def post(path, **kwargs):
        assert path == '/v1/host/memory/read'
        return httpx.Response(200, request=httpx.Request('POST','http://fixture'+path),
            json={'source':{'source_refs':[rt.ref], 'watermark':0, 'content':rt.fact}})
    monkeypatch.setattr(rt.client, 'post', post)
    # This is a real persistence error, not a mocked success/failure boolean.
    monkeypatch.setattr(rt.outbox, '_connect', lambda **kw: (_ for _ in ()).throw(OSError('fixture disk unavailable')))
    result = json.loads(handler(rt.ref, rt.scope, rt.client, rt.memory, {'tool_call_id':'failed-read'}))
    assert result['complete'] is False and 'unavailable' in result['error']
    assert rt.fact not in json.dumps(result)
    assert 'failed-read' not in rt.memory._read_receipts[('owner','reader','read-turn')]


def test_failed_ownership_write_withholds_recall_and_preserves_ordinary_input(native_runtime, monkeypatch):
    rt = native_runtime
    current = {'role':'user', 'content':'Preserve this ordinary new request.'}
    rt.memory.observe_native_anchor(rt.scope, [current], user_message=current['content'])
    rt.memory.observe(rt.scope, [current], user_message=current['content'])
    current['api_content'] = current['content'] + '\n[pacomind-recall-v1 ' + json.dumps({
        'contact_id':'owner','watermark':0,'sources':[rt.ref]}) + ']\n' + rt.fact + '\n[/pacomind-recall-v1]'
    current['_row_id'] = rt.db.append_message('reader','user',current['content'],api_content=current['api_content'])
    # The feed works; only the ownership retention transaction fails.
    original_connect = rt.owned._native_read
    monkeypatch.setattr(rt.owned, '_native_read', lambda *a, **kw: (_ for _ in ()).throw(OSError('fixture ownership read unavailable')))
    result = rt.memory({'messages':[{'role':'user','content':current['api_content']}]}, rt.scope)
    assert result['reason'] == 'native_source_ownership_unavailable'
    assert rt.fact not in json.dumps(result['request'])
    assert current['content'] in json.dumps(result['request'])
    assert not rt.memory.supplied_snapshot(rt.scope)
    # Withholding model exposure is not a claim that a preexisting native API
    # copy disappeared during a storage outage. That copy remains observable.
    monkeypatch.setattr(rt.owned, '_native_read', original_connect)
    assert rt.fact in rows(rt)[current['_row_id']]['api_content']


def test_settlement_does_not_decode_permanent_origin_history(native_runtime, monkeypatch):
    rt = native_runtime
    with closing(rt.outbox._connect()) as db, db:
        for index in range(300):
            db.execute('INSERT INTO native_source_ownership VALUES (?,?,?,?,?)',
                (f'origin:history-{index}', 'owner', 'reader', f'history-{index}',
                 json.dumps({'kind':'origin','native_db':str(rt.db.db_path),'anchors':{}})))
    decode = rt.module.json.loads
    def checked(value, *args, **kwargs):
        result = decode(value, *args, **kwargs)
        assert not isinstance(result, dict) or result.get('kind') != 'origin'
        return result
    monkeypatch.setattr(rt.module.json, 'loads', checked)
    assert settle(rt)['status'] == 'settled'


@pytest.mark.parametrize('changed', [False, True])
def test_exact_origin_replays_only_fully_redacted_native_marker(native_runtime, changed):
    rt = native_runtime
    scope = SimpleNamespace(**{**vars(rt.scope), 'session_id':'original'})
    assert rt.owned.retain_origin(scope, 'original-source', messages=[
        {'role':'user','content':rt.fact,'_row_id':rt.original}])
    expected = rt.db.get_message_redaction_snapshot('original', [rt.original])
    rt.db.redact_message_payloads('original', expected)
    if changed:
        rt.db._execute_write(lambda db: db.execute('UPDATE messages SET content=? WHERE id=?',
            ('Keep this new unrelated content despite the retained marker.', rt.original)))
    before = rows(rt)[rt.original]
    erase(rt)
    result = settle(rt)
    assert result['status'] == ('pending' if changed else 'settled')
    assert rows(rt)[rt.original] == before


@pytest.mark.parametrize('change', ['late_answer', 'anchor_edit'])
def test_changed_native_turn_between_selection_and_writer_stays_pending(native_runtime, monkeypatch, change):
    rt = native_runtime
    current = begin_read(rt)
    rt.db.append_message('reader', 'assistant', 'Initial forgettoken answer.')
    save = rt.owned._save
    changed = []
    def intervene(row, metadata):
        save(row, metadata)
        if row['metadata']['kind'] != 'supplied' or 'selection' not in metadata or changed:
            return
        changed.append(True)
        if change == 'late_answer':
            # The native writer has finished before erasure acquires its own
            # lease. Its final row was absent from the earlier snapshot.
            rt.db.append_message('reader','assistant','Late forgettoken final answer.')
        else:
            rt.db._execute_write(lambda db: db.execute('UPDATE messages SET content=? WHERE id=?',
                ('Preserve this newly edited unrelated request.', current['_row_id'])))
    monkeypatch.setattr(rt.owned, '_save', intervene)
    erase(rt)
    first = settle(rt)
    assert changed and first['status'] == 'pending'
    assert any(row['metadata']['kind']=='supplied' for row in rt.owned._rows())
    assert rt.db.search_messages('forgettoken', include_inactive=True)
    if change == 'late_answer':
        assert settle(rt)['status'] == 'settled'
        assert not rt.db.search_messages('forgettoken', include_inactive=True)
    else:
        assert settle(rt)['status'] == 'pending'
        assert rows(rt)[current['_row_id']]['content'] == 'Preserve this newly edited unrelated request.'


@pytest.mark.parametrize('changed_original', [False, True])
def test_native_persisted_input_override_is_distinct_from_request_wrapper(native_runtime, changed_original):
    rt = native_runtime
    original = 'Use the original maintenance request.'
    wrapper = 'Native task wrapper: perform the requested maintenance work.'
    current = {'role':'user', 'content':wrapper}
    # Exact native order: original in the hook, wrapper in its shallow history,
    # then the native writer persists original and stamps the same live row ID.
    rt.memory.observe_native_anchor(rt.scope, [current], user_message=original)
    rt.memory.observe(rt.scope, [current], user_message=original)
    rt.memory.observe_host_input(rt.scope, [current], wrapper, text='Typed source context',
                                 sources=[rt.ref], watermark=0)
    current['api_content'] = wrapper + '\nTyped source context'
    current['_row_id'] = rt.db.append_message('reader', 'user',
        'A different persisted human input.' if changed_original else original, api_content=current['api_content'])
    assert rt.memory._aliases[('owner','reader','read-turn')][1]['content'] == wrapper
    assert rt.memory.native_anchor(rt.scope)['content'] == original
    assert rt.memory.native_anchor(rt.scope)['_row_id'] == current['_row_id']
    assert rt.owned.retain(rt.scope, [rt.ref]) is (not changed_original)
    if changed_original:
        assert not rt.owned._rows()
    else:
        rt.db.append_message('reader','assistant','Derived forgettoken maintenance answer.')
        erase(rt)
        assert settle(rt)['status'] == 'settled'
        persisted = rows(rt)[current['_row_id']]
        assert persisted['content'] == original and persisted['api_content'] is None
        assert not rt.db.search_messages('forgettoken', include_inactive=True)
