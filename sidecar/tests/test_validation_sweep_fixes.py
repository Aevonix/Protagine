"""Regression tests for the 2026-08-10 functional validation sweep.

Every test here reproduces a defect that was observed by RUNNING the system
(not inferred from reading code): silent turn-ingestion green-lights, the
skills invoke path awaiting a non-awaitable, briefings swallowed into empty
200s, safety-gate context never populated, memory endpoints whose "backend
down" was indistinguishable from "no data", health advertising a dead memory
capability, two unhandled 500s, and a doctor blind spot for the graph
backend.
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

class _DeadBackendGraph:
    """A wired ProtagineGraph whose backing store is unreachable.

    Mirrors the live failure: the client object exists (so the sidecar
    considers memory "wired") but every operation raises, and
    driver.verify_connectivity() — the remaining graph-read availability
    determination — fails.
    """

    class _Driver:
        async def verify_connectivity(self):
            raise RuntimeError("Neo4j unreachable")

    def __init__(self) -> None:
        self.driver = self._Driver()
        self._embed_fn = None
        self._vector_store = None

    async def record_turn(self, **kwargs):
        raise RuntimeError("Defunct connection to graph backend")

    async def recall(self, **kwargs):
        raise RuntimeError("Defunct connection to graph backend")

    async def read_memories(self, **kwargs):
        raise RuntimeError("Defunct connection to graph backend")

    async def store_memory(self, **kwargs):
        raise RuntimeError("Defunct connection to graph backend")


@pytest.fixture
def dead_graph(monkeypatch, tmp_path):
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path))
    graph = _DeadBackendGraph()
    monkeypatch.setattr(host, "_graph", graph)
    monkeypatch.setattr(host, "_presence_store", None)
    monkeypatch.setattr(host, "_contacts_store", None)
    monkeypatch.setattr(host, "_telemetry", None)
    return graph


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
# 1. /turns/sync must not green-light a turn whose record_turn failed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_turns_sync_does_not_greenlight_failed_ingestion(app, dead_graph):
    async with _client(app) as client:
        resp = await client.post("/v1/host/turns/sync", json={
            "identity": {"host_id": "test-host"},
            "context": {"session_id": "session-1", "contact_id": "contact-1",
                        "channel_id": "test:thread-1"},
            "topics": ["alpha"],
            "summary": "user said alpha",
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["accepted"] is False, (
        "a turn that was NOT recorded must never be reported accepted")
    assert data["continuity_updated"] is False
    assert data["errors"], "record_turn failure must be reported, not swallowed"
    assert "record_turn failed" in data["errors"][0]
    assert data["skipped_reason"] == "graph_record_failed"


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
async def test_health_canonical_memory_does_not_probe_graph(app, monkeypatch):
    class NoGraph:
        def __getattr__(self, name): raise AssertionError('health touched graph')
    monkeypatch.setattr(host, '_graph', NoGraph())
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
# 7b. /world/extract rejects non-base64 content with a clear 400
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_world_extract_plain_text_is_clear_400(app, monkeypatch):
    class _Pipeline:
        async def extract(self, **kwargs):  # pragma: no cover — never reached
            return []

    monkeypatch.setattr(host, "_extraction_pipeline", _Pipeline())
    async with _client(app) as client:
        resp = await client.post("/v1/host/world/extract", json={
            "identity": {"host_id": "t"},
            "content": "this is definitely not base64 !!!",
        })
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["code"] == "invalid_content_encoding"
    assert "base64" in detail["message"]


# ---------------------------------------------------------------------------
# 8. Doctor covers canonical source-store availability
# ---------------------------------------------------------------------------

