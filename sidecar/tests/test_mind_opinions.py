"""Approach opinions from task outcomes, and stances rendered into turn context (architecture 4.4, 4.9).

Three failures in a row at the same work become an ``avoid`` view with no model call;
the next task at that work carries it in its body, ``dispatch()`` sends it, and it
survives a restart. A verified success turns it into ``prefer``; an unverified one
changes nothing. The context section is bounded, cited and audience-filtered.
"""

from __future__ import annotations

import json

import pytest

from protagine.api.routers import mind as mind_router
from protagine.mind.drives import slug
from protagine.mind.opinions import CONTEXT_CHARS, CUE_LINE, STANDING, Opinions
from protagine.mind.rank import Candidate
from protagine.self_model.judgments import Proposal
from test_mind_loop import OWNER, Fixture

TOPIC = "bee dances"
SIGNATURE = f"research:{slug(TOPIC)}"
# The breaker and the hourly budget would turn later attempts into asks or deferrals; these tests
# are about the view the outcomes form, not about authority.
ROOMY = {"breaker": {"failures": 50}, "budgets": {"tasks_per_hour": 50, "concurrent_tasks": 50}}
FINDING_CHECK = {"kind": "result_field", "field": "finding"}     # what deliberation gives every task by default


@pytest.fixture
def fx(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fixture = Fixture(tmp_path, config={"faculties": {"opinions": True}, **ROOMY})
    yield fixture
    mind_router.set_mind(None)
    fixture.store.close()


def candidate(n: int, topic: str = TOPIC) -> Candidate:
    return Candidate(type="research", drive="curiosity", kind="task", title=f"Research {topic}",
                     dedup_key=f"research:{slug(topic)}:{n}", salience=0.9, cost=0.1,
                     text=f"Find out how {topic} work; report finding: <text>.", topic=topic,
                     concern=f"research {topic}", evidence=[f"interest:{slug(topic)}"])


async def attempt(fx, n: int, status: str, *, topic: str = TOPIC, check=None, **report):
    fx.shift(minutes=5)
    row = await fx.mind._form(candidate(n, topic), 0.9, fx.now)
    assert row is not None and row.status == "approved", row
    if check is not None:
        fx.store.update(row.id, success_check=json.dumps(check))
    fx.mind.bound(row.id, f"kanban:{n}")
    return fx.mind.outcomes.record(row.id, status=status, hermes_ref=f"kanban:{n}", **report)


def approach(fx):
    return fx.mind.opinions.store.head(subject_kind="approach", subject=SIGNATURE, topic="approach")


async def three_failures(fx):
    for n, error in enumerate(["the archive site timed out", "the archive site timed out again",
                               "the only mirror returned 404"], 1):
        await attempt(fx, n, "failed", error=error)


async def test_three_failures_become_an_avoid_view_in_the_next_task_body(fx):
    await attempt(fx, 1, "failed", error="the archive site timed out")
    await attempt(fx, 2, "failed", error="the archive site timed out again")
    assert approach(fx) is None                                   # two failures are not yet a pattern
    await attempt(fx, 3, "failed", error="the only mirror returned 404")
    head = approach(fx)
    assert head["status"] == "current" and head["stance_class"] == "avoid" and head["audience"] == "owner"
    assert head["stance"].startswith('The last 3 attempts at "Research bee dances" failed: ')
    assert "the only mirror returned 404" in head["stance"] and head["revise_if"] == "A verified success at this work."
    assert [p["kind"] for p in head["premises"]] == ["outcome"] * 3
    assert {p["verified"] for p in head["premises"]} == {"hermes_failure"}

    row = await fx.mind._form(candidate(4), 0.9, fx.now)
    assert row.context["opinion_ids"] == [head["id"]]
    assert row.context["body"].endswith(f"Your recorded view on this work [opinion {head['id']}]: {head['stance']}")
    [sent] = [item for item in fx.mind.dispatch() if item["id"] == row.id]
    assert f"[opinion {head['id']}]" in sent["body"] and sent["body"].startswith("Find out how bee dances work")
    # Other work carries no view.
    other = await fx.mind._form(candidate(5, topic="tide tables"), 0.9, fx.now)
    assert "opinion_ids" not in other.context and "[opinion" not in other.context["body"]

    fx.restart()
    again = await fx.mind._form(candidate(6), 0.9, fx.now)
    assert again.context["opinion_ids"] == [head["id"]]


async def test_a_fourth_failure_agrees_and_changes_nothing(fx):
    await three_failures(fx)
    head = approach(fx)
    await attempt(fx, 4, "failed", error="timed out once more")
    assert approach(fx)["id"] == head["id"]


async def test_a_verified_success_turns_it_into_prefer_and_an_unverified_one_does_not(fx):
    await three_failures(fx)
    avoid = approach(fx)
    await attempt(fx, 4, "done", summary="finding: they encode distance in the waggle run.")
    assert approach(fx)["id"] == avoid["id"]                      # unverified: admits nothing
    done = await attempt(fx, 5, "done", summary="finding: the waggle run encodes distance.", check=FINDING_CHECK)
    assert done.verified == "check" and done.result_metadata["check"]["passed"] is True
    prefer = approach(fx)
    assert prefer["id"] != avoid["id"] and prefer["supersedes"] == avoid["id"]
    assert prefer["stance_class"] == "prefer" and f"attempt {done.id} succeeded (check)" in prefer["stance"]
    assert prefer["revise_if"] == "Three failures in a row at this work."
    row = await fx.mind._form(candidate(6), 0.9, fx.now)
    assert row.context["opinion_ids"] == [prefer["id"]] and "Prefer that approach." in row.context["body"]


async def test_a_done_report_whose_check_failed_is_not_a_success(fx):
    """``verified`` names the verifier that ran, so a failed check is ``check`` too: only a check
    that passed (or the owner) verifies a success. A body's bare claim of a check that never ran
    verifies nothing, and is not even recorded: only the mind grants ``check``."""
    await three_failures(fx)
    avoid = approach(fx)
    done = await attempt(fx, 4, "done", check=FINDING_CHECK, summary="nothing was found, the mirror was empty.")
    assert done.verified == "check" and done.result_metadata["check"]["passed"] is False
    claimed = await attempt(fx, 5, "done", summary="all good", verified="check")
    assert claimed.verified == "none" and "check" not in (claimed.result_metadata or {})
    head = approach(fx)
    assert head["id"] == avoid["id"] and head["stance_class"] == "avoid"
    body = (await fx.mind._form(candidate(6), 0.9, fx.now)).context["body"]
    assert f"[opinion {avoid['id']}]" in body and "Prefer that approach." not in body


async def test_a_failed_check_is_a_failure_in_the_run(fx):
    await attempt(fx, 1, "failed", error="the archive site timed out")
    await attempt(fx, 2, "failed", error="the archive site timed out again")
    missed = await attempt(fx, 3, "done", check=FINDING_CHECK, summary="nothing was found, the mirror was empty.")
    head = approach(fx)
    assert head is not None and head["stance_class"] == "avoid"
    assert "check failed: nothing was found" in head["stance"]
    [premise] = [p for p in head["premises"] if p["ref"] == f"intention:{missed.id}"]
    assert premise["text"].startswith("done, check failed: nothing was found")
    await attempt(fx, 4, "done", check=FINDING_CHECK, summary="finding: the waggle run encodes distance.")
    prefer = approach(fx)
    assert prefer["stance_class"] == "prefer" and prefer["supersedes"] == head["id"]
    # Three misses in a row at the preferred work turn it back into avoid.
    for n in (5, 6, 7):
        await attempt(fx, n, "done", check=FINDING_CHECK, summary=f"still nothing, run {n}.")
    again = approach(fx)
    assert again["stance_class"] == "avoid" and again["supersedes"] == prefer["id"]


def _finding_audience(fx, intention_id):
    with fx.ledger._connect() as conn:
        row = conn.execute("SELECT messages_json FROM turn_sources WHERE turn_id=?",
                           (f"mind:{intention_id}:finding",)).fetchone()
    return json.loads(row[0])[0]["metadata"].get("audience") if row else None


async def test_a_finding_is_public_only_when_it_researched_the_agents_own_interest(fx):
    """Findings are the owner's autobiography entries; only research into an interest the agent
    declared (not one the owner's own words raised, never an owner's question, goal step or an
    investigation of work done for the owner) is marked for everyone."""
    interest = await attempt(fx, 1, "done", summary="finding: the waggle run encodes distance.")
    assert _finding_audience(fx, interest.id) == "all"
    asked = Candidate(type="question", drive="curiosity", kind="task", title="Answer: custody hearing date",
                      dedup_key="question:custody-hearing:1", salience=0.9, cost=0.1,
                      text="Find the hearing date; report finding: <text>.", topic="custody hearing",
                      concern="custody hearing", evidence=["turn:ask-1"])
    raised = Candidate(type="research", drive="curiosity", kind="task", title="Research: garden soil",
                       dedup_key="research:garden-soil:1", salience=0.9, cost=0.1,
                       text="Find out about garden soil; report finding: <text>.", topic="garden soil",
                       concern="garden soil", evidence=["appraisal:a-7"])
    for n, item in enumerate((asked, raised), 2):
        fx.shift(minutes=5)
        row = await fx.mind._form(item, 0.9, fx.now)
        fx.mind.bound(row.id, f"kanban:{n}")
        fx.mind.outcomes.record(row.id, status="done", hermes_ref=f"kanban:{n}", summary="finding: in March.")
        assert _finding_audience(fx, row.id) == "owner"


async def test_opinions_off_forms_and_renders_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fx = Fixture(tmp_path, config=ROOMY)                           # no faculty flag: the faculty is off
    try:
        assert fx.mind.opinions is not None and fx.mind.opinions.enabled is False
        await three_failures(fx)
        assert approach(fx) is None
        store = fx.mind.opinions.store
        on = Opinions(store, fx.store, enabled=True)
        on.observe_outcome(fx.store.get(fx.store.intentions(kind=["task"], limit=1)[0].id), "failed", None)
        head = approach(fx)
        assert head is not None                                    # the same history forms it when on
        row = await fx.mind._form(candidate(4), 0.9, fx.now)
        assert "opinion_ids" not in row.context
        assert fx.mind.opinions.context("Which is better?", viewer_contact_id=OWNER, viewer_is_owner=True,
                                        session_id="s") == ""
    finally:
        fx.store.close()


# -- the context section ----------------------------------------------------------------------------

def stance(store, ledger, n, *, topic, text, reason="It rests on what was said.", revise_if="New figures.",
           outcome=None):
    """A stored view resting on the agent's reply in a recorded turn, or on a settled outcome (audience all)."""
    if outcome is not None:
        premises = [store.outcome_premise(outcome)]
    else:
        ledger.record_source(f"view-{n}", contact_id=OWNER, session_id=f"s-{n}", messages=[
            {"role": "user", "content": f"What do you think about {topic}?"},
            {"role": "assistant", "content": f"{text} Reason {n}."}], derive_claims=False)
        premises = store.statements(f"view-{n}")
    result = store.form(Proposal(subject_kind="topic", subject="", topic=topic, stance=text, reason=reason,
                                 certainty="moderate", revise_if=revise_if, premises=premises,
                                 source_ref=f"intention:i-{n}" if outcome is not None else f"turn:view-{n}"))
    assert result.disposition == "formed", result
    return result.stance_id


def test_the_section_is_bounded_cited_and_carries_the_standing_sentence(fx):
    store = fx.mind.opinions.store
    # Five views on five matters (views on one matter from the agent's words alone would be one view).
    for n, topic in enumerate(["kitchen herbs", "front lawn", "rose bushes", "vegetable patch", "fruit trees"], 1):
        stance(store, fx.ledger, n, topic=topic, text=f"Garden plan for the {topic}: " + "raised beds " * 35,
               reason="Because " + "the soil drains poorly " * 20, revise_if="A soil test " * 15)
    text = fx.mind.opinions.context("Which garden plan should we use?", viewer_contact_id=OWNER,
                                    viewer_is_owner=True, session_id="later")
    lines = [line for line in text.splitlines() if line.startswith("- Your recorded view on ")]
    assert 1 <= len(lines) <= 3 and all(len(line) <= 420 for line in lines)
    assert len(text) <= CONTEXT_CHARS and text.endswith(STANDING)
    assert "You may disagree and still do what the owner authorizes; say so when you do." in STANDING
    assert all("[opinion " in line and "Rests on:" in line and "(turn:view-" in line for line in lines)


def test_unweighed_newer_evidence_is_noted(fx):
    store = fx.mind.opinions.store
    stance(store, fx.ledger, 1, topic="garden plan", text="Raised beds suit this garden.")
    query = dict(query="Which garden plan should we use?", viewer_contact_id=OWNER, viewer_is_owner=True,
                 session_id="later")
    assert "has not been weighed" not in fx.mind.opinions.context(**query)
    fx.ledger.record_source("newer-1", contact_id=OWNER, session_id="later", messages=[
        {"role": "user", "content": "Record s-19: the soil test found clay under the whole plot."}])
    text = fx.mind.opinions.context(**query)
    assert "Newer evidence from " in text and "has not been weighed into these views yet" in text


def test_guests_see_only_everyone_audience_views_and_the_owner_gets_the_cue_line(fx):
    store = fx.mind.opinions.store
    stance(store, fx.ledger, 1, topic="garden plan", text="Raised beds suit this garden.")
    outcome = fx.store.create_intention(kind="task", type="research", title="Research garden soil", drive="curiosity",
                                        cls="internal", decision="act", decision_reason="fixture", status="done",
                                        dedup_key="soil-1", hermes_kind="none", created_at=fx.now)[0]
    fx.store.update(outcome.id, outcome="done", verified="check", result="clay soil", completed_at=fx.now)
    shared = stance(store, fx.ledger, 2, topic="garden soil", text="The soil is clay.",
                    outcome=fx.store.get(outcome.id))
    guest = fx.mind.opinions.context("Tell me about the garden plan and the soil", viewer_contact_id="p-02",
                                     viewer_is_owner=False, session_id="g")
    assert f"[opinion {shared}]" in guest and "Raised beds" not in guest
    owner = fx.mind.opinions.context("Tell me about the garden plan and the soil", viewer_contact_id=OWNER,
                                     viewer_is_owner=True, session_id="o")
    assert "Raised beds" in owner and "The soil is clay." in owner
    assert fx.mind.opinions.context("Should we repaint the shed?", viewer_contact_id=OWNER, viewer_is_owner=True,
                                    session_id="o") == CUE_LINE
    assert fx.mind.opinions.context("Should we repaint the shed?", viewer_contact_id="p-02", viewer_is_owner=False,
                                    session_id="g") == ""
    assert fx.mind.opinions.context("hello there", viewer_contact_id=OWNER, viewer_is_owner=True,
                                    session_id="o") == ""


def test_the_body_line_uses_the_same_signature_as_the_row(fx):
    """task_lines keys by failure_signature of the candidate as the row will store it."""
    from protagine.mind.drives import failure_signature
    row = {"type": "research", "context": {"topic": TOPIC, "concern": f"research {TOPIC}"},
           "description": f"Research {TOPIC}"}
    assert failure_signature(row) == SIGNATURE
    assert Opinions(None, None, enabled=False).task_lines(candidate(1)) == ("", [])
