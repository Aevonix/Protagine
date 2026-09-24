"""The recipient-scoped packet and the guest projection of /context/assemble (M5).

A non-owner viewer gets the contact-scoped sections and never an owner-only one; the
mind composes messages to a contact from that same packet (architecture 6.3). The
contact's own digest rides along as "About this person". Guests are never refused
for lacking an attested projection; the retired projection policy is refused as
unsupported instead of selecting anything.
"""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from protagine.api.errors import install_exception_handlers
from protagine.api.middleware import ApiKeyMiddleware
from protagine.api.routers import host
from protagine.api.schemas.host import (
    ContactIntroRequest, ContextAssembleRequest, HostIdentity, HostMessage, HostTurnContext,
    TurnSyncRequest,
)
from onekey import KEY

CANARY = "amber-cobalt-42"
OWNER = "cid-owner"
GUEST = "cid-guest"
DIGEST = "p-07: a regular contact, met through the owner. Open: the venue booking."


class Row:
    def __init__(self, contact_id, **fields):
        self.contact_id = contact_id
        self.display_name = fields.pop("display_name", contact_id)
        self.given_name = None
        self.timezone = None
        self.last_interaction_at = None
        self.trust_tier = fields.pop("trust_tier", "regular")
        self.interaction_count = 3
        self.digest = fields.pop("digest", None)
        for key, value in fields.items():
            setattr(self, key, value)

    def to_dict(self):
        return {"contact_id": self.contact_id, "display_name": self.display_name,
                "trust_tier": self.trust_tier}


class Contacts:
    """The contact-store surface these routes touch, recording every write."""

    def __init__(self, digest=DIGEST):
        self.rows = {GUEST: Row(GUEST, display_name="p-07", digest=digest), OWNER: Row(OWNER, trust_tier="owner")}
        self.created, self.handles, self.interactions = [], [], []

    async def get(self, contact_id):
        return self.rows.get(contact_id)

    async def compute_cadence_overdue(self, **kwargs):
        return []

    async def record_interaction(self, contact_id, *args, **kwargs):
        self.interactions.append(contact_id)
        return True

    async def resolve_messaging_handle(self, gateway, address):
        return None

    async def create(self, **kwargs):
        self.created.append(kwargs)
        row = Row("cid-new", display_name=kwargs.get("display_name"), trust_tier=kwargs.get("trust_tier"))
        self.rows[row.contact_id] = row
        return row

    async def add_handle(self, contact_id, gateway=None, address=None, **kwargs):
        self.handles.append({"contact_id": contact_id, "gateway": gateway, "address": address, **kwargs})
        return SimpleNamespace(handle_id="h-1")

    async def get_handles(self, contact_id):
        return []


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    monkeypatch.delenv("PROTAGINE_OWNER_PERSON_ID", raising=False)
    for name in ("_graph", "_presence_store", "_telemetry", "_reranker", "_context_recall_selector",
                 "_commitment_store", "_initiative_store", "_world_store", "_situation_store",
                 "_comms_log", "_facts_store", "_affect_store", "_signal_collector"):
        monkeypatch.setattr(host, name, None)
    contacts = Contacts()
    monkeypatch.setattr(host, "_contacts_store", contacts)
    # Owner-only producers, each carrying the canary.
    from protagine.goals.models import GoalStatus
    goals = SimpleNamespace(list_goals=lambda status=None: [SimpleNamespace(
        priority=SimpleNamespace(name="HIGH"), title=CANARY, description="owner goal", progress_pct=0.5)]
        if status == GoalStatus.ACTIVE else [])
    monkeypatch.setattr(host, "_goals_store", goals)
    monkeypatch.setattr(host, "_preference_learner", SimpleNamespace(
        perspective=None, build_brief=lambda source_ids=None: "Prefers bullets; " + CANARY))
    return contacts


def app_with_key():
    app = FastAPI()
    install_exception_handlers(app)
    app.add_middleware(ApiKeyMiddleware, api_key=KEY)
    app.include_router(host.router)
    app.include_router(host.v2_router)
    return app


def assemble_body(contact_id, query="hello", **extra):
    return {"identity": {"host_id": "fixture"},
            "context": {"contact_id": contact_id, "session_id": "s-1"},
            "incoming_message": {"role": "user", "content": query}, **extra}


# --- the packet ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_owner_packet_has_no_owner_only_section_and_carries_the_digest(wired):
    packet = await host.assemble_packet(GUEST, query="the venue")
    assert CANARY not in packet
    assert "## About this person" in packet and DIGEST in packet
    assert "Active Goals" not in packet and "How they want me to communicate" not in packet
    # The owner's own packet is the owner's context: the exclusion is by viewer, not by producer.
    owner = await host.assemble_packet(OWNER, query="the venue")
    assert CANARY in owner and "About this person" not in owner


@pytest.mark.asyncio
async def test_packet_is_bounded_and_empty_for_nobody(wired):
    wired.rows[GUEST].digest = "x" * 3000
    assert len(await host.assemble_packet(GUEST, limit_chars=500)) <= 500
    assert await host.assemble_packet("") == ""


@pytest.mark.asyncio
async def test_digest_section_needs_a_digest(wired):
    wired.rows[GUEST].digest = None
    packet = await host.assemble_packet(GUEST)
    assert "About this person" not in packet


# --- the route ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_guest_assemble_is_contact_scoped_and_never_refused(wired):
    async with AsyncClient(transport=ASGITransport(app=app_with_key()), base_url="http://test") as client:
        headers = {"Authorization": "Bearer " + KEY}
        guest = await client.post("/v1/host/context/assemble", json=assemble_body(GUEST), headers=headers)
        assert guest.status_code == 200, guest.text
        ids = {row["id"]: row for row in guest.json()["sections"]}
        assert "protagine-person" in ids and ids["protagine-person"]["body"] == DIGEST
        assert ids["protagine-person"]["priority"] == 84
        assert "protagine-goals" not in ids and "protagine-owner-preferences" not in ids
        assert CANARY not in guest.text
        assert "omitted" in guest.json()["notices"][0]
        assert guest.json().get("projection_attestation") is None
        # The owner with the key is the owner's context; no attestation gate stands in the way.
        owner = await client.post("/v1/host/context/assemble", json=assemble_body(OWNER), headers=headers)
        assert owner.status_code == 200, owner.text
        owner_ids = {row["id"] for row in owner.json()["sections"]}
        assert {"protagine-goals", "protagine-owner-preferences"} <= owner_ids
        assert "protagine-person" not in owner_ids and owner.json()["notices"] is None


@pytest.mark.asyncio
async def test_a_stale_providers_projection_policy_changes_nothing(wired):
    """Audit B4: the memory provider before this release sends ``projection_policy`` on every guest
    prefetch; refusing it would leave every guest without recall. It is ignored: the guest context
    is contact-scoped by construction."""
    async with AsyncClient(transport=ASGITransport(app=app_with_key()), base_url="http://test") as client:
        headers = {"Authorization": "Bearer " + KEY}
        plain = await client.post("/v1/host/context/assemble", headers=headers, json=assemble_body(GUEST))
        response = await client.post("/v1/host/context/assemble", headers=headers,
                                     json=assemble_body(GUEST, projection_policy="scoped_viewer_required"))
        assert response.status_code == 200, response.text
        ids = [row["id"] for row in response.json()["sections"]]
        assert ids == [row["id"] for row in plain.json()["sections"]] and "protagine-person" in ids
        assert "protagine-goals" not in ids
        assert "projection_policy" not in host.ContextAssembleRequest.model_fields
        assert (await client.get("/v1/host/context/projection-readiness", params={"contact_id": GUEST},
                                 headers={"Authorization": "Bearer " + KEY})).status_code == 404


@pytest.mark.asyncio
async def test_in_process_assemble_for_a_guest_returns_sections(wired):
    body = ContextAssembleRequest(identity=HostIdentity(host_id="fixture"),
                                  context=HostTurnContext(session_id="s", contact_id=GUEST),
                                  incoming_message=HostMessage(role="user", content="hi"))
    response = await host.context_assemble(body)
    assert any(section.id == "protagine-person" for section in response.sections)
    assert not any(section.id == "protagine-goals" for section in response.sections)


@pytest.mark.asyncio
async def test_guest_time_brief_carries_no_owner_heads_up(wired, monkeypatch):
    # The memory provider refreshes the time brief every turn; the owner's overdue
    # errands and neglected contacts are owner context, never a guest's.
    overdue = [{"description": "owner errand " + CANARY, "due_at": "2026-01-01T00:00:00+00:00"}]
    monkeypatch.setattr(host, "_commitment_store", SimpleNamespace(get_overdue=lambda: overdue))
    async with AsyncClient(transport=ASGITransport(app=app_with_key()), base_url="http://test") as client:
        headers = {"Authorization": "Bearer " + KEY}
        guest = await client.get("/v1/host/context/temporal", params={"contact_id": GUEST}, headers=headers)
        assert guest.status_code == 200, guest.text
        assert CANARY not in guest.text and "Heads-up" not in guest.text
        owner = await client.get("/v1/host/context/temporal", params={"contact_id": OWNER}, headers=headers)
        assert CANARY in owner.text


def test_retired_capabilities_are_not_advertised(wired):
    caps = host.supported_capabilities()
    assert "tom_extract" not in caps and "tom_p8_shadow" not in caps


# --- claims for the digest ----------------------------------------------------------

@pytest.mark.asyncio
async def test_claims_for_reads_the_contacts_own_current_claims(wired, tmp_path):
    from protagine.beliefs.source_projection import SourceClaimProjection
    from protagine.turns import get_turn_idempotency_ledger
    from test_source_claim_projection import Model, claim
    ledger = get_turn_idempotency_ledger(tmp_path)
    text = "My office is in River."
    ledger.record_source("turn-guest", contact_id=GUEST, session_id="first",
                         messages=[{"role": "user", "content": text}])
    other = "My office is in Harbor."
    ledger.record_source("turn-owner", contact_id=OWNER, session_id="first",
                         messages=[{"role": "user", "content": other}])
    projection = SourceClaimProjection(ledger)
    model = Model({text: claim(text, "River"), other: claim(other, "Harbor")})
    while await projection.process_one(model):
        pass
    guest_claims = await host.claims_for(GUEST)
    assert guest_claims == ["office location: River"]
    assert await host.claims_for(OWNER) == ["office location: Harbor"]
    assert await host.claims_for("cid-nobody") == []
    assert await host.claims_for(GUEST, limit=0) == []


# --- the 2.8 edits for the other parts -------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_with_create_uses_the_participant_ladder(wired):
    async with AsyncClient(transport=ASGITransport(app=app_with_key()), base_url="http://test") as client:
        response = await client.get("/v1/host/contacts/resolve", headers={"Authorization": "Bearer " + KEY},
                                    params={"gateway": "whatsapp", "address": "+15550001", "create": "true"})
        assert response.status_code == 200, response.text
        assert response.json()["contact_id"] == "cid-new"
    created, = wired.created
    assert created["import_source"] == "auto:sender" and created["trust_tier"] == "unknown"
    assert "interaction_allowed" not in created or created["interaction_allowed"] is False
    handle, = wired.handles
    assert (handle["gateway"], handle["address"], handle["source"]) == ("whatsapp", "+15550001", "auto:sender")


@pytest.mark.asyncio
async def test_intro_creates_an_ask_contact_and_keeps_the_transport(wired):
    response = await host.capture_introduction(ContactIntroRequest(
        name="p-05", gateway="rcs", address="+15550005", introduced_by=OWNER))
    assert response.created is True
    created, = wired.created
    assert created["may_contact"] == "ask" and "interaction_allowed" not in created
    assert created["import_source"] == "agent_intro" and created["introduced_by"] == OWNER
    handle, = wired.handles
    assert handle["gateway"] == "rcs" and handle["address"] == "+15550005"


@pytest.mark.asyncio
async def test_turns_sync_runs_opt_out_detection_on_the_resolved_contact(wired, monkeypatch):
    apply = AsyncMock(return_value=None)
    fake = types.ModuleType("protagine.contacts.optout")
    fake.apply_opt_out = apply
    monkeypatch.setitem(sys.modules, "protagine.contacts.optout", fake)
    body = TurnSyncRequest(identity=HostIdentity(host_id="fixture"),
                           context=HostTurnContext(session_id="s-1", contact_id=GUEST, turn_id="turn-9"),
                           user_message=HostMessage(role="user", content="please stop messaging me"))
    response = await host.turns_sync(body)
    assert response.accepted is True
    apply.assert_awaited_once()
    args, kwargs = apply.await_args
    assert args == (wired, GUEST, "please stop messaging me")
    assert kwargs == {"source_ref": "turn:turn-9", "owner_id": OWNER}
    assert wired.interactions == [GUEST]


@pytest.mark.asyncio
async def test_system_turns_skip_opt_out_detection(wired, monkeypatch):
    apply = AsyncMock(return_value=None)
    fake = types.ModuleType("protagine.contacts.optout")
    fake.apply_opt_out = apply
    monkeypatch.setitem(sys.modules, "protagine.contacts.optout", fake)
    body = TurnSyncRequest(identity=HostIdentity(host_id="fixture"),
                           context=HostTurnContext(session_id="s-1", contact_id="whoever", channel_id="cron:tick"),
                           user_message=HostMessage(role="user", content="stop"))
    await host.turns_sync(body)
    apply.assert_not_awaited()
