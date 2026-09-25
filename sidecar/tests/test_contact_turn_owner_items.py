"""A contact's turn sees the owner's open items that involve them: the item and its due time only.

The contact session has no transcript search and recall scoped to the contact, so without this a
contact writing "I need the notes in 13 minutes, not 47" reached a model that had no record of the
owner's promise to them, and it searched files until the iteration cap. The owner's items named with
the contact as counterpart, obligor or recipient are listed by wording and due time; words the owner
means to send later (a notice's content, a check-in's topic), cadences and deliverables are not, and
nothing about anyone else is.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
import pytest

from protagine.api.routers import host
from protagine.commitments.store import CommitmentStore
from test_canonical_scoped_context import GuestContacts, PrivateProducer, context
from test_turn_source_evidence import source_app  # noqa: F401  (pytest fixture)

OWNER, GUEST, OTHER = "owner", "cid-guest", "cid-other"


class NamedContacts(GuestContacts):
    async def get(self, contact_id):
        self.reads.append(contact_id)
        names = {GUEST: "p-79", OTHER: "p-12"}
        return SimpleNamespace(contact_id=contact_id, display_name=names.get(contact_id), digest=None)


def _stores(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    store = CommitmentStore(tmp_path / "commitments.db")
    due = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=47)
    promise = store.create(OWNER, "Send p-79 the meeting notes", due_at=due.isoformat(),
                           metadata={"counterpart": "p-79", "obligor": "owner"})
    theirs = store.create(OWNER, "p-79 returns the signed lease", due_at=(due + timedelta(hours=2)).isoformat(),
                          metadata={"counterpart": "owner", "obligor": "p-79", "kind": "reminder"})
    store.create(OWNER, "Chase p-79 about the deposit", due_at=due.isoformat(),
                 metadata={"kind": "check_in", "recipient": "p-79", "topic": "owner-held deposit words",
                           "grant": "owner", "counterpart": "p-79", "obligor": "assistant"})
    store.create(OWNER, "Tell p-79 the private-notice-words", due_at=due.isoformat(),
                 metadata={"kind": "notice", "recipient": "p-79", "content": "private-notice-words",
                           "grant": "owner", "counterpart": "p-79", "obligor": "assistant"})
    store.create(OWNER, "Send p-12 the other-contact-item", due_at=due.isoformat(),
                 metadata={"counterpart": "p-12", "obligor": "owner"})
    store.create(OWNER, "owner-private obligation", due_at=due.isoformat())
    monkeypatch.setattr(host, "_commitment_store", store)
    private = PrivateProducer()
    for name in ("_facts_store", "_goals_store", "_initiative_store", "_briefings_engine",
                 "_affect_store", "_preference_learner", "_comms_log"):
        monkeypatch.setattr(host, name, private)
    contacts = NamedContacts()
    monkeypatch.setattr(host, "_contacts_store", contacts)
    return promise, theirs, contacts, private


@pytest.mark.asyncio
async def test_a_contacts_turn_lists_the_owners_open_items_with_them(source_app, tmp_path, monkeypatch):
    promise, theirs, contacts, private = _stores(tmp_path, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
        response = await client.post("/v1/host/context/assemble",
                                     json=context(GUEST, "I need the meeting notes inside 13 minutes rather than 47."))
    assert response.status_code == 200, response.text
    sections = {row["id"]: row["body"] for row in response.json()["sections"]}
    body = sections["protagine-commitments"]
    assert "Send p-79 the meeting notes" in body and promise["due_at"][:16] in body
    assert "p-79 returns the signed lease" in body
    for private_text in ("deposit", "private-notice-words", "other-contact-item", "owner-private", promise["id"]):
        assert private_text not in response.text
    assert contacts.reads == [GUEST] and private.calls == [] and contacts.calls == []


@pytest.mark.asyncio
async def test_another_contacts_turn_sees_none_of_them(source_app, tmp_path, monkeypatch):
    _stores(tmp_path, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
        response = await client.post("/v1/host/context/assemble", json=context(OTHER, "the notes"))
    assert response.status_code == 200
    assert "meeting notes" not in response.text and "signed lease" not in response.text
    assert "Send p-12 the other-contact-item" in response.text
