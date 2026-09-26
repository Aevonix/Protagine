"""What the owner's verdicts on outreach teach at night (architecture 4.8 and 4.10, M11).

An outreach message the owner rated is a result the owner verified: the night's lesson stage reads it
beside the agent's own work, a lesson it admits carries the message's class and topic
(``outreach_finding:<topic>``), and the next finding the lesson bears on is scored by it (a pitfall
halves it, a strategy lifts it a fifth).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from protagine.mind import outreach
from test_mind_consolidate import OWNER
from test_mind_lessons_night import label, make, night

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def shared(fx, topic="tidal energy", verdict="not_useful"):
    """An outreach finding that went out and that the owner rated."""
    row, _ = fx.store.create_intention(
        kind="message", type="outreach_finding", title=f"You said you care about {topic}, so I looked into it",
        drive="social", cls="owner", decision="act", decision_reason="standard: owner -> act", status="approved",
        dedup_key=f"outreach:finding:{topic}", recipient=OWNER,
        context={"topic": topic, "topic_slug": outreach._slug(topic), "text": f"On {topic}: QX-41 is out."},
        hermes_kind="message", created_at=fx.now)
    fx.store.transition(row.id, "approved", action="queued", at=fx.now)
    fx.mind.outbox.sending(row.id, target="capture:owner")
    fx.mind.outbox.sent(row.id)
    fx.mind.outcomes.rate(row.id, verdict, by="owner")
    return fx.store.get(row.id)


PITFALL = {"op": "add", "kind": "pitfall", "title": "Tidal energy findings are not wanted",
           "when_to_use": "a finding about tidal energy comes up",
           "content": "Do not message the owner about tidal energy; list it in the digest at most."}


async def test_a_rated_outreach_is_a_verified_result_the_night_reads_and_learns_from(tmp_path, monkeypatch):
    def answer(prompt):
        return {"verdicts": [], "ops": [{**PITFALL, "cites": [label(prompt, "outreach_finding")]}]}
    fx = make(tmp_path, monkeypatch, answer)
    row = shared(fx)
    assert [item.id for item in fx.mind.lessons._events(fx.now)] == [row.id]
    result = await night(fx)
    assert result["counts"]["lessons_admitted"] == 1 and result["errors"] == []
    _, prompt = fx.router.prompts[0]
    assert "message outreach_finding" in prompt and "On tidal energy: QX-41 is out." in prompt
    [lesson] = fx.mind.lessons.all()
    assert lesson.signature == "outreach_finding:tidal-energy" and lesson.kind == "pitfall"
    assert lesson.verified == "owner" and lesson.evidence == [f"intention:{row.id}"]
    fx.store.close()


async def test_an_unrated_or_silent_outreach_is_not_a_verified_result(tmp_path, monkeypatch):
    fx = make(tmp_path, monkeypatch)
    shared(fx, "fern species", verdict="ignored")
    assert fx.mind.lessons._events(fx.now) == []
    fx.store.close()


async def test_an_owner_verified_outreach_lesson_scores_the_next_finding_it_bears_on(tmp_path, monkeypatch):
    def answer(prompt):
        return {"verdicts": [], "ops": [{**PITFALL, "cites": [label(prompt, "outreach_finding")]}]}
    fx = make(tmp_path, monkeypatch, answer)
    shared(fx)
    await night(fx)
    fx.mind.add_interest("tidal energy", by="turn:t-1")
    fx.mind.add_interest("fern species", by="turn:t-2")
    # A finding waiting to be shared: the owner branch reads the lessons only then.
    research, _ = fx.store.create_intention(
        kind="task", type="research", title="Research: tidal energy", drive="curiosity", cls="internal",
        decision="act", decision_reason="r", status="dispatched", dedup_key="research:tidal", hermes_kind="kanban",
        context={"topic": "tidal energy"}, created_at=fx.now)
    fx.mind.outcomes.record(research.id, status="done", summary="finding: QX-41 on tidal energy is out.")
    state = (await fx.mind._gather(fx.now)).outreach
    assert [lesson.signature for lesson in state.lessons] == ["outreach_finding:tidal-energy"]

    def finding(topic):
        return outreach.Finding(id="i-9", type="research", topic=topic, slug=outreach._slug(topic),
                                summary=f"A practical study of {topic} was published.",
                                completed_at=fx.now - timedelta(minutes=10))
    assert outreach.relevance(finding("tidal energy"), state)[0] == pytest.approx(0.5)
    assert outreach.relevance(finding("fern species"), state)[0] == pytest.approx(1.0)
    # A lesson the agent learned from its own work (not the owner's verdict) never scores outreach.
    fx.mind.lessons.admit({"signature": "research:fern-species", "kind": "pitfall", "title": "Fern species findings",
                           "when_to_use": "a finding about fern species", "content": "Skip it."},
                          verified="check", origin="night", status="active", evidence=["intention:x"], lineage=[],
                          now=fx.now)
    state = (await fx.mind._gather(fx.now)).outreach
    assert outreach.relevance(finding("fern species"), state)[0] == pytest.approx(1.0)
    fx.store.close()
