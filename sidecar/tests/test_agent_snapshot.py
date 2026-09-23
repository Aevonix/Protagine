"""Tests for the the agent heartbeat agent-snapshot endpoints."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from types import SimpleNamespace
from fastapi.testclient import TestClient

from protagine.telemetry import TelemetryStore
from protagine.initiatives.store import InitiativeStore


class TestAgentSnapshot:
    """Tests for GET /v1/host/agent-snapshot and POST /v1/host/agent-snapshot/record-outreach."""

    @pytest.fixture
    def client(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
        """Create a test client with telemetry and initiative stores injected."""
        from protagine.api.routers import host as host_mod
        from protagine.api.routers import mind as mind_mod
        from protagine.api.routers.host import (
            set_telemetry,
            set_initiative_store,
        )
        from protagine.server import create_app

        monkeypatch.setenv("PROTAGINE_API_KEY", "test-api-key")

        # Save module globals — leaking a Mock autonomy loop poisons
        # other test modules (e.g. test_sidecar's not-wired tests).
        prev = (
            host_mod._telemetry,
            host_mod._initiative_store,
            mind_mod.get_mind(),
        )

        telemetry = TelemetryStore()
        initiative_store = InitiativeStore(state_dir=tmp_path)

        set_telemetry(telemetry)
        set_initiative_store(initiative_store)

        # Mock autonomy loop
        # The status routes read the mind: its level is the reported mode and
        # its switch the running flag.
        mind_mod.set_mind(SimpleNamespace(level="proactive", enabled=True, ticks=0))

        app = create_app()
        yield TestClient(app, headers={"Authorization": "Bearer test-api-key"})

        set_telemetry(prev[0])
        set_initiative_store(prev[1])
        mind_mod.set_mind(prev[2])

    # -----------------------------------------------------------------------
    # GET /v1/host/agent-snapshot
    # -----------------------------------------------------------------------

    def test_agent_snapshot_empty(self, client: TestClient):
        """Snapshot with no initiatives or telemetry history."""
        resp = client.get("/v1/host/agent-snapshot")
        assert resp.status_code == 200
        data = resp.json()

        assert "timestamp" in data
        assert data["pending_count"] == 0
        assert data["assigned_count"] == 0
        assert data["failed_count"] == 0
        assert data["pending_initiatives"] == []
        assert data["recently_completed"] == []
        assert data["flags"] == []
        assert data["autonomy_mode"] == "proactive"
        assert data["autonomy_running"] is True
        assert data["last_tick_age_minutes"] is None
        assert data["telemetry"]["last_agent_outreach_at"] is None

    def test_agent_snapshot_with_initiatives(self, client: TestClient, tmp_path: Path):
        """Snapshot reflects pending and failed initiatives."""
        from protagine.api.routers.host import set_initiative_store
        from protagine.initiatives.store import InitiativeStore

        store = InitiativeStore(state_dir=tmp_path)
        set_initiative_store(store)

        # Insert initiatives
        store.create(
            type="test",
            description="High priority pending",
            priority=0.95,
            rationale="test",
        )
        failed_init = store.create(
            type="test",
            description="Failed initiative",
            priority=0.5,
            rationale="test",
        )
        store.fail(failed_init.id, agent_id="test-agent", reason="timeout")

        resp = client.get("/v1/host/agent-snapshot")
        assert resp.status_code == 200
        data = resp.json()

        assert data["pending_count"] == 1
        assert data["failed_count"] == 1
        assert len(data["pending_initiatives"]) == 1
        assert data["pending_initiatives"][0]["description"] == "High priority pending"
        assert data["pending_initiatives"][0]["priority"] == 0.95
        assert len(data["recently_completed"]) == 0
        assert "high_priority_pending" in data["flags"]
        assert "failed_initiatives" in data["flags"]

    def test_agent_snapshot_stale_tick(self, client: TestClient):
        """Flag stale_autonomy_loop when last tick is old."""
        from protagine.api.routers.host import set_telemetry
        from protagine.telemetry import TelemetryStore

        telemetry = TelemetryStore()
        telemetry.last_tick_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        set_telemetry(telemetry)

        resp = client.get("/v1/host/agent-snapshot")
        assert resp.status_code == 200
        data = resp.json()

        assert data["last_tick_age_minutes"] is not None
        assert data["last_tick_age_minutes"] > 30
        assert "stale_autonomy_loop" in data["flags"]

    def test_agent_snapshot_long_initiative_silence(self, client: TestClient):
        """Flag long_initiative_silence when no initiatives for 4+ hours."""
        from protagine.api.routers.host import set_telemetry
        from protagine.telemetry import TelemetryStore

        telemetry = TelemetryStore()
        telemetry.last_initiative_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        set_telemetry(telemetry)

        resp = client.get("/v1/host/agent-snapshot")
        assert resp.status_code == 200
        data = resp.json()

        assert "long_initiative_silence" in data["flags"]

    # -----------------------------------------------------------------------
    # POST /v1/host/agent-snapshot/record-outreach
    # -----------------------------------------------------------------------

    def test_record_outreach(self, client: TestClient):
        """Record outreach updates telemetry and returns timestamps."""
        resp = client.post("/v1/host/agent-snapshot/record-outreach", json={
            "agent_id": "test-agent",
            "channel": "whatsapp",
            "reason": "test",
        })
        assert resp.status_code == 200
        data = resp.json()

        assert "recorded_at" in data
        assert "last_agent_outreach_at" in data
        assert data["last_agent_outreach_at"] is not None

    def test_record_outreach_then_snapshot(self, client: TestClient):
        """After recording outreach, snapshot reflects the new timestamp."""
        client.post("/v1/host/agent-snapshot/record-outreach", json={
            "agent_id": "test-agent",
            "channel": "whatsapp",
            "reason": "test",
        })

        resp = client.get("/v1/host/agent-snapshot")
        assert resp.status_code == 200
        data = resp.json()

        assert data["telemetry"]["last_agent_outreach_at"] is not None
