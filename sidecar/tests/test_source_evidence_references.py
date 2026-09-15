"""Current source references select bytes without bypassing admission checks."""
import json

from jsonschema import Draft202012Validator, ValidationError
import pytest

from pacomind.beliefs.source_claims import claim_response_schema, validated_claims
from pacomind.beliefs.source_projection import SourceClaimProjection
from pacomind.turns import TurnIdempotencyLedger
from test_source_claim_projection import claim, prepared
from test_source_claim_review import ReviewedModel, review
from test_source_claim_subject_basis import rows
from test_source_episode_memory import episode


ORIGINAL = ('\nWhen I arrange a ferry crossing, I prefer a window seat and a morning '
            'departure, except on overnight sailings, when I prefer a cabin.\n')
CORRECTION = ('Correction to my ferry crossing preference: when I arrange a ferry '
              'crossing, I prefer a window seat and an afternoon departure, except '
              'on overnight sailings, when I prefer a cabin.\n')


def preference(text, **fields):
    proposal = claim(text, None, predicate='crossing preference', memory_kind='preference',
        representation='preference', recall_reason='Preserve chosen conditions when helping with future plans.')
    proposal.pop('value')
    return proposal | fields


def referenced(proposal):
    return {key: value for key, value in proposal.items() if key != 'evidence'} | {
        'evidence_ref': 'current_message'}


def record(projection, turn, text):
    projection.ledger.record_source(turn, contact_id='owner', session_id='session-' + turn,
        messages=[{'role': 'user', 'content': text}], occurred_at='2026-05-01T10:00:00+00:00')


@pytest.mark.asyncio
async def test_reference_correction_preserves_conditions_actual_lineage_and_reopened_preferences(tmp_path):
    projection = SourceClaimProjection(TurnIdempotencyLedger(tmp_path / 'sources.db'))
    for turn, text in [('original', ORIGINAL), ('correction', CORRECTION)]:
        proposal = preference(text, predicate='crossing choices')
        if turn == 'correction':
            predecessor = rows(projection)['original']
            proposal.update(operation='correct', prior_claim_id=predecessor['id'])
        proposal = referenced(proposal)
        record(projection, turn, text)
        model = ReviewedModel(review(True), extraction_output=json.dumps([proposal]))
        assert await projection.process_one(model)
        assert len(model.calls) == 2
        supplied, options = model.calls[0]
        assert supplied['evidence_refs'] == {
            'current_message': {'source_start': 0, 'source_end': len(text)}}
        Draft202012Validator(options['context']['response_schema']['schema']).validate([proposal])
        reviewed = model.calls[1][0]['proposals'][0]['claim']
        assert reviewed['evidence'] == reviewed['value'] == text
        assert 'evidence_ref' not in reviewed
        assert (reviewed['span_start'], reviewed['span_end']) == (0, len(text))
        if turn == 'correction':
            assert supplied['prior_assertions'][0]['id'] == predecessor['id']
    stored = rows(projection)
    assert stored['original']['retracted_by'] == stored['correction']['id']
    assert stored['correction']['prior_claim_id'] == stored['original']['id']
    assert stored['correction']['operation'] == 'correct'
    reopened = SourceClaimProjection(TurnIdempotencyLedger(projection.ledger.db_path))
    current, = reopened.preferences('owner', 'later-session')
    assert current['value'] == CORRECTION and current['turn_id'] == 'correction'
    assert current['sources'][0]['source_contact_id'] == 'owner'
    assert current['sources'][0]['source_id'] == 'correction'
    assert current['admission_review']['version'] == 'source-claim-review-v1'
    assert 'source_admission' not in current
    packets = prepared(reopened, 'ferry crossing preference', contact='owner')
    recalled = [row for packet in packets for row in packet.get('assertions', [])]
    assert [row['value'] for row in recalled] == [CORRECTION]
    assert recalled[0]['prior_claim_id'] == stored['original']['id']


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['review_rejected', 'erased_during_review'])
async def test_referenced_correction_cannot_retire_predecessor_without_admission(tmp_path, failure):
    projection = SourceClaimProjection(TurnIdempotencyLedger(tmp_path / 'sources.db'))
    record(projection, 'original', ORIGINAL)
    initial = referenced(preference(ORIGINAL))
    assert await projection.process_one(ReviewedModel(review(True), extraction_output=json.dumps([initial])))
    original = rows(projection)['original']
    record(projection, 'correction', CORRECTION)
    proposal = referenced(preference(CORRECTION, operation='correct', prior_claim_id=original['id']))

    async def erase():
        projection.ledger.erase_sources(contact_id='owner', turn_ids=['correction'])

    model = ReviewedModel(review(failure != 'review_rejected'),
        before_review=erase if failure == 'erased_during_review' else None,
        extraction_output=json.dumps([proposal]))
    assert await projection.process_one(model)
    assert len(model.calls) == 2
    assert model.calls[1][0]['proposals'][0]['claim']['evidence'] == CORRECTION
    assert rows(projection) == {'original': original}
    assert projection.preferences('owner')[0]['id'] == original['id']


@pytest.mark.parametrize('text,proposal', [
    ('My office is in Cedar.', claim('My office is in Cedar.', 'Cedar', representation='assertion')),
    ('If the lamp flickers, switch the lamp off.',
     {k: v for k, v in claim('If the lamp flickers, switch the lamp off.', None,
        subject='lamp', predicate='reset procedure', memory_kind='procedure',
        representation='procedure').items() if k != 'value'}),
    (ORIGINAL, preference(ORIGINAL)),
    ('The pump stopped twice during our bench run. Its cause remains unknown.',
     episode('The pump stopped twice during our bench run. Its cause remains unknown.')),
])
def test_reference_schema_resolves_all_existing_representations(text, proposal):
    selected = referenced(proposal)
    schema = claim_response_schema(text)['schema']
    Draft202012Validator(schema).validate([selected])
    resolved, = validated_claims(json.dumps([selected]), message=text, prior=[], observed_at=None)
    literal, = validated_claims(json.dumps([proposal]), message=text, prior=[], observed_at=None)
    assert resolved == literal and resolved['evidence'] == text
    Draft202012Validator(schema).validate([selected | {'evidence': text}])


@pytest.mark.parametrize('fields,reason', [
    ({'evidence_ref': 'unoffered'}, 'evidence_ref_unknown'),
    ({'evidence_ref': None}, 'evidence_ref_unknown'),
    ({'evidence_ref': {'current_message': True}}, 'evidence_ref_unknown'),
    ({'evidence': ORIGINAL.replace('I prefer', 'prefer')}, 'evidence_ref_conflict'),
    ({'evidence': None}, 'evidence_ref_conflict'),
])
def test_unknown_or_conflicting_reference_never_repairs_a_quote(fields, reason):
    proposal = referenced(preference(ORIGINAL)) | fields
    with pytest.raises(ValidationError):
        Draft202012Validator(claim_response_schema(ORIGINAL)['schema']).validate([proposal])
    diagnostic = {}
    assert validated_claims(json.dumps([proposal]), message=ORIGINAL, prior=[],
                            observed_at=None, diagnostics=diagnostic) == []
    assert diagnostic['rejection_counts'] == {reason: 1}
    # Without a selected reference a copying error retains its old failure.
    literal = preference(ORIGINAL) | {'evidence': ORIGINAL.replace('I prefer', 'prefer')}
    diagnostic = {}
    assert validated_claims(json.dumps([literal]), message=ORIGINAL, prior=[],
                            observed_at=None, diagnostics=diagnostic) == []
    assert diagnostic['rejection_counts'] == {'evidence_not_in_source': 1}


@pytest.mark.asyncio
@pytest.mark.parametrize('text', [
    'In a fictional character sketch, I prefer quiet cabins on overnight crossings.',
    'If I took a ferry one day, I might prefer a quiet cabin.',
])
async def test_reference_still_requires_review_of_fiction_and_preference_quality(tmp_path, text):
    projection = SourceClaimProjection(TurnIdempotencyLedger(tmp_path / 'sources.db'))
    record(projection, 'source', text)
    model = ReviewedModel(review(False), extraction_output=json.dumps([referenced(preference(text))]))
    assert await projection.process_one(model)
    assert len(model.calls) == 2
    assert model.calls[1][0]['message'] == text
    assert model.calls[1][0]['proposals'][0]['claim']['evidence'] == text
    assert rows(projection) == {} and projection.preferences('owner') == []
    assert projection.status('owner')[0]['diagnostics']['review_rejected_count'] == 1


def test_reference_does_not_grant_authority_or_replace_value_checks():
    for text, proposal, reason in [
        ('I grant permission to make purchases.', preference('I grant permission to make purchases.'),
         'sensitive_evidence'),
        ('My office is in Cedar.', claim('My office is in Cedar.', 'Maple'), 'value_not_grounded'),
    ]:
        diagnostic = {}
        assert validated_claims(json.dumps([referenced(proposal)]), message=text, prior=[],
                                observed_at=None, diagnostics=diagnostic) == []
        assert diagnostic['rejection_counts'] == {reason: 1}


@pytest.mark.asyncio
async def test_long_text_keeps_bounded_literal_passages_and_offers_no_reference(tmp_path):
    text = 'An unrelated observation. ' * 24 + 'I prefer quiet cabins.'
    projection = SourceClaimProjection(TurnIdempotencyLedger(tmp_path / 'sources.db'))
    record(projection, 'source', text)
    model = ReviewedModel(review(True), extraction_output=json.dumps([preference('I prefer quiet cabins.')]))
    assert await projection.process_one(model)
    assert 'evidence_refs' not in model.calls[0][0]
    assert rows(projection)['source']['evidence'] == 'I prefer quiet cabins.'
    schema = model.calls[0][1]['context']['response_schema']['schema']
    proposal = referenced(preference(text))
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate([proposal])
    diagnostic = {}
    assert validated_claims(json.dumps([proposal]), message=text, prior=[],
                            observed_at=None, diagnostics=diagnostic) == []
    assert diagnostic['rejection_counts'] == {'evidence_ref_unknown': 1}


@pytest.mark.asyncio
async def test_audio_reference_cannot_select_rendered_message_or_widen_segment_lineage(tmp_path):
    from test_source_audio_claims import AudioModel, claims, record as record_audio

    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    text = 'My office is in Cedar.'
    _, rendered = record_audio(ledger, text)
    projection = SourceClaimProjection(ledger)
    proposal = referenced(claim(text, 'Cedar'))
    model = AudioModel({rendered: proposal})
    assert await projection.process_one(model)
    assert len(model.calls) == 1 and claims(ledger) == []
    assert 'evidence_refs' not in model.calls[0][0]
    schema = model.calls[0][1]['context']['response_schema']['schema']
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate([proposal])
    assert projection.status('person')[0]['diagnostics']['rejection_counts'] == {'evidence_ref_unknown': 1}
