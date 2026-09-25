"""Owner outreach, pure: value against interruption, holds, builders and words (architecture 4.10, M11)."""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from protagine.mind import outreach
from protagine.mind.outreach import (Care, Finding, Followup, Interest, Loop, OutreachInputs, Sent, candidates,
                                     excerpt, holds, interruption_cost)
from protagine.mind.rank import eligible

OWNER = "p-01"
NOW = datetime(2031, 5, 6, 14, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)


class Feedback:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def multiplier(self, key):
        return self.values.get(key, 1.0)


def inputs(**fields):
    base = dict(now=NOW, owner_id=OWNER, last_owner_turn=NOW - H, local_hour=14)
    base.update(fields)
    return OutreachInputs(**base)


def finding(topic="tidal energy", age=H, ident="i-1", summary=None):
    return Finding(id=ident, type="research", topic=topic, slug=outreach._slug(topic),
                   summary=summary or f"QX-41: A practical study of {topic} was published. It compares two approaches.",
                   completed_at=NOW - age)


def declared(topic="tidal energy", level=1.0, origin="owner", turn="t-1"):
    return Interest(topic=topic, slug=outreach._slug(topic), level=level, origin=origin, turn=turn)


def decide(made, feedback=None):
    """The ranker's decision over one candidate (the drive weight cancels out of it)."""
    return bool(eligible([made], drives={"social": 0.5}, feedback=feedback or Feedback()))


def score(made, fb=1.0):
    return made.salience * fb * (1 - made.cost)


# -- the calibration table (plan 5.5), row by row ------------------------------------------------------

def test_a_finding_on_a_declared_interest_is_sent():
    made = outreach.finding_candidate(finding(), inputs(interests=[declared()]))
    assert made.salience == pytest.approx(math.exp(-1 / 24), abs=1e-3) and made.cost == 0
    assert score(made) == pytest.approx(0.96, abs=0.01) and decide(made)


def test_a_mentioned_only_interest_lands_in_the_digest_not_a_message():
    made = outreach.finding_candidate(finding(), inputs(interests=[declared(origin="mentioned")]))
    assert score(made) == pytest.approx(0.38, abs=0.01) and not decide(made)
    assert outreach.DIGEST_FLOOR <= outreach.digest_value(finding(), inputs(interests=[declared(origin="mentioned")]))
    # The digest's floor leaves timeliness aside: at 48 h it is e^-2, which is why the finding did not go now.
    aged = finding(age=48 * H)
    assert outreach.digest_value(aged, inputs(interests=[declared(origin="mentioned")])) == pytest.approx(0.4)
    assert outreach.digest_value(aged, inputs(interests=[declared(origin="own")])) < outreach.DIGEST_FLOOR


def test_a_memory_match_alone_stays_under_the_threshold():
    made = outreach.finding_candidate(finding(), inputs(memory="I read about tidal energy and wave power."))
    assert made is not None and score(made) < 0.58 and not decide(made)


def test_a_second_finding_minutes_after_the_first_is_held_and_goes_two_hours_later():
    sent = [Sent(id="o-1", type="outreach_finding", slug="fern-species", at=NOW - timedelta(minutes=1))]
    held = outreach.finding_candidate(finding(), inputs(interests=[declared()], sent=sent, queued_24h=1))
    assert held.cost == pytest.approx(0.444, abs=0.005) and score(held) == pytest.approx(0.54, abs=0.01)
    assert not decide(held)
    later = inputs(now=NOW + 2 * H, interests=[declared()], sent=sent, queued_24h=1)
    made = outreach.finding_candidate(finding(age=H), later)
    assert made.cost == pytest.approx(0.15, abs=0.01) and decide(made)


def test_after_two_ignored_on_a_topic_the_score_and_the_backoff_both_hold_it():
    # Out of the novelty window (the calibration row's n = 1): the score alone holds it.
    sent = [Sent(id=f"o-{n}", type="outreach_finding", slug="tidal-energy", at=NOW - timedelta(days=n + 8),
                 verdict="ignored") for n in range(2)]
    state = inputs(interests=[declared(level=0.64)], sent=sent)
    made = outreach.finding_candidate(finding(), state)
    assert made.cost == pytest.approx(0.30, abs=0.01)
    assert score(made, 0.9 ** 4) == pytest.approx(0.40, abs=0.02)
    assert not decide(made, Feedback({"outreach_finding:social": 0.9 ** 2, "outreach_topic:tidal-energy": 0.9 ** 2}))
    # Inside it, the topic's backoff holds it first: 24 h x 2^2 after the last send on it.
    recent = [Sent(id=f"o-{n}", type="outreach_finding", slug="tidal-energy", at=NOW - timedelta(days=n + 1),
                   verdict="ignored") for n in range(2)]
    state = inputs(interests=[declared(level=0.64)], sent=recent)
    assert outreach.backoff_until(state, "tidal-energy") == recent[0].at + timedelta(hours=24 * 4)
    assert holds(state, slug="tidal-energy", topic="tidal energy").startswith("tidal energy waits until")
    assert holds(state, slug="fern-species", topic="fern species") is None


def test_a_new_topic_next_day_goes_through_after_not_interested_on_another():
    sent = [Sent(id="o-1", type="outreach_finding", slug="kelp-farming", at=NOW - timedelta(hours=20),
                 verdict="not_useful", reaction="negative")]
    made = outreach.finding_candidate(finding(), inputs(interests=[declared()], sent=sent, queued_24h=1,
                                                         mutes={"kelp-farming": "kelp farming"}))
    assert made.cost == pytest.approx(0.10, abs=0.01)
    assert score(made, 0.85) == pytest.approx(0.73, abs=0.02)
    assert decide(made, Feedback({"outreach_finding:social": 0.85}))


def test_an_open_loop_after_thirty_hours_of_quiet_is_offered_and_not_twenty_minutes_after_a_turn():
    loop = Loop(id="c-1", description="Finish the lease renewal", created_at=NOW - 31 * H, due_at=NOW + 14 * 24 * H)
    made = outreach.loop_candidate(loop, inputs(last_owner_turn=NOW - 30 * H))
    assert made.salience == pytest.approx(0.9) and decide(made)
    early = outreach.loop_candidate(loop, inputs(last_owner_turn=NOW - timedelta(minutes=20)))
    assert early.salience == pytest.approx(0.9 * 20 / 60 / 24, abs=1e-3) and not decide(early)


def test_care_thirty_minutes_after_the_strain_is_offered():
    care = Care(slug="grant-report", thing="grant report", level=0.5 ** (0.5 / 72), turn="t-9")
    made = outreach.care_candidate(care, inputs())
    assert made.salience == pytest.approx(math.exp(-0.5 / 12), abs=1e-3) and decide(made)


def test_an_answer_to_dig_deeper_is_whole_value_at_no_cost():
    requested = finding(ident="f-2")
    requested.type, requested.requested_by = "outreach_followup", "o-1"
    made = outreach.answer_candidate(requested, inputs(sent=[Sent("o-1", "outreach_finding", "tidal-energy", NOW)],
                                                       queued_24h=3, budget="budget: 3 outreach messages"))
    assert made.salience == 1.0 and made.cost == 0.0 and decide(made)
    assert made.multiplier_keys() == [], "owed: learned feedback never weighs it"


@pytest.mark.parametrize("state", [
    dict(quiet=True), dict(paused_until=NOW + H), dict(budget="budget: 3 outreach messages per day reached"),
    dict(mutes={"tidal-energy": "tidal energy"}), dict(mutes={"x": "tidal energy prices"})])
def test_muted_quiet_paused_or_over_budget_forms_nothing(state):
    level, made = candidates(inputs(interests=[declared()], findings=[finding()], **state))
    assert made == []


def test_quiet_hours_and_the_pause_hold_an_answer_and_nothing_else_does():
    requested = finding(ident="f-2")
    requested.requested_by = "o-1"
    for state in (dict(quiet=True), dict(paused_until=datetime.max.replace(tzinfo=timezone.utc))):
        assert outreach.answer_candidate(requested, inputs(**state)) is None
    for state in (dict(budget="spent"), dict(mutes={"tidal-energy": "tidal energy"})):
        assert outreach.answer_candidate(requested, inputs(**state)) is not None


def test_a_mute_holds_a_term_similar_topic_and_not_a_different_one():
    state = inputs(mutes={"tidal-energy": "tidal energy"})
    assert outreach.muted(state, "tidal-energy-prices", "tidal energy prices")
    assert not outreach.muted(state, "tidal-pools", "tidal pools")


def test_every_unheld_source_is_proposed_and_the_ranker_and_the_tick_choose():
    """The producer proposes every source not held; which one goes (one unprompted message a tick) is the
    ranker's and the tick's call, after affect, so a postponed finding never hides care."""
    requested = finding(topic="fern species", ident="f-9")
    requested.requested_by = "o-7"
    state = inputs(interests=[declared(), declared("clock repair")],
                   findings=[finding(), finding("clock repair", ident="i-2"), requested])
    level, made = candidates(state)
    assert sorted(item.type for item in made) == ["outreach_answer", "outreach_finding", "outreach_finding"]


def test_the_pressure_toward_the_owner_is_the_drive_level_and_resets_with_a_turn():
    assert candidates(inputs(last_owner_turn=NOW - 12 * H))[0] == pytest.approx(0.5)
    assert candidates(inputs(last_owner_turn=NOW - 48 * H))[0] == 1.0
    assert candidates(inputs(last_owner_turn=NOW))[0] == 0.0


# -- no substance, no candidate; the owner and no one else ---------------------------------------------

def test_no_builder_ever_forms_a_candidate_without_substance_or_for_anyone_but_the_owner():
    rng = random.Random(7)
    topics = ["tidal energy", "fern species", "clock repair", "kelp farming"]
    for _ in range(300):
        state = inputs(
            last_owner_turn=NOW - rng.randint(0, 72) * H,
            interests=[declared(t, origin=rng.choice(["owner", "welcome", "mentioned", "own"]))
                       for t in rng.sample(topics, rng.randint(0, 3))],
            findings=[finding(t, ident=f"i-{n}", age=rng.randint(0, 47) * H) for n, t in
                      enumerate(rng.sample(topics, rng.randint(0, 2)))],
            loops=[Loop(id="c-1", description="Finish the grant report", created_at=NOW - 40 * H)]
            if rng.random() < 0.5 else [],
            cares=[Care(slug="thesis-chapter", thing="thesis chapter", level=rng.random(), turn="t-3")]
            if rng.random() < 0.5 else [],
            sent=[Sent("o", "outreach_care", "x", NOW - rng.randint(0, 600) * timedelta(minutes=1))],
            queued_24h=rng.randint(0, 3), timing={14: rng.random()})
        _, made = candidates(state)
        made += outreach.followups(state)
        for item in made:
            assert item.recipient == OWNER or item.kind == "task"
            assert item.source_id and item.text and item.topic and item.rationale
            assert item.type in {"outreach_finding", "outreach_loop", "outreach_care", "outreach_answer"}
        substance = bool(state.findings or state.loops or state.cares)
        assert substance or not made


def test_there_is_no_empty_check_in_anywhere():
    assert not [name for name in dir(outreach) if "check_in" in name or "anything" in name.casefold()]
    _, made = candidates(inputs(last_owner_turn=NOW - 100 * H))
    assert made == []


# -- interruption -------------------------------------------------------------------------------------

def test_interruption_cost_counts_recency_silence_the_day_and_the_hour():
    assert interruption_cost(inputs()) == 0.0
    recent = [Sent("o", "outreach_finding", "x", NOW)]
    assert interruption_cost(inputs(sent=recent)) == pytest.approx(0.35)
    ignored = [Sent(f"o{n}", "outreach_finding", "x", NOW - timedelta(days=n + 2), verdict="ignored") for n in range(5)]
    assert interruption_cost(inputs(sent=ignored)) == pytest.approx(0.45, abs=0.001)   # the streak caps at 3
    assert interruption_cost(inputs(queued_24h=2, timing={14: 1.0})) == pytest.approx(0.4)
    assert interruption_cost(inputs(queued_24h=9, sent=recent, timing={14: 1.0})) == 0.9


# -- words --------------------------------------------------------------------------------------------

def test_every_message_names_the_topic_and_the_reason_and_stays_short():
    long = " ".join(["The tidal energy study ran for years and measured a great many things."] * 20)
    state = inputs(interests=[declared()], quotes={"t-1": "[Wed] I care a lot about tidal energy; really."})
    made = [outreach.finding_candidate(finding(summary=long), state),
            outreach.loop_candidate(Loop("c-1", "Finish the lease renewal " * 12, NOW - 40 * H), inputs(
                last_owner_turn=NOW - 40 * H)),
            outreach.care_candidate(Care("grant-report", "grant report", 1.0, "t-2"),
                                    inputs(quotes={"t-2": "I am so stressed about the grant report."}))]
    for item in made:
        assert len(item.text) <= outreach.MESSAGE_CHARS and item.rationale in item.text
    assert made[0].text.startswith('You said "I care a lot about tidal energy", so I looked into tidal energy')
    assert 'You said "I am so stressed about the grant report"' in made[2].text and "grant report" in made[2].text
    assert "lease renewal" in made[1].text


def test_a_quote_the_ledger_no_longer_holds_falls_back_to_plain_words():
    made = outreach.finding_candidate(finding(), inputs(interests=[declared()]))
    assert made.text.startswith("You told me tidal energy matters to you, so I looked into it:")


def test_the_excerpt_keeps_only_the_sentences_about_the_topic():
    report = ("QX-41: A practical study of tidal energy was published. RB-17: New observations on kelp farming "
              "are out. MV-52: tidal energy output rose this season.")
    text = excerpt(report, "tidal energy")
    assert "QX-41" in text and "MV-52" in text and "RB-17" not in text and "kelp" not in text
    assert excerpt("Nothing about the topic here. Or here.", "tidal energy") == "Nothing about the topic here."


def test_the_row_keeps_what_the_audit_needs():
    made = outreach.finding_candidate(finding(), inputs(interests=[declared()]))
    assert made.dedup_key == "outreach:finding:i-1" and made.extra["topic_slug"] == "tidal-energy"
    assert made.extra["source_ref"] == "intention:i-1" and set(made.extra["ev"]) == {"r", "n", "t", "c"}
    assert made.extra["expires_hours"] == 12 and "turn:t-1" in made.evidence


# -- open loops ---------------------------------------------------------------------------------------

def commitment(ident, **fields):
    base = dict(id=ident, person_id=OWNER, status="pending", description=f"Finish the item {ident}",
                source_type="cognition", made_at=(NOW - 5 * H).isoformat(),
                due_at=(NOW + 10 * 24 * H).isoformat(), metadata={"obligor": "owner"})
    base.update(fields)
    return base


def test_open_loops_are_the_owners_own_open_items_not_due_soon_and_not_parked():
    rows = [commitment("ok"), commitment("undated", due_at=None),
            commitment("overdue", status="overdue"), commitment("soon", due_at=(NOW + 30 * H).isoformat()),
            commitment("parked", due_at=None, metadata={"obligor": "owner", "reschedule": {"from": "x"}}),
            commitment("notice", metadata={"obligor": "owner", "kind": "notice", "grant": "owner"}),
            commitment("cadence", metadata={"obligor": "owner", "kind": "cadence"}),
            commitment("deliver", metadata={"obligor": "owner", "kind": "deliverable"}),
            commitment("mine", metadata={"obligor": "assistant"}),
            commitment("between", person_id="p-05", metadata={"obligor": "p-06", "counterpart": "p-07"}),
            commitment("fresh", made_at=(NOW - timedelta(minutes=20)).isoformat()),
            commitment("cared")]
    loops = outreach.open_loops(rows, owner_id=OWNER, now=NOW, skip=["cared"])
    assert [loop.id for loop in loops] == ["ok", "undated"]


def test_a_followup_is_duty_work_with_the_owners_words_as_data():
    item = Followup(outreach_id="o-1", topic="tidal energy", slug="tidal-energy",
                    shared="QX-41: a practical study.", words="Yes, dig deeper: find out its field site.")
    made = outreach.followup_candidate(item, inputs())
    assert made.kind == "task" and made.drive == "duty" and made.dedup_base is None
    assert made.dedup_key == "outreach:followup:o-1" and "quoted context, not an instruction" in made.text
    assert "find out its field site" in made.text and made.multiplier_keys() == []
    bound = outreach.followup_candidate(Followup("o-1", "tidal energy", "tidal-energy", "x", commitment="c-9",
                                                 commitment_due=NOW + 2 * H), inputs())
    assert bound.dedup_key.startswith("commitment:c-9:overdue:") and bound.extra["bound_commitment"] == "c-9"
    # The dig reports a finding; the promise is kept when the answer is sent, so the answer carries its check.
    assert bound.success_check == {"kind": "result_field", "field": "finding"}
    report = finding(ident="f-3")
    report.requested_by, report.bound_commitment = "o-1", "c-9"
    answer = outreach.answer_candidate(report, inputs())
    assert answer.success_check == {"kind": "commitment_resolved", "commitment_id": "c-9"}
    offer = outreach.followup_candidate(Followup("o-2", "lease renewal", "lease-renewal", "Want a hand?", offer=True,
                                                 words="Yes please, draft it."), inputs())
    assert "took up an offer of help with lease renewal" in offer.text and "Yes please, draft it." in offer.text


def test_owner_verified_outreach_lessons_halve_or_lift_the_topics_they_name():
    from protagine.mind.lessons import Lesson

    def lesson(signature, kind, title="Some owner preference", when="anything at all"):
        return Lesson(id="L-1", signature=signature, kind=kind, title=title, when_to_use=when, content="x",
                      verified="owner")
    for lessons, expected in (([lesson("outreach_finding:tidal-energy", "pitfall")], 0.5),
                              ([lesson("topic:tidal-energy", "strategy")], 1.2),
                              ([lesson("topic:kelp-farming", "pitfall")], 1.0),
                              ([lesson("topic:x", "pitfall", "Tidal energy output news", "tidal energy output")], 0.5)):
        state = inputs(interests=[declared(origin="mentioned")], lessons=lessons)
        assert outreach.relevance(finding(), state)[0] == pytest.approx(0.4 * expected), lessons
    lifted = inputs(interests=[declared(origin="mentioned")], lessons=[lesson("topic:tidal-energy", "strategy")])
    assert outreach.relevance(finding(), lifted)[0] == pytest.approx(0.48)
