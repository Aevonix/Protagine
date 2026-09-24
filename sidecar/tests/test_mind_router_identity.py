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

def test_why_names_the_missing_intention(stand_in):
    response = _call("GET", "/v1/mind/why/nope")
    assert response.status_code == 404
    assert response.json()["detail"] == {"code": "unknown_intention",
                                         "message": "no intention nope exists in the audit log"}
    assert _call("GET", "/v1/mind/log/nope").json()["detail"]["message"] == "no intention nope exists in the audit log"
    known = _row(stand_in.store, "known")
    assert _call("GET", "/v1/mind/why/" + known).json()["id"] == known
