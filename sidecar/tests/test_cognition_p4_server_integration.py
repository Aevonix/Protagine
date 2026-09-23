"""P4 startup and HTTP integration regression locks.

These tests exercise the wiring intentionally omitted from the isolated P4
source slice.  They use temporary state and scoped credentials only.
"""

from __future__ import annotations

import json

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from protagine.api.middleware import ApiKeyMiddleware
from protagine.api.routers import host
from protagine.self_model.params import (
    AdaptiveParamStore,
    register_core_params,
)
from protagine.server import (
    _initialize_controlled_learning,
    _wire_controlled_learning_pipeline,
)
from onekey import KEY


HOST_GLOBALS = (
    "_adaptive_params",
    "_benchmark",
    "_experiments",
    "_learning_feedback_store",
    "_learner",
    "_metalearner",
)


@pytest.fixture(autouse=True)
def _restore_host_globals():
    originals = {name: getattr(host, name) for name in HOST_GLOBALS}
    yield
    for name, value in originals.items():
        setattr(host, name, value)


def _configure(monkeypatch, state_dir, *, mode="shadow"):
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("PROTAGINE_COGNITION_P4_MODE", mode)
    monkeypatch.setenv("PROTAGINE_BENCHMARK_ENABLED", "true")
    monkeypatch.setenv("PROTAGINE_EXPERIMENTS_ENABLED", "true")
    monkeypatch.setenv("PROTAGINE_EXPERIMENT_PREGRANTS_JSON", "")
    monkeypatch.setenv("PROTAGINE_SKIP_DOTENV", "1")


def _params(state_dir):
    state_dir.mkdir(parents=True, exist_ok=True)
    params = AdaptiveParamStore(str(state_dir / "protagine-params.db"))
    register_core_params(params)
    host.set_adaptive_params(params)
    return params


def _principal(principal, secret, scopes):
    return {
        "principal": principal,
        "status": "active",
        "scopes": scopes,
        "viewer_person_id": "contact-owner",
        "audiences": ["viewer", "owner"],
        "credentials": [
            {"id": "current", "secret": secret, "status": "active"}
        ],
    }


def _app(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    keyring = tmp_path / "api-keyring.json"
    keyring.write_text(json.dumps({
        "version": 1,
        "principals": [
            _principal(
                "p4-manager",
                "manager-secret",
                [
                    "api:access",
                    "cognition:benchmark-manage",
                    "cognition:experiment-manage",
                ],
            ),
            _principal(
                "p4-reader",
                "reader-secret",
                ["cognition:benchmark-read", "cognition:experiment-read"],
            ),
        ],
    }))
    keyring.chmod(0o600)
    app = FastAPI()
    app.add_middleware(ApiKeyMiddleware, api_key=KEY)
    app.include_router(host.router)
    return app


def _headers(kind="manager"):
    return {
        "Authorization": f"Bearer " + KEY,
        "X-Protagine-Principal": f"p4-{kind}",
    }


def _proposal(**overrides):
    payload = {
        "hypothesis": "bounded recall tuning improves verified coverage",
        "ref": "recall.min_relevance",
        "variant": 0.2,
        "metric": "recall.fact_coverage",
        "metric_version": "v2",
        "assignment_mode": "cohort",
        "min_control_samples": 1,
        "min_variant_samples": 1,
        "min_total_samples": 2,
        "min_power": 0.0,
        "min_effect": 0.0,
        "source": "body-spoofed-principal",
        "sample_principal": "body-spoofed-principal",
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_correction_is_persisted_before_continuous_learning(
    tmp_path, monkeypatch,
):
    state_dir = tmp_path / "state"
    _configure(monkeypatch, state_dir)
    params = _params(state_dir)
    wiring = _initialize_controlled_learning(
        state_dir=state_dir, adaptive_params=params)

    class Learner:
        def __init__(self):
            self.corrections = []

        async def ingest_correction(self, correction):
            self.corrections.append(correction)

    learner = Learner()
    host.set_learner(learner)
    app = _app(tmp_path)
    payload = {
        "identity": {"host_id": "test"},
        "context": {"session_id": "s1", "contact_id": "contact-owner"},
        "original": "The release is Tuesday",
        "correction": "The release is Wednesday",
        "correction_type": "factual",
        "external_ref": "response:owner:42",
        "correction_id": "correction-owner-42",
        "person_id": "body-spoofed-principal",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post(
            "/v1/host/learning/correction", headers=_headers(), json=payload)
        replay = await client.post(
            "/v1/host/learning/correction", headers=_headers(), json=payload)

    assert first.status_code == 200 and first.json()["accepted"] is True
    assert replay.status_code == 200 and replay.json()["accepted"] is True
    assert wiring["corrections"].count() == 1
    stored = wiring["corrections"].between(
        "1970-01-01T00:00:00+00:00", "2999-01-01T00:00:00+00:00")
    assert stored[0]["context_hash"] == "response:owner:42"
    assert stored[0]["person_id"] == "contact-owner"
    assert learner.corrections[0].context_hash == "response:owner:42"


@pytest.mark.asyncio
async def test_cpi_and_cycle_publish_truthful_legacy_payload(
    tmp_path, monkeypatch,
):
    state_dir = tmp_path / "state"
    _configure(monkeypatch, state_dir)
    params = _params(state_dir)
    _initialize_controlled_learning(
        state_dir=state_dir, adaptive_params=params)
    host.set_metalearner(None)
    app = _app(tmp_path)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        cpi = await client.get(
            "/v1/host/cognition/cpi", headers=_headers("reader"))
        cycle = await client.post(
            "/v1/host/cognition/cycle",
            headers=_headers(),
            json={"identity": {"host_id": "test"}},
        )

    assert cpi.status_code == 200
    assert cpi.json()["deprecated"] is True
    assert cpi.json()["available"] is True
    assert "memory" not in cpi.json()
    assert cycle.status_code == 200
    assert cycle.json()["cpi"]["canonical"] == "selfhood_benchmark"
    assert "reasoning" not in cycle.json()["cpi"]
