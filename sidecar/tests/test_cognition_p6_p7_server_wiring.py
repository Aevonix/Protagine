"""P6/P7 startup, scope, and HTTP attachment regression locks.

The feature modules deliberately shipped without shared server wiring.  These
tests pin the attachment boundary before it is implemented: off creates no
state, P6 shadow remains observer-only, P6 live composes (and never weakens)
P3 capacity, and P7 ranks only durable P3 projects using server authority.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from onekey import required_scope
from protagine.api.middleware import ApiKeyMiddleware
from protagine.api.routers import host
from protagine.cognition.drive_governance import (
    DriveGovernance,
    DriveGovernanceStore,
    DriveRanker,
    DriveV1,
    ScopeV1,
)
from protagine.projects.models import Project
from protagine.projects.store import ProjectStore
from protagine.self_model.situation import (
    SituationObservationV1,
    SituationReducer,
    SituationStore,
)
from protagine.self_model.workspace import ConcernStore
from protagine.server import (
    _attach_drive_governance,
    _attach_situation_spine,
    _capacity_plus_attachment_failure,
    _compose_p7_charter_admission,
)
from protagine.cognition.drive_governance import (
    CharterAdmissionConstraintsV1,
    ScopeV1,
)
from onekey import KEY


NOW = datetime(2026, 7, 12, 16, 0, tzinfo=timezone.utc)


class FakeScheduler:
    def __init__(self):
        self.callbacks = {}

    def register(self, name, callback, interval_seconds, metadata=None):
        self.callbacks[name] = {
            "callback": callback,
            "interval_seconds": interval_seconds,
            "metadata": metadata or {},
        }
        return f"schedule:{name}"


class FakeProjectStore:
    def __init__(self, projects=()):
        self.projects = list(projects)

    def list_projects(self, status=None, limit=50):
        items = self.projects
        if status:
            items = [item for item in items if item.status == status]
        return items[:limit]

    def get_project(self, project_id):
        return next(
            (item for item in self.projects if item.id == project_id), None
        )


class FakeCognitionStore:
    def get_policy_decision(self, reference):
        return {"payload": {"decision_ref": reference}}


@pytest.fixture(autouse=True)
def restore_host_wiring():
    names = (
        "_situation_store",
        "_situation_reducer",
        "_drive_governance",
        "_drive_ranker",
        "_drive_project_store",
        "_cognition_spine",
    )
    originals = {name: getattr(host, name, None) for name in names}
    yield
    for name, value in originals.items():
        setattr(host, name, value)


def _p3(*, capacity=None, projects=()):
    capacity = capacity or (
        lambda _proposal, _concern: (True, "capacity_available")
    )
    project_store = FakeProjectStore(projects)
    project_engine = SimpleNamespace(store=project_store)
    return SimpleNamespace(
        _situation=capacity,
        store=FakeCognitionStore(),
        project_engine=project_engine,
    )


def test_p6_shadow_is_periodic_observer_and_never_replaces_p3_validator(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PROTAGINE_SITUATION_SPINE", "shadow")
    monkeypatch.setattr(
        SituationReducer,
        "run_once",
        lambda self, limit=100: {
            "enabled": True,
            "mode": "shadow",
            "processed": 0,
        },
    )
    capacity = lambda _proposal, _concern: (True, "capacity_available")
    p3 = _p3(capacity=capacity)
    scheduler = FakeScheduler()

    wiring = _attach_situation_spine(
        state_dir=tmp_path,
        cognition_spine=p3,
        scheduler=scheduler,
    )

    assert wiring is not None
    assert p3._situation is capacity
    assert host._situation_store is wiring["store"]
    assert host._situation_reducer is wiring["reducer"]
    assert "situation" in host.supported_capabilities()
    assert "situation_reduce" in scheduler.callbacks
    assert scheduler.callbacks["situation_reduce"]["interval_seconds"] >= 5
    assert scheduler.callbacks["situation_reduce"]["callback"]()["mode"] ==\
        "shadow"


@pytest.mark.asyncio
async def test_p6_periodic_reducer_ingests_real_queue_resource_observation(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PROTAGINE_SITUATION_SPINE", "shadow")
    monkeypatch.setenv("PROTAGINE_OWNER_PERSON_ID", "person-owner")
    monkeypatch.setattr(
        SituationReducer,
        "run_once",
        lambda self, limit=100: {
            "enabled": True,
            "mode": "shadow",
            "processed": 0,
        },
    )

    class Queue:
        def execution_readiness(self):
            return {
                "ready": True,
                "reason": "scheduler_ready",
                "routing_ready": True,
                "routing_reason": "agent_action_routes_ready",
                "typed_routes": {},
            }

        async def get_queue_stats(self):
            return SimpleNamespace(
                registered_workers=1,
                active_workers=1,
                stale_workers=0,
                available_workers=1,
                worker_heartbeat_ttl_secs=60.0,
            )

    scheduler = FakeScheduler()
    wiring = _attach_situation_spine(
        state_dir=tmp_path,
        cognition_spine=_p3(),
        scheduler=scheduler,
        task_queue=SimpleNamespace(queue=Queue()),
    )

    result = await scheduler.callbacks[
        "situation_reduce"
    ]["callback"]()
    snapshot = wiring["store"].snapshot(
        subject_person_id="person-owner",
        viewer_scope="owner",
    )

    assert result["resource_observation"]["disposition"] == "applied"
    assert result["resource_observation"]["state"] == "available"
    resource = snapshot.active_facts("resource")
    assert len(resource) == 1
    assert resource[0].entity_id == "task-queue-execution"
    assert dict(resource[0].attributes)["capacity_available"] is True


def test_p6_scheduler_registration_failure_is_atomic(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_SITUATION_SPINE", "live")
    monkeypatch.setattr(
        SituationReducer,
        "run_once",
        lambda self, limit=100: {
            "enabled": True, "mode": "live", "processed": 0,
        },
    )
    capacity = lambda _proposal, _concern: (True, "capacity_available")
    p3 = _p3(capacity=capacity)

    class BrokenScheduler:
        def register(self, *_args, **_kwargs):
            raise RuntimeError("schedule store unavailable")

    with pytest.raises(RuntimeError, match="schedule store unavailable"):
        _attach_situation_spine(
            state_dir=tmp_path,
            cognition_spine=p3,
            scheduler=BrokenScheduler(),
        )

    assert p3._situation is capacity
    assert host._situation_store is None
    assert host._situation_reducer is None


def test_p6_live_composes_capacity_and_fails_closed_on_reducer_or_gate_error(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PROTAGINE_SITUATION_SPINE", "live")
    monkeypatch.setattr(
        SituationReducer,
        "run_once",
        lambda self, limit=100: {
            "enabled": True,
            "mode": "live",
            "processed": 0,
        },
    )
    calls = []

    def capacity(proposal, concern):
        calls.append((proposal, concern))
        return {"allowed": proposal.objective != "blocked", "reason": "capacity"}

    p3 = _p3(capacity=capacity)
    wiring = _attach_situation_spine(
        state_dir=tmp_path,
        cognition_spine=p3,
        scheduler=None,
    )
    concern = SimpleNamespace(subject_person_id="person-owner")
    blocked = SimpleNamespace(
        objective="blocked",
        subject_person_id="person-owner",
        viewer_scope="owner",
    )
    allowed = SimpleNamespace(
        objective="allowed",
        subject_person_id="person-owner",
        viewer_scope="owner",
    )

    assert p3._situation(blocked, concern) == {
        "allowed": False,
        "reason": "capacity",
    }

    wiring["reducer"].run_once = lambda limit=100: {
        "enabled": True,
        "error": "journal_unavailable",
    }
    reducer_denial = p3._situation(allowed, concern)
    assert reducer_denial["allowed"] is False
    assert reducer_denial["reason"] == "situation_reducer_unhealthy"

    wiring["reducer"].run_once = lambda limit=100: {
        "enabled": True,
        "processed": 0,
    }
    wiring["gate"].for_goal_proposal = lambda *_args, **_kwargs: 1 / 0
    gate_denial = p3._situation(allowed, concern)
    assert gate_denial["allowed"] is False
    assert gate_denial["reason"] == "situation_gate_failed_closed"
    assert len(calls) == 3


def test_p6_attachment_failure_gate_preserves_denial_and_holds_capacity_allow():
    def capacity(proposal, _concern):
        return (
            (False, "capacity_exhausted")
            if proposal.objective == "blocked"
            else (True, "capacity_available")
        )

    validator = _capacity_plus_attachment_failure(
        capacity, "situation_attachment_failed_closed",
    )
    blocked = validator(SimpleNamespace(objective="blocked"), object())
    held = validator(SimpleNamespace(objective="allowed"), object())

    assert blocked == (False, "capacity_exhausted")
    assert held["allowed"] is False
    assert held["reason"] == "situation_attachment_failed_closed"


def test_p6_and_p7_stores_have_explicit_idempotent_close(tmp_path):
    situation = SituationStore(str(tmp_path / "situation.db"))
    drives = DriveGovernanceStore(tmp_path / "drives.db")
    situation.close()
    situation.close()
    drives.close()
    drives.close()


def _app(tmp_path, principals):
    keyring = tmp_path / "api-keyring.json"
    keyring.write_text(json.dumps({"version": 1, "principals": principals}))
    keyring.chmod(0o600)
    app = FastAPI()
    app.add_middleware(ApiKeyMiddleware, api_key=KEY)
    app.include_router(host.router)
    return app


def _headers(secret, principal):
    return {
        "Authorization": f"Bearer " + KEY,
        "X-Protagine-Principal": principal,
    }


class RecordingRanker:
    def __init__(self):
        self.goals = None

    def rank(self, goals, *, mode=None, now=None):
        self.goals = tuple(goals)

        class Batch:
            def observer_projection(self, **_viewer):
                return {
                    "schema": "RankingBatchObserverV1",
                    "results": [goal.payload() for goal in goals],
                }

        return Batch()


class RecordingGovernance:
    mode = "live"
    store = SimpleNamespace()

    def __init__(self):
        self.calls = []


class AllowingDirectives:
    def check(self, _action):
        return SimpleNamespace(allowed=True, reason="no_active_boundaries")


