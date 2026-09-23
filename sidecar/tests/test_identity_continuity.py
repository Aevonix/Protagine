"""Cross-channel corrections must reach active recall and exact transport resolution."""
import json
from types import SimpleNamespace

import pytest

from onekey import KEY

from test_identity_corrections import store
from protagine.identity.participants import ParticipantResolver
from protagine.turns.source_attribution import correct, history
from protagine.turns.idempotency import canonical_turn_digest
from test_turn_source_evidence import source_app


@pytest.mark.asyncio
async def test_exact_corrected_phone_handle_does_not_become_an_unknown_third_person(store):
    first = await store.create(display_name='First')
    second = await store.create(display_name='Second')
    await store.add_handle(first.contact_id, 'sms', '+12125550101', verified=True)
    await store.add_handle(first.contact_id, 'whatsapp', '12125550101@s.whatsapp.net', verified=True)
    await store.correct_handle_identity(operation_id='split-channel', performed_by='owner-fixture',
        gateway='whatsapp', address='12125550101@s.whatsapp.net', expected_contact_id=first.contact_id,
        contact_id=second.contact_id, evidence_refs=['fixture:owner-correction'])
    resolved = await ParticipantResolver(store).resolve(platform='whatsapp', user_id='12125550101@s.whatsapp.net')
    assert resolved.contact_id == second.contact_id
    assert (await store.resolve_messaging_handle('sms', '+12125550101')).contact_id == first.contact_id
    assert len(await store.list()) == 2
    await store.close()
    await store.connect()
    await store.correct_handle_identity(operation_id='reverse-channel', performed_by='owner-fixture',
        gateway='whatsapp', address='12125550101@s.whatsapp.net', expected_contact_id=second.contact_id,
        contact_id=first.contact_id, evidence_refs=['fixture:owner-reversal'])
    assert (await ParticipantResolver(store).resolve(platform='whatsapp', user_id='12125550101@s.whatsapp.net')).contact_id == first.contact_id
    assert (await store.resolve_messaging_handle('rcs', '(212) 555-0101')).contact_id == first.contact_id
    assert len(await store.list()) == 2


@pytest.mark.asyncio
async def test_inflight_extractor_cannot_commit_old_person_interpretation_after_correction(tmp_path):
    import asyncio
    from protagine.turns import TurnIdempotencyLedger
    from protagine.beliefs.source_projection import SourceClaimProjection
    from test_source_claim_projection import Model, claim
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    text = 'My office is in River.'
    ledger.record_source('inflight', contact_id='old-person', session_id='text',
                         messages=[{'role': 'user', 'content': text}])
    entered, release = asyncio.Event(), asyncio.Event()
    class PausedExtractor(Model):
        async def complete(self, *args, **kwargs):
            if not entered.is_set():
                entered.set()
                await asyncio.wait_for(release.wait(), 2)
            return await super().complete(*args, **kwargs)
    task = asyncio.create_task(SourceClaimProjection(ledger).process_one(PausedExtractor({text: claim(text, 'River')})))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        correct(ledger, operation_id='extractor-race', performed_by='owner-fixture',
                old_contact_id='old-person', contact_id='new-person', source_ids=['inflight'],
                evidence_refs=['fixture:correction-during-extraction'])
    finally:
        release.set()
    assert await task
    with ledger._connect() as conn:
        assert conn.execute('SELECT count(*) FROM source_claims').fetchone()[0] == 0
        assert conn.execute("SELECT status FROM source_claim_jobs WHERE turn_id='inflight'").fetchone()[0] == 'pending'
    reopened = TurnIdempotencyLedger(ledger.db_path)
    assert await SourceClaimProjection(reopened).process_one(Model({text: claim(text, 'River')}, model='new-fixture-processor'))
    with reopened._connect() as conn:
        rows = conn.execute('SELECT s.contact_id,j.status,j.model FROM source_claims c '
            'JOIN turn_sources s USING(turn_id) JOIN source_claim_jobs j USING(turn_id)').fetchall()
    assert [tuple(row) for row in rows] == [('new-person', 'complete', 'new-fixture-processor')]
