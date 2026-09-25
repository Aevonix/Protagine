"""An obligation's action is visible to the person it is owed to.

* A word the person asked for (a reminder, a nudge, a word if something has not happened) is a message
  when it falls due, never a kanban task, whoever the extractor names as obligor: capture marks it
  ``metadata.kind: reminder``.
* A task that fulfils an owed item reports its outcome (done, failed, blocked) to the person it is owed
  to: the owner hears it as a notice; a contact only through the owner's word on the exact text.
* One obligation gets one task: a deadline the worker moves, or a clock jump, never tasks it again.
"""

from __future__ import annotations

from datetime import timedelta

from protagine.commitments import extract
from protagine.commitments.extract import record_items
from protagine.commitments.store import CommitmentStore
from protagine.mind import audit
from protagine.mind.drives import DriveInputs, duty
from test_mind_loop import CONTACT, OWNER, fx  # noqa: F401  (pytest fixture)


def _row(store, *, person=OWNER, metadata=None, due, description="Nudge the owner if the brief goes quiet",
         source_type="cognition"):
    return store.create(person_id=person, description=description, due_at=due.isoformat(), priority=70,
                        source_type=source_type, metadata=metadata, allow_overdue=True)


# --- a word the person asked for is a message ----------------------------------------------------------

def test_the_contract_marks_a_word_for_the_person_as_a_reminder():
    system = " ".join(extract.SYSTEM.split())
    assert '{"kind":"reminder"}' in system
    assert "never a task" in system


def test_a_reminder_filed_as_the_assistants_work_is_a_message_to_the_owner(tmp_path, fx):
    store = CommitmentStore(tmp_path / "c.db")
    row = _row(store, metadata={"kind": "reminder", "obligor": "assistant", "counterpart": "p-39"},
               due=fx.now - timedelta(minutes=2), description="Remind the owner the venue contract is late")
    candidates = duty(DriveInputs(now=fx.now, owner_id=OWNER, commitments=[store.get(row["id"])]))[1]
    assert [(c.type, c.kind, c.recipient) for c in candidates] == [("commitment_reminder", "message", OWNER)]


def test_capture_keeps_the_reminder_kind(tmp_path):
    store = CommitmentStore(tmp_path / "c.db")
    item = {"action": "create", "target": None, "description": "Nudge the owner if the brief goes quiet",
            "due_at": "2026-06-26T16:00:00+00:00", "priority": 70, "source_type": "cognition",
            "metadata": {"kind": "reminder"}, "listed_due": None, "counterpart": "owner", "obligor": "assistant"}
    result = record_items([item], person_id=OWNER, commitment_store=store, existing=[], rejections=[],
                          owner_id=OWNER, owner_text="Should I go quiet past that, a nudge from you is welcome.")
    assert store.get(result["created"][0])["metadata"]["kind"] == "reminder"


async def test_a_reminder_row_never_becomes_a_task_in_the_tick(fx):
    _row(fx.commitments, metadata={"kind": "reminder", "obligor": "assistant"}, due=fx.now + timedelta(minutes=5))
    fx.shift(minutes=10)
    formed = (await fx.mind.tick(force=True))["formed"]
    assert [(item["type"], item["kind"]) for item in formed] == [("commitment_reminder", "message")]
    assert fx.mind.dispatch() == []


# --- a task reports its outcome to the person it is owed to --------------------------------------------

async def _one_task(fx, *, person=OWNER, description="Draft the offsite agenda"):
    """An assistant-owed item past due: exactly one task, dispatched and bound."""
    row = fx.commitments.create(person_id=person, description=description,
                                due_at=(fx.now + timedelta(minutes=5)).isoformat())
    fx.shift(minutes=10)
    summary = await fx.mind.tick(force=True)
    formed, = summary["formed"]
    assert formed["type"] == "commitment_overdue"
    if formed["status"] == "asked":          # work for a contact waits for the owner's yes
        await fx.mind.answer(fx.store.get(formed["id"]).ask_code, yes=True, contact_id=OWNER)
    task, = fx.mind.dispatch()
    fx.mind.bound(task["id"], f"kanban:{task['id'][:8]}")
    return row, task


def _reports(fx, recipient=OWNER):
    return [row for row in fx.store.intentions(kind=["message"], limit=100)
            if row.type == "task_outcome" and row.entity_id == recipient]


async def test_a_done_task_tells_the_owner_what_it_did(fx):
    row, task = await _one_task(fx)
    fx.mind.outcomes.record(task["id"], status="done", hermes_ref=f"kanban:{task['id'][:8]}",
                            summary="Agenda drafted: three sessions, lunch at noon.")
    report, = _reports(fx)
    assert report.status == "approved" and report.decision == "act"
    assert "Draft the offsite agenda" in report.context["text"]
    assert "three sessions, lunch at noon" in report.context["text"]
    payloads = [p for p in await fx.mind.outbox_ready() if p["id"] == report.id]
    assert payloads and payloads[0]["recipient"] == OWNER
    # Reported once: the same outcome again, and later ticks, add nothing.
    fx.mind.outcomes.record(task["id"], status="done", summary="Agenda drafted: three sessions, lunch at noon.")
    await fx.mind.tick(force=True)
    assert len(_reports(fx)) == 1
    assert "task_outcome" in audit.NOTICE_TYPES


async def test_a_failed_task_tells_the_owner_it_did_not_finish_and_why(fx):
    row, task = await _one_task(fx)
    fx.mind.outcomes.record(task["id"], status="failed", summary="", error="Iteration budget exhausted (8/8)")
    report, = _reports(fx)
    assert "could not finish" in report.context["text"] and "Iteration budget exhausted" in report.context["text"]
    assert fx.commitments.get(row["id"])["status"] in {"pending", "overdue"}      # still owed, and said so


async def test_a_blocked_task_tells_the_owner_once(fx):
    row, task = await _one_task(fx)
    fx.mind.outcomes.record(task["id"], status="blocked", summary="Needs the venue name to go on.")
    fx.mind.outcomes.record(task["id"], status="blocked", summary="Needs the venue name to go on.")
    report, = _reports(fx)
    assert "stuck" in report.context["text"] and "venue name" in report.context["text"]


async def test_a_task_owed_to_a_contact_reaches_them_only_on_the_owners_word(fx):
    row, task = await _one_task(fx, person=CONTACT, description="Send the contact the delivery date")
    fx.mind.outcomes.record(task["id"], status="done", summary="The delivery date is the 14th.")
    assert _reports(fx, CONTACT) == []                   # a contact report is formed through authority
    await fx.mind.tick(force=True)
    report, = _reports(fx, CONTACT)
    assert report.status == "asked" and report.ask_code
    assert "The delivery date is the 14th." in report.context["text"]
    assert [p for p in await fx.mind.outbox_ready() if p["recipient"] == CONTACT] == []


# --- one obligation, one task ------------------------------------------------------------------------------

async def test_a_deadline_the_worker_moved_does_not_task_the_obligation_again(fx):
    """The worker snoozed the commitment while it ran, the run failed, the clock jumped four hours: the
    owner was told once and no second task forms for the same obligation."""
    row, task = await _one_task(fx, description="Nudge the owner if the brief goes quiet")
    fx.commitments.update(row["id"], due_at=(fx.now + timedelta(minutes=20)).isoformat())
    fx.mind.outcomes.record(task["id"], status="failed", error="Iteration budget exhausted (8/8)")
    fx.shift(hours=4)
    for _ in range(3):
        assert [item["type"] for item in (await fx.mind.tick(force=True))["formed"]] == []
    assert fx.mind.dispatch() == []
    assert len(_reports(fx)) == 1
    tasks = [r for r in fx.store.intentions(kind=["task"], limit=50) if r.source_id == row["id"]]
    assert len(tasks) == 1 and tasks[0].dedup_key == f"commitment:{row['id']}:task"


async def test_the_person_asking_again_in_conversation_earns_a_new_task(fx):
    """A new schedule the person sets ("try it again by five") is a new obligation to act on: capture's
    reschedule names its turn, and the one-task rule is per schedule the person set."""
    row, task = await _one_task(fx, description="Book the meeting room")
    fx.mind.outcomes.record(task["id"], status="failed", error="The booking page was down.")
    listed = fx.commitments.get_pending_for_person(OWNER)
    new_due = (fx.now + timedelta(hours=2)).isoformat()
    moved = record_items([{"action": "reschedule", "target": 1, "description": "Book the meeting room",
                           "due_at": new_due, "priority": 70, "source_type": "cognition", "metadata": None,
                           "listed_due": listed[0]["due_at"], "counterpart": None, "obligor": None}],
                         person_id=OWNER, commitment_store=fx.commitments, existing=listed, rejections=[],
                         turn_id="turn-again", owner_id=OWNER)
    assert moved["updated"] == [row["id"]]
    fx.shift(hours=3)
    formed = (await fx.mind.tick(force=True))["formed"]
    assert [item["type"] for item in formed] == ["commitment_overdue"]
    assert fx.store.get(formed[0]["id"]).dedup_key == f"commitment:{row['id']}:task:turn:turn-again"
