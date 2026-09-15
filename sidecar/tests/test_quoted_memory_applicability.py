"""Quoted conditions remain useful without certifying when they apply."""
from datetime import datetime
import json

from jsonschema import Draft202012Validator
import pytest

from pacomind.beliefs.source_claims import SourceClaimOutputError, claim_response_schema, validated_claims
from pacomind.beliefs.source_projection import SourceClaimProjection
from pacomind.beliefs.source_time import MemoryTimeQuery
from pacomind.memory.recall import render_memory_context
from pacomind.self_model.appraisals import AppraisalStore
from pacomind.turns import TurnIdempotencyLedger
from pacomind.turns.source_read import read
from test_source_claim_projection import claim, prepared
from test_source_claim_subject_basis import remove_original, rows
from test_source_quoted_preferences import add, preference, projection_at


def timestamp(value):
    return datetime.fromisoformat(value).timestamp()


@pytest.mark.parametrize('kind', ['preference', 'procedure'])
def test_quote_schema_does_not_require_scalar_temporal_projection(kind):
    text = 'When the room is occupied, I prefer quiet ventilation.'
    proposal = preference(text) | {'representation': kind, 'memory_kind': kind}
    for name in ('valid_from_text', 'valid_to_text', 'event_at_text'):
        proposal.pop(name)
    validator = Draft202012Validator(claim_response_schema(text)['schema'])
    validator.validate({'claims': [proposal]})
    proposal.update(valid_from_text=None, valid_to_text=None, event_at_text=None)
    validator.validate({'claims': [proposal]})


def test_dynamic_source_schema_has_strict_object_boundaries():
    def check(node):
        if isinstance(node, dict):
            if node.get('type') == 'object':
                assert node['additionalProperties'] is False
                assert set(node['required']) == set(node['properties'])
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    for message, segments in [('I prefer quiet ventilation.', None), ('Long source. ' * 60, None),
            ('I prefer quiet ventilation.', [{'source_start': 0, 'source_end': 27}])]:
        for prior in ([], [{'id': 'episode:first', 'representation': 'episode'},
                           {'id': 'preference:first', 'representation': 'preference'}]):
            schema = claim_response_schema(message, audio_segments=segments, prior=prior)['schema']
            assert schema['type'] == 'object' and set(schema['properties']) == {'claims'}
            check(schema)


def test_claim_envelope_and_bare_array_have_identical_semantics():
    text = 'I prefer quiet ventilation.'
    proposal = preference(text)
    claims = validated_claims(json.dumps([proposal]), message=text, prior=[], observed_at=None)
    assert validated_claims(json.dumps({'claims': [proposal]}),
        message=text, prior=[], observed_at=None) == claims
    assert validated_claims('{"claims": []}', message=text, prior=[], observed_at=None) == []
    for malformed in ({'claims': [], 'commentary': 'extra'}, {'claims': None}, {'claims': {}},
                      {'claims': [None]}, {'claims': [proposal] * 7}, {}):
        with pytest.raises(SourceClaimOutputError, match='invalid_claim_array_shape'):
            validated_claims(json.dumps(malformed), message=text, prior=[], observed_at=None)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['preference', 'procedure'])
async def test_unresolved_quoted_condition_reaches_review_recall_and_context(tmp_path, monkeypatch, kind):
    text = ('I prefer decaffeinated tea after 18:00 because caffeine keeps me awake.' if kind == 'preference'
            else 'When the room is occupied, I open the ventilation panel only after checking the indicator.')
    expression = 'after 18:00' if kind == 'preference' else 'When the room is occupied'
    proposal = preference(text, valid_from_text=expression) | {'representation': kind, 'memory_kind': kind}
    projection = projection_at(tmp_path)
    from test_source_claim_review import ReviewedModel, review
    projection.ledger.record_source('original', contact_id='owner', session_id='session-original',
        messages=[{'role': 'user', 'content': text}], occurred_at='2026-05-01T10:00:00+00:00')
    model = ReviewedModel(review(True), extraction_output=json.dumps({'claims': [proposal]}))
    assert await projection.process_one(model)
    assert len(model.calls) == 2
    reviewed = model.calls[1][0]['proposals'][0]['claim']
    assert reviewed['value'] == reviewed['evidence'] == text
    assert reviewed['valid_from'] is None
    assert reviewed['applicability']['status'] == 'unresolved'
    assert reviewed['applicability']['unresolved_time_expressions'] == [expression]
    reopened = SourceClaimProjection(TurnIdempotencyLedger(projection.ledger.db_path))
    packet, = prepared(reopened, 'tea' if kind == 'preference' else 'ventilation', contact='owner')
    assert packet['assertions'][0]['value'] == text
    assert packet['assertions'][0]['applicability']['status'] == 'unresolved'
    assert packet['status'] == ('quoted_preference_statements' if kind == 'preference' else 'source_assertion')
    ref, = reopened.ledger.source_references(['original'], contact_id='owner', session_id='later')
    history = read(reopened.ledger, **ref, contact_id='owner', session_id='later', view='assertions',
                   claim_id=packet['assertions'][0]['claim_id'])
    historic, = json.loads(history['content'])['assertions']
    assert historic['evidence'] == text and historic['applicability'] == reviewed['applicability']
    if kind == 'preference':
        view = AppraisalStore(reopened.ledger, owner_id='owner').view('owner', viewer_contact_id='owner', query='tea')
        record, = view['records']
        assert record['text'] == text and record['sources']
        assert record['status'] == 'statement' and record['governing'] is False
        assert record['applicability']['status'] == 'unresolved'
        from pacomind.api.routers import social_state
        monkeypatch.setattr(social_state, 'appraisal_store', lambda: AppraisalStore(reopened.ledger, owner_id='owner'))
        body, refs = social_state.appraisal_context(contact_id='owner', session_id='later', query='tea')
        assert text in body and 'applicability unresolved; source conditions not evaluated' in body and refs
        assert 'when the current context supports its conditions' in body
        assert 'exceptions and connected requirements' in body
    diagnostic, = reopened.status('owner')
    assert diagnostic['diagnostics']['review_kept_count'] == 1


@pytest.mark.asyncio
async def test_unresolved_quoted_change_does_not_invent_effective_now(tmp_path):
    projection = projection_at(tmp_path)
    old = 'I prefer a concise handoff.'
    await add(projection, 'original', old, preference(old))
    original = rows(projection)['original']
    new = 'Starting when I coordinate an emergency, I prefer detailed handoffs.'
    await add(projection, 'future', new, preference(new, operation='change', prior_claim_id=original['id'],
              valid_from_text='when I coordinate an emergency'))
    found = rows(projection)
    assert found['future']['operation'] == 'change' and found['future']['prior_claim_id'] == original['id']
    assert found['future']['valid_from'] is None
    assert found['original']['superseded_by'] is None and found['original']['valid_to'] is None
    assert {row['turn_id'] for row in projection.preferences('owner')} == {'original', 'future'}
    assert all(row['applicability']['status'] == 'unresolved' for row in projection.preferences('owner'))


@pytest.mark.asyncio
async def test_future_preference_change_keeps_predecessor_until_effective_date(tmp_path):
    projection = projection_at(tmp_path)
    old = 'I prefer quiet handoffs.'
    await add(projection, 'original', old, preference(old))
    original = rows(projection)['original']
    new = 'Starting 2026-06-01, I prefer spoken handoffs until 2026-07-01.'
    await add(projection, 'future', new, preference(new, operation='change', prior_claim_id=original['id'],
              valid_from_text='2026-06-01', valid_to_text='2026-07-01'))
    assert [r['turn_id'] for r in projection.preferences('owner', now=timestamp('2026-05-20T12:00:00+00:00'))] == ['original']
    assert [r['turn_id'] for r in projection.preferences('owner', now=timestamp('2026-06-20T12:00:00+00:00'))] == ['future']
    assert projection.preferences('owner', now=timestamp('2026-07-02T12:00:00+00:00')) == []
    projection.ledger.erase_sources(contact_id='owner', turn_ids=['future'])
    assert projection.preferences('owner', now=timestamp('2026-06-20T12:00:00+00:00')) == []


@pytest.mark.asyncio
async def test_future_change_does_not_extend_an_expired_predecessor(tmp_path):
    projection = projection_at(tmp_path)
    old = 'I prefer quiet handoffs until 2026-05-15.'
    await add(projection, 'original', old, preference(old, valid_to_text='2026-05-15'))
    new = 'Starting 2026-06-01, I prefer spoken handoffs.'
    await add(projection, 'future', new, preference(new, operation='change',
        prior_claim_id=rows(projection)['original']['id'], valid_from_text='2026-06-01'))
    assert rows(projection)['original']['valid_to'] == '2026-05-15T00:00:00+00:00'
    assert [r['turn_id'] for r in projection.preferences('owner', now=timestamp('2026-05-10T12:00:00+00:00'))] == ['original']
    assert projection.preferences('owner', now=timestamp('2026-05-20T12:00:00+00:00')) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['preference', 'procedure'])
async def test_complete_source_context_keeps_unknown_applicability(tmp_path, kind):
    quote = 'I prefer quiet ventilation.' if kind == 'preference' else 'I open the ventilation panel.'
    text = quote + ' If the alarm sounds, keep the panel closed instead.'
    projection = projection_at(tmp_path)
    await add(projection, 'original', text,
              preference(quote) | {'representation': kind, 'memory_kind': kind})
    hits = projection.ledger.search_sources('ventilation', contact_id='owner', session_id='later')
    _, candidates = projection.prepare_context([], hits, contact_id='owner', session_id='later',
        time_query=MemoryTimeQuery())
    selected, = candidates
    assert selected['content'] == text and 'content_format' not in selected
    body = render_memory_context(candidates)
    assert text in body and '"validity_status": "unknown"' in body


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['erase', 'annotate', 'reattribute'])
async def test_unresolved_statement_still_obeys_source_revocation(tmp_path, action):
    projection = projection_at(tmp_path)
    text = 'I prefer quiet ventilation after the inspection.'
    await add(projection, 'original', text, preference(text, valid_from_text='after the inspection'))
    assert len(projection.preferences('owner')) == 1
    remove_original(projection, action)
    assert projection.preferences('owner') == []
    assert AppraisalStore(projection.ledger, owner_id='owner').view(
        'owner', viewer_contact_id='owner', query='ventilation')['records'] == []


@pytest.mark.asyncio
async def test_quoted_correction_retracts_without_certifying_its_condition(tmp_path):
    projection = projection_at(tmp_path)
    old = 'I prefer quiet handoffs.'
    await add(projection, 'original', old, preference(old))
    new = 'Correction: I prefer spoken handoffs only after the inspection.'
    proposal = preference(new, operation='correct', prior_claim_id=rows(projection)['original']['id'],
                          valid_from_text='after the inspection')
    await add(projection, 'rejected', new, proposal, keep=False)
    assert rows(projection)['original']['retracted_by'] is None
    await add(projection, 'correction', new, proposal)
    found = rows(projection)
    assert found['original']['retracted_by'] == found['correction']['id']
    retained, = projection.preferences('owner')
    assert retained['turn_id'] == 'correction' and retained['valid_from'] is None
    assert retained['applicability']['status'] == 'unresolved'
    projection.ledger.erase_sources(contact_id='owner', turn_ids=['correction'])
    assert projection.preferences('owner') == []


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['valid_range', 'observed_range'])
async def test_dated_query_retains_statement_without_certifying_query_time(tmp_path, mode):
    projection = projection_at(tmp_path)
    text = 'I prefer quiet ventilation after the inspection.'
    await add(projection, 'original', text, preference(text, valid_from_text='after the inspection'))
    hits = projection.ledger.search_sources('ventilation', contact_id='owner', session_id='later')
    _, candidates = projection.prepare_context([], hits, contact_id='owner', session_id='later',
        time_query=MemoryTimeQuery(mode=mode, start='2026-06-01T00:00:00+00:00', end='2026-06-02T00:00:00+00:00'))
    selected, = candidates
    assert selected['validity_status'] == 'unknown'
    body = render_memory_context(candidates)
    assert text in body and '"status": "unresolved"' in body


def test_scalar_and_ungrounded_date_rejections_remain_strict():
    text = 'After the inspection, my office is in River.'
    diagnostic = {}
    assert validated_claims(json.dumps([claim(text, 'River', valid_from_text='After the inspection')]),
        message=text, prior=[], observed_at='2026-05-01T10:00:00+00:00', diagnostics=diagnostic) == []
    assert diagnostic['rejection_counts'] == {'invalid_date': 1}
    text = 'I prefer quiet ventilation.'
    assert validated_claims(json.dumps([preference(text, valid_from_text='invented date')]),
        message=text, prior=[], observed_at=None) == []
