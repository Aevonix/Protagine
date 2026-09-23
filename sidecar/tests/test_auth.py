"""One key: the bearer check, the loopback development rule and the bind guard."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from protagine.api import auth
from protagine.api.auth import (
    AuthConfigError,
    check_bind,
    is_loopback_host,
    key_authority,
    request_authority,
    resolve_request_person,
    resolve_turn_person,
)
from protagine.api.middleware import ApiKeyMiddleware
from protagine.config import write_api_key

LOCAL = ("127.0.0.1", 50000)
REMOTE = ("192.0.2.10", 50000)


def _app(api_key: str | None) -> FastAPI:
    app = FastAPI()

    @app.get("/v1/host/health")
    async def health():
        return {"status": "ok"}

    @app.get("/v1/host/whoami")
    async def whoami(request: Request):
        authority = request_authority(request)
        return {"principal": authority.principal_id, "authenticated": authority.authenticated,
                "anonymous": authority.anonymous, "viewer": authority.viewer_person_id}

    @app.get("/v1/host/person")
    async def person(request: Request, contact_id: str | None = None, audience: str | None = None):
        try:
            return {"person": resolve_request_person(request, claimed_person_id=contact_id, audience=audience)}
        except HTTPException as exc:
            return {"error": exc.detail["code"]}

    @app.post("/v1/host/configure")
    async def configure():
        return {"configured": True}

    app.add_middleware(ApiKeyMiddleware, api_key=api_key)
    return app


def _client(app, client=LOCAL):
    return TestClient(app, base_url="http://127.0.0.1:7777", client=client)


# --- the bearer check -------------------------------------------------------

def test_bearer_key_is_accepted_and_bound_to_the_owner(monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", "cid-owner")
    with _client(_app("k")) as client:
        body = client.get("/v1/host/whoami", headers={"Authorization": "Bearer k"}).json()
    assert body == {"principal": "api-key", "authenticated": True, "anonymous": False, "viewer": "cid-owner"}


def test_x_api_key_header_is_accepted_too():
    with _client(_app("k")) as client:
        assert client.get("/v1/host/whoami", headers={"X-API-Key": "k"}).status_code == 200


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic k"},
                                     {"X-API-Key": "kk"}, {"Authorization": "Bearer "}])
def test_wrong_or_missing_key_is_401(headers):
    with _client(_app("k")) as client:
        response = client.get("/v1/host/whoami", headers=headers)
    assert response.status_code == 401


def test_health_needs_no_key():
    with _client(_app("k"), client=REMOTE) as client:
        assert client.get("/v1/host/health").status_code == 200


def test_key_selects_the_person_from_the_body():
    with _client(_app("k")) as client:
        auth_header = {"Authorization": "Bearer k"}
        assert client.get("/v1/host/person", headers=auth_header).json() == {"person": None}
        assert client.get("/v1/host/person", headers=auth_header, params={"contact_id": "p-02"}).json() == {"person": "p-02"}
        assert client.get("/v1/host/person", headers=auth_header, params={"audience": "shared"}).json() == {"person": "shared"}
        assert client.get("/v1/host/person", headers=auth_header,
                          params={"audience": "shared", "contact_id": "p-02"}).json() == {"error": "person_scope_conflict"}
        assert client.get("/v1/host/person", headers=auth_header,
                          params={"audience": "team"}).json() == {"error": "invalid_audience"}
        # The provider marks a guest prefetch with the viewer audience: the body person is the viewer.
        assert client.get("/v1/host/person", headers=auth_header,
                          params={"audience": "viewer", "contact_id": "guest-7"}).json() == {"person": "guest-7"}
        assert client.get("/v1/host/person", headers=auth_header,
                          params={"audience": "viewer"}).json() == {"person": "owner"}


# --- keyless loopback development mode -------------------------------------

def test_no_key_serves_loopback_only():
    app = _app(None)
    with _client(app) as client:
        body = client.get("/v1/host/whoami").json()
        assert body["anonymous"] is True and body["authenticated"] is False
    with _client(app, client=REMOTE) as client:
        response = client.get("/v1/host/whoami")
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "dev_mode_not_local"


def test_no_key_refuses_foreign_host_header():
    with TestClient(_app(None), base_url="http://evil.example", client=LOCAL) as client:
        assert client.get("/v1/host/whoami").status_code == 403


def test_no_key_keeps_credential_routes_closed():
    with _client(_app(None)) as client:
        response = client.post("/v1/host/configure")
    assert response.status_code == 503


def test_dev_mode_person_rules():
    with _client(_app(None)) as client:
        assert client.get("/v1/host/person").json() == {"person": "dev-anonymous"}
        assert client.get("/v1/host/person", params={"contact_id": "p-02"}).json() == {"person": "p-02"}
        # The middleware refuses reserved query selectors before the handler runs.
        assert client.get("/v1/host/person", params={"contact_id": "shared"}).status_code == 403
        assert resolve_request_person(None, claimed_person_id="p-02") == "p-02"
        assert client.get("/v1/host/person", params={"audience": "owner"}).json() == {"error": "audience_not_granted"}
        assert client.get("/v1/host/person", params={"person_id": "global"}).status_code == 403


# --- helpers -----------------------------------------------------------------

def test_in_process_calls_are_trusted_and_turns_use_the_context_person():
    assert request_authority(None) == key_authority()
    assert resolve_turn_person(None, context_person_id="p-03", has_sender=True) == "p-03"


def test_configured_key_comes_from_env_or_the_key_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_HOME", str(tmp_path))
    monkeypatch.delenv("PROTAGINE_API_KEY", raising=False)
    assert auth.configured_api_key() is None
    write_api_key("file-key", tmp_path)
    assert auth.configured_api_key() == "file-key"
    monkeypatch.setenv("PROTAGINE_API_KEY", "env-key")
    assert auth.configured_api_key() == "env-key"


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "", "[::1]", "127.0.0.2"])
def test_loopback_hosts(host):
    assert is_loopback_host(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "10.0.0.1", "example.com"])
def test_non_loopback_hosts(host):
    assert is_loopback_host(host) is False


def test_non_loopback_bind_without_a_key_is_refused():
    with pytest.raises(AuthConfigError):
        check_bind("0.0.0.0", None)
    check_bind("0.0.0.0", "k")
    check_bind("127.0.0.1", None)


def test_create_app_reads_the_key_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_HOME", str(tmp_path))
    monkeypatch.delenv("PROTAGINE_API_KEY", raising=False)
    write_api_key("file-key", tmp_path)
    from protagine.server import create_app
    with _client(create_app()) as client:
        assert client.get("/v1/host/queue/stats", headers={"Authorization": "Bearer file-key"}).status_code != 401
        assert client.get("/v1/host/queue/stats", headers={"Authorization": "Bearer other"}).status_code == 401


def test_events_socket_uses_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_HOME", str(tmp_path))
    monkeypatch.delenv("PROTAGINE_API_KEY", raising=False)
    write_api_key("file-key", tmp_path)
    from protagine.server import create_app
    from starlette.websockets import WebSocketDisconnect
    with _client(create_app()) as client:
        with client.websocket_connect("/v1/host/events") as socket:
            socket.send_json({"type": "auth", "token": "file-key"})
            socket.close()
        with pytest.raises(WebSocketDisconnect) as info:
            with client.websocket_connect("/v1/host/events") as socket:
                socket.send_json({"type": "auth", "token": "wrong"})
                socket.receive_text()
        assert info.value.code == 4003
