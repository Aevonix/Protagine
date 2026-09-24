"""The M9 campaign smoke tests (build plan M9 acceptance 1), end to end in the sidecar.

An owner's correction of the agent's work becomes a lesson at night, and the next similar task body
carries it with its ``lesson_ids``; an owner's verdict in conversation becomes a lesson the owner's
next session is given, and that use is logged. Only the model is a fake.
"""

from __future__ import annotations

import json

from httpx import ASGITransport, AsyncClient
import pytest

from protagine.api.middleware import ApiKeyMiddleware
from protagine.api.routers import mind as mind_router
from onekey import AUTH, KEY
from test_mind_lessons_night import LESSONS_ONLY, REQUEST, RULE_QUOTE, VERDICT, add, label, make, ruled, training_day
from test_turn_source_evidence import source_app  # noqa: F401  (pytest fixture)
from test_mind_consolidate import OWNER

ROOMY = {"breaker": {"failures": 50}, "budgets": {"tasks_per_hour": 50, "concurrent_tasks": 50}}


async def test_campaign_smoke_an_owner_correction_becomes_a_lesson_in_the_next_similar_task_body(tmp_path, monkeypatch):
    def answer(prompt):
        corrected = label(prompt, "Research: tide tables")
        return {"verdicts": [], "ops": [add([corrected], topic="", title="Tide tables come from the harbour office",
                                            when_to_use="tide tables are researched",
                                            content="Use the harbour office's published table, not the forecast site.")]}
    # The owner's "wrong" also lowers the ranker's multiplier for research (M2); a lower act threshold keeps
    # next week's research eligible, since this test is about what its body carries.
    fx = make(tmp_path, monkeypatch, answer, config={**LESSONS_ONLY, **ROOMY, "act_threshold": 0.3})
    fx.mind.add_interest("tide tables")
    first = await fx.mind.tick(force=True)
    [formed] = [item for item in first["formed"] if item["type"] == "research"]
    row = fx.store.get(formed["id"])
    assert row.lesson_ids is None
    fx.mind.bound(row.id, "kanban:1")
    fx.mind.outcomes.record(row.id, status="done", hermes_ref="kanban:1", summary="finding: high tide at nine")
    fx.mind.rate(row.id, "wrong")                                  # the owner's correction
    assert fx.store.get(row.id).verified == "owner"
    fx.shift(days=7, hours=1)
    second = await fx.mind.tick(force=True)                        # the night, then the week's next research
    assert second["consolidation"] == "done"
    [lesson] = fx.mind.lessons.all()
    assert lesson.verified == "owner" and lesson.signature == "research:tide-tables"
    assert lesson.evidence == [f"intention:{row.id}"]
    [again] = [fx.store.get(item["id"]) for item in second["formed"] if item["type"] == "research"]
    assert json.loads(again.lesson_ids) == [lesson.id]
    [sent] = [item for item in fx.mind.dispatch() if item["id"] == again.id]
    assert f"[lesson {lesson.id}, strategy] When tide tables are researched:" in sent["body"]
    fx.store.close()


@pytest.fixture
def served(source_app, tmp_path, monkeypatch):  # noqa: F811
    def answer(prompt):
        if "Verdict on train-01.json" not in prompt:
            return {"verdicts": [], "ops": []}
        return {"verdicts": ruled(prompt), "ops": [add([label(prompt, "Verdict on train-01.json")], quote=RULE_QUOTE)]}
    fx = make(tmp_path, monkeypatch, answer)
    source_app.add_middleware(ApiKeyMiddleware, api_key=KEY)
    mind_router.set_mind(fx.mind)
    try:
        yield source_app, fx
    finally:
        mind_router.set_mind(None)
        fx.store.close()


async def owner_turn(app, session, text):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/host/context/assemble", headers=AUTH, json={
            "identity": {"host_id": "fixture"}, "context": {"contact_id": OWNER, "session_id": session},
            "incoming_message": {"role": "user", "content": text}})
    assert response.status_code == 200, response.text
    return {section["id"]: section for section in response.json()["sections"]}


async def test_campaign_smoke_an_owner_verdict_in_conversation_reaches_the_next_session(served):
    app, fx = served
    before = await owner_turn(app, "day-01", REQUEST)
    assert "protagine-lessons" not in before
    training_day(fx)                                               # day-01: the request, the reply, the verdict
    assert VERDICT
    fx.shift(days=1)
    assert (await fx.mind.tick(force=True))["consolidation"] == "done"
    [lesson] = fx.mind.lessons.all()
    later = await owner_turn(app, "day-04", "Order 5320 came in from p-11 by email. I need its order code.")
    section = later["protagine-lessons"]
    assert section["body"].startswith(f"[lesson {lesson.id}, from the owner's verdicts] When an order code is asked for:")
    [note] = [row for row in fx.store.intentions(kind=["note"], limit=20) if row.type == "lesson_use"]
    assert note.source_id == "day-04" and json.loads(note.lesson_ids) == [lesson.id]
