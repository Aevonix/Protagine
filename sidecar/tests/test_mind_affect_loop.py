"""The agent's feelings wired into the mind, end to end in the sidecar (build plan M6, part B).

Owner statements reach affect through the existing appraisal call (its ``outcomes``); the four
consumers read one ``AffectView``: the strategy switch (a note in the section and the task body,
a different approach from deliberation or one question, and the refusal to re-dispatch an
identical failing plan), overload (optional work waits), priority (worry lifts owed duty) and
satiation (optional owner nudges wait after dismissals; a hard promise still fires). The
``full-affect`` arm changes nothing and writes nothing; the rules arm writes no state.
"""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from protagine.api.routers import mind as mind_router
from protagine.mind import Mind
from protagine.mind.affect import AffectView, Frustration, plan_hash
from protagine.mind.concerns import Concern
from protagine.mind.deliberate import (
    ASK_RESPONSE_SCHEMA, RESPONSE_SCHEMA, SYSTEM, TASK, Deliberation, apply_proposal, build_prompt, parse_proposal,
    template,
)
from protagine.mind.rank import Candidate, eligible, pick, rank, score, threshold_for
from protagine.self_model.appraisals import AppraisalStore
from test_mind_loop import AUTH, OWNER, Fixture
from test_turn_source_evidence import source_app  # noqa: F401  (fixture)

TOPIC = "quarterly figures"
APPROACH = "the archive export"
NOTE = (f"Prior attempts at {TOPIC} failed 2 times using {APPROACH}; "
        "choose a different approach or ask one question.")


# ---------------------------------------------------------------------------
# Rank and deliberation units (I-4)
# ---------------------------------------------------------------------------

def candidate(**fields) -> Candidate:
    values = dict(type="commitment_overdue", drive="duty", kind="task", title="t", dedup_key="k", salience=1.0,
                  cost=0.0, priority=0.7)
    values.update(fields)
    return Candidate(**values)


def failing(**fields) -> Frustration:
    values = dict(topic=TOPIC, key="affect.frustration:quarterly-figures", level=0.6, failures=2,
                  approaches=(APPROACH,), pitfalls=("the archive scrape returned stale data",),
                  body_hashes=frozenset(), causes=())
    values.update(fields)
    return Frustration(**values)


def concern(summary=TOPIC, **fields) -> Concern:
    return Concern(id="c-1", drive=fields.pop("drive", "curiosity"), kind="interest", summary=summary,
                   dedup_key="research:x", salience=0.8, sources=["interest:x"], **fields)


def test_affect_view_scales_the_score_and_the_threshold():
    view = AffectView(route={}, owner_id=OWNER, worry=0.4, curiosity=0.3)
    duty = candidate()
    research = candidate(type="research", drive="curiosity", dedup_base="research:x", priority=0.5)
    assert score(duty, affect=view) == pytest.approx(1.2)                   # owed duty x (1 + 0.5 worry)
    assert score(research, drives={"curiosity": 1.0}, affect=view) == pytest.approx(1.3)
    assert score(duty) == score(duty, affect=None) == 1.0
    assert threshold_for(duty, 0.6, None, affect=view) == pytest.approx(0.6)
    overloaded = replace(view, overloaded=True)
    assert threshold_for(research, 0.6, {"curiosity": 0.5}, affect=overloaded) == math.inf
    assert eligible([research], threshold=0.6, affect=overloaded) == []     # postponed: ineligible whatever the score
    assert eligible([research], threshold=0.6, affect=view) == [(research, pytest.approx(1.3))]
    assert pick([research], threshold=0.6, affect=overloaded)[0] is None
    assert [c for c, _ in rank([research, duty], affect=view)] == [research, duty]


def test_satiation_raises_the_bar_only_for_owner_facing_optional_messages():
    view = AffectView(route={}, owner_id=OWNER, satiated=True, boost=0.5)
    nudge = candidate(type="commitment_reminder", kind="message", recipient=OWNER, priority=0.3, salience=0.8)
    promise = replace(nudge, priority=0.7)
    contact = replace(nudge, recipient="p-02")
    assert threshold_for(nudge, 0.6, None, affect=view) == pytest.approx(0.9)
    assert eligible([nudge], threshold=0.6, affect=view) == []
    assert eligible([promise], threshold=0.6, affect=view) == [(promise, pytest.approx(0.8))]
    assert threshold_for(contact, 0.6, None, affect=view) == pytest.approx(0.6)


def test_a_zero_weight_drive_stays_off_under_postponement():
    view = AffectView(route={}, owner_id=OWNER, overloaded=True)
    research = candidate(type="research", drive="curiosity", dedup_base="research:x")
    assert eligible([research], threshold=0.6, drives={"curiosity": 0.0}, affect=view) == []


def test_the_candidate_carries_the_affect_question_through_its_detail():
    shaped = candidate(affect_ask="quarterly figures failed 2 times with this same plan; run it again anyway?")
    assert Candidate.from_detail(shaped.as_detail()).affect_ask == shaped.affect_ask
    assert candidate().affect_ask == ""


def test_ask_is_offered_only_with_the_prior_attempts():
    """The system prompt and the default schema are the deliberation contract every arm shares (task,
    goal or note); kind ``ask`` exists only in the schema sent with a failing topic's prompt."""
    assert '"ask"' not in SYSTEM and 'kind ("task", "goal" or "note")' in SYSTEM
    assert RESPONSE_SCHEMA["schema"]["properties"]["kind"]["enum"] == ["task", "goal", "note"]
    assert ASK_RESPONSE_SCHEMA["schema"]["properties"]["kind"]["enum"] == ["task", "goal", "note", "ask"]
    assert {k: v for k, v in ASK_RESPONSE_SCHEMA["schema"]["properties"].items() if k != "kind"} == {
        k: v for k, v in RESPONSE_SCHEMA["schema"]["properties"].items() if k != "kind"}
    text = json.dumps({"kind": "ask", "title": "Ask", "body": "Which export is current?"})
    assert parse_proposal(text) is None
    assert parse_proposal(text, ask=True)["kind"] == "ask"
    research = candidate(type="research", drive="curiosity", open_ended=True, topic=TOPIC, text="")
    unasked = apply_proposal(replace(research), concern(), json.loads(text))
    assert unasked.affect_ask == "" and "Research 'quarterly figures'" in unasked.text, "no failing topic, no ask"


class AskRouter:
    """Always answers with one question for the owner; records what it was offered."""

    supports_function_routing = True

    def function_deadline_seconds(self, *, context=None):
        return 20

    async def complete(self, messages, *, context=None, **_):
        self.system, self.prompt, self.schema = messages[0]["content"], messages[1]["content"], context["response_schema"]
        return SimpleNamespace(content=json.dumps({"kind": "ask", "title": "Ask",
                                                   "body": "Which source should I use for this?"}),
                               usage={"total_tokens": 10})


@pytest.mark.parametrize("faculties", [{}, {"affect": False}], ids=["full", "full-affect"])
async def test_without_a_failing_topic_no_arm_offers_or_honours_an_ask(tmp_path, monkeypatch, faculties):
    """The deliberation call is the same in every arm until a topic fails: the same system prompt and
    schema, and a model that answers ``ask`` anyway gets the template, not an owner ask."""
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fx = arm(tmp_path, "arm", **faculties)
    fx.mind.router = fx.mind.deliberation.router = router = AskRouter()
    fx.mind.add_interest(TOPIC)
    summary = await fx.mind.tick(force=True)
    assert router.system == SYSTEM and router.schema == RESPONSE_SCHEMA and "ask" not in router.prompt
    research, = [item for item in summary["formed"] if item["type"] == "research"]
    row = fx.store.get(research["id"])
    assert (research["decision"], row.status) == ("act", "approved") and "Which source" not in row.decision_reason
    assert fx.mind.deliberation.last_error == "unparsable"
    fx.store.close()


def test_build_prompt_names_the_failure_its_pitfalls_and_asks_for_another_approach():
    research = candidate(type="goal_step", drive="curiosity", open_ended=True, topic=TOPIC, parent_goal_id="g-1")
    prompt = build_prompt(concern(), research, open_goals=1, may_adopt_goal=False, failing=failing())
    assert "Prior attempts: " + NOTE in prompt
    assert "Pitfalls: the archive scrape returned stale data" in prompt
    assert (f'Propose an approach other than: {APPROACH}, or return kind "ask" with the one question for the owner '
            'as the body.') in prompt
    plain = build_prompt(concern(), research, open_goals=1, may_adopt_goal=False)
    assert "Prior attempts" not in plain and "Pitfalls" not in plain


def test_template_and_proposals_carry_the_note_and_ask_keeps_a_runnable_body():
    research = candidate(type="research", drive="curiosity", open_ended=True, topic=TOPIC, text="")
    shaped = template(replace(research), concern(), failing())
    assert shaped.text.endswith("\n\n" + NOTE) and "Research 'quarterly figures'" in shaped.text
    task = apply_proposal(replace(research), concern(), {"kind": "task", "title": "Try the ledger",
                                                          "body": "Read the ledger database instead."},
                          failing=failing())
    assert task.kind == "task" and task.text.endswith(NOTE) and task.affect_ask == ""
    asked = apply_proposal(replace(research), concern(), {"kind": "ask", "title": "Ask",
                                                           "body": "Which export holds the current figures?"},
                           failing=failing())
    assert asked.kind == "task" and asked.affect_ask == "Which export holds the current figures?"
    assert "Research 'quarterly figures'" in asked.text and asked.text.endswith(NOTE)
    assert asked.success_check == {"kind": "result_field", "field": "finding"}


async def test_form_refuses_to_redispatch_an_identical_failing_plan():
    research = candidate(type="research", drive="curiosity", open_ended=True, topic=TOPIC, text="")
    first = template(replace(research), concern())
    repeat = failing(body_hashes=frozenset({plan_hash(first.text)}))
    deliberation = Deliberation(None)
    shaped = await deliberation.form(concern(), replace(research), failing=repeat)
    assert shaped.affect_ask == f"{TOPIC} failed 2 times with this same plan; run it again anyway?"
    assert shaped.text.endswith(NOTE) and plan_hash(shaped.text) == plan_hash(first.text)
    other = await deliberation.form(concern(), replace(research), failing=failing())
    assert other.affect_ask == ""
    # A closed-form task gets the note and keeps its obligation; a message never gets the note.
    owed = candidate(text="Fulfil the overdue commitment to the owner: send the quarterly figures.")
    assert (await deliberation.form(concern(), replace(owed), failing=failing())).text.endswith(NOTE)
    word = candidate(kind="message", text="Reminder: send the quarterly figures.")
    assert (await deliberation.form(concern(), replace(word), failing=failing())).text == word.text


async def test_an_identical_failed_plan_is_refused_without_the_switch_and_no_note_is_added():
    research = candidate(type="research", drive="curiosity", open_ended=True, topic=TOPIC, text="")
    first = template(replace(research), concern())
    record = failing(level=None, body_hashes=frozenset({plan_hash(first.text)}))
    shaped = await Deliberation(None).form(concern(), replace(research), tried=(record,))
    assert shaped.affect_ask == f"{TOPIC} failed 2 times with this same plan; run it again anyway?"
    assert "Prior attempts" not in shaped.text, "no switch, no note: only the refusal"
    assert (await Deliberation(None).form(concern(), replace(research), tried=(failing(),))).affect_ask == ""


# ---------------------------------------------------------------------------
# The novel-topic input (P/api/routers/host.py)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case", ["owner_nothing_recalled", "owner_recalled", "guest"])
async def test_an_owner_turn_memory_knows_nothing_about_is_a_novel_topic(source_app, tmp_path, monkeypatch, case):
    from onekey import RequestAuthority
    from protagine.turns import TurnIdempotencyLedger
    monkeypatch.setenv("PROTAGINE_RECALL_RERANK", "off")
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", "contact-a")
    viewer = "contact-b" if case == "guest" else "contact-a"
    if case == "owner_recalled":
        TurnIdempotencyLedger(tmp_path / "turn-idempotency.db").record_source(
            "earlier", contact_id="contact-a", session_id="earlier", derive_claims=False,
            messages=[{"role": "user", "content": "The hydrofoil departure is Friday at nine."}])

    @source_app.middleware("http")
    async def auth(request, call_next):
        request.state.protagine_authority = RequestAuthority(
            principal_id="host", credential_id="key", scopes=frozenset({"context:read"}), viewer_person_id=viewer,
            person_ids=frozenset({viewer}), audiences=frozenset({"owner"}), authenticated=True)
        return await call_next(request)

    noted = []
    mind = SimpleNamespace(feelings=SimpleNamespace(note_novel_topic=lambda text: noted.append(text)),
                           section=lambda: "", broadcast=lambda: [])
    mind_router.set_mind(mind)
    try:
        async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
            response = await client.post("/v1/host/context/assemble", json={
                "identity": {"host_id": "test-host"}, "context": {"contact_id": viewer, "session_id": "later"},
                "incoming_message": {"role": "user", "content": "When is the hydrofoil departure?"}})
    finally:
        mind_router.set_mind(None)
    assert response.status_code == 200, response.text
    recalled = any(s["id"] == "protagine-memory" for s in response.json()["sections"])
    assert recalled is (case == "owner_recalled")
    assert noted == (["When is the hydrofoil departure?"] if case == "owner_nothing_recalled" else [])


async def test_the_novel_topic_hook_is_a_no_op_without_a_mind(monkeypatch):
    from protagine.api.routers import host
    mind_router.set_mind(None)
    host._mind_note_novel("anything")                 # nothing to note it on, nothing raised
    mind_router.set_mind(SimpleNamespace(feelings=SimpleNamespace(
        note_novel_topic=lambda text: (_ for _ in ()).throw(RuntimeError("boom")))))
    try:
        host._mind_note_novel("anything")             # a failing feeling never breaks context assembly
    finally:
        mind_router.set_mind(None)


# ---------------------------------------------------------------------------
# End to end: owner statements -> the appraisal call -> affect -> the consumers
# ---------------------------------------------------------------------------

class AppraisalRouter:
    """The appraisal call's controlled answer: ``outcomes`` read off the owner's words by fixed phrases
    (one entry per phrase occurrence); no observations, every supplied incident unchanged."""

    supports_function_routing = True

    def __init__(self, phrases):
        self.phrases, self.calls = dict(phrases), 0

    def function_deadline_seconds(self, *, context=None):
        return 20

    async def complete(self, messages, *, context=None, **_):
        self.calls += 1
        payload = json.loads(messages[-1]["content"])
        evidence = next(e for e in payload["evidence"] if e["current"])
        outcomes = []
        for phrase, (event, topic, approach) in self.phrases.items():
            outcomes += [{"event": event, "topic": topic, "approach": approach,
                          "support": [{"handle": evidence["handle"], "quote": phrase}]}] * evidence["text"].count(phrase)
        return SimpleNamespace(content=json.dumps({
            "observations": [], "outcomes": outcomes[:4],
            "incident_decisions": [{"record_id": i, "outcome": "unchanged"} for i in payload["incident_ids"]]}))


REPORTS = AppraisalRouter({
    "the archive export gave stale quarterly figures": ("failed", TOPIC, APPROACH),
    "the ledger figures were right": ("succeeded", TOPIC, "the ledger database"),
    "waved off your stretching nudge": ("dismissed", "stretching nudge", "a reminder"),
})


class AffectFixture(Fixture):
    """The mind-loop fixture with the owner's appraisal store wired as production wires it."""

    def build(self) -> Mind:
        self.appraisals = AppraisalStore(self.ledger, owner_id=OWNER, clock=lambda: self.now.timestamp())
        mind = Mind(config=self.config, store=self.store, state_dir=self.state, owner_id=OWNER,
                    commitments=self.commitments, feedback=self.feedback, expectations=self.expectations,
                    contacts=self.contacts, ledger=self.ledger, clock=lambda: self.now, backups=False,
                    persist=self.persisted.append, router=self.router, appraisals=self.appraisals)
        mind.digest_hour = 25
        return mind

    async def say(self, turn_id, text, *, appraise=True):
        """An owner statement, appraised by the (controlled) appraisal call as the worker would."""
        self.ledger.record_source(turn_id, contact_id=OWNER, session_id=f"session-{turn_id}", messages=[
            {"role": "user", "content": text}, {"role": "assistant", "content": "Noted."}],
            occurred_at=self.now.isoformat())
        while appraise and await self.appraisals.process_one(REPORTS):
            pass

    def affect_rows(self):
        return self.mind.mind_state.items("affect.")

    def owe(self, description, *, hours, priority=70, obligor="owner"):
        return self.commitments.create(person_id=OWNER, description=description, priority=priority,
                                       due_at=(self.now + timedelta(hours=hours)).isoformat(), source_type="cognition",
                                       metadata={"obligor": obligor}, allow_overdue=True)


@pytest.fixture
def ax(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fixture = AffectFixture(tmp_path)
    yield fixture
    mind_router.set_mind(None)
    fixture.store.close()


def arm(tmp_path, name, **faculties):
    fixture = AffectFixture(tmp_path / name, config={"faculties": faculties} if faculties else None)
    return fixture


async def test_owner_reported_failures_switch_strategy_and_a_verified_success_calms_it(ax):
    """Acceptance: two owner-reported failures of the archive export -> the note in the Mind section and
    in the task body, levels with cited causes on /v1/mind/state; a verified success halves frustration."""
    await ax.say("owner-1", "Ugh, the archive export gave stale quarterly figures again.")
    ax.shift(hours=1)
    await ax.say("owner-2", "Checked again: the archive export gave stale quarterly figures.")
    promise = ax.owe("Send the owner the quarterly figures", hours=-1, obligor="assistant")
    summary = await ax.mind.tick(force=True)
    assert summary["affect"]["switch"] == [TOPIC] and summary["appraisal_wait"] == {
        "waited_seconds": summary["appraisal_wait"]["waited_seconds"], "pending": 0, "running": 0}
    assert NOTE in ax.mind.section().splitlines()
    async with AsyncClient(transport=ASGITransport(app=ax.app()), base_url="http://mind") as client:
        state = (await client.get("/v1/mind/state", headers=AUTH)).json()["affect"]
    assert state["enabled"] is True and state["source"] == "state"
    frustration = state["levels"]["frustration"][0]
    assert frustration["topic"] == TOPIC and 0.5 <= frustration["level"] <= 0.7 and frustration["failures"] == 2
    assert frustration["approaches"] == [APPROACH]
    cited = [cause for cause in frustration["causes"] if cause.startswith("failed outcome:")]
    assert len(cited) == 2 and all(cause.endswith(f" via {APPROACH}") for cause in cited)
    formed, = [item for item in summary["formed"] if item["type"] == "commitment_overdue"]
    item, = ax.mind.dispatch()
    assert item["id"] == formed["id"] and item["body"].count(NOTE) == 1
    assert item["body"].startswith("Fulfil the overdue commitment to the owner: Send the owner the quarterly figures")

    ax.mind.bound(item["id"], "kanban:q1")
    ax.commitments.resolve(promise["id"], "done", note="the owner has the figures", resolved_by="owner")
    done = ax.mind.outcomes.record(item["id"], status="done", hermes_ref="kanban:q1", summary="sent from the ledger")
    assert done.verified == "check"
    after = await ax.mind.tick(force=True)
    assert after["affect"]["switch"] == []
    levels = ax.mind.state()["affect"]["levels"]
    assert levels["frustration"][0]["level"] == pytest.approx(frustration["level"] / 2, abs=0.01)
    assert levels["satisfaction"]["level"] == pytest.approx(0.3, abs=0.01)
    assert f"verified intention:{item['id']}:verified" in levels["satisfaction"]["causes"]
    section = ax.mind.section()
    assert "Prior attempts" not in section and "Mood: " in section and "frustrated about quarterly figures" in section


class GoalRouter:
    """Deliberation: adopts a goal on the topic, then proposes the archive export for each step until
    the prompt lists the failed attempts; then a different approach (or one question, ``ask=True``)."""

    supports_function_routing = True

    def __init__(self, *, ask=False):
        self.ask, self.prompts = ask, []

    def function_deadline_seconds(self, *, context=None):
        return 20

    async def complete(self, messages, *, context=None, **_):
        assert context["task"] == TASK
        prompt = messages[1]["content"]
        self.prompts.append(prompt)
        assert context["response_schema"] == (ASK_RESPONSE_SCHEMA if "Prior attempts:" in prompt else RESPONSE_SCHEMA)
        if "adopted goal" not in prompt:
            proposal = {"kind": "goal", "title": "Get the quarterly figures right", "body": "Pin down the figures.",
                        "goal": {"description": "Report the current quarterly figures.",
                                 "success_check": {"kind": "result_field", "field": "figures"},
                                 "horizon_days": 3, "tasks": 4}}
        elif "Prior attempts:" not in prompt:
            proposal = {"kind": "task", "title": "Scrape the archive export",
                        "body": "Scrape the archive export and report figures: <the numbers>."}
        elif self.ask:
            proposal = {"kind": "ask", "title": "Ask the owner", "body": "Which source holds the current figures?"}
        else:
            proposal = {"kind": "task", "title": "Read the ledger database",
                        "body": "Read the ledger database instead and report figures: <the numbers>."}
        proposal.setdefault("success_check", {"kind": "result_field", "field": "figures"})
        return SimpleNamespace(content=json.dumps(proposal), usage={"total_tokens": 50})


async def adopted_goal(fx, router):
    fx.mind.router = router
    fx.mind.add_interest(TOPIC)
    first = await fx.mind.tick(force=True)
    goal, = [item for item in first["formed"] if item["kind"] == "goal"]
    assert goal["decision"] == "act"
    return goal["id"]


async def failed_step(fx, number):
    summary = await fx.mind.tick(force=True)
    step, = [item for item in summary["formed"] if item["type"] == "goal_step"]
    row = fx.store.get(step["id"])
    assert row.dedup_key.endswith(f":step:{number}")
    item, = [queued for queued in fx.mind.dispatch() if queued["id"] == step["id"]]
    fx.mind.bound(item["id"], f"kanban:s{number}")
    fx.mind.outcomes.record(item["id"], status="failed", hermes_ref=f"kanban:s{number}",
                            summary="stale", error="the archive scrape returned stale data")
    fx.shift(minutes=10)
    return item


def no_mastery(fx):
    fx.config["drives"] = {"mastery": 0}
    fx.restart()


async def test_two_failed_steps_lead_to_a_different_approach_that_succeeds(ax):
    """Acceptance: steps 1 and 2 fail with the archive export; step 3's prompt names the failure and the
    pitfall, the model proposes another approach, it goes out with the note, succeeds, and calms it."""
    no_mastery(ax)
    router = GoalRouter()
    await adopted_goal(ax, router)
    first, second = await failed_step(ax, 1), await failed_step(ax, 2)
    assert first["body"] == second["body"] and "Prior attempts" not in first["body"]
    summary = await ax.mind.tick(force=True)
    step, = [item for item in summary["formed"] if item["type"] == "goal_step"]
    prompt = router.prompts[-1]
    assert "Prior attempts: Prior attempts at quarterly figures failed 2 times" in prompt
    assert "the archive scrape returned stale data" in prompt and 'or return kind "ask"' in prompt
    assert prompt.count("the archive scrape returned stale data") == 1         # a pitfall is also the lesson
    item, = [queued for queued in ax.mind.dispatch() if queued["id"] == step["id"]]
    assert "Read the ledger database instead" in item["body"] and item["body"].count(
        "Prior attempts at quarterly figures failed 2 times") == 1
    level = ax.mind.state()["affect"]["levels"]["frustration"][0]["level"]
    ax.mind.bound(item["id"], "kanban:s3")
    done = ax.mind.outcomes.record(item["id"], status="done", hermes_ref="kanban:s3", summary="done",
                                   result={"figures": "Q3: 1.2M"})
    assert done.verified == "check"
    await ax.mind.tick(force=True)
    assert ax.mind.state()["affect"]["levels"]["frustration"][0]["level"] == pytest.approx(level / 2, abs=0.01)


async def test_the_model_can_answer_with_one_question_for_the_owner(ax):
    """Acceptance (or an ask): the step is asked with the question, the ask notice lists it, and a yes
    approves the runnable body."""
    no_mastery(ax)
    await adopted_goal(ax, GoalRouter(ask=True))
    await failed_step(ax, 1)
    await failed_step(ax, 2)
    summary = await ax.mind.tick(force=True)
    step, = [item for item in summary["formed"] if item["type"] == "goal_step"]
    assert step["decision"] == "ask" and step["status"] == "asked"
    row = ax.store.get(step["id"])
    assert "Which source holds the current figures?" in row.decision_reason
    assert ax.mind.dispatch() == []
    notice = ax.store.get(summary["ask_notice"])
    assert f"[{row.ask_code}]" in notice.context["text"] and "Which source holds" in notice.context["text"]
    ax.mind.answer(row.ask_code, yes=True)
    item, = ax.mind.dispatch()
    assert item["id"] == row.id and "next step toward the goal" in item["body"]


async def test_an_identical_plan_that_failed_twice_is_asked_never_redispatched(ax):
    """The deterministic refusal: without a router the steps are templates; the third identical plan is
    formed as an ask and never dispatched without the owner."""
    no_mastery(ax)
    await adopted_goal(ax, GoalRouter())
    ax.mind.router = None
    first, second = await failed_step(ax, 1), await failed_step(ax, 2)
    assert plan_hash(first["body"]) == plan_hash(second["body"])
    summary = await ax.mind.tick(force=True)
    step, = [item for item in summary["formed"] if item["type"] == "goal_step"]
    row = ax.store.get(step["id"])
    assert row.status == "asked" and "failed 2 times with this same plan; run it again anyway?" in row.decision_reason
    assert plan_hash(row.context["body"]) == plan_hash(first["body"])
    for _ in range(2):
        await ax.mind.tick(force=True)
        assert ax.mind.dispatch() == []


async def test_a_step_reported_blocked_then_failed_is_one_failure_and_no_switch(ax):
    """The body reports a step ``blocked`` and later ``failed``: one failed task, so no strategy switch
    and the next step (the same template plan) is approved, not asked."""
    no_mastery(ax)
    await adopted_goal(ax, GoalRouter())
    ax.mind.router = None
    step, = [item for item in (await ax.mind.tick(force=True))["formed"] if item["type"] == "goal_step"]
    item, = [queued for queued in ax.mind.dispatch() if queued["id"] == step["id"]]
    ax.mind.bound(item["id"], "kanban:s1")
    ax.mind.outcomes.record(item["id"], status="blocked", hermes_ref="kanban:s1", summary="waiting on access")
    ax.shift(minutes=5)
    await ax.mind.tick(force=True)
    ax.mind.outcomes.record(item["id"], status="failed", hermes_ref="kanban:s1", summary="stale",
                            error="the archive scrape returned stale data")
    ax.shift(minutes=10)
    summary = await ax.mind.tick(force=True)
    assert summary["affect"]["switch"] == []
    frustration, = ax.mind.state()["affect"]["levels"]["frustration"]
    assert frustration["failures"] == 1 and frustration["level"] < 0.35
    following, = [entry for entry in summary["formed"] if entry["type"] == "goal_step"]
    assert (following["decision"], following["status"]) == ("act", "approved")


@pytest.mark.parametrize("faculties,hours,asked", [({}, 7, True), ({}, 30, True),
                                                    ({"affect": False, "affect_rules": True}, 7, True),
                                                    ({"affect": False, "affect_rules": True}, 30, False)],
                         ids=["state-7h", "state-30h", "rules-7h", "rules-30h"])
async def test_the_refusal_reads_the_failure_record_not_the_decaying_level(tmp_path, monkeypatch, faculties,
                                                                          hours, asked):
    """An identical plan that failed twice is asked, not dispatched, after the frustration has decayed
    below the switch: the state keeps the record for its 7-day window, the rule for its 24 h."""
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fx = arm(tmp_path, "a", **faculties)
    no_mastery(fx)
    await adopted_goal(fx, GoalRouter())
    fx.mind.router = None
    first, second = await failed_step(fx, 1), await failed_step(fx, 2)
    fx.shift(hours=hours)
    summary = await fx.mind.tick(force=True)
    # The state's level decayed below the switch by 7 h; the rule switches for 24 h.
    assert summary["affect"]["switch"] == ([TOPIC] if faculties and hours < 24 else [])
    step, = [item for item in summary["formed"] if item["type"] == "goal_step"]
    row = fx.store.get(step["id"])
    assert plan_hash(row.context["body"]) == plan_hash(first["body"]) == plan_hash(second["body"])
    if asked:
        assert row.status == "asked" and "failed 2 times with this same plan" in row.decision_reason
        assert fx.mind.dispatch() == []
    else:
        assert row.status == "approved" and [item["id"] for item in fx.mind.dispatch()] == [row.id]
    fx.store.close()


async def test_satiation_holds_an_optional_nudge_but_never_a_promise(ax, tmp_path):
    await ax.say("owner-1", "I waved off your stretching nudge, and I waved off your stretching nudge again.")
    optional = ax.owe("Stretch (a nice-to-have)", hours=-1, priority=30)
    for _ in range(3):
        summary = await ax.mind.tick(force=True)
        assert summary["formed"] == [] and summary["below_threshold"] == 1
        ax.shift(minutes=5)
    assert "Holding back optional nudges: 2 were waved off recently." in ax.mind.section()
    promise = ax.owe("Call the landlord about the lease", hours=-1, priority=70)
    fired = await ax.mind.tick(force=True)
    assert [(item["type"], item["decision"]) for item in fired["formed"]] == [("commitment_reminder", "act")]
    assert ax.store.get(fired["formed"][0]["id"]).source_id == promise["id"]
    # The same nudge without the dismissals goes out once.
    fresh = arm(tmp_path, "fresh")
    fresh.owe("Stretch (a nice-to-have)", hours=-1, priority=30)
    first, second = await fresh.mind.tick(force=True), await fresh.mind.tick(force=True)
    assert [item["type"] for item in first["formed"]] == ["commitment_reminder"] and second["formed"] == []
    assert optional["id"] != promise["id"]
    fresh.store.close()


async def test_overload_postpones_optional_work_until_the_obligations_are_done(ax):
    owed = [ax.owe(f"Prepare the board pack part {n}", hours=6 + n) for n in range(3)]
    ax.mind.add_interest("bees")
    ax.owe("Water the plants (optional)", hours=-1, priority=30)
    summary = await ax.mind.tick(force=True)
    assert summary["affect"]["overloaded"] is True and summary["formed"] == [] and summary["below_threshold"] == 2
    section = ax.mind.section()
    assert "Stretched: 3 open obligations (Prepare the board pack part 0; " in section
    assert "optional work waits and replies stay brief." in section
    for row in owed:
        ax.commitments.resolve(row["id"], "done", resolved_by="owner")
    ax.shift(minutes=5)
    after = await ax.mind.tick(force=True)
    assert sorted(item["type"] for item in after["formed"]) == ["commitment_reminder", "research"]


async def test_worry_notes_what_is_due_soon_and_lifts_owed_duty(ax):
    ax.owe("File the tax return", hours=0.5)
    ax.owe("Send the owner the summary", hours=-1, obligor="assistant")
    summary = await ax.mind.tick(force=True)
    due = (ax.now + timedelta(minutes=30)).strftime("%H:%M UTC")
    assert f"Due soon and not started: File the tax return (due {due})." in ax.mind.section()
    worry = ax.mind.state()["affect"]["levels"]["worry"]
    assert worry["level"] == pytest.approx(0.1) and worry["causes"][0].startswith("due_soon commitment:")
    formed, = summary["formed"]
    assert formed["type"] == "commitment_overdue" and formed["score"] == round(0.8 * 0.85 * (1 + 0.5 * 0.1), 3)


async def seed_every_consumer(fx):
    """Two failures, two dismissals, three near obligations (one due soon), an interest and an optional nudge."""
    await fx.say("owner-1", "The archive export gave stale quarterly figures; "
                            "the archive export gave stale quarterly figures.".lower())
    await fx.say("owner-2", "I waved off your stretching nudge, and I waved off your stretching nudge again.")
    for n, hours in enumerate((0.5, 6, 7)):
        fx.owe(f"Prepare the board pack part {n}", hours=hours)
    fx.mind.add_interest("bees")
    fx.owe("Stretch (a nice-to-have)", hours=-1, priority=30)


async def test_full_affect_changes_no_decision_and_writes_nothing(ax, tmp_path):
    off = arm(tmp_path, "off", affect=False)
    await seed_every_consumer(off)
    summary = await off.mind.tick(force=True)
    assert sorted(item["type"] for item in summary["formed"]) == ["commitment_reminder", "research"]
    assert summary["affect"] == {"source": None}
    assert (summary["appraisal_wait"]["pending"], summary["appraisal_wait"]["running"]) == (0, 0)
    assert off.affect_rows() == []
    state = off.mind.state()["affect"]
    assert state["enabled"] is False and state["levels"] == {} and state["notes"] == []
    section = off.mind.section()
    assert "Prior attempts" not in section and "Stretched" not in section and "Mood" not in section
    off.store.close()


async def test_the_rules_arm_decides_from_the_rules_and_keeps_no_state(ax, tmp_path):
    rules = arm(tmp_path, "rules", affect=False, affect_rules=True)
    await seed_every_consumer(rules)
    summary = await rules.mind.tick(force=True)
    assert summary["formed"] == [] and summary["below_threshold"] == 2      # overload and satiation hold both
    assert summary["affect"]["source"] == "rules" and summary["affect"]["switch"] == [TOPIC]
    assert rules.affect_rows() == []
    section = rules.mind.section()
    assert section.startswith(NOTE + "\n") and "Mood" not in section
    state = rules.mind.state()["affect"]
    assert state["source"] == "rules" and state["levels"] == {} and state["line"] == ""
    assert [note.split(":")[0] for note in state["notes"]] == [
        "Prior attempts at quarterly figures failed 2 times using the archive export; choose a different approach "
        "or ask one question.".split(":")[0], "Due soon and not started", "Stretched", "Holding back optional nudges"]
    rules.owe("Send the owner the summary", hours=-1, obligor="assistant")
    lifted, = (await rules.mind.tick(force=True))["formed"]
    assert lifted["score"] == round(0.8 * 0.85 * 1.25, 3)                   # the rule's worry: owed duty x 1.25
    rules.store.close()


async def test_a_forced_tick_waits_for_the_owners_appraisal_in_flight(ax):
    await ax.say("owner-1", "Twice now: the archive export gave stale quarterly figures, "
                            "the archive export gave stale quarterly figures.", appraise=False)
    assert ax.appraisals.pending_jobs(contact_id=OWNER) == {"pending": 1, "running": 0}

    async def worker():
        await asyncio.sleep(0.3)
        while await ax.appraisals.process_one(REPORTS):
            pass
    concurrent = asyncio.create_task(worker())
    summary = await ax.mind.tick(force=True)
    await concurrent
    assert summary["appraisal_wait"]["pending"] == 0 and summary["appraisal_wait"]["running"] == 0
    assert 0.2 <= summary["appraisal_wait"]["waited_seconds"] < 3
    assert summary["affect"]["switch"] == [TOPIC] and NOTE in ax.mind.section()


class SlowAppraisals:
    """The owner's appraisal job is in flight at tick time and lands 0.3 s later with an interest record;
    ``process_one`` must never be called by the mind."""

    def __init__(self, jobs=None):
        self.done, self.polls, self.jobs = False, 0, jobs

    def pending_jobs(self, *, contact_id=None):
        assert contact_id == OWNER
        self.polls += 1
        if self.jobs is not None:
            if isinstance(self.jobs, Exception):
                raise self.jobs
            return dict(self.jobs)
        return {"pending": 0, "running": 0} if self.done else {"pending": 0, "running": 1}

    def affect_events(self, *, since, limit=1000):
        return []

    def view(self, subject_id, *, viewer_contact_id, limit=4, **_):
        return {"records": [{"id": "appraisal:1", "kind": "appraisal", "dimension": "interest",
                             "topic": "tidal energy", "intensity": "moderate"}] if self.done else []}

    def process_one(self, *args, **kwargs):
        raise AssertionError("the mind waits for appraisal jobs; it never processes one")


class SlowFixture(Fixture):
    def __init__(self, tmp_path, appraisals, **faculties):
        self.appraisals = appraisals
        super().__init__(tmp_path, config={"faculties": faculties} if faculties else None)

    def build(self) -> Mind:
        mind = Mind(config=self.config, store=self.store, state_dir=self.state, owner_id=OWNER,
                    commitments=self.commitments, feedback=self.feedback, expectations=self.expectations,
                    contacts=self.contacts, ledger=self.ledger, clock=lambda: self.now, backups=False,
                    persist=self.persisted.append, router=None, appraisals=self.appraisals)
        mind.digest_hour = 25
        return mind


@pytest.mark.parametrize("faculties", [{}, {"affect": False}, {"affect": False, "affect_rules": True}],
                         ids=["full", "full-affect", "full-affect-plus-rules"])
async def test_every_arm_waits_for_the_owners_appraisal_so_arms_differ_only_by_affect(tmp_path, monkeypatch,
                                                                                    faculties):
    """The wait decides what the curiosity drive reads in the same tick (the owner's interest appraisals),
    so it is the mind's, whatever the affect switches: the same statement raises research in every arm."""
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    appraisals = SlowAppraisals()
    fx = SlowFixture(tmp_path, appraisals, **faculties)

    async def worker():
        await asyncio.sleep(0.3)
        appraisals.done = True
    concurrent = asyncio.create_task(worker())
    summary = await fx.mind.tick(force=True)
    await concurrent
    assert summary["appraisal_wait"]["running"] == 0 and 0.25 <= summary["appraisal_wait"]["waited_seconds"] < 3
    assert [item["type"] for item in summary["formed"]] == ["research"]
    fx.store.close()


async def test_the_wait_stops_at_the_budget_when_nothing_runs_and_on_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fx = SlowFixture(tmp_path, SlowAppraisals(jobs={"pending": 2, "running": 1}))
    fx.mind.appraisal_forced_s, fx.mind.appraisal_timer_s = 0.4, 0.2
    assert (fx.mind.appraisal_idle_s, fx.mind.appraisal_poll_s) == (3.0, 0.1)
    for force, budget in ((True, 0.4), (False, 0.2)):
        started = asyncio.get_running_loop().time()
        waited = await fx.mind._await_appraisals(force)
        assert budget - 0.05 <= asyncio.get_running_loop().time() - started < budget + 1
        assert (waited["pending"], waited["running"]) == (2, 1)
    fx.mind.appraisals.jobs = {"pending": 2, "running": 0}            # queued, and no consumer is working
    fx.mind.appraisal_forced_s, fx.mind.appraisal_idle_s = 30.0, 0.3
    started = asyncio.get_running_loop().time()
    waited = await fx.mind._await_appraisals(True)
    assert 0.25 <= asyncio.get_running_loop().time() - started < 2 and waited["pending"] == 2
    fx.mind.appraisals.jobs = RuntimeError("ledger locked")
    assert (await fx.mind._await_appraisals(True))["error"] == "RuntimeError"
    fx.mind.appraisals = None
    assert await fx.mind._await_appraisals(True) is None
    from protagine.mind import tick as tick_module
    assert (tick_module.APPRAISAL_FORCED_S, tick_module.APPRAISAL_TIMER_S) == (30.0, 2.0)
    fx.store.close()


LONG = " about migrating the family photo archive from the old laptop drives onto the new NAS volume"


@pytest.mark.parametrize("faculties", [{}, {"affect": False}], ids=["full", "full-affect"])
async def test_affect_lines_take_only_the_room_the_section_has_left(tmp_path, monkeypatch, faculties):
    """The open asks line (the owner's view of the codes, including the asks the switch creates) is never
    cut to make room for affect: affect's lines come first but get only what the rest leaves."""
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fx = arm(tmp_path, "a", **faculties)
    await fx.say("owner-1", "the archive export gave stale quarterly figures; "
                            "the archive export gave stale quarterly figures.")
    for n, hours in enumerate((0.5, 6, 7)):
        fx.owe(f"Prepare the board pack part {n}", hours=hours)
    for n in range(3):
        fx.mind.add_interest(f"question {n}{LONG}")
    fx.store.create_intention(kind="task", type="research", title="Re-run the archive export for the board figures",
                              drive="duty", cls="internal", decision="ask", decision_reason="test", status="asked",
                              dedup_key="ask-1", ask_code="K7F", context={"body": "x"})
    await fx.mind.tick(force=True)
    section = fx.mind.section()
    assert len(section) <= 600
    assert section.splitlines()[-1] == "Waiting for your say on: [K7F] Re-run the archive export for the board figures."
    if not faculties:
        assert section.startswith("Prior attempts at quarterly figures failed 2 times")
    fx.store.close()


async def test_form_demotes_act_to_ask_and_never_promotes(ax, monkeypatch):
    from protagine.mind.authority import Verdict
    question = f"{TOPIC} failed 2 times with this same plan; run it again anyway?"
    for n, decision in enumerate(("act", "ask", "drop", "defer")):
        monkeypatch.setattr(ax.mind.authority, "decide",
                            lambda **_: Verdict(decision=decision, reason="policy", cls="internal"))
        row = await ax.mind._form(candidate(dedup_key=f"k{n}", affect_ask=question, text="body"), 0.9, ax.now)
        assert row.decision == ("ask" if decision == "act" else decision)
        assert (question in row.decision_reason) is (decision == "act")
        plain = await ax.mind._form(candidate(dedup_key=f"p{n}", text="body"), 0.9, ax.now)
        assert plain.decision == decision and plain.decision_reason == "policy"
    # With affect off nothing demotes, whatever a candidate carries (a detail stored while it was on).
    monkeypatch.setattr(ax.mind.authority, "decide", lambda **_: Verdict(decision="act", reason="policy", cls="internal"))
    monkeypatch.setattr(ax.mind.feelings, "state_on", False)
    row = await ax.mind._form(candidate(dedup_key="off", affect_ask=question, text="body"), 0.9, ax.now)
    assert row.decision == "act" and row.decision_reason == "policy"


async def test_a_task_formed_before_the_failures_carries_the_note_at_dispatch(ax):
    ax.owe("Send the owner the quarterly figures", hours=-1, obligor="assistant")
    formed, = (await ax.mind.tick(force=True))["formed"]
    assert "Prior attempts" not in ax.store.get(formed["id"]).context["body"]
    await ax.say("owner-1", "The archive export gave stale quarterly figures, "
                            "and the archive export gave stale quarterly figures again.".lower())
    await ax.mind.tick(force=True)
    item, = ax.mind.dispatch()
    assert item["id"] == formed["id"] and item["body"].endswith("\n\n" + NOTE)
    assert "Prior attempts" not in ax.store.get(formed["id"]).context["body"]    # the stored plan is unchanged


@pytest.mark.parametrize("faculties", [{}, {"affect": False, "affect_rules": True}], ids=["state", "rules"])
async def test_old_failures_and_a_success_between_failures_force_no_switch(tmp_path, monkeypatch, faculties):
    """Acceptance, in both sources: two failures that switched the strategy no longer do three days
    later, and a failure, a success and a failure are one failure since the success, so the owed
    task on the topic goes out without the note and nothing is asked."""
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    old = arm(tmp_path, "old", **faculties)
    await old.say("owner-1", "Ugh, the archive export gave stale quarterly figures.")
    old.shift(hours=1)
    await old.say("owner-2", "Checked again: the archive export gave stale quarterly figures.")
    assert (await old.mind.tick(force=True))["affect"]["switch"] == [TOPIC]
    old.shift(days=3)
    later = await old.mind.tick(force=True)
    assert later["affect"]["switch"] == [] and "Prior attempts" not in old.mind.section()
    if not faculties:
        frustration, = old.mind.state()["affect"]["levels"]["frustration"]
        assert frustration["level"] == pytest.approx(0.6 / 8, abs=0.01) and len(frustration["causes"]) == 2
    old.store.close()

    mixed = arm(tmp_path, "mixed", **faculties)
    await mixed.say("owner-1", "Ugh, the archive export gave stale quarterly figures.")
    mixed.shift(hours=1)
    await mixed.say("owner-2", "Good news: the ledger figures were right.")
    mixed.shift(hours=1)
    await mixed.say("owner-3", "And now the archive export gave stale quarterly figures once more.")
    assert [event["kind"] for event in mixed.appraisals.affect_events(since=0)] == ["failed", "succeeded", "failed"]
    mixed.owe("Send the owner the quarterly figures", hours=-1, obligor="assistant")
    summary = await mixed.mind.tick(force=True)
    assert summary["affect"]["switch"] == [] and "Prior attempts" not in mixed.mind.section()
    formed, = summary["formed"]
    assert formed["type"] == "commitment_overdue" and formed["decision"] == "act"
    item, = mixed.mind.dispatch()
    assert "Prior attempts" not in item["body"]
    if not faculties:
        assert mixed.mind.state()["affect"]["levels"]["frustration"][0]["level"] < 0.5
    mixed.store.close()
