"""Internal initiative executors are not installed host instruction skills."""
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
import pytest

from protagine.api.routers import host
from protagine.skills.registry import SkillRegistry
from protagine.turns import TurnIdempotencyLedger
from test_turn_source_evidence import source_app


@pytest.mark.asyncio
async def test_internal_executor_registry_is_not_advertised_in_turn_context(source_app, tmp_path, monkeypatch):
    registry = SkillRegistry()
    for name in ('behavioral_correction', 'knowledge_acquisition'):
        registry.register(SimpleNamespace(skill_name=name, skill_id=name, __doc__='An internal executor.'))
    monkeypatch.setattr(host, '_skills_registry', registry)
    monkeypatch.setenv('PROTAGINE_RECALL_RERANK', 'off')
    names = registry.list_skills()
    assert 'behavioral_correction' in names and 'knowledge_acquisition' in names
    ledger = TurnIdempotencyLedger(tmp_path / 'turn-idempotency.db')
    ledger.record_source('manual-index', contact_id='contact-a', session_id='earlier',
        messages=[{'role':'user','content':'Use colored index tabs to organize the printed manuals.'}],
        derive_claims=False)
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://test') as client:
        listed = await client.get('/v1/host/skills/registry')
        assert listed.status_code == 200
        assert set(names) == {row['name'] for row in listed.json()['skills']}
        response = await client.post('/v1/host/context/assemble', json={
            'identity':{'host_id':'fixture-host'},
            'context':{'session_id':'later','contact_id':'contact-a'},
            'incoming_message':{'role':'user','content':'How should I organize the printed manuals?'}})
    assert response.status_code == 200, response.text
    sections = response.json()['sections']
    text = '\n'.join(section['body'] for section in sections)
    assert 'colored index tabs' in text  # Other useful context survives.
    assert not any(section['id'] == 'protagine-skills' for section in sections)
    assert not any(name in text for name in names)
