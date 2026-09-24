"""Regression tests for the 2026-08-10 functional validation sweep.

Every test here reproduces a defect that was observed by RUNNING the system
(not inferred from reading code): the skills invoke path awaiting a
non-awaitable, briefings swallowed into empty 200s, safety-gate context never
populated, memory endpoints whose "backend down" was indistinguishable from
"no data", health advertising a dead memory capability and two unhandled 500s.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from protagine.api.routers import host


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    app.include_router(host.router)
    return app


def _client(app: FastAPI, **kwargs) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, **kwargs), base_url="http://test",
    )


# ---------------------------------------------------------------------------
# 3. /briefings returns stored briefings and surfaces store failures
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_briefings_returned_and_failures_surface(app, monkeypatch):
    from protagine.briefings.models import (
        Briefing, BriefingSection, BriefingType,
    )

    briefing = Briefing(
        briefing_type=BriefingType.DAILY,
        sections=[BriefingSection(name="tasks",
                                  narrative="Two tasks due today.")],
    )

    class _Engine:
        def get_recent(self, limit=10):
            return [briefing]

    monkeypatch.setattr(host, "_briefings_engine", _Engine())
    async with _client(app) as client:
        resp = await client.get("/v1/host/briefings")
        assert resp.status_code == 200
        items = resp.json()["briefings"]
        assert len(items) == 1, (
            "stored briefings must be returned, not an empty success")
        assert items[0]["id"] == briefing.briefing_id
        assert items[0]["briefing_type"] == "daily"
        assert "Two tasks due today." in items[0]["body"]

        class _Broken:
            def get_recent(self, limit=10):
                raise RuntimeError("briefing store exploded")

        monkeypatch.setattr(host, "_briefings_engine", _Broken())
        resp = await client.get("/v1/host/briefings")
        assert resp.status_code == 500, (
            "a store failure must surface, not become 200 []")


# ---------------------------------------------------------------------------
# 5. Memory endpoints: backend-down must be distinguishable from "no data"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_reports_canonical_memory(app, monkeypatch):
    async with _client(app) as client:
        response = await client.get('/v1/host/health')
    assert response.status_code == 200, response.text
    assert 'memory' in response.json()['capabilities']
    assert 'Canonical source ledger readable' in response.json()['notes']['memory']


@pytest.mark.asyncio
async def test_health_does_not_claim_unavailable_canonical_memory(app, monkeypatch):
    from protagine import turns
    def unavailable(*args): raise OSError('fixture unavailable')
    monkeypatch.setattr(turns, 'get_turn_idempotency_ledger', unavailable)
    async with _client(app) as client:
        response = await client.get('/v1/host/health')
    assert response.status_code == 200
    data = response.json()
    assert 'memory' not in data['capabilities']
    assert data['status'] == 'degraded'
    assert 'Canonical source ledger unavailable' in data['notes']['memory']


# ---------------------------------------------------------------------------
# 8. Doctor covers canonical source-store availability
# ---------------------------------------------------------------------------

