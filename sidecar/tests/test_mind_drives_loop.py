"""Drives, deliberation and agent-owned goals end to end in the sidecar (build plan M4 acceptance).

A seeded interest plus idle ticks produces exactly one research task whose
finding is stored as an autobiography entry a later turn recalls; the goal
lifecycle (satisfied, expired at the horizon, a third not adopted while two
are open); the off switch mid-episode leaves 0 new effects; the per-tick
call cap holds; reconsideration happens only on a matching event; duty
reads the plugin's board observations; the Mind section and the broadcast
flag.
"""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from protagine.api.routers import mind as mind_router
from protagine.mind.deliberate import ASK_RESPONSE_SCHEMA, RESPONSE_SCHEMA, TASK
from protagine.mind.drives import period
from test_mind_loop import AUTH, OWNER, Fixture


class DeliberationRouter:
    """Answers ``mind_deliberate`` with a proposal chosen by the topic in the prompt."""

    supports_function_routing = True

    def __init__(self, proposals=None, *, fail=False):
        self.proposals, self.fail, self.calls, self.prompts = dict(proposals or {}), fail, [], []

    def function_deadline_seconds(self, *, context=None):
        return 20

    async def complete(self, messages, *, context=None, **_):
        prompt = messages[1]["content"]
        # Kind "ask" is offered only with a failing topic's prior attempts (affect's strategy switch).
        schema = ASK_RESPONSE_SCHEMA if "Prior attempts:" in prompt else RESPONSE_SCHEMA
        assert context["task"] == TASK and context["response_schema"] == schema
        assert "tools" not in context and context["allow_fallback"] is False    # one request per call
        self.calls.append(context)
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError("endpoint down")
        for topic, proposal in sorted(self.proposals.items(), key=lambda item: -len(item[0])):
            if topic in prompt:
                return SimpleNamespace(content=json.dumps(proposal), usage={"total_tokens": 120})
        return SimpleNamespace(content=json.dumps({
            "kind": "task", "title": "Look into it", "body": "Look it up in two sources and report finding: <text>.",
            "success_check": {"kind": "result_field", "field": "finding"}}), usage={"total_tokens": 100})


def research(topic, *, body="Find out and report finding: what was learned."):
    return {"kind": "task", "title": f"Research {topic}", "body": body,
            "success_check": {"kind": "result_field", "field": "finding"}}


def goal(topic, *, check=None, horizon_days=3, tasks=2):
    return {"kind": "goal", "title": f"Learn about {topic}", "body": f"Work through {topic} step by step.",
            "goal": {"description": f"Build a working understanding of {topic}.",
                     "success_check": check or {"kind": "steps_done", "count": 1},
                     "horizon_days": horizon_days, "tasks": tasks}}


@pytest.fixture
def fx(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fixture = Fixture(tmp_path)
    yield fixture
    mind_router.set_mind(None)
    fixture.store.close()


async def idle(fx, ticks=1):
    summaries = []
    for _ in range(ticks):
        summaries.append(await fx.mind.tick(force=True))
    return summaries


# ---------------------------------------------------------------------------
# Curiosity: one research task, its finding stored and used later
# ---------------------------------------------------------------------------

async def test_seeded_interest_produces_one_research_task_whose_finding_a_later_turn_uses(fx):
    router = DeliberationRouter({"local history": research(
        "local history", body="Find when the town was founded and by whom; report finding: <one paragraph>.")})
    fx.mind.router = router
    seeded = fx.mind.add_interest("local history", why="the owner asked about the old mill")
    assert seeded["topic"] == "local history" and seeded["weight"] == 1.0

    first, second, third = await idle(fx, 3)
    assert [item["type"] for item in first["formed"]] == ["research"] and first["model_calls"] == 1
    assert first["formed"][0]["decision"] == "act" and first["formed"][0]["drive"] == "curiosity"
    assert second["formed"] == [] and third["formed"] == []                 # reported once
    assert len(router.calls) == 1 and "the owner asked about the old mill" in router.prompts[0]
    assert "Quoted" in router.prompts[0] or "quoted" in router.prompts[0]

    queue = fx.mind.dispatch()
    assert len(queue) == 1
    item = queue[0]
    assert item["type"] == "research" and item["goal_mode"] is False and item["assignee"] == "protagine-act"
    assert "Find when the town was founded" in item["body"] and "Reason: curiosity drive" in item["body"]
    row = fx.store.get(item["id"])
    assert row.dedup_key == f"research:local-history:{period(fx.now)}" and row.cost_tokens == 120
    assert json.loads(row.success_check) == {"kind": "result_field", "field": "finding"}
    concern = fx.mind.concerns.by_intention(row.id)
    assert concern is not None and concern.status == "intended" and concern.drive == "curiosity"

    fx.mind.bound(item["id"], "kanban:r1")
    done = fx.mind.outcomes.record(item["id"], status="done", hermes_ref="kanban:r1",
                                   summary="finding: The town was founded in 1851 by settlers from the valley.")
    assert done.outcome == "done" and done.verified == "check"
    hits = fx.ledger.search_sources("town founded 1851", contact_id=OWNER, session_id="a-later-session")
    findings = [hit for hit in hits if hit["session_id"] == "mind" and "What I learned about local history" in hit["content"]]
    assert findings and "1851" in findings[0]["content"] and findings[0]["role"] == "assistant"

    # satisfied: the interest is not researched again while the finding is recent
    assert fx.mind.concerns.by_key(row.dedup_key).status == "resolved"
    fourth, = await idle(fx)
    assert fourth["formed"] == [] and len(router.calls) == 1
    state = fx.mind.state()
    assert state["drives"]["curiosity"]["satiety"] > 0.9 and state["drives"]["curiosity"]["effective"] < 0.5
    assert state["concerns"]["broadcast"] == [] and state["interests"][0]["topic"] == "local history"
    assert fx.mind.stats()["mind_tokens"] == 120

    # a week later the interest is researched again, as a new intention
    fx.shift(days=8)
    fifth, = await idle(fx)
    assert [item["type"] for item in fifth["formed"]] == ["research"] and len(router.calls) == 2
    assert fx.store.get(fifth["formed"][0]["id"]).dedup_key == f"research:local-history:{period(fx.now)}"


async def test_a_satisfied_drive_holds_its_next_work_until_the_satiety_decays(fx):
    fx.mind.add_interest("bees")
    fx.mind.add_interest("tides")
    first, = await idle(fx)
    assert [item["type"] for item in first["formed"]] == ["research", "research"]     # templates: no call cap
    assert first["below_threshold"] == 0                                              # both eligible, nothing satiates
    item = next(item for item in fx.mind.dispatch() if "bees" in item["title"])
    fx.mind.bound(item["id"], "kanban:b1")
    fx.mind.outcomes.record(item["id"], status="done", hermes_ref="kanban:b1", summary="finding: bees dance.")
    assert fx.mind.state()["drives"]["curiosity"]["satiety"] == 1.0
    fx.mind.add_interest("kilns")
    third, = await idle(fx)
    assert third["formed"] == [] and third["below_threshold"] == 1              # satisfied: kilns waits
    assert fx.mind.concerns.by_key(f"research:kilns:{period(fx.now)}").status == "open"
    fx.shift(hours=16)
    fourth, = await idle(fx)
    assert [item["type"] for item in fourth["formed"]] == ["research"]        # the satiety decayed
    assert "kilns" in fx.store.get(fourth["formed"][0]["id"]).description


async def test_a_fulfilled_commitment_does_not_hold_the_next_obligation(fx):
    fx.commitments.create(person_id=OWNER, description="send the notes",
                          due_at=(fx.now + timedelta(minutes=1)).isoformat())
    fx.shift(minutes=10)
    first, = await idle(fx)
    item, = fx.mind.dispatch()
    fx.mind.bound(item["id"], "kanban:c1")
    fx.mind.outcomes.record(item["id"], status="done", hermes_ref="kanban:c1", summary="sent the notes")
    assert fx.mind.state()["drives"]["duty"]["satiety"] == 1.0
    fx.commitments.create(person_id=OWNER, description="pay the invoice", priority=90,
                          due_at=(fx.now + timedelta(minutes=1)).isoformat())
    fx.shift(minutes=10)
    second, = await idle(fx)
    assert [item["type"] for item in second["formed"]] == ["commitment_overdue"]   # owed: not held by satiety
    assert second["formed"][0]["decision"] == "act" and second["below_threshold"] == 0
    assert [item["title"] for item in fx.mind.dispatch()] == ["Overdue: pay the invoice"]


async def test_a_new_week_does_not_duplicate_research_under_way_or_just_settled(fx):
    from datetime import datetime, timezone
    fx.now = datetime(2026, 9, 27, 23, 50, tzinfo=timezone.utc)          # Sunday, ten minutes before week 40
    fx.mind.add_interest("tides")
    first, = await idle(fx)
    formed, = first["formed"]
    row = fx.store.get(formed["id"])
    assert row.dedup_key == "research:tides:2026w39" and row.dedup_base == "research:tides"
    item, = fx.mind.dispatch()
    fx.mind.bound(item["id"], "kanban:t1")
    fx.shift(minutes=20)                                                    # Monday: a new period key
    assert period(fx.now) == "2026w40"
    second, = await idle(fx)
    assert second["formed"] == [] and second["drives"]["raised"] == 0 and fx.mind.dispatch() == []
    assert fx.mind.concerns.by_key("research:tides:2026w40") is None      # not even raised
    fx.mind.outcomes.record(item["id"], status="done", hermes_ref="kanban:t1", summary="finding: twice a day.")
    fx.shift(days=2)
    third, = await idle(fx)
    assert third["formed"] == [] and third["drives"]["raised"] == 0       # settled across the week boundary
    fx.shift(days=6)
    fourth, = await idle(fx)
    assert [item["type"] for item in fourth["formed"]] == ["research"]    # re-armed a week after the finding
    assert fx.store.get(fourth["formed"][0]["id"]).dedup_key == f"research:tides:{period(fx.now)}"


async def test_turning_a_drive_off_cancels_its_waiting_work_including_goal_steps(fx):
    router = DeliberationRouter({"beekeeping": goal("beekeeping", horizon_days=5, tasks=2)})
    fx.mind.router = router
    fx.mind.add_interest("beekeeping")
    fx.mind.add_interest("tides")
    fx.commitments.create(person_id="p-02", description="deliver the draft", priority=90,
                          due_at=(fx.now + timedelta(minutes=1)).isoformat(),
                          metadata={"kind": "deliverable", "content": "Here is the draft."})
    fx.shift(minutes=10)
    first, second, third = await idle(fx, 3)
    goal_row = next(fx.store.get(item["id"]) for s in (first, second, third) for item in s["formed"] if item["kind"] == "goal")
    waiting = [row for row in fx.store.intentions(status=["approved"], limit=20) if row.drive == "curiosity"]
    assert {row.kind for row in waiting} == {"goal", "task"} and len(waiting) == 3      # the goal, its step, tides
    notice, = [row for row in fx.store.intentions(status=["approved"], kind=["message"], limit=20)]
    assert notice.type == "ask_notice" and notice.drive == "upkeep"          # the contact message asked; the owner is told
    fx.config["drives"] = {"curiosity": 0.0, "upkeep": 0.0}
    fx.restart()                                                            # the owner's setting, after a restart
    assert fx.mind.drive_weights["curiosity"] == 0.0 and fx.mind.drive_weights["upkeep"] == 0.0
    summary, = await idle(fx)
    assert summary["invalidated"] == 3 and fx.mind.dispatch() == [] and fx.mind.goals.open() == []
    for row in (fx.store.get(row.id) for row in waiting):
        assert row.status == "cancelled" and row.verdict is None and "curiosity drive is off" in row.cancelled_reason
    assert summary["formed"] == [] and summary["drives"]["levels"]["curiosity"] == 0.0
    assert fx.store.get(goal_row.id).status == "cancelled"
    assert fx.store.get(notice.id).status == "approved"                     # reporting is not drive work
    assert [item["id"] for item in await fx.mind.outbox_ready()] == [notice.id]


async def test_without_a_router_the_research_task_is_a_template_and_deliberation_off_never_calls(fx):
    fx.mind.add_interest("tide tables")
    summary, = await idle(fx)
    assert [item["type"] for item in summary["formed"]] == ["research"] and summary["model_calls"] == 0
    body = fx.mind.dispatch()[0]["body"]
    assert "Research 'tide tables'" in body and "Reason: curiosity drive" in body

    quiet = Fixture(fx.state / "quiet", config={"faculties": {"deliberation": False}})
    router = DeliberationRouter()
    quiet.mind.router = router
    quiet.mind.add_interest("tide tables")
    summary = await quiet.mind.tick(force=True)
    assert [item["type"] for item in summary["formed"]] == ["research"] and router.calls == []
    quiet.store.close()


async def test_the_per_tick_call_cap_holds_and_the_rest_wait(fx):
    router = DeliberationRouter()
    fx.mind.router = router
    for topic in ("bees", "tides", "kilns", "maps", "yeast"):
        fx.mind.add_interest(topic)
    first, = await idle(fx)
    assert first["model_calls"] == 1 and len(router.calls) == 1 and len(first["formed"]) == 1
    assert fx.mind.concerns.count("intended") == 1 and fx.mind.concerns.count("open") == 4
    second, third = await idle(fx, 2)
    assert len(router.calls) == 3 and second["model_calls"] == 1 and third["model_calls"] == 1
    formed = [item for summary in (first, second, third) for item in summary["formed"]]
    assert len(formed) == 3 and {item["type"] for item in formed} == {"research"}
    # the concurrency budget defers the third; deferred intentions wait, they are not errors
    assert [item["decision"] for item in formed] == ["act", "act", "defer"]


async def test_a_failed_deliberation_call_falls_back_to_the_template(fx):
    router = DeliberationRouter(fail=True)
    fx.mind.router = router
    fx.mind.add_interest("kilns")
    summary, = await idle(fx)
    assert [item["type"] for item in summary["formed"]] == ["research"] and len(router.calls) == 1
    assert fx.mind.state()["deliberation"]["last_error"] == "RuntimeError"
    assert "Research 'kilns'" in fx.mind.dispatch()[0]["body"]


async def test_a_deliberated_note_is_recorded_and_settles_at_once(fx):
    router = DeliberationRouter({"local history": {
        "kind": "note", "title": "Already answered", "body": "The mill's founding date is already in memory: 1851."}})
    fx.mind.router = router
    fx.mind.add_interest("local history")
    first, = await idle(fx)
    note, = first["formed"]
    assert note["kind"] == "note" and note["status"] == "done" and first["model_calls"] == 1
    row = fx.store.get(note["id"])
    assert row.outcome == "done" and row.verified == "none" and row.result == "The mill's founding date is already in memory: 1851."
    assert row.expectation_id is None and fx.mind.dispatch() == []
    concern = fx.mind.concerns.by_intention(row.id)
    assert concern.status == "resolved" and fx.mind.concerns.count("intended") == 0
    hits = fx.ledger.search_sources("mill founding 1851", contact_id=OWNER, session_id="later")
    assert any("What I learned about local history" in hit["content"] for hit in hits)
    second, third = await idle(fx, 2)
    assert second["formed"] == [] and third["formed"] == [] and len(router.calls) == 1   # settled
    fx.shift(days=3)
    assert (await idle(fx))[0]["formed"] == [] and fx.mind.state()["concerns"] == {
        "open": 0, "intended": 0, "broadcast": []}


async def test_a_topic_that_already_had_a_goal_gets_one_task_and_one_call(fx):
    router = DeliberationRouter({"beekeeping": goal("beekeeping", horizon_days=2, tasks=1)})
    fx.mind.router = router
    fx.mind.add_interest("beekeeping")
    first, = await idle(fx)
    goal_row = fx.store.get(next(item["id"] for item in first["formed"] if item["kind"] == "goal"))
    fx.shift(days=3)
    second, = await idle(fx)                       # the horizon passed: the goal is closed, its key stays
    assert fx.store.get(goal_row.id).status == "expired" and fx.mind.goals.open() == []
    fx.shift(days=6)                               # the settled week is over: the interest re-arms
    third, = await idle(fx)
    formed = [item for item in third["formed"] if item["type"] == "research"]
    assert len(formed) == 1 and formed[0]["kind"] == "task" and len(router.calls) == 2
    assert "Do not propose a goal" in router.prompts[-1] and fx.mind.goals.open() == []
    fourth, fifth = await idle(fx, 2)
    assert fourth["formed"] == [] and fifth["formed"] == [] and len(router.calls) == 2   # no re-deliberation


async def test_a_deliberation_that_forms_nothing_is_charged_and_bounded(fx, monkeypatch):
    router = DeliberationRouter()
    fx.mind.router = router
    fx.mind.add_interest("tides")

    async def no_row(candidate, score, now):
        return None
    monkeypatch.setattr(fx.mind, "_form", no_row)
    summaries = await idle(fx, 6)
    assert all(summary["formed"] == [] for summary in summaries)
    assert len(router.calls) == 4                                          # the thought budget bounds the retries
    concern = fx.mind.concerns.by_key(f"research:tides:{period(fx.now)}")
    assert concern.status == "open" and concern.thoughts_spent == 4 and concern.exhausted
    assert fx.mind.broadcast() == [] and fx.mind.dispatch() == []
    assert fx.mind.stats()["mind_tokens"] == 4 * 100                       # every call is on the books
    notes = [row for row in fx.store.intentions(kind=["note"], limit=20) if row.type == "deliberation"]
    assert len(notes) == 4 and all(row.outcome == "done" and row.cost_tokens == 100 for row in notes)


# ---------------------------------------------------------------------------
# Goals: satisfied, expired at the horizon, a third not adopted while two are open
# ---------------------------------------------------------------------------

async def test_goal_lifecycle(fx):
    router = DeliberationRouter({
        "beekeeping": goal("beekeeping", check={"kind": "steps_done", "count": 1}, horizon_days=5, tasks=2),
        "astronomy": goal("astronomy", check={"kind": "result_field", "field": "impossible"}, horizon_days=1, tasks=1),
        "pottery": goal("pottery"),
        "This is the next step of an adopted goal": research(
            "beekeeping step", body="Read the hive basics; report finding: <text>."),
    })
    fx.mind.router = router
    fx.mind.add_interest("beekeeping")
    first, = await idle(fx)
    adopted = [item for item in first["formed"] if item["kind"] == "goal"]
    assert len(adopted) == 1 and adopted[0]["type"] == "goal" and adopted[0]["status"] == "approved"
    goal_a = fx.store.get(adopted[0]["id"])
    assert goal_a.kind == "goal" and goal_a.due_at is not None and goal_a.expires_at == goal_a.due_at
    assert (goal_a.due_at - fx.now).days == 5 and json.loads(goal_a.success_check) == {"kind": "steps_done", "count": 1}
    assert fx.mind.dispatch() == []                                            # a goal owns no Hermes object
    assert len(fx.mind.goals.open()) == 1 and first["goals"]["open"] == 0      # adopted after the goals were tended
    assert "Working toward: Goal: Learn about beekeeping (0/2 steps" in fx.mind.section()

    second, = await idle(fx)
    steps = [item for item in second["formed"] if item["type"] == "goal_step"]
    assert len(steps) == 1 and second["model_calls"] == 1
    queue = fx.mind.dispatch()
    assert len(queue) == 1 and queue[0]["goal_mode"] is True and queue[0]["goal_max_turns"] == 12
    assert queue[0]["parent_goal_id"] == goal_a.id and "Read the hive basics" in queue[0]["body"]
    third, = await idle(fx)
    assert [item["type"] for item in third["formed"]] == []                    # one step at a time
    fx.mind.bound(queue[0]["id"], "kanban:s1")
    done = fx.mind.outcomes.record(queue[0]["id"], status="done", hermes_ref="kanban:s1",
                                   summary="finding: a hive needs a queen, workers and a dry box.")
    assert done.outcome == "done" and done.parent_goal_id == goal_a.id
    fourth, = await idle(fx)
    assert fourth["goals"]["closed"] == [{"id": goal_a.id, "outcome": "done", "why": "the success check passed",
                                          "steps_retired": 0}]
    goal_a = fx.store.get(goal_a.id)
    assert goal_a.status == "done" and goal_a.outcome == "done" and goal_a.verified == "check"
    assert fx.mind.goals.open() == []

    # a goal whose check cannot pass expires at its horizon; a third is not adopted while two are open
    # (the satisfied curiosity drive holds new work until its satiety decays: 16 h later it is nearly gone)
    fx.shift(hours=16)
    fx.mind.add_interest("astronomy")
    fx.mind.add_interest("pottery")
    for _ in range(4):                                # one deliberation call per tick: the steps compete too
        summary, = await idle(fx)
        if len(fx.mind.goals.open()) == 2:
            break
    assert len(fx.mind.goals.open()) == 2 and fx.mind.goals.may_adopt() is False
    fx.mind.add_interest("weaving")
    third_proposal = []
    for _ in range(4):
        summary, = await idle(fx)
        third_proposal += [item for item in summary["formed"] if item["type"] == "research"]
        if third_proposal:
            break
    assert third_proposal and third_proposal[0]["kind"] == "task"              # the proposal became one task
    assert len(fx.mind.goals.open()) == 2 and "Do not propose a goal" in router.prompts[-1]

    goal_b = next(g for g in fx.mind.goals.open() if "astronomy" in g.description)
    fx.shift(days=2)
    eighth, = await idle(fx)
    closed = {item["id"]: item for item in eighth["goals"]["closed"]}
    assert closed[goal_b.id]["outcome"] == "expired" and closed[goal_b.id]["why"] == "the horizon passed"
    row = fx.store.get(goal_b.id)
    assert row.status == "expired" and row.outcome == "expired" and row.verdict is None
    assert len(fx.mind.goals.open()) == 1 and fx.mind.goals.may_adopt() is True


async def test_a_goal_awaiting_approval_owns_no_work_until_the_owner_says_yes(fx):
    for index in range(3):                          # three internal failures trip the breaker: internal work asks
        row, _ = fx.store.create_intention(kind="task", type="research", title=f"Research: r{index}", drive="curiosity",
                                           cls="internal", decision="act", decision_reason="r", status="dispatched",
                                           dedup_key=f"research:r{index}", hermes_kind="kanban", created_at=fx.now)
        fx.mind.outcomes.record(row.id, status="failed", summary="no sources", error="timeout")
    assert fx.mind.authority.breaker_state("internal", fx.now)["tripped"] is True
    router = DeliberationRouter({"beekeeping": goal("beekeeping", horizon_days=5, tasks=2)})
    fx.mind.router = router
    fx.mind.add_interest("beekeeping")
    first, = await idle(fx)
    adopted = next(item for item in first["formed"] if item["kind"] == "goal")
    assert adopted["decision"] == "ask" and adopted["status"] == "asked"
    goal_row = fx.store.get(adopted["id"])
    assert len(fx.mind.goals.open()) == 1 and fx.mind.goals.approved() == []
    fx.mind.reset("internal")                       # the breaker resets; the goal is still waiting for the owner
    second, third = await idle(fx, 2)
    assert second["goals"]["steps_raised"] == 0 and third["goals"]["steps_raised"] == 0
    assert fx.mind.dispatch() == [] and len(router.calls) == 1
    assert fx.mind.answer(goal_row.ask_code, yes=True).status == "approved"
    fourth, = await idle(fx)
    assert fourth["goals"]["steps_raised"] == 1
    steps = [item for item in fourth["formed"] if item["type"] == "goal_step"]
    assert len(steps) == 1 and steps[0]["status"] == "approved"
    queue = fx.mind.dispatch()
    assert len(queue) == 1 and queue[0]["parent_goal_id"] == goal_row.id and queue[0]["goal_mode"] is True


async def test_a_closed_goal_takes_its_pending_steps_with_it(fx):
    router = DeliberationRouter({"astronomy": goal("astronomy", check={"kind": "result_field", "field": "impossible"},
                                                   horizon_days=1, tasks=3)})
    fx.mind.router = router
    fx.mind.add_interest("astronomy")
    first, = await idle(fx)
    goal_row = fx.store.get(next(item["id"] for item in first["formed"] if item["kind"] == "goal"))
    second, = await idle(fx)
    step = fx.store.get(next(item["id"] for item in second["formed"] if item["type"] == "goal_step"))
    assert step.status == "approved" and [item["id"] for item in fx.mind.dispatch()] == [step.id]
    fx.shift(hours=30)                              # past the horizon, inside the step's own 48 h window
    third, = await idle(fx)
    closed, = third["goals"]["closed"]
    assert closed["id"] == goal_row.id and closed["outcome"] == "expired" and closed["steps_retired"] == 1
    step = fx.store.get(step.id)
    assert step.status == "cancelled" and step.outcome == "cancelled" and step.verdict is None
    assert "goal closed" in step.cancelled_reason
    assert fx.mind.dispatch() == [] and fx.mind.concerns.by_key(f"goal:{goal_row.id}:step:1").status == "dropped"
    assert third["formed"] == []
    fourth, = await idle(fx)                        # the interest is still alive: one plain task, no second goal
    assert [(item["type"], item["kind"]) for item in fourth["formed"]] == [("research", "task")]
    assert fx.mind.goals.open() == [] and all(item["parent_goal_id"] is None for item in fx.mind.dispatch())


async def test_an_unformed_step_concern_dies_with_its_goal(fx):
    router = DeliberationRouter({"astronomy": goal("astronomy", check={"kind": "result_field", "field": "impossible"},
                                                   horizon_days=1, tasks=3)})
    fx.mind.router = router
    fx.mind.add_interest("astronomy")
    first, = await idle(fx)
    goal_row = fx.store.get(next(item["id"] for item in first["formed"] if item["kind"] == "goal"))
    fx.mind.deliberation.max_calls_per_tick = 0     # the call cap is spent every tick: the step concern waits
    second, = await idle(fx)
    concern = fx.mind.concerns.by_key(f"goal:{goal_row.id}:step:1")
    assert second["goals"]["steps_raised"] == 1 and second["formed"] == [] and concern.status == "open"
    fx.shift(hours=30)
    fx.mind.deliberation.max_calls_per_tick = 1
    third, = await idle(fx)
    closed, = third["goals"]["closed"]
    assert closed["outcome"] == "expired" and closed["steps_retired"] == 1
    assert fx.mind.concerns.by_key(concern.dedup_key).status == "dropped"
    assert third["formed"] == [] and len(router.calls) == 1 and fx.mind.dispatch() == []


async def test_a_structured_step_result_satisfies_the_parent_goal_check(fx):
    router = DeliberationRouter({"pottery": goal("pottery", check={"kind": "result_field", "field": "glaze"},
                                                 horizon_days=3, tasks=2),
                                 "This is the next step of an adopted goal": research("pottery step")})
    fx.mind.router = router
    fx.mind.add_interest("pottery")
    first, = await idle(fx)
    goal_row = fx.store.get(next(item["id"] for item in first["formed"] if item["kind"] == "goal"))
    second, = await idle(fx)
    step = fx.store.get(next(item["id"] for item in second["formed"] if item["type"] == "goal_step"))
    fx.mind.bound(step.id, "kanban:p1")
    done = fx.mind.outcomes.record(step.id, status="done", hermes_ref="kanban:p1",
                                   summary="Read three sources; finding: cone 6 is the sweet spot.",
                                   result={"finding": "cone 6", "glaze": "tenmoku"})
    assert done.verified == "check" and done.result_metadata["result"] == {"finding": "cone 6", "glaze": "tenmoku"}
    assert fx.mind.goals.check(fx.store.get(goal_row.id), evaluate=fx.mind._evaluate_goal_check) is True
    third, = await idle(fx)
    assert third["goals"]["closed"][0]["outcome"] == "done" and fx.store.get(goal_row.id).verified == "check"


async def test_the_flat_priority_arm_adopts_no_goals_and_never_satiates(fx):
    flat = Fixture(fx.state / "flat", config={"faculties": {"drives": False},
                                              "drives": {"duty": 1.0, "curiosity": 0.2}})
    router = DeliberationRouter({"pottery": goal("pottery")})
    flat.mind.router = router
    flat.mind.add_interest("pottery")
    summary = await flat.mind.tick(force=True)
    assert flat.mind.drive_weights == {"duty": 1.0, "social": 1.0, "curiosity": 1.0, "mastery": 1.0, "upkeep": 1.0}
    assert [item["kind"] for item in summary["formed"]] == ["task"] and flat.mind.goals.open() == []
    item = flat.mind.dispatch()[0]
    flat.mind.bound(item["id"], "kanban:p1")
    flat.mind.outcomes.record(item["id"], status="done", summary="finding: clay.")
    curiosity = flat.mind.state()["drives"]["curiosity"]
    assert curiosity["weight"] == 1.0 and curiosity["effective"] == 1.0 and curiosity["satiety"] == 0.0
    flat.store.close()


# ---------------------------------------------------------------------------
# The off switch mid-episode
# ---------------------------------------------------------------------------

async def test_off_switch_mid_episode_leaves_no_new_effects(fx):
    router = DeliberationRouter()
    fx.mind.router = router
    for topic in ("bees", "tides"):
        fx.mind.add_interest(topic)
    fx.commitments.create(person_id=OWNER, description="send the notes",
                          due_at=(fx.now + timedelta(minutes=1)).isoformat())
    fx.shift(minutes=10)
    first, = await idle(fx)
    assert len(first["formed"]) == 2 and len(fx.mind.dispatch()) == 2      # the obligation and one research task
    before = fx.store.count()
    calls = len(router.calls)

    fx.mind.off(reason="mid-episode")
    assert fx.mind.dispatch() == [] and await fx.mind.outbox_ready() == []
    fx.shift(hours=1)
    fx.commitments.create(person_id=OWNER, description="another obligation",
                          due_at=(fx.now - timedelta(minutes=1)).isoformat())
    for summary in await idle(fx, 3):
        assert summary["skipped"] == "off" and summary["formed"] == [] and summary["model_calls"] == 0
    assert len(router.calls) == calls and fx.mind.dispatch() == [] and await fx.mind.outbox_ready() == []
    assert fx.store.count() == before + 1                                   # only the off-switch audit row
    assert fx.mind.concerns.count("open") == 1                              # the waiting concern is untouched
    guard = await fx.mind.guard(tool="kanban_create", args={"assignee": "protagine-act"}, run="mind")
    assert guard["allow"] is False


# ---------------------------------------------------------------------------
# Reconsideration, duty inputs from observations, mastery
# ---------------------------------------------------------------------------

async def test_active_intentions_are_reconsidered_only_on_a_matching_event(fx):
    fx.commitments.create(person_id=OWNER, description="send the notes",
                          due_at=(fx.now + timedelta(minutes=1)).isoformat())
    fx.mind.add_interest("tides")
    fx.shift(minutes=10)
    first, = await idle(fx)
    assert {item["type"] for item in first["formed"]} == {"commitment_overdue", "research"} and first["revised"] == 0
    overdue = next(item for item in first["formed"] if item["type"] == "commitment_overdue")
    second, = await idle(fx)
    assert second["revised"] == 0                                           # nothing new: no reconsideration
    fx.shift(hours=2)
    third, = await idle(fx)                                                 # the same obligation, new evidence
    assert third["revised"] == 1
    row = fx.store.get(overdue["id"])
    assert row.status == "approved" and any(item.action == "reconsidered" for item in fx.store.get_history(row.id))
    assert any("overdue by 2 h" in item for item in row.context["evidence"])
    research_row = fx.store.get(next(item["id"] for item in first["formed"] if item["type"] == "research"))
    assert not any(item.action == "reconsidered" for item in fx.store.get_history(research_row.id))
    commitment_id = fx.commitments.get_pending_for_person(OWNER)[0]["id"] if fx.commitments.get_pending_for_person(OWNER) else \
        fx.commitments.list(status=["overdue"], limit=5)["commitments"][0]["id"]
    fx.commitments.resolve(commitment_id, outcome="done", note="sent", resolved_by="owner")
    fourth, = await idle(fx)
    assert fourth["invalidated"] == 1 and fx.store.get(overdue["id"]).status == "cancelled"


async def test_duty_reads_hermes_goals_and_stale_tasks_from_the_board_observations(fx):
    fx.mind.observe({"observed_at": fx.now.isoformat(), "board": "default", "counts": {"ready": 2},
                     "stale_tasks": [{"id": "t-1", "title": "Renew the domain", "assignee": "default", "status": "ready",
                                      "idle_s": 80 * 3600}],
                     "blocked_tasks": [],
                     "goals": [{"id": "g-1", "title": "finish the site", "assignee": "default", "status": "ready",
                                "idle_s": 30 * 3600, "goal_max_turns": 9},
                               {"id": "g-2", "title": "fresh goal", "assignee": "default", "status": "ready", "idle_s": 60}],
                     "mind_tasks": []})
    summary, = await idle(fx)
    assert sorted(item["type"] for item in summary["formed"]) == ["goal_stalled", "stale_task"]
    assert all(item["drive"] == "duty" and item["kind"] == "message" for item in summary["formed"])
    ready = await fx.mind.outbox_ready()
    assert {item["recipient"] for item in ready} == {OWNER}
    assert any("finish the site" in item["text"] for item in ready)


async def test_a_failure_cluster_raises_mastery_and_an_investigation_is_formed(fx):
    router = DeliberationRouter()
    fx.mind.router = router
    for index in range(2):
        row, _ = fx.store.create_intention(kind="task", type="research", title="Research: tide tables", drive="curiosity",
                                           cls="internal", decision="act", decision_reason="r", status="dispatched",
                                           dedup_key=f"research:tide-tables:w{index}", hermes_kind="kanban",
                                           context={"topic": "tide tables"}, created_at=fx.now - timedelta(days=1))
        fx.mind.outcomes.record(row.id, status="failed", summary="no sources reachable", error="timeout")
    summary, = await idle(fx)
    assert [item["type"] for item in summary["formed"]] == ["mastery_investigation"]
    assert summary["drives"]["levels"]["mastery"] > 0 and "the same work failed 2 times" in router.prompts[0]
    queued = fx.mind.dispatch()[0]
    assert queued["type"] == "mastery_investigation" and queued["drive"] == "mastery"
    assert "Reason: mastery drive" in queued["body"] and "timeout" in queued["body"]


# ---------------------------------------------------------------------------
# The Mind section, the broadcast flag, the routes
# ---------------------------------------------------------------------------

async def test_mind_section_and_broadcast(fx):
    assert fx.mind.section() == ""
    for topic in ("bees", "tides", "kilns", "maps"):
        fx.mind.add_interest(topic)
    fx.commitments.create(person_id="p-02", description="deliver the draft", priority=90,
                          due_at=(fx.now + timedelta(minutes=1)).isoformat(),
                          metadata={"kind": "deliverable", "content": "Here is the draft."})
    fx.shift(minutes=10)
    summary, = await idle(fx)
    asked = [item for item in summary["formed"] if item["decision"] == "ask"]
    assert asked                                                            # a message to a contact asks first
    section = fx.mind.section()
    assert section.startswith("On my mind: ") and len(section) <= 600 and "Waiting for your say on: [" in section
    assert len(fx.mind.broadcast()) == 2                                    # five concerns, the top three intended
    fx.mind.faculties["broadcast"] = False
    assert "On my mind" not in fx.mind.section() and fx.mind.broadcast() == []
    fx.mind.faculties["broadcast"] = True
    fx.mind.off()
    assert fx.mind.section() == "" and fx.mind.broadcast() == []


async def test_concern_goal_and_interest_routes(fx):
    app = fx.app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://mind") as client:
        seeded = await client.post("/v1/mind/interests", headers=AUTH, json={"topic": "local history", "why": "seeded"})
        assert seeded.status_code == 200 and seeded.json()["topic"] == "local history"
        assert (await client.post("/v1/mind/interests", headers=AUTH, json={"topic": "   "})).status_code == 422
        await client.post("/v1/mind/tick", headers=AUTH)
        concerns = (await client.get("/v1/mind/concerns", headers=AUTH)).json()
        assert concerns["concerns"] == [] or concerns["concerns"][0]["status"] in {"open", "intended"}
        assert "drives" in concerns and set(concerns["drives"]) == {"duty", "social", "curiosity", "mastery", "upkeep"}
        goals = (await client.get("/v1/mind/goals", headers=AUTH)).json()
        assert goals == {"goals": [], "open_goals": 2, "text": "(no open goals)"}
        state = (await client.get("/v1/mind/state", headers=AUTH)).json()
        assert state["faculties"]["drives"] is True and state["interests"] == [{"topic": "local history", "weight": 1.0}]
        assert state["deliberation"]["available"] is False
