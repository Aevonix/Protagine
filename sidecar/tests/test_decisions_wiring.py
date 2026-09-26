"""The decision points the fast decision layer reads (``protagine.decisions``): where the model's typed answer
may change what the existing path decided, and where its silence (disabled, failed, unsure) leaves it as it was."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from protagine.commitments.extract import CommitmentExtractor
from protagine.commitments.store import CommitmentStore
from protagine.contacts.config import ContactsConfig
from protagine.contacts.optout import apply_opt_out
from protagine.contacts.store import SQLiteContactStore
from protagine.decisions import Decision
from protagine.mind import outreach
from protagine.turns import TurnIdempotencyLedger
from test_commitment_first_mention_hold import _Router, _item
from test_mind_interest_settled import CaptureRouter, fx  # noqa: F401  (fx is a pytest fixture)
from test_mind_lessons_night import OWN_RULE, add, judged, label, make as lesson_fixture, night, two_requests
from test_mind_loop import OWNER
from test_mind_outreach_loop import make  # noqa: F401  (make is a pytest fixture)
from test_mind_outreach_reactions import reaction, say, shared


class Decider:
    """A decision model that answers ``answers[point]`` (a label, or None for no answer) and keeps every call."""

    def __init__(self, **answers):
        self.answers, self.calls = answers, []

    def enabled(self, point):
        return point in self.answers

    async def decide(self, point, **fields):
        self.calls.append((point, fields))
        answer = self.answers.get(point)
        return None if answer is None else Decision(point, answer, 0.97, {answer: 0.97}, 12.0)


def served(answer, *, point, truncated=False):
    """The real decision client, with ``point`` enabled, over an endpoint that always gives ``answer``."""
    from protagine import decisions
    body = {"protocol": "local-decision.v1", "backend": {"name": "fake"}, "elapsed_ms": 3.0,
            "usage": {"input_tokens": 40, "output_tokens": 0}, "input_truncated": truncated,
            "answers": {decisions._QUESTION: answer}}
    return decisions.Decider("http://decide.test", points={point: {"enabled": True}},
                             transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))


# -- the owner's reply to an outreach -----------------------------------------------------------------------

NO_CUE = "Ah, the tidal energy item you sent reminds me of a trip I took."


async def test_a_linked_reply_the_phrases_do_not_read_takes_the_decision_models_reading(make):
    fx = make()
    row = await shared(fx)
    fx.shift(timedelta(minutes=5))
    fx.mind.decisions = Decider(outreach_reply="not_interested")
    await say(fx, NO_CUE, "t-1", "owner-2")
    assert reaction(fx, row)["class"] == "negative" and reaction(fx, row)["read"] == "decision"
    assert fx.store.get(row.id).verdict == "not_useful"
    assert fx.mind.mind_state.get("outreach.mute:tidal-energy")["level"] == 1.0
    [(point, fields)] = fx.mind.decisions.calls
    assert point == "outreach_reply" and fields == {"text": NO_CUE, "topic": "tidal energy"}


async def test_no_answer_from_the_decision_model_leaves_the_reply_as_engagement(make):
    fx = make()
    row = await shared(fx)
    fx.shift(timedelta(minutes=5))
    fx.mind.decisions = Decider(outreach_reply=None)
    await say(fx, NO_CUE, "t-1", "owner-2")
    assert reaction(fx, row)["class"] == "engaged" and "read" not in reaction(fx, row)
    assert len(fx.mind.decisions.calls) == 1


async def test_a_reply_the_phrases_read_is_never_sent_to_the_decision_model(make):
    fx = make()
    row = await shared(fx)
    fx.shift(timedelta(minutes=5))
    fx.mind.decisions = Decider(outreach_reply="stop")
    await say(fx, "That tidal energy item you sent was not useful to me.", "t-1", "owner-2")
    assert reaction(fx, row)["class"] == "negative" and fx.mind.decisions.calls == []


async def test_a_stop_the_decision_model_reads_in_a_linked_reply_pauses_outreach(make):
    fx = make()
    row = await shared(fx)
    fx.shift(timedelta(minutes=5))
    fx.mind.decisions = Decider(outreach_reply="stop")
    summary = await say(fx, NO_CUE, "t-1", "owner-2")
    assert reaction(fx, row)["class"] == "stop" and "paused" in summary["applied"]
    assert outreach.pause_until(fx.mind.mind_state.get(outreach.PAUSE_KEY)) is not None


async def test_a_stop_read_from_a_malformed_choice_leaves_the_reply_as_engagement(make):
    fx = make()
    row = await shared(fx)
    fx.shift(timedelta(minutes=5))
    faint_stop = {"engaged": 0, "dig_deeper": 0, "not_interested": 0, "not_now": 0, "stop": 0.05}
    fx.mind.decisions = served({"type": "choice", "choice": "stop", "probabilities": faint_stop, "confidence": 0.05},
                               point="outreach_reply")
    summary = await say(fx, NO_CUE, "t-1", "owner-2")
    assert reaction(fx, row)["class"] == "engaged" and "paused" not in summary["applied"]
    assert outreach.pause_until(fx.mind.mind_state.get(outreach.PAUSE_KEY)) is None
    assert fx.mind.decisions.stats["outreach_reply"]["failed"] == 1


# -- a contact's opt-out ------------------------------------------------------------------------------------

@pytest.fixture
async def contacts():
    value = SQLiteContactStore(ContactsConfig(sqlite_path=":memory:"))
    await value.connect()
    yield value
    await value.close()


async def test_an_opt_out_the_phrases_miss_is_one_when_the_decision_model_says_so(contacts):
    owner = await contacts.create(display_name="Owner", may_contact="auto")
    contact = await contacts.create(display_name="Contact", may_contact="auto")
    decider = Decider(opt_out="yes")
    lowered = await apply_opt_out(contacts, contact.contact_id, "Lose my number.", source_ref="turn:1",
                                  owner_id=owner.contact_id, decider=decider)
    assert lowered is not None and lowered.may_contact == "never"
    [row] = [row for row in await contacts.get_audit_log(contact.contact_id) if row["action"] == "opt_out"]
    assert "decision model" in row["detail"] and "Lose my number" not in row["detail"]
    assert decider.calls == [("opt_out", {"text": "Lose my number."})]


async def test_no_answer_or_a_no_leaves_the_contact_as_the_phrases_did(contacts):
    owner = await contacts.create(display_name="Owner", may_contact="auto")
    contact = await contacts.create(display_name="Contact", may_contact="auto")
    for answer in (None, "no"):
        assert await apply_opt_out(contacts, contact.contact_id, "Lose my number.", source_ref="turn:1",
                                   owner_id=owner.contact_id, decider=Decider(opt_out=answer)) is None
    assert (await contacts.get(contact.contact_id)).may_contact == "auto"


async def test_a_phrase_opt_out_and_the_owner_never_reach_the_decision_model(contacts):
    owner = await contacts.create(display_name="Owner", may_contact="auto")
    contact = await contacts.create(display_name="Contact", may_contact="auto")
    decider = Decider(opt_out="yes")
    assert await apply_opt_out(contacts, owner.contact_id, "Lose my number.", source_ref="turn:1",
                               owner_id=owner.contact_id, decider=decider) is None
    lowered = await apply_opt_out(contacts, contact.contact_id, "Please remove me.", source_ref="turn:2",
                                  owner_id=owner.contact_id, decider=decider)
    assert lowered.may_contact == "never" and decider.calls == []


# -- a new item the person wants no reminders about ---------------------------------------------------------

def capture(tmp_path, monkeypatch, text):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    commitments = CommitmentStore(tmp_path / "commitments.db")
    ledger = TurnIdempotencyLedger(tmp_path / "turn-idempotency.db")
    extractor = CommitmentExtractor(ledger, lambda: commitments)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    ledger.record_source("t-1", contact_id=OWNER, session_id="owner-1", occurred_at=now.isoformat(), messages=[
        {"role": "user", "content": text}, {"role": "assistant", "content": "Understood."}])
    return extractor, commitments, now


HANDLED = "The tax form for p-05 is due in two hours. I've got it, no pings about it please."


async def test_a_reminder_the_decision_model_reads_as_unwanted_is_created_held(tmp_path, monkeypatch):
    extractor, commitments, now = capture(tmp_path, monkeypatch, HANDLED)
    extractor.decisions = Decider(no_reminders="yes")
    due = (now + timedelta(hours=2)).isoformat()
    item = {**_item("Send p-05 the tax form", due_at=due), "metadata": {"heads_up_at": due}}
    assert await extractor.process_one(_Router([item])) is True
    [row] = commitments.get_pending_for_person(OWNER)
    assert row["due_at"] is None and "heads_up_at" not in (row["metadata"] or {})
    [(point, fields)] = extractor.decisions.calls
    assert point == "no_reminders" and fields == {"item": "Send p-05 the tax form", "text": HANDLED}


async def test_no_answer_keeps_the_reminder_the_capture_call_set(tmp_path, monkeypatch):
    extractor, commitments, now = capture(tmp_path, monkeypatch, HANDLED)
    extractor.decisions = Decider(no_reminders=None)
    due = (now + timedelta(hours=2)).isoformat()
    assert await extractor.process_one(_Router([_item("Send p-05 the tax form", due_at=due)])) is True
    [row] = commitments.get_pending_for_person(OWNER)
    assert row["due_at"] is not None


async def test_a_held_item_and_a_message_to_a_contact_are_never_asked_about(tmp_path, monkeypatch):
    extractor, commitments, now = capture(tmp_path, monkeypatch, "Tell p-05 at five that the form is in. "
                                                                 "The receipt I will handle, no reminders.")
    extractor.decisions = Decider(no_reminders="yes")
    due = (now + timedelta(hours=2)).isoformat()
    notice = {**_item("Tell p-05 the form is in", due_at=due),
              "metadata": {"kind": "notice", "recipient": "p-05", "content": "The form is in.",
                           "asked": "Tell p-05 at five that the form is in", "grant": "owner"},
              "obligor": "assistant"}
    assert await extractor.process_one(_Router([notice, _item("Send p-05 the receipt")])) is True
    assert extractor.decisions.calls == []


# -- the owner settling an interest -------------------------------------------------------------------------

SATISFIED = "Found a good video on tide tables, so I'm sorted there."


async def test_an_interest_the_owner_settles_in_words_the_capture_call_missed_is_settled(fx):
    fx.mind.add_interest("tide tables")
    fx.mind.add_interest("moss lawns")
    fx.mind.capture.decisions = Decider(interest_settled="yes")
    fx.turn("t-settle", OWNER, SATISFIED, "Good to hear.")
    assert await fx.mind.capture.process_one(CaptureRouter(lambda prompt: [])) is True
    assert fx.mind.open_interests() == ["moss lawns"]
    assert fx.mind.capture.decisions.calls == [("interest_settled", {"topic": "tide tables", "text": SATISFIED})]


async def test_an_interest_stays_open_without_an_answer_or_when_the_turn_does_not_name_it(fx):
    fx.mind.add_interest("tide tables")
    fx.mind.capture.decisions = Decider(interest_settled=None)
    fx.turn("t-1", OWNER, SATISFIED, "Good to hear.")
    assert await fx.mind.capture.process_one(CaptureRouter(lambda prompt: [])) is True
    assert fx.mind.open_interests() == ["tide tables"]
    fx.mind.capture.decisions = Decider(interest_settled="yes")
    fx.turn("t-2", OWNER, "The lease is signed, so I'm sorted there.", "Good.")
    assert await fx.mind.capture.process_one(CaptureRouter(lambda prompt: [])) is True
    assert fx.mind.open_interests() == ["tide tables"] and fx.mind.capture.decisions.calls == []


# -- whether an owner message is a verdict on the agent's work ----------------------------------------------

def reported_request(prompt):
    """The lesson call reporting the owner's second request (it follows the agent's reply) as a verdict."""
    second = label(prompt, "Order 5522 came in")
    return {"verdicts": [judged(prompt, "Order 5522 came in", "What is its code?", work_was="right")],
            "ops": [add([second], quote="What is its code?", content=OWN_RULE)]}


async def test_a_reported_verdict_the_decision_model_reads_as_a_request_is_withdrawn(tmp_path, monkeypatch):
    fx = lesson_fixture(tmp_path, monkeypatch, reported_request)
    fx.mind.lessons.decisions = Decider(owner_verdict="no")
    two_requests(fx)
    result = await night(fx)
    assert result["counts"]["lesson_verdicts_vetoed"] == 1 and result["counts"]["lesson_ops_rejected"] == 1
    assert fx.mind.lessons.all(include_closed=True) == []
    [(point, fields)] = fx.mind.lessons.decisions.calls
    assert point == "owner_verdict" and fields["reply"] == "The order code is C4411."
    assert fields["text"].startswith("Order 5522 came in")
    fx.store.close()


async def test_no_answer_leaves_the_reported_verdict_standing(tmp_path, monkeypatch):
    fx = lesson_fixture(tmp_path, monkeypatch, reported_request)
    fx.mind.lessons.decisions = Decider(owner_verdict=None)
    two_requests(fx)
    result = await night(fx)
    assert "lesson_verdicts_vetoed" not in result["counts"] and result["counts"].get("lessons_admitted") == 1
    fx.store.close()


async def test_a_veto_read_from_a_cut_input_leaves_the_reported_verdict_standing(tmp_path, monkeypatch):
    fx = lesson_fixture(tmp_path, monkeypatch, reported_request)
    fx.mind.lessons.decisions = served({"type": "noul", "noul": 0.01, "confidence": 0.99}, point="owner_verdict",
                                       truncated=True)
    two_requests(fx)
    result = await night(fx)
    assert "lesson_verdicts_vetoed" not in result["counts"] and result["counts"].get("lessons_admitted") == 1
    assert fx.mind.lessons.decisions.stats["owner_verdict"]["failed"] >= 1
    fx.store.close()


def test_the_points_are_the_ones_wired():
    from protagine.decisions import POINTS
    assert set(POINTS) == {"outreach_reply", "opt_out", "no_reminders", "interest_settled", "owner_verdict"}
    assert json.dumps(sorted(POINTS))
