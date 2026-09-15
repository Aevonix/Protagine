"""Clipped correction quotes keep their exact short source before review."""
import json
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
import pytest

from protagine.beliefs.source_projection import SourceClaimProjection
from protagine.turns import TurnIdempotencyLedger
from test_procedure_source_context import ProcedureModel
from test_source_claim_projection import claim, ingest, prepared
from test_turn_source_evidence import source_app


ORIGINAL = ('The loaner kit label is LK-72. It needs four glove pairs and one nylon brush; '
            'pickup is at the west workbench.')
CORRECTION = ('Small correction: the loaner kit now needs six glove pairs instead of four; '
              'pickup moves to the north shelving unit. The label and brush count stay the same.')


def stored(ledger):
    with ledger._connect() as db:
        return [{**json.loads(row['data_json']), **dict(row)} for row in db.execute(
            'SELECT id,data_json,turn_id,retracted_by,superseded_by,valid_to FROM source_claims')]


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['correct', 'change'])
async def test_clipped_explicit_proposal_retires_only_its_exact_predecessors(
        source_app, tmp_path, operation):
    # These controlled responses demonstrate a validator path, not recovered
    # output from W11 (its pre-validation model draft was not retained).
    projection = SourceClaimProjection(TurnIdempotencyLedger(tmp_path/'turn-idempotency.db'))
    model = ProcedureModel({
        ORIGINAL: [claim(ORIGINAL, value, subject=subject, predicate=predicate)
            for subject, predicate, value in [('loaner kit', 'label', 'LK-72'),
                ('glove pairs', 'quantity', 'four'), ('nylon brush', 'count', 'one'),
                ('pickup', 'location', 'west workbench')]],
        CORRECTION: [
            claim('the loaner kit now needs six glove pairs instead of four', 'six',
                subject='glove pairs', predicate='quantity', operation=operation, match_prior=True),
            claim('pickup moves to the north shelving unit', 'north shelving unit',
                subject='pickup', predicate='location', operation=operation, match_prior=True)],
    })
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://test') as client:
        await ingest(client, 'original-kit', ORIGINAL)
        assert await projection.process_one(model)
        before = {row['predicate']: row for row in stored(projection.ledger)}
        await ingest(client, 'kit-correction', CORRECTION, occurred='2026-03-02T09:00:00+00:00')
        assert await projection.process_one(model)
    review = model.calls[-1][0]
    assert review['message'] == CORRECTION
    assert all(p['claim']['evidence'] == CORRECTION and p['claim']['operation'] == operation
               for p in review['proposals']), review
    after = {row['id']: row for row in stored(projection.ledger)}
    for predicate in ('quantity', 'location'):
        previous = before[predicate]
        latest, = [row for row in after.values()
                   if row['turn_id'] == 'kit-correction' and row['predicate'] == predicate]
        assert latest['prior_claim_id'] == previous['id']
        assert latest['evidence'] == CORRECTION and latest['span_start'] == 0
        assert latest['span_end'] == len(CORRECTION)
        retired = after[previous['id']]
        if operation == 'correct':
            assert retired['retracted_by'] == latest['id'] and retired['superseded_by'] is None
        else:
            assert retired['superseded_by'] == latest['id'] and retired['retracted_by'] is None
            assert retired['valid_to'] == latest['valid_from'] == '2026-03-02T09:00:00+00:00'
    for predicate in ('label', 'count'):
        assert after[before[predicate]['id']] == before[predicate]
    reopened = SourceClaimProjection(TurnIdempotencyLedger(projection.ledger.db_path))
    # The selected current source remains independently available after reopen.
    current = [c for packet in prepared(reopened, 'pickup glove pairs loaner kit')
               for c in packet.get('assertions', [])]
    assert {'north shelving unit', 'six'} <= {c['value'] for c in current}, current
    assert not any(c['value'] in ('west workbench', 'four') for c in current)
    assert not await reopened.process_one(model)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['assert', 'correct'])
async def test_prior_id_or_unrelated_correction_cue_does_not_retire_claims(tmp_path, operation):
    ledger = TurnIdempotencyLedger(tmp_path/'sources.db')
    old = 'The pickup location is the west workbench.'
    new = ('Correction: the independent meter label is ML-3. '
           'Nora reports the pickup location is the north shelving unit.')

    class ScopeReview(ProcedureModel):
        async def complete(self, messages, **kwargs):
            payload = json.loads(messages[-1]['content'])
            if kwargs['context']['task'] == 'source_claim_review' and payload['message'] == new:
                self.calls.append((payload, kwargs))
                assert payload['message'] == new
                # A full-source semantic judgment remains necessary. This
                # correction cue belongs to a different property's report.
                return SimpleNamespace(model_id=self.model, content=json.dumps({'0': {
                    'keep': operation == 'assert',
                    'reason': 'The pickup report is independent; only the meter label is corrected.'}}))
            return await super().complete(messages, **kwargs)

    model = ScopeReview({old: [claim(old, 'west workbench', subject='pickup', predicate='location')],
        new: [claim('the pickup location is the north shelving unit', 'north shelving unit',
            subject='pickup', predicate='location', operation=operation, match_prior=True)]})
    projection = SourceClaimProjection(ledger)
    ledger.record_source('old', contact_id='person', session_id='earlier',
        messages=[{'role':'user', 'content':old}], occurred_at='2026-03-01T09:00:00+00:00')
    assert await projection.process_one(model)
    original, = stored(ledger)
    ledger.record_source('new', contact_id='person', session_id='later',
        messages=[{'role':'user', 'content':new}], occurred_at='2026-03-02T09:00:00+00:00')
    assert await projection.process_one(model)
    rows = stored(ledger)
    assert next(row for row in rows if row['id'] == original['id']) == original
    assert len(rows) == (2 if operation == 'assert' else 1)
    if operation == 'assert':
        latest, = [row for row in rows if row['turn_id'] == 'new']
        assert latest['operation'] == 'assert' and latest['prior_claim_id'] == original['id']


@pytest.mark.asyncio
async def test_audio_correction_cannot_borrow_a_neighboring_segment_cue(tmp_path):
    from test_source_audio_claims import AudioModel, audio, retained, source_text
    ledger = TurnIdempotencyLedger(tmp_path/'sources.db')
    old = 'My office is in River.'
    projection = SourceClaimProjection(ledger)
    ledger.record_source('old-office', contact_id='person', session_id='text',
        messages=[{'role':'user','content':old}], occurred_at='2026-09-08T12:00:00+00:00')
    assert await projection.process_one(AudioModel({old: claim(old, 'River')}))
    previous, = stored(ledger)
    original = audio('Correction: the independent meter label is ML-3.')
    segments = original['content'][1]['segments']
    segments[0]['end_ms'] = 40
    text = 'Nora reports my office is in Lake.'
    segments.append({'start_ms':50, 'end_ms':100, 'text':text})
    ledger.record_source('audio', contact_id='person', session_id='voice', messages=[original],
        occurred_at='2026-09-09T12:00:00+00:00')
    rendered = source_text(retained(ledger)['content'])
    model = AudioModel({rendered: claim('my office is in Lake', 'Lake',
                                      operation='correct', match_prior=True)})
    assert await projection.process_one(model)
    rows = stored(ledger)
    assert next(row for row in rows if row['id'] == previous['id']) == previous
    latest, = [row for row in rows if row['turn_id'] == 'audio']
    assert latest['evidence'] == text and latest['operation'] == 'assert'
    assert latest['evidence_basis']['segment']['segment_index'] == 1
    assert 'Correction:' not in latest['evidence']
