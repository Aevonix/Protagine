"""A contact's turn never sees the owner's own items, whoever they name.

``person_id`` is not an audience grant (``host._canonical_shared_commitments``): a contact sees only
descriptions already present in their own source. An item on the owner's lane that names the contact
(the counterpart of the owner's promise, the subject of a reminder the owner asked for about them, or
the plain subject of an owner item) is the owner's record in the owner's wording. Capture turns every
"tell me if X has not ..." into such a reminder, and a display name is shared by every contact who
goes by it, so listing these on the contact's turn showed the owner's private words to the contact
and to anyone with the same name. The contact's model sees its own items only, by the source-evidence
contract, with people on or off.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
import pytest

from protagine.api.routers import host
from protagine.commitments.store import CommitmentStore
from test_canonical_scoped_context import GuestContacts, PrivateProducer, context
from test_turn_source_evidence import source_app  # noqa: F401  (pytest fixture)

OWNER, GUEST, TWIN = "owner", "cid-guest", "cid-twin"


class SameName(GuestContacts):
    """Two contacts who both go by one display name."""

    async def get(self, contact_id):
        self.reads.append(contact_id)
        return SimpleNamespace(contact_id=contact_id, display_name="Dana", digest=None)


def _stores(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    store = CommitmentStore(tmp_path / "commitments.db")
    due = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(microsecond=0).isoformat()
    # "If Dana has not paid the rent by Friday, tell me": the owner's reminder, with Dana its counterpart.
    store.create(OWNER, "Remind me to call my lawyer about evicting Dana if the rent is unpaid", due_at=due,
                 metadata={"kind": "reminder", "counterpart": "Dana", "obligor": "owner"})
    store.create(OWNER, "Check whether Dana's background check came back clean", due_at=due,
                 metadata={"counterpart": "Dana", "obligor": "owner"})
    # Named by the contact's own id, and a promise to them: still the owner's wording.
    store.create(OWNER, "Send cid-guest the meeting notes before the audit", due_at=due,
                 metadata={"counterpart": GUEST, "obligor": "owner"})
    store.create(OWNER, "cid-guest returns the signed lease", due_at=due,
                 metadata={"counterpart": "owner", "obligor": GUEST})
    monkeypatch.setattr(host, "_commitment_store", store)
    private = PrivateProducer()
    for name in ("_facts_store", "_goals_store", "_initiative_store", "_briefings_engine",
                 "_affect_store", "_preference_learner", "_comms_log"):
        monkeypatch.setattr(host, name, private)
    monkeypatch.setattr(host, "_contacts_store", SameName())
    return private


@pytest.mark.asyncio
@pytest.mark.parametrize("viewer", [GUEST, TWIN])
async def test_a_contacts_turn_never_lists_the_owners_items_that_name_them(source_app, tmp_path, monkeypatch,
                                                                            viewer):
    private = _stores(tmp_path, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
        response = await client.post("/v1/host/context/assemble",
                                     json=context(viewer, "hi, any news on the meeting notes and the lease?"))
    assert response.status_code == 200, response.text
    for private_text in ("lawyer", "evicting", "background check", "meeting notes before the audit",
                         "returns the signed lease", "The owner's open items"):
        assert private_text not in response.text
    assert private.calls == []
