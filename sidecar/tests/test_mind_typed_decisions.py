"""Round 3: the owner's stop, pause or resume and whether a report is a finding are typed decisions.

The deterministic readers decide the clear cases (the fast path) and are the fallback. Only their ambiguous band
(an instruction cue beside reported-speech markers; a null marker beside other words) is put to the main model,
as a small typed-JSON task on the mind's router. Unavailable or uncertain, the conservative fallback applies:
a stop or a pause stands (the owner can lift it), a resume is taken only as a reply linked to an outreach, and a
report goes to the digest (never sent alone, never discarded).
"""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from hypothesis import given, settings, strategies as st

from protagine.mind import decisions, outreach
from protagine.mind.reactions import read
from test_mind_outreach_loop import H, T0, make  # noqa: F401  (make is a pytest fixture)
from test_mind_outreach_reactions import say, shared

OWNER = "p-01"


class DecisionRouter:
    """The mind's router, faked: answers the typed decision tasks it is given; any other task is unavailable."""

    supports_function_routing = True

    def __init__(self, **answers):
        self.answers, self.calls = answers, []

    def function_deadline_seconds(self, *, context=None):
        return 5

    async def complete(self, messages, *, context=None, **_):
        task = (context or {}).get("task")
        self.calls.append({"task": task, "user": messages[-1]["content"], "context": context})
        if task == decisions.INSTRUCTION_TASK and "instruction" in self.answers:
            value = self.answers["instruction"]
            field = "instruction"
        elif task == decisions.SUBSTANCE_TASK and "verdict" in self.answers:
            value = self.answers["verdict"]
            field = "verdict"
        else:
            raise RuntimeError("endpoint down")
        if value == "garbage":
            return SimpleNamespace(content="I think they probably meant it.")
        return SimpleNamespace(content=json.dumps({field: value}))

    def asked(self, task):
        return [call for call in self.calls if call["task"] == task]


UNAVAILABLE = [None, DecisionRouter(), DecisionRouter(instruction="unsure", verdict="unsure"),
               DecisionRouter(instruction="garbage", verdict="garbage")]


def _paused(fx):
    return fx.mind.state()["outreach"]["paused_until"]


# -- the reader: the fast path and the fallback -----------------------------------------------------------------

REVIEWER_RESUME = 'Alice: "Hello\nYou can check in again.\nBye"\nWhat should I say?'


def test_the_reviewers_multiline_quotation_is_no_resume_and_is_put_to_the_model():
    reading = read(REVIEWER_RESUME)
    assert not reading.resume and "resume" in reading.unsure


@pytest.mark.parametrize("text", ["I insist: stop checking in", "I just wanted to say stop checking in",
                                  "I insist: stop checking in.", "Honestly, I wanted to say this: stop checking in!"])
def test_the_reviewers_first_person_stops_are_stops(text):
    """Re-check round 2, R4: without an outreach link, an explicit stop (an indefinite pause)."""
    reading = read(text)
    assert reading.stop and reading.stop_explicit, text


@pytest.mark.parametrize("text", ["Stop checking in.", "You can check in again.", "Ok, you can check in again.",
                                  "No messages today, please.", "Please stop checking in with me unprompted."])
def test_a_clear_instruction_is_decided_by_the_reader_alone(text):
    assert read(text).unsure == [], text


QUOTES = ['"{}"', "“{}”", "'{}'", '"Hi.\n{}\nBye."', '"Hello\n{}\nBye"', "«{}»", '"\n{}\n"']
FRAMES = ["{r}: {q}\nWhat should I say?", "{r} {v} {q}", "{r} {v}:\n{q}\nHow do I reply?", "> {q}\nWhat do I say to {r}?",
          "{q}, {r} {v}. Thoughts?", "Got this from {r}:\n{q}", "{r} {v}, and I quote, {q}", "According to {r}, {q}",
          "Should I tell {r} {q}?", "{r} keeps asking me whether {q}"]
REPORTERS = ["Alice", "My boss", "She", "They", "The landlord", "Kim"]
VERBS = ["said", "wrote", "texted", "told me", "just said", "already wrote", "insisted", "wants me to say"]
RESUMES = ["You can check in again.", "Feel free to reach out.", "Resume the check-ins.", "Check-ins are fine again."]
STOPS = ["Stop checking in.", "Stop messaging me.", "No more check-ins.", "Don't check in."]


@settings(max_examples=250, deadline=None)
@given(frame=st.sampled_from(FRAMES), quote=st.sampled_from(QUOTES), reporter=st.sampled_from(REPORTERS),
       verb=st.sampled_from(VERBS), words=st.sampled_from(RESUMES))
def test_a_resume_in_someone_elses_words_is_never_taken_without_the_model(frame, quote, reporter, verb, words):
    """Property: whatever the layout, a resume beside reported-speech markers is not taken by the reader; it is
    the model's to decide (``unsure``)."""
    text = frame.format(r=reporter, v=verb, q=quote.format(words))
    reading = read(text)
    assert not reading.resume and "resume" in reading.unsure, text


@settings(max_examples=250, deadline=None)
@given(frame=st.sampled_from(FRAMES), quote=st.sampled_from(QUOTES), reporter=st.sampled_from(REPORTERS),
       verb=st.sampled_from(VERBS), words=st.sampled_from(STOPS))
def test_a_stop_beside_reported_speech_stands_unless_the_model_says_otherwise(frame, quote, reporter, verb, words):
    """Property: a stop the reader cannot attribute is taken (a pause the owner can lift) and put to the model."""
    text = frame.format(r=reporter, v=verb, q=quote.format(words))
    reading = read(text)
    assert reading.stop and "stop" in reading.unsure, text


# -- the owner's instruction through the mind: model, unavailable, uncertain ------------------------------------

@pytest.mark.parametrize("router", UNAVAILABLE + [DecisionRouter(instruction="none")])
async def test_the_reviewers_quoted_resume_never_lifts_the_pause(make, router):
    """Re-check round 2, item 7: a pause, then a multi-line quotation holding "you can check in again"."""
    fx = make()
    await say(fx, "Please stop checking in with me unprompted.", "t-stop")
    fx.mind.router = router
    fx.shift(timedelta(minutes=30))
    summary = await say(fx, REVIEWER_RESUME, "t-quote", "owner-2")
    assert "resumed" not in summary["applied"] and _paused(fx) == outreach.INDEFINITE


@pytest.mark.parametrize("text", ["I insist: stop checking in", "I just wanted to say stop checking in"])
@pytest.mark.parametrize("router", UNAVAILABLE + [DecisionRouter(instruction="stop")])
async def test_the_reviewers_first_person_stops_pause_outreach_indefinitely(make, text, router):
    fx = make()
    fx.mind.router = router
    await say(fx, text, "t-stop")
    assert _paused(fx) == outreach.INDEFINITE


async def test_the_model_settles_a_stop_that_is_someone_elses(make):
    fx = make()
    fx.mind.router = router = DecisionRouter(instruction="none")
    summary = await say(fx, 'My boss wrote "stop checking in on the team every hour". How do I answer that?', "t-1")
    assert "paused" not in summary["applied"] and _paused(fx) is None
    call, = router.asked(decisions.INSTRUCTION_TASK)
    assert "stop checking in on the team" in call["user"] and call["context"]["response_schema"] == \
        decisions.INSTRUCTION_SCHEMA


@pytest.mark.parametrize("router", UNAVAILABLE)
async def test_without_the_model_a_stop_the_reader_cannot_attribute_pauses_and_the_owner_lifts_it(make, router):
    fx = make()
    fx.mind.router = router
    await say(fx, 'My boss wrote "stop checking in on the team every hour". How do I answer that?', "t-1")
    assert _paused(fx) == outreach.INDEFINITE
    fx.shift(timedelta(minutes=5))
    summary = await say(fx, "You can check in again.", "t-2")
    assert "resumed" in summary["applied"] and _paused(fx) is None


async def test_the_model_takes_a_resume_the_reader_cannot_attribute(make):
    fx = make()
    await say(fx, "Please stop checking in with me unprompted.", "t-stop")
    fx.mind.router = DecisionRouter(instruction="resume")
    fx.shift(timedelta(minutes=30))
    summary = await say(fx, "I said it before: you can check in again.", "t-go", "owner-2")
    assert "resumed" in summary["applied"] and _paused(fx) is None


@pytest.mark.parametrize("router", UNAVAILABLE)
async def test_without_the_model_an_unattributed_resume_needs_a_reply_linked_to_an_outreach(make, router):
    fx = make()
    await shared(fx)
    fx.mind.pause_outreach(None, by="owner")
    fx.mind.router = router
    fx.shift(timedelta(minutes=10))
    summary = await say(fx, "I said it before: you can check in again.", "t-go", "owner-2")
    assert "resumed" not in summary["applied"] and _paused(fx) == outreach.INDEFINITE
    summary = await say(fx, "Thanks for the tidal energy item. Like I said before: you can check in again.", "t-go2",
                        "owner-2")
    assert summary["linked"] and "resumed" in summary["applied"] and _paused(fx) is None


@pytest.mark.parametrize("text", ["Stop checking in.", "You can check in again.", "Not today.", "Dig deeper please."])
async def test_a_clear_turn_never_asks_the_model(make, text):
    fx = make()
    fx.mind.router = router = DecisionRouter(instruction="none")
    await say(fx, text, "t-1")
    assert router.asked(decisions.INSTRUCTION_TASK) == []


# -- is a report a finding: the reader, the model, the digest ------------------------------------------------

def _settled(summary, topic="tidal energy", decided=None):
    finding = outreach.Finding(id="f-1", type="research", topic=topic, slug=topic.replace(" ", "-"),
                               summary=summary, completed_at=T0, decided=decided)
    return outreach.settle(finding, outreach.OutreachInputs(now=T0, owner_id=OWNER))


NULLISH = ["nothing", "nothing new", "no news", "nothing much", "no real change", "nothing to speak of"]
STRAY = ["groundbreaking", "sadly", "unfortunately", "honestly", "exciting", "worth noting", "big", "dramatic",
         "surprising", "this time", "so far", "to be fair"]


@settings(max_examples=200, deadline=None)
@given(null=st.sampled_from(NULLISH), first=st.sampled_from(STRAY), second=st.sampled_from(STRAY),
       label=st.sampled_from(["Tidal energy: ", "", "Update on tidal energy: ", "finding: "]))
def test_a_null_report_with_stray_words_is_never_a_finding_by_the_reader(null, first, second, label):
    """Property (re-check round 2, item 9): a null marker with any stray words is never sent as a finding by the
    reader; it is empty or the model's to decide (uncertain: the digest)."""
    summary = f"{label}{null} {first}, {second}."
    assert _settled(summary) in {"empty", "uncertain"}, summary


QUALIFIERS = ["without any changes", "without changes", "with no changes", "without any conditions",
              "with no objections", "without any fuss", "with nothing left to decide"]
EVENTS = ["was approved", "was completed", "launched", "was signed", "opened"]


@settings(max_examples=100, deadline=None)
@given(event=st.sampled_from(EVENTS), qualifier=st.sampled_from(QUALIFIERS))
def test_a_qualified_milestone_is_never_empty_by_the_reader(event, qualifier):
    """Property (re-check round 2, R5): the topic's own milestone with a null-sounding qualifier is never
    discarded by the reader: a finding, or the model's to decide (uncertain: the digest)."""
    summary = f"The Orkney tidal energy project {event} {qualifier}."
    assert _settled(summary, "Orkney tidal energy project") in {None, "uncertain"}, summary


def test_the_reviewers_reports_are_the_models_to_decide():
    assert _settled("Tidal energy: nothing groundbreaking, sadly") == "uncertain"
    assert _settled("The Orkney tidal energy project was approved without any changes.",
                    "Orkney tidal energy project") == "uncertain"
    assert _settled("Tidal energy: nothing groundbreaking, sadly", decided="empty") == "empty"
    assert _settled("The Orkney tidal energy project was approved without any changes.", "Orkney tidal energy project",
                    decided="finding") is None


async def _report(fx, topic, summary, router):
    fx.owner_spoke()
    fx.mind.add_interest(topic, by="turn:t-1")
    fx.mind.router = router
    done = await fx.research(topic, summary)
    fx.shift(timedelta(minutes=30))
    await fx.tick()
    return fx.store.get(done.id)


async def test_the_model_decides_a_null_report_with_stray_words_is_empty(make):
    fx = make()
    router = DecisionRouter(verdict="empty")
    done = await _report(fx, "tidal energy", "finding: Tidal energy: nothing groundbreaking, sadly", router)
    assert fx.outreach_rows("outreach_finding") == [] and done.result_metadata["outreach"]["state"] == "empty"
    call, = router.asked(decisions.SUBSTANCE_TASK)
    assert "nothing groundbreaking" in call["user"] and call["context"]["response_schema"] == decisions.SUBSTANCE_SCHEMA


async def test_the_model_decides_a_qualified_milestone_is_a_finding(make):
    fx = make()
    done = await _report(fx, "Orkney tidal energy project",
                         "finding: The Orkney tidal energy project was approved without any changes.",
                         DecisionRouter(verdict="finding"))
    row, = fx.outreach_rows("outreach_finding")
    assert row.status == "sent" and "approved" in row.context["text"]
    assert done.result_metadata["outreach"]["substance_decision"] == "finding"


@pytest.mark.parametrize("summary, topic", [
    ("finding: Tidal energy: nothing groundbreaking, sadly", "tidal energy"),
    ("finding: The Orkney tidal energy project was approved without any changes.", "Orkney tidal energy project"),
])
@pytest.mark.parametrize("router", UNAVAILABLE)
async def test_an_undecided_report_goes_to_the_digest_never_alone_never_dropped(make, summary, topic, router):
    fx = make()
    done = await _report(fx, topic, summary, router)
    assert fx.outreach_rows("outreach_finding") == [] and done.result_metadata["outreach"]["state"] == "digest"
    fx.mind.digest_hour = fx.now.astimezone(fx.mind.tz).hour
    await fx.tick()
    digest, = [p["text"] for p in fx.sent if p["type"] == "digest"]
    assert summary.split(": ", 1)[1].split(",")[0][:20] in digest


@pytest.mark.parametrize("summary, state", [
    ("finding: QX-41: A practical study of tidal energy was published.", "sent"),
    ("finding: Nothing new on tidal energy this week.", "empty"),
])
async def test_a_clear_report_never_asks_the_model(make, summary, state):
    fx = make()
    router = DecisionRouter(verdict="empty" if state == "sent" else "finding")
    done = await _report(fx, "tidal energy", summary, router)
    assert router.asked(decisions.SUBSTANCE_TASK) == [] and done.result_metadata["outreach"]["state"] == state
