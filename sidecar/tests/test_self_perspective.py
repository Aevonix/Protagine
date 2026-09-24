"""Owner source → durable correction → ranking; runtime history remains scoped."""
import json
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from httpx import ASGITransport, AsyncClient
import pytest

from protagine.api.routers import host
from protagine.intelligence.components.preference_learner import PreferenceLearner
from protagine.self_model.perspective import SelfPerspective
from protagine.self_model.store import CompetenceStore, SelfModel
from protagine.turns import TurnIdempotencyLedger
from test_turn_source_evidence import source_app
from onekey import KEY


@pytest.fixture
def perspective(source_app, tmp_path, monkeypatch):
    monkeypatch.setenv('PROTAGINE_OWNER_CONTACT_ID', 'contact-a')
    ledger = TurnIdempotencyLedger(tmp_path / 'turn-idempotency.db')
    perspective = SelfPerspective(ledger, owner_id='contact-a')
    learner = PreferenceLearner(db_path=str(tmp_path / 'old-preferences.db'), perspective=perspective)
    sm = SelfModel(CompetenceStore(str(tmp_path / 'competence.db')))
    sm.perspective = perspective
    monkeypatch.setattr(host, '_preference_learner', learner)
    monkeypatch.setattr(host, '_self_model', sm)
    from protagine.api.middleware import ApiKeyMiddleware
    from onekey import KEY, _principal, _write_keyring
    principals = [_principal(principal=who, secret=who+'-key', viewer=person,
        scopes=['context:read', 'memory:write', 'turns:write'])
        for who, person in [('owner', 'contact-a'), ('guest', 'contact-b')]]
    for principal in principals:
        principal['allow_unscoped_api'] = False
    keys = tmp_path / 'perspective-keys.json'; _write_keyring(keys, principals)
    source_app.add_middleware(ApiKeyMiddleware, api_key=KEY)
    return perspective, learner, sm


async def tell(client, text, turn, *, person='contact-a', occurred='2026-09-05T12:00:00+00:00'):
    response = await client.put('/v2/host/turns/' + turn,
        headers={'Authorization': 'Bearer ' + KEY if person == 'contact-a' else 'Bearer ' + KEY}, json={
        'identity': {'host_id': 'fixture'},
        'context': {'contact_id': person, 'session_id': 's-' + turn, 'turn_id': turn,
                    'channel_id': 'test:' + person, 'metadata': {'occurred_at': occurred}},
        'user_message': {'role': 'user', 'content': text},
    })
    assert response.status_code in {200, 201}, response.text


async def context(client, session, *, headers=None):
    response = await client.post('/v1/host/context/assemble', headers=headers or {'Authorization': 'Bearer ' + KEY}, json={
        'identity': {'host_id': 'fixture'},
        'context': {'contact_id': 'contact-a', 'session_id': session},
        'incoming_message': {'role': 'user', 'content': 'What is your working judgment?'},
    })
    assert response.status_code == 200, response.text
    return '\n'.join(section['body'] for section in response.json()['sections']
                     if section['id'] in {'protagine-owner-preferences', 'protagine-self-perspective'})


@pytest.mark.asyncio
@pytest.mark.parametrize('statement', ['I prefer brief replies.', 'I want brief replies.'])
async def test_first_person_preference_reaches_later_context_and_can_be_forgotten(source_app, perspective, statement):
    state, learner, _ = perspective
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://test') as client:
        await tell(client, statement, 'first-person')
        preferences = state.preferences()
        assert [(p['pref_key'], p['value'], p['source_turn_id']) for p in preferences] == [
            ('communication_style.length', 'short', 'first-person')]
        source_ids = []
        brief = learner.build_brief(source_ids=source_ids)
        assert source_ids == ['first-person']
        assert 'Keep replies short and to the point.' in brief and 'turn:first-person' in brief
        # The later question contains no style terms: the dedicated section
        # must carry this correction independently of semantic recall.
        assert brief in await context(client, 'unrelated-later-session')
        await tell(client, statement, 'first-person')
        assert len(state.preferences(history=True)) == 1

        await tell(client, 'Actually, I prefer detailed replies.', 'first-person-correction',
                   occurred='2026-09-06T12:00:00+00:00')
        correction = state.preferences()[0]
        assert correction['value'] == 'long'
        assert correction['supersedes'] == preferences[0]['id']
        corrected_brief = learner.build_brief()
        assert 'Give thorough, detailed replies.' in corrected_brief
        assert 'turn:first-person-correction' in corrected_brief
        assert corrected_brief in await context(client, 'corrected-later-session')

        erased = await client.post('/v1/host/memory/sources/forget',
            headers={'Authorization': 'Bearer ' + KEY},
            json={'contact_id': 'contact-a', 'source_ids': ['first-person-correction']})
        assert erased.status_code == 200
        assert state.preferences() == []  # Erasure must not reactivate the earlier preference.
        assert learner.build_brief() == ''
        assert await context(client, 'after-forget') == ''


@pytest.mark.asyncio
async def test_ordinary_correction_survives_reopen_and_cannot_be_overwritten_or_resurrected(source_app, perspective, tmp_path, monkeypatch):
    state, learner, sm = perspective
    await learner.learn_directive('be concise')  # a legacy value must not reappear after erase
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://test') as client:
        await tell(client, 'Be concise.', 'early', occurred='2026-09-01T00:00:00+00:00')
        await tell(client, 'Actually be detailed and thorough.', 'correction', occurred='2026-09-03T00:00:00+00:00')
        await tell(client, 'Be brief.', 'late-old', occurred='2026-09-02T00:00:00+00:00')
        await tell(client, 'Be concise.', 'guest', person='contact-b')
        await tell(client, 'Alice said "be concise".', 'quote')
        assert len(state.preferences(history=True)) == 3
        assert state.preferences()[0]['value'] == 'long'
        assert state.preferences()[0]['supersedes'] is not None
        for _ in range(10):
            await learner.learn_from_behavior('clicked_short_response')
        assert await learner.get_preference('communication_style', 'length') == 'long'
        first = await context(client, 'new-model-session')
        assert 'thorough' in first and 'turn:correction' in first
        reopened = SelfPerspective(TurnIdempotencyLedger(state.ledger.db_path), owner_id='contact-a')
        replacement = PreferenceLearner(db_path=str(tmp_path / 'old-preferences.db'), perspective=reopened)
        monkeypatch.setattr(host, '_preference_learner', replacement)
        assert await context(client, 'another-model-session') == first
        erased = await client.post('/v1/host/memory/sources/forget', headers={'Authorization': 'Bearer ' + KEY}, json={'contact_id': 'contact-a', 'source_ids': ['correction']})
        assert erased.status_code == 200
        assert reopened.preferences() == []  # older history is not an active correction
        assert replacement.build_brief() == ''
        await tell(client, 'Be concise.', 'even-older', occurred='2026-08-31T00:00:00+00:00')
        assert reopened.preferences() == []
        with sqlite3.connect(state.ledger.db_path) as conn:
            assert conn.execute('SELECT count(*) FROM self_preference_events WHERE source_turn_id=?', ('correction',)).fetchone()[0] == 0


@pytest.mark.parametrize('text', ['Stop using bullet points.', 'Be concise and detailed.',
    'Use prose rather than bullets.', 'Alice says be formal.', 'Should you be concise?',
    'Use the code example to explain this bug.', 'I prefer brief meetings.',
    'I want detailed answers for this task.', 'I prefer not to use prose.',
    'I said "I prefer brief replies."', 'I want a code example.',
    'I want a spreadsheet.', 'I want prose.', 'I want a list of brief replies.'])
def test_ambiguous_or_reported_directives_remain_evidence(perspective, text):
    state, learner, _ = perspective
    state.ledger.record_source('uncertain', contact_id='contact-a', session_id='s', messages=[{'role': 'user', 'content': text}])
    assert learner.learn_source('uncertain') == []
    assert state.preferences() == []


def test_negative_emoji_directive_keeps_its_polarity(perspective):
    state, learner, _ = perspective
    state.ledger.record_source('emoji', contact_id='contact-a', session_id='s', messages=[{'role': 'user', 'content': "Don't use emoji."}])
    assert learner.learn_source('emoji') == [('communication_style.emoji', 'off')]


def test_status_reports_opinions_and_the_brief_no_longer_carries_them(perspective):
    from test_self_judgments import admit_source, proposal
    state, _, _ = perspective
    state.ledger.record_source('first', contact_id='contact-a', session_id='session-first',
        messages=[{'role': 'user', 'content': 'Long local work lost progress after an interruption.'}])
    admit_source(state.judgments, 'first')
    stance = state.judgments.form(proposal(state.judgments.admitted_premises('first'))).stance_id
    status = state.status()
    assert 'judgments_enabled' not in status
    assert [row['id'] for row in status['judgments']] == [stance]
    assert [row['id'] for row in status['judgment_history']] == [stance]
    assert [row['ref'] for row in status['judgment_processing']] == ['first']
    source_ids = []
    assert state.brief('What is your view on local work checkpoints?', source_ids=source_ids) == ''
    assert source_ids == []
