"""Quoted preferences retain meaning without turning paraphrases into facts."""
import json

from jsonschema import Draft202012Validator, ValidationError
import pytest

from pacomind.beliefs.source_claims import (
    admission_metadata, claim_response_schema, validated_claims,
)
from pacomind.beliefs.source_projection import SourceClaimProjection
from pacomind.self_model.appraisals import AppraisalStore
from pacomind.turns import TurnIdempotencyLedger
from test_source_claim_projection import claim, prepared
from test_source_claim_review import ReviewedModel, review
from test_source_claim_subject_basis import remove_original, rows


TEXT = 'I prefer a concise handoff with a purchasing table.'
CONDITIONAL = (
    'When I coordinate a workshop, I prefer a concise handoff with a purchasing '
    'table showing quantities, prices and the remaining allowance. For an emergency, '
    'include the full safety explanation even if that makes the handoff longer.'
)


def preference(text=TEXT, value=None, **fields):
    proposal = claim(text, value, predicate='handoff format', memory_kind='preference',
        recall_reason='Preserve the chosen handoff format when preparing future workshop plans.', **fields)
    if value is None:
        proposal.pop('value')
        proposal['representation'] = 'preference'
    return proposal


def projection_at(tmp_path):
    return SourceClaimProjection(TurnIdempotencyLedger(tmp_path / 'sources.db'))


async def add(projection, turn, text, proposal, *, keep=True, output=None):
    projection.ledger.record_source(turn, contact_id='owner', session_id='session-' + turn,
        messages=[{'role': 'user', 'content': text}], occurred_at='2026-05-01T10:00:00+00:00')
    model = ReviewedModel(review(keep) if output is None else output,
                          extraction_output=json.dumps([proposal]))
    assert await projection.process_one(model)
    return model


@pytest.mark.asyncio
@pytest.mark.parametrize('direct', [False, True])
async def test_nonliteral_preference_forms_reviewed_statement_and_survives_reopen(tmp_path, direct):
    projection = projection_at(tmp_path)
    # This elision is not a source span. It is discarded, never admitted as
    # a literal value. The alternate explicit form needs no generated value.
    proposal = preference(value=None if direct else 'concise with a purchasing table')
    model = await add(projection, 'original', TEXT, proposal)
    assert len(model.calls) == 2
    reviewed, = model.calls[1][0]['proposals']
    assert model.calls[1][0]['message'] == TEXT
    assert reviewed['claim']['representation'] == 'preference'
    assert reviewed['claim']['value'] == reviewed['claim']['evidence'] == TEXT
    reopened = SourceClaimProjection(TurnIdempotencyLedger(projection.ledger.db_path))
    retained, = reopened.preferences('owner', 'fresh-session')
    assert retained['value'] == TEXT and retained['representation'] == 'preference'
    assert retained['admission_review']['basis'] == 'model_judgment_unverified'
    assert 'source_admission' not in retained
    assert retained['sources'][0]['source_id'] == 'original'
    assert retained['span_start'] == 0 and retained['span_end'] == len(TEXT)
    packet, = prepared(reopened, 'handoff', contact='owner')
    assert packet['assertions'][0]['value'] == packet['assertions'][0]['quote'] == TEXT
    assert packet['assertions'][0]['representation'] == 'preference'
    assert packet['status'] == 'quoted_preference_statements'
    diagnostic, = reopened.status('owner')
    assert diagnostic['diagnostics']['reviewed_count'] == 1
    assert diagnostic['diagnostics']['whole_source_episode_count'] == 0
    view = AppraisalStore(reopened.ledger, owner_id='owner').view(
        'owner', viewer_contact_id='owner', query='handoff')
    assert view['records'][0]['value'] == TEXT
    assert view['records'][0]['text'] == TEXT


@pytest.mark.asyncio
async def test_long_conditional_preference_preserves_exact_bytes_and_review_scope(tmp_path):
    text = '\n  ' + CONDITIONAL + '  \n'
    assert 160 < len(text) <= 500
    projection = projection_at(tmp_path)
    model = await add(projection, 'original', text, preference(text))
    retained = rows(projection)['original']
    assert retained['value'] == retained['evidence'] == text
    assert model.calls[1][0]['proposals'][0]['claim']['value'] == text
    schema = claim_response_schema(text)['schema']
    Draft202012Validator(schema).validate([preference(text)])
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate([preference(text, 'paraphrase', representation='preference')])


@pytest.mark.asyncio
@pytest.mark.parametrize('output', [review(False), '{}', RuntimeError('Review unavailable')])
@pytest.mark.parametrize('direct', [False, True])
async def test_whole_source_preference_has_no_episode_admission_bypass(tmp_path, output, direct):
    projection = projection_at(tmp_path)
    model = await add(projection, 'original', TEXT,
        preference(value=None if direct else 'concise with a purchasing table'), output=output)
    assert len(model.calls) == 2 and rows(projection) == {}
    assert projection.preferences('owner') == []
    assert projection.ledger.search_sources('handoff', contact_id='owner', session_id='later')


@pytest.mark.parametrize('update', [
    {'memory_kind': 'personal_context'}, {'memory_kind': 'procedure'},
    {'representation': 'episode'}, {'subject': 'Invented person'},
    {'evidence': 'I prefer invented source bytes.'},
])
def test_quote_representation_does_not_relax_kind_subject_or_evidence(update):
    assert validated_claims(json.dumps([preference() | update]),
        message=TEXT, prior=[], observed_at=None) == []


def test_nonliteral_facts_still_reject_and_literal_preferences_remain_compact():
    diagnostic = {}
    assert validated_claims(json.dumps([claim(TEXT, 'invented factual value')]),
        message=TEXT, prior=[], observed_at=None, diagnostics=diagnostic) == []
    assert diagnostic['rejection_counts'] == {'value_not_grounded': 1}
    compact, = validated_claims(json.dumps([preference(value='concise handoff')]),
        message=TEXT, prior=[], observed_at=None)
    assert compact['value'] == 'concise handoff' and 'representation' not in compact
    quoted, = validated_claims(json.dumps([preference()]), message=TEXT, prior=[], observed_at=None)
    quoted['source_admission'] = {'version': 'source-episode-admission-v1',
                                  'basis': 'whole_source_quote_unverified'}
    assert admission_metadata(quoted) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('text', [
    'In this fictional scene, I prefer a concise handoff with a purchasing table.',
    'If I were coordinating a workshop, I might prefer a concise handoff with a purchasing table.',
    'Another participant said, "I prefer a concise handoff with a purchasing table."',
])
async def test_semantic_review_can_reject_fiction_tentative_and_wrong_speaker(tmp_path, text):
    projection = projection_at(tmp_path)
    model = await add(projection, 'original', text, preference(text), keep=False)
    assert model.calls[1][0]['message'] == text
    assert rows(projection) == {} and projection.preferences('owner') == []


def test_disavowal_cannot_be_removed_by_selecting_a_preference_quote():
    text = 'This is not a factual claim about me: ' + TEXT
    for proposal in [preference(), preference(value='concise with a purchasing table')]:
        assert validated_claims(json.dumps([proposal]), message=text, prior=[], observed_at=None) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('original_quoted', [False, True])
@pytest.mark.parametrize('operation', ['correct', 'change'])
async def test_explicit_lifecycle_preserves_compact_and_quoted_preference_identity(tmp_path, original_quoted, operation):
    projection = projection_at(tmp_path)
    await add(projection, 'original', TEXT, preference(value=None if original_quoted else 'concise handoff'))
    unrelated = 'I prefer quiet rooms.'
    await add(projection, 'unrelated', unrelated, preference(unrelated) | {'predicate': 'room atmosphere'})
    original = rows(projection)['original']
    text = ('Correction: I prefer detailed handoffs.' if operation == 'correct'
            else 'Starting now, I prefer detailed handoffs.')
    proposal = preference(text, 'detailed' if original_quoted else None,
                          operation=operation, prior_claim_id=original['id'])
    # Restore a provider's clipped passage including the operation cue.
    proposal['evidence'] = 'I prefer detailed handoffs.'
    await add(projection, 'correction', text, proposal)
    found = rows(projection)
    assert found['correction']['prior_claim_id'] == original['id']
    assert found['correction']['predicate'] == original['predicate']
    assert found['correction']['operation'] == operation
    assert found['original']['retracted_by' if operation == 'correct' else 'superseded_by'] == found['correction']['id']
    assert found['correction']['value'] == ('detailed' if original_quoted else text)
    assert found['correction']['evidence'] == text
    assert found['unrelated']['retracted_by'] is None and found['unrelated']['superseded_by'] is None
    assert {row['turn_id'] for row in projection.preferences('owner')} == {'unrelated', 'correction'}


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['correct', 'change'])
async def test_reviewed_quoted_revision_does_not_use_normalized_wording_equality(tmp_path, operation):
    from pacomind.beliefs.source_claims import norm_value
    projection = projection_at(tmp_path)
    old = 'I actually prefer 4-6 pages in a handoff now.'
    new = 'I actually prefer 4.6 pages in a handoff now.'
    assert old != new and norm_value(old) == norm_value(new)
    await add(projection, 'original', old, preference(old))
    original = rows(projection)['original']
    await add(projection, 'correction', new,
        preference(new, operation=operation, prior_claim_id=original['id']))
    found = rows(projection)
    assert found['original']['retracted_by' if operation == 'correct' else 'superseded_by'] == found['correction']['id']
    assert [row['value'] for row in projection.preferences('owner')] == [new]


@pytest.mark.asyncio
async def test_identical_scalar_and_quoted_candidate_bytes_keep_distinct_representations(tmp_path):
    projection = projection_at(tmp_path)
    projection.ledger.record_source('original', contact_id='owner', session_id='session-a',
        messages=[{'role': 'user', 'content': TEXT}])
    model = ReviewedModel(review(True, True),
        extraction_output=json.dumps([preference(value=TEXT), preference()]))
    assert await projection.process_one(model)
    retained = projection.preferences('owner')
    assert len(retained) == 2
    assert {row.get('representation', 'assertion') for row in retained} == {'assertion', 'preference'}
    packet, = prepared(projection, 'handoff', contact='owner')
    assert len(packet['assertions']) == 2 and packet['status'] == 'quoted_preference_statements'


@pytest.mark.asyncio
async def test_partial_preference_correction_cannot_use_subject_basis_as_missing_condition(tmp_path):
    projection = projection_at(tmp_path)
    await add(projection, 'original', CONDITIONAL, preference(CONDITIONAL))
    original = rows(projection)['original']
    text = 'Correction to the workshop handoff preference: a detailed explanation.'
    model = await add(projection, 'correction', text,
        preference(text, operation='correct', prior_claim_id=original['id']), keep=False)
    candidate = model.calls[1][0]['proposals'][0]['claim']
    assert candidate['subject_basis_claim_id'] == original['id']
    assert candidate['value'] == text and 'value_parts' not in candidate
    basis = model.calls[1][0]['prior_assertions'][0]['evidence']
    assert basis == CONDITIONAL
    assert rows(projection)['original']['retracted_by'] is None
    assert [row['value'] for row in projection.preferences('owner')] == [CONDITIONAL]


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['annotate', 'erase', 'reattribute'])
async def test_quoted_preference_guidance_revokes_with_source_after_reopen(tmp_path, action):
    projection = projection_at(tmp_path)
    await add(projection, 'original', TEXT, preference())
    remove_original(projection, action)
    reopened = SourceClaimProjection(TurnIdempotencyLedger(projection.ledger.db_path))
    assert reopened.preferences('owner') == []
    assert AppraisalStore(reopened.ledger, owner_id='owner').view(
        'owner', viewer_contact_id='owner', query='handoff')['records'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('third', ['equivalent', 'different', 'identical'])
async def test_quoted_wording_neither_deduplicates_nor_asserts_conflict(tmp_path, third):
    from datetime import datetime, timezone
    from pacomind.beliefs.source_time import interpret_time_query
    from pacomind.memory.recall import pack_memory_context
    projection = projection_at(tmp_path)
    await add(projection, 'compact', TEXT, preference(value='concise handoff'))
    await add(projection, 'quoted', TEXT, preference())
    text = {'equivalent': 'For my handoff, I prefer a purchasing table and concise prose.',
            'different': 'I prefer detailed handoffs with an appendix.', 'identical': TEXT}[third]
    await add(projection, 'third', text, preference(text))
    packet, = prepared(projection, 'handoff', contact='owner')
    assert len(packet['assertions']) == 3
    assert {row['source'] for row in packet['assertions']} == {'turn:compact', 'turn:quoted', 'turn:third'}
    assert packet['status'] == 'quoted_preference_statements'
    assert packet['comparison_basis'] == 'quoted_preferences_not_compared'
    hits = projection.ledger.search_sources('handoff', contact_id='owner', session_id='fresh')
    _, candidates = projection.prepare_context([], hits, contact_id='owner', session_id='fresh',
        time_query=interpret_time_query('handoff', now=datetime.now(timezone.utc)))
    card, = [row for row in candidates if row.get('content_format') == 'source_assertions_v1']
    assert card['contradiction_count'] is None
    selected, body = pack_memory_context(candidates, max_chars=6000)
    assert selected and '"representation": "preference"' in body
    assert '"comparison_basis": "quoted_preferences_not_compared"' in body
    assert '"contradictions"' not in body


@pytest.mark.asyncio
async def test_quoted_statement_does_not_hide_existing_scalar_conflict(tmp_path):
    projection = projection_at(tmp_path)
    for turn, text, value in [('a', TEXT, 'concise handoff'),
                              ('b', 'I prefer detailed handoffs.', 'detailed handoffs'),
                              ('quoted', TEXT, None)]:
        await add(projection, turn, text, preference(text, value))
    packet, = prepared(projection, 'handoff', contact='owner')
    assert packet['status'] == 'unresolved_conflict'
    assert len(packet['assertions']) == 3
    assert packet['comparison_basis'] == 'quoted_preferences_not_compared'


@pytest.mark.asyncio
async def test_repeated_quoted_witnesses_retain_bounded_history(tmp_path):
    projection = projection_at(tmp_path)
    for index in range(9):
        await add(projection, str(index), TEXT, preference())
    packet, = prepared(projection, 'handoff', contact='owner')
    assert packet['status'] == 'incomplete_assertion_history'
    assert packet['statements_at_least'] == 9 and 'distinct_values_at_least' not in packet
    assert 'assertions' not in packet
