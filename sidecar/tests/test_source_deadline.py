"""An explicit deadline read follows retained corrections, never a guessed date."""
from contextlib import closing
import json

from httpx import ASGITransport, AsyncClient
import pytest

from pacomind.api.middleware import ApiKeyMiddleware
from pacomind.beliefs.source_projection import SourceClaimProjection
from pacomind.turns import TurnIdempotencyLedger
from test_scoped_api_authority import _principal, _write_keyring
from test_source_claim_projection import Model, claim
from test_turn_source_evidence import source_app


async def add_deadline(ledger, identifier, value, *, operation='assert', contact='contact-a'):
    prefix = 'Correction: ' if operation == 'correct' else ('Now ' if operation == 'change' else '')
    text = prefix + 'The Birch workshop submission deadline is ' + value + '.'
    ledger.record_source(identifier, contact_id=contact, session_id='session-' + identifier,
        messages=[{'role': 'user', 'content': text}], occurred_at='2026-09-13T12:00:00+00:00',
        derive_claims=True)
    model = Model({text: claim(text, value, subject='Birch workshop', predicate='submission deadline',
        memory_kind='decision', operation=operation, match_prior=operation != 'assert')})
    assert await SourceClaimProjection(ledger).process_one(model)
    with closing(ledger._connect()) as conn:
        identifier_row = conn.execute('SELECT id FROM source_claims WHERE turn_id=?', (identifier,)).fetchone()
    assert identifier_row is not None
    return dict(contact_id=contact, session_id='later', claim_id=identifier_row[0], timezone_name='UTC',
        **ledger.source_references([identifier], contact_id=contact, session_id='later')[0])


@pytest.fixture
def deadline_app(source_app, tmp_path):
    keys = tmp_path / 'deadline-keys.json'
    _write_keyring(keys, [
        _principal(principal='reader', secret='read', viewer='contact-a', scopes=['memory:read']),
        _principal(principal='other', secret='other', viewer='contact-b', scopes=['memory:read']),
        _principal(principal='writer', secret='write', viewer='contact-a', scopes=['memory:write'])])
    source_app.add_middleware(ApiKeyMiddleware, keyring_path=str(keys))
    return source_app, TurnIdempotencyLedger(tmp_path / 'turn-idempotency.db')


@pytest.mark.asyncio
async def test_exact_deadline_read_follows_correction_after_reopen_without_changing_claim_dates(deadline_app):
    app, ledger = deadline_app
    original = await add_deadline(ledger, 'original', '16:20 UTC on 22 September 2026')
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://fixture',
                           headers={'Authorization': 'Bearer read'}) as client:
        first = await client.post('/v1/host/memory/sources/deadline', json=original)
        assert first.status_code == 200, first.text
        assert first.json()['deadline_at'] == '2026-09-22T16:20:00+00:00'
        correction = await add_deadline(ledger, 'corrected', '17:05 UTC on 22 September 2026', operation='correct')
        second = await client.post('/v1/host/memory/sources/deadline', json=original)
        assert second.status_code == 200, second.text
    result = second.json()
    assert result['status'] == 'current' and result['deadline_at'] == '2026-09-22T17:05:00+00:00'
    assert result['claim_id'] == correction['claim_id'] and result['source_ref']['source_id'] == 'corrected'
    assert result['original_claim_id'] == original['claim_id']
    assert result['original_source_ref'] == {k: original[k] for k in ('source_id', 'source_version')}
    reopened = SourceClaimProjection(TurnIdempotencyLedger(ledger.db_path))
    assert reopened.deadline(**original) == result
    corrected_selection = reopened.deadline(**{**correction, 'session_id': 'another-channel'})
    assert corrected_selection['root_claim_id'] == result['root_claim_id'] == original['claim_id']
    assert corrected_selection['original_claim_id'] == correction['claim_id']
    assert corrected_selection['source_ref'] == result['source_ref']
    assert corrected_selection['deadline_at'] == result['deadline_at']
    with closing(ledger._connect()) as conn:
        rows = conn.execute('SELECT data_json,valid_from,valid_to,retracted_by FROM source_claims').fetchall()
        assert len(rows) == 2 and sum(bool(row['retracted_by']) for row in rows) == 1
        assert all(row['valid_from'] is None and row['valid_to'] is None
                   and json.loads(row['data_json'])['event_at'] is None for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['forget-original', 'forget-correction', 'annotation-original',
                                    'annotation-correction', 'attribution', 'version', 'claim', 'contact'])
async def test_deadline_never_revives_missing_or_unavailable_binding(deadline_app, mutation):
    _, ledger = deadline_app
    original = await add_deadline(ledger, 'original', '16:20 UTC on 22 September 2026')
    correction = await add_deadline(ledger, 'corrected', '17:05 UTC on 22 September 2026', operation='correct')
    if mutation.startswith('forget'):
        ledger.erase_sources(contact_id='contact-a', turn_ids=['original' if mutation.endswith('original') else 'corrected'])
    elif mutation.startswith('annotation'):
        ref = original if mutation.endswith('original') else correction
        ledger.append_source_annotation(contact_id='contact-a', session_id='later', annotation_id='cancel',
            source_id=ref['source_id'], source_version=ref['source_version'], excerpt='Birch workshop',
            correction='This workshop was cancelled; do not act on the date.', author_principal='operator')
    elif mutation == 'attribution':
        from pacomind.turns.source_attribution import correct
        # The public attribution operation invalidates retained derived claims.
        correct(ledger, operation_id='participant-correction', performed_by='operator',
            source_ids=['corrected'], old_contact_id='contact-a', contact_id='contact-b',
            evidence_refs=['source:participant-correction'])
    else:
        original[{'version': 'source_version', 'claim': 'claim_id', 'contact': 'contact_id'}[mutation]] = (
            '0' * 64 if mutation == 'version' else 'missing')
    result = SourceClaimProjection(ledger).deadline(**original)
    assert result['status'] == 'unavailable' and 'deadline_at' not in result


@pytest.mark.asyncio
@pytest.mark.parametrize('value', ['22 September 2026', 'after approval'])
async def test_imprecise_value_is_not_a_midnight_or_invented_deadline(deadline_app, value):
    _, ledger = deadline_app
    original = await add_deadline(ledger, 'original', value)
    result = SourceClaimProjection(ledger).deadline(**original)
    assert result['status'] == 'unresolved' and result['reason'] == 'deadline_not_instant'
    assert 'deadline_at' not in result and result['value'] == value


@pytest.mark.asyncio
async def test_independent_conflicting_deadline_is_not_a_successor(deadline_app):
    _, ledger = deadline_app
    original = await add_deadline(ledger, 'original', '16:20 UTC on 22 September 2026')
    await add_deadline(ledger, 'competing', '17:05 UTC on 22 September 2026')
    result = SourceClaimProjection(ledger).deadline(**original)
    assert result['status'] == 'unresolved' and result['reason'] == 'conflicting_claims'
    assert 'deadline_at' not in result


@pytest.mark.asyncio
async def test_changed_deadline_follows_exact_successor(deadline_app):
    _, ledger = deadline_app
    original = await add_deadline(ledger, 'original', '16:20 UTC on 22 September 2026')
    change = await add_deadline(ledger, 'changed', '17:05 UTC on 22 September 2026', operation='change')
    result = SourceClaimProjection(ledger).deadline(**original)
    assert result['status'] == 'current' and result['claim_id'] == change['claim_id']
    assert result['deadline_at'] == '2026-09-22T17:05:00+00:00'


@pytest.mark.asyncio
async def test_deadline_retains_all_exact_chain_sources_for_rendered_copy_erasure(deadline_app):
    _, ledger = deadline_app
    first = await add_deadline(ledger, 'first', '16:20 UTC on 22 September 2026')
    middle = await add_deadline(ledger, 'middle', '17:05 UTC on 22 September 2026', operation='correct')
    last = await add_deadline(ledger, 'last', '17:30 UTC on 22 September 2026', operation='correct')
    projection = SourceClaimProjection(ledger)
    result = projection.deadline(**middle)
    assert result['status'] == 'current' and result['root_claim_id'] == first['claim_id']
    assert result['claim_id'] == last['claim_id']
    assert {tuple(sorted(ref.items())) for ref in result['source_refs']} == {
        tuple(sorted((key, bound[key]) for key in ('source_id', 'source_version')))
        for bound in (first, middle, last)}
    assert len(result['source_refs']) == 3
    ledger.erase_sources(contact_id='contact-a', turn_ids=['middle'])
    assert projection.deadline(**last)['status'] == 'unavailable'


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['cycle', 'wrong-prior', 'overflow'])
async def test_deadline_does_not_certify_incomplete_or_invalid_chain(deadline_app, mutation):
    _, ledger = deadline_app
    original = await add_deadline(ledger, 'original', '16:20 UTC on 22 September 2026')
    correction = await add_deadline(ledger, 'corrected', '17:05 UTC on 22 September 2026', operation='correct')
    with closing(ledger._connect()) as conn, conn:
        if mutation == 'cycle':
            conn.execute('UPDATE source_claims SET retracted_by=? WHERE id=?',
                         (original['claim_id'], correction['claim_id']))
        elif mutation == 'wrong-prior':
            conn.execute("UPDATE source_claims SET data_json=json_set(data_json,'$.prior_claim_id','wrong') WHERE id=?",
                         (correction['claim_id'],))
        else:
            # Earlier malformed candidates must not let the bounded reader
            # overlook an independent value beyond its scan limit.
            conn.executemany('''INSERT INTO source_claims SELECT ?,turn_id,'missing-message',subject_key,
                predicate,value_key,data_json,valid_from,valid_to,NULL,NULL FROM source_claims WHERE id=?''',
                [(f'overflow-{i}', original['claim_id']) for i in range(256)])
    result = SourceClaimProjection(ledger).deadline(**original)
    assert result['status'] == ('unresolved' if mutation == 'overflow' else 'unavailable')
    assert 'deadline_at' not in result


@pytest.mark.asyncio
@pytest.mark.parametrize('secret,changes,code', [('other', {}, 403), ('write', {}, 403),
    ('read', {'contact_id': 'contact-b'}, 403), ('read', {'timezone_name': 'invalid/zone'}, 422)])
async def test_deadline_uses_existing_scoped_read_authority(deadline_app, secret, changes, code):
    app, ledger = deadline_app
    original = await add_deadline(ledger, 'original', '16:20 UTC on 22 September 2026')
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://fixture',
                           headers={'Authorization': 'Bearer ' + secret}) as client:
        result = await client.post('/v1/host/memory/sources/deadline', json={**original, **changes})
        assert result.status_code == code, result.text


@pytest.mark.asyncio
async def test_deadline_reads_complete_grounded_event_expression_without_reextracting(deadline_app):
    app, ledger = deadline_app
    text = 'My appointment is 09:30 on 8 October 2026.'
    ledger.record_source('appointment', contact_id='contact-a', session_id='reported',
        messages=[{'role': 'user', 'content': text}], occurred_at='2026-10-04T12:00:00Z',
        derive_claims=True)
    model = Model({text: claim(text, '09:30', predicate='appointment time',
        event_at_text='09:30 on 8 October 2026')})
    projection = SourceClaimProjection(ledger)
    assert await projection.process_one(model)
    with closing(ledger._connect()) as db:
        stored = dict(db.execute('SELECT * FROM source_claims WHERE turn_id=?', ('appointment',)).fetchone())
    data = json.loads(stored['data_json'])
    assert data['value'] == '09:30' and data['event_time']['precision'] == 'instant'
    bound = dict(contact_id='contact-a', session_id='later', claim_id=stored['id'],
        timezone_name='America/New_York',
        **ledger.source_references(['appointment'], contact_id='contact-a', session_id='later')[0])
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://fixture',
                           headers={'Authorization': 'Bearer read'}) as client:
        response = await client.post('/v1/host/memory/sources/deadline', json=bound)
    assert response.status_code == 200
    result = response.json()
    assert result['status'] == 'current'
    assert result['deadline_at'] == '2026-10-08T13:30:00+00:00'
    assert result['deadline_time']['expression'] == '09:30 on 8 October 2026'
    with closing(ledger._connect()) as db:
        assert dict(db.execute('SELECT * FROM source_claims WHERE id=?', (stored['id'],)).fetchone()) == stored
    assert len(model.calls) == 2  # Existing extraction/review only, no replay on read.


async def appointment(ledger, identifier, text, value, event, *, occurred='2026-10-04T23:30:00Z'):
    ledger.record_source(identifier, contact_id='contact-a', session_id='report-'+identifier,
        messages=[{'role': 'user', 'content': text}], occurred_at=occurred, derive_claims=True)
    model = Model({text: claim(text, value, predicate='appointment time', event_at_text=event)})
    assert await SourceClaimProjection(ledger).process_one(model)
    with closing(ledger._connect()) as db:
        row = db.execute('SELECT id FROM source_claims WHERE turn_id=?', (identifier,)).fetchone()
    return dict(contact_id='contact-a', session_id='later', claim_id=row[0], timezone_name='America/New_York',
        **ledger.source_references([identifier], contact_id='contact-a', session_id='later')[0])


@pytest.mark.asyncio
async def test_ordinary_weekday_uses_report_local_day_and_retained_full_expression(deadline_app):
    _, ledger = deadline_app
    quote = 'No, I told you my appointment is 9:30am Monday morning.'
    bound = await appointment(ledger, 'weekday', quote, '9:30am', '9:30am Monday morning')
    projection = SourceClaimProjection(ledger)
    with closing(ledger._connect()) as db:
        before = dict(db.execute('SELECT * FROM source_claims WHERE id=?', (bound['claim_id'],)).fetchone())
        data = json.loads(before['data_json'])
        assert data['event_time'] == {'expression': '9:30am Monday morning', 'status': 'unresolved'}
        # A later import cannot silently shift this source-relative date.
        db.execute("UPDATE turn_sources SET ingested_at='2040-01-01T00:00:00Z' WHERE turn_id='weekday'")
        db.commit()
    result = projection.deadline(**bound)
    assert result['status'] == 'current' and result['deadline_at'] == '2026-10-05T13:30:00+00:00'
    assert result['value'] == '9:30am' and result['evidence'] == quote
    assert result['deadline_time']['basis'] == {
        'rule': 'next_distinct_weekday_from_source_occurrence',
        'observed_at': '2026-10-04T23:30:00+00:00', 'timezone_name': 'America/New_York'}
    # The same report occurred on Monday in Tokyo. This/next Monday is then
    # ambiguous; never shift it seven days merely to produce a future instant.
    local_monday = projection.deadline(**{**bound, 'timezone_name': 'Asia/Tokyo'})
    assert local_monday['status'] == 'unresolved' and 'deadline_at' not in local_monday
    reopened = SourceClaimProjection(TurnIdempotencyLedger(ledger.db_path))
    assert reopened.deadline(**bound) == result
    with closing(ledger._connect()) as db:
        assert dict(db.execute('SELECT * FROM source_claims WHERE id=?', (bound['claim_id'],)).fetchone()) == before
    ledger.append_source_annotation(contact_id='contact-a', session_id='later', annotation_id='withdraw',
        source_id=bound['source_id'], source_version=bound['source_version'], excerpt='appointment',
        correction='This appointment was cancelled.', author_principal='operator')
    assert reopened.deadline(**bound)['status'] == 'unavailable'


@pytest.mark.asyncio
async def test_same_clock_with_different_grounded_days_is_a_deadline_conflict(deadline_app):
    _, ledger = deadline_app
    bound = await appointment(ledger, 'monday', 'My appointment is 9:30am Monday.', '9:30am', '9:30am Monday')
    await appointment(ledger, 'tuesday', 'My appointment is 9:30am Tuesday.', '9:30am', '9:30am Tuesday')
    result = SourceClaimProjection(ledger).deadline(**bound)
    assert result['status'] == 'unresolved' and result['reason'] == 'conflicting_claims'
    assert 'deadline_at' not in result


@pytest.mark.asyncio
@pytest.mark.parametrize('quote,value,event', [
    ('My appointment was 9:30am Monday, last week.', '9:30am', '9:30am Monday'),
    ('My appointment took place 9:30am Monday.', '9:30am', '9:30am Monday'),
    ('My appointment is not 9:30am Monday.', '9:30am', '9:30am Monday'),
    ('My appointment is 9:30pm Monday morning.', '9:30pm', '9:30pm Monday morning'),
    ('My appointment is 9:30am next Monday.', '9:30am', '9:30am next Monday'),
    ('My appointment is 9:30am Monday or Tuesday.', '9:30am', '9:30am Monday'),
    # Separate event operands in the same quotation do not authorize joining
    # this claim's clock to another appointment's date.
    ('My appointment is 9:30am. The delivery is 8am Monday.', '9:30am', '8am Monday'),
    ('My appointment is 9:30am Monday.', '9:30am', 'Monday'),
])
async def test_relative_deadline_never_hides_history_or_borrows_another_event(deadline_app, quote, value, event):
    _, ledger = deadline_app
    bound = await appointment(ledger, 'unresolved', quote, value, event)
    result = SourceClaimProjection(ledger).deadline(**bound)
    assert result['status'] == 'unresolved' and 'deadline_at' not in result


@pytest.mark.parametrize('expression,occurred,zone,expected', [
    ('tomorrow at 9:30am', '2026-10-04T00:30:00Z', 'America/New_York', '2026-10-04T13:30:00+00:00'),
    ('9:30am on 8 October 2026', None, 'Asia/Tokyo', '2026-10-08T00:30:00+00:00'),
    ('12pm on October 8, 2026', None, 'UTC', '2026-10-08T12:00:00+00:00'),
    ('12am on 2026-10-08', None, 'UTC', '2026-10-08T00:00:00+00:00'),
    ('1:30am Sunday', '2026-10-28T12:00:00Z', 'America/New_York', None),
    ('2:30am Sunday', '2026-03-05T12:00:00Z', 'America/New_York', None),
    ('9:30am Monday', None, 'UTC', None),
])
def test_deadline_clock_forms_keep_absolute_relative_and_dst_precision(expression, occurred, zone, expected):
    from pacomind.beliefs.source_time import source_deadline_time, source_event_time
    result = source_deadline_time(expression, observed_at=occurred, timezone_name=zone,
                                  evidence='My appointment is '+expression+'.')
    if expected:
        assert result['status'] == 'resolved' and result['precision'] == 'instant' and result['at'] == expected
    else:
        assert result['status'] == 'unresolved'
    # Deadline interpretation never retroactively rewrites the parser used
    # for past event memories, extraction or state validity.
    assert source_event_time(expression, observed_at=occurred, timezone_name=zone)['status'] == 'unresolved'


@pytest.mark.asyncio
async def test_deadline_clock_frame_reuses_contact_and_agent_settings_with_explicit_override(source_app, tmp_path, monkeypatch):
    from pacomind.api.routers import host
    from pacomind.contacts.config import ContactsConfig
    from pacomind.contacts.store import SQLiteContactStore
    store = SQLiteContactStore(config=ContactsConfig(sqlite_path=':memory:'))
    await store.connect()
    try:
        contact = await store.create(display_name='Appointment owner', trust_tier='trusted')
        monkeypatch.setattr(host, '_contacts_store', store)
        monkeypatch.setenv('PACOMIND_AGENT_TIMEZONE', 'America/New_York')
        keys = tmp_path/'clock-frame-keyring.json'
        _write_keyring(keys, [_principal(principal='clock-reader', secret='read',
            viewer=contact.contact_id, scopes=['memory:read'])])
        source_app.add_middleware(ApiKeyMiddleware, keyring_path=str(keys))
        ledger = TurnIdempotencyLedger(tmp_path/'turn-idempotency.db')
        bound = await add_deadline(ledger, 'clock-frame', '9:30am on 8 October 2026', contact=contact.contact_id)
        bound.pop('timezone_name')
        async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://fixture',
                               headers={'Authorization': 'Bearer read'}) as client:
            agent = (await client.post('/v1/host/memory/sources/deadline', json={**bound, 'timezone_name': None})).json()
            assert agent['timezone_name'] == 'America/New_York' and agent['timezone_basis'] == 'communication_frame'
            assert agent['deadline_at'] == '2026-10-08T13:30:00+00:00'
            await store.set_timezone(contact.contact_id, 'Europe/London')
            local = (await client.post('/v1/host/memory/sources/deadline', json=bound)).json()
            assert local['timezone_name'] == 'Europe/London' and local['timezone_basis'] == 'communication_frame'
            assert local['deadline_at'] == '2026-10-08T08:30:00+00:00'
            explicit = (await client.post('/v1/host/memory/sources/deadline',
                json={**bound, 'timezone_name': 'Asia/Tokyo'})).json()
            assert explicit['timezone_name'] == 'Asia/Tokyo' and explicit['timezone_basis'] == 'caller_override'
            assert explicit['deadline_at'] == '2026-10-08T00:30:00+00:00'
    finally:
        await store.close()
