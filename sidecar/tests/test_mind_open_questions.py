"""The mind's open questions about a subject reach the owner's reply.

The mind found that the owner had said two things about one subject and asked which was right; a
later turn about that subject reached a model with no sign of the question or the disagreement, and
it answered the later value as settled. A question the mind asked the owner stays open while its two
statements still disagree; on the owner's own turn about that subject, the context carries it.
"""

from __future__ import annotations

from contextlib import closing

import pytest

from protagine.api.routers import host
from protagine.mind.questions import open_questions
from test_mind_consolidate import OWNER, TASK_DIGEST, fx  # noqa: F401  (pytest fixture)


async def _asked(fx):
    """Two statements about the owner's office, the night's question, and the question sent."""
    fx.router.answers[TASK_DIGEST] = None
    await fx.fact("turn-a", OWNER, "s-a", "My office is room 4.", "room 4")
    fx.shift(days=1)
    second = await fx.fact("turn-b", OWNER, "s-b", "My office is room 7.", "room 7")
    await fx.mind.consolidate()
    await fx.mind.tick(force=True)
    question, = fx.messages("contradiction")
    for payload in await fx.mind.outbox_ready():
        fx.mind.outbox.sending(payload["id"], target=f"cli:{payload['recipient']}")
        fx.mind.outbox.sent(payload["id"])
    assert fx.store.get(question.id).status == "sent"
    return question, second


async def test_a_sent_question_reaches_a_turn_about_its_subject(fx):
    question, _ = await _asked(fx)
    lines = open_questions(fx.mind, "Which office should visitors come to now?")
    assert len(lines) == 1 and "room 4" in lines[0] and "room 7" in lines[0]
    assert "no answer yet" in lines[0]
    assert open_questions(fx.mind, "What is on the lunch menu today?") == []


async def test_a_question_whose_statements_agree_now_is_closed(fx):
    question, second = await _asked(fx)
    with closing(fx.ledger._connect()) as conn, conn:
        conn.execute("UPDATE source_claims SET retracted_by='owner-correction' WHERE id=?", (second,))
    assert open_questions(fx.mind, "Which office should visitors come to now?") == []


async def test_the_owners_turn_context_carries_the_open_question(fx, monkeypatch):
    await _asked(fx)
    monkeypatch.setattr(host, "_mind", lambda: fx.mind)
    text = host._mind_open_questions("Remind me which office I work in?")
    assert "room 4" in text and "room 7" in text
    monkeypatch.setattr(host, "_mind", lambda: None)
    assert host._mind_open_questions("Remind me which office I work in?") == ""
