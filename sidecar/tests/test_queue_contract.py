"""Authenticated deploy identity and exact-job canary inspection."""

from __future__ import annotations

import json

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from onekey import required_scope
from protagine.api.middleware import ApiKeyMiddleware
from protagine.api.routers import task_queue as queue_router
from protagine.task_queue.contract import queue_contract_identity
from protagine.task_queue.models import (
    Job,
    JobType,
    WorkerCapabilities,
)
from protagine.task_queue.queue_manager import TaskQueueManager
from protagine.task_queue.routing import AGENT_SYNC_ROUTE
from onekey import KEY


COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
MANIFEST_A = "c" * 64
MANIFEST_B = "d" * 64


@pytest.fixture(autouse=True)
def _contract_env(monkeypatch):
    monkeypatch.setenv("PROTAGINE_RELEASE_COMMIT", COMMIT_A)
    monkeypatch.setenv(
        "PROTAGINE_RELEASE_ARTIFACT_MANIFEST_SHA256", MANIFEST_A,
    )
    monkeypatch.setenv("PROTAGINE_WORKER_AUTHORITY_MODE", "shadow")
    monkeypatch.setenv("PROTAGINE_AGENT_JOB_CLAIMS_ENABLED", "false")
    monkeypatch.setenv("PROTAGINE_AGENT_WORKER_ROUTES", "agent_sync")
    monkeypatch.setenv("PROTAGINE_AGENT_SYNC_WORKER_NODE_ID", "sync-node")
    monkeypatch.delenv("PROTAGINE_ACTION_PLANE_WORKER_NODE_ID", raising=False)
    monkeypatch.delenv("PROTAGINE_HERMES_RUN_WORKER_NODE_ID", raising=False)


def _write_keyring(path, scopes):
    path.write_text(json.dumps({
        "version": 1,
        "principals": [{
            "principal": "contract-reader",
            "status": "active",
            "scopes": list(scopes),
            "audiences": [],
            "credentials": [{
                "id": "current",
                "secret": "scoped-secret",
                "status": "active",
            }],
        }],
    }))
    path.chmod(0o600)


def _app(*, legacy_key=None, keyring=None):
    app = FastAPI()
    app.add_middleware(
        ApiKeyMiddleware,
        api_key=KEY if (keyring or legacy_key) else None,
    )
    app.include_router(queue_router.router)
    return app


def _headers(secret):
    return {"Authorization": "Bearer " + KEY}




@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("commit", "manifest"),
    [
        ("", MANIFEST_A),
        ("not-a-commit", MANIFEST_A),
        ("0" * 40, MANIFEST_A),
        (COMMIT_A, ""),
        (COMMIT_A, "not-a-digest"),
        (COMMIT_A, "0" * 64),
    ],
)
async def test_contract_rejects_missing_malformed_or_null_release_identity(
    monkeypatch, commit, manifest,
):
    monkeypatch.setenv("PROTAGINE_RELEASE_COMMIT", commit)
    monkeypatch.setenv(
        "PROTAGINE_RELEASE_ARTIFACT_MANIFEST_SHA256", manifest,
    )
    app = _app(legacy_key="legacy-secret")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost",
    ) as client:
        response = await client.get(
            "/v1/host/queue/contract", headers=_headers("legacy-secret"),
        )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == (
        "queue_contract_identity_unavailable"
    )


def test_contract_digest_is_deterministic_and_binds_release_and_owners(
    monkeypatch,
):
    first = queue_contract_identity()
    replay = queue_contract_identity()
    assert first == replay
    assert len(first["contract_sha256"]) == 64

    monkeypatch.setenv("PROTAGINE_RELEASE_COMMIT", COMMIT_B)
    changed_commit = queue_contract_identity()
    assert changed_commit["contract_sha256"] != first["contract_sha256"]

    monkeypatch.setenv("PROTAGINE_RELEASE_COMMIT", COMMIT_A)
    monkeypatch.setenv(
        "PROTAGINE_RELEASE_ARTIFACT_MANIFEST_SHA256", MANIFEST_B,
    )
    changed_manifest = queue_contract_identity()
    assert changed_manifest["contract_sha256"] != first["contract_sha256"]

    monkeypatch.setenv(
        "PROTAGINE_RELEASE_ARTIFACT_MANIFEST_SHA256", MANIFEST_A,
    )
    monkeypatch.setenv("PROTAGINE_AGENT_SYNC_WORKER_NODE_ID", "new-sync-node")
    changed_owner = queue_contract_identity()
    assert changed_owner["contract_sha256"] != first["contract_sha256"]


@pytest.mark.asyncio
async def test_contract_runtime_thought_readiness_is_dynamic_not_in_digest(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PROTAGINE_WORKERS_MODE", "off")
    monkeypatch.setenv("PROTAGINE_THOUGHT_WORKER_NODE_ID", "thought-node")
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "thought.db")
    try:
        before = await queue_router.queue_contract()
        assert before["runtime_readiness"]["thought"]["ready"] is False
        static_digest = before["contract_sha256"]

        assert manager.queue.set_thought_runtime_ready(
            True, node_id="thought-node",
        ) is True
        after = await queue_router.queue_contract()
        assert after["runtime_readiness"]["thought"] == {
            "ready": True,
            "node_id": "thought-node",
            "reason": "thought_handler_ready",
        }
        assert after["contract_sha256"] == static_digest
    finally:
        await manager.stop()
        TaskQueueManager._instance = None


