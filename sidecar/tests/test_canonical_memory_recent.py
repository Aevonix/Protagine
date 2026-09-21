"""Recent conversations use occurrence and exact evidence, not matching old questions."""
import json
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
import pytest

from protagine.api.middleware import ApiKeyMiddleware
from protagine.api.routers import host
from protagine.contacts.comms import CommsLog
from protagine.memory.recent import read_recent, MAX_CONTENT
from protagine.turns import TurnIdempotencyLedger
from protagine.turns.idempotency import source_message_hash
from test_scoped_api_authority import _principal, _write_keyring
from test_turn_source_evidence import source_app


@pytest.fixture
def ledger(tmp_path):
    return TurnIdempotencyLedger(tmp_path/'turn-idempotency.db')


def add(ledger, identifier, text, *, at='2026-08-02T12:00:00+00:00',
        person='person', session='chat', channel='whatsapp:conversation', scope='person',
        messages=None):
    return ledger.record_source(identifier, contact_id=person, session_id=session, scope=scope,
        messages=messages or [{'role': 'user', 'content': text},
                              {'role': 'assistant', 'content': 'We finished ' + text}],
        occurred_at=at, channel_id=channel, derive_claims=False)


def recent(ledger, **kwargs):
    return read_recent(ledger, **{'contact_id':'person', 'session_id':'call',
                                  'platform':'whatsapp', **kwargs})


def content(packet):
    return '\n'.join(entry['content'] for entry in packet['entries'])


def test_latest_exchange_not_old_matching_question_and_limits_are_chronological(ledger, monkeypatch):
    add(ledger, 'old-question', 'What was the last thing we worked on?', at='2026-07-01T00:00:00Z')
    add(ledger, 'middle', 'the boat inventory', at='2026-08-01T00:00:00Z')
    add(ledger, 'latest', 'the packing checklist')
    add(ledger, 'other-platform', 'an unrelated call', channel='voice:room', at='2026-08-03T00:00:00Z')
    monkeypatch.setattr(ledger, 'search_sources', lambda *a, **kw: pytest.fail('semantic/lexical query'))
    packet = recent(ledger, limit=2)
    assert [entry['source_id'] for entry in packet['entries']] == ['middle', 'latest']
    assert 'What was the last' not in content(packet) and 'unrelated call' not in content(packet)
    assert 'USER: the packing checklist' in content(packet)
    assert 'ASSISTANT: We finished the packing checklist' in content(packet)
    assert all(entry['roles'] == ['user', 'assistant'] for entry in packet['entries'])
    assert packet['coverage']['reasons'] == ['result_limit']
    assert packet['source_refs'] == ledger.source_references(['latest', 'middle'], contact_id='person', session_id='call')


def test_channel_capture_does_not_change_versions_and_cannot_rebind(ledger):
    add(ledger, 'source', 'the map', channel=None)
    original = ledger.source_references(['source'], contact_id='person', session_id='call')
    assert add(ledger, 'source', 'the map') is False
    assert ledger.source_references(['source'], contact_id='person', session_id='call') == original
    assert add(ledger, 'source', 'the map') is False
    with pytest.raises(ValueError, match='source_channel_conflict'):
        add(ledger, 'source', 'the map', channel='sms:elsewhere')
    assert recent(ledger)['entries'][0]['conversation_id'] == 'whatsapp:conversation'


def test_forget_purges_only_fully_deleted_channel_locators(ledger):
    add(ledger, 'parent', 'the source inventory')
    refs = ledger.source_references(['parent'], contact_id='person', session_id='chat')
    add(ledger, 'survivor', '', messages=[{'role':'user','content':'An independent request.'},
        {'role':'assistant','content':'A source-derived answer.', '_supplied_sources':refs}])
    ledger.erase_sources(contact_id='person', turn_ids=['parent'])
    with ledger._connect() as db:
        rows = db.execute('SELECT turn_id FROM source_channels').fetchall()
    assert [row[0] for row in rows] == ['survivor']
    assert 'independent request' in content(recent(ledger))
    ledger.erase_sources(contact_id='person', turn_ids=['survivor'])
    with ledger._connect() as db:
        assert db.execute('SELECT count(*) FROM source_channels').fetchone()[0] == 0
    # A predecessor application deleting its canonical row also fires cleanup.
    add(ledger, 'predecessor', 'another source')
    with ledger._connect() as db:
        db.execute("DELETE FROM turn_sources WHERE turn_id='predecessor'")
        assert db.execute('SELECT count(*) FROM source_channels').fetchone()[0] == 0


def test_attribution_correction_transfers_channel_lookup_with_canonical_owner(ledger):
    from protagine.turns.source_attribution import correct
    add(ledger, 'source', 'a correctly attributed conversation')
    version = recent(ledger)['source_refs'][0]['source_version']
    correct(ledger, operation_id='correct-person', performed_by='fixture-admin',
        old_contact_id='person', contact_id='guest', source_ids=['source'],
        evidence_refs=['reviewed-fixture'])
    assert recent(ledger)['entries'] == []
    corrected = recent(ledger, contact_id='guest')
    assert corrected['entries'][0]['source_id'] == 'source'
    assert corrected['entries'][0]['source_version'] == version
    assert add(ledger, 'source', 'a correctly attributed conversation') is False
    assert recent(ledger)['entries'] == []
    assert recent(ledger, contact_id='guest')['entries'][0]['source_version'] == version
    with ledger._connect() as db:
        assert db.execute("SELECT contact_id FROM source_channels WHERE turn_id='source'").fetchone()[0] == 'guest'


def test_assistant_only_legacy_source_reports_missing_channel_coverage(ledger):
    add(ledger, 'reply', '', channel=None, messages=[{'role':'assistant','content':'A retained reply.'}])
    packet = recent(ledger)
    assert packet['entries'] == []
    assert packet['coverage']['status'] == 'partial'
    assert 'legacy_channel_metadata_incomplete' in packet['coverage']['reasons']


@pytest.mark.parametrize('predecessor', [False, True])
def test_reviewed_import_orders_by_occurrence_and_numeric_message_id(ledger, predecessor):
    for identifier, ordinal, at in [('old-import', 99, '2025-01-01T00:00:00Z'),
                                    ('reply-100', 100, '2026-08-02T12:00:00Z'),
                                    ('input-99', 99, '2026-08-02T12:00:00Z')]:
        add(ledger, identifier, '', channel=None, at=at, messages=[{
            'role': 'assistant' if ordinal == 100 else 'user', 'content': identifier,
            'provenance': {'kind':'hermes_history', 'actor_basis':'reviewed_direct_session',
                           'platform':'whatsapp', 'chat_id':'direct-chat', 'message_id':ordinal}}])
    if predecessor:
        with ledger._connect() as db:
            db.execute('DELETE FROM source_channels')
    packet = recent(ledger, limit=2)
    assert [entry['source_id'] for entry in packet['entries']] == ['input-99', 'reply-100']
    assert all(entry['timestamp_basis'] == 'recorded_occurrence' for entry in packet['entries'])
    assert 'provenance' not in content(packet) and 'old-import' not in content(packet)
    assert all(entry['conversation_id'] == 'whatsapp:direct-chat' for entry in packet['entries'])


def test_exact_scope_unknown_times_and_background_are_not_latest_conversation(ledger):
    add(ledger, 'person', 'the shared plan')
    add(ledger, 'other', 'foreign private words', person='other')
    add(ledger, 'session', 'session private words', session='original', scope='session')
    add(ledger, 'unknown', 'unknown import time', at=None)
    add(ledger, 'background', 'a background task', channel='protagine_task:person')
    packet = recent(ledger)
    assert [entry['source_id'] for entry in packet['entries']] == ['person']
    assert 'unknown_occurrence_time' in packet['coverage']['reasons']
    assert 'session private words' in content(recent(ledger, session_id='original'))
    assert recent(ledger, platform='protagine_task')['entries'] == []


def test_correction_receipts_are_current_and_erase_never_falls_back_to_old_copies(ledger):
    add(ledger, 'report', 'Friday at nine')
    first = recent(ledger)
    expected = first['source_refs'][0]
    note = ledger.append_source_annotation(contact_id='person', session_id='call', annotation_id='correction',
        **expected, excerpt='Friday at nine', correction='The day is supported; the time is unverified.',
        author_principal='person')
    corrected = recent(ledger)
    assert corrected['entries'][0]['source_version'] == expected['source_version']
    assert corrected['entries'][0]['read_revision'] != first['entries'][0]['read_revision']
    assert 'time is unverified' in content(corrected)
    assert {ref['source_id'] for ref in corrected['source_refs']} == {'report', note['source_id']}
    assert corrected['annotation_checks'][0]['annotation_ids'] == [note['source_id']]
    assert set(corrected['annotation_checks'][0]['message_hashes']) == {'report', note['source_id']}
    ledger.erase_sources(contact_id='person', turn_ids=['report'])
    erased = recent(ledger)
    assert erased['entries'] == [] and erased['source_refs'] == [] and erased['watermark'] > 0


def test_ordinary_input_linked_reply_remains_a_conversation_and_keeps_its_parent(ledger):
    message = {'role':'user', 'content':'The fixture lamp is violet.'}
    add(ledger, 'input', '', messages=[message])
    add(ledger, 'reply', '', at='2026-08-02T12:00:01Z', messages=[{
        'role':'assistant', 'content':'The lamp has a purple finish.',
        '_supplied_inputs':[{'source_id':'input', 'input_message_hash':source_message_hash('chat', message)}]}])
    packet = recent(ledger)
    assert [entry['source_id'] for entry in packet['entries']] == ['input', 'reply']
    assert 'ASSISTANT: The lamp has a purple finish.' in content(packet)
    assert packet['coverage']['status'] == 'complete'
    assert {ref['source_id'] for ref in packet['annotation_checks'][0]['source_refs']} == {'input', 'reply'}
    ledger.erase_sources(contact_id='person', turn_ids=['input'])
    assert recent(ledger)['entries'] == []


def test_source_linked_comms_is_locator_only_and_rejects_stale_or_unlinked(ledger, tmp_path):
    add(ledger, 'legacy-live', 'the actual canonical exchange', channel=None)
    log = CommsLog(str(tmp_path/'comms.db'), source_ledger=ledger)
    lineage, _ = log.source_input('legacy-live', 'person')
    log.log('person', channel='whatsapp:chat', direction='out', summary='WRONG SUMMARY',
            source_lineage=lineage, ts='2099-01-01T00:00:00Z')
    log.log('person', channel='whatsapp:chat', direction='out', summary='UNLINKED PRIVATE COPY')
    packet = recent(ledger, comms_log=log)
    assert 'actual canonical exchange' in content(packet)
    assert 'WRONG' not in content(packet) and 'UNLINKED' not in content(packet)
    assert packet['entries'][0]['occurred_at'] == '2026-08-02T12:00:00+00:00'
    with log._conn:
        bad = dict(lineage, message_hashes=['0'*64])
        log._conn.execute('UPDATE communications SET source_lineage_json=? WHERE source_lineage_json IS NOT NULL',
                          (json.dumps(bad),))
    assert recent(ledger, comms_log=log)['entries'] == []
    ledger.erase_sources(contact_id='person', turn_ids=['legacy-live'])
    assert recent(ledger, comms_log=log)['entries'] == []


def test_different_live_channel_cannot_be_overridden_by_an_old_comms_locator(ledger, tmp_path):
    add(ledger, 'source', 'the SMS conversation', channel='sms:chat')
    log = CommsLog(str(tmp_path/'comms.db'), source_ledger=ledger)
    lineage, _ = log.source_input('source', 'person')
    log.log('person', channel='whatsapp:old', source_lineage=lineage)
    assert recent(ledger, comms_log=log)['entries'] == []
    assert 'SMS conversation' in content(recent(ledger, platform='sms', comms_log=log))


def test_aggregate_content_bound_is_truthful_and_corrections_are_never_clipped(ledger):
    add(ledger, 'long', 'large detail ' * 2000)
    packet = recent(ledger)
    assert len(content(packet)) == MAX_CONTENT
    assert packet['entries'][0]['complete'] is False
    assert 'content_limit' in packet['coverage']['reasons']
    ref = packet['source_refs'][0]
    ledger.append_source_annotation(contact_id='person', session_id='call', annotation_id='note',
        **ref, excerpt='large detail', correction='The asserted details were not verified.', author_principal='person')
    packet = recent(ledger)
    assert packet['entries'] == [] and packet['source_refs'] == []
    assert packet['coverage']['status'] == 'partial' and 'content_limit' in packet['coverage']['reasons']


def test_erasure_during_read_fails_closed(ledger, monkeypatch):
    import protagine.memory.recent as module
    add(ledger, 'source', 'the current plan')
    original = module.current_candidates
    calls = []
    def checked(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            ledger.erase_sources(contact_id='person', turn_ids=['source'])
        return original(*args, **kwargs)
    monkeypatch.setattr(module, 'current_candidates', checked)
    with pytest.raises(ValueError, match='changed_during_read'):
        recent(ledger)


@pytest.fixture
def recent_app(source_app, tmp_path, ledger):
    path = tmp_path/'keys.json'
    _write_keyring(path, [_principal(principal=person, secret=person, viewer=person,
        scopes=['memory:read', 'turns:write']) for person in ('person', 'guest')]
        + [_principal(principal='no-memory', secret='no-memory', viewer='person', scopes=['context:read'])])
    source_app.add_middleware(ApiKeyMiddleware, keyring_path=str(path))
    return source_app


def body(**changes):
    return {'identity':{'host_id':'fixture'}, 'person_id':'person', 'session_id':'call',
            'platform':'whatsapp', **changes}


@pytest.mark.asyncio
async def test_http_scope_schema_and_real_annotation_receipt(recent_app, ledger):
    add(ledger, 'source', 'the current plan')
    async with AsyncClient(transport=ASGITransport(app=recent_app), base_url='http://test',
                           headers={'Authorization':'Bearer person'}) as client:
        result = await client.post('/v1/host/memory/recent', json=body())
        assert result.status_code == 200, result.text
        packet = result.json()
        async def check(packet):
            response = await client.post('/v1/host/memory/sources/erasures', json={
                'contact_id':'person', 'session_id':'call', 'after':packet['watermark'],
                'source_refs':packet['source_refs'], 'annotation_checks':packet['annotation_checks']})
            assert response.status_code == 200, response.text
            return response.json()
        assert (await check(packet))['annotation_checks_current'] == [True]
        ledger.append_source_annotation(contact_id='person', session_id='call', annotation_id='note',
            **packet['source_refs'][0], excerpt='current plan', correction='The plan was revised.', author_principal='person')
        assert (await check(packet))['annotation_checks_current'] == [False]
        corrected = (await client.post('/v1/host/memory/recent', json=body())).json()
        assert (await check(corrected))['annotation_checks_current'] == [True]
        for changes in ({'person_id':'guest'},):
            assert (await client.post('/v1/host/memory/recent', json=body(**changes))).status_code == 403
        for changes in ({'limit':21}, {'limit':'8'}, {'query':'last thing'}, {'session_id':' '}, {'platform':'WhatsApp'}):
            assert (await client.post('/v1/host/memory/recent', json=body(**changes))).status_code == 422
        denied = await client.post('/v1/host/memory/recent', json=body(), headers={'Authorization':'Bearer no-memory'})
        assert denied.status_code == 403
        guest = await client.post('/v1/host/memory/recent', json=body(person_id='guest'), headers={'Authorization':'Bearer guest'})
        assert guest.status_code == 200 and guest.json()['entries'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('channel', ['whatsapp:chat', 'WhatsApp:chat', 'WHATSAPP:chat'])
async def test_live_turn_capture_preserves_channel_in_source_only_path(source_app, ledger, channel):
    payload = {'identity':{'host_id':'fixture'}, 'context':{'contact_id':'person', 'session_id':'chat',
        'channel_id':channel, 'turn_id':'live', 'metadata':{'occurred_at':'2026-08-02T12:00:00Z'}},
        'user_message':{'role':'user','content':'The lantern inventory.'},
        'assistant_message':{'role':'assistant','content':'The list is complete.'}, 'source_only':True}
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://test') as client:
        response = await client.put('/v2/host/turns/source-survivors/live', json=payload)
        assert response.status_code in {200,201}, response.text
    packet = recent(ledger)
    assert packet['entries'][0]['conversation_id'] == 'whatsapp:chat'
    assert 'USER: The lantern inventory.' in content(packet)


@pytest.mark.asyncio
async def test_recent_read_does_not_hold_request_event_loop(recent_app, ledger, monkeypatch):
    import asyncio
    import threading
    from protagine.memory import recent as module
    entered, release = threading.Event(), threading.Event()
    worker_threads = []
    original = module.read_recent
    add(ledger, 'source', 'the current plan')
    def slow_read(*args, **kwargs):
        worker_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(2), 'event loop could not release the storage reader'
        return original(*args, **kwargs)
    monkeypatch.setattr(module, 'read_recent', slow_read)
    async with AsyncClient(transport=ASGITransport(app=recent_app), base_url='http://test',
                           headers={'Authorization':'Bearer person'}) as client:
        pending = asyncio.create_task(client.post('/v1/host/memory/recent', json=body()))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            assert worker_threads == [worker_threads[0]]
            assert worker_threads[0] != threading.get_ident()
            assert not pending.done()
        finally:
            release.set()
        response = await pending
        assert response.status_code == 200, response.text
        assert response.json()['entries'][0]['source_id'] == 'source'


@pytest.mark.asyncio
async def test_derived_channel_uses_resolved_sender_instead_of_stale_contact(source_app, ledger, monkeypatch):
    from protagine.identity.participants import ParticipantResolver
    async def stale_gateway(*args, **kwargs):
        return 'sms:stale-person'
    async def resolve(*args, **kwargs):
        return SimpleNamespace(contact_id='person', method='verified_handle', created=False)
    monkeypatch.setattr(host, '_ensure_channel_id', stale_gateway)
    monkeypatch.setattr(host, '_contacts_store', SimpleNamespace())
    monkeypatch.setattr(ParticipantResolver, 'resolve', resolve)
    observed = []
    monkeypatch.setattr(host, '_observe_channel', observed.append)
    payload = {'identity':{'host_id':'fixture'}, 'context':{'contact_id':'stale-person', 'session_id':'chat',
        'turn_id':'resolved', 'metadata':{'occurred_at':'2026-08-02T12:00:00Z'}},
        'sender':{'platform':'whatsapp','user_id':'fixture-handle'},
        'user_message':{'role':'user','content':'A conversation from the actual sender.'}, 'source_only':True}
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://test') as client:
        response = await client.put('/v2/host/turns/source-survivors/resolved', json=payload)
        assert response.status_code in {200,201}, response.text
    assert recent(ledger)['entries'][0]['conversation_id'] == 'whatsapp:person'
    assert recent(ledger, platform='sms')['entries'] == []
    assert observed == ['whatsapp:person']
