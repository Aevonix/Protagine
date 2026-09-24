"""Tests for the Protagine sidecar — import checks, API endpoints, setup wizard."""

from __future__ import annotations


import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def app():
    """Create a fresh sidecar app for each test."""
    from protagine.server import create_app
    return create_app()


@pytest_asyncio.fixture
async def client(app):
    """Async HTTP client wired to the ASGI app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


# ---------------------------------------------------------------------------
# Import checks — every wired subsystem
# ---------------------------------------------------------------------------

SUBSYSTEMS = [
    ("protagine.intelligence.synthesis.connection_discoverer", "ConnectionDiscoverer"),
    ("protagine.goals.store", "GoalStore"),
    ("protagine.briefings.engine", "BriefingEngine"),
    ("protagine.mind", "Mind"),
    ("protagine.research.pipeline", "ResearchPipeline"),
    ("protagine.contacts.store", "ContactStore"),
    ("protagine.vector.embedder", "EmbeddingPipeline"),
]


@pytest.mark.parametrize("module,cls", SUBSYSTEMS, ids=[s[1] for s in SUBSYSTEMS])
def test_subsystem_import(module, cls):
    """Every subsystem should import without errors."""
    import importlib
    mod = importlib.import_module(module)
    assert hasattr(mod, cls), f"{module} has no {cls}"


# ---------------------------------------------------------------------------
# Server + OpenAPI
# ---------------------------------------------------------------------------

def test_create_app():
    from protagine.server import create_app
    app = create_app()
    assert app.title == "Protagine"


def test_openapi_spec_export():
    from protagine.server import create_app
    app = create_app()
    spec = app.openapi()
    assert "paths" in spec
    assert "components" in spec
    schemas = spec.get("components", {}).get("schemas", {})
    paths = spec.get("paths", {})
    assert len(schemas) >= 50, f"Only {len(schemas)} schemas"
    assert len(paths) >= 25, f"Only {len(paths)} paths"


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/v1/host/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "capabilities" in data
    assert "notes" in data


# ---------------------------------------------------------------------------
# Memory stubs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_memory_read_requires_exact_source(client):
    resp = await client.post("/v1/host/memory/read", json={
        "identity": {"host_id": "test"},
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_memory_search_requires_scope(client):
    resp = await client.post("/v1/host/memory/search", json={
        "identity": {"host_id": "test"},
        "query": "hello", "person_id": "person", "session_id": "session",
    })
    assert resp.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize('method,path', [('post','write'),('post','flush'),('post','reconcile'),('get','status')])
async def test_retired_graph_memory_routes_are_absent(client, method, path):
    resp = await client.request(method, '/v1/host/memory/' + path,
                                json={'identity': {'host_id': 'test'}})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_memory_embed_not_wired(client):
    resp = await client.post("/v1/host/memory/embed", json={
        "identity": {"host_id": "test"},
        "inputs": ["hello"],
    })
    assert resp.status_code == 501


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_context_assemble_empty(client):
    resp = await client.post("/v1/host/context/assemble", json={
        "identity": {"host_id": "test"},
        "context": {"session_id": "s1", "contact_id": "c1"},
        "incoming_message": {"role": "user", "content": "hello"},
    })
    assert resp.status_code == 200
    assert "sections" in resp.json()


@pytest.mark.asyncio
async def test_enriched_context(client):
    resp = await client.post("/v1/host/context/enriched", json={
        "identity": {"host_id": "test"},
        "context": {"session_id": "s1", "contact_id": "c1"},
        "message": "hello",
    })
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_signals_route_is_retired(client):
    """The Neo4j-only signal collector and its ingest route are gone (build plan M6): no plugin
    posted it, and the agent's affect reads the appraisal outcomes instead."""
    import importlib
    from protagine.api.routers import host
    resp = await client.post("/v1/host/signals/ingest", json={
        "identity": {"host_id": "test"},
        "context": {"session_id": "s1", "contact_id": "c1"},
    })
    assert resp.status_code == 404
    for module in ("protagine.intelligence.mind_model.signal_collector",
                   "protagine.intelligence.mind_model.graph_baseline"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module)
    assert "signals" not in host.supported_capabilities()
    for name in ("set_signal_collector", "_signal_collector", "_attribute_signal_contact", "_LooseMessage"):
        assert not hasattr(host, name), name


# ---------------------------------------------------------------------------
# Turns sync
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_turns_sync(client):
    resp = await client.post("/v1/host/turns/sync", json={
        "identity": {"host_id": "test"},
        "context": {"session_id": "s1", "contact_id": "c1"},
        "topics": ["test"],
        "entities": [],
        "tools_used": [],
    })
    assert resp.status_code == 200
    assert resp.json()["accepted"] is True


# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_goals_not_wired(client):
    resp = await client.get("/v1/host/goals")
    assert resp.status_code == 501
    assert resp.json()["detail"] == {
        "error": {"code": "not_wired", "message": "Backend not configured"},
    }


@pytest.mark.asyncio
async def test_get_goal_not_found(client):
    resp = await client.get("/v1/host/goals/nonexistent")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_contacts_empty(client):
    resp = await client.get("/v1/host/contacts")
    assert resp.status_code == 200
    assert resp.json()["contacts"] == []


@pytest.mark.asyncio
async def test_get_contact_not_found(client):
    resp = await client.get("/v1/host/contacts/nonexistent")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_contact_style(client):
    resp = await client.post("/v1/host/contacts/c1/style", json={
        "identity": {"host_id": "test"},
        "person_id": "c1",
    })
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Briefings
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_briefings_empty(client):
    resp = await client.get("/v1/host/briefings")
    assert resp.status_code == 200
    assert resp.json()["briefings"] == []


# ---------------------------------------------------------------------------
# Cognition
# ---------------------------------------------------------------------------





# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_research_start_not_wired(client):
    resp = await client.post("/v1/host/research/start", json={
        "identity": {"host_id": "test"},
        "topic": "quantum computing",
    })
    assert resp.status_code == 501


@pytest.mark.asyncio
async def test_list_research_empty(client):
    resp = await client.get("/v1/host/research")
    assert resp.status_code == 200
    assert resp.json()["runs"] == []


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discover_connections_empty(client):
    resp = await client.post("/v1/host/synthesis/discover", json={
        "identity": {"host_id": "test"},
    })
    assert resp.status_code == 200
    assert resp.json()["connections"] == []


# ---------------------------------------------------------------------------
# Learning
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_submit_correction_without_a_feedback_store(client):
    resp = await client.post("/v1/host/learning/correction", json={
        "identity": {"host_id": "test"},
        "context": {"session_id": "s1", "contact_id": "c1"},
        "original": "hi",
        "correction": "hello",
    })
    assert resp.status_code == 200
    assert resp.json()["accepted"] is False


# ---------------------------------------------------------------------------
# Insights
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_insights_empty(client):
    resp = await client.get("/v1/host/insights")
    assert resp.status_code == 200
    assert resp.json()["insights"] == []


@pytest.mark.asyncio
async def test_dismiss_insight(client):
    """Endpoint persists the dismissal when the InsightStore is wired,
    otherwise returns 503 — never silently claims success."""
    resp = await client.post("/v1/host/insights/i1/dismiss")
    assert resp.status_code in (200, 503)
    if resp.status_code == 200:
        body = resp.json()
        assert body["ok"] is True
        assert body["insight_id"] == "i1"


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------

def test_setup_wizard_import():
    """Verify setup module imports correctly."""
    from protagine.setup import run_init
    assert callable(run_init)


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_secrets_list_empty(client):
    resp = await client.post("/v1/host/secrets/list", json={
        "identity": {"host_id": "test"},
    })
    assert resp.status_code == 200
    assert resp.json()["keys"] == []


@pytest.mark.asyncio
async def test_secrets_get_not_found(client):
    resp = await client.post("/v1/host/secrets/get", json={
        "identity": {"host_id": "test"},
        "key": "nonexistent",
    })
    assert resp.status_code == 200
    assert resp.json()["exists"] is False


@pytest.mark.asyncio
async def test_secrets_set_no_manager(client):
    resp = await client.post("/v1/host/secrets/set", json={
        "identity": {"host_id": "test"},
        "key": "test_key",
        "value": "test_val",
    })
    assert resp.status_code == 200
    assert resp.json()["stored"] is False


@pytest.mark.asyncio
async def test_secrets_delete_no_manager(client):
    resp = await client.post("/v1/host/secrets/delete", json={
        "identity": {"host_id": "test"},
        "key": "test_key",
    })
    assert resp.status_code == 200
    assert resp.json()["deleted"] is False


# ---------------------------------------------------------------------------
# Autonomy
# ---------------------------------------------------------------------------


