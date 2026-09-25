"""``/v1/mind``: the narrative and consolidation routes and the audit-log filters ``protagine_self`` answers from.

The mind behind the router is a stand-in with a real initiative store, so the routes are checked
with and without the memory milestone's ``narrative()`` / ``consolidate()`` ."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from protagine.api.routers import mind as mind_router
from protagine.initiatives.store import InitiativeStore

NOW = datetime(2027, 3, 1, 12, 0, tzinfo=timezone.utc)
NARRATIVE = {"enabled": True, "text": "I researched tides for the owner. [i-1]",
             "sections": {"interests": "tides", "strengths": "", "recent": "I researched tides. [i-1]", "stances": ""},
             "cites": ["i-1"], "updated_at": "2027-03-01T03:10:00+00:00"}


@pytest.fixture
def stand_in(tmp_path):
    store = InitiativeStore(state_dir=tmp_path)
    mind = SimpleNamespace(store=store, clock=lambda: NOW, enabled=True)
    mind_router.set_mind(mind)
    try:
        yield mind
    finally:
        mind_router.set_mind(None)
        store.close()


def _call(method, path, **kwargs):
    app = FastAPI()
    app.include_router(mind_router.router)

    async def run():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://mind") as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(run())


def _row(store, title, *, kind="task", decision="act", status="dispatched", recipient=None, age=timedelta(0)):
    row, _ = store.create_intention(kind=kind, type="research", title=title, drive="curiosity", cls="internal",
                                    decision=decision, decision_reason="test", status=status, dedup_key=None,
                                    recipient=recipient, created_at=NOW - age)
    return row.id


def test_narrative_is_empty_until_the_mind_exposes_one(stand_in):
    response = _call("GET", "/v1/mind/narrative")
    assert response.status_code == 200
    assert response.json() == {"enabled": False, "text": "", "sections": {}, "cites": [], "updated_at": None}
    stand_in.narrative = lambda: dict(NARRATIVE)
    assert _call("GET", "/v1/mind/narrative").json() == NARRATIVE


def test_consolidate_is_501_until_the_mind_can_and_awaits_it_when_it_can(stand_in):
    response = _call("POST", "/v1/mind/consolidate")
    assert response.status_code == 501
    assert response.json()["detail"]["code"] == "consolidation_not_available"
    calls = []

    async def consolidate(*, force=True):
        calls.append(force)
        return {"local_date": "2027-03-01", "done": ["narrative"], "tokens": 12}
    stand_in.consolidate = consolidate
    response = _call("POST", "/v1/mind/consolidate")
    assert response.status_code == 200 and response.json()["done"] == ["narrative"]
    assert calls == [True]


def test_log_filters_by_since_hours_and_recipient(stand_in):
    old = _row(stand_in.store, "old message", kind="message", recipient="p-07", age=timedelta(hours=40))
    recent = _row(stand_in.store, "recent message", kind="message", recipient="p-07", age=timedelta(hours=2))
    task = _row(stand_in.store, "a task", age=timedelta(hours=1))
    everything = _call("GET", "/v1/mind/log").json()["entries"]
    assert [e["id"] for e in everything] == [task, recent, old]
    yesterday = _call("GET", "/v1/mind/log", params={"since_hours": 24}).json()["entries"]
    assert {e["id"] for e in yesterday} == {task, recent}
    to_p07 = _call("GET", "/v1/mind/log", params={"since_hours": 24, "recipient": "p-07"}).json()["entries"]
    assert [e["id"] for e in to_p07] == [recent] and to_p07[0]["recipient"] == "p-07"
    assert _call("GET", "/v1/mind/log", params={"recipient": "p-99"}).json()["entries"] == []
    assert _call("GET", "/v1/mind/log", params={"since_hours": "soon"}).status_code == 422
    assert _call("GET", "/v1/mind/log", params={"since_hours": 24, "kind": "task"}).json()["entries"][0]["id"] == task


def test_the_recipient_filter_selects_before_the_limit(stand_in):
    """"Did I message p-07?" must find the message however many newer rows there are: the tool tells the
    agent that what is not in the log did not happen."""
    sent = _row(stand_in.store, "message to p-07", kind="message", recipient="p-07", age=timedelta(hours=3))
    for index in range(20):
        _row(stand_in.store, f"note {index}", age=timedelta(minutes=index))
    answer = _call("GET", "/v1/mind/log", params={"since_hours": 24, "recipient": "p-07"}).json()["entries"]
    assert [entry["id"] for entry in answer] == [sent]
    assert [entry["id"] for entry in _call("GET", "/v1/mind/log", params={"recipient": "p-07", "limit": 1}).json()["entries"]] == [sent]

def test_the_agents_log_lists_its_actions_apart_from_its_notes(stand_in):
    """What ``protagine_self log`` reads (``split``): the rows ``audit.is_action`` counts as the agent's own
    actions under ``actions``, and every other row (the nightly consolidation, a notice) under ``notes``, which
    never reads as something the agent did. Every id is shown whole: a prefix is not an id anyone can cite."""
    action = _row(stand_in.store, "message to p-07", kind="message", recipient="p-07", age=timedelta(hours=2))
    note, _ = stand_in.store.create_intention(
        kind="note", type="consolidation", title="nightly consolidation", drive="upkeep", cls="internal",
        decision="act", decision_reason="the nightly pass", status="done", dedup_key=None,
        created_at=NOW - timedelta(hours=1))
    answer = _call("GET", "/v1/mind/log", params={"split": "true"}).json()
    assert set(answer) == {"actions", "notes", "text"}
    assert [entry["id"] for entry in answer["actions"]] == [action]
    assert [entry["id"] for entry in answer["notes"]] == [note.id]
    assert "drive" not in answer["notes"][0] and "decision" not in answer["notes"][0]
    actions, _, notes = answer["text"].partition("Not actions")
    assert action in actions and note.id not in actions and note.id in notes
    # The unsplit log (the CLI's, the harness's) still holds every row, with whole ids.
    plain = _call("GET", "/v1/mind/log").json()
    assert {entry["id"] for entry in plain["entries"]} == {action, note.id}
    assert action in plain["text"] and note.id in plain["text"]


def test_why_names_the_missing_intention(stand_in):
    response = _call("GET", "/v1/mind/why/nope")
    assert response.status_code == 404
    assert response.json()["detail"] == {"code": "unknown_intention",
                                         "message": "no intention nope exists in the audit log"}
    assert _call("GET", "/v1/mind/log/nope").json()["detail"]["message"] == "no intention nope exists in the audit log"
    known = _row(stand_in.store, "known")
    assert _call("GET", "/v1/mind/why/" + known).json()["id"] == known


def test_a_named_guest_reads_only_the_bare_rows_addressed_to_them(stand_in):
    """Integration map X7, the sidecar's half: the routes take an optional ``viewer``; a viewer other
    than the owner sees only the rows addressed to them, without the title, concern, evidence or reason
    (which carry the owner's words about that person), no asks and only the switches. Canaries sit in
    an owner-granted notice, a check-in's matter, another contact's message and a task title."""
    stand_in.owner_id = "owner-1"
    stand_in.state = lambda: {"enabled": True, "autonomy": "standard", "asks": [{"title": "CANARY-ask-8e2"}],
                              "concerns": {"broadcast": ["CANARY-concern-3a7"]}}
    stand_in.asks = lambda: [{"ask_code": "K7F", "title": "CANARY-ask-8e2", "decision_reason": "r", "expires_at": None}]

    def row(title, *, kind, type, recipient=None, context=None):
        made, _ = stand_in.store.create_intention(
            kind=kind, type=type, title=title, drive="social", cls="message", decision="act",
            decision_reason="the owner said CANARY-reason-5f0", status="done", dedup_key=None, recipient=recipient,
            context=context or {}, created_at=NOW)
        return made.id

    notice = row("Tell p-05 CANARY-notice-2c9", kind="message", type="commitment_notice", recipient="p-05",
                 context={"concern": "CANARY-concern-3a7", "evidence": ["owner: CANARY-evidence-9d4"]})
    check_in = row("Check in with p-05 about CANARY-topic-1b6", kind="message", type="check_in", recipient="p-05")
    other = row("Tell p-07 CANARY-other-6c3", kind="message", type="commitment_notice", recipient="p-07")
    task = row("CANARY-task-7f3", kind="task", type="research")
    guest = {"viewer": "p-05"}

    log = _call("GET", "/v1/mind/log", params=guest).json()
    assert {entry["id"] for entry in log["entries"]} == {notice, check_in}
    assert all(set(entry) == {*mind_router.GUEST_FIELDS, "text"} for entry in log["entries"])
    assert _call("GET", "/v1/mind/log", params={**guest, "recipient": "p-07"}).json()["entries"] == []
    assert _call("GET", f"/v1/mind/why/{notice}", params=guest).json()["id"] == notice
    for hidden in (other, task):
        for path in ("/v1/mind/why/", "/v1/mind/log/"):
            response = _call("GET", path + hidden, params=guest)
            assert response.status_code == 404
            assert response.json()["detail"]["message"] == f"no intention {hidden} exists in the audit log"
    assert _call("GET", "/v1/mind/state", params=guest).json() == {"enabled": True, "autonomy": "standard"}
    assert _call("GET", "/v1/mind/status", params=guest).json() == {"enabled": True, "autonomy": "standard"}
    assert _call("GET", "/v1/mind/asks", params=guest).json()["asks"] == []
    answers = [_call("GET", "/v1/mind/log", params=guest).text, _call("GET", f"/v1/mind/why/{notice}", params=guest).text,
               _call("GET", f"/v1/mind/log/{check_in}", params=guest).text,
               _call("GET", "/v1/mind/state", params=guest).text, _call("GET", "/v1/mind/asks", params=guest).text]
    assert not any("CANARY" in text for text in answers)
    # The owner, named or not, reads the whole record.
    for owner in ({"viewer": "owner-1"}, {}):
        assert {e["id"] for e in _call("GET", "/v1/mind/log", params=owner).json()["entries"]} == {notice, check_in, other, task}
        assert "CANARY-concern-3a7" in _call("GET", f"/v1/mind/why/{notice}", params=owner).text
        assert _call("GET", "/v1/mind/asks", params=owner).json()["asks"][0]["title"] == "CANARY-ask-8e2"
    # No owner configured: every named viewer is a guest.
    stand_in.owner_id = None
    assert _call("GET", "/v1/mind/state", params={"viewer": "owner-1"}).json() == {"enabled": True, "autonomy": "standard"}


def test_the_lessons_route_shows_nothing_to_a_guest(stand_in, tmp_path):
    """``/v1/mind/lessons``: the owner's (and the key holder's) view of what the mind learned and where it
    was used; a guest viewer gets nothing, and only the owner or the key holder retires a lesson."""
    from protagine.mind.lessons import Lessons
    from protagine.mind.outcomes import Autobiography
    from protagine.turns.idempotency import TurnIdempotencyLedger
    ledger = TurnIdempotencyLedger(tmp_path / "turn-idempotency.db")
    stand_in.owner_id = "p-01"
    stand_in.lessons = Lessons(ledger=ledger, store=stand_in.store, owner_id="p-01", clock=lambda: NOW,
                               autobiography=Autobiography(ledger, owner_id="p-01", clock=lambda: NOW))
    lesson = stand_in.lessons.admit({"signature": "topic:tides", "kind": "strategy", "title": "Tide tables",
                                     "when_to_use": "tide tables are asked for", "content": "Use the harbour table."},
                                    verified="owner", origin="night", status="active", evidence=[], lineage=[], now=NOW)
    task, _ = stand_in.store.create_intention(kind="task", type="research", title="tides", drive="curiosity",
                                              cls="internal", decision="act", decision_reason="t", status="done",
                                              dedup_key=None, created_at=NOW)
    stand_in.store.update(task.id, lesson_ids=[lesson.id], outcome="done", verified="owner", verdict="useful")
    owner = _call("GET", "/v1/mind/lessons", params={"uses": "true"}).json()
    assert [row["id"] for row in owner["lessons"]] == [lesson.id] and owner["enabled"] is True
    assert owner["lessons"][0]["tally"] == {"uses": 1, "wins": 1, "losses": 0, "applied": 1}
    assert owner["uses"] == [{"lesson_id": lesson.id, "intention_id": task.id, "kind": "task", "session_id": None,
                              "at": task.created_at.isoformat(), "result": "win"}]
    assert _call("GET", "/v1/mind/lessons", params={"viewer": "p-01"}).json()["lessons"]
    guest = _call("GET", "/v1/mind/lessons", params={"uses": "true", "viewer": "p-02"}).json()
    assert guest["lessons"] == [] and guest["uses"] == [] and lesson.id not in guest["text"]
    assert _call("GET", "/v1/mind/lessons", params={"status": "retired"}).json()["lessons"] == []
    # Retiring: a reason is required, an unknown id is a 404, and a retired lesson stays listed as retired.
    assert _call("POST", f"/v1/mind/lessons/{lesson.id}/retire", json={}).status_code == 422
    assert _call("POST", "/v1/mind/lessons/L-0000000000/retire", json={"reason": "x"}).status_code == 404
    retired = _call("POST", f"/v1/mind/lessons/{lesson.id}/retire", json={"reason": "the rule changed", "by": "cli"})
    assert retired.status_code == 200 and retired.json()["status"] == "retired"
    assert retired.json()["closed_reason"] == "the rule changed"
    assert [row["id"] for row in _call("GET", "/v1/mind/lessons", params={"status": "retired"}).json()["lessons"]] == [
        lesson.id]
    assert _call("POST", f"/v1/mind/lessons/{lesson.id}/retire", json={"reason": "again"}).status_code == 409
