"""A message to a third party exists only when the owner's own words ask the assistant to send it.

Capture's case 3 (a ``notice`` or ``check_in`` the mind later sends to a contact with the owner's
grant) reaches someone the owner did not write to, so it needs more than the extractor's reading:

* the extractor quotes the owner's words that ask for it (``asked``), naming the recipient;
* the quote must be the owner's own text and name the recipient (checked in code);
* the claim-review pass confirms, against the owner's whole message, that the quote asks the
  assistant itself to contact that recipient (``beliefs.source_claims.review_proposals``);
* a recipient that is the owner ("owner", "me", the owner's id or name) is never a third party.

Anything short of that is the owner's own reminder: a word to the owner when it falls due, never a
message to the contact and never a question about who the recipient is.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from protagine.commitments import extract
from protagine.commitments.extract import CommitmentExtractor, record_items
from protagine.commitments.store import CommitmentStore
from protagine.mind.drives import DriveInputs, duty
from test_mind_social import make  # noqa: F401  (pytest fixture)

OWNER = "owner-7"
FLORIST = "p-31"
LANDLORD = "p-05"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _item(description, *, metadata, due_in=timedelta(hours=2), counterpart=None, obligor="assistant"):
    return {"action": "create", "target": None, "description": description,
            "due_at": (_now() + due_in).isoformat(), "priority": 70, "source_type": "cognition",
            "metadata": metadata, "listed_due": None, "counterpart": counterpart, "obligor": obligor}


def _reply(content):
    content = content if isinstance(content, str) else json.dumps(content)
    choice = SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))
    return SimpleNamespace(raw=SimpleNamespace(choices=[choice]), content=content, model_id="fake-judge")


class _Router:
    """Answers the capture call with ``items`` and the review call with ``keep`` (one decision per
    proposal), or raises on the review when ``review_error`` is set."""

    supports_function_routing = True

    def __init__(self, items, *, keep=None, review_error=None):
        self.items, self.keep, self.review_error = items, keep, review_error
        self.calls = []

    def function_deadline_seconds(self, *, context=None):
        return 20

    async def complete(self, messages, *, context=None, **_):
        self.calls.append((messages, context))
        if (context or {}).get("task") == "source_claim_review":
            if self.review_error is not None:
                raise self.review_error
            count = len(json.loads(messages[1]["content"])["proposals"])
            keep = self.keep if isinstance(self.keep, list) else [bool(self.keep)] * count
            return _reply({str(index): {"keep": keep[index], "reason": "judged"} for index in range(count)})
        return _reply(self.items)

    def reviews(self):
        return [(messages, context) for messages, context in self.calls
                if (context or {}).get("task") == "source_claim_review"]


def _setup(tmp_path, monkeypatch):
    from protagine.turns.idempotency import TurnIdempotencyLedger
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    store = CommitmentStore(db_path=tmp_path / "c.db")
    ledger = TurnIdempotencyLedger(tmp_path / "ledger.db")
    return store, ledger, CommitmentExtractor(ledger, lambda: store)


def _turn(ledger, turn_id, said, *, person=OWNER, reply="Understood."):
    ledger.record_source(turn_id, contact_id=person, session_id=f"s-{person}", channel_id=None, messages=[
        {"role": "user", "content": said}, {"role": "assistant", "content": reply}])


def _rows(store, person=OWNER):
    return store.list(status=["pending", "overdue"], person_id=person)["commitments"]


def _due(store, person=OWNER):
    """What the duty drive forms once every row is past due."""
    rows = _rows(store, person)
    later = max(datetime.fromisoformat(row["due_at"]) for row in rows) + timedelta(minutes=1)
    return duty(DriveInputs(now=later, owner_id=OWNER, commitments=rows))[1]


def _check_in(recipient, *, asked=None, topic="the quote"):
    metadata = {"kind": "check_in", "recipient": recipient, "topic": topic, "grant": "owner"}
    if asked is not None:
        metadata["asked"] = asked
    return metadata


def _assert_owner_reminder_only(store):
    row, = _rows(store)
    metadata = row["metadata"] or {}
    assert metadata.get("kind") == "reminder" and "grant" not in metadata
    assert "recipient" not in metadata and metadata.get("obligor") == "owner"
    candidates = _due(store)
    assert [c.type for c in candidates] == ["commitment_reminder"]
    assert all(c.recipient == OWNER for c in candidates)


# --- the unsafe case: "tell me if it slips" never becomes a message to the contact -------------------

SAID_TELL_ME = f"The florist {FLORIST} promised the flower quote by four. If it slips, give me a shout."


@pytest.mark.asyncio
async def test_a_word_the_owner_wants_for_themselves_is_never_a_message_to_the_contact(tmp_path, monkeypatch):
    """The extractor misreads "give me a shout" as a chase of the florist and quotes the words that ask
    for it: they never name the florist as the one to be contacted, so no review is spent and the row is
    the owner's own reminder."""
    store, ledger, extractor = _setup(tmp_path, monkeypatch)
    _turn(ledger, "t-1", SAID_TELL_ME)
    router = _Router([_item(f"Chase {FLORIST} for the flower quote", counterpart=FLORIST,
                            metadata=_check_in(FLORIST, asked="If it slips, give me a shout."))])
    assert await extractor.process_one(router) is True
    assert router.reviews() == []
    _assert_owner_reminder_only(store)


@pytest.mark.asyncio
async def test_the_review_pass_refuses_a_quote_that_asks_for_a_word_to_the_owner(tmp_path, monkeypatch):
    """The quote is the owner's and names the florist, so the claim-review pass judges it against the
    whole message; it finds a word the owner wants for themselves and the row is their reminder."""
    store, ledger, extractor = _setup(tmp_path, monkeypatch)
    _turn(ledger, "t-1", SAID_TELL_ME)
    router = _Router([_item(f"Chase {FLORIST} for the flower quote", counterpart=FLORIST,
                            metadata=_check_in(FLORIST, asked=SAID_TELL_ME))], keep=False)
    assert await extractor.process_one(router) is True
    (messages, context), = router.reviews()
    payload = json.loads(messages[1]["content"])
    assert payload["message"] == SAID_TELL_ME
    proposal = payload["proposals"][0]["claim"]
    assert proposal["recipient"] == FLORIST and proposal["asked"] == SAID_TELL_ME
    assert context["response_schema"]["name"] == "source_claim_review"
    _assert_owner_reminder_only(store)


@pytest.mark.asyncio
@pytest.mark.parametrize("asked", [None, "", "Please do chase the florist tomorrow.", "chase p-31"])
async def test_a_request_the_owner_never_wrote_is_the_owners_reminder(tmp_path, monkeypatch, asked):
    """No quote, an empty one, or words that are not in the owner's message: nothing to review."""
    store, ledger, extractor = _setup(tmp_path, monkeypatch)
    _turn(ledger, "t-1", SAID_TELL_ME)
    router = _Router([_item(f"Chase {FLORIST} for the flower quote", counterpart=FLORIST,
                            metadata=_check_in(FLORIST, asked=asked))], keep=True)
    assert await extractor.process_one(router) is True
    assert router.reviews() == []
    _assert_owner_reminder_only(store)


@pytest.mark.asyncio
async def test_a_review_that_fails_leaves_the_owners_reminder(tmp_path, monkeypatch):
    """Fail closed: a review the router could not give is no confirmation."""
    store, ledger, extractor = _setup(tmp_path, monkeypatch)
    said = f"If {LANDLORD} has not sent it by 5, chase them yourself."
    _turn(ledger, "t-1", said)
    router = _Router([_item(f"Chase {LANDLORD} for the signed lease", counterpart=LANDLORD,
                            metadata=_check_in(LANDLORD, asked=said, topic="the signed lease"))],
                     review_error=TimeoutError("judge unavailable"))
    assert await extractor.process_one(router) is True
    assert len(router.reviews()) == 1
    _assert_owner_reminder_only(store)


@pytest.mark.asyncio
async def test_a_review_the_extractor_wrote_itself_counts_for_nothing(tmp_path, monkeypatch):
    """The review is the pass's to record: a ``request_review`` in the model's own metadata is dropped
    before the pass runs, so it can neither skip the review nor overrule it."""
    store, ledger, extractor = _setup(tmp_path, monkeypatch)
    _turn(ledger, "t-1", SAID_TELL_ME)
    forged = {**_check_in(FLORIST, asked=SAID_TELL_ME),
              "request_review": {"version": "message-request-review-v1", "keep": True, "reason": "ok"}}
    router = _Router([_item(f"Chase {FLORIST} for the flower quote", counterpart=FLORIST, metadata=forged)],
                     keep=False)
    assert await extractor.process_one(router) is True
    _assert_owner_reminder_only(store)


# --- a recipient who is the owner --------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("recipient", ["owner", "the owner", "me", OWNER, "Robin Vale"])
async def test_a_recipient_who_is_the_owner_is_the_owners_reminder_without_a_question(tmp_path, monkeypatch,
                                                                                         recipient):
    """"Check I sent the brief" recorded as a check-in with the owner as recipient: no review, no grant,
    and never "I do not know who owner is"."""
    monkeypatch.setenv("PROTAGINE_OWNER_NAME", "Robin Vale")
    store, ledger, extractor = _setup(tmp_path, monkeypatch)
    said = f"Make sure I actually sent {LANDLORD} the design brief by half past."
    _turn(ledger, "t-1", said)
    router = _Router([_item(f"Check the owner sent {LANDLORD} the design brief", counterpart=LANDLORD,
                            metadata=_check_in(recipient, asked=said, topic="the design brief"))], keep=True)
    assert await extractor.process_one(router) is True
    assert router.reviews() == []
    _assert_owner_reminder_only(store)
    assert not [c for c in _due(store) if c.type == "recipient_unknown"]


def test_a_stored_message_naming_the_owner_is_never_asked_about_and_reminds_the_owner(tmp_path):
    """Rows captured before the guard (a granted check-in addressed to "owner") read the same way."""
    store = CommitmentStore(db_path=tmp_path / "c.db")
    store.create(person_id=OWNER, description=f"Check the owner sent {LANDLORD} the brief", priority=70,
                 due_at=(_now() + timedelta(minutes=30)).isoformat(), source_type="cognition",
                 metadata={"kind": "check_in", "recipient": "owner", "topic": "the brief", "grant": "owner",
                           "obligor": "assistant", "counterpart": LANDLORD})
    rows = _rows(store)
    early = duty(DriveInputs(now=_now(), owner_id=OWNER, commitments=rows))[1]
    assert early == []                                   # nothing before it is due, and no "who is owner?"
    assert [(c.type, c.recipient) for c in _due(store)] == [("commitment_reminder", OWNER)]


def test_a_message_the_mind_may_not_send_is_the_owners_reminder_never_a_worker_task(tmp_path):
    """A notice or check-in on the owner's lane without a grant (captured unverified, or before the
    review existed) is a word to the owner when due; a worker with messaging tools never gets it."""
    store = CommitmentStore(db_path=tmp_path / "c.db")
    store.create(person_id=OWNER, description=f"Chase {FLORIST} for the flower quote", priority=70,
                 due_at=(_now() + timedelta(minutes=30)).isoformat(), source_type="cognition",
                 metadata={"kind": "check_in", "recipient": FLORIST, "topic": "the quote",
                           "obligor": "assistant", "counterpart": FLORIST})
    assert [(c.type, c.kind, c.recipient) for c in _due(store)] == [("commitment_reminder", "message", OWNER)]


# --- the legitimate delegated chase ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_chase_the_owner_hands_over_is_confirmed_and_granted(tmp_path, monkeypatch):
    store, ledger, extractor = _setup(tmp_path, monkeypatch)
    said = f"{LANDLORD} owes me the signed lease. If {LANDLORD} has not sent it by 5, chase them yourself."
    asked = f"If {LANDLORD} has not sent it by 5, chase them yourself."
    _turn(ledger, "t-1", said)
    router = _Router([_item(f"Chase {LANDLORD} for the signed lease", counterpart=LANDLORD,
                            metadata=_check_in(LANDLORD, asked=asked, topic="the signed lease"))], keep=True)
    assert await extractor.process_one(router) is True
    assert len(router.reviews()) == 1
    row, = _rows(store)
    metadata = row["metadata"]
    assert metadata["kind"] == "check_in" and metadata["recipient"] == LANDLORD and metadata["grant"] == "owner"
    assert metadata["asked"] == asked
    assert metadata["request_review"]["keep"] is True and metadata["request_review"]["model_id"] == "fake-judge"
    # Once the tick resolves the recipient, the duty drive forms the check-in to them.
    resolved = [{**row, "metadata": {**metadata, "recipient_id": LANDLORD, "recipient_exact": True}}]
    later = datetime.fromisoformat(row["due_at"]) + timedelta(minutes=1)
    candidates = duty(DriveInputs(now=later, owner_id=OWNER, commitments=resolved))[1]
    assert [(c.type, c.recipient, c.grant) for c in candidates] == [("commitment_check_in", LANDLORD, "owner")]


@pytest.mark.asyncio
async def test_one_review_call_judges_every_proposal_of_the_turn_separately(tmp_path, monkeypatch):
    store, ledger, extractor = _setup(tmp_path, monkeypatch)
    said = (f"If {LANDLORD} has not sent the lease by 5, chase them yourself. "
            f"And {FLORIST} owes me the quote; tell me if it has not come by six.")
    _turn(ledger, "t-1", said)
    chase = _item(f"Chase {LANDLORD} for the lease", counterpart=LANDLORD,
                  metadata=_check_in(LANDLORD, asked=f"If {LANDLORD} has not sent the lease by 5, chase them yourself.",
                                     topic="the lease"))
    wrong = _item(f"Chase {FLORIST} for the quote", counterpart=FLORIST, due_in=timedelta(hours=3),
                  metadata=_check_in(FLORIST, asked=f"{FLORIST} owes me the quote; tell me if it has not come by six."))
    router = _Router([chase, wrong], keep=[True, False])
    assert await extractor.process_one(router) is True
    assert len(router.reviews()) == 1
    by_kind = {(row["metadata"] or {}).get("kind"): row for row in _rows(store)}
    assert by_kind["check_in"]["metadata"]["recipient"] == LANDLORD
    reminder = by_kind["reminder"]
    assert reminder["metadata"]["obligor"] == "owner" and "grant" not in reminder["metadata"]


# --- the same shapes elsewhere -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_deliverable_for_a_third_party_needs_the_same_request(tmp_path, monkeypatch):
    """Case 2 aimed at someone else becomes a case-3 notice, so it needs the owner's asking words too."""
    store, ledger, extractor = _setup(tmp_path, monkeypatch)
    _turn(ledger, "t-1", f"The venue is 5 Main St; {FLORIST} will need that at some point.")
    deliverable = _item(f"Send {FLORIST} the venue address", counterpart=FLORIST,
                        metadata={"kind": "deliverable", "content": "The venue is 5 Main St.", "channel_hint": "sms"})
    router = _Router([deliverable], keep=True)
    assert await extractor.process_one(router) is True
    assert router.reviews() == []
    _assert_owner_reminder_only(store)


@pytest.mark.asyncio
async def test_a_contacts_turn_is_never_reviewed_and_never_granted(tmp_path, monkeypatch):
    store, ledger, extractor = _setup(tmp_path, monkeypatch)
    said = f"If {LANDLORD} has not sent it by 5, chase them yourself."
    _turn(ledger, "t-1", said, person=FLORIST)
    router = _Router([_item(f"Chase {LANDLORD} for the lease", counterpart=LANDLORD,
                            metadata=_check_in(LANDLORD, asked=said, topic="the lease"))], keep=True)
    assert await extractor.process_one(router) is True
    assert router.reviews() == []
    row, = _rows(store, FLORIST)
    assert "grant" not in row["metadata"]


def test_record_items_without_a_review_never_grants(tmp_path):
    """The direct call (no review ran) records the owner's reminder, whatever the model's metadata says."""
    store = CommitmentStore(db_path=tmp_path / "c.db")
    said = f"If {LANDLORD} has not sent it by 5, chase them yourself."
    item = _item(f"Chase {LANDLORD} for the lease", counterpart=LANDLORD,
                 metadata=_check_in(LANDLORD, asked=said, topic="the lease"))
    result = record_items([item], person_id=OWNER, commitment_store=store, existing=[], rejections=[],
                          owner_id=OWNER, owner_text=said)
    assert result["owner_reminders"] == 1
    _assert_owner_reminder_only(store)


def test_the_contract_asks_for_the_owners_words_and_names_the_word_for_the_owner():
    system = " ".join(extract.SYSTEM.split())
    assert '"asked"' in system
    for phrase in ("tell me", "let me know", "flag it to me"):
        assert phrase in system
    assert "never case 3" in system


# --- end to end: owner turn -> capture -> tick past the time -> outbox ---------------------------------

class _MindRouter:
    """The extraction model misreads "let me know" as a chase and quotes the owner's passage; a correct
    judge refuses it. Nothing else may be asked of the model on this path."""

    supports_function_routing = True

    def __init__(self, item, *, keep):
        self.item, self.keep, self.tasks = item, keep, []

    def function_deadline_seconds(self, *, context=None):
        return 20

    async def complete(self, messages, *, context=None, **_):
        task = (context or {}).get("task")
        self.tasks.append(task)
        if task == "commitment_extract":
            said = messages[1]["content"].split("This turn, verbatim:", 1)[-1]
            return SimpleNamespace(content=json.dumps([self.item] if "quote" in said else []))
        if task == "source_claim_review":
            count = len(json.loads(messages[1]["content"])["proposals"])
            return SimpleNamespace(content=json.dumps({str(i): {"keep": self.keep, "reason": "read"}
                                                       for i in range(count)}), model_id="judge")
        if task == "mind_compose":
            return SimpleNamespace(content="Hi, any news on the quote?", usage={"total_tokens": 10})
        raise AssertionError(f"unexpected task {task}")


@pytest.mark.asyncio
async def test_end_to_end_a_word_for_the_owner_never_reaches_the_contact(make):
    """"Let me know if it has not come" misread as a chase: the quote names the florist, so the judge
    decides, refuses it, and past the time the owner gets their reminder while the florist gets nothing."""
    from test_mind_social import CONTACT as FLORIST_ID, OWNER as FX_OWNER, PAST, T0, contact
    said = f"{FLORIST_ID} promised me the flower quote within 20 minutes; let me know if it has not come by then."
    item = {"action": "create", "target": None, "description": f"Chase {FLORIST_ID} for the flower quote",
            "due_at": (T0 + timedelta(minutes=20)).isoformat(), "priority": 70, "source_type": "cognition",
            "listed_due": None, "counterpart": FLORIST_ID, "obligor": "assistant",
            "metadata": {"kind": "check_in", "recipient": FLORIST_ID, "topic": "the flower quote", "grant": "owner",
                         "asked": said}}
    router = _MindRouter(item, keep=False)
    fx = make([contact(FLORIST_ID, may_contact="auto")], router=router)
    fx.mind.capture = CommitmentExtractor(fx.ledger, lambda: fx.commitments)
    fx.ledger.record_source("turn-1", contact_id=FX_OWNER, session_id="owner-1", messages=[
        {"role": "user", "content": said}, {"role": "assistant", "content": "Will do."}], occurred_at=fx.now.isoformat())
    first = await fx.tick()
    assert first["capture_drained"]["recorded"] == 1 and first["formed"] == []
    assert router.tasks.count("source_claim_review") == 1
    fx.shift(timedelta(minutes=20) + PAST)
    formed = [entry for tick in [await fx.tick(), await fx.tick()] for entry in tick["formed"]]
    sent = await fx.send_all()
    assert [entry["type"] for entry in formed] == ["commitment_reminder"]
    assert [payload["recipient"] for payload in sent] == [FX_OWNER]
    assert fx.messages_to(FLORIST_ID) == []


def test_a_stored_grant_the_owners_words_never_stood_behind_is_never_sent(tmp_path):
    """A row granted before the review existed (or by any other writer) carries ``grant: owner`` but no
    confirmed request: once the people faculty is back on, it is the owner's reminder when due, never a
    message to the contact and never a question about who they are."""
    store = CommitmentStore(db_path=tmp_path / "c.db")
    for recipient_id in (FLORIST, None):
        metadata = {"kind": "check_in", "recipient": FLORIST, "topic": "the quote", "grant": "owner",
                    "obligor": "assistant", "counterpart": FLORIST}
        if recipient_id:
            metadata.update(recipient_id=recipient_id, recipient_exact=True)
        store.create(person_id=OWNER, description=f"Chase {FLORIST} for the flower quote ({recipient_id})",
                     priority=70, due_at=(_now() + timedelta(minutes=30)).isoformat(), source_type="cognition",
                     metadata=metadata)
    candidates = _due(store)
    assert sorted((c.type, c.recipient) for c in candidates) == [("commitment_reminder", OWNER)] * 2


# --- a chase about someone the conversation already named ---------------------------------------------

class _ByTurn(_Router):
    """Answers each capture call with the items for the turn it audits (matched by the owner's words)."""

    def __init__(self, by_said, **kwargs):
        super().__init__([], **kwargs)
        self.by_said = by_said

    async def complete(self, messages, *, context=None, **_):
        if (context or {}).get("task") != "source_claim_review":
            turn = messages[1]["content"].split("This turn, verbatim:", 1)[-1]
            self.items = next((items for said, items in self.by_said.items() if said in turn), [])
        return await super().complete(messages, context=context)


@pytest.mark.asyncio
async def test_a_chase_about_someone_named_in_an_earlier_turn_is_reviewed_with_that_turn(tmp_path, monkeypatch):
    """"p-05 owes me the site photos by 3." then "If they have not sent them by 3, chase them yourself.": the
    words ask the assistant to chase whoever "they" are, and the earlier turn says who. The code check and
    the review both see that turn; the request itself is still the owner's words in this one."""
    store, ledger, extractor = _setup(tmp_path, monkeypatch)
    earlier = f"{LANDLORD} owes me the site photos by 3."
    said = "If they have not sent them by 3, chase them yourself."
    _turn(ledger, "t-0", earlier, reply="Noted.")
    _turn(ledger, "t-1", said)
    router = _ByTurn({said: [_item(f"Ask {LANDLORD} where the site photos are", counterpart=LANDLORD,
                                   metadata=_check_in(LANDLORD, asked=said, topic="the site photos"))]}, keep=True)
    assert await extractor.process_one(router) is True and await extractor.process_one(router) is True
    (messages, _), = router.reviews()
    payload = json.loads(messages[1]["content"])
    assert payload["message"] == said and earlier in payload["earlier_conversation"]
    row, = _rows(store)
    assert row["metadata"]["kind"] == "check_in" and row["metadata"]["grant"] == "owner"
    assert "earlier" in " ".join(extract.REQUEST_REVIEW_SYSTEM.split())


@pytest.mark.asyncio
async def test_a_chase_about_the_listed_item_it_refers_to_is_granted_and_kept_beside_it(tmp_path, monkeypatch):
    """The open item "p-05 sends the site photos" is what "they" refers to: the chase is reviewed with it,
    granted, and kept beside the promise (a message to p-05 is not the promise's own word)."""
    store, ledger, extractor = _setup(tmp_path, monkeypatch)
    promise = store.create(person_id=OWNER, description=f"{LANDLORD} sends the site photos", priority=70,
                           due_at=(_now() + timedelta(hours=2)).isoformat(), source_type="cognition",
                           metadata={"counterpart": "owner", "obligor": LANDLORD})
    said = "If they have not arrived by then, chase them yourself."
    _turn(ledger, "t-1", said)
    router = _Router([_item(f"Chase {LANDLORD} for the site photos", counterpart=LANDLORD,
                            metadata=_check_in(LANDLORD, asked=said, topic="the site photos"))], keep=True)
    assert await extractor.process_one(router) is True
    (messages, _), = router.reviews()
    assert f"{LANDLORD} sends the site photos" in json.dumps(json.loads(messages[1]["content"])["open_items"])
    kinds = {row["id"]: (row["metadata"] or {}).get("kind") for row in _rows(store)}
    assert kinds.pop(promise["id"]) is None and list(kinds.values()) == ["check_in"]


def test_a_recipient_named_nowhere_the_words_could_refer_to_is_not_reviewed():
    said = "If they have not sent them by 3, chase them yourself."
    metadata = _check_in("p-99", asked=said)
    assert extract.request_problem(metadata, owner_text=said, referents=f"{LANDLORD} owes me the photos") == \
        "recipient_not_named"
    assert extract.request_problem(_check_in(LANDLORD, asked=said), owner_text=said,
                                   referents=f"{LANDLORD} owes me the photos") is None


def test_a_deliverable_to_a_name_the_owner_goes_by_is_theirs_with_its_content(tmp_path):
    """"Email me the Q3 revenue number" with the owner's own name as counterpart is a deliverable to the
    owner, never a notice to a third party turned into a reminder without the figure."""
    store = CommitmentStore(db_path=tmp_path / "c.db")
    item = _item("Email Robin the Q3 revenue", counterpart="Robin", due_in=timedelta(minutes=2),
                 metadata={"kind": "deliverable", "content": "Q3 revenue was 4.2 million.", "channel_hint": "email"})
    item["source_type"] = "introspection"
    record_items([item], person_id=OWNER, commitment_store=store, existing=[], rejections=[], owner_id=OWNER,
                 owner_text="Email me the Q3 revenue number.", turn_time=_now(), owner_names=["Robin"])
    row, = _rows(store)
    assert row["metadata"]["kind"] == "deliverable" and row["metadata"]["content"] == "Q3 revenue was 4.2 million."


async def test_a_capture_cancelled_during_the_request_review_hands_the_job_back(tmp_path, monkeypatch):
    """A drain's budget runs out while the review pass is judging the owner's request: the job is handed back
    at once, uncharged and unleased, so the owner's next capture is not held behind it."""
    import asyncio
    from contextlib import closing
    store, ledger, extractor = _setup(tmp_path, monkeypatch)
    said = f"{LANDLORD} owes me the signed lease. If {LANDLORD} has not sent it by 5, chase them yourself."
    asked = f"If {LANDLORD} has not sent it by 5, chase them yourself."
    _turn(ledger, "t-1", said)
    item = _item(f"Chase {LANDLORD} for the signed lease", counterpart=LANDLORD,
                 metadata=_check_in(LANDLORD, asked=asked, topic="the signed lease"))

    class _StuckReview(_Router):
        async def complete(self, messages, *, context=None, **kwargs):
            if (context or {}).get("task") == "source_claim_review":
                await asyncio.Event().wait()
            return await super().complete(messages, context=context, **kwargs)

    summary = await extractor.drain(_StuckReview([item], keep=True), budget_seconds=0.5)
    assert summary["recorded"] == 0
    with closing(ledger._connect()) as conn:
        job = dict(conn.execute("SELECT * FROM commitment_runs WHERE turn_id='t-1'").fetchone())
    assert (job["status"], job["attempts"], job["lease_until"]) == ("pending", 0, 0)
    assert await extractor.process_one(_Router([item], keep=True)) is True
    row, = _rows(store)
    assert row["metadata"]["grant"] == "owner"
