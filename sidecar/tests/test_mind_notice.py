"""The owner-granted message to a named third party (deferred to M5 by the initiative work).

"If X has not happened by T, tell C": capture records a ``notice`` (the owner's own words) or
a ``check_in`` (the matter, composed later) with a per-commitment owner grant; the duty drive
emits the message at T; the grant counts as ``may_contact=auto`` for that recipient only, never
over a ``never``; a sent row settles the commitment; an immediate relay is the reply's job.
The ``delegated_chase`` template of ``mind-initiative-1`` runs here end to end with a fake body.
"""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from protagine.commitments import extract
from protagine.commitments.extract import CommitmentExtractor, record_items
from protagine.commitments.store import CommitmentStore
from protagine.mind.drives import DriveInputs, commitment_candidate, duty
from test_mind_social import C, CONTACT, OTHER, OWNER, PAST, T0, contact, make  # noqa: F401  (pytest fixture)

NOTICE = {"action": "create", "target": None, "description": f"Tell {CONTACT} the parcel is late", "priority": 70,
          "due_at": (T0 + timedelta(minutes=30)).isoformat(), "source_type": "cognition", "listed_due": None,
          "metadata": {"kind": "notice", "recipient": CONTACT, "content": "The parcel is running late, sorry.",
                       "grant": "owner"}, "counterpart": CONTACT, "obligor": "assistant"}
CHECK_IN = {**NOTICE, "description": f"Ask {CONTACT} about the budget draft",
            "metadata": {"kind": "check_in", "recipient": CONTACT, "topic": "the budget draft figures and more words here",
                         "grant": "owner"}}


def _store(tmp_path):
    return CommitmentStore(tmp_path / "protagine-commitments.db")


# ---------------------------------------------------------------------------
# capture: the contract and the stored metadata
# ---------------------------------------------------------------------------

def test_the_extractor_contract_names_case_three_with_both_shapes_and_the_immediate_relay_rule():
    assert '"kind":"notice"' in extract.SYSTEM and '"kind":"check_in"' in extract.SYSTEM
    assert "DEADLINE-CONDITIONED MESSAGE TO A THIRD PARTY" in extract.SYSTEM
    assert '"grant":"owner"' in extract.SYSTEM and "p-05" in extract.SYSTEM
    assert "6 words" in extract.SYSTEM and "record nothing" in extract.SYSTEM.lower()
    assert extract.ITEM_SCHEMA["properties"]["metadata"]["type"] == ["object", "null"]


def test_record_items_stores_the_notice_and_the_check_in_metadata_for_the_owner(tmp_path):
    store = _store(tmp_path)
    result = record_items([NOTICE, CHECK_IN], person_id=OWNER, commitment_store=store, existing=[], rejections=[],
                          turn_id="turn-1", owner_id=OWNER)
    assert len(result["created"]) == 2
    notice, check_in = (store.get(ident) for ident in result["created"])
    assert notice["metadata"]["kind"] == "notice" and notice["metadata"]["content"] == "The parcel is running late, sorry."
    assert notice["metadata"]["recipient"] == CONTACT and notice["metadata"]["grant"] == "owner"
    assert notice["metadata"]["obligor"] == "assistant" and notice["metadata"]["counterpart"] == CONTACT
    assert check_in["metadata"]["kind"] == "check_in" and check_in["metadata"]["grant"] == "owner"
    assert check_in["metadata"]["topic"] == "the budget draft figures and more"     # clamped to six words


def test_a_contacts_own_request_to_message_a_third_party_carries_no_grant(tmp_path):
    store = _store(tmp_path)
    result = record_items([NOTICE], person_id=OTHER, commitment_store=store, existing=[], rejections=[],
                          turn_id="turn-2", owner_id=OWNER)
    row = store.get(result["created"][0])
    assert row["metadata"]["kind"] == "notice" and "grant" not in row["metadata"]


def test_a_notice_without_words_is_a_plain_commitment(tmp_path):
    store = _store(tmp_path)
    empty = {**NOTICE, "metadata": {"kind": "notice", "recipient": CONTACT, "content": "  ", "grant": "owner"}}
    result = record_items([empty], person_id=OWNER, commitment_store=store, existing=[], rejections=[], owner_id=OWNER)
    row = store.get(result["created"][0])
    assert "kind" not in row["metadata"] and row["metadata"]["counterpart"] == CONTACT


# ---------------------------------------------------------------------------
# the drive: candidates from the rows
# ---------------------------------------------------------------------------

def _row(ident, item, *, recipient_id=CONTACT, due=T0 - timedelta(minutes=5)):
    metadata = dict(item["metadata"], counterpart=item["counterpart"], obligor=item["obligor"])
    if recipient_id:
        metadata["recipient_id"] = recipient_id
    return {"id": ident, "person_id": OWNER, "description": item["description"], "priority": 70, "status": "overdue",
            "due_at": due.isoformat(), "source_type": "cognition", "metadata": metadata}


def test_commitment_candidate_emits_a_verbatim_notice_and_a_composed_check_in_with_the_grant():
    due = T0 - timedelta(minutes=5)
    notice = commitment_candidate(_row("c-1", NOTICE), due, T0, owner_id=OWNER)
    assert notice.type == "commitment_notice" and notice.kind == "message" and notice.recipient == CONTACT
    assert notice.text == "The parcel is running late, sorry." and notice.grant == "owner"
    assert notice.dedup_key == "commitment:c-1:notice" and notice.drive == "duty"
    assert notice.success_check == {"kind": "commitment_resolved", "commitment_id": "c-1"}
    assert notice.invalidates_if == "commitment:c-1:resolved" and notice.purpose is None
    check_in = commitment_candidate(_row("c-2", CHECK_IN), due, T0, owner_id=OWNER)
    assert check_in.type == "commitment_check_in" and check_in.kind == "message" and check_in.recipient == CONTACT
    assert check_in.text == "" and check_in.purpose == "follow_up:c-2" and check_in.grant == "owner"
    assert check_in.topic == CHECK_IN["metadata"]["topic"] and check_in.dedup_key == "commitment:c-2:check_in"
    assert check_in.success_check == {"kind": "commitment_resolved", "commitment_id": "c-2"}


def test_an_unresolved_recipient_becomes_an_owner_ask_instead():
    inputs = DriveInputs(now=T0, owner_id=OWNER, commitments=[_row("c-3", NOTICE, recipient_id=None)])
    _, candidates = duty(inputs)
    candidate, = candidates
    assert candidate.type == "recipient_unknown" and candidate.kind == "message" and candidate.recipient == OWNER
    assert candidate.dedup_key == "commitment:c-3:recipient" and CONTACT in candidate.text
    assert "I do not know who that is" in candidate.text and NOTICE["description"] in candidate.text
    assert candidate.grant is None
    # Only the owner grants: without one, or on a contact's own row, it is an ordinary commitment.
    without_owner = DriveInputs(now=T0, owner_id=None, commitments=[_row("c-3", NOTICE, recipient_id=None)])
    assert [c.type for c in duty(without_owner)[1]] == ["commitment_overdue"]
    contacts_row = {**_row("c-4", NOTICE), "person_id": OTHER}
    assert [c.type for c in duty(DriveInputs(now=T0, owner_id=OWNER, commitments=[contacts_row]))[1]] == \
        ["commitment_overdue"]


# ---------------------------------------------------------------------------
# the tick: the grant rule, settlement, the unknown recipient
# ---------------------------------------------------------------------------

def _seed(fx, item, **overrides):
    metadata = {**item["metadata"], "counterpart": item["counterpart"], "obligor": item["obligor"], **overrides}
    return fx.commitments.create(person_id=OWNER, description=item["description"], due_at=(fx.now + C).isoformat(),
                                 source_type="cognition", metadata=metadata)


async def test_the_grant_acts_under_standard_for_an_ask_contact_and_the_sent_row_settles_the_commitment(make):
    fx = make([contact(CONTACT, may_contact="ask"), contact(OTHER, may_contact="auto")])
    row = _seed(fx, NOTICE)
    fx.shift(C + PAST)
    summary = await fx.tick()
    formed, = summary["formed"]
    assert formed["type"] == "commitment_notice" and formed["decision"] == "act" and formed["status"] == "approved"
    stored = fx.store.get(formed["id"])
    assert stored.context["may_contact"] == "auto" and stored.context["grant"] == "owner"
    assert stored.context["text"] == "The parcel is running late, sorry."
    assert fx.commitments.get(row["id"])["metadata"]["recipient_id"] == CONTACT
    payload, = await fx.mind.outbox_ready()
    assert payload["recipient"] == CONTACT and payload["kind"] == "message" and payload["text"] == stored.context["text"]
    fx.mind.outbox.sending(payload["id"], target=f"capture:{CONTACT}")
    fx.mind.outbox.sent(payload["id"])
    resolved = fx.commitments.get(row["id"])
    assert resolved["status"] == "fulfilled" and resolved["metadata"]["resolution"]["by"] == "body"
    assert fx.messages_to(OTHER) == [] and (await fx.tick())["formed"] == []


async def test_a_never_contact_is_never_overridden_and_the_owner_hears_why(make):
    fx = make([contact(CONTACT, may_contact="never")])
    _seed(fx, NOTICE)
    fx.shift(C + PAST)
    formed, = (await fx.tick())["formed"]
    assert formed["type"] == "commitment_notice" and formed["decision"] == "drop"
    dropped = fx.store.get(formed["id"])
    assert dropped.status == "dropped" and dropped.context["may_contact"] == "never"
    refused, = [row for row in fx.messages_to(OWNER)]
    assert refused.type == "grant_refused" and refused.status == "approved" and CONTACT in refused.context["text"]
    assert "never" in refused.context["text"]
    assert [p["recipient"] for p in await fx.mind.outbox_ready()] == [OWNER]
    assert (await fx.tick())["formed"] == [] and len(fx.messages_to(OWNER)) == 1


async def test_an_unknown_recipient_asks_the_owner_who_they_are(make):
    fx = make([contact(OTHER, may_contact="auto")])
    _seed(fx, {**NOTICE, "metadata": {**NOTICE["metadata"], "recipient": "Kim"}, "counterpart": "Kim"})
    fx.shift(C + PAST)
    formed, = (await fx.tick())["formed"]
    assert formed["type"] == "recipient_unknown" and formed["decision"] == "act"
    row = fx.store.get(formed["id"])
    assert row.entity_id == OWNER and row.dedup_key.endswith(":recipient") and "Kim" in row.context["text"]
    payload, = await fx.mind.outbox_ready()
    assert payload["recipient"] == OWNER and payload["kind"] == "notice"
    assert (await fx.tick())["formed"] == []


async def test_people_off_leaves_the_granted_message_in_its_m4_form(make):
    """Audit M9: with the faculty off the owner's message to a third party is what it was before
    M5, the assistant's overdue work for the owner: no notice, no composed check-in, no
    recipient question, and no rewrite of anything into an owner notice."""
    fx = make([contact(CONTACT, may_contact="auto", name="Sam")], config={"faculties": {"people": False}})
    notice = _seed(fx, NOTICE)
    _seed(fx, {**CHECK_IN, "metadata": {**CHECK_IN["metadata"], "recipient": "Kim"}, "counterpart": "Kim"})
    fx.shift(C + PAST)
    formed = (await fx.tick())["formed"]
    assert sorted(item["type"] for item in formed) == ["commitment_overdue", "commitment_overdue"]
    assert all(fx.store.get(item["id"]).entity_id == OWNER and item["kind"] == "task" for item in formed)
    assert fx.messages_to(CONTACT) == [] and fx.messages_to(OWNER) == []
    assert "recipient_id" not in fx.commitments.get(notice["id"])["metadata"]


async def test_people_off_keeps_the_m2_deliverable_to_the_contact_who_asked(make):
    """A deliverable a contact asked for predates M5: the ablation leaves it going to that contact."""
    fx = make([contact(CONTACT, may_contact="auto", name="Sam")], config={"faculties": {"people": False}})
    fx.commitments.create(person_id=CONTACT, description="Email Sam the venue address",
                          due_at=(fx.now + C).isoformat(), source_type="introspection",
                          metadata={"kind": "deliverable", "content": "The venue is at 5 Main St."})
    fx.shift(C + PAST)
    formed, = (await fx.tick())["formed"]
    assert formed["type"] == "commitment_deliverable" and formed["decision"] == "act"
    row = fx.store.get(formed["id"])
    assert row.entity_id == CONTACT and row.context["text"] == "The venue is at 5 Main St."


# ---------------------------------------------------------------------------
# delegated_chase end to end: owner turn -> capture -> tick past the horizon -> outbox
# ---------------------------------------------------------------------------

class ScriptedRouter:
    """Plays the extraction model for the owner's two turns and the composer for the check-in."""

    supports_function_routing = True

    def __init__(self, item, reply):
        self.item, self.reply, self.calls = item, reply, []

    def function_deadline_seconds(self, *, context=None):
        return 20

    async def complete(self, messages, *, context=None, **_):
        self.calls.append((context, messages))
        task = (context or {}).get("task")
        if task == "commitment_extract":
            prompt = messages[1]["content"].split("This turn, verbatim:", 1)[-1]
            return SimpleNamespace(content=json.dumps([self.item] if "within 12 minutes" in prompt else []))
        if task == "mind_compose":
            return SimpleNamespace(content=self.reply, usage={"total_tokens": 20})
        raise AssertionError(f"unexpected task {task}")


async def test_delegated_chase_reaches_the_contact_not_the_owner_and_never_the_other_contact(make):
    item = {"action": "create", "target": None, "description": f"Ask {CONTACT} for the budget draft", "priority": 70,
            "due_at": (T0 + timedelta(minutes=12)).isoformat(), "source_type": "cognition", "listed_due": None,
            "metadata": {"kind": "check_in", "recipient": CONTACT, "topic": "the budget draft", "grant": "owner"},
            "counterpart": CONTACT, "obligor": "assistant"}
    router = ScriptedRouter(item, f"Hi {CONTACT}, could you send over the budget draft when you have a moment?")
    fx = make([contact(CONTACT), contact(OTHER)], router=router)
    fx.mind.capture = CommitmentExtractor(fx.ledger, lambda: fx.commitments)
    fx.ledger.record_source("turn-1", contact_id=OWNER, session_id="owner-1", messages=[
        {"role": "user", "content": f"If {CONTACT} has not sent me the budget draft within 12 minutes, ask them for "
                                    "it yourself and leave me out of it. Nothing to do right now."},
        {"role": "assistant", "content": "Understood."}], occurred_at=fx.now.isoformat())
    fx.ledger.record_source("turn-2", contact_id=OWNER, session_id="owner-1", messages=[
        {"role": "user", "content": f"{OTHER} has nothing to do with the budget draft; do not bring them into it."},
        {"role": "assistant", "content": "Noted."}], occurred_at=fx.now.isoformat())
    early = await fx.tick()
    assert early["capture_drained"]["recorded"] == 1 and early["formed"] == []
    row, = fx.commitments.get_pending_for_person(OWNER)
    assert row["metadata"]["kind"] == "check_in" and row["metadata"]["recipient"] == CONTACT

    fx.shift(timedelta(minutes=12) + PAST)
    formed = [item for tick in [await fx.tick(), await fx.tick(), await fx.tick()] for item in tick["formed"]]
    assert [item["type"] for item in formed] == ["commitment_check_in"] and formed[0]["decision"] == "act"
    payload, = await fx.mind.outbox_ready()
    assert payload["recipient"] == CONTACT and payload["recipient_is_owner"] is False
    assert payload["recipient_handles"] == [{"gateway": "capture", "address": CONTACT, "is_primary": True, "verified": True}]
    assert "budget draft" in payload["text"] and OTHER not in payload["text"]
    assert fx.messages_to(OTHER) == [] and fx.messages_to(OWNER) == []
    compose_calls = [c for c, _ in router.calls if c.get("task") == "mind_compose"]
    assert len(compose_calls) == 1
    fx.mind.outbox.sending(payload["id"], target=f"capture:{CONTACT}")
    fx.mind.outbox.sent(payload["id"])
    assert fx.commitments.get(row["id"])["status"] == "fulfilled"
    assert fx.mind.dispatch() == [] and (await fx.tick())["formed"] == []
