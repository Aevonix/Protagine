"""The owner-granted message to a named third party (deferred to M5 by the initiative work).

"If X has not happened by T, tell C": capture records a ``notice`` (the owner's own words) or
a ``check_in`` (the matter, composed later) with a per-commitment owner grant, once the owner's own words
asking for it (``asked``) are confirmed by the claim-review pass; the duty drive
emits the message at T; the grant counts as ``may_contact=auto`` for that recipient only, never
over a ``never``; a sent row settles the commitment; a message to pass on now is the reply's own job,
never a notice. Permission is read again when a message may leave.
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
from test_mind_social import C, CONTACT, OTHER, OWNER, PAST, T0, confirmed, contact, make  # noqa: F401  (pytest fixture)

NOTICE = {"action": "create", "target": None, "description": f"Tell {CONTACT} the parcel is late", "priority": 70,
          "due_at": (T0 + timedelta(minutes=30)).isoformat(), "source_type": "cognition", "listed_due": None,
          "metadata": {"kind": "notice", "recipient": CONTACT, "content": "The parcel is running late, sorry.",
                       "grant": "owner"}, "counterpart": CONTACT, "obligor": "assistant"}
CHECK_IN = {**NOTICE, "description": f"Ask {CONTACT} about the budget draft",
            "metadata": {"kind": "check_in", "recipient": CONTACT, "topic": "the budget draft figures and more words here",
                         "grant": "owner"}}


def _store(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    return CommitmentStore(tmp_path / "protagine-commitments.db")


class KeepRouter:
    """The claim-review pass's judge, keeping every proposal (what a correct judge does with a real ask)."""

    async def complete(self, messages, *, context=None, **_):
        assert (context or {}).get("task") == "source_claim_review"
        count = len(json.loads(messages[1]["content"])["proposals"])
        content = json.dumps({str(index): {"keep": True, "reason": "the owner asks for it"} for index in range(count)})
        return SimpleNamespace(content=content, model_id="judge")


def asking(item, asked):
    """The item with the owner's words that ask for the message, as the extractor quotes them."""
    return {**item, "metadata": {**item["metadata"], "asked": asked}}


async def reviewed(items, said, *, person=None):
    """The items as the claim-review pass leaves them on the owner's turn."""
    return await extract.review_message_requests(KeepRouter(), items, person_id=person or OWNER, owner_text=said,
                                                 owner_names=[OWNER])


# ---------------------------------------------------------------------------
# capture: the contract and the stored metadata
# ---------------------------------------------------------------------------

def test_the_extractor_contract_names_case_three_with_both_shapes():
    assert '"kind":"notice"' in extract.SYSTEM and '"kind":"check_in"' in extract.SYSTEM
    assert "A MESSAGE TO A THIRD PARTY" in extract.SYSTEM
    assert '"grant":"owner"' in extract.SYSTEM and "p-05" in extract.SYSTEM
    assert "6 words" in extract.SYSTEM
    assert extract.ITEM_SCHEMA["properties"]["metadata"]["type"] == ["object", "null"]


def test_every_timed_message_for_a_third_party_is_case_three_and_case_two_is_the_persons_own():
    """Audit M5 (timed half): case 2 no longer sends "send it to someone else" back to the person
    who asked; a message for a third party at a time or on a condition is case 3."""
    assert "send it to someone else" not in extract.SYSTEM
    assert "Anything for SOMEONE ELSE is case 3, never case 2" in extract.SYSTEM
    assert "unless the reply shows it already went to them" in extract.SYSTEM


async def test_a_deliverable_for_someone_else_is_a_message_to_them_never_to_the_person_who_asked(tmp_path):
    """Audit M5: a deliverable goes to the turn's own person, so words meant for a third party are a
    case-3 message to that party: a notice when they are the owner's own words, else a check-in. Like
    any case-3 message it stands only on the owner's confirmed words asking for it."""
    store = _store(tmp_path)
    deliverable = {**NOTICE, "description": f"Send {CONTACT} the venue address", "source_type": "introspection",
                   "metadata": {"kind": "deliverable", "content": "The venue is at 5 Main St.", "channel_hint": "sms"}}
    said = f"Send {CONTACT} the venue address."
    composed = record_items(await reviewed([asking(deliverable, said)], said), person_id=OWNER, commitment_store=store,
                            existing=[], rejections=[], owner_id=OWNER, owner_text=said)
    row = store.get(composed["created"][0])
    assert row["metadata"]["kind"] == "check_in" and row["metadata"]["recipient"] == CONTACT
    assert row["metadata"]["grant"] == "owner" and "content" not in row["metadata"]
    said = f"Text {CONTACT}: the venue is at 5 Main St."
    dictated = record_items(await reviewed([asking(deliverable, f"Text {CONTACT}")], said), person_id=OWNER,
                            commitment_store=_store(tmp_path / "dictated"), existing=[], rejections=[], owner_id=OWNER,
                            owner_text=said)
    row = _store(tmp_path / "dictated").get(dictated["created"][0])
    assert row["metadata"]["kind"] == "notice" and row["metadata"]["content"] == "The venue is at 5 Main St."
    # Without the owner's words asking for it, it is the owner's own reminder, never a message to them.
    unasked = record_items([deliverable], person_id=OWNER, commitment_store=_store(tmp_path / "unasked"), existing=[],
                           rejections=[], owner_id=OWNER, owner_text=said)
    row = _store(tmp_path / "unasked").get(unasked["created"][0])
    assert row["metadata"]["kind"] == "reminder" and "grant" not in row["metadata"]
    assert row["metadata"]["obligor"] == "owner"
    # A contact cannot have their deliverable relayed: no grant, so it is an ordinary row.
    relayed = record_items([deliverable], person_id=OTHER, commitment_store=store, existing=[], rejections=[],
                           owner_id=OWNER, owner_text=f"Text {CONTACT}: the venue is at 5 Main St.")
    row = store.get(relayed["created"][0])
    assert row["metadata"]["kind"] == "notice" and "grant" not in row["metadata"]
    # A deliverable for the person themselves is still one.
    for index, counterpart in enumerate((None, "owner", OWNER)):
        own_store = _store(tmp_path / f"own-{index}")
        own = record_items([{**deliverable, "description": "Email me the venue address", "counterpart": counterpart}],
                           person_id=OWNER, commitment_store=own_store, existing=[], rejections=[], owner_id=OWNER,
                           owner_text="Email me the venue address.")
        assert own_store.get(own["created"][0])["metadata"]["kind"] == "deliverable"


async def test_record_items_stores_the_notice_and_the_check_in_metadata_for_the_owner(tmp_path):
    store = _store(tmp_path)
    said = (f"If {CONTACT} has not confirmed within 30 minutes, tell them: the parcel is running late, sorry. "
            f"Then ask {CONTACT} about the budget draft too.")
    items = await reviewed([asking(NOTICE, f"If {CONTACT} has not confirmed within 30 minutes, tell them"),
                            asking(CHECK_IN, f"ask {CONTACT} about the budget draft")], said)
    result = record_items(items, person_id=OWNER, commitment_store=store, existing=[], rejections=[],
                          turn_id="turn-1", owner_id=OWNER, owner_text=said)
    assert len(result["created"]) == 2
    notice, check_in = (store.get(ident) for ident in result["created"])
    assert notice["metadata"]["kind"] == "notice" and notice["metadata"]["content"] == "The parcel is running late, sorry."
    assert notice["metadata"]["recipient"] == CONTACT and notice["metadata"]["grant"] == "owner"
    assert notice["metadata"]["obligor"] == "assistant" and notice["metadata"]["counterpart"] == CONTACT
    assert check_in["metadata"]["kind"] == "check_in" and check_in["metadata"]["grant"] == "owner"
    assert check_in["metadata"]["topic"] == "the budget draft figures and more"     # clamped to six words
    assert notice["metadata"]["asked"] == f"If {CONTACT} has not confirmed within 30 minutes, tell them"
    assert notice["metadata"]["request_review"]["keep"] is True


def test_a_contacts_own_request_to_message_a_third_party_carries_no_grant(tmp_path):
    store = _store(tmp_path)
    result = record_items([NOTICE], person_id=OTHER, commitment_store=store, existing=[], rejections=[],
                          turn_id="turn-2", owner_id=OWNER)
    row = store.get(result["created"][0])
    assert row["metadata"]["kind"] == "notice" and "grant" not in row["metadata"]


async def test_a_notice_carries_only_words_the_owner_said(tmp_path):
    """Audit M4(d): the notice path sends the owner's own words verbatim, so words the owner never
    said (a model's paraphrase, or owner-only detail) become a check-in around the matter."""
    store = _store(tmp_path)
    said = f"If {CONTACT} has not confirmed within 30 minutes, tell them: the parcel is running late,  sorry!"
    asked = f"If {CONTACT} has not confirmed within 30 minutes, tell them"
    kept = record_items(await reviewed([asking(NOTICE, asked)], said), person_id=OWNER, commitment_store=store,
                        existing=[], rejections=[], owner_id=OWNER, owner_text=said)
    assert store.get(kept["created"][0])["metadata"]["content"] == "The parcel is running late, sorry."
    invented = {**NOTICE, "description": f"Warn {CONTACT} about the late parcel",
                "metadata": {**NOTICE["metadata"], "content": "The parcel is late; the reserve is amber-cobalt-42."}}
    moved = record_items(await reviewed([asking(invented, asked)], said), person_id=OWNER, commitment_store=store,
                         existing=[], rejections=[], owner_id=OWNER, owner_text=said)
    row = store.get(moved["created"][0])
    assert row["metadata"]["kind"] == "check_in" and "content" not in row["metadata"]
    assert row["metadata"]["grant"] == "owner" and row["metadata"]["topic"] == "Warn about the late parcel"


def test_the_extractor_contract_says_a_withheld_permission_is_no_message():
    """Audit M4(c): permission-ask-holds is release-blocking, so the contract shows the negatives."""
    assert "Do not message p-05 until I say so." in extract.SYSTEM
    assert "I have not said you may write to p-05 yet" in extract.SYSTEM


def test_a_notice_without_words_is_the_owners_reminder(tmp_path):
    store = _store(tmp_path)
    empty = {**NOTICE, "metadata": {"kind": "notice", "recipient": CONTACT, "content": "  ", "grant": "owner"}}
    result = record_items([empty], person_id=OWNER, commitment_store=store, existing=[], rejections=[], owner_id=OWNER)
    row = store.get(result["created"][0])
    assert row["metadata"]["kind"] == "reminder" and row["metadata"]["counterpart"] == CONTACT
    assert "grant" not in row["metadata"] and "recipient" not in row["metadata"]


# ---------------------------------------------------------------------------
# the drive: candidates from the rows
# ---------------------------------------------------------------------------

def _row(ident, item, *, recipient_id=CONTACT, due=T0 - timedelta(minutes=5)):
    metadata = confirmed(dict(item["metadata"], counterpart=item["counterpart"], obligor=item["obligor"]))
    if recipient_id:
        metadata.update(recipient_id=recipient_id, recipient_exact=True)   # as the tick resolves an id
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
    metadata = confirmed({**item["metadata"], "counterpart": item["counterpart"], "obligor": item["obligor"],
                          **overrides})
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


async def test_a_grant_to_someone_matched_only_by_name_is_an_owner_ask(make):
    """Audit M4(a): the owner named "Sam"; the store found Sam by name only. The mind does not send
    on a guess: the owner is asked, seeing both the name they gave and who it matched."""
    fx = make([contact(CONTACT, may_contact="auto", name="Sam")])
    row = _seed(fx, {**NOTICE, "metadata": {**NOTICE["metadata"], "recipient": "Sam"}, "counterpart": "Sam"})
    fx.shift(C + PAST)
    formed, = (await fx.tick())["formed"]
    assert formed["type"] == "commitment_notice" and formed["decision"] == "ask" and formed["status"] == "asked"
    stored = fx.store.get(formed["id"])
    assert stored.entity_id == CONTACT and "Sam" in stored.description and CONTACT in stored.description
    assert fx.commitments.get(row["id"])["metadata"]["recipient_exact"] is False
    assert [p for p in await fx.mind.outbox_ready() if p["recipient"] == CONTACT] == []


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


async def test_people_off_turns_the_granted_message_into_the_owners_reminder(make):
    """Audit M9, revised by the capture-safety review: with the faculty off the owner's message to a
    third party is never sent, composed or asked about, and never a worker's task either (a worker
    with a send tool could reach the contact): it is the owner's own reminder that it fell due."""
    fx = make([contact(CONTACT, may_contact="auto", name="Sam")], config={"faculties": {"people": False}})
    notice = _seed(fx, NOTICE)
    _seed(fx, {**CHECK_IN, "metadata": {**CHECK_IN["metadata"], "recipient": "Kim"}, "counterpart": "Kim"})
    fx.shift(C + PAST)
    formed = (await fx.tick())["formed"]
    assert sorted(item["type"] for item in formed) == ["commitment_reminder", "commitment_reminder"]
    assert all(fx.store.get(item["id"]).entity_id == OWNER and item["kind"] == "message" for item in formed)
    assert fx.messages_to(CONTACT) == [] and len(fx.messages_to(OWNER)) == 2
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

    def __init__(self, item, reply, cue="within 12 minutes"):
        self.item, self.reply, self.cue, self.calls = item, reply, cue, []

    def function_deadline_seconds(self, *, context=None):
        return 20

    async def complete(self, messages, *, context=None, **_):
        self.calls.append((context, messages))
        task = (context or {}).get("task")
        if task == "commitment_extract":
            prompt = messages[1]["content"].split("This turn, verbatim:", 1)[-1]
            return SimpleNamespace(content=json.dumps([self.item] if self.cue in prompt else []))
        if task == "mind_compose":
            return SimpleNamespace(content=self.reply, usage={"total_tokens": 20})
        if task == "source_claim_review":
            return await KeepRouter().complete(messages, context=context)
        raise AssertionError(f"unexpected task {task}")


async def test_delegated_chase_reaches_the_contact_not_the_owner_and_never_the_other_contact(make):
    item = {"action": "create", "target": None, "description": f"Ask {CONTACT} for the budget draft", "priority": 70,
            "due_at": (T0 + timedelta(minutes=12)).isoformat(), "source_type": "cognition", "listed_due": None,
            "metadata": {"kind": "check_in", "recipient": CONTACT, "topic": "the budget draft", "grant": "owner",
                         "asked": f"If {CONTACT} has not sent me the budget draft within 12 minutes, ask them for "
                                  "it yourself"},
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
    assert len([c for c, _ in router.calls if c.get("task") == "source_claim_review"]) == 1
    fx.mind.outbox.sending(payload["id"], target=f"capture:{CONTACT}")
    fx.mind.outbox.sent(payload["id"])
    assert fx.commitments.get(row["id"])["status"] == "fulfilled"
    assert fx.mind.dispatch() == [] and (await fx.tick())["formed"] == []


async def test_an_owner_message_for_a_contact_now_is_the_replys_job_and_never_a_notice(make):
    """Task scope: "an immediate relay is the foreground turn's job and never becomes a notice".
    Even when the model records "tell p-05 X" as a notice due in two minutes, capture drops it, so
    the mind never sends a second copy of what the reply (with its own send tool) already sent."""
    item = {"action": "create", "target": None, "description": f"Tell {CONTACT} the meeting moved to Tuesday",
            "due_at": (T0 + timedelta(minutes=2)).isoformat(), "priority": 70, "source_type": "cognition",
            "listed_due": None, "counterpart": CONTACT, "obligor": "assistant",
            "metadata": {"kind": "notice", "recipient": CONTACT, "content": "The meeting moved to Tuesday.",
                         "grant": "owner"}}
    router = ScriptedRouter(item, "unused", cue="the meeting moved to Tuesday")
    fx = make([contact(CONTACT, may_contact="ask"), contact(OTHER, may_contact="auto")], router=router)
    fx.mind.capture = CommitmentExtractor(fx.ledger, lambda: fx.commitments)
    fx.ledger.record_source("turn-1", contact_id=OWNER, session_id="owner-1", messages=[
        {"role": "user", "content": f"Tell {CONTACT} the meeting moved to Tuesday."},
        {"role": "assistant", "content": "Done, I sent it to them."}], occurred_at=fx.now.isoformat())
    first = await fx.tick()
    assert first["capture_drained"]["processed"] == 1 and first["formed"] == []
    assert fx.commitments.get_pending_for_person(OWNER) == []
    fx.shift(timedelta(minutes=2, seconds=30))
    assert (await fx.tick())["formed"] == []
    assert await fx.mind.outbox_ready() == []
    assert fx.messages_to(CONTACT) == [] and fx.messages_to(OWNER) == []


# ---------------------------------------------------------------------------
# permission at send time, and the owner's word on a name (review F1, F2)
# ---------------------------------------------------------------------------

async def test_an_approved_notice_is_not_sent_after_the_recipient_opts_out(make):
    """Review F1: a notice formed at 22:40 is approved and held by quiet hours; the contact texts
    STOP at 23:10 and is lowered to never. At 07:15 it must not go: permission is read again at
    the moment a message may leave, not only when it was formed."""
    fx = make([contact(CONTACT, may_contact="ask")], config={"quiet_hours": "22:00-07:00"})
    fx.now = T0.replace(hour=22, minute=30)
    fx.commitments.create(person_id=OWNER, description=NOTICE["description"],
                          due_at=(fx.now + timedelta(minutes=10)).isoformat(), source_type="cognition",
                          metadata=confirmed({**NOTICE["metadata"], "counterpart": CONTACT, "obligor": "assistant"}))
    fx.shift(timedelta(minutes=10) + PAST)
    formed, = (await fx.tick())["formed"]
    assert formed["type"] == "commitment_notice" and formed["status"] == "approved"
    assert await fx.mind.outbox_ready() == []
    fx.contacts.records[CONTACT]["may_contact"] = "never"
    fx.shift(timedelta(hours=8, minutes=30))
    assert [p for p in await fx.mind.outbox_ready() if p["recipient"] == CONTACT] == []
    row = fx.store.get(formed["id"])
    assert row.status == "cancelled" and "never" in (row.cancelled_reason or "")
    await fx.tick()
    assert [p for p in await fx.mind.outbox_ready() if p["recipient"] == CONTACT] == []


async def test_the_owners_yes_does_not_send_to_someone_who_opted_out_since_the_ask(make):
    """Review F1: ``answer(yes)`` approved an asked message without reading ``may_contact`` again,
    so a contact who opted out while the ask waited was messaged on the owner's yes."""
    fx = make([contact(CONTACT, may_contact="ask", cadence=10)])
    fx.shift(C + PAST)
    formed, = (await fx.tick())["formed"]
    asked = fx.store.get(formed["id"])
    assert asked.status == "asked" and asked.type == "check_in"
    fx.contacts.records[CONTACT]["may_contact"] = "never"
    answered = await fx.mind.answer(asked.ask_code, yes=True, contact_id=OWNER)
    assert answered is not None and answered.status == "cancelled"
    assert [p for p in await fx.mind.outbox_ready() if p["recipient"] == CONTACT] == []


async def test_a_name_matched_notice_held_by_the_cooldown_still_waits_for_the_owners_word(make):
    """Review F2: the owner named "Sam" and the store matched the contact by name only. The first,
    exact notice to them started the 24 h cooldown; the name-matched one must never go out on a
    re-decision without the owner confirming who "Sam" is."""
    fx = make([contact(CONTACT, may_contact="auto", name="Sam")])
    _seed(fx, NOTICE)
    fx.shift(C + PAST)
    first, = (await fx.tick())["formed"]
    assert first["status"] == "approved"
    await fx.send_all()
    named = {**NOTICE, "description": "Tell Sam the van is booked",
             "metadata": {**NOTICE["metadata"], "recipient": "Sam", "content": "The van is booked."},
             "counterpart": "Sam"}
    row = _seed(fx, named)
    fx.shift(C + PAST)
    formed, = (await fx.tick())["formed"]
    assert fx.commitments.get(row["id"])["metadata"]["recipient_exact"] is False
    assert formed["decision"] == "ask" and formed["status"] == "asked"
    for _ in range(3):
        fx.shift(timedelta(hours=25))
        await fx.tick()
        assert [p for p in await fx.mind.outbox_ready() if p["recipient"] == CONTACT] == []
    stored = fx.store.get(formed["id"])
    assert stored.status in {"asked", "expired"} and stored.context.get("ask_owner") is True


async def test_a_deferred_row_that_needs_the_owners_word_is_asked_when_it_is_reconsidered(make):
    """Whatever deferred it (a budget, a breaker), a message the owner must confirm, or an owner
    question, is re-decided as an ask: the rule is stored on the row, not only in the form step."""
    fx = make([contact(CONTACT, may_contact="auto", name="Sam")])
    row, _ = fx.store.create_intention(
        kind="message", type="commitment_notice", title="Notice to Sam: Tell Sam the van is booked", drive="duty",
        cls="contact", decision="defer", decision_reason="budget", status="proposed", dedup_key="commitment:x:notice",
        recipient=CONTACT, context={"text": "The van is booked.", "may_contact": "auto", "ask_owner": True},
        expires_at=fx.now + timedelta(hours=48), hermes_kind="none", created_at=fx.now)
    fx.store.transition(row.id, "proposed", action="deferred", at=fx.now, decision="defer")
    fx.shift(timedelta(minutes=1))
    await fx.tick()
    stored = fx.store.get(row.id)
    assert stored.status == "asked" and stored.ask_code
    assert [p for p in await fx.mind.outbox_ready() if p["recipient"] == CONTACT] == []


async def test_a_name_match_ask_shows_the_name_given_and_the_person_and_handle_it_matched(make):
    """Audit M4(a): the owner confirms who a name meant from the ask itself: the words they used,
    the contact's name and a handle, not an internal id."""
    fx = make([contact(CONTACT, may_contact="auto", name="Sam Rivera")])
    fx.contacts.records[CONTACT]["given_name"] = "Sam"
    _seed(fx, {**NOTICE, "metadata": {**NOTICE["metadata"], "recipient": "Sam Rivera"}, "counterpart": "Sam Rivera"})
    fx.shift(C + PAST)
    formed, = (await fx.tick())["formed"]
    stored = fx.store.get(formed["id"])
    assert formed["status"] == "asked"
    assert "'Sam Rivera'" in stored.description and f"capture:{CONTACT}" in stored.description


# ---------------------------------------------------------------------------
# an immediate relay is the foreground turn's job (task scope; review F4)
# ---------------------------------------------------------------------------

def test_the_contract_says_a_message_to_pass_on_now_is_the_replys_own_job():
    example = extract.SYSTEM.split("They said: Tell p-05 the meeting moved to Tuesday.", 1)[1]
    assert example.split("\n", 2)[1].startswith("[]")
    assert "reply's own job" in extract.SYSTEM
    assert "about two minutes from now when it is to go now" not in extract.SYSTEM


async def test_capture_never_records_a_message_to_a_third_party_due_as_the_turn_happens(tmp_path):
    """A notice, a check-in or a deliverable for someone else due within minutes of the turn is a
    relay the reply itself sends (every arm's foreground has a send path): recording it as a
    notice would send it twice. A message timed later or on a condition is recorded as before."""
    from datetime import datetime
    turn = datetime.fromisoformat(NOTICE["due_at"]) - timedelta(minutes=30)
    store = _store(tmp_path)
    now_notice = {**NOTICE, "due_at": (turn + timedelta(minutes=2)).isoformat()}
    now_check_in = {**CHECK_IN, "due_at": turn.isoformat()}
    deliverable = {**NOTICE, "description": f"Send {CONTACT} the venue address", "source_type": "introspection",
                   "due_at": (turn + timedelta(minutes=2)).isoformat(),
                   "metadata": {"kind": "deliverable", "content": "The venue is at 5 Main St.", "channel_hint": "sms"}}
    later = asking({**NOTICE, "description": f"Tell {CONTACT} the parcel is late if it has not arrived"},
                   f"If it has not arrived by then, tell {CONTACT}")
    said = (f"Tell {CONTACT}: the parcel is running late, sorry. The venue is at 5 Main St. If it has not "
            f"arrived by then, tell {CONTACT}.")
    result = record_items(await reviewed([now_notice, now_check_in, deliverable, later], said), person_id=OWNER,
                          commitment_store=store, existing=[], rejections=[], owner_id=OWNER, turn_time=turn,
                          owner_text=said)
    assert len(result["created"]) == 1 and result["ignored_actions"] == 3
    kept = store.get(result["created"][0])
    assert kept["metadata"]["kind"] == "notice" and kept["due_at"].startswith(NOTICE["due_at"][:16])


async def test_a_notice_formed_before_the_request_review_is_the_owners_reminder_when_it_would_go(make, monkeypatch):
    """An owner-granted notice the previous release formed and approved, from a row whose request the review
    never confirmed ("tell me if it is late" misread as a word to the contact), is read again against its
    commitment when it may leave: never sent to the contact, and the owner's reminder takes its place."""
    from protagine.mind import drives
    fx = make([contact(CONTACT, may_contact="ask")], config={"quiet_hours": "22:00-07:00"})
    fx.now = T0.replace(hour=22, minute=30)
    fx.commitments.create(person_id=OWNER, description=NOTICE["description"],
                          due_at=(fx.now + timedelta(minutes=10)).isoformat(), source_type="cognition",
                          metadata={**NOTICE["metadata"], "counterpart": CONTACT, "obligor": "assistant"})
    with monkeypatch.context() as previous:          # the release before the review: every stored grant stood
        previous.setattr(drives, "request_confirmed", lambda metadata: True)
        fx.shift(timedelta(minutes=10) + PAST)
        formed, = (await fx.tick())["formed"]
    assert formed["type"] == "commitment_notice" and formed["status"] == "approved"
    fx.shift(timedelta(hours=8, minutes=30))         # upgraded while quiet hours held it
    assert [p for p in await fx.mind.outbox_ready() if p["recipient"] == CONTACT] == []
    assert fx.store.get(formed["id"]).status == "cancelled"
    later = [entry for _ in range(2) for entry in (await fx.tick())["formed"]]
    sent = await fx.send_all()
    assert [entry["type"] for entry in later] == ["commitment_reminder"]
    assert [p["recipient"] for p in sent] == [OWNER]
    assert all(row.status == "cancelled" for row in fx.messages_to(CONTACT))


async def test_an_asked_notice_formed_before_the_request_review_never_goes_on_the_owners_yes(make, monkeypatch):
    """The same stored grant to a name the store matched only by name waited for the owner's word: their yes
    after the upgrade sends nothing to the contact either."""
    from protagine.mind import drives
    fx = make([contact(CONTACT, may_contact="auto", name="Sam")])
    fx.commitments.create(person_id=OWNER, description=NOTICE["description"],
                          due_at=(fx.now + timedelta(minutes=10)).isoformat(), source_type="cognition",
                          metadata={**NOTICE["metadata"], "recipient": "Sam", "counterpart": "Sam",
                                    "obligor": "assistant"})
    with monkeypatch.context() as previous:
        previous.setattr(drives, "request_confirmed", lambda metadata: True)
        fx.shift(timedelta(minutes=10) + PAST)
        formed, = [entry for entry in (await fx.tick())["formed"] if entry["type"] == "commitment_notice"]
    asked = fx.store.get(formed["id"])
    assert asked.status == "asked" and asked.entity_id == CONTACT
    await fx.mind.answer(asked.ask_code, yes=True, contact_id=OWNER)
    assert [p for p in await fx.mind.outbox_ready() if p["recipient"] == CONTACT] == []
    assert fx.store.get(formed["id"]).status == "cancelled"
