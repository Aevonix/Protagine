"""An obligation between two other people is neither captured nor turned into a word to the owner.

Capture records who owes an item (``obligor``) and the other party (``counterpart``). When both
are named third parties, and not the same one, the item is between other people: the owner does
not owe it, is not owed it and did not hand it to the assistant. Capture drops it where it records
new items, and the duty drive never reminds the owner of such a row (one stored before the guard,
or written by anything else). Every shape the owner does have a stake in still lands: a contact's
promise to the owner, a promise the owner relies on, the owner's own half of a split obligation,
a chase the owner handed to the assistant.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from protagine.commitments import extract
from protagine.commitments.extract import CommitmentExtractor, record_items
from protagine.commitments.parties import between_others
from protagine.commitments.store import CommitmentStore
from protagine.mind.drives import DriveInputs, duty

OWNER = "owner-1"
NOW = datetime(2026, 3, 2, 15, 0, tzinfo=timezone.utc)


def _iso(**delta) -> str:
    return (datetime.now(timezone.utc).replace(microsecond=0) + timedelta(**delta)).isoformat()


def _item(description, *, obligor=None, counterpart=None, metadata=None, due_at=None):
    return {"action": "create", "target": None, "description": description, "due_at": due_at or _iso(hours=1),
            "priority": 70, "source_type": "cognition", "metadata": metadata, "listed_due": None,
            "counterpart": counterpart, "obligor": obligor}


def _record(tmp_path, item, *, person=OWNER, **kwargs):
    store = CommitmentStore(db_path=tmp_path / "c.db")
    result = record_items([item], person_id=person, commitment_store=store, existing=[], rejections=[],
                          owner_id=OWNER, **kwargs)
    return store, result


# --- the rule itself ------------------------------------------------------------------------------

def test_two_different_named_third_parties_are_between_others_and_nothing_else_is():
    assert between_others("p-12", "p-13")
    assert between_others(" Kim ", "Dana Ruiz")
    for obligor, counterpart in (("p-12", "owner"), ("p-12", "me"), ("owner", "p-12"), ("assistant", "p-12"),
                                 ("p-12", "assistant"), ("p-12", None), ("p-12", ""), ("p-12", "null"),
                                 (None, "p-13"), ("p-12", "P-12 "), ("p-12", "them"), ("the owner", "p-13")):
        assert not between_others(obligor, counterpart), (obligor, counterpart)
    # A name the owner is known by is the owner; two spellings of one person are one party.
    assert not between_others("p-12", "Robin Vale", owner_names=["Robin Vale", OWNER])
    assert not between_others("p-12", OWNER, owner_names=[OWNER])
    assert not between_others("p-12", "Kim Lee", same=["p-12", "Kim Lee"])
    assert not between_others("Aide", "p-13", assistant_names=["Aide"])


# --- capture: the guard where new items are recorded ----------------------------------------------

def test_capture_does_not_record_an_obligation_between_two_other_people(tmp_path, caplog):
    with caplog.at_level(logging.DEBUG, logger="protagine.commitments.extract"):
        store, result = _record(tmp_path, _item("p-12 finishes the stock report for p-13", obligor="p-12",
                                                counterpart="p-13"))
    assert result["created"] == [] and result["between_others"] == 1
    assert store.list(status=["pending", "overdue"])["commitments"] == []
    assert any("between two other people" in record.getMessage() for record in caplog.records)


def test_a_contacts_own_promise_to_another_contact_is_not_the_owners_either(tmp_path):
    store, result = _record(tmp_path, _item("p-12 sends p-13 the slides", obligor="p-12", counterpart="p-13"),
                            person="p-12", speaker_names=["p-12", "Kim Lee"])
    assert result["created"] == [] and result["between_others"] == 1


@pytest.mark.parametrize("person, item, names", [
    # A contact's promise to the owner, on the contact's own turn.
    ("p-12", _item("p-12 sends the signed lease", obligor="p-12", counterpart="owner"), {}),
    # A promise the owner relies on, told on the owner's turn ("they promised me", "I am counting on it").
    (OWNER, _item("p-12 delivers the budget draft", obligor="p-12", counterpart="owner"), {}),
    (OWNER, _item("p-12 delivers the budget draft", obligor="p-12", counterpart="me"), {}),
    # The owner asked for a word if a contact's delivery does not turn up.
    (OWNER, _item("Tell the owner if p-12's figures are missing", obligor="owner", counterpart="p-12"), {}),
    # The second half of an obligation split between the owner and a contact.
    (OWNER, _item("Send p-12 the venue contract", obligor="owner", counterpart="p-12"), {}),
    # A chase the owner handed to the assistant.
    (OWNER, _item("Chase p-12 for the parcel receipt", obligor="assistant", counterpart="p-12",
                  metadata={"kind": "check_in", "recipient": "p-12", "topic": "the parcel receipt",
                            "grant": "owner"}), {}),
    # Someone's promise with no other party named.
    (OWNER, _item("p-12 files the audit checklist", obligor="p-12", counterpart=None), {}),
    # The owner named by the name the contact used for them.
    ("p-12", _item("p-12 brings Robin the spare keys", obligor="p-12", counterpart="Robin Vale"),
     {"owner_names": ["Robin Vale"]}),
    # One person under two spellings (the speaker's id and display name).
    ("p-12", _item("Kim Lee returns the reading list", obligor="Kim Lee", counterpart="p-12"),
     {"speaker_names": ["p-12", "Kim Lee"]}),
    # A message the owner asked the assistant to send is the assistant's work by its kind, whatever
    # the obligor field says.
    (OWNER, _item("Tell p-13 the booking lapses", obligor="p-12", counterpart="p-13",
                  metadata={"kind": "notice", "recipient": "p-13", "content": "The booking lapses tonight.",
                            "grant": "owner"}), {}),
])
def test_capture_still_records_every_obligation_the_owner_has_a_stake_in(tmp_path, person, item, names):
    store, result = _record(tmp_path, item, person=person, owner_text="The booking lapses tonight.", **names)
    assert len(result["created"]) == 1 and result["between_others"] == 0, result


class _Router:
    supports_function_routing = True

    def __init__(self, items):
        self.items, self.prompts = items, []

    def function_deadline_seconds(self, *, context=None):
        return 20

    async def complete(self, messages, *, context=None, **_):
        self.prompts.append(messages[1]["content"])
        return SimpleNamespace(content=json.dumps(self.items))


async def test_the_capture_job_knows_the_owner_by_the_names_the_contacts_store_holds(tmp_path, monkeypatch):
    """The job reads the owner's names (display name, handles) and the speaker's through the same
    alias lookup the prompt uses: a contact's promise to the owner by name lands, an obligation between
    the contact and someone else does not."""
    from protagine.turns.idempotency import TurnIdempotencyLedger
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    names = {OWNER: ["Robin Vale", "+15550199"], "p-12": ["Kim Lee"]}
    store = CommitmentStore(db_path=tmp_path / "c.db")
    ledger = TurnIdempotencyLedger(tmp_path / "ledger.db")
    extractor = CommitmentExtractor(ledger, lambda: store, aliases=lambda contact: names.get(contact, []))
    ledger.record_source("t-1", contact_id="p-12", session_id="c-12", messages=[
        {"role": "user", "content": "I will drop the spare keys with Robin by four, and Dana gets the slides "
                                    "from me tomorrow."},
        {"role": "assistant", "content": "Thanks, noted."}])
    router = _Router([_item("Kim Lee brings Robin the spare keys", obligor="Kim Lee", counterpart="Robin Vale"),
                      _item("p-12 sends Dana the slides", obligor="p-12", counterpart="Dana")])
    assert await extractor.process_one(router) is True
    rows = store.list(status=["pending", "overdue"])["commitments"]
    assert [row["description"] for row in rows] == ["Kim Lee brings Robin the spare keys"]


# --- the prompt says whom an item is owed to --------------------------------------------------------

def test_the_contract_owes_a_relied_on_promise_to_the_owner_and_names_the_other_party_between_others():
    system = extract.SYSTEM
    assert "waiting on or relies on" in system                 # a contact's promise the owner depends on
    assert "between two other people" in system                # names the other of the two, never "owner"
    # One example of work passing between other people, recorded as nothing.
    example = system.split("They said: p-03 ")[1].split("\n")
    assert example[1].startswith("[]") and "p-06" in example[0]


# --- the mind: no word to the owner about someone else's obligation --------------------------------

def _row(**fields):
    row = {"id": "c-9", "person_id": OWNER, "description": "p-12 finishes the stock report for p-13",
           "due_at": (NOW - timedelta(hours=1)).isoformat(), "status": "overdue", "priority": 70,
           "source_type": "cognition", "metadata": {"obligor": "p-12", "counterpart": "p-13"}}
    return {**row, **fields}


def test_the_duty_drive_never_reminds_the_owner_of_an_obligation_between_two_other_people():
    """A row stored before capture refused such items (or by any other writer) raises neither the
    overdue reminder nor a heads-up."""
    assert duty(DriveInputs(now=NOW, owner_id=OWNER, commitments=[_row()]))[1] == []
    ahead = _row(due_at=(NOW + timedelta(minutes=5)).isoformat(), status="pending",
                 metadata={"obligor": "p-12", "counterpart": "p-13", "lead_minutes": 10})
    assert duty(DriveInputs(now=NOW, owner_id=OWNER, commitments=[ahead]))[1] == []
    # The same row on a contact's lane: neither party is that contact.
    assert duty(DriveInputs(now=NOW, owner_id=OWNER, commitments=[_row(person_id="p-14")]))[1] == []


@pytest.mark.parametrize("fields, kind", [
    ({"metadata": {"obligor": "p-12", "counterpart": "owner"}}, "commitment_reminder"),
    ({"metadata": {"obligor": "p-12", "counterpart": OWNER}}, "commitment_reminder"),
    ({"metadata": {"obligor": "p-12"}}, "commitment_reminder"),
    ({"metadata": None}, "commitment_reminder"),
    ({"metadata": {"obligor": "owner", "counterpart": "p-12"}}, "commitment_reminder"),
    # A row the mind cannot tell apart from a contact's promise to the owner under another name: one
    # party is the row's own person, whose other names the mind does not hold.
    ({"person_id": "p-12", "metadata": {"obligor": "p-12", "counterpart": "Robin Vale"}}, "commitment_reminder"),
    ({"metadata": {"obligor": "assistant", "counterpart": "p-12"}}, "commitment_overdue"),
    ({"metadata": {"obligor": "p-12", "counterpart": "p-13", "kind": "deliverable", "content": "Here."}},
     "commitment_deliverable"),
])
def test_the_duty_drive_still_acts_on_every_row_the_owner_or_the_assistant_is_party_to(fields, kind):
    candidates = duty(DriveInputs(now=NOW, owner_id=OWNER, commitments=[_row(**fields)]))[1]
    assert [candidate.type for candidate in candidates] == [kind]
