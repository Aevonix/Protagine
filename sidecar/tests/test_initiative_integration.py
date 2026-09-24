"""The initiative iteration's units together, offline: capture writes and amends the row the mind
reads, the tick drains capture before it decides, an owner reminder is a message with an address
the body can send to, and a deadline the conversation pushed out means no action at the old time,
whichever side of the formed intention the push-out lands on.

A scripted router plays the extraction model; the mind's clock is the fixture's.
"""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

from protagine.mind.drives import schedule_key
from test_mind_loop import OWNER, FakeRouter, Fixture

DESCRIPTION = "Send Kim the summary"
PROMISE = "I owe Kim the summary and it needs to be with them in five minutes."
PUSH_OUT = "Kim wrote that the summary can wait until this afternoon."


class ScriptedRouter(FakeRouter):
    """Answers each turn's extraction from what that turn said. The reply is keyed on a phrase of
    the audited turn, read below "This turn, verbatim", so the earlier turns the prompt carries as
    context never pick the wrong reply."""

    def __init__(self):
        super().__init__(due_at=None)
        self.script, self.prompts = [], []

    async def complete(self, messages, *, context=None, **_):
        self.calls.append(context)
        prompt = messages[1]["content"]
        self.prompts.append(prompt)
        turn = prompt.split("This turn, verbatim:", 1)[-1]
        for phrase, items in self.script:
            if phrase in turn:
                return SimpleNamespace(content=json.dumps(items))
        return SimpleNamespace(content="[]")


def _item(action, target, due_at):
    return {"action": action, "target": target, "description": DESCRIPTION, "due_at": due_at.isoformat(),
            "priority": 70, "source_type": "cognition", "metadata": None}


def _fixture(tmp_path, monkeypatch):
    """The production wiring: the extractor drained by the tick, the same router for both."""
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fx = Fixture(tmp_path, router=ScriptedRouter(), drain=True)
    first_due, later_due = fx.now + timedelta(minutes=5), fx.now + timedelta(hours=3)
    fx.router.script[:] = [("can wait until", [_item("reschedule", 1, later_due)]),
                           ("needs to be with them", [_item("create", None, first_due)])]
    return fx, first_due, later_due


async def test_a_deadline_pushed_out_in_conversation_means_no_action_at_the_old_time(tmp_path, monkeypatch):
    """Capture -> update -> tick. The promise becomes a row inside the tick's drain; the later message
    moves that same row's deadline out (the extractor saw it numbered, with the promise as context);
    the ticks after the old deadline form nothing; at the new deadline the reminder is one message
    to the owner with an address."""
    fx, first_due, later_due = _fixture(tmp_path, monkeypatch)
    try:
        fx.turn("turn-1", OWNER, PROMISE, "Noted.")
        first = await fx.mind.tick(force=True)
        assert first["capture_drained"]["recorded"] == 1 and first["formed"] == []
        row, = fx.commitments.get_pending_for_person(OWNER)
        assert row["due_at"] == first_due.isoformat() and row["source_type"] == "cognition"

        fx.shift(minutes=3)
        fx.turn("turn-2", OWNER, PUSH_OUT, "Understood.")
        fx.shift(minutes=4)                                              # past the first deadline
        second = await fx.mind.tick(force=True)
        assert second["capture_drained"]["recorded"] == 1
        prompt = fx.router.prompts[-1]
        assert f"[1] {DESCRIPTION} (due {first_due.isoformat()})" in prompt
        assert "Recent conversation" in prompt and PROMISE in prompt
        assert second["overdue_flipped"] == 0 and second["formed"] == []
        assert await fx.mind.outbox_ready() == [] and fx.mind.dispatch() == []
        moved, = fx.commitments.get_pending_for_person(OWNER)           # the same row, not a second one
        assert moved["id"] == row["id"] and moved["status"] == "pending"
        assert moved["due_at"] == later_due.isoformat()
        assert moved["metadata"]["reschedule"] == {"from": first_due.isoformat(), "by": "conversation",
                                                   "note": "turn:turn-2"}
        assert (await fx.mind.tick(force=True))["formed"] == []          # still nothing at the old time

        fx.shift(hours=3)
        third = await fx.mind.tick(force=True)
        assert [item["type"] for item in third["formed"]] == ["commitment_reminder"]
        ready, = await fx.mind.outbox_ready()
        assert ready["recipient"] == OWNER and ready["recipient_is_owner"] is True
        assert ready["recipient_handles"][0]["address"] == f"{OWNER}-handle"
        assert DESCRIPTION in ready["text"] and later_due.strftime("%Y-%m-%d %H:%M UTC") in ready["text"]
        assert fx.mind.dispatch() == []                                  # a reminder is never a task
    finally:
        fx.store.close()


async def test_a_push_out_that_lands_after_the_reminder_formed_cancels_it_before_it_is_sent(tmp_path, monkeypatch):
    """The other order: the reminder is approved and in the outbox when the push-out is spoken. The
    next tick's drain moves the row; the outbox re-checks the row before any send and cancels the
    reminder as the check's verdict; the obligation is raised again only at the new deadline."""
    fx, first_due, later_due = _fixture(tmp_path, monkeypatch)
    try:
        fx.turn("turn-1", OWNER, PROMISE, "Noted.")
        assert (await fx.mind.tick(force=True))["formed"] == []
        row, = fx.commitments.get_pending_for_person(OWNER)
        fx.shift(minutes=10)
        formed = await fx.mind.tick(force=True)
        reminder, = formed["formed"]
        assert reminder["type"] == "commitment_reminder" and len(await fx.mind.outbox_ready()) == 1
        assert fx.commitments.get(row["id"])["status"] == "overdue"

        fx.turn("turn-2", OWNER, PUSH_OUT, "Understood.")
        summary = await fx.mind.tick(force=True)
        assert summary["capture_drained"]["recorded"] == 1 and summary["formed"] == []
        assert fx.commitments.get(row["id"])["status"] == "pending"
        assert await fx.mind.outbox_ready() == []
        cancelled = fx.store.get(reminder["id"])
        assert cancelled.status == "cancelled" and cancelled.verified == "check" and cancelled.verdict is None
        assert "no longer due" in cancelled.cancelled_reason
        assert (await fx.mind.tick(force=True))["formed"] == []

        fx.shift(hours=3)
        again = await fx.mind.tick(force=True)
        fresh, = again["formed"]
        assert fresh["type"] == "commitment_reminder" and fresh["id"] != reminder["id"]
        ready, = await fx.mind.outbox_ready()
        assert ready["recipient_is_owner"] is True and DESCRIPTION in ready["text"]
        assert fx.store.get(fresh["id"]).dedup_key == schedule_key(row["id"], "overdue", later_due)
    finally:
        fx.store.close()
