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

import pytest

from protagine.commitments import extract
from protagine.commitments.extract import record_items
from protagine.commitments.store import CommitmentStore
from protagine.mind import audit
from protagine.mind.drives import DriveInputs, duty
from test_mind_loop import CONTACT, OWNER, fx, pinned_clock  # noqa: F401  (pinned_clock, fx: pytest fixtures)


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


async def test_an_obligation_the_previous_release_tasked_is_not_tasked_again(fx, monkeypatch):
    """The release before this one keyed an obligation's task on its deadline
    (``commitment:<id>:overdue:<due>``): an upgrade finds that task under the old key and forms no second."""
    from protagine.mind import drives
    row = fx.commitments.create(person_id=OWNER, description="Draft the offsite agenda",
                                due_at=(fx.now + timedelta(minutes=5)).isoformat())
    fx.shift(minutes=10)
    current = drives.task_key
    monkeypatch.setattr(drives, "task_key",
                        lambda item: drives.schedule_key(item["id"], "overdue", drives._utc(item["due_at"])))
    formed, = (await fx.mind.tick(force=True))["formed"]
    assert formed["type"] == "commitment_overdue"
    monkeypatch.setattr(drives, "task_key", current)
    fx.shift(minutes=10)
    for _ in range(2):
        assert (await fx.mind.tick(force=True))["formed"] == []
    assert len([r for r in fx.store.intentions(kind=["task"], limit=50) if r.source_id == row["id"]]) == 1
    # Settled under the old key (resolved), the obligation is not raised as a new task either.
    due = drives._utc(row["due_at"])
    rows = [fx.commitments.get(row["id"])]
    settled = {drives.schedule_key(row["id"], "overdue", due)}
    assert duty(DriveInputs(now=fx.now, owner_id=OWNER, commitments=rows, settled=settled))[1] == []


def test_a_reminder_on_a_contacts_lane_is_a_word_never_a_task(tmp_path, fx):
    """"Remind me at 1 to send the signed form" on p-07's lane, filed with the assistant as obligor: a word
    when due (to the owner, as every contact-lane word is), never a worker's task."""
    store = CommitmentStore(tmp_path / "c.db")
    row = _row(store, person=CONTACT, metadata={"kind": "reminder", "obligor": "assistant"},
               due=fx.now - timedelta(minutes=2), description="Remind them to send the signed form")
    candidates = duty(DriveInputs(now=fx.now, owner_id=OWNER, commitments=[store.get(row["id"])]))[1]
    assert [(c.type, c.kind, c.recipient) for c in candidates] == [("commitment_reminder", "message", OWNER)]


async def test_the_owner_sees_the_exact_words_a_contact_report_would_send_before_saying_yes(fx):
    """A worker's report for a contact may carry what only the owner may see: every place the owner is asked
    about it (the ask notice, the digest, ``asks`` and ``why``) shows the exact text that would be sent."""
    from protagine.mind.outbox import message_payload
    row, task = await _one_task(fx, person=CONTACT, description="Send the contact the delivery date")
    fx.mind.outcomes.record(task["id"], status="done",
                            summary="Delivery is Friday. Owner confidential: acquisition offer is $2 million.")
    await fx.mind.tick(force=True)
    report, = _reports(fx, CONTACT)
    assert report.status == "asked" and report.ask_code
    words = message_payload(report, owner_id=OWNER)["text"]
    assert "acquisition offer is $2 million" in words
    fx.shift(minutes=1)
    notice = fx.mind.outbox.notify_asks([report], force=True)
    assert notice is not None and words in notice.context["text"]
    listed, = [item for item in fx.mind.asks() if item["ask_code"] == report.ask_code]
    assert listed["message"] == words
    assert words in audit.why(fx.store, report.id)["sentence"]
    assert words in fx.mind.outbox.build_digest(since=fx.now - timedelta(days=1), level="standard")


SECRETS = "Owner private phone +14155552671; PASSWORD=notforthiscontact"


def _every_preview(fx, report):
    """Every place the owner is shown an ask: the ask notice, ``asks``, ``why``, the digest and the context
    packet's waiting line."""
    notice = fx.mind.outbox.notify_asks([report], force=True)
    listed, = [item for item in fx.mind.asks() if item["ask_code"] == report.ask_code]
    return {"notice": notice.context["text"], "asks": listed["message"],
            "why": audit.why(fx.store, report.id)["sentence"],
            "digest": fx.mind.outbox.build_digest(since=fx.now - timedelta(days=1), level="standard"),
            "packet": fx.mind.section()}


@pytest.mark.parametrize("summary", [
    f"Delivery is Friday. {SECRETS}.",
    f"Delivery is Friday.\n{SECRETS}\nToken: sk-abcdefghijklmnopqrstuvwxyz0123456789",
    "Delivery is Friday, nothing private.",
])
async def test_the_words_the_owner_approves_are_byte_for_byte_the_words_sent(fx, summary):
    """Re-check F2: a redacted preview approved an unredacted send. What the owner is shown in every place is
    exactly what the contact receives after the yes: a value hidden from the preview is hidden in the send."""
    from protagine.mind.outbox import message_payload
    from protagine.redact import redact_sensitive_text
    row, task = await _one_task(fx, person=CONTACT, description="Send the contact the delivery date")
    fx.mind.outcomes.record(task["id"], status="done", summary=summary)
    await fx.mind.tick(force=True)
    report, = _reports(fx, CONTACT)
    assert report.status == "asked" and report.ask_code
    fx.shift(minutes=1)
    previews = _every_preview(fx, report)
    await fx.mind.answer(report.ask_code, yes=True, contact_id=OWNER)
    sent, = [p for p in await fx.mind.outbox_ready() if p["id"] == report.id]
    words = sent["text"]
    assert words == message_payload(fx.store.get(report.id), owner_id=OWNER)["text"]
    assert words == redact_sensitive_text(words)                  # nothing the preview would hide goes out
    assert "notforthiscontact" not in words and "4155552671" not in words
    for where, shown in previews.items():
        assert words in shown, where
    assert previews["asks"] == words


async def test_the_packet_never_offers_a_message_ask_by_its_title_alone(fx):
    """The conversational context shows a waiting message's exact words with its code, or no code to answer
    at all: an owner never approves in conversation what they could not read."""
    row, task = await _one_task(fx, person=CONTACT, description="Send the contact the delivery date")
    fx.mind.outcomes.record(task["id"], status="done", summary="Delivery is Friday. " + "More detail. " * 60)
    await fx.mind.tick(force=True)
    report, = _reports(fx, CONTACT)
    section = fx.mind.section()
    words = audit.outgoing_text(report)
    assert report.ask_code not in section and "its words are in the ask notice" in section
    assert words not in section


async def test_owner_reports_go_through_authority_and_never_take_a_reminders_budget(fx):
    """A report of work owed to the owner is decided by authority like every word to them (the off switch, the
    level, the floor and the budgets), and, as the answer to what they asked for, it neither spends nor is held
    by the daily owner budget: two reports and the reminder the owner asked for all go on a budget of one."""
    fx.mind.policy.budgets.owner_messages_per_day = 1
    for description in ("Draft the offsite agenda", "Book the offsite venue"):
        fx.commitments.create(person_id=OWNER, description=description,
                              due_at=(fx.now + timedelta(minutes=5)).isoformat())
    fx.shift(minutes=10)
    await fx.mind.tick(force=True)
    tasks = fx.mind.dispatch()
    assert len(tasks) == 2
    for task in tasks:
        fx.mind.bound(task["id"], f"kanban:{task['id'][:8]}")
        fx.mind.outcomes.record(task["id"], status="done", summary="Done as asked.")
    reports = _reports(fx)
    assert len(reports) == 2 and all(row.decision == "act" and row.decision_reason != "owner notice"
                                     for row in reports)
    assert fx.mind.authority.budget_check(kind="message", recipient=OWNER, type="task_outcome") is None
    ready = {p["id"] for p in await fx.mind.outbox_ready()}
    assert {row.id for row in reports} <= ready
    fx.commitments.create(person_id=OWNER, description="Call the landlord about the boiler",
                          due_at=(fx.now + timedelta(minutes=5)).isoformat(), metadata={"kind": "reminder"})
    fx.shift(minutes=10)
    formed = (await fx.mind.tick(force=True))["formed"]
    reminder, = [entry for entry in formed if entry["type"] == "commitment_reminder"]
    assert reminder["status"] == "approved"


async def test_with_the_mind_off_a_task_report_is_not_queued(fx):
    row, task = await _one_task(fx)
    fx.mind.authority.set_enabled(False)
    fx.mind.outcomes.record(task["id"], status="done", summary="Agenda drafted.")
    assert [report.status for report in _reports(fx)] in ([], ["proposed"])
    assert [p for p in await fx.mind.outbox_ready() if p["type"] == "task_outcome"] == []


@pytest.mark.parametrize("switch", ["authority", "off"])
async def test_a_report_of_a_task_finished_while_the_mind_is_off_goes_once_it_is_back_on(fx, switch):
    """Re-check new P2: the task was dispatched, the mind turned off, the task completed: the report waits and
    reaches the owner when the mind is back on (the commitment is fulfilled; the report is its only word)."""
    row, task = await _one_task(fx)
    if switch == "off":
        fx.mind.off(reason="test")
    else:
        fx.mind.authority.set_enabled(False)
    fx.mind.outcomes.record(task["id"], status="done", summary="Agenda drafted: three sessions.")
    assert [p for p in await fx.mind.outbox_ready() if p["type"] == "task_outcome"] == []
    if switch == "off":
        fx.mind.on(by="owner")
    else:
        fx.mind.authority.set_enabled(True)
    await fx.mind.tick(force=True)
    sent = [p for p in await fx.mind.outbox_ready() if p["type"] == "task_outcome"]
    assert len(sent) == 1 and "three sessions" in sent[0]["text"]


async def test_a_report_waiting_to_go_when_the_mind_is_turned_off_goes_once_it_is_back_on(fx):
    row, task = await _one_task(fx)
    fx.mind.outcomes.record(task["id"], status="done", summary="Agenda drafted: three sessions.")
    report, = _reports(fx)
    assert report.status == "approved"
    fx.mind.off(reason="test")
    assert fx.store.get(report.id).status != "cancelled"
    assert [p for p in await fx.mind.outbox_ready() if p["type"] == "task_outcome"] == []
    fx.mind.on(by="owner")
    await fx.mind.tick(force=True)
    assert [p["id"] for p in await fx.mind.outbox_ready() if p["type"] == "task_outcome"] == [report.id]
