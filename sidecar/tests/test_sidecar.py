"""Tests for the Protagine sidecar — import checks, API endpoints, setup wizard."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

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
# Import checks — all 16 subsystems
# ---------------------------------------------------------------------------

SUBSYSTEMS = [
    ("protagine.reasoning.loop", "ReasoningLoop"),
    ("protagine.reasoning.executor", "ToolExecutor"),
    ("protagine.gate.pipeline", "ResponseGate"),
    ("protagine.intelligence.graph.client", "ProtagineGraph"),
    ("protagine.intelligence.cognition.metalearner", "MetaLearner"),
    ("protagine.intelligence.synthesis.connection_discoverer", "ConnectionDiscoverer"),
    ("protagine.intelligence.learning.continuous_learner", "ContinuousLearner"),
    ("protagine.intelligence.mind_model.signal_collector", "SignalCollector"),
    ("protagine.intelligence.relationships.trust_tiers", "TrustTier"),
    ("protagine.goals.store", "GoalStore"),
    ("protagine.briefings.engine", "BriefingEngine"),
    ("protagine.mind", "Mind"),
    ("protagine.research.pipeline", "ResearchPipeline"),
    ("protagine.contacts.store", "ContactStore"),
    ("protagine.world_model.store", "WorldModelStore"),
    ("protagine.vector.embedder", "EmbeddingPipeline"),
    ("protagine.skills.registry", "SkillRegistry"),
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


def test_unrecognised_guard_mode_refuses():
    """A PROTAGINE_GUARD_MODE typo must refuse loudly, never silently shadow."""
    from protagine.gate.response_guard import GuardMode
    from protagine.server import _resolve_guard_mode
    assert _resolve_guard_mode(None) is GuardMode.SHADOW
    assert _resolve_guard_mode("shadow") is GuardMode.SHADOW
    assert _resolve_guard_mode("ENFORCE") is GuardMode.ENFORCE
    with pytest.raises(RuntimeError):
        _resolve_guard_mode("enforec")   # the typo that used to silently shadow


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
# Reasoning
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reasoning_turn_not_wired(client):
    resp = await client.post("/v1/host/reasoning/turn", json={
        "identity": {"host_id": "test"},
        "context": {"session_id": "s1", "contact_id": "c1"},
        "messages": [{"role": "user", "content": "hello"}],
    })
    assert resp.status_code == 501


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_safety_check_unavailable_when_gate_missing(client, monkeypatch):
    """No response gate => 503 + decision "unavailable", NEVER "pass" —
    a caller must not mistake "not evaluated" for "evaluated and clean"."""
    from protagine.api.routers import host as host_mod
    monkeypatch.setattr(host_mod, "_response_gate", None)
    resp = await client.post("/v1/host/safety/check", json={
        "identity": {"host_id": "test"},
        "context": {"session_id": "s1", "contact_id": "c1"},
        "response_text": "Hello!",
    })
    assert resp.status_code == 503
    data = resp.json()
    assert data["decision"] == "unavailable"
    assert data["blocked"] is True


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_signals_ingest(client):
    resp = await client.post("/v1/host/signals/ingest", json={
        "identity": {"host_id": "test"},
        "context": {"session_id": "s1", "contact_id": "c1"},
    })
    assert resp.status_code == 200
    assert resp.json()["accepted"] is True


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
# World model
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_entities_empty(client):
    resp = await client.get("/v1/host/world/entities")
    assert resp.status_code == 200
    assert resp.json()["entities"] == []


@pytest.mark.asyncio
async def test_query_entities_empty(client):
    resp = await client.post("/v1/host/world/entities/query", json={
        "identity": {"host_id": "test"},
        "query": "python",
    })
    assert resp.status_code == 200
    assert resp.json()["entities"] == []


@pytest.mark.asyncio
async def test_query_entities_requires_identity(client):
    resp = await client.post("/v1/host/world/entities/query", json={
        "query": "python",
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_query_entities_forwards_the_type_filter(client, monkeypatch):
    from protagine.api.routers import host as host_router

    class _Store:
        def __init__(self):
            self.calls = []

        async def find_entities(self, **kwargs):
            self.calls.append(kwargs)
            return []

    store = _Store()
    monkeypatch.setattr(host_router, "_world_store", store)
    for entity_type, expected in (("person", "person"), ("all", None), (None, None)):
        body = {"identity": {"host_id": "test"}, "query": "python", "limit": 3}
        if entity_type is not None:
            body["entity_type"] = entity_type
        resp = await client.post("/v1/host/world/entities/query", json=body)
        assert resp.status_code == 200
        assert store.calls[-1] == {"query": "python", "entity_type": expected, "limit": 3}


# ---------------------------------------------------------------------------
# Cognition
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cognition_cycle_no_backend(client):
    resp = await client.post("/v1/host/cognition/cycle", json={
        "identity": {"host_id": "test"},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["cpi"]["deprecated"] is True
    assert data["cpi"]["canonical_endpoint"] == "/v1/host/self/benchmark"
    assert "memory" not in data["cpi"]


@pytest.mark.asyncio
async def test_cpi_no_backend(client):
    resp = await client.get("/v1/host/cognition/cpi")
    assert resp.status_code == 200
    data = resp.json()
    assert data["deprecated"] is True
    assert data["canonical_endpoint"] == "/v1/host/self/benchmark"
    assert "overall" not in data


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
async def test_submit_correction_no_learner(client):
    resp = await client.post("/v1/host/learning/correction", json={
        "identity": {"host_id": "test"},
        "context": {"session_id": "s1", "contact_id": "c1"},
        "original": "hi",
        "correction": "hello",
    })
    assert resp.status_code == 200
    assert resp.json()["accepted"] is False


@pytest.mark.asyncio
async def test_submit_engagement_no_learner(client):
    resp = await client.post("/v1/host/learning/engagement", json={
        "identity": {"host_id": "test"},
        "briefing_id": "b1",
        "action": "opened",
    })
    assert resp.status_code == 200
    assert resp.json()["accepted"] is False


@pytest.mark.asyncio
async def test_learning_weights_no_learner(client):
    resp = await client.get("/v1/host/learning/weights")
    assert resp.status_code == 200
    assert resp.json()["weights"] == {}


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_skills_empty(client):
    resp = await client.get("/v1/host/skills/registry")
    assert resp.status_code == 200
    assert resp.json()["skills"] == []


@pytest.mark.asyncio
async def test_get_skill_not_found(client):
    resp = await client.get("/v1/host/skills/registry/nonexistent")
    assert resp.status_code == 404


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
# ReasoningLoop unit tests
# ---------------------------------------------------------------------------

def test_tool_call_extraction():
    """Verify tool call extraction from a mock LiteLLM response."""
    from protagine.reasoning.loop import ReasoningLoop

    class MockFunc:
        name = "read_file"
        arguments = '{"path": "/tmp/test"}'

    class MockToolCall:
        id = "tc_123"
        function = MockFunc()

    class MockMessage:
        tool_calls = [MockToolCall()]

    class MockChoice:
        message = MockMessage()

    class MockResponse:
        choices = [MockChoice()]

    result = ReasoningLoop._extract_tool_calls(MockResponse())
    assert len(result) == 1
    assert result[0]["name"] == "read_file"
    assert result[0]["arguments"] == {"path": "/tmp/test"}


def test_tool_call_extraction_empty():
    from protagine.reasoning.loop import ReasoningLoop
    assert ReasoningLoop._extract_tool_calls(None) == []
    assert ReasoningLoop._extract_tool_calls(type("R", (), {"choices": []})()) == []


def test_build_assistant_message():
    from protagine.reasoning.loop import ReasoningLoop
    msg = ReasoningLoop._build_assistant_message(None, "hello", [])
    assert msg["role"] == "assistant"
    assert msg["content"] == "hello"

    msg_with_tools = ReasoningLoop._build_assistant_message(
        None, "", [{"id": "tc_1", "name": "run", "arguments": {"cmd": "ls"}}]
    )
    assert msg_with_tools["tool_calls"]
    assert msg_with_tools["tool_calls"][0]["function"]["name"] == "run"


# ---------------------------------------------------------------------------
# ToolExecutor unit tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_executor_unknown_tool():
    """Unknown tools return a structured error envelope so the LLM can
    see the miss and adjust — the executor does NOT defer back to the
    host (that earlier design was superseded when Protagine grew its own
    native-tool surface)."""
    from protagine.reasoning.executor import ToolExecutor
    executor = ToolExecutor()
    results = await executor.execute_batch([
        {"id": "tc_1", "name": "unknown_tool", "arguments": {}}
    ])
    assert len(results) == 1
    parsed = json.loads(results[0]["content"])
    assert parsed["error"] is True
    assert "unknown_tool" in parsed["message"]
    assert "available_tools" in parsed


@pytest.mark.asyncio
async def test_tool_executor_custom_handler():
    from protagine.reasoning.executor import ToolExecutor

    async def mock_handler(args):
        return f"result: {args.get('x', 0)}"

    executor = ToolExecutor(handlers={"add": mock_handler})
    results = await executor.execute_batch([
        {"id": "tc_1", "name": "add", "arguments": {"x": 42}}
    ])
    assert len(results) == 1
    assert results[0]["content"] == "result: 42"


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Chain / Identity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_identity_status_not_initialized(client):
    resp = await client.get("/v1/host/identity/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["initialized"] is False


@pytest.mark.asyncio
async def test_identity_init_not_wired(client):
    resp = await client.post("/v1/host/identity/init", json={
        "identity": {"host_id": "test"},
    })
    assert resp.status_code == 501


@pytest.mark.asyncio
async def test_chain_verify_no_chain(client):
    resp = await client.post("/v1/host/chain/verify", json={
        "identity": {"host_id": "test"},
        "data": "hello",
    })
    assert resp.status_code == 200
    assert resp.json()["valid"] is False


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


