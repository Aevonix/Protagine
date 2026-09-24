"""Phase 1 regression tests for stable turn-ingestion idempotency.

Audit reproduction: the legacy handler accepted the same successful turn twice
and ran its downstream effects twice. A stable turn ID must instead produce one
set of downstream effects, an identical replay, or an explicit content conflict.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from protagine.api.routers import host
from protagine.contacts.comms import CommsLog
from protagine.turns import TurnIdempotencyLedger


def _payload(turn_id: str = "turn-001", *, topic: str = "alpha") -> dict:
    return {
        "identity": {"host_id": "test-host", "instance_id": "instance-a"},
        "context": {
            "session_id": "session-1",
            "contact_id": "contact-1",
            "channel_id": "test:thread-1",
            "turn_id": turn_id,
        },
        "topics": [topic],
        "user_message": {"role": "user", "content": "Remember alpha"},
        "assistant_message": {"role": "assistant", "content": "I will."},
    }


@pytest.fixture
def stores(monkeypatch, tmp_path):
    """The source ledger and the communications log: the turn's durable effects."""
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path))
    # Keep this contract test focused on synchronous ingestion effects.
    monkeypatch.setattr(host, "_presence_store", None)
    monkeypatch.setattr(host, "_contacts_store", None)
    monkeypatch.setattr(host, "_telemetry", None)
    ledger = TurnIdempotencyLedger(tmp_path / "turn-idempotency.db")
    comms = CommsLog(str(tmp_path / "comms.db"), source_ledger=ledger)
    monkeypatch.setattr(host, "_comms_log", comms)
    yield SimpleNamespace(ledger=ledger, comms=comms)
    comms._conn.close()


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    app.include_router(host.router)
    app.include_router(host.v2_router)
    return app


def _communications(stores) -> list[str]:
    return [row[0] for row in stores.comms._conn.execute(
        "SELECT direction FROM communications ORDER BY direction")]


@pytest.mark.asyncio
async def test_v1_identical_turn_retry_has_one_downstream_effect(app, stores):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post("/v1/host/turns/sync", json=_payload())
        retry = await client.post("/v1/host/turns/sync", json=_payload())

    assert first.status_code == 200
    assert retry.status_code == 200
    assert retry.headers["Idempotency-Status"] == "replayed"
    assert retry.json() == first.json()
    assert first.json()["source_recorded"] and first.json()["continuity_updated"]
    assert _communications(stores) == ["in", "out"]
    assert len(stores.ledger.source_references(["turn-001"],
        contact_id="contact-1", session_id="session-1")) == 1


@pytest.mark.asyncio
async def test_v1_same_turn_id_with_different_content_conflicts(app, stores):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post("/v1/host/turns/sync", json=_payload())
        conflict = await client.post(
            "/v1/host/turns/sync", json=_payload(topic="different")
        )

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "turn_id_content_conflict"
    assert _communications(stores) == ["in", "out"]


@pytest.mark.asyncio
async def test_v2_put_returns_created_replayed_and_conflict(app, stores):
    body = _payload()
    body["context"].pop("turn_id")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.put("/v2/host/turns/turn-002", json=body)
        retry = await client.put("/v2/host/turns/turn-002", json=body)
        body["assistant_message"]["content"] = "Conflicting response"
        conflict = await client.put("/v2/host/turns/turn-002", json=body)

    assert (first.status_code, first.headers["Idempotency-Status"]) == (201, "created")
    assert (retry.status_code, retry.headers["Idempotency-Status"]) == (200, "replayed")
    assert conflict.status_code == 409
    assert stores.comms._conn.execute("SELECT count(*) FROM communications").fetchone()[0] == 2
    assert len(stores.ledger.source_references(["turn-002"],
        contact_id="contact-1", session_id="session-1")) == 1


@pytest.mark.asyncio
async def test_v2_path_and_body_turn_ids_must_match(app, stores):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(
            "/v2/host/turns/path-turn", json=_payload(turn_id="body-turn")
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "turn_id_mismatch"
    assert _communications(stores) == []


@pytest.mark.asyncio
async def test_v2_accepts_url_escaped_host_turn_ids(app, stores):
    body = _payload(turn_id="host/turn 7")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(
            "/v2/host/turns/host%2Fturn%207", json=body
        )

    assert response.status_code == 201
    assert len(stores.ledger.source_references(["host/turn 7"],
        contact_id="contact-1", session_id="session-1")) == 1


@pytest.mark.asyncio
async def test_summary_only_turn_records_nothing_and_says_so(app, stores):
    """The legacy summary-only shape has no messages: the ledger is the one memory."""
    body = _payload(turn_id="turn-summary")
    body.pop("user_message")
    body.pop("assistant_message")
    body["summary"] = "Legacy summary-only turn."
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put("/v2/host/turns/turn-summary", json=body)

    assert response.status_code == 201
    assert response.json()["accepted"] is True
    assert response.json()["source_recorded"] is False
    assert response.json()["continuity_updated"] is False
    assert response.json()["skipped_reason"] == "no_source_messages"
    assert _communications(stores) == []


@pytest.mark.asyncio
async def test_v2_concurrent_retry_is_truthful_pending_then_completed_replay(
        app, stores, monkeypatch):
    original = host._process_turn_sync
    entered, release, calls = asyncio.Event(), asyncio.Event(), []

    async def blocking(request_body, *args, **kwargs):
        # Hold the real ingestion open so a concurrent retry meets the reservation.
        calls.append(request_body)
        entered.set()
        await release.wait()
        return await original(request_body, *args, **kwargs)

    monkeypatch.setattr(host, "_process_turn_sync", blocking)
    body = _payload(turn_id="turn-concurrent")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        first_task = asyncio.create_task(
            client.put("/v2/host/turns/turn-concurrent", json=body)
        )
        await asyncio.wait_for(entered.wait(), timeout=2)

        pending = await client.put(
            "/v2/host/turns/turn-concurrent", json=body
        )
        assert pending.status_code == 202
        assert pending.headers["Idempotency-Status"] == "in_progress"
        assert pending.headers["Retry-After"] == "1"
        assert pending.json()["accepted"] is False
        assert len(calls) == 1

        release.set()
        first = await asyncio.wait_for(first_task, timeout=2)
        replay = await client.put(
            "/v2/host/turns/turn-concurrent", json=body
        )

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Status"] == "replayed"
    assert replay.json() == first.json()
    assert len(calls) == 1
    assert _communications(stores) == ["in", "out"]


@pytest.mark.asyncio
async def test_interrupted_creator_becomes_ambiguous_not_success(
        app, stores, monkeypatch):
    async def _crash(_body):
        raise RuntimeError("simulated creator crash")

    monkeypatch.setattr(host, "_process_turn_sync", _crash)
    body = _payload(turn_id="turn-crashed")
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        first = await client.put("/v2/host/turns/turn-crashed", json=body)
        retry = await client.put("/v2/host/turns/turn-crashed", json=body)

    assert first.status_code == 500
    assert retry.status_code == 503
    assert retry.json()["detail"]["code"] == "turn_ingestion_ambiguous"


@pytest.mark.asyncio
async def test_v2_retry_ingests_turn_abandoned_by_a_killed_first_attempt(app, stores):
    import sqlite3

    from protagine.api.schemas.host import TurnSyncRequest
    from protagine.turns import ReservationOutcome, canonical_turn_digest

    body = _payload(turn_id="turn-stale")
    digest = canonical_turn_digest(TurnSyncRequest.model_validate(body))
    # The first attempt reserved the row and its process was then hard-killed,
    # so neither complete() nor mark_ambiguous() will ever run for it.
    assert stores.ledger.reserve("turn-stale", digest).outcome == ReservationOutcome.CREATED

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        pending = await client.put("/v2/host/turns/turn-stale", json=body)
        assert pending.status_code == 202
        assert pending.json()["accepted"] is False

        with sqlite3.connect(stores.ledger.db_path) as conn:
            conn.execute(
                "UPDATE turn_ingestion SET created_at="
                "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-10 minutes')"
            )
        reclaimed = await client.put("/v2/host/turns/turn-stale", json=body)
        replay = await client.put("/v2/host/turns/turn-stale", json=body)

    assert (reclaimed.status_code, reclaimed.headers["Idempotency-Status"]) == (201, "created")
    assert reclaimed.json()["accepted"] is True
    assert (replay.status_code, replay.headers["Idempotency-Status"]) == (200, "replayed")
    assert replay.json() == reclaimed.json()
    assert stores.ledger.get("turn-stale")["state"] == "completed"
    assert stores.comms._conn.execute("SELECT count(*) FROM communications").fetchone()[0] == 2
