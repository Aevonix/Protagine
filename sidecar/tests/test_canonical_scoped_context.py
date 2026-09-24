"""A guest's context is contact-scoped: exact recall, proven shared commitments and
their own digest, and no private legacy producer is ever queried."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
import pytest

from protagine.api.middleware import ApiKeyMiddleware
from protagine.api.routers import host
from protagine.beliefs.source_projection import SourceClaimProjection
from protagine.commitments.store import CommitmentStore
from protagine.turns import TurnIdempotencyLedger
from protagine.turns.media import SourceMedia
from onekey import KEY, _principal, _write_keyring
from test_source_claim_projection import Model, claim
from test_source_media import message as image_message
from test_turn_source_evidence import source_app


class PrivateProducer:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        self.calls.append(name)
        raise AssertionError("private legacy producer was queried")


class GuestContacts(PrivateProducer):
    """The contact store answers one read: the viewer's own record, for their digest."""

    def __init__(self):
        super().__init__()
        self.reads = []

    async def get(self, contact_id):
        self.reads.append(contact_id)
        return SimpleNamespace(contact_id=contact_id, digest=f"Digest of {contact_id}.")


def headers(person):
    return {"Authorization": "Bearer " + KEY}


def context(person, query, session="second-session"):
    return {
        "identity": {"host_id": "fixture"},
        "context": {"contact_id": person, "session_id": session},
        "incoming_message": {"role": "user", "content": query},
    }


@pytest.mark.asyncio
async def test_guest_http_capture_claim_media_commitment_and_digest_recall(
    source_app, tmp_path, monkeypatch,
):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", "owner")
    monkeypatch.setenv("PROTAGINE_RECALL_RERANK", "off")
    principals = []
    for person in ("guest-a", "guest-b", "owner"):
        principal = _principal(principal=person, secret="fixture-" + person, viewer=person)
        principal["allow_unscoped_api"] = False
        principals.append(principal)
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, principals)
    source_app.add_middleware(ApiKeyMiddleware, api_key=KEY)

    async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
        text = "My office is in River."
        for person, content in (
            ("guest-a", text), ("guest-b", "My office is other-guest-secret."),
            ("owner", "My office is owner-secret."),
        ):
            turn = "turn-" + person
            response = await client.put("/v2/host/turns/" + turn, headers=headers(person), json={
                "identity": {"host_id": "fixture"},
                "context": {"contact_id": person, "session_id": "first-session", "turn_id": turn},
                "user_message": {"role": "user", "content": content},
                "assistant_message": {"role": "assistant", "content": "I will prepare the office handout."} if person == "guest-a" else None,
            })
            assert response.status_code == 201, response.text
        ledger = TurnIdempotencyLedger(tmp_path / "turn-idempotency.db")
        projection = SourceClaimProjection(ledger)
        assert await projection.process_one(Model({text: claim(text, "River")}))
        ledger.record_source("guest-image", contact_id="guest-a", session_id="first-session",
                             messages=[image_message()])
        media = SourceMedia(ledger)
        assert media.finish(media.claim_job(), description="An orchid beside a red rectangle.", model="fixture-vision")
        ledger.record_source("old-checkpoint", contact_id="guest-a", session_id="first-session", scope="session",
                             messages=[{"role": "user", "content": "office mixed-speaker-secret"}])
        commitments = CommitmentStore(tmp_path / "commitments.db")
        due = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        own = commitments.create("guest-a", "Prepare the office handout", due_at=due,
                                 metadata={"source_turn_id": "turn-guest-a"})
        commitments.create("guest-a", "owner-private task ABOUT the guest", due_at=due)
        commitments.create("guest-a", "private task with an invented link", due_at=due,
                           metadata={"source_turn_id": "turn-guest-a"})
        commitments.create("guest-a", "My office is owner-secret", due_at=due,
                           metadata={"source_turn_id": "turn-owner"})
        commitments.create("owner", "owner-secret obligation", due_at=due)
        commitments.create("guest-b", "other-guest-secret obligation", due_at=due)
        monkeypatch.setattr(host, "_commitment_store", commitments)
        private = PrivateProducer()
        for name in ("_graph", "_facts_store", "_goals_store", "_initiative_store",
                     "_briefings_engine", "_world_store", "_skills_registry", "_affect_store",
                     "_preference_learner", "_comms_log"):
            monkeypatch.setattr(host, name, private)
        contacts = GuestContacts()
        monkeypatch.setattr(host, "_contacts_store", contacts)

        response = await client.post("/v1/host/context/assemble", json=context("guest-a", "office"), headers=headers("guest-a"))
        assert response.status_code == 200, response.text
        sections = {row["id"]: row["body"] for row in response.json()["sections"]}
        assert "source_assertion" in sections["protagine-memory"] and "River" in sections["protagine-memory"]
        assert own["id"] in sections["protagine-commitments"] and "Prepare the office handout" in sections["protagine-commitments"]
        assert sections["protagine-person"] == "Digest of guest-a."
        assert set(sections) == {"temporal-context", "protagine-memory", "protagine-commitments", "protagine-person"}
        assert contacts.reads == ["guest-a"]
        assert not any(secret in response.text for secret in ("owner-secret", "other-guest-secret", "mixed-speaker-secret", "owner-private", "invented link"))
        assert "omitted" in response.json()["notices"][0]

        image = await client.post("/v1/host/context/assemble", json=context("guest-a", "orchid"), headers=headers("guest-a"))
        assert image.status_code == 200 and "orchid" in image.text and "derived_unverified" in image.text
        other = await client.post("/v1/host/context/assemble", json=context("guest-b", "orchid"), headers=headers("guest-b"))
        assert other.status_code == 200 and "orchid" not in other.text
        assert "Digest of guest-b." in other.text and "Digest of guest-a." not in other.text
        # A memory provider from before this release still sends the retired projection policy on
        # every guest prefetch. A guest's context is contact-scoped by construction, so the field
        # changes nothing: the guest keeps its own recall (audit B4).
        retired = await client.post("/v1/host/context/assemble", headers=headers("guest-a"),
                                    json={**context("guest-a", "office"), "projection_policy": "scoped_viewer_required"})
        assert retired.status_code == 200, retired.text
        again = {row["id"]: row["body"] for row in retired.json()["sections"]}
        assert set(again) == set(sections) and all(again[key] == sections[key] for key in sections
                                                   if key != "temporal-context")
        missing = await client.post("/v1/host/context/assemble", json=context("guest-a", "office"))
        assert missing.status_code == 401
        assert contacts.reads == ["guest-a", "guest-a", "guest-b", "guest-a"]
        assert private.calls == [] and contacts.calls == []

        monkeypatch.setenv("PROTAGINE_RECALL_CONTEXT_MAX_CHARS", "80")
        small = await client.post("/v1/host/context/assemble", json=context("guest-a", "office"), headers=headers("guest-a"))
        assert small.status_code == 200
        assert all(len(row["body"]) <= 80 for row in small.json()["sections"] if row["id"] == "protagine-memory")
        assert private.calls == []

        ledger.erase_sources(contact_id="guest-a", turn_ids=["turn-guest-a"])
        erased = await client.post("/v1/host/context/assemble", json=context("guest-a", "office"), headers=headers("guest-a"))
        assert erased.status_code == 200
        assert "protagine-commitments" not in erased.text and "River" not in erased.text
        assert private.calls == [] and contacts.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_configured", [True, False])
async def test_a_guest_is_contact_scoped_without_the_key_and_without_a_configured_owner(
    source_app, tmp_path, monkeypatch, owner_configured,
):
    """Audit M7: the guest projection fails closed. A development caller without the key is never
    the owner, so a context it asks for about anyone is the contact-scoped one; and with no owner
    configured nobody's view is the owner's."""
    for name in ("PROTAGINE_OWNER_CONTACT_ID", "PROTAGINE_OWNER_PERSON_ID"):
        monkeypatch.delenv(name, raising=False)
    if owner_configured:
        monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", "owner")
    private = PrivateProducer()
    for name in ("_graph", "_facts_store", "_goals_store", "_initiative_store", "_briefings_engine",
                 "_world_store", "_skills_registry", "_affect_store", "_preference_learner", "_comms_log"):
        monkeypatch.setattr(host, name, private)
    monkeypatch.setattr(host, "_commitment_store", None)
    contacts = GuestContacts()
    monkeypatch.setattr(host, "_contacts_store", contacts)
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
        response = await client.post("/v1/host/context/assemble", json=context("guest-a", "office"))
    assert response.status_code == 200, response.text
    sections = {row["id"] for row in response.json()["sections"]}
    assert sections <= {"temporal-context", "protagine-memory", "protagine-commitments", "protagine-person"}
    assert private.calls == [] and contacts.calls == [] and contacts.reads == ["guest-a"]
    assert "omitted" in response.json()["notices"][0]


@pytest.mark.asyncio
async def test_the_person_section_is_bounded_like_the_template_digest(source_app, tmp_path, monkeypatch):
    """Integration map X16: the person section is M5's per-turn cost, at most the template
    digest's 600 characters on every non-owner turn, whoever wrote the stored digest."""
    from protagine.contacts.digest import MAX_CHARS
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", "owner")

    class LongDigest(GuestContacts):
        async def get(self, contact_id):
            return SimpleNamespace(contact_id=contact_id, digest="A long generated digest. " * 200)

    monkeypatch.setattr(host, "_contacts_store", LongDigest())
    monkeypatch.setattr(host, "_commitment_store", None)
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
        response = await client.post("/v1/host/context/assemble", json=context("guest-a", "office"))
    person, = [row["body"] for row in response.json()["sections"] if row["id"] == "protagine-person"]
    assert 0 < len(person) <= MAX_CHARS
