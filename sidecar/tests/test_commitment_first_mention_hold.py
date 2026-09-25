"""A first mention the owner wants no reminder about is recorded held, never as a reminder.

The HOLD rule ("do not remind me about X for now": a reschedule with no time) covered only an item already on
the numbered list. In the re-pilot the owner said of a new obligation "I am handling it myself. No reminders
about it", capture created it with its due time, and the tick reminded them of it (a forbidden body action).
The contract now applies the hold from the first mention: the item is created open with no time, so nothing
is said about it unasked, and a later "remind me at T" reinstates it like any held item.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from protagine.commitments import extract
from protagine.commitments.extract import CommitmentExtractor
from protagine.commitments.store import CommitmentStore
from protagine.mind.drives import DriveInputs, duty
from protagine.turns import TurnIdempotencyLedger

OWNER = "owner-1"


def _item(description, *, action="create", target=None, due_at=None, listed_due=None):
    return {"action": action, "target": target, "description": description, "due_at": due_at, "priority": 70,
            "source_type": "cognition", "metadata": None, "listed_due": listed_due, "counterpart": "p-05",
            "obligor": "owner" if action == "create" else None}


class _Router:
    supports_function_routing = True

    def __init__(self, *answers):
        self.answers, self.prompts = list(answers), []

    def function_deadline_seconds(self, *, context=None):
        return 20

    async def complete(self, messages, *, context=None, **_):
        self.prompts.append(messages[1]["content"])
        return SimpleNamespace(content=json.dumps(self.answers.pop(0)))


def test_the_contract_holds_a_first_mention_the_person_wants_no_reminder_about():
    system = " ".join(extract.SYSTEM.split())
    rule = system.split("A NEW item the person wants no reminders about")[1].split("Reinstating")[0]
    assert "is a HOLD from its first mention" in rule and '"create" it with due_at null' in rule
    assert "no reminders" in rule and "don't remind me" in rule


async def test_a_held_first_mention_is_open_says_nothing_and_a_later_time_reinstates_it(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    commitments = CommitmentStore(tmp_path / "commitments.db")
    ledger = TurnIdempotencyLedger(tmp_path / "turn-idempotency.db")
    extractor = CommitmentExtractor(ledger, lambda: commitments)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    ledger.record_source("t-1", contact_id=OWNER, session_id="owner-1", occurred_at=now.isoformat(), messages=[
        {"role": "user", "content": "The parcel receipt for p-05 is due in 20 minutes and I am handling it myself. "
                                    "No reminders about it."},
        {"role": "assistant", "content": "Understood."}])
    assert await extractor.process_one(_Router([_item("Send p-05 the parcel receipt")])) is True
    [row] = commitments.get_pending_for_person(OWNER)
    assert row["due_at"] is None and row["metadata"]["obligor"] == "owner"
    later = DriveInputs(now=now + timedelta(days=1), owner_id=OWNER, commitments=[row])
    assert duty(later)[1] == []                                     # held: no reminder, no heads-up, no task

    due = (now + timedelta(hours=2)).isoformat()
    ledger.record_source("t-2", contact_id=OWNER, session_id="owner-1", occurred_at=now.isoformat(), messages=[
        {"role": "user", "content": "Actually, remind me about the parcel receipt in two hours."},
        {"role": "assistant", "content": "Will do."}])
    router = _Router([_item("Send p-05 the parcel receipt", action="reschedule", target=1, due_at=due)])
    assert await extractor.process_one(router) is True
    assert "[1] Send p-05 the parcel receipt (no due)" in router.prompts[0]
    [row] = commitments.get_pending_for_person(OWNER)
    assert row["due_at"] is not None
