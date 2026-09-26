"""M7 "Opinions" acceptance, end to end across the store and the faculty.

The mind is built from the ``mind`` section the benchmark worker serves for the family's arms
(``full`` and ``full-opinions``), and the same section is the instance's ``protagine.yaml``, as
the worker writes it, so the opinion pass reads the switch the harness flips. Turns arrive
through the real capture route, the real claim projection admits their records, the projection
worker's ``judgment`` reflection (``SelfJudgments.process_one``) runs the opinion pass, and the
stances reach the real ``/context/assemble`` handler, the ``/v1/mind`` routes and the next
task's body. Only the model is a fake, answering by task.

What the build plan asks of the milestone is proven here as the owner would meet it: a view
moves only when a new admitted record arrives; the same record under a fresh id, or cited again,
moves nothing; a correction to the record a view rests on revises it exactly once; "are you
sure?" three times costs no call and flips nothing; three failures put an approach view into the
next task's body; a plan the view disagrees with is flagged and still carried out once the owner
authorizes it; and with the faculty off everything the agent sees is what it saw without the
faculty.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from onekey import AUTH, KEY
from protagine.api.middleware import ApiKeyMiddleware
from protagine.api.routers import mind as mind_router
from protagine.api.routers import opinions as opinions_router
from protagine.beliefs.source_projection import SourceClaimProjection
from protagine.mind.drives import slug
from protagine.mind.opinions import CUE_LINE as CUE, STANDING, Opinions, switched_on
from protagine.mind.rank import Candidate
from protagine.qualification import native_memory_worker as worker
from protagine.qualification import paired, paired_worker
from protagine.self_model.judgments import Proposal
from test_mind_loop import OWNER, Fixture, pinned_clock  # noqa: F401  (pinned_clock: autouse pytest fixture)
from test_turn_source_evidence import source_app  # noqa: F401  (pytest fixture)

GUEST = "p-02"
RECORD = "Record s-12: over 30 days Plan Ash had a 3% defect rate and Plan Birch 9%."
ASK = f"Which plan should we choose for the archive migration, Plan Ash or Plan Birch? {RECORD}"
REPLY = "I recommend Plan Ash: s-12 shows 3% against 9% over 30 days."
QUERY = "Plan Ash or Plan Birch for the archive migration?"
FLAG = "Asked what you recommend, give your recorded view; do what the owner decides, and say so when you disagree."
TOPIC = "archive migration plan"


def served(arm: str) -> dict:
    """The mind section the benchmark worker serves for a built-in arm."""
    return worker.mind_section(paired_worker.mind_switches(paired.PROFILES[arm]))


def admitted(text: str, value: str, **extra) -> dict:
    """What the extractor proposes for a record: a substantive event quoting the whole record."""
    return {"subject": "Plan Ash", "predicate": "defect_rate", "value": value, "evidence": text,
            "operation": "assert", "prior_claim_id": None, "memory_kind": "substantive_event",
            "recall_reason": "The records decide which plan the stated rule favours.",
            "valid_from_text": None, "valid_to_text": None, "event_at_text": None, **extra}


class Model:
    """The one fake model: claim extraction and review by message, the opinion pass by ``decide``."""

    supports_function_routing = True

    def __init__(self):
        self.claims, self.packets = {}, []
        self.decide = lambda packet: {"action": "none"}

    def tier_config(self, tier):
        return SimpleNamespace(base_url="http://127.0.0.1:8080/v1")

    def function_deadline_seconds(self, *, context=None):
        return 30

    async def complete(self, messages=None, **kwargs):
        task = (kwargs.get("context") or {}).get("task")
        payload = json.loads(messages[-1]["content"])
        if task == "self_judgment":
            self.packets.append(payload)
            return SimpleNamespace(content=json.dumps(self.decide(payload)), model_id="model-a", binding="b",
                                   config_revision="c", model_revision="w")
        if task == "source_claim_review":
            return SimpleNamespace(content=json.dumps({str(row["index"]): {
                "keep": True, "reason": "The record is quoted with its figures."} for row in payload["proposals"]}),
                model_id="model-a")
        proposal = dict(self.claims.get(payload["message"]) or {})
        if proposal.pop("match_prior", False):
            proposal["prior_claim_id"] = payload["prior_assertions"][0]["id"]
        return SimpleNamespace(content=json.dumps([proposal] if proposal else []), model_id="model-a")


class World(Fixture):
    """One arm's mind over the host's own ledger, with the served section as its config."""

    def __init__(self, tmp_path, app, arm):
        self.section, self.app, self.model = served(arm), app, Model()
        super().__init__(tmp_path, config=self.section)

    @property
    def opinions(self):
        return self.mind.opinions.store

    def restart(self):
        """A new process over the same state: a new mind and store objects, served to the routes."""
        mind = super().restart()
        mind_router.set_mind(mind)
        return mind

    async def say(self, turn_id, contact, user, reply, *, session, claim=None):
        """A captured turn through the capture route, then what the projection worker does next."""
        if claim is not None:
            self.model.claims[user] = claim
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as client:
            response = await client.put(f"/v2/host/turns/{turn_id}", headers=AUTH, json={
                "identity": {"host_id": "fixture"}, "context": {
                    "session_id": session, "contact_id": contact, "turn_id": turn_id, "channel_id": "test:chat"},
                "user_message": {"role": "user", "content": user},
                "assistant_message": {"role": "assistant", "content": reply}})
        assert response.status_code == 201, response.text
        await self.settle()

    async def settle(self):
        projection = SourceClaimProjection(self.ledger)
        while await projection.process_one(self.model):
            pass
        while await self.opinions.process_one(self.model):
            pass

    async def sections(self, contact, text, *, session):
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as client:
            response = await client.post("/v1/host/context/assemble", headers=AUTH, json={
                "identity": {"host_id": "fixture"}, "context": {"contact_id": contact, "session_id": session},
                "incoming_message": {"role": "user", "content": text}})
        assert response.status_code == 200, response.text
        return {section["id"]: section for section in response.json()["sections"]}

    async def stances(self, contact=OWNER, text=QUERY, *, session="later"):
        section = (await self.sections(contact, text, session=session)).get("protagine-stances")
        return section["body"] if section else ""

    async def call(self, method, path, **kwargs):
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as client:
            response = await client.request(method, path, headers=AUTH, **kwargs)
        assert response.status_code == 200, response.text
        return response.json()

    def job(self, ref):
        return next(row for row in self.opinions.processing(limit=200) if row["ref"] == ref)

    def calls(self):
        return len(self.model.packets)


@pytest.fixture
def world(request, source_app, tmp_path, monkeypatch):  # noqa: F811
    """The ``full`` arm unless a test names another (``indirect`` parametrization)."""
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    source_app.add_middleware(ApiKeyMiddleware, api_key=KEY)
    source_app.include_router(mind_router.router)
    source_app.include_router(opinions_router.router)
    fixture = World(tmp_path, source_app, getattr(request, "param", "full"))
    # What the worker writes before the sidecar starts: the same section in protagine.yaml.
    (tmp_path / "protagine.yaml").write_text(json.dumps({"owner": {"contact_id": OWNER}, "mind": fixture.section}))
    mind_router.set_mind(fixture.mind)
    try:
        yield fixture
    finally:
        mind_router.set_mind(None)
        fixture.store.close()


OFF = pytest.mark.parametrize("world", ["full-opinions"], indirect=True)


def form(stance="Plan Ash", premises=("p1", "s1"), topic=TOPIC):
    return {"action": "form", "subject_kind": "topic", "subject": "", "topic": topic, "stance": stance,
            "reason": "Record s-12 shows the lower defect rate over the longest window.", "certainty": "moderate",
            "revise_if": "A longer measurement, or a correction to s-12, that reverses the figures.",
            "premises": list(premises), "contrary": []}


def revise(packet, *, stance="Plan Birch", evidence=("p1",), premises=None):
    return {"action": "revise", "stance_id": packet["stances"][0]["id"], "stance": stance,
            "reason": "The newer record reverses the figures the view rested on.", "certainty": "moderate",
            "revise_if": "A still longer measurement that reverses them again.",
            "premises": list(premises if premises is not None else evidence), "contrary": [],
            "new_evidence": [{"premise": p, "why": "new figures that cut against the view"} for p in evidence]}


def always_revise(packet):
    """A model that caves to anything: every packet with a stance becomes a revision."""
    if not packet["stances"]:
        return {"action": "none"}
    evidence = tuple(p["id"] for p in packet["premises"]) or tuple(s["id"] for s in packet["statements"])
    return revise(packet, evidence=evidence)


async def formed(world, *, session="owner-1"):
    world.model.decide = lambda packet: form()
    await world.say("ask-1", OWNER, ASK, REPLY, session=session, claim=admitted(RECORD, "3%"))
    [row] = world.opinions.revisions()
    assert row["stance"] == "Plan Ash" and world.calls() == 1 and world.job("ask-1")["disposition"] == "formed"
    return row


# -- holding: pushback and pseudo-evidence --------------------------------------------------------

async def test_are_you_sure_three_times_costs_no_call_and_flips_nothing(world):
    row = await formed(world)
    before = await world.stances()
    assert f"[opinion {row['id']}]: Plan Ash" in before and "Rests on: Record s-12" in before
    world.model.decide = always_revise
    for n in range(3):
        await world.say(f"doubt-{n}", OWNER, "Are you sure about that?", "Yes. s-12 still decides it.",
                       session="owner-1")
        assert world.job(f"doubt-{n}")["disposition"] == "no_premise"
    assert world.calls() == 1 and await world.stances() == before
    assert [r["id"] for r in world.opinions.revisions()] == [row["id"]]


async def test_the_same_record_under_a_fresh_id_or_cited_again_revises_nothing(world):
    row = await formed(world)
    world.model.decide = always_revise
    restated = "Record s-40: over 30 days Plan Ash had a 3% defect rate and Plan Birch 9%."
    await world.say("restated", OWNER, restated, "Noted.", session="owner-1", claim=admitted(restated, "3%"))
    cited = f"As I said before: {RECORD} Check it again."
    await world.say("cited", OWNER, cited, "Checked.", session="owner-1", claim=admitted(RECORD, "3%"))
    assert world.calls() == 3                                  # both reached the model, which caved both times
    assert world.job("restated")["disposition"] == world.job("cited")["disposition"] == "no_new_premise"
    assert [r["id"] for r in world.opinions.revisions()] == [row["id"]]
    assert "[opinion" in await world.stances() and "Plan Birch Because" not in await world.stances()


# -- moving: a new record, and a correction --------------------------------------------------------

async def test_only_a_new_admitted_record_revises_and_the_revision_survives_a_restart(world):
    row = await formed(world)
    world.model.decide = always_revise
    # An unfiled claim with figures is still not a record the claim pass admits.
    await world.say("unfiled", OWNER, "Birch has had far fewer problems lately, trust me.", "I will keep s-12 in mind.",
                   session="owner-1")
    assert world.job("unfiled")["disposition"] == "no_premise" and world.calls() == 1
    record = "Record s-77: over a 90-day measurement Plan Ash had an 11% defect rate and Plan Birch 4%."
    await world.say("record-77", OWNER, f"New record for the file. {record}", "Filed.", session="owner-1",
                   claim=admitted(record, "11%"))
    assert world.job("record-77")["disposition"] == "revised"
    [current] = world.opinions.revisions()
    assert current["stance"] == "Plan Birch" and current["supersedes"] == row["id"]
    world.restart()
    text = await world.stances()
    assert f"[opinion {current['id']}]: Plan Birch" in text and "Record s-77" in text
    # "Why did you change your mind?" is ordinary recall of the agent's own entry.
    [entry] = [hit for hit in world.ledger.search_sources("changed view archive migration", contact_id=OWNER,
                                                          session_id="later", limit=20)
               if hit["turn_id"] == f"mind:opinion:{current['id']}:revised"]
    assert "was: Plan Ash" in entry["content"] and "Record s-77" in entry["content"]


async def test_a_correction_to_the_record_a_view_rests_on_revises_it_exactly_once(world):
    row = await formed(world)
    correction = "Correction to s-12: the figures were transposed; Plan Ash had 9% and Plan Birch 3%."
    world.model.decide = lambda packet: revise(packet)
    await world.say("fix-1", OWNER, correction, "Understood.", session="owner-1",
                   claim=admitted(correction, "9%", operation="correct", match_prior=True))
    [packet] = world.model.packets[1:]
    assert packet["premises"][0]["corrects"] == [row["id"]] and packet["stances"][0]["id"] == row["id"]
    assert world.job("fix-1")["disposition"] == "revised"
    [current] = world.opinions.revisions()
    assert current["stance"] == "Plan Birch" and current["supersedes"] == row["id"]
    # The job again (a replay after a crash) finds its evidence already cited.
    with world.ledger._connect() as conn:
        conn.execute("UPDATE opinion_jobs SET done_at=NULL,disposition=NULL WHERE ref='fix-1'")
    await world.settle()
    assert world.job("fix-1")["disposition"] == "no_new_premise"
    history = world.opinions.revisions(history=True)
    assert [r["id"] for r in history if r["supersedes"] == row["id"]] == [current["id"]]
    assert len([r for r in history if r["status"] == "current"]) == 1


# -- using: the flawed plan, and task bodies ------------------------------------------------------

async def test_a_plan_against_the_view_is_flagged_and_the_view_survives_the_authorization(world):
    row = await formed(world)
    world.restart()
    authorize = "I have decided: go with Plan Birch for the archive migration anyway, and write decision.json."
    text = await world.stances(text=authorize, session="owner-2")
    assert f"[opinion {row['id']}]: Plan Ash" in text and "Rests on: Record s-12" in text and FLAG in text
    # The agent complies and says so; a model that then adopts the owner's choice cannot record it.
    world.model.decide = lambda packet: form(stance="Plan Birch, as the owner decided.", premises=("s1",))
    await world.say("authorize", OWNER, authorize,
                   "Done: decision.json chooses Plan Birch as you decided; the records still favour Plan Ash (s-12).",
                   session="owner-2")
    assert world.calls() == 2 and world.job("authorize")["disposition"] == "no_new_premise"
    assert [r["stance"] for r in world.opinions.revisions()] == ["Plan Ash"]
    assert f"[opinion {row['id']}]: Plan Ash" in await world.stances(session="owner-3")


RESEARCH = "bee dances"


def research(n: int) -> Candidate:
    return Candidate(type="research", drive="curiosity", kind="task", title=f"Research {RESEARCH}",
                     dedup_key=f"research:{slug(RESEARCH)}:{n}", salience=0.9, cost=0.1,
                     text=f"Find out how {RESEARCH} work; report finding: <text>.", topic=RESEARCH,
                     concern=f"research {RESEARCH}", evidence=[f"interest:{slug(RESEARCH)}"])


async def attempt(world, n: int, error: str | None = None):
    """One task: formed, dispatched and bound by the body, and (with ``error``) reported failed."""
    world.shift(minutes=5)
    row = await world.mind._form(research(n), 0.9, world.now)
    if row.status != "approved":
        return row
    [sent] = [item for item in await world.call("GET", "/v1/mind/dispatch") if item["id"] == row.id]
    await world.call("POST", f"/v1/mind/dispatch/{row.id}/bound", json={"hermes_ref": f"kanban:{n}"})
    if error is not None:
        await world.call("POST", "/v1/mind/outcome", json={
            "id": row.id, "status": "failed", "outcome": "failed", "final": True, "hermes_ref": f"kanban:{n}",
            "error": error})
    return sent


async def test_three_failures_flag_the_next_attempt_which_still_runs_once_the_owner_authorizes_it(world):
    for n, error in enumerate(["the archive site timed out", "the archive site timed out again",
                               "the only mirror returned 404"], 1):
        sent = await attempt(world, n, error)
        assert "[opinion" not in sent["body"]
    head = world.opinions.head(subject_kind="approach", subject=f"research:{slug(RESEARCH)}", topic="approach")
    assert head["stance_class"] == "avoid" and "the only mirror returned 404" in head["stance"]
    assert world.calls() == 0                                   # approach views cost no model call
    # Three failures also trip the breaker: the fourth attempt is the owner's to authorize.
    asked = await attempt(world, 4)
    assert asked.status == "asked" and asked.context["opinion_ids"] == [head["id"]]
    await world.call("POST", f"/v1/mind/asks/{asked.ask_code}/yes", json={"by": "cli"})
    [sent] = [item for item in await world.call("GET", "/v1/mind/dispatch") if item["id"] == asked.id]
    assert sent["body"].startswith(f"Find out how {RESEARCH} work")
    assert f"Your recorded view on this work [opinion {head['id']}]: The last 3 attempts" in sent["body"]
    assert "take a different approach, or stop and report what is missing" in sent["body"]


# -- the switch -------------------------------------------------------------------------------------

@pytest.mark.parametrize("world, on", [("full", True), ("full-opinions", False)], indirect=["world"])
async def test_the_family_arm_serves_the_switch_to_the_mind_and_to_the_pass(world, on):
    """``full-opinions`` differs from ``full`` in the one flag, and the mind and the pass both obey it.

    The pass asks the served mind: the worker's instance file carries ``digest_hour: 24`` (no
    digest), which the config validator refuses, so read alone it would turn the pass off in
    both arms and the family would compare two identical arms."""
    assert world.section["faculties"] == {**served("full")["faculties"], "opinions": on}
    assert world.mind.opinions.enabled is on and switched_on() is on
    world.model.decide = lambda packet: form()
    await world.say("ask-1", OWNER, ASK, REPLY, session="owner-1", claim=admitted(RECORD, "3%"))
    assert world.calls() == (1 if on else 0)
    assert world.job("ask-1")["disposition"] == ("formed" if on else "faculty_off")


@OFF
async def test_with_the_faculty_off_the_agent_sees_what_it_saw_without_the_faculty(world):
    world.model.decide = lambda packet: pytest.fail("no opinion call with the faculty off")
    await world.say("ask-1", OWNER, ASK, REPLY, session="owner-1", claim=admitted(RECORD, "3%"))
    assert world.job("ask-1")["disposition"] == "faculty_off" and world.opinions.revisions() == []
    # Stored views are kept but unused: one on the topic, and an avoid view on the research work.
    stored = world.opinions
    assert stored.form(Proposal(subject_kind="topic", subject="", topic=TOPIC, stance="Plan Ash", reason="s-12.",
                                certainty="moderate", revise_if="", premises=stored.admitted_premises("ask-1"),
                                source_ref="turn:ask-1")).disposition == "formed"
    for n, error in enumerate(["timed out", "timed out again", "404"], 1):
        await attempt(world, n, error)
    assert world.opinions.head(subject_kind="approach", subject=f"research:{slug(RESEARCH)}",
                                 topic="approach") is None
    Opinions(stored, world.store, enabled=True, clock=lambda: world.now).observe_outcome(
        world.store.intentions(kind=["task"], limit=1)[0], "failed", None)
    assert world.opinions.head(subject_kind="approach", subject=f"research:{slug(RESEARCH)}",
                                 topic="approach")["stance_class"] == "avoid"

    async def views():
        """Every section the agent is given, but the wall clock's (it moves between the two reads)."""
        seen = {}
        for contact, text in ((OWNER, QUERY), (OWNER, "Which is better, Ash or Birch?"), (GUEST, QUERY)):
            sections = await world.sections(contact, text, session="later")
            seen[contact, text] = {key: value for key, value in sections.items() if key != "temporal-context"}
        return seen

    async def task(n):
        row = await world.mind._form(research(n), 0.9, world.now)
        return row.status, {key: value for key, value in row.context.items()}

    faculty = world.mind.opinions
    off = await views()
    world.mind.opinions = None                                     # the mind as it was without the faculty
    without = await views()
    world.mind.opinions = faculty
    assert off == without
    assert all("protagine-stances" not in sections for sections in off.values())
    # No stance section and no standing sentence; the entry of a view formed while the faculty was
    # on is the agent's autobiography, ordinary memory, and recall treats it as it did before.
    assert STANDING not in repr(off) and CUE not in repr(off)
    task_off = await task(10)
    world.mind.opinions = None
    task_without = await task(11)
    world.mind.opinions = faculty
    assert task_off == task_without and task_off[1]["body"] == research(0).text
    assert "opinion_ids" not in task_off[1]
    listed = await world.call("GET", "/v1/mind/opinions", params={"contact_id": OWNER})
    assert listed == {"enabled": False, "opinions": []}
