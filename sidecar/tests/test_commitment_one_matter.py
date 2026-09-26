"""One matter is one item: a word the owner asks for about an item is that item's own word when it is the
same word, about the same object, to the same person.

A reminder the owner asks for about something the same turn records, or about an item already open, is
folded into that item rather than recorded beside it only when its object is the item's own, word for word:
with the reminder verb and the time said with it ("remind me at 5pm to ...", "remind me to ... at 17:00")
set aside, what remains is the item's wording (case, spacing and punctuation aside). The item's own word at
its deadline is then a reminder to the owner, that deadline is still ahead, and the reminder falls at the
deadline, before it (the item's heads-up, where it has none yet; on an open row of the owner's, written
compare-and-set against what was listed) or shortly after it at no time the owner named. Everything else
stays its own item: a paraphrase, another date, month, ordinal or id is another reminder (a duplicate word
is acceptable, a lost one is not), two obligations are never merged, a word the owner timed is never moved
or dropped, and a word never becomes a heads-up to a contact. A new item moves a listed one only with that
item's own wording.
"""

from __future__ import annotations

import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

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

def test_a_promise_and_the_same_word_asked_for_shortly_after_it_are_one_item(tmp_path):
    store = CommitmentStore(tmp_path / "c.db")
    result = _record(store, [
        _create("Send p-41 the floor plan", _at(11), counterpart="p-41"),
        _reminder("Remind me to send p-41 the floor plan", _at(15), counterpart="owner", obligor="assistant")])
    row, = _open(store)
    assert row["description"] == "Send p-41 the floor plan" and result["folded"] == 1
    assert "heads_up_at" not in (row["metadata"] or {})


def test_a_paraphrased_nudge_about_a_promise_is_its_own_reminder(tmp_path):
    """Strict folding: a nudge worded otherwise than the item is another reminder, never folded away."""
    store = CommitmentStore(tmp_path / "c.db")
    result = _record(store, [
        _create("Send p-41 the floor plan", _at(11), counterpart="p-41"),
        _reminder("Nudge the owner if the p-41 floor plan goes quiet", _at(15), counterpart="owner",
                  obligor="assistant")])
    assert result["folded"] == 0 and result["skipped_duplicates"] == 0 and len(_open(store)) == 2


def test_a_reminder_before_the_deadline_becomes_the_items_heads_up(tmp_path):
    store = CommitmentStore(tmp_path / "c.db")
    _record(store, [_reminder("Remind me to send p-41 the floor plan", _at(50), counterpart="p-41"),
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

def test_the_owner_restating_a_contacts_promise_in_other_words_is_a_reminder_of_their_own(tmp_path):
    """p-74 promised the reading list on their own turn; the owner restates it with "tell me if it
    passes", which the extractor files as a check-in. It is the owner's reminder; worded otherwise than
    the promise, it is kept as its own row (strict folding): both words go to the owner, none to p-74."""
    store = CommitmentStore(tmp_path / "c.db")
    promise = store.create(person_id="p-74", description="p-74 sends the reading list", due_at=_at(16),
                           source_type="cognition", allow_overdue=True,
                           metadata={"counterpart": "owner", "obligor": "p-74"})
    said = "I am depending on p-74 for the reading list; they said 15 minutes. If it passes, tell me."
    check_in = _create("Check on p-74 when the reading list deadline passes", _at(16), counterpart="p-74",
                       obligor="assistant", metadata={"kind": "check_in", "recipient": "p-74",
                                                      "topic": "the reading list", "grant": "owner"})
    result = _record(store, [check_in], existing=[promise], said=said)
    assert len(result["created"]) == 1 and result["folded"] == 0 and result["skipped_duplicates"] == 0
    assert len(_open(store)) == 1 and len(_open(store, "p-74")) == 1
    later = T0 + timedelta(minutes=17)
    rows = _open(store) + _open(store, "p-74")
    candidates = duty(DriveInputs(now=later, owner_id=OWNER, commitments=rows))[1]
    assert [(c.type, c.recipient) for c in candidates] == [("commitment_reminder", OWNER)] * 2


def test_an_earlier_reminder_about_an_open_item_is_its_heads_up_written_against_the_listing(tmp_path):
    store = CommitmentStore(tmp_path / "c.db")
    item = store.create(person_id=OWNER, description="Send p-41 the floor plan", due_at=_at(90),
                        source_type="cognition", metadata={"counterpart": "p-41", "obligor": "owner"})
    result = _record(store, [_reminder("Remind me to send p-41 the floor plan", _at(60), counterpart="p-41")],
                     existing=[item])
    assert result["created"] == [] and result["updated"] == [item["id"]]
    assert datetime.fromisoformat(store.get(item["id"])["metadata"]["heads_up_at"]) == T0 + timedelta(minutes=60)
    # A row the owner changed since it was listed is left as they left it: a conflict to rerun.
    store.update(item["id"], due_at=_at(30))
    again = _record(store, [_reminder("Remind me to send p-41 the floor plan", _at(20), counterpart="p-41")],
                    existing=[item])
    assert again["conflicts"] == 1 and store.get(item["id"])["due_at"].startswith(_at(30)[:16])


def test_an_unconfirmed_message_becomes_a_reminder_kind(tmp_path):
    converted = owner_reminder(_create("Chase p-41", _at(60), counterpart="p-41", obligor="assistant"),
                               {"kind": "check_in", "recipient": "p-41", "topic": "x", "grant": "owner"})
    assert converted["metadata"] == {"kind": "reminder"} and converted["obligor"] == "owner"


def test_the_contract_says_a_word_about_an_item_is_that_items_own():
    system = " ".join(extract.SYSTEM.split())
    assert "never a second item" in system


def test_a_misread_check_in_beside_the_promise_it_is_about_is_the_owners_own_reminder(tmp_path):
    """One owner turn: p-52's promise and "tell me if it has not come", misread as a chase of p-52. The
    chase is the owner's reminder (the words never ask to contact p-52); worded otherwise than the promise,
    it is kept as its own row (strict folding): both words go to the owner when it falls due, none to p-52."""
    store = CommitmentStore(tmp_path / "c.db")
    said = "p-52 will send the tile samples within 20 minutes; tell me if they have not come by then."
    items = [_create("p-52 sends the tile samples", _at(20), counterpart="owner", obligor="p-52"),
             _create("Chase p-52 for the tile samples", _at(20), counterpart="p-52", obligor="assistant",
                     metadata={"kind": "check_in", "recipient": "p-52", "topic": "the tile samples", "grant": "owner",
                               "asked": said})]
    result = _record(store, items, said=said)
    assert sorted(row["description"] for row in _open(store)) == ["Chase p-52 for the tile samples",
                                                                   "p-52 sends the tile samples"]
    assert result["folded"] == 0 and result["owner_reminders"] == 1
    assert all((row["metadata"] or {}).get("kind") in (None, "reminder") for row in _open(store))
    later = T0 + timedelta(minutes=21)
    candidates = duty(DriveInputs(now=later, owner_id=OWNER, commitments=_open(store)))[1]
    assert [(c.type, c.recipient) for c in candidates] == [("commitment_reminder", OWNER)] * 2


def test_the_owner_restating_an_open_item_with_a_new_time_moves_it(tmp_path):
    """Restated as a new item instead of a reschedule, the listed row is moved compare-and-set: one row,
    at the time the owner now gave; the same time again changes nothing."""
    store = CommitmentStore(tmp_path / "c.db")
    item = store.create(person_id=OWNER, description="Send p-41 the floor plan", due_at=_at(60),
                        source_type="cognition", metadata={"counterpart": "p-41", "obligor": "owner"})
    same = _record(store, [_create("Send p-41 the floor plan", _at(60), counterpart="p-41")], existing=[item])
    assert same["created"] == [] and same["updated"] == [] and same["skipped_duplicates"] == 1
    moved = _record(store, [_create("Send p-41 the floor plan", _at(120), counterpart="p-41")], existing=[item])
    assert moved["created"] == [] and moved["updated"] == [item["id"]]
    row, = _open(store)
    assert datetime.fromisoformat(row["due_at"]) == T0 + timedelta(minutes=120)
    assert row["metadata"]["reschedule"]["by"] == "conversation"
    # Against a stale listing (the row moved since), it is a conflict to rerun, never an overwrite.
    stale = _record(store, [_create("Send p-41 the floor plan", _at(30), counterpart="p-41")], existing=[item])
    assert stale["conflicts"] == 1 and datetime.fromisoformat(store.get(item["id"])["due_at"]) == T0 + timedelta(minutes=120)


# --- a word is folded only where it is the same word to the same person ------------------------------------

def _candidates(store, minutes, *, settled=(), person=None):
    rows = store.list(status=["pending", "overdue"], **({"person_id": person} if person else {}))["commitments"]
    return duty(DriveInputs(now=T0 + timedelta(minutes=minutes), owner_id=OWNER, commitments=rows,
                            settled=set(settled)))[1]


def test_a_reminder_that_adds_a_matter_of_its_own_is_never_the_items_heads_up(tmp_path):
    """"I owe p-41 the floor plan by 5; remind me at 4 to pay p-41 the floor plan deposit": the deposit is a
    second obligation, so both are kept and the 4 o'clock word is about the deposit."""
    store = CommitmentStore(tmp_path / "c.db")
    said = "I owe p-41 the floor plan by 5. Also remind me at 4 to pay p-41 the floor plan deposit."
    result = _record(store, [_create("Send p-41 the floor plan", _at(300), counterpart="p-41"),
                             _reminder("Remind me to pay p-41 the floor plan deposit", _at(240), counterpart="p-41")],
                     said=said)
    assert result["folded"] == 0 and len(result["created"]) == 2
    plan = next(row for row in _open(store) if row["description"] == "Send p-41 the floor plan")
    assert "heads_up_at" not in (plan["metadata"] or {})
    assert [c.title for c in _candidates(store, 241)] == ["Overdue: Remind me to pay p-41 the floor plan deposit"]


def test_a_reminder_never_displaces_a_heads_up_the_item_already_has(tmp_path):
    """The item warns at 15:30; a word at 13:00 about the same invoice is a second word the owner asked
    for, kept as its own item, never dropped."""
    store = CommitmentStore(tmp_path / "c.db")
    said = "The invoice has to reach Kim by 4pm, give me a heads-up at half three; and remind me at 1 about it."
    result = _record(store, [_create("Send Kim the invoice", _at(240), counterpart="Kim",
                                     metadata={"heads_up_at": _at(210)}),
                             _reminder("Remind me about the invoice for Kim", _at(60), counterpart="Kim")],
                     said=said)
    assert result["folded"] == 0 and len(result["created"]) == 2
    assert [c.type for c in _candidates(store, 61)] == ["commitment_reminder"]
    assert [c.type for c in _candidates(store, 211)] == ["commitment_reminder", "commitment_due_soon"]


def test_a_second_word_the_owner_times_after_an_overdue_item_is_recorded(tmp_path):
    """p-05's photos were due at +0 and the owner was reminded then. At +30 the owner asks for a word again
    at 1:30: a new reminder, delivered at +90 though the first one's key is settled."""
    store = CommitmentStore(tmp_path / "c.db")
    promise = store.create(person_id=OWNER, description="p-05 sends the site photos", due_at=_at(0),
                           source_type="cognition", allow_overdue=True,
                           metadata={"counterpart": "owner", "obligor": "p-05"})
    first = _candidates(store, 1)
    assert [c.type for c in first] == ["commitment_reminder"]
    said = "If the site photos from p-05 still are not here by 1:30, remind me again."
    again = {**_reminder("Remind me to chase p-05 about the site photos", _at(90), counterpart="p-05"),
             "due_text": "by 1:30"}
    result = record_items([again], person_id=OWNER, commitment_store=store, existing=[store.get(promise["id"])],
                          rejections=[], turn_id="t-2", owner_id=OWNER, owner_text=said,
                          turn_time=T0 + timedelta(minutes=30))
    assert result["folded"] == 0 and len(result["created"]) == 1
    later = _candidates(store, 91, settled={first[0].dedup_key})
    assert [(c.type, c.recipient) for c in later] == [("commitment_reminder", OWNER)]
    assert "site photos" in later[0].title


def test_a_word_the_owner_times_after_the_deadline_keeps_its_time(tmp_path):
    """"Send Kim the invoice by 4; remind me at 5:30 to check Kim got it": the 5:30 word is the owner's own
    time, never moved to 4."""
    store = CommitmentStore(tmp_path / "c.db")
    said = "I have to send Kim the invoice by 4. Remind me at 5:30 to check Kim got the invoice."
    check = {**_reminder("Check Kim got the invoice", _at(90), counterpart="Kim"), "due_text": "at 5:30"}
    result = _record(store, [_create("Send Kim the invoice", _at(60), counterpart="Kim"), check],
                     said=said)
    assert result["folded"] == 0 and len(result["created"]) == 2
    assert sorted(c.title for c in _candidates(store, 91)) == ["Overdue: Check Kim got the invoice",
                                                               "Overdue: Send Kim the invoice"]


def test_a_reminder_never_folds_into_work_the_assistant_owes(tmp_path):
    """"Draft the budget summary for p-74 by 3, and remind me at 3:30 to check p-74 got it": the draft is a
    task at its deadline, not a word to the owner, so the owner's 3:30 word stays its own."""
    store = CommitmentStore(tmp_path / "c.db")
    said = "Draft the budget summary for p-74 by 3, and remind me at 3:30 to check p-74 got the budget summary."
    result = _record(store, [_create("Draft the budget summary for p-74", _at(60), counterpart="p-74",
                                     obligor="assistant"),
                             _reminder("Check that p-74 has the budget summary now", _at(90), counterpart="p-74")],
                     said=said)
    assert result["folded"] == 0 and len(result["created"]) == 2
    kinds = sorted((c.type, c.recipient) for c in _candidates(store, 91))
    assert kinds == [("commitment_overdue", OWNER), ("commitment_reminder", OWNER)]


def test_a_contacts_turn_never_folds_a_word_into_a_heads_up_to_them(tmp_path):
    """On p-07's turn, a reminder meant for the owner never becomes a heads-up on p-07's own row (which
    would message p-07, whom nobody asked the mind to message)."""
    store = CommitmentStore(tmp_path / "c.db")
    row = store.create(person_id="p-07", description="p-07 sends the signed form", due_at=_at(300),
                       source_type="cognition", metadata={"counterpart": "owner", "obligor": "p-07"})
    said = "Tell the owner the signed form will be late, and remind them about my signed form at 4."
    item = _reminder("Remind the owner about p-07's signed form", _at(240), counterpart="owner", obligor="assistant")
    result = record_items([item], person_id="p-07", commitment_store=store, existing=[row], rejections=[],
                          turn_id="t-c", owner_id=OWNER, owner_text=said, turn_time=T0, speaker_names=["p-07"])
    assert result["folded"] == 0 and result["updated"] == []
    assert "heads_up_at" not in (store.get(row["id"])["metadata"] or {})
    assert [c.recipient for c in _candidates(store, 241)] == [OWNER]


def test_a_similar_new_item_never_moves_a_different_listed_one(tmp_path):
    """The Q4 report is not the Q3 report restated; the lease renewal form is not the lease. The listed
    deadline stays as it was (the similar new item is the duplicate check's to judge, as before)."""
    store = CommitmentStore(tmp_path / "c.db")
    q3 = store.create(person_id=OWNER, description="Send p-05 the Q3 report", due_at=_at(60),
                      source_type="cognition", metadata={"counterpart": "p-05", "obligor": "owner"})
    lease = store.create(person_id=OWNER, description="Send Dana the lease", due_at=_at(60),
                         source_type="cognition", metadata={"counterpart": "Dana", "obligor": "owner"})
    listed = [q3, lease]
    for description, counterpart in (("Send p-05 the Q4 report", "p-05"), ("Send Dana the lease renewal form", "Dana")):
        result = _record(store, [_create(description, _at(60 * 24 * 5), counterpart=counterpart)], existing=listed)
        assert result["updated"] == []
    assert {row["id"]: row["due_at"][:16] for row in _open(store) if row["id"] in {q3["id"], lease["id"]}} == {
        q3["id"]: _at(60)[:16], lease["id"]: _at(60)[:16]}
    assert [c.type for c in _candidates(store, 61) if c.source_id == q3["id"]] == ["commitment_reminder"]


@pytest.mark.parametrize("item, word", [
    ("Send p-41 invoice 123", "Remind me to send p-41 invoice 456"),
    ("Send p-05 the Q3 report", "Remind me about the p-05 Q4 report"),
    ("File form W-2 for p-05", "Remind me to file form W-4 for p-05"),
])
def test_a_reminder_about_another_numbered_document_is_its_own_item(tmp_path, item, word):
    """Two identifiers are two matters: a reminder is never folded into an item it names another number or id
    of, in the same turn or against a listed row."""
    counterpart = "p-41" if "p-41" in item else "p-05"
    store = CommitmentStore(tmp_path / "c.db")
    result = _record(store, [_create(item, _at(60), counterpart=counterpart),
                             _reminder(word, _at(60), counterpart=counterpart)])
    assert result["folded"] == 0 and sorted(row["description"] for row in _open(store)) == sorted([item, word])
    listed_store = CommitmentStore(tmp_path / "listed.db")
    row = listed_store.create(person_id=OWNER, description=item, due_at=_at(90), source_type="cognition",
                              metadata={"counterpart": counterpart, "obligor": "owner"})
    result = _record(listed_store, [_reminder(word, _at(60), counterpart=counterpart)], existing=[row])
    assert result["folded"] == 0 and len(_open(listed_store)) == 2


def test_a_reminder_naming_the_same_number_still_folds(tmp_path):
    store = CommitmentStore(tmp_path / "c.db")
    result = _record(store, [_create("Send p-41 invoice 123", _at(60), counterpart="p-41"),
                             _reminder("Remind me to send p-41 invoice 123", _at(60), counterpart="p-41")])
    row, = _open(store)
    assert result["folded"] == 1 and row["description"] == "Send p-41 invoice 123"


# --- round 2: identifiers kept whole, times read as times, a reminder never dropped ------------------------

DISTINCT = [
    ("Send p-41 invoice AB_12", "Remind me to send p-41 invoice CD_12"),
    ("Send p-41 invoice A", "Remind me to send p-41 invoice B"),
    ("Send p-41 invoice AB-12", "Remind me to send p-41 invoice AB-13"),
    ("Send p-41 invoice 12", "Remind me to send p-41 invoice 123"),
    ("Send p-41 the plan A draft", "Remind me to send p-41 the plan B draft"),
    ("Send p-41 version 2.1 of the spec", "Remind me to send p-41 version 2.2 of the spec"),
    ("Send p-41 the room X key", "Remind me to send p-41 the room Y key"),
    ("Send p-41 report_v2", "Remind me to send p-41 report_v3"),
    ("Send p-41 form A", "Remind me to send p-41 form a-1"),
    ("Send p-41 the Q3 report", "Remind me to send p-41 the Q3b report"),
]


@pytest.mark.parametrize("item, word", DISTINCT)
def test_a_reminder_naming_another_identifier_is_its_own_item(tmp_path, item, word):
    """Re-check F3: underscores, hyphens, dots, mixed alphanumerics and a single letter after an identifier
    noun are one identifier each, compared whole: two documents are two items, in the turn and listed."""
    store = CommitmentStore(tmp_path / "c.db")
    result = _record(store, [_create(item, _at(60), counterpart="p-41"), _reminder(word, _at(60), counterpart="p-41")])
    assert result["folded"] == 0 and result["skipped_duplicates"] == 0
    assert sorted(row["description"] for row in _open(store)) == sorted([item, word])
    listed_store = CommitmentStore(tmp_path / "listed.db")
    row = listed_store.create(person_id=OWNER, description=item, due_at=_at(90), source_type="cognition",
                              metadata={"counterpart": "p-41", "obligor": "owner"})
    result = _record(listed_store, [_reminder(word, _at(60), counterpart="p-41")], existing=[row])
    assert result["folded"] == 0 and result["skipped_duplicates"] == 0 and len(_open(listed_store)) == 2


@pytest.mark.parametrize("item, word", [
    ("Send p-41 invoice AB_12", "Remind me to send p-41 invoice AB_12"),
    ("Send p-41 invoice A", "Remind me to send p-41 invoice A"),
    ("Send p-41 version 2.1 of the spec", "Remind me to send p-41 version 2.1 of the spec"),
    ("Send p-41 the floor plan.", "remind me to SEND p-41  the floor plan"),
])
def test_a_reminder_naming_the_same_identifier_folds(tmp_path, item, word):
    store = CommitmentStore(tmp_path / "c.db")
    result = _record(store, [_create(item, _at(60), counterpart="p-41"), _reminder(word, _at(60), counterpart="p-41")])
    row, = _open(store)
    assert result["folded"] == 1 and row["description"] == item


TIMED = ["at 17:00", "at 5pm", "at 5 pm", "at 5:00 p.m.", "by 17:00", "at 17h00", "at five", "tomorrow at 9",
         "on Friday at 4:30pm", "on 2026-10-02 at 16:00", "on Oct 2 at 4pm", "on 2/10 at 16:00", "in 2 hours",
         "at noon", "this evening", "before 17:00 tomorrow"]


@pytest.mark.parametrize("lead", [False, True])
@pytest.mark.parametrize("when", TIMED)
def test_a_reminder_differing_only_by_a_time_is_the_items_heads_up(tmp_path, when, lead):
    """Re-check new P1: "Send p-41 the floor plan" due 18:00 and "Remind me to send p-41 the floor plan at
    17:00" (or "Remind me at 17:00 to send p-41 the floor plan") due 17:00: the time said with the reminder
    verb is set aside, the object is the item's word for word, and the reminder is the item's heads-up."""
    word = (f"Remind me {when} to send p-41 the floor plan" if lead
            else f"Remind me to send p-41 the floor plan {when}")
    store = CommitmentStore(tmp_path / "c.db")
    result = _record(store, [_create("Send p-41 the floor plan", _at(120), counterpart="p-41"),
                             _reminder(word, _at(60), counterpart="p-41")])
    row, = _open(store)
    assert result["folded"] == 1 and result["skipped_duplicates"] == 0
    assert datetime.fromisoformat(row["metadata"]["heads_up_at"]) == T0 + timedelta(minutes=60)
    assert [c.type for c in _candidates(store, 61)] == ["commitment_due_soon"]
    listed_store = CommitmentStore(tmp_path / "listed.db")
    item = listed_store.create(person_id=OWNER, description="Send p-41 the floor plan", due_at=_at(120),
                               source_type="cognition", metadata={"counterpart": "p-41", "obligor": "owner"})
    result = _record(listed_store, [_reminder(word, _at(60), counterpart="p-41")], existing=[item])
    assert result["folded"] == 1 and len(_open(listed_store)) == 1
    assert [c.type for c in _candidates(listed_store, 61)] == ["commitment_due_soon"]


@pytest.mark.parametrize("item, word, heads_up", [
    ("Send p-41 the floor plan", "Remind me to send p-41 the floor plan at 17:00", True),     # heads-up taken
    ("Send p-41 the floor plan", "Remind me to send p-41 the floor plan at 19:00", False),    # after, own time
    ("Send p-41 invoice 12", "Remind me to send p-41 invoice 123 at 17:00", False),
    ("Send p-41 the floor plan", "Remind me about the p-41 floor plan", True),
])
def test_a_reminder_that_does_not_fold_is_never_dropped_as_a_duplicate(tmp_path, item, word, heads_up):
    """When the fold is refused (the heads-up is taken, the word falls after the deadline at a time the owner
    named, another identifier), the reminder is its own item: never counted as a duplicate of the item."""
    store = CommitmentStore(tmp_path / "c.db")
    due = _at(240) if "19:00" in word else _at(60)
    result = _record(store, [_create(item, _at(120), counterpart="p-41",
                                     metadata={"heads_up_at": _at(90)} if heads_up else None),
                             _reminder(word, due, counterpart="p-41")], said=f"{item}. {word}.")
    assert result["folded"] == 0 and result["skipped_duplicates"] == 0
    assert sorted(row["description"] for row in _open(store)) == sorted([item, word])


def test_the_same_reminder_said_twice_at_the_same_time_is_one_item(tmp_path):
    store = CommitmentStore(tmp_path / "c.db")
    first = _record(store, [_reminder("Remind me to call the bank at 5pm", _at(60))])
    again = _record(store, [_reminder("Remind me to call the bank at 5pm", _at(60))], existing=_open(store))
    assert len(first["created"]) == 1 and again["created"] == [] and len(_open(store)) == 1


# -- round 3: only the word's own time is set aside; a month, a date or an ordinal naming the object is kept ----

# Object phrases that differ only by a month, a date, a weekday or an ordinal: two documents, never one.
DATED_PAIRS = [
    ("May report", "June report"), ("the March figures", "the April figures"),
    ("invoice 2026-10-02", "invoice 2026-10-03"), ("the Oct 2 memo", "the Oct 3 memo"),
    ("the 2/10 statement", "the 3/10 statement"), ("the Monday rota", "the Tuesday rota"),
    ("the first draft", "the second draft"), ("the 2nd draft", "the 3rd draft"),
    ("the report for May", "the report for June"), ("the last report", "the next report"),
    ("the 5pm slides", "the 6pm slides"), ("the 2 October invoice", "the 3 October invoice"),
    ("the September statement", "the October statement"), ("the Q3 May pack", "the Q3 June pack"),
]
OWN_TIMES = ["", "at 17:00", "at 5pm", "tomorrow at 9", "on Friday at 4:30pm", "on 2026-10-02 at 16:00",
             "on Oct 2 at 4pm", "in 2 hours", "at noon", "this evening", "before 17:00 tomorrow"]


def _distinct(tmp, item, word, due_word):
    store = CommitmentStore(tmp / "c.db")
    result = _record(store, [_create(item, _at(120), counterpart="p-41"),
                             _reminder(word, due_word, counterpart="p-41")])
    assert result["folded"] == 0 and result["skipped_duplicates"] == 0, (item, word)
    assert sorted(row["description"] for row in _open(store)) == sorted([item, word]), (item, word)
    listed_store = CommitmentStore(tmp / "listed.db")
    row = listed_store.create(person_id=OWNER, description=item, due_at=_at(120), source_type="cognition",
                              metadata={"counterpart": "p-41", "obligor": "owner"})
    result = _record(listed_store, [_reminder(word, due_word, counterpart="p-41")], existing=[row])
    assert result["folded"] == 0 and result["skipped_duplicates"] == 0 and len(_open(listed_store)) == 2, (item, word)


def test_the_reviewers_may_and_june_reports_and_invoice_dates_are_two_items(tmp_path):
    """Re-check round 2, item 3: "Send p-41 May report" and "Remind me to send p-41 June report" at the same
    deadline, and invoices 2026-10-02 and 2026-10-03, are two documents: two rows."""
    for index, (item, word) in enumerate([("Send p-41 May report", "Remind me to send p-41 June report"),
                                          ("Send p-41 invoice 2026-10-02", "Remind me to send p-41 invoice 2026-10-03")]):
        directory = tmp_path / str(index)
        directory.mkdir()
        store = CommitmentStore(directory / "c.db")
        result = _record(store, [_create(item, _at(60), counterpart="p-41"), _reminder(word, _at(60), counterpart="p-41")])
        assert result["folded"] == 0 and len(_open(store)) == 2, (item, word)


@settings(max_examples=60, deadline=None)
@given(pair=st.sampled_from(DATED_PAIRS), when=st.sampled_from(OWN_TIMES), swap=st.booleans(),
       minutes=st.sampled_from([60, 120]))
def test_a_month_date_weekday_or_ordinal_naming_the_object_keeps_two_items(pair, when, swap, minutes):
    """Property: whatever time the reminder is for (at the deadline or before it), and whichever side names
    which, two objects that differ by a month, a date, a weekday or an ordinal stay two items."""
    first, second = pair[::-1] if swap else pair
    with tempfile.TemporaryDirectory() as directory:
        _distinct(Path(directory), f"Send p-41 {first}", f"Remind me to send p-41 {second} {when}".strip(), _at(minutes))


@settings(max_examples=40, deadline=None)
@given(obj=st.sampled_from([side for pair in DATED_PAIRS for side in pair]),
       when=st.sampled_from([time for time in OWN_TIMES if time]))
def test_the_same_dated_object_with_the_words_own_time_is_the_items_heads_up(obj, when):
    """Property: the same object, dates and months included, with the reminder's own time phrase set aside, is
    the item's heads-up (one row)."""
    with tempfile.TemporaryDirectory() as directory:
        store = CommitmentStore(Path(directory) / "c.db")
        result = _record(store, [_create(f"Send p-41 {obj}", _at(120), counterpart="p-41"),
                                 _reminder(f"Remind me to send p-41 {obj} {when}", _at(60), counterpart="p-41")])
        assert result["folded"] == 1 and len(_open(store)) == 1, (obj, when)


# -- round 4: folding is strict; only the reminder's own verb and time are set aside, the object must be identical --

def test_the_reviewers_invoice_due_dates_and_inflation_months_are_two_items(tmp_path):
    """Re-check round 3, item 3: "Send p-41 the invoice due 2026-10-02" and "Remind me to send p-41 the invoice
    due 2026-10-03", and the reports on March and April inflation, are two documents: two rows at the same
    deadline and before it, in the turn and against a listed row, never folded and never skipped as a duplicate."""
    pairs = [("Send p-41 the invoice due 2026-10-02", "Remind me to send p-41 the invoice due 2026-10-03"),
             ("Send p-41 the report on March inflation", "Remind me to send p-41 the report on April inflation")]
    for index, (item, word) in enumerate(pairs):
        for minutes in (120, 60):
            directory = tmp_path / f"{index}-{minutes}"
            directory.mkdir()
            _distinct(directory, item, word, _at(minutes))


_FILLERS = {
    "date": st.tuples(st.sampled_from(["%Y-%m-%d", "%b %d", "%d %B", "%d/%m", "%B %d, %Y"]),
                      st.dates(min_value=datetime(2025, 1, 1).date(), max_value=datetime(2028, 12, 31).date()))
              .map(lambda pair: pair[1].strftime(pair[0])),
    "month": st.sampled_from(["January", "February", "March", "April", "May", "June", "July", "August",
                              "September", "October", "November", "December", "Q1", "Q2", "Q3", "Q4"]),
    "ordinal": st.sampled_from(["1st", "2nd", "3rd", "4th", "11th", "22nd", "first", "second", "third",
                                "fourth", "last", "next", "final"]),
    "id": st.one_of(st.integers(min_value=0, max_value=99999).map(str),
                    st.tuples(st.sampled_from(["AB", "INV", "x", "Q", "rev"]), st.sampled_from(["-", "_", ".", "#", ""]),
                              st.integers(min_value=0, max_value=999)).map(lambda t: f"{t[0]}{t[1]}{t[2]}")),
}
_SLOTS = ["the invoice due {}", "the report on {} inflation", "the {} report", "invoice {}", "the report for {}",
          "the report on {}", "the minutes of the {} meeting", "the {} draft of the lease"]


@st.composite
def _two_objects(draw):
    kind = draw(st.sampled_from(sorted(_FILLERS)))
    first, second = draw(st.lists(_FILLERS[kind], min_size=2, max_size=2,
                                  unique_by=lambda text: " ".join(re.findall(r"[^\W_]+", text.casefold()))))
    slot = draw(st.sampled_from(_SLOTS))
    return slot.format(first), slot.format(second)


@settings(max_examples=80, deadline=None)
@given(objects=_two_objects(), when=st.sampled_from(OWN_TIMES), lead=st.booleans(),
       minutes=st.sampled_from([60, 120]))
def test_a_reminder_whose_object_differs_by_a_date_month_ordinal_or_id_never_folds(objects, when, lead, minutes):
    """Property: an object that differs from the item's by a date, a month, an ordinal or an id, wherever it
    stands in the wording and whatever time the reminder carries (before its object or after it), is another
    item: never folded, never skipped as a duplicate, in the turn and against a listed row."""
    item_object, word_object = objects
    if lead and when:
        word = f"Remind me {when} to send p-41 {word_object}"
    else:
        word = f"Remind me to send p-41 {word_object} {when}".strip()
    with tempfile.TemporaryDirectory() as directory:
        _distinct(Path(directory), f"Send p-41 {item_object}", word, _at(minutes))


@settings(max_examples=40, deadline=None)
@given(objects=_two_objects(), when=st.sampled_from([time for time in OWN_TIMES if time]), lead=st.booleans())
def test_the_identical_object_with_the_reminders_own_time_is_the_items_heads_up(objects, when, lead):
    """Property: the item's object word for word, with the reminder verb and the time said with it set aside
    (before the object or after it), is the item's heads-up: one row."""
    obj = objects[0]
    word = f"Remind me {when} to send p-41 {obj}" if lead else f"Remind me to send p-41 {obj} {when}"
    with tempfile.TemporaryDirectory() as directory:
        store = CommitmentStore(Path(directory) / "c.db")
        result = _record(store, [_create(f"Send p-41 {obj}", _at(120), counterpart="p-41"),
                                 _reminder(word, _at(60), counterpart="p-41")])
        assert result["folded"] == 1 and len(_open(store)) == 1, (obj, word)
        row, = _open(store)
        assert datetime.fromisoformat(row["metadata"]["heads_up_at"]) == T0 + timedelta(minutes=60)


@pytest.mark.parametrize("item, word", [
    ("Send p-41 the floor plan", "Remind me to send p-41 the floor plan draft"),
    ("Send p-41 the floor plan draft", "Remind me to send p-41 the floor plan"),
    ("Send p-41 the floor plan", "Remind me to email p-41 the floor plan"),
    ("Send p-41 the floor plan", "Remind me about the p-41 floor plan"),
])
def test_a_reminder_with_any_other_object_wording_is_its_own_item(tmp_path, item, word):
    """Strict folding: one word more or less, another verb or another order is another reminder."""
    _distinct(tmp_path, item, word, _at(60))
