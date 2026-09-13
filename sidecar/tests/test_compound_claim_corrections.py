"""Partial updates retain quoted support for each unchanged part of a value."""
import json

import pytest

from pacomind.beliefs.source_projection import SourceClaimProjection
from pacomind.turns import TurnIdempotencyLedger
from pacomind.turns.source_attribution import correct as reattribute
from test_source_claim_projection import Model, claim, prepared

ORIGINAL = 'The loaner kit needs four glove pairs and two nylon brushes.'
CORRECTION = ('Correction: the loaner kit now needs six glove pairs instead of four. '
              'The brush count stays the same.')
LATER = ('Correction: the loaner kit needs three nylon brushes instead of two. '
         'The glove count stays the same.')


def records(projection):
    with projection.ledger._connect() as db:
        return {row['turn_id']: {**json.loads(row['data_json']), **dict(row)}
                for row in db.execute('SELECT * FROM source_claims')}


async def add(projection, turn, text, value, *, subject='loaner kit', predicate='needs',
              occurred='2026-03-02T09:00:00+00:00', **fields):
    projection.ledger.record_source(turn, contact_id='contact-a', session_id='session-'+turn,
        messages=[{'role': 'user', 'content': text}], occurred_at=occurred)
    model = Model({text: claim(text, value, subject=subject, predicate=predicate, **fields)})
    assert await projection.process_one(model)
    return model


@pytest.mark.asyncio
async def test_partial_correction_preserves_unchanged_support_across_revisions_and_restart(tmp_path):
    projection = SourceClaimProjection(TurnIdempotencyLedger(tmp_path/'sources.db'))
    await add(projection, 'original', ORIGINAL, 'four glove pairs and two nylon brushes')
    first = await add(projection, 'correction', CORRECTION, 'six glove pairs and two nylon brushes',
                      operation='correct', match_prior=True)
    before = records(projection)
    assert 'correction' in before, 'A grounded partial correction was silently discarded'
    assert len(first.calls) == 2, 'Compound corrections still require the existing semantic review'
    assert before['original']['retracted_by'] == before['correction']['id']
    await add(projection, 'later', LATER, 'six glove pairs and three nylon brushes',
              operation='correct', match_prior=True)
    rows = records(projection)
    assert rows['correction']['retracted_by'] == rows['later']['id']
    reopened = SourceClaimProjection(TurnIdempotencyLedger(projection.ledger.db_path))
    packet, = prepared(reopened, 'loaner kit needs')
    current, = packet['assertions']
    assert current['value'] == 'six glove pairs and three nylon brushes'
    bases = current['value_basis']
    assert {row['turn_id'] for row in bases} == {'original', 'correction'}
    assert any(row['evidence'] == ORIGINAL for row in bases)
    assert any(row['evidence'] == CORRECTION for row in bases)
    hits = reopened.ledger.search_sources('loaner kit needs', contact_id='contact-a', session_id='s')
    from pacomind.beliefs.source_time import interpret_time_query
    from datetime import datetime, timezone
    _, bundles = reopened.prepare_context([], hits, contact_id='contact-a', session_id='s',
        time_query=interpret_time_query('loaner kit needs', now=datetime.now(timezone.utc)))
    bundle, = [row for row in bundles if row.get('atomic_evidence')]
    assert set(bundle['source_turn_ids']) == {'original', 'correction', 'later'}


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['erase', 'reattribute', 'annotate'])
async def test_revoked_value_source_cannot_supply_a_current_composite(action, tmp_path):
    projection = SourceClaimProjection(TurnIdempotencyLedger(tmp_path/'sources.db'))
    await add(projection, 'original', ORIGINAL, 'four glove pairs and two nylon brushes')
    await add(projection, 'correction', CORRECTION, 'six glove pairs and two nylon brushes',
              operation='correct', match_prior=True)
    await add(projection, 'later', LATER, 'six glove pairs and three nylon brushes',
              operation='correct', match_prior=True)
    assert 'later' in records(projection)
    if action == 'erase':
        projection.ledger.erase_sources(contact_id='contact-a', turn_ids=['correction'])
    elif action == 'reattribute':
        reattribute(projection.ledger, operation_id='change-speaker', performed_by='operator',
            old_contact_id='contact-a', contact_id='contact-b', source_ids=['correction'],
            evidence_refs=['operator:correction'])
    else:
        source, = projection.ledger.source_references(['correction'], contact_id='contact-a', session_id='s')
        projection.ledger.append_source_annotation(contact_id='contact-a', session_id='s',
            annotation_id='disputed-count', **source, excerpt=CORRECTION,
            correction='The glove quantity was misreported; its replacement is unknown.', author_principal='operator')
    assert not prepared(projection, 'loaner kit needs')
    # Erasure invalidates derived claims but does not erase the owner's other messages.
    assert projection.ledger.source_references(['later'], contact_id='contact-a', session_id='s')
    if action != 'annotate':
        assert 'later' not in records(projection)


@pytest.mark.asyncio
@pytest.mark.parametrize('fields', [
    {'operation': 'assert', 'match_prior': True},
    {'operation': 'correct', 'prior_claim_id': 'claim:unoffered'},
    {'operation': 'correct', 'match_prior': True, 'value': 'six glove pairs and two steel brushes'},
    {'operation': 'correct', 'match_prior': True, 'predicate': 'inspection needs'},
])
async def test_nonliteral_new_facts_and_unsupported_changes_still_fail(fields, tmp_path):
    projection = SourceClaimProjection(TurnIdempotencyLedger(tmp_path/'sources.db'))
    await add(projection, 'original', ORIGINAL, 'four glove pairs and two nylon brushes')
    value = fields.get('value', 'six glove pairs and two nylon brushes')
    proposal = claim(CORRECTION, value, subject='loaner kit', predicate='needs',
                     **{k: v for k, v in fields.items() if k not in {'value', 'predicate'}})
    if 'predicate' in fields:
        proposal['predicate'] = fields['predicate']
    projection.ledger.record_source('correction', contact_id='contact-a', session_id='s',
        messages=[{'role':'user', 'content':CORRECTION}])
    assert await projection.process_one(Model({CORRECTION: proposal}))
    assert set(records(projection)) == {'original'}


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['reject', 'erase', 'competing_update'])
async def test_review_and_current_predecessor_still_control_commit(outcome, tmp_path):
    projection = SourceClaimProjection(TurnIdempotencyLedger(tmp_path/'sources.db'))
    await add(projection, 'original', ORIGINAL, 'four glove pairs and two nylon brushes')

    class ChangingReview(Model):
        async def complete(self, messages, **kwargs):
            response = await super().complete(messages, **kwargs)
            if kwargs.get('context', {}).get('task') == 'source_claim_review':
                if outcome == 'reject':
                    decisions = json.loads(response.content)
                    for item in decisions.values():
                        item.update(keep=False, reason='The claimed update is not supported.')
                    response.content = json.dumps(decisions)
                elif outcome == 'erase':
                    projection.ledger.erase_sources(contact_id='contact-a', turn_ids=['original'])
                else:
                    await add(projection, 'competing', 'Correction: the loaner kit needs eight glove pairs.',
                              'eight glove pairs', operation='correct', match_prior=True)
            return response

    projection.ledger.record_source('correction', contact_id='contact-a', session_id='s',
        messages=[{'role': 'user', 'content': CORRECTION}])
    model = ChangingReview({CORRECTION: claim(CORRECTION, 'six glove pairs and two nylon brushes',
        subject='loaner kit', predicate='needs', operation='correct', match_prior=True)})
    assert await projection.process_one(model)
    assert len(model.calls) == 2, 'The grounded candidate must reach the semantic review'
    assert 'correction' not in records(projection)
    assert projection.ledger.source_references(['correction'], contact_id='contact-a', session_id='s')


@pytest.mark.asyncio
async def test_carried_relative_date_retains_its_original_reporting_clock(tmp_path):
    projection = SourceClaimProjection(TurnIdempotencyLedger(tmp_path/'sources.db'))
    old = 'The flight departs tomorrow at 8pm.'
    new = 'Correction: the flight departs at 11am; the day stays the same.'
    await add(projection, 'original', old, 'tomorrow at 8pm', subject='flight', predicate='departs',
              occurred='2026-03-02T09:00:00+00:00')
    await add(projection, 'correction', new, 'tomorrow at 11am', subject='flight', predicate='departs',
              occurred='2026-03-03T09:00:00+00:00', operation='correct', match_prior=True)
    current, = prepared(projection, 'flight departs')[0]['assertions']
    basis, = current['value_basis']
    assert current['reported_at'] == '2026-03-03T09:00:00+00:00'
    assert basis['reported_at'] == '2026-03-02T09:00:00+00:00'
    assert basis['recorded_at'] and basis['evidence'] == old
    assert basis['timezone'] == 'UTC'
    assert current['event_time']['status'] == 'unknown'
