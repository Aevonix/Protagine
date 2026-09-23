"""Dev mode (no API key, no keyring) must only serve local requests.

Binding to a non-loopback interface is refused by ``protagine start``, but
anything that serves ``protagine.server:app`` directly (``uvicorn --host
0.0.0.0``) bypasses that guard. The middleware therefore checks the request
itself: the peer address and the Host header must both be loopback, so a
LAN caller is refused and a browser on the same machine cannot be steered
in through a DNS-rebinding hostname.
"""

import pytest
from fastapi.testclient import TestClient


LOCAL = ("127.0.0.1", 50000)
REMOTE = ("192.0.2.10", 50000)


@pytest.fixture
def app(monkeypatch):
    monkeypatch.delenv("PROTAGINE_API_KEY", raising=False)
    monkeypatch.delenv("PROTAGINE_API_KEYRING_PATH", raising=False)
    from protagine.server import create_app
    return create_app()


def _client(app, *, base_url, client):
    return TestClient(
        app, base_url=base_url, client=client, raise_server_exceptions=False,
    )


def test_local_client_with_loopback_host_is_served(app):
    resp = _client(app, base_url="http://localhost:7777", client=LOCAL).get("/v1/host/agents")
    assert resp.status_code not in (401, 403), resp.text


@pytest.mark.parametrize(
    "base_url, client",
    [
        ("http://127.0.0.1:7777", ("127.0.0.1", 1)),
        ("http://127.0.0.1", ("::ffff:127.0.0.1", 1)),
        ("http://127.0.0.2:7777", ("127.0.0.2", 1)),
    ],
)
def test_loopback_spellings_are_all_local(app, base_url, client):
    resp = _client(app, base_url=base_url, client=client).get("/v1/host/agents")
    assert resp.status_code not in (401, 403), resp.text


def _request(host_header, client):
    # The test client cannot build a bracketed IPv6 base URL, so exercise the
    # helper on a raw ASGI scope for those spellings.
    from starlette.requests import Request

    return Request({
        "type": "http", "method": "GET", "path": "/", "query_string": b"",
        "headers": [(b"host", host_header.encode())], "client": client,
    })


@pytest.mark.parametrize(
    "host_header, client, expected",
    [
        ("[::1]:7777", ("::1", 1), True),
        ("[::1]", ("::1", 1), True),
        ("localhost:7777", ("::1", 1), True),
        ("[::1]:7777", ("2001:db8::1", 1), False),
        ("[2001:db8::1]:7777", ("::1", 1), False),
        ("localhost.evil.example", LOCAL, False),
        ("127.0.0.1.evil.example", LOCAL, False),
        ("evil.example@localhost", LOCAL, False),
    ],
)
def test_is_local_request_ipv6_and_lookalike_hosts(host_header, client, expected):
    from protagine.api.middleware import is_local_request

    assert is_local_request(_request(host_header, client)) is expected


def test_is_local_request_without_peer_address_is_not_local():
    from protagine.api.middleware import is_local_request

    assert is_local_request(_request("localhost", None)) is False


def test_remote_client_is_refused_with_setup_hint(app):
    resp = _client(app, base_url="http://localhost:7777", client=REMOTE).get("/v1/host/agents")
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["code"] == "dev_mode_not_local"
    assert "PROTAGINE_API_KEY" in detail["message"]


def test_local_client_with_foreign_host_header_is_refused(app):
    # DNS rebinding: the socket is local but the browser thinks it is
    # talking to some other site.
    resp = _client(app, base_url="http://evil.example:7777", client=LOCAL).get("/v1/host/agents")
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "dev_mode_not_local"


def test_missing_host_header_is_refused(app):
    resp = _client(app, base_url="http://localhost", client=LOCAL).get(
        "/v1/host/agents", headers={"host": ""},
    )
    assert resp.status_code == 403


def test_health_stays_reachable_for_remote_clients(app):
    resp = _client(app, base_url="http://example.com", client=REMOTE).get("/v1/host/health")
    assert resp.status_code == 200


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
def test_agent_update_and_revoke_require_configured_auth(app, method):
    # Same rule as /agents/register and /agents/connect: these change
    # is_primary/capabilities, so dev mode must not reach the handler.
    client = _client(app, base_url="http://localhost:7777", client=LOCAL)
    resp = client.request(method, "/v1/host/agents/some-agent", json={"is_primary": True})
    assert resp.status_code == 503
    assert "PROTAGINE_API_KEY" in resp.json()["detail"]


def test_agent_read_stays_in_dev_mode(app):
    client = _client(app, base_url="http://localhost:7777", client=LOCAL)
    resp = client.get("/v1/host/agents/some-agent")
    assert resp.status_code != 503
