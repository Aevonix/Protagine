"""One matter is one item: a word the person asks for about an item is that item's own word.

A reminder or a nudge the owner asks for about something the same turn records, or about an item
already open, is folded into that item rather than recorded beside it: two rows for one matter
are two words at one time. A reminder due before the item's deadline becomes its heads-up (on an
open row, written compare-and-set against what was listed). Two substantive obligations are never
merged, and a reminder about another matter, or for much later, stays its own item.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from protagine.commitments import extract
from protagine.commitments.extract import owner_reminder, record_items
from protagine.commitments.store import CommitmentStore
from protagine.mind.drives import DriveInputs, duty

OWNER = "owner-3"
T0 = (datetime.now(timezone.utc) + timedelta(days=1)).replace(minute=0, second=0, microsecond=0)


def _at(minutes):
    return (T0 + timedelta(minutes=minutes)).isoformat()


def _create(description, due, *, metadata=None, counterpart=None, obligor="owner", priority=70):
    return {"action": "create", "target": None, "description": description, "due_at": due, "priority": priority,
            "source_type": "cognition", "metadata": metadata, "listed_due": None, "counterpart": counterpart,
            "obligor": obligor}


def _reminder(description, due, **kwargs):
    return _create(description, due, metadata={"kind": "reminder"}, **kwargs)


def _record(store, items, *, existing=(), person=OWNER, said="(the turn)"):
    return record_items(list(items), person_id=person, commitment_store=store, existing=list(existing),
                        rejections=[], turn_id="t-1", owner_id=OWNER, owner_text=said, turn_time=T0)


def _open(store, person=OWNER):
    return store.list(status=["pending", "overdue"], person_id=person)["commitments"]


# --- one turn -------------------------------------------------------------------------------------

def test_a_promise_and_the_nudge_asked_for_it_are_one_item(tmp_path):
    store = CommitmentStore(tmp_path / "c.db")
    result = _record(store, [
        _create("Send p-41 the floor plan", _at(11), counterpart="p-41"),
        _reminder("Nudge the owner if the p-41 floor plan goes quiet", _at(15), counterpart="owner",
                  obligor="assistant")])
    row, = _open(store)
    assert row["description"] == "Send p-41 the floor plan" and result["folded"] == 1
    assert "heads_up_at" not in (row["metadata"] or {})


def test_a_reminder_before_the_deadline_becomes_the_items_heads_up(tmp_path):
    store = CommitmentStore(tmp_path / "c.db")
    _record(store, [_reminder("Remind me about the floor plan for p-41", _at(50), counterpart="p-41"),
                    _create("Send p-41 the floor plan", _at(60), counterpart="p-41")])
    row, = _open(store)
    assert row["description"] == "Send p-41 the floor plan"
    assert datetime.fromisoformat(row["metadata"]["heads_up_at"]) == T0 + timedelta(minutes=50)


def test_two_obligations_to_one_person_are_never_merged(tmp_path):
    store = CommitmentStore(tmp_path / "c.db")
    _record(store, [_create("Send p-41 the floor plan draft", _at(60), counterpart="p-41"),
                    _create("Pay p-41 the floor plan deposit", _at(60), counterpart="p-41")])
    assert len(_open(store)) == 2


def test_a_reminder_about_another_matter_with_the_same_person_stays(tmp_path):
    store = CommitmentStore(tmp_path / "c.db")
    _record(store, [_create("Send p-41 the floor plan", _at(60), counterpart="p-41"),
                    _reminder("Remind me to call p-41 about dinner", _at(60), counterpart="p-41")])
    assert len(_open(store)) == 2


def test_a_reminder_for_much_later_is_its_own_request(tmp_path):
    store = CommitmentStore(tmp_path / "c.db")
    _record(store, [_create("Send p-41 the floor plan", _at(60), counterpart="p-41"),
                    _reminder("Remind me the floor plan for p-41 next week", _at(60 * 24 * 7), counterpart="p-41")])
    assert len(_open(store)) == 2


def test_a_word_for_the_owner_never_folds_into_a_message_for_someone_else(tmp_path):
    """A confirmed chase of the contact and the owner's own reminder are two people's words."""
    store = CommitmentStore(tmp_path / "c.db")
    said = "If p-41 has not sent the floor plan by one, chase them yourself, and remind me then too."
    chase = _create("Chase p-41 for the floor plan", _at(60), counterpart="p-41", obligor="assistant",
                    metadata={"kind": "check_in", "recipient": "p-41", "topic": "the floor plan", "grant": "owner",
                              "asked": "If p-41 has not sent the floor plan by one, chase them yourself",
                              "request_review": {"version": extract.REQUEST_REVIEW_VERSION, "keep": True,
                                                 "reason": "asked", "recipient": "p-41",
                                                 "quote": "If p-41 has not sent the floor plan by one, chase them "
                                                          "yourself", "model_id": "judge"}})
    _record(store, [chase, _reminder("Remind me about the p-41 floor plan", _at(60), counterpart="p-41")], said=said)
    assert sorted((row["metadata"] or {}).get("kind") for row in _open(store)) == ["check_in", "reminder"]


# --- an item already open --------------------------------------------------------------------------

def test_the_owner_restating_a_contacts_promise_is_not_a_second_item(tmp_path):
    """p-74 promised the reading list on their own turn; the owner restates it with "tell me if it
    passes", which the extractor files as a check-in. It is the owner's reminder, and the open promise
    already brings the owner that word when it falls due: no second row, and one reminder then."""
    store = CommitmentStore(tmp_path / "c.db")
    promise = store.create(person_id="p-74", description="p-74 sends the reading list", due_at=_at(16),
                           source_type="cognition", allow_overdue=True,
                           metadata={"counterpart": "owner", "obligor": "p-74"})
    said = "I am depending on p-74 for the reading list; they said 15 minutes. If it passes, tell me."
    check_in = _create("Check on p-74 when the reading list deadline passes", _at(16), counterpart="p-74",
                       obligor="assistant", metadata={"kind": "check_in", "recipient": "p-74",
                                                      "topic": "the reading list", "grant": "owner"})
    result = _record(store, [check_in], existing=[promise], said=said)
    assert result["created"] == [] and result["folded"] == 1
    assert _open(store) == [] and len(_open(store, "p-74")) == 1
    later = T0 + timedelta(minutes=17)
    candidates = duty(DriveInputs(now=later, owner_id=OWNER, commitments=_open(store, "p-74")))[1]
    assert [(c.type, c.recipient) for c in candidates] == [("commitment_reminder", OWNER)]


def test_an_earlier_reminder_about_an_open_item_is_its_heads_up_written_against_the_listing(tmp_path):
    store = CommitmentStore(tmp_path / "c.db")
    item = store.create(person_id=OWNER, description="Send p-41 the floor plan", due_at=_at(90),
                        source_type="cognition", metadata={"counterpart": "p-41", "obligor": "owner"})
    result = _record(store, [_reminder("Remind me about the p-41 floor plan", _at(60), counterpart="p-41")],
                     existing=[item])
    assert result["created"] == [] and result["updated"] == [item["id"]]
    assert datetime.fromisoformat(store.get(item["id"])["metadata"]["heads_up_at"]) == T0 + timedelta(minutes=60)
    # A row the owner changed since it was listed is left as they left it: a conflict to rerun.
    store.update(item["id"], due_at=_at(30))
    again = _record(store, [_reminder("Remind me about the p-41 floor plan", _at(20), counterpart="p-41")],
                    existing=[item])
    assert again["conflicts"] == 1 and store.get(item["id"])["due_at"].startswith(_at(30)[:16])


def test_an_unconfirmed_message_becomes_a_reminder_kind(tmp_path):
    converted = owner_reminder(_create("Chase p-41", _at(60), counterpart="p-41", obligor="assistant"),
                               {"kind": "check_in", "recipient": "p-41", "topic": "x", "grant": "owner"})
    assert converted["metadata"] == {"kind": "reminder"} and converted["obligor"] == "owner"


def test_the_contract_says_a_word_about_an_item_is_that_items_own():
    system = " ".join(extract.SYSTEM.split())
    assert "never a second item" in system


def test_a_misread_check_in_beside_the_promise_it_is_about_folds_into_it(tmp_path):
    """One owner turn: p-52's promise and "tell me if it has not come", misread as a chase of p-52. The
    chase is the owner's reminder (the words never ask to contact p-52), and that reminder is the
    promise's own word: one row, one reminder to the owner when it falls due."""
    store = CommitmentStore(tmp_path / "c.db")
    said = "p-52 will send the tile samples within 20 minutes; tell me if they have not come by then."
    items = [_create("p-52 sends the tile samples", _at(20), counterpart="owner", obligor="p-52"),
             _create("Chase p-52 for the tile samples", _at(20), counterpart="p-52", obligor="assistant",
                     metadata={"kind": "check_in", "recipient": "p-52", "topic": "the tile samples", "grant": "owner",
                               "asked": said})]
    result = _record(store, items, said=said)
    row, = _open(store)
    assert row["description"] == "p-52 sends the tile samples" and result["folded"] == 1
    assert result["owner_reminders"] == 1
    later = T0 + timedelta(minutes=21)
    candidates = duty(DriveInputs(now=later, owner_id=OWNER, commitments=_open(store)))[1]
    assert [(c.type, c.recipient) for c in candidates] == [("commitment_reminder", OWNER)]
