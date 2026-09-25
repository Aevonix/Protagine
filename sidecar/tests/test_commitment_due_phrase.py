"""The commitment line shows the person's own time phrase next to the converted date.

The owner said "on Tuesday at 09:15"; capture stored 2026-09-29T09:15, and the context offered only
that conversion, which the reply then echoed ("2026-09-29") where the owner's word was "Tuesday".
Capture now keeps the words the person used for the time (``metadata.due_text``, only when they are
the person's own), a reschedule replaces or clears them, and the Pending Commitments line shows them
beside the date.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from protagine.api.routers import host
from protagine.commitments import extract
from protagine.commitments.extract import ITEM_SCHEMA, record_items
from protagine.commitments.store import CommitmentStore

OWNER = "owner"


def _due(hours=24):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(microsecond=0).isoformat()


def _create(description, due, due_text):
    return {"action": "create", "target": None, "description": description, "due_at": due, "due_text": due_text,
            "priority": 70, "source_type": "cognition", "metadata": None, "listed_due": None,
            "counterpart": None, "obligor": "owner"}


def test_the_contract_asks_for_the_persons_own_time_words():
    assert "due_text" in ITEM_SCHEMA["properties"] and "due_text" in ITEM_SCHEMA["required"]
    assert ITEM_SCHEMA["properties"]["due_text"]["type"] == ["string", "null"]
    assert "due_text" in extract.SYSTEM and len(extract.SYSTEM) <= 13200


def test_capture_keeps_the_time_words_only_when_they_are_the_persons(tmp_path):
    store = CommitmentStore(tmp_path / "c.db")
    said = "For the record: the quarterly review is on Tuesday at 09:15 in the studio two."
    kept = record_items([_create("Quarterly review", _due(), "Tuesday at 09:15")], person_id=OWNER,
                        commitment_store=store, existing=[], rejections=[], owner_id=OWNER, owner_text=said)
    assert store.get(kept["created"][0])["metadata"]["due_text"] == "Tuesday at 09:15"
    invented = record_items([_create("Book the plumber", _due(), "Tue 2026-09-29 09:15")], person_id=OWNER,
                            commitment_store=store, existing=[], rejections=[], owner_id=OWNER, owner_text=said)
    assert "due_text" not in (store.get(invented["created"][0])["metadata"] or {})


def test_a_reschedule_replaces_the_time_words_or_clears_them(tmp_path):
    store = CommitmentStore(tmp_path / "c.db")
    said = "Remind me to send Kim the recap by five."
    row = store.get(record_items([_create("Send Kim the recap", _due(5), "by five")], person_id=OWNER,
                                 commitment_store=store, existing=[], rejections=[], owner_id=OWNER,
                                 owner_text=said)["created"][0])
    moved = {"action": "reschedule", "target": 1, "description": "Send Kim the recap", "due_at": _due(3),
             "due_text": "by noon now", "priority": 70, "source_type": "cognition", "metadata": None,
             "listed_due": row["due_at"], "counterpart": None, "obligor": None}
    record_items([moved], person_id=OWNER, commitment_store=store, existing=[row], rejections=[], owner_id=OWNER,
                 owner_text="Kim needs the recap by noon now.")
    row = store.get(row["id"])
    assert row["metadata"]["due_text"] == "by noon now"
    record_items([{**moved, "due_at": _due(2), "due_text": None, "listed_due": row["due_at"]}], person_id=OWNER,
                 commitment_store=store, existing=[row], rejections=[], owner_id=OWNER,
                 owner_text="Make it an hour earlier than that.")
    assert store.get(row["id"])["metadata"].get("due_text") is None


def test_the_commitment_line_shows_the_words_beside_the_date():
    due = _due()
    row = {"id": "c-1", "description": "Quarterly review with p-72", "due_at": due,
           "metadata": {"due_text": "Tuesday at 09:15"}}
    assert host._commitment_due(row) == f' (due: {due}, said as "Tuesday at 09:15")'
    assert host._commitment_due({**row, "metadata": {}}) == f" (due: {due})"
    assert host._commitment_due({**row, "due_at": None}) == ""
