"""``/v1/mind/people``: reads for everyone, mutations for the owner (architecture 4.7, 7.4, 7.10; evals 7.2 test 4)."""

from __future__ import annotations

from contextlib import closing
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from protagine.api.routers import host as host_mod
from protagine.api.routers import people as people_mod
from protagine.contacts.config import ContactsConfig
from protagine.contacts.optout import apply_opt_out
from protagine.contacts.store import SQLiteContactStore
from protagine.turns import get_turn_idempotency_ledger


@pytest.fixture
async def world(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path / "state"))
    store = SQLiteContactStore(ContactsConfig(sqlite_path=str(tmp_path / "contacts.db")))
    await store.connect()
    owner = await store.create(display_name="Owner", trust_tier="inner_circle", may_contact="auto")
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", owner.contact_id)
    guest = await store.create(display_name="Casey Lee", trust_tier="regular")
    await store.add_handle(guest.contact_id, "sms", "+15550000005")
    monkeypatch.setattr(host_mod, "_contacts_store", store)
    for name in ("_comms_log", "_affect_store", "_facts_store", "_graph", "_world_store"):
        monkeypatch.setattr(host_mod, name, None, raising=False)
    app = FastAPI()
    app.include_router(people_mod.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, store, owner, guest
    await store.close()


# -- reads --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_who_finds_by_name_handle_and_id_and_a_guest_sees_only_who(world):
    client, store, owner, guest = world
    for query in ("casey", "Casey Lee", "+1 555 000 0005", "sms:+15550000005", guest.contact_id, "0000005"):
        response = await client.get("/v1/mind/people", params={"q": query})
        assert response.status_code == 200, response.text
        rows = response.json()["contacts"]
        assert rows and rows[0]["contact_id"] == guest.contact_id, query
    full = (await client.get("/v1/mind/people", params={"q": "casey"})).json()["contacts"][0]
    assert full["may_contact"] == "ask" and full["handles"] == [
        {"gateway": "sms", "address": "+15550000005", "verified": False}]
    assert "interaction_allowed" not in full
    as_guest = (await client.get("/v1/mind/people", params={"q": "owner", "contact_id": guest.contact_id})).json()
    assert as_guest["contacts"] == [{"contact_id": owner.contact_id, "display_name": "Owner",
                                     "trust_tier": "inner_circle"}]
    as_owner = (await client.get("/v1/mind/people", params={"q": "casey", "contact_id": owner.contact_id})).json()
    assert "handles" in as_owner["contacts"][0]
    everyone = (await client.get("/v1/mind/people")).json()
    assert {row["contact_id"] for row in everyone["contacts"]} == {owner.contact_id, guest.contact_id}
    assert "Casey Lee" in everyone["text"]
    assert (await client.get("/v1/mind/people", params={"q": "nobody"})).json()["contacts"] == []


@pytest.mark.asyncio
async def test_a_guest_learns_who_one_named_person_is_but_cannot_browse_the_contact_list(world):
    """A guest viewer gets the one person a reference names exactly; no listing, no partial matches and
    no 404 candidates, so the contact list is the owner's (review security-3 on the M5 surface)."""
    client, store, owner, guest = world
    other = await store.create(display_name="Casey Park", trust_tier="regular")
    seen = {"contact_id": guest.contact_id}
    named = (await client.get("/v1/mind/people", params={"q": "Casey Park", **seen})).json()["contacts"]
    assert named == [{"contact_id": other.contact_id, "display_name": "Casey Park", "trust_tier": "regular"}]
    for query in ("", "casey", "0000005"):
        assert (await client.get("/v1/mind/people", params={"q": query, **seen})).json()["contacts"] == [], query
    missing = await client.get("/v1/mind/people/Casey", params=seen)
    assert missing.status_code == 404 and missing.json()["detail"]["candidates"] == []
    assert "Casey Lee" not in missing.text and "Casey Park" not in missing.text
    owners = await client.get("/v1/mind/people/Casey")
    assert owners.status_code == 404 and len(owners.json()["detail"]["candidates"]) == 2


@pytest.mark.asyncio
async def test_inspect_shows_the_record_digest_handles_proposals_and_permission_history(world):
    client, store, owner, guest = world
    await store.set_digest(guest.contact_id, "Casey: a supplier, known since August.", ["turn:1"])
    await store.set_may_contact(guest.contact_id, "auto", by=owner.contact_id, reason="owner said so")
    await store.propose_handle_link(guest.contact_id, "email", "casey@example.test")
    response = await client.get(f"/v1/mind/people/{guest.contact_id}")
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["contact"]["digest"] == "Casey: a supplier, known since August."
    assert value["contact"]["may_contact"] == "auto" and value["contact"]["handles"][0]["address"] == "+15550000005"
    assert [p["address"] for p in value["proposals"]] == ["casey@example.test"]
    assert value["permission_history"][0]["action"] == "may_contact_set"
    assert value["permission_history"][0]["detail"]["to"] == "auto"
    assert "supplier" in value["text"]
    by_name = (await client.get("/v1/mind/people/Casey")).json()
    assert by_name["contact"]["contact_id"] == guest.contact_id
    as_guest = (await client.get(f"/v1/mind/people/{owner.contact_id}", params={"contact_id": guest.contact_id})).json()
    assert as_guest["contact"] == {"contact_id": owner.contact_id, "display_name": "Owner", "trust_tier": "inner_circle"}
    missing = await client.get("/v1/mind/people/Nobody")
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "unknown_contact"


# -- owner mutations ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_permission_is_the_owners_alone_and_moves_any_direction(world):
    client, store, owner, guest = world
    path = f"/v1/mind/people/{guest.contact_id}/permission"
    for refused in ({"may_contact": "auto", "contact_id": guest.contact_id},
                    {"may_contact": "auto"},
                    {"may_contact": "auto", "contact_id": "cid-someone-else"}):
        response = await client.post(path, json=refused)
        assert response.status_code == 403 and response.json()["detail"]["code"] == "not_owner", refused
    assert (await store.get(guest.contact_id)).may_contact == "ask"
    raised = await client.post(path, json={"may_contact": "auto", "contact_id": owner.contact_id})
    assert raised.status_code == 200 and raised.json()["may_contact"] == "auto"
    lowered = await client.post("/v1/mind/people/Casey/permission", json={"may_contact": "never", "by": "cli"})
    assert lowered.status_code == 200 and lowered.json()["may_contact"] == "never"
    audit = [row for row in await store.get_audit_log(guest.contact_id) if row["action"] == "may_contact_set"]
    assert sorted(row["performed_by"] for row in audit) == sorted([owner.contact_id, "cli"])
    assert (await client.post(path, json={"may_contact": "sometimes", "by": "cli"})).status_code == 422
    owner_path = f"/v1/mind/people/{owner.contact_id}/permission"
    assert (await client.post(owner_path, json={"may_contact": "never", "by": "cli"})).status_code == 422


@pytest.mark.asyncio
async def test_cadence_is_the_owners_and_null_clears_it(world):
    client, store, owner, guest = world
    path = f"/v1/mind/people/{guest.contact_id}/cadence"
    refused = await client.post(path, json={"minutes": 60, "contact_id": guest.contact_id})
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "not_owner"
    assert (await client.post(path, json={"minutes": 60, "contact_id": owner.contact_id})).json()["cadence_minutes"] == 60
    assert (await store.get(guest.contact_id)).cadence_minutes == 60
    assert (await client.post(path, json={"minutes": None, "by": "cli"})).json()["cadence_minutes"] is None
    assert (await client.post(path, json={"minutes": -3, "by": "cli"})).status_code == 422


@pytest.mark.asyncio
async def test_merge_is_owner_only_and_moves_handles_sources_and_the_other_stores(world, monkeypatch):
    client, store, owner, guest = world
    shadow = await store.create(display_name="+15550000077", import_source="auto:sender")
    await store.add_handle(shadow.contact_id, "custom-phone-app", "+15550000077", source="auto:sender")
    await store.record_interaction(shadow.contact_id, "2026-09-20T10:00:00Z")
    ledger = get_turn_idempotency_ledger(people_mod._ledger().db_path.parent)
    for name in ("t-shadow-1", "t-shadow-2"):
        ledger.record_source(name, contact_id=shadow.contact_id, session_id="s-" + name,
                             messages=[{"role": "user", "content": "hello from the other phone"}], derive_claims=False)
    ledger.record_source("t-guest-1", contact_id=guest.contact_id, session_id="s-g",
                         messages=[{"role": "user", "content": "hi"}], derive_claims=False)
    moved = []

    class Comms:
        def reattribute(self, old_id, new_id):
            moved.append(("comms", old_id, new_id))
            return 3

    monkeypatch.setattr(host_mod, "_comms_log", Comms())
    body = {"keep": "Casey Lee", "drop": shadow.contact_id}
    refused = await client.post("/v1/mind/people/merge", json={**body, "contact_id": guest.contact_id})
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "not_owner"
    assert await store.get(shadow.contact_id) is not None
    response = await client.post("/v1/mind/people/merge", json={**body, "contact_id": owner.contact_id})
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["dropped"] == shadow.contact_id and value["contact"]["contact_id"] == guest.contact_id
    assert value["sources_moved"] == 2 and value["sources_pending"] == 0
    assert value["contact"]["last_interaction_at"] == "2026-09-20T10:00:00Z"
    assert {h["address"] for h in value["contact"]["handles"]} == {"+15550000005", "+15550000077"}
    # At the merge, and again once the ledger moved the sources: a sourced row moves only after its source.
    assert moved == [("comms", shadow.contact_id, guest.contact_id)] * 2
    with closing(ledger._connect()) as conn:
        owners = dict(conn.execute("SELECT turn_id, contact_id FROM turn_sources").fetchall())
    assert owners == {"t-shadow-1": guest.contact_id, "t-shadow-2": guest.contact_id, "t-guest-1": guest.contact_id}
    assert await store.get(shadow.contact_id) is None
    assert await store.pending_identity_reconciliations() == []
    again = await client.post("/v1/mind/people/merge", json={"keep": guest.contact_id, "drop": owner.contact_id,
                                                               "by": "cli"})
    assert again.status_code == 422 and again.json()["detail"]["code"] == "owner_cannot_be_dropped"
    same = await client.post("/v1/mind/people/merge", json={"keep": "Casey", "drop": guest.contact_id, "by": "cli"})
    assert same.status_code == 422


@pytest.mark.asyncio
async def test_a_merge_moves_the_dropped_records_commitments_and_messages_owed_to_it(world, monkeypatch, tmp_path):
    """Audit m6: the dropped record's open items are the person's; an owner's message granted to
    the dropped record now goes to the person, never to a soft-deleted contact."""
    from protagine.commitments.store import CommitmentStore
    client, store, owner, guest = world
    shadow = await store.create(display_name="+15550000078", import_source="auto:sender")
    commitments = CommitmentStore(tmp_path / "protagine-commitments.db")
    monkeypatch.setattr(host_mod, "_commitment_store", commitments)
    theirs = commitments.create(person_id=shadow.contact_id, description="Send the signed form")
    notice = commitments.create(person_id=owner.contact_id, description="Tell them the venue moved",
                                metadata={"kind": "notice", "recipient": "+15550000078", "content": "It moved.",
                                          "grant": "owner", "recipient_id": shadow.contact_id,
                                          "recipient_exact": True})
    other = commitments.create(person_id=owner.contact_id, description="Unrelated",
                               metadata={"recipient_id": guest.contact_id})
    response = await client.post("/v1/mind/people/merge", json={"keep": guest.contact_id, "drop": shadow.contact_id,
                                                                "by": "cli"})
    assert response.status_code == 200, response.text
    assert commitments.get(theirs["id"])["person_id"] == guest.contact_id
    moved = commitments.get(notice["id"])
    assert moved["person_id"] == owner.contact_id and moved["metadata"]["recipient_id"] == guest.contact_id
    assert moved["metadata"]["content"] == "It moved." and moved["metadata"]["recipient_exact"] is True
    assert commitments.get(other["id"])["metadata"] == {"recipient_id": guest.contact_id}


@pytest.mark.asyncio
async def test_a_merge_keeps_the_dropped_records_sourced_affect_through_the_reconciliation(world, monkeypatch, tmp_path):
    """C2 on the real stores (audit m6): the affect row moves before its source does, so the
    reconciliation's purge of rows whose source no longer agrees finds nothing to delete."""
    from protagine.tom.affect import AffectStore
    client, store, owner, guest = world
    shadow = await store.create(display_name="+15550000079", import_source="auto:sender")
    ledger = get_turn_idempotency_ledger(people_mod._ledger().db_path.parent)
    ledger.record_source("t-affect", contact_id=shadow.contact_id, session_id="s-affect",
                         messages=[{"role": "user", "content": "This is taking far too long."}], derive_claims=False)
    affect = AffectStore(str(tmp_path / "affect.db"), source_ledger=ledger)
    monkeypatch.setattr(host_mod, "_affect_store", affect)
    lineage, _ = affect.source_input("t-affect", shadow.contact_id)
    affect.create_event(contact_id=shadow.contact_id, valence=-0.6, source="appraisal", source_lineage=lineage)
    response = await client.post("/v1/mind/people/merge", json={"keep": guest.contact_id, "drop": shadow.contact_id,
                                                                "by": "cli"})
    assert response.status_code == 200 and response.json()["sources_moved"] == 1, response.text
    rows = affect._conn.execute("SELECT contact_id FROM affect_events").fetchall()
    assert [row["contact_id"] for row in rows] == [guest.contact_id]


@pytest.mark.asyncio
async def test_a_merge_whose_source_move_is_deferred_loses_no_sourced_affect_or_comms(world, monkeypatch, tmp_path):
    """Review F11: the merge moved the sourced rows before the ledger moved their sources; when the
    reconciliation was deferred (the ledger busy), the next read of the kept contact purged the
    moved rows as erased and the retry found nothing to move. A sourced row moves only once its
    source has: it waits, valid, under the dropped record, and the reconciliation moves it."""
    from protagine.api.routers import social_state
    from protagine.contacts.comms import CommsLog
    from protagine.tom.affect import AffectStore
    client, store, owner, guest = world
    shadow = await store.create(display_name="+15550000079", import_source="auto:sender")
    ledger = get_turn_idempotency_ledger(people_mod._ledger().db_path.parent)
    ledger.record_source("t-affect", contact_id=shadow.contact_id, session_id="s-affect",
                         messages=[{"role": "user", "content": "This is taking far too long."}], derive_claims=False)
    affect = AffectStore(str(tmp_path / "affect.db"), source_ledger=ledger)
    comms = CommsLog(str(tmp_path / "comms.db"), source_ledger=ledger)
    monkeypatch.setattr(host_mod, "_affect_store", affect)
    monkeypatch.setattr(host_mod, "_comms_log", comms)
    lineage, _ = affect.source_input("t-affect", shadow.contact_id)
    affect.create_event(contact_id=shadow.contact_id, valence=-0.6, source="appraisal", source_lineage=lineage)
    comms.log(shadow.contact_id, channel="sms", direction="in", summary="too long", source_lineage=lineage)
    comms.log(shadow.contact_id, channel="sms", direction="out", summary="a check-in", external_ref="mind:check_in:i-1")
    real = social_state.reconcile_identity_sources

    async def busy(*args, **kwargs):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(social_state, "reconcile_identity_sources", busy)
    response = await client.post("/v1/mind/people/merge", json={"keep": guest.contact_id, "drop": shadow.contact_id,
                                                                "by": "cli"})
    assert response.status_code == 200 and response.json()["sources_pending"] == 1, response.text
    affect.trend(guest.contact_id)                   # the next tick reads the kept contact first
    comms.history(guest.contact_id)
    assert comms.mind_sends(guest.contact_id)[0]["intention_id"] == "i-1"   # an unsourced row moved at once
    monkeypatch.setattr(social_state, "reconcile_identity_sources", real)
    for operation in await store.pending_identity_reconciliations(limit=10):
        await real(store, ledger, operation)
    assert [row["contact_id"] for row in affect._conn.execute("SELECT contact_id FROM affect_events")] == [
        guest.contact_id]
    assert {row["contact_id"] for row in comms._conn.execute("SELECT contact_id FROM communications")} == {
        guest.contact_id}
    assert comms.counts(guest.contact_id) == {"inbound": 1, "outbound": 1, "channels": 1}
    comms._conn.close()


@pytest.mark.asyncio
async def test_the_owner_reads_as_auto_and_an_unresolved_reference_lists_the_candidates(world):
    """Audit m5, m7: the owner's permission is ``auto`` by identity whatever the column says (a
    pre-M5 row may say never); a reference that names no single contact is answered with the
    people it could mean, and nothing is changed."""
    client, store, owner, guest = world
    db = store._require_db()
    await db.execute("UPDATE contacts SET may_contact = 'never' WHERE contact_id = ?", (owner.contact_id,))
    await db.commit()
    inspected = (await client.get(f"/v1/mind/people/{owner.contact_id}")).json()
    assert inspected["contact"]["may_contact"] == "auto" and "may_contact=auto" in inspected["text"]
    listed = (await client.get("/v1/mind/people", params={"q": "Owner"})).json()
    assert [row["may_contact"] for row in listed["contacts"]] == ["auto"]
    await store.create(display_name="Casey Park", trust_tier="regular")
    ambiguous = await client.post("/v1/mind/people/merge", json={"keep": "Casey", "drop": guest.contact_id, "by": "cli"})
    assert ambiguous.status_code == 404
    detail = ambiguous.json()["detail"]
    assert detail["code"] == "unknown_contact"
    assert {row["display_name"] for row in detail["candidates"]} == {"Casey Lee", "Casey Park"}
    assert all(set(row) == {"contact_id", "display_name", "trust_tier"} for row in detail["candidates"])
    assert await store.get(guest.contact_id) is not None


@pytest.mark.asyncio
async def test_anyone_may_propose_a_link_and_the_proposal_waits_for_the_owner(world):
    client, store, owner, guest = world
    response = await client.post("/v1/mind/people/link", json={
        "contact_id": "Casey", "gateway": "email", "address": "Casey@Example.test", "by": guest.contact_id})
    assert response.status_code == 200, response.text
    candidate = response.json()
    assert candidate["status"] == "pending" and candidate["address"] == "casey@example.test"
    assert candidate["authority_granted"] is False
    assert await store.resolve_messaging_handle("email", "casey@example.test") is None  # not attributed
    listed = (await client.get("/v1/mind/people/proposals")).json()
    assert [row["candidate_id"] for row in listed["proposals"]] == [candidate["candidate_id"]]
    assert "casey@example.test" in listed["text"]
    audit = [row for row in await store.get_audit_log(guest.contact_id) if row["action"] == "link_proposed"]
    assert len(audit) == 1 and audit[0]["performed_by"] == guest.contact_id
    already = await client.post("/v1/mind/people/link", json={
        "contact_id": guest.contact_id, "gateway": "whatsapp", "address": "15550000005@s.whatsapp.net"})
    assert already.json()["status"] == "already_linked"
    await store.confirm_link(candidate["candidate_id"], performed_by=owner.contact_id)
    assert (await store.resolve_messaging_handle("email", "casey@example.test")).contact_id == guest.contact_id
    assert (await client.get("/v1/mind/people/proposals")).json()["proposals"] == []


@pytest.mark.asyncio
async def test_may_contact_is_raised_only_by_the_owner_and_an_opt_out_only_lowers_it(world):
    """Evals 7.2 test 4: the owner route raises; STOP lowers; nobody else raises it back."""
    client, store, owner, guest = world
    path = f"/v1/mind/people/{guest.contact_id}/permission"
    assert (await client.post(path, json={"may_contact": "auto", "contact_id": owner.contact_id})).status_code == 200
    lowered = await apply_opt_out(store, guest.contact_id, "STOP", source_ref="turn:t-9", owner_id=owner.contact_id)
    assert lowered.may_contact == "never"
    assert (await client.post(path, json={"may_contact": "auto", "contact_id": guest.contact_id})).status_code == 403
    assert await apply_opt_out(store, guest.contact_id, "ok you may message me again", source_ref="turn:t-10",
                               owner_id=owner.contact_id) is None
    assert await store.lower_may_contact(guest.contact_id, reason="appraisal opt_out", source_ref="turn:t-11") is None
    assert (await store.get(guest.contact_id)).may_contact == "never"
    assert (await client.post(path, json={"may_contact": "ask", "by": "cli"})).json()["may_contact"] == "ask"


@pytest.mark.asyncio
async def test_a_merge_outside_the_router_moves_comms_affect_and_sources_through_the_server_defaults(
        tmp_path, monkeypatch):
    """The owner confirming a link folds the shadow that held the handle (``confirm_link`` ->
    ``merge``) with no hooks passed: the store's defaults, set by the server, move the comms and
    affect rows, the ledger sources, and then the sourced rows those sources carry."""
    from protagine import server
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PROTAGINE_CONTACTS_DB", str(tmp_path / "contacts.db"))
    calls = []

    class Rows:
        def __init__(self, name):
            self.name = name

        def reattribute(self, old_id, new_id):
            calls.append((self.name, old_id, new_id))
            return 1

        def purge_erased_sources(self, source_ids):
            return 0

    monkeypatch.setattr(host_mod, "_comms_log", Rows("comms"))
    monkeypatch.setattr(host_mod, "_affect_store", Rows("affect"))
    monkeypatch.setattr(host_mod, "_contacts_store", None)
    store = await server._initialize_contacts_store()
    try:
        person = await store.create(display_name="Casey Lee", trust_tier="regular")
        shadow = await store.create(display_name="+15550000088", import_source="auto:sender")
        await store.add_handle(shadow.contact_id, "sms", "+15550000088", source="auto:sender")
        ledger = get_turn_idempotency_ledger(people_mod._ledger().db_path.parent)
        ledger.record_source("t-shadow", contact_id=shadow.contact_id, session_id="s-shadow",
                             messages=[{"role": "user", "content": "hello"}], derive_claims=False)
        proposal = await store.propose_handle_link(person.contact_id, "sms", "+15550000088")
        confirmed = await store.confirm_link(proposal["candidate_id"], performed_by="owner")
        assert confirmed["merged"] is True
        # Before the source move (unsourced rows) and after it (the sourced rows, now valid there).
        assert calls == [("comms", shadow.contact_id, person.contact_id),
                         ("affect", shadow.contact_id, person.contact_id)] * 2
        with closing(ledger._connect()) as conn:
            owners = dict(conn.execute("SELECT turn_id, contact_id FROM turn_sources").fetchall())
        assert owners == {"t-shadow": person.contact_id}
        assert await store.pending_identity_reconciliations() == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_with_the_people_faculty_off_merge_link_and_cadence_are_refused_and_the_rest_stays(world, monkeypatch):
    """Audit M9: ``full-people`` must lack what M5 adds (merge, link proposals, cadences) and keep
    what came before it (who, inspect, the owner's permission)."""
    from protagine.api.routers import mind as mind_router
    client, store, owner, guest = world
    other = await store.create(display_name="Other Person", trust_tier="regular")
    monkeypatch.setattr(mind_router, "_mind", SimpleNamespace(faculties={"people": False}))
    by_owner = {"contact_id": owner.contact_id}
    refused = [
        await client.post(f"/v1/mind/people/{guest.contact_id}/cadence", json={"minutes": 30, **by_owner}),
        await client.post("/v1/mind/people/merge", json={"keep": guest.contact_id, "drop": other.contact_id, **by_owner}),
        await client.post("/v1/mind/people/link", json={"contact_id": guest.contact_id, "gateway": "email",
                                                        "address": "casey@example.test"})]
    for response in refused:
        assert response.status_code == 409 and response.json()["detail"]["code"] == "people_off", response.text
    assert (await store.get(guest.contact_id)).cadence_minutes is None and await store.get(other.contact_id)
    assert await store.list_handle_proposals() == []
    permitted = await client.post(f"/v1/mind/people/{guest.contact_id}/permission",
                                  json={"may_contact": "auto", **by_owner})
    assert permitted.status_code == 200 and (await store.get(guest.contact_id)).may_contact == "auto"
    assert (await client.get("/v1/mind/people", params={"q": "casey"})).status_code == 200
    assert (await client.get(f"/v1/mind/people/{guest.contact_id}")).status_code == 200
    monkeypatch.setattr(mind_router, "_mind", SimpleNamespace(faculties={"people": True}))
    assert (await client.post(f"/v1/mind/people/{guest.contact_id}/cadence",
                              json={"minutes": 30, **by_owner})).status_code == 200
