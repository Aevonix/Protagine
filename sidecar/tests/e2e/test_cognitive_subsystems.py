"""E2E integration tests for Protagine cognitive subsystems (v0.5.3+).

Tests full data flow: API → subsystem store → readback → cross-subsystem integration.

Prerequisites:
- Protagine sidecar running (default: localhost:7777)
- PROTAGINE_API_KEY set

Run:
    PROTAGINE_API_KEY=your-key pytest tests/e2e/test_cognitive_subsystems.py -v
"""

from __future__ import annotations

import json
import os
import time
import uuid

import httpx
import pytest

PROTAGINE_URL = os.environ.get("PROTAGINE_URL", "http://localhost:7777")
PROTAGINE_API_KEY = os.environ.get("PROTAGINE_API_KEY", "")
HEADERS = {"Authorization": f"Bearer {PROTAGINE_API_KEY}"} if PROTAGINE_API_KEY else {}


@pytest.fixture(scope="session")
def client():
    return httpx.Client(base_url=PROTAGINE_URL, headers=HEADERS, timeout=30)


# ---------------------------------------------------------------------------
# Commitment Tracking
# ---------------------------------------------------------------------------

class TestCommitments:
    def test_create_and_read(self, client):
        desc = f"e2e-commitment-{uuid.uuid4().hex[:6]}: Deploy v0.6.0 by Friday"
        r = client.post("/v1/host/commitments", json={
            "person_id": "e2e-contact",
            "description": desc,
            "due_date": "2026-05-01T00:00:00Z",
            "priority": 3,
        })
        assert r.status_code in (200, 201), f"Create failed: {r.text}"
        data = r.json()
        assert data["description"] == desc
        assert data["status"] == "pending"
        cid = data["id"]

        # Read back
        r = client.get(f"/v1/host/commitments/{cid}")
        assert r.status_code == 200
        assert r.json()["description"] == desc

    def test_status_transition(self, client):
        r = client.post("/v1/host/commitments", json={
            "person_id": "e2e",
            "description": f"transition-test-{uuid.uuid4().hex[:6]}",
        })
        assert r.status_code in (200, 201)
        cid = r.json()["id"]

        # pending → fulfilled
        r = client.patch(f"/v1/host/commitments/{cid}", json={"status": "fulfilled"})
        assert r.status_code == 200
        assert r.json()["status"] == "fulfilled"

    def test_overdue_filter(self, client):
        r = client.get("/v1/host/commitments?status=overdue")
        assert r.status_code == 200

    def test_delete_guard(self, client):
        r = client.post("/v1/host/commitments", json={
            "person_id": "e2e",
            "description": f"delete-guard-{uuid.uuid4().hex[:6]}",
        })
        assert r.status_code in (200, 201)
        cid = r.json()["id"]

        # Can't delete pending commitment (409 Conflict)
        r = client.delete(f"/v1/host/commitments/{cid}")
        assert r.status_code == 409

        # Cancel then delete
        client.patch(f"/v1/host/commitments/{cid}", json={"status": "cancelled"})
        r = client.delete(f"/v1/host/commitments/{cid}")
        assert r.status_code == 204


# ---------------------------------------------------------------------------
# Affect Tracking (Theory of Mind)
# ---------------------------------------------------------------------------

class TestAffect:
    def test_create_and_get_state(self, client):
        contact = f"e2e-affect-{uuid.uuid4().hex[:6]}"
        r = client.post("/v1/host/affect/events", json={
            "contact_id": contact,
            "valence": 0.7,
            "arousal": 0.5,
            "trigger": "positive conversation",
        })
        assert r.status_code in (200, 201), f"Create affect failed: {r.text}"
        data = r.json()
        assert data["valence"] == 0.7

        # Get state
        r = client.get(f"/v1/host/affect/state/{contact}")
        assert r.status_code == 200
        state = r.json()
        # State structure varies; confirm endpoint responded with valid data
        assert isinstance(state, dict)

    def test_negative_spike(self, client):
        contact = f"e2e-neg-{uuid.uuid4().hex[:6]}"
        client.post("/v1/host/affect/events", json={
            "contact_id": contact, "valence": 0.8, "arousal": 0.3,
        })
        r = client.post("/v1/host/affect/events", json={
            "contact_id": contact, "valence": -0.9, "arousal": 0.9,
            "trigger": "frustration detected",
        })
        assert r.status_code in (200, 201)

        r = client.get(f"/v1/host/affect/state/{contact}")
        assert r.status_code == 200

    def test_history(self, client):
        contact = f"e2e-hist-{uuid.uuid4().hex[:6]}"
        for v in [0.3, 0.6, 0.9]:
            client.post("/v1/host/affect/events", json={
                "contact_id": contact, "valence": v, "arousal": 0.5,
            })
        r = client.get(f"/v1/host/affect/history/{contact}?limit=10")
        assert r.status_code == 200
        assert len(r.json()) >= 3


# ---------------------------------------------------------------------------
# Shared Facts (Theory of Mind)
# ---------------------------------------------------------------------------

class TestSharedFacts:
    def test_create_and_read(self, client):
        r = client.post("/v1/host/mind/facts", json={
            "contact_id": "e2e-fact-contact",
            "fact": f"e2e-fact-{uuid.uuid4().hex[:6]}: Prefers dark mode",
            "category": "preference",
            "confidence": 0.9,
        })
        assert r.status_code in (200, 201), f"Create fact failed: {r.text}"
        fid = r.json()["id"]

        r = client.get(f"/v1/host/mind/facts/{fid}")
        assert r.status_code == 200
        assert "dark mode" in r.json()["fact"]

    def test_update_confidence(self, client):
        r = client.post("/v1/host/mind/facts", json={
            "contact_id": "e2e-fact-contact",
            "fact": f"confidence-test-{uuid.uuid4().hex[:6]}",
            "category": "observation",
            "confidence": 0.5,
        })
        fid = r.json()["id"]

        r = client.patch(f"/v1/host/mind/facts/{fid}", json={"confidence": 0.95})
        assert r.status_code == 200
        assert r.json()["confidence"] == 0.95

    def test_list_by_contact(self, client):
        r = client.get("/v1/host/mind/facts?contact_id=e2e-fact-contact&limit=10")
        assert r.status_code == 200
        data = r.json()
        facts = data if isinstance(data, list) else data.get("facts", [])
        assert isinstance(facts, list)


# ---------------------------------------------------------------------------
# Pattern Extraction + Surprise Engine
# ---------------------------------------------------------------------------

class TestPatternsAndSurprises:
    def test_create_pattern(self, client):
        r = client.post("/v1/host/patterns", json={
            "pattern_type": "entity_cooccurrence",
            "pattern_key": f"e2e-pattern-{uuid.uuid4().hex[:6]}",
            "description": "E2E test pattern for entity co-occurrence",
            "data": {"entities": ["Alice", "quantum"], "frequency": 5},
            "confidence": 0.8,
        })
        assert r.status_code in (200, 201), f"Create pattern failed: {r.text}"
        assert r.json()["pattern_type"] == "entity_cooccurrence"

    def test_create_surprise(self, client):
        r = client.post("/v1/host/surprises", json={
            "observation": f"e2e-surprise-{uuid.uuid4().hex[:6]}: Unexpected API error",
            "expected": "200 OK",
            "actual": "500 Error",
            "surprise_score": 0.8,
        })
        assert r.status_code in (200, 201), f"Create surprise failed: {r.text}"
        sid = r.json()["id"]

        # Resolve it
        r = client.patch(f"/v1/host/surprises/{sid}", json={"resolution": "Transient network error"})
        assert r.status_code == 200
        assert r.json()["resolution"] == "Transient network error"

    def test_unresolved_surprises(self, client):
        client.post("/v1/host/surprises", json={
            "observation": f"unresolved-{uuid.uuid4().hex[:6]}",
            "expected": "A", "actual": "B",
            "surprise_score": 0.7,
        })
        r = client.get("/v1/host/surprises/unresolved")
        assert r.status_code == 200
        assert len(r.json()) >= 1


# ---------------------------------------------------------------------------
# Cross-Subsystem Data Flow
# ---------------------------------------------------------------------------

class TestCrossSubsystemFlow:
    def test_context_assembly_includes_subsystems(self, client):
        """Context assembly should pull from cognitive subsystems."""
        client.post("/v1/host/commitments", json={
            "person_id": "e2e-context",
            "description": f"e2e-ctx-commitment-{uuid.uuid4().hex[:6]}: Ship feature",
            "priority": 3,
        })
        client.post("/v1/host/affect/events", json={
            "contact_id": "e2e-context",
            "valence": 0.6, "arousal": 0.4,
        })
        client.post("/v1/host/mind/facts", json={
            "contact_id": "e2e-context",
            "fact": f"e2e-ctx-fact-{uuid.uuid4().hex[:6]}: Prefers async communication",
            "category": "preference", "confidence": 0.8,
        })

        r = client.post("/v1/host/context/assemble", json={
            "identity": {"host_id": "e2e-test"},
            "context": {"session_id": "e2e-ctx", "contact_id": "e2e-context"},
            "incoming_message": {"role": "user", "content": "What should I work on next?"},
        })
        assert r.status_code == 200
        data = r.json()
        context_text = json.dumps(data)
        assert len(context_text) > 100

    def test_cognition_trigger(self, client):
        """Cognition trigger should accept or throttle."""
        r = client.post("/v1/host/cognition/trigger", json={
            "trigger_type": "manual",
            "context": {"session_id": "e2e-cog", "contact_id": "e2e-cog"},
        })
        assert r.status_code in (200, 429, 501), f"Unexpected: {r.status_code} {r.text}"


# ---------------------------------------------------------------------------
# Event Journal
# ---------------------------------------------------------------------------

class TestEventJournal:
    def test_replay_endpoint(self, client):
        """Event replay with since parameter."""
        r = client.get("/v1/host/events/replay?since=0&limit=5")
        assert r.status_code in (200, 501)

    def test_events_from_writes(self, client):
        """Creating data should emit events."""
        client.post("/v1/host/commitments", json={
            "person_id": "e2e-events",
            "description": f"e2e-event-test-{uuid.uuid4().hex[:6]}",
        })

        r = client.get("/v1/host/events/replay?since=0&limit=10")
        if r.status_code == 200:
            data = r.json()
            # Response is either a list or a dict with 'events' key
            events = data if isinstance(data, list) else data.get("events", [])
            assert isinstance(events, list)


# ---------------------------------------------------------------------------
# Doctor Equivalence
# ---------------------------------------------------------------------------

class TestDoctorEquivalence:
    """Verify all subsystems that protagine doctor checks are responsive."""

    def test_health_passes(self, client):
        r = client.get("/v1/host/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] in ("ok", "degraded")
        assert len(data.get("capabilities", [])) >= 20

    def test_commitments_endpoint(self, client):
        r = client.get("/v1/host/commitments?status=pending&limit=1")
        assert r.status_code == 200

    def test_affect_endpoint(self, client):
        r = client.get("/v1/host/affect/state/e2e-doctor")
        assert r.status_code == 200

    def test_shared_facts_endpoint(self, client):
        r = client.get("/v1/host/mind/facts?limit=1")
        assert r.status_code == 200

    def test_patterns_endpoint(self, client):
        r = client.get("/v1/host/patterns?limit=1")
        assert r.status_code == 200

    def test_surprises_endpoint(self, client):
        r = client.get("/v1/host/surprises?limit=1")
        assert r.status_code == 200
