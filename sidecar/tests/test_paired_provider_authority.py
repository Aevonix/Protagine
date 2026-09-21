"""The isolated fixture authorizes the provider's existing legacy read routes."""

import json

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from protagine.api.authority import required_scope
from protagine.api.middleware import ApiKeyMiddleware
from protagine.qualification.paired_worker import PAIRED_FIXTURE_SCOPES


# Routes called by the four read/context tools exposed in general-plugin mode.
_PROVIDER_READS = (
    ("/v1/host/commitments", {"person_id": "synthetic-owner"}),
    ("/v1/host/mind/facts", {"contact_id": "synthetic-owner"}),
    ("/v1/host/timeline", {}),
    ("/v1/host/affect/state/synthetic-owner", {}),
)


def _app(tmp_path, scopes):
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
