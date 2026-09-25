"""The owner's word that settles an interest, and optional work that waits for the owner's words to land.

An interest was satisfied only by a research task whose finding was stored, so "a friend explained it,
my curiosity is satisfied" had no path, and a tick that ran before that turn was captured dispatched the
research anyway. Capture now lists the owner's open interests after the open items, so the same pass
that completes or cancels a commitment settles an interest; and the tick holds optional work (research,
check-ins, optional nudges) while the owner's turns are still waiting for capture.
"""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from protagine.api.routers import mind as mind_router
from protagine.mind.concerns import SETTLED_FOR
from test_mind_loop import CONTACT, OWNER, Fixture

SETTLED = ("On tide tables: a friend explained it to me over lunch, so my curiosity is satisfied. "
           "Nothing to look into.")


class CaptureRouter:
    """``commitment_extract``: ``answer(prompt)`` is the model's answer; every prompt is kept."""

    supports_function_routing = True

    def __init__(self, answer):
        self.answer, self.prompts = answer, []

    def function_deadline_seconds(self, *, context=None):
        return 20

    async def complete(self, messages, *, context=None, **_):
        assert context["task"] == "commitment_extract"
        self.prompts.append(messages[1]["content"])
        return SimpleNamespace(content=json.dumps(self.answer(messages[1]["content"])))


def update(action, target, description):
    return {"action": action, "target": target, "description": description, "due_at": None, "priority": 70,
            "source_type": "cognition", "metadata": None, "listed_due": None, "counterpart": None, "obligor": None}


class Owed:
    """A capture that still owes ``owed`` of the owner's turns: its drain returns at once."""

    def __init__(self, owed):
        self.owed = owed

    async def drain(self, router, *, budget_seconds):
        return {"processed": 0, "pending_left": self.owed}

    def unfinished(self, contact_id):
        return self.owed if contact_id == OWNER else 0


@pytest.fixture
def fx(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    # Outreach reads owner turns by phrase and has its own suites; here the capture pass alone settles.
    fixture = Fixture(tmp_path, config={"faculties": {"outreach": False}}, drain=True)
    yield fixture
    mind_router.set_mind(None)
    fixture.store.close()


def titles(fx, summary):
    return [fx.store.get(item["id"]).description for item in summary["formed"]]


async def test_the_owner_settling_an_interest_closes_it_in_the_pass_that_closes_items(fx):
    fx.mind.add_interest("tide tables")
    first = await fx.mind.tick(force=True)
    [queued] = [item for item in first["formed"] if item["type"] == "research"]
    assert fx.store.get(queued["id"]).status == "approved"                # formed, not dispatched yet
    fx.mind.add_interest("moss lawns")
    fx.commitments.create(person_id=OWNER, description="Send Kim the invoice", priority=70, source_type="cognition",
                          due_at=(fx.now + timedelta(hours=2)).isoformat())
    assert fx.mind.open_interests() == ["moss lawns", "tide tables"]

    fx.turn("t-settle", OWNER, SETTLED, "Glad that is sorted.")
    router = CaptureRouter(lambda prompt: [update("complete", 3, "tide tables")])
    assert await fx.mind.capture.process_one(router) is True
    prompt = router.prompts[-1]
    assert "[1] Send Kim the invoice" in prompt
    assert "[2] moss lawns (interest, no due)" in prompt and "[3] tide tables (interest, no due)" in prompt
    assert fx.mind.open_interests() == ["moss lawns"]
    assert fx.mind.mind_state.get("interest:tide-tables")["level"] == 0.0
    assert "research:tide-tables" in fx.mind.concerns.settled_keys(fx.now - SETTLED_FOR)
    assert fx.store.get(queued["id"]).status == "cancelled"               # queued research on it goes too
    [item] = fx.commitments.get_pending_for_person(OWNER)
    assert item["description"] == "Send Kim the invoice"                  # the listed item is untouched

    later = await fx.mind.tick(force=True)
    assert titles(fx, later) and all("moss lawns" in title for title in titles(fx, later))

    # Only the owner's turn lists the owner's interests; an update aimed at one that is not a close is nothing.
    fx.turn("t-contact", CONTACT, "Thanks for the notes on the lawn.", "You're welcome.")
    quiet = CaptureRouter(lambda prompt: [])
    assert await fx.mind.capture.process_one(quiet) is True
    assert "(interest, no due)" not in quiet.prompts[-1]
    fx.turn("t-move", OWNER, "Moss lawns can wait until spring.", "Noted.")
    moved = CaptureRouter(lambda prompt: [update("reschedule", 2, "moss lawns")])
    assert await fx.mind.capture.process_one(moved) is True
    assert "[2] moss lawns (interest, no due)" in moved.prompts[-1] and fx.mind.open_interests() == ["moss lawns"]


async def test_optional_work_waits_while_the_owners_words_are_still_being_captured(fx):
    fx.mind.add_interest("tide tables")
    fx.commitments.create(person_id=OWNER, description="Send Kim the invoice", priority=70, source_type="cognition",
                          due_at=(fx.now - timedelta(minutes=5)).isoformat(), allow_overdue=True)
    fx.mind.capture = Owed(1)
    held = await fx.mind.tick(force=True)
    assert "research" not in [item["type"] for item in held["formed"]]
    assert [item["drive"] for item in held["formed"]] == ["duty"]         # what is owed does not wait
    fx.mind.capture.owed = 0
    landed = await fx.mind.tick(force=True)
    assert [item["type"] for item in landed["formed"]] == ["research"]


async def test_the_capture_extractor_counts_the_owners_turns_it_has_not_landed(fx):
    extractor = fx.mind.capture
    fx.turn("t-1", OWNER, "The lease is signed.", "Good.")
    fx.turn("t-2", CONTACT, "I sent the photos.", "Thanks.")
    assert extractor.unfinished(OWNER) == 1 and extractor.unfinished(CONTACT) == 1
    assert await extractor.process_one(CaptureRouter(lambda prompt: [])) is True
    assert extractor.unfinished(OWNER) == 0
