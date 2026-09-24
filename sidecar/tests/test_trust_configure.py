"""Host LLM configure: retained router references and refused invalid updates."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from protagine.api.routers import host as host_mod


@pytest.mark.asyncio
async def test_configure_host_preserves_router_references(tmp_path, monkeypatch):
    """A host update reaches retained consumers of the shared router."""
    pytest.importorskip("litellm")
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path))
    from protagine.router.router import LLMRouter
    previous = LLMRouter(tiers={})
    previous.configure({'provider': 'vllm', 'baseUrl': 'http://127.0.0.1:8080/v1',
                        'models': {'small': 'old-neutral'}})
    retained_extractor = previous
    monkeypatch.setattr(host_mod, '_llm_router', previous)
    cfg = {'provider': 'vllm', 'baseUrl': 'http://127.0.0.1:8081/v1',
           'apiKey': 'neutral-key', 'models': {'small': 'new-neutral'}}
    app = FastAPI(); app.include_router(host_mod.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post('/v1/host/configure', json={'identity': {'host_id': 'h'}, 'llm': cfg})
        assert resp.status_code == 200, resp.text
        assert resp.json()['routing']['models']['small']['model_id'] == 'openai/new-neutral'
        assert retained_extractor is host_mod._llm_router
        old_revision = retained_extractor.routing_status()['config_revision']
        cfg['functionRoles'] = {'vision': ['missing-binding']}
        invalid = await client.post('/v1/host/configure', json={'identity': {'host_id': 'h'}, 'llm': cfg})
        assert invalid.status_code == 422
        assert retained_extractor.routing_status()['config_revision'] == old_revision
    persisted = tmp_path / '.protagine-llm-config.json'
    assert json.loads(persisted.read_text())['models']['small'] == 'new-neutral'
    assert persisted.stat().st_mode & 0o077 == 0


@pytest.mark.asyncio
async def test_configure_host_missing_llm_returns_not_configured():
    app = FastAPI()
    app.include_router(host_mod.router)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            "/v1/host/configure",
            json={"identity": {"host_id": "h"}},
        )
        body = resp.json()
        assert body["configured"] is False
