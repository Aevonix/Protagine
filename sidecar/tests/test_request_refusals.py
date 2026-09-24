"""A request the sidecar's own validation refuses is answered 422 with the reason.

Stores raise ``ValueError`` for a request that cannot be honoured as sent; a
route that does not translate it used to answer a bare 500. One handler on the
application turns every such escape into 422 ``invalid_request`` with the text,
while a library's ``ValueError`` about the sidecar's own data stays a server error.
"""
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
import pytest

from protagine.api.errors import INVALID_REQUEST, install_exception_handlers, refused_request
from test_turn_source_evidence import source_app  # noqa: F401 (fixture)


def turn(session_id, turn_id="task-observe-1", **context):
    return {"identity": {"host_id": "test-host"},
            "context": {"session_id": session_id, "contact_id": "contact-a", "turn_id": turn_id, **context},
            "user_message": {"role": "user", "content": "Dispatched task result."},
            "assistant_message": {"role": "assistant", "content": "Noted."}}


@pytest.mark.asyncio
async def test_a_turn_without_a_session_is_refused_with_the_reason(source_app):
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
        response = await client.post("/v1/host/turns/sync", json=turn(""))
        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert detail["code"] == INVALID_REQUEST and detail["message"] == "source requires session_id"
        naive = await client.post("/v1/host/turns/sync", json=turn("session-a", "task-observe-2",
                                                                    metadata={"occurred_at": "2026-01-01T00:00:00"}))
        assert naive.status_code == 422 and "timezone" in naive.json()["detail"]["message"]
        accepted = await client.post("/v1/host/turns/sync", json=turn("session-a", "task-observe-3"))
        assert accepted.status_code == 200 and accepted.json()["source_recorded"] is True


def test_only_the_sidecars_own_refusals_become_422():
    app = install_exception_handlers(FastAPI())

    @app.get("/refused")
    def refused():
        raise ValueError("thing_required")

    @app.get("/broken")
    def broken():
        json.loads("{not json")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/refused")
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": INVALID_REQUEST, "message": "thing_required"}}
    assert client.get("/broken").status_code == 500


def test_the_served_application_installs_the_handler(monkeypatch):
    monkeypatch.delenv("PROTAGINE_API_KEY", raising=False)
    from protagine.server import create_app
    assert create_app().exception_handlers[ValueError] is refused_request
