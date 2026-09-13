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
