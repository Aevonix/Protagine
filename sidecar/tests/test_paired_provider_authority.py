"""The isolated fixture authorizes the provider's existing legacy read routes."""

import json
import socket
import threading
import time

from fastapi import FastAPI
import httpx
from httpx import ASGITransport, AsyncClient
import pytest
import uvicorn

from protagine.api.authority import required_scope
from protagine.api.middleware import ApiKeyMiddleware
from protagine.qualification.paired_worker import (
    PAIRED_FIXTURE_SCOPES, provider_read_lifespan, provider_read_services,
)


# Routes called by the four read/context tools exposed in general-plugin mode.
_PROVIDER_READS = (
    ("/v1/host/commitments", {"person_id": "synthetic-owner"}),
    ("/v1/host/mind/facts", {"contact_id": "synthetic-owner"}),
    ("/v1/host/timeline", {}),
    ("/v1/host/affect/state/synthetic-owner", {}),
)


def _app(tmp_path, scopes, *, real_handlers=False):
    keyring = tmp_path / "fixture-keyring.json"
    keyring.write_text(json.dumps({"version": 1, "principals": [{
        "principal": "benchmark-owner", "status": "active",
        "viewer_person_id": "synthetic-owner",
        "person_ids": ["synthetic-owner"], "audiences": ["viewer"],
        "scopes": list(scopes),
        "credentials": [{"id": "fixture", "secret": "fixture-secret", "status": "active"}],
    }]}))
    keyring.chmod(0o600)
    app = FastAPI()
    app.add_middleware(ApiKeyMiddleware, keyring_path=str(keyring))
    if real_handlers:
        from protagine.api.routers import host
        app.include_router(host.router)
        app.include_router(host.v2_router)
        return app

    async def synthetic_data():
        return {"contact_id": "synthetic-owner", "data": []}

    for path, _ in _PROVIDER_READS:
        app.add_api_route(path, synthetic_data, methods=["GET"])
    app.add_api_route("/v1/host/transport/observe", synthetic_data, methods=["POST"])
    app.add_api_route("/v1/host/queue/work/operations", synthetic_data, methods=["POST"])
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("include_api_access", [False, True])
async def test_paired_fixture_authorizes_provider_reads_without_changing_route_policy(
    tmp_path, include_api_access,
):
    scopes = [scope for scope in PAIRED_FIXTURE_SCOPES
              if include_api_access or scope != "api:access"]
    app = _app(tmp_path, scopes)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path, params in _PROVIDER_READS:
            # Keep production routes on their existing authority contract.
            assert required_scope("GET", path) == "api:access"
            response = await client.get(path, params=params,
                headers={"Authorization": "Bearer fixture-secret"})
            if include_api_access:
                assert response.status_code == 200
                assert response.json() == {"contact_id": "synthetic-owner", "data": []}
            else:
                assert response.status_code == 403
                assert response.json()["detail"] == {
                    "code": "insufficient_scope", "required_scope": "api:access"}


@pytest.mark.asyncio
async def test_fixture_api_access_preserves_explicit_scope_and_query_boundaries(tmp_path):
    app = _app(tmp_path, PAIRED_FIXTURE_SCOPES)
    headers = {"Authorization": "Bearer fixture-secret"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path, scope in (
            ("/v1/host/transport/observe", "transport:write"),
            ("/v1/host/queue/work/operations", "work:control"),
        ):
            response = await client.post(path, headers=headers)
            assert response.status_code == 403
            assert response.json()["detail"] == {
                "code": "insufficient_scope", "required_scope": scope}
        response = await client.get("/v1/host/mind/facts", headers=headers,
            params={"contact_id": "another-person"})
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "person_scope_not_granted"
        response = await client.get("/v1/host/timeline")
        assert response.status_code == 401


def test_fresh_provider_services_serve_real_handlers_on_api_thread_and_close(tmp_path, monkeypatch):
    from protagine.api.routers import host
    from protagine.tom.affect import AffectStore
    from protagine.tom.facts import SharedFactsStore
    from protagine.turns import get_turn_idempotency_ledger

    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path / "memory-state"))
    monkeypatch.setenv("PROTAGINE_EVENT_JOURNAL_DIR", str(tmp_path / "memory-state" / "events"))
    originals = {name: object() for name in ("commitment", "affect", "facts")}
    for name, value in originals.items():
        monkeypatch.setattr(host, "_" + name + "_store", value)
    closed = []
    for store_type in (AffectStore, SharedFactsStore):
        original_close = store_type.close

        def close(store, _original=original_close):
            _original(store)
            closed.append((type(store).__name__, threading.get_ident()))

        monkeypatch.setattr(store_type, "close", close)
    app = _app(tmp_path, PAIRED_FIXTURE_SCOPES, real_handlers=True)
    app.router.lifespan_context = provider_read_lifespan(tmp_path)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(64)
    server = uvicorn.Server(uvicorn.Config(app, lifespan="on", access_log=False, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        ledger = get_turn_idempotency_ledger(tmp_path / "memory-state")
        assert host._facts_store._source_ledger is ledger
        assert host._affect_store._source_ledger is ledger
        with httpx.Client(base_url=f"http://127.0.0.1:{listener.getsockname()[1]}", timeout=5,
                headers={"Authorization": "Bearer fixture-secret"}, trust_env=False) as client:
            data = {}
            for path, params in _PROVIDER_READS:
                response = client.get(path, params=params)
                assert response.status_code == 200, response.text
                data[path] = response.json()
            assert data["/v1/host/commitments"]["commitments"] == []
            assert data["/v1/host/mind/facts"]["facts"] == []
            assert data["/v1/host/timeline"]["events"] == []
            assert data["/v1/host/affect/state/synthetic-owner"]["event_count"] == 0
    finally:
        server.should_exit = True
        thread.join(5)
        listener.close()
    assert not thread.is_alive()
    assert {name for name, _ in closed} == {"AffectStore", "SharedFactsStore"}
    assert len(closed) == 2 and all(identity == thread.ident for _, identity in closed)
    for name, value in originals.items():
        assert getattr(host, "_" + name + "_store") is value


def test_provider_store_initialization_failure_closes_and_restores_partial_setup(tmp_path, monkeypatch):
    from protagine.api.routers import host
    from protagine.tom import facts
    from protagine.tom.affect import AffectStore

    originals = {name: object() for name in ("commitment", "affect", "facts")}
    for name, value in originals.items():
        monkeypatch.setattr(host, "_" + name + "_store", value)
    closed = []
    original_close = AffectStore.close

    def close(store):
        original_close(store)
        closed.append(True)

    def unavailable(*args, **kwargs):
        raise RuntimeError("controlled facts initialization failure")

    monkeypatch.setattr(AffectStore, "close", close)
    monkeypatch.setattr(facts, "SharedFactsStore", unavailable)
    with pytest.raises(RuntimeError, match="controlled facts initialization failure"):
        with provider_read_services(tmp_path):
            pytest.fail("An incomplete provider surface must not become ready")
    assert closed == [True]
    for name, value in originals.items():
        assert getattr(host, "_" + name + "_store") is value
