"""Selective erasure across source, projection and disconnected-host boundaries."""
import json
import sqlite3
from unittest.mock import AsyncMock
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response, Request
import pytest
from protagine.api.routers import host
from protagine.turns import TurnIdempotencyLedger
from protagine.turns.idempotency import SourceErased

@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path))
    return TurnIdempotencyLedger(tmp_path / "turn-idempotency.db")

def source(ledger, turn_id="turn-a", *, contact="contact-a", session="session-a", messages=None):
    messages = messages or [{"role": "user", "content": "The hydrofoil password is pâss-unique."}]
    ledger.record_source(turn_id, contact_id=contact, session_id=session, messages=messages)
    return messages

def queued(turn_id, messages, *, contact="contact-a"):
    return {"turn_id": turn_id, "contact_id": contact, "session_id": "session-a", "checkpoint_messages": messages}

def test_selective_redaction_survives_restart(ledger):
    messages = source(ledger)
    survivor = {"role": "user", "content": "The bicycle is blue."}
    source(ledger, "checkpoint-a", messages=messages + [survivor])
    source(ledger, "other-contact", contact="contact-b", messages=messages)
    source(ledger, "other-session", session="session-b", messages=messages)
    result = ledger.erase_sources(contact_id="contact-a", turn_ids=["turn-a"])
    assert set(result["affected_source_ids"]) == {"turn-a", "checkpoint-a"}
    reopened = TurnIdempotencyLedger(ledger.db_path)
    assert reopened.is_source_erased("turn-a", "contact-a")
    assert not reopened.is_source_erased("turn-a", "contact-b")
    assert reopened.is_projection_erased("checkpoint-a")
    assert reopened.search_sources("bicycle", contact_id="contact-a", session_id="session-a")
    assert all(row["turn_id"] == "other-session" for row in reopened.search_sources("password", contact_id="contact-a", session_id="session-a"))
    assert reopened.search_sources("password", contact_id="contact-b", session_id="session-a")
    with sqlite3.connect(ledger.db_path) as conn:
        checkpoint = json.loads(conn.execute("SELECT messages_json FROM turn_sources WHERE turn_id='checkpoint-a'").fetchone()[0])
        tombstones = str(conn.execute("SELECT * FROM source_erasures").fetchall())
    assert checkpoint == [survivor] and "pâss-unique" not in tombstones
    with pytest.raises(SourceErased):
        source(reopened)
    source(reopened, "checkpoint-rewrapped", messages=messages + [survivor])
    assert reopened.search_sources("password", contact_id="contact-a", session_id="session-a")[0]["turn_id"] == "other-session"

def test_ambiguity_and_cross_contact_selection_are_non_mutating(ledger):
    message = source(ledger)
    source(ledger, "checkpoint-a", messages=message)
    with pytest.raises(ValueError, match="ambiguous_source"):
        ledger.erase_sources(contact_id="contact-a", old_text=message[0]["content"], session_id="session-a")
    with pytest.raises(ValueError, match="source_not_found"):
        ledger.erase_sources(contact_id="contact-b", turn_ids=["turn-a"])
    assert ledger.erasure_feed("contact-a")["head"] == 0

def test_feed_pages_and_detects_restore_behind_host(ledger):
    source(ledger, "a")
    source(ledger, "b", session="session-b")
    ledger.erase_sources(contact_id="contact-a", turn_ids=["a", "b"])
    first = ledger.erasure_feed("contact-a", limit=1)
    assert first["complete"] is False
    second = ledger.erasure_feed("contact-a", after=first["through"])
    assert second["complete"] is True and second["head"] == 2
    with pytest.raises(ValueError, match="cursor"):
        ledger.erasure_feed("contact-a", after=3)


@pytest.mark.asyncio
async def test_api_erasure_blocks_replay(ledger, monkeypatch):
    messages = source(ledger)
    effects = AsyncMock(side_effect=AssertionError("erased turn ran effects"))
    monkeypatch.setattr(host, "_process_turn_sync", effects)
    app = FastAPI()
    app.include_router(host.router)
    app.include_router(host.v2_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        erased = await client.post("/v1/host/memory/sources/forget", json={"contact_id": "contact-a", "source_ids": ["turn-a"]})
        assert erased.status_code == 200 and erased.json()["source_erased"]
        body = {"identity": {"host_id": "test"}, "context": {"session_id": "session-a", "contact_id": "contact-a", "turn_id": "turn-a"}, "checkpoint_messages": messages}
        replay = await client.put("/v2/host/turns/turn-a", json=body)
        assert replay.json()["skipped_reason"] == "source_erased"
        assert replay.json()["source_recorded"] is False and effects.await_count == 0
        wrong = await client.post("/v1/host/memory/sources/forget", json={"contact_id": "contact-b", "source_ids": ["turn-a"]})
        assert wrong.status_code == 422

def test_repeat_erase_retains_derived_cleanup_targets(ledger):
    messages = source(ledger)
    source(ledger, "checkpoint-a", messages=messages + [{"role": "user", "content": "Unrelated."}])
    first = ledger.erase_sources(contact_id="contact-a", turn_ids=["turn-a"])
    again = ledger.erase_sources(contact_id="contact-a", turn_ids=["turn-a"])
    assert set(again["affected_source_ids"]) == set(first["affected_source_ids"])
    assert again["watermark"] == first["watermark"]

@pytest.mark.asyncio
async def test_mcp_forget_tool_reaches_the_real_erasure_api(ledger, monkeypatch):
    pytest.importorskip("mcp")
    from protagine.mcp.server import create_server
    import httpx
    source(ledger)
    monkeypatch.setenv("PROTAGINE_MCP_SOURCE", "test-host")
    app = FastAPI()
    app.include_router(host.router)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original_client(transport=ASGITransport(app=app), base_url="http://test", **kw))
    server = create_server()
    result = await server._tool_manager._tools["protagine_forget_sources"].fn(source_ids=["turn-a"], contact_id="contact-a")
    assert result["source_erased"] is True
    assert ledger.is_source_erased("turn-a", "contact-a")
