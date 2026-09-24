"""The agent's own affect (architecture 4.3, build plan M6 part A): the decaying state, its rule
table, the per-consumer view, calm rendering, self-report and the two binary switches.

The appraisal store is a fake with the agreed ``affect_events`` read API (part B
provides the real one); intention rows are real ``InitiativeStore`` rows; ``mind_state`` is the real
table on a temporary ``mind.db``.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from protagine.initiatives.store import InitiativeStore
from protagine.mind import Affect, AffectView, affect_rules
from protagine.mind.affect import (
    CAP, CONSUMERS, OVERLOAD_AT, SECTION_CHARS, SWITCH_AT, AffectEvent, AffectInputs, Frustration, Obligation,
    discretionary, plan_hash, postponable, topic_matches,
)
from protagine.mind.authority import Budgets
from protagine.mind.concerns import MindState, open_mind_db

OWNER = "p-01"
NOW = datetime(2026, 9, 24, 9, 30, tzinfo=timezone.utc)
TOPIC = "quarterly figures"
NOTE = ("Prior attempts at quarterly figures failed 2 times using the archive export; choose a different "
        "approach or ask one question.")
INTENSE = ("very", "extremely", "desperate", "furious", "panic", "terrified", "urgent")


def digest(*parts) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12]


class Appraisals:
    """The part B read API over a list of rows; ``process_one`` must never be called by affect."""

    def __init__(self):
        self.rows = []

    def affect_events(self, *, since, limit=1000):
        rows = [row for row in self.rows if row["occurred_at"] >= since]
        return sorted(rows, key=lambda row: (row["occurred_at"], row["ref"]))[:limit]

    def process_one(self, *args, **kwargs):
        raise AssertionError("affect reads appraisal records; it never processes one")


class Commitments:
    def __init__(self):
        self.rows = []

    def list(self, status=None, limit=50, **_):
        return {"commitments": [dict(r) for r in self.rows if not status or r["status"] in status][:limit]}

    def add(self, ident, *, hours=None, priority=70, obligor=None, made_hours=1.0, status="pending",
            description=None):
        self.rows.append({"id": ident, "person_id": OWNER, "description": description or f"deliver {ident}",
                          "due_at": (NOW + timedelta(hours=hours)).isoformat() if hours is not None else None,
                          "made_at": (NOW - timedelta(hours=made_hours)).isoformat(), "status": status,
                          "priority": priority, "metadata": {"obligor": obligor} if obligor else None})


class World:
    def __init__(self, tmp_path, **flags):
        self.now = NOW
        self.path = tmp_path
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.conn = open_mind_db(tmp_path / "mind.db")
        self.state = MindState(self.conn, clock=lambda: self.now)
        self.store = InitiativeStore(state_dir=tmp_path)
        self.appraisals, self.commitments, self.misses = Appraisals(), Commitments(), []
        self.flags = flags
        self.affect = self.build()

    def build(self, **flags) -> Affect:
        expectations = SimpleNamespace(store=SimpleNamespace(
            resolved_since=lambda since: [m for m in self.misses if m.resolved_at >= since]))
        return Affect(self.state, store=self.store, commitments=self.commitments, appraisals=self.appraisals,
                      expectations=expectations, budgets=Budgets(), owner_id=OWNER, clock=lambda: self.now,
                      **{**self.flags, **flags})

    def shift(self, **delta):
        self.now += timedelta(**delta)

    def update(self):
        return self.affect.update(self.now)

    def level(self, key):
        return float((self.state.get(key) or {}).get("level") or 0.0)

    def rows(self):
        return {row["key"]: row for row in self.state.items("affect.")}

    # -- events -------------------------------------------------------------------------

    def outcome(self, event, topic=TOPIC, *, hours=0.0, approach="", turn=None, ref=None):
        at = self.now - timedelta(hours=hours)
        turn = turn or f"turn-{len(self.appraisals.rows)}"
        ref = ref or f"outcome:{digest(turn, event, topic, len(self.appraisals.rows))}"
        self.appraisals.rows.append({"ref": ref, "kind": event, "topic": topic, "approach": approach,
                                     "dimension": "", "intensity": "", "turn_id": turn,
                                     "occurred_at": at.timestamp(), "created_at": at.timestamp()})
        return ref

    def record(self, dimension, intensity="moderate", topic=TOPIC, *, hours=0.0, turn=None, kind="appraisal"):
        at = self.now - timedelta(hours=hours)
        turn = turn or f"turn-{len(self.appraisals.rows)}"
        ref = f"appraisal:{digest(turn, dimension, topic, len(self.appraisals.rows))}"
        self.appraisals.rows.append({"ref": ref, "kind": kind, "topic": topic, "approach": "",
                                     "dimension": dimension, "intensity": intensity, "turn_id": turn,
                                     "occurred_at": at.timestamp(), "created_at": at.timestamp()})
        return ref

    def intention(self, *, kind="task", topic=TOPIC, recipient=None, body="Fetch the quarterly figures.\n\nReport.",
                  hours=0.0, **transition):
        at = self.now - timedelta(hours=hours)
        row, _ = self.store.create_intention(
            kind=kind, type="research" if kind == "task" else "commitment_reminder", title=f"about {topic}",
            drive="duty", cls="owner", decision="act", decision_reason="test", status="approved",
            dedup_key=f"k-{len(self.store.intentions(limit=1000))}", recipient=recipient,
            context={"topic": topic, "body": body} if kind == "task" else {"concern": topic, "text": "hi"},
            created_at=at)
        if transition:
            self.store.transition(row.id, transition.pop("status", "done"), action="test", at=at, **transition)
        return row.id

    def miss(self, domain, ident="e-1", hours=0.0):
        self.misses.append(SimpleNamespace(prediction_id=ident, domain=domain, subject="commitment:c-1",
                                           expectation="the report arrives", outcome="miss",
                                           resolved_at=(self.now - timedelta(hours=hours)).timestamp()))


@pytest.fixture
def world(tmp_path):
    instance = World(tmp_path)
    yield instance
    instance.store.close()


def candidate(**fields):
    base = {"drive": "duty", "kind": "task", "priority": 0.7, "dedup_base": None, "recipient": OWNER, "topic": ""}
    return SimpleNamespace(**{**base, **fields})


FRUSTRATION_KEY = "affect.frustration:quarterly-figures"


# -- 1. the rule table ----------------------------------------------------------------------------

RULES = [
    # name, how the event is made, {key: (level, cause template)}
    ("owner failed", lambda w: w.outcome("failed", approach="the archive export"),
     {FRUSTRATION_KEY: (0.3, "failed {ref} via the archive export")}),
    ("owner corrected", lambda w: w.outcome("corrected"), {FRUSTRATION_KEY: (0.2, "corrected {ref}")}),
    ("owner dismissed", lambda w: w.outcome("dismissed", "the stretch nudge"),
     {"affect.dismissed": (0.25, "dismissed {ref}")}),
    ("appraisal frustration low", lambda w: w.record("frustration", "low"),
     {FRUSTRATION_KEY: (0.1, "appraisal {ref}")}),
    ("appraisal frustration moderate", lambda w: w.record("frustration", "moderate"),
     {FRUSTRATION_KEY: (0.2, "appraisal {ref}")}),
    ("appraisal annoyance", lambda w: w.record("annoyance", "low"), {FRUSTRATION_KEY: (0.2, "appraisal {ref}")}),
    ("appraisal interest low", lambda w: w.record("interest", "low"), {"affect.curiosity": (0.1, "appraisal {ref}")}),
    ("appraisal interest moderate", lambda w: w.record("interest", "moderate"),
     {"affect.curiosity": (0.2, "appraisal {ref}")}),
    ("appraisal satisfaction low", lambda w: w.record("satisfaction", "low"),
     {"affect.satisfaction": (0.1, "appraisal {ref}")}),
    ("appraisal satisfaction moderate", lambda w: w.record("satisfaction", "moderate"),
     {"affect.satisfaction": (0.2, "appraisal {ref}")}),
    ("intention failed", lambda w: "intention:" + w.intention(
        status="failed", outcome="failed", failed_at=w.now, failed_reason="the scrape returned stale data") + ":failed",
     {FRUSTRATION_KEY: (0.3, "failed {ref}")}),
    ("intention blocked", lambda w: "intention:" + w.intention(
        status="dispatched", outcome="blocked", assigned_at=w.now, result="waiting on access") + ":failed",
     {FRUSTRATION_KEY: (0.3, "failed {ref}")}),
    ("owner rated wrong", lambda w: "intention:" + w.intention(
        status="done", outcome="done", verdict="wrong", completed_at=w.now) + ":wrong",
     {FRUSTRATION_KEY: (0.2, "corrected {ref}")}),
    ("owner rated not useful", lambda w: "intention:" + w.intention(
        status="done", outcome="done", verdict="not_useful", completed_at=w.now) + ":not_useful",
     {FRUSTRATION_KEY: (0.2, "corrected {ref}")}),
    ("owner nudge dismissed", lambda w: "intention:" + w.intention(kind="message", recipient=OWNER, status="cancelled",
                                                                  outcome="cancelled", verdict="dismissed",
                                                                  cancelled_at=w.now) + ":dismissed",
     {"affect.dismissed": (0.25, "dismissed {ref}")}),
    ("owner rated a nudge ignored", lambda w: "intention:" + w.intention(
        kind="message", recipient=OWNER, status="sent", outcome="done", verdict="ignored",
        completed_at=w.now) + ":ignored",
     {"affect.dismissed": (0.25, "dismissed {ref}")}),
    ("duty miss", lambda w: (w.miss("commitment"), "expectation:e-1")[1], {"affect.worry": (0.2, "miss {ref}")}),
    ("knowledge miss", lambda w: (w.miss("weather"), "expectation:e-1")[1], {"affect.curiosity": (0.2, "miss {ref}")}),
    ("novel owner topic", lambda w: (w.affect.note_novel_topic("tidal energy storage", w.now),
                                     f"novel:{hashlib.sha256(b'tidal energy storage').hexdigest()[:8]}"
                                     "@20260924T0930Z")[1],
     {"affect.curiosity": (0.1, "novel {ref}")}),
]


@pytest.mark.parametrize("name,make,expected", RULES, ids=[rule[0] for rule in RULES])
def test_each_event_kind_moves_one_level_by_its_table_increment_and_cites_itself(world, name, make, expected):
    ref = make(world)
    result = world.update()
    assert result["applied"] == 1 and result["source"] == "state"
    rows = world.rows()
    for key, (level, cause) in expected.items():
        assert rows[key]["level"] == pytest.approx(level), name
        assert rows[key]["causes"] == [cause.format(ref=ref)]
    levels = {key for key, row in rows.items() if row["level"]}
    assert levels == set(expected), f"{name} touched only its own key"
    if FRUSTRATION_KEY in expected:
        assert rows[FRUSTRATION_KEY]["text"] == TOPIC and rows[FRUSTRATION_KEY]["half_life_s"] == 86400
    assert list(json.loads(rows["affect.applied"]["text"])) == [ref]


@pytest.mark.parametrize("success", ["reported", "repair", "verified", "useful"])
def test_success_halves_the_topics_frustration_and_only_verified_work_is_satisfying(world, success):
    world.outcome("failed", hours=1, approach="the archive export")
    world.outcome("failed", hours=0.5)
    world.update()
    before = world.level(FRUSTRATION_KEY)
    if success == "reported":
        ref, word = world.outcome("succeeded"), "succeeded"
    elif success == "repair":
        ref, word = world.record("satisfaction", "low", kind="resolved"), "resolved"
    elif success == "verified":
        ident = world.intention(status="done", outcome="done", verified="check", completed_at=world.now,
                                result_metadata={"check": {"passed": True, "at": world.now.isoformat()}})
        ref, word = f"intention:{ident}:verified", "verified"
    else:
        ident = world.intention(status="done", outcome="done", verified="owner", verdict="useful",
                                completed_at=world.now)
        ref, word = f"intention:{ident}:useful", "useful"
    world.update()
    assert world.level(FRUSTRATION_KEY) == pytest.approx(before * 0.5)
    assert f"{word} {ref}" in world.rows()[FRUSTRATION_KEY]["causes"]
    satisfaction = world.level("affect.satisfaction")
    assert satisfaction == pytest.approx(0.3 if success in {"verified", "useful"} else 0.0)


def test_one_success_calms_once_however_many_reports_carry_it(world):
    """A success halves what the failures since the last success built up. One statement that is both a
    reported success and a repair receipt halves once; so do a verified task and the owner's "it worked"
    about it; a new failure re-arms the halving."""
    world.outcome("failed", hours=1, approach="the archive export")
    world.outcome("failed", hours=0.5, approach="the archive export")
    world.update()
    before = world.level(FRUSTRATION_KEY)
    world.outcome("succeeded", turn="turn-fix")
    world.record("frustration", "moderate", turn="turn-fix", kind="resolved")
    world.update()
    assert world.level(FRUSTRATION_KEY) == pytest.approx(before / 2)
    world.intention(status="done", outcome="done", verified="check", completed_at=world.now,
                    result_metadata={"check": {"passed": True}})
    world.outcome("succeeded", turn="turn-owner")
    world.update()
    assert world.level(FRUSTRATION_KEY) == pytest.approx(before / 2), "the same success, reported twice"
    assert world.level("affect.satisfaction") == pytest.approx(0.3), "the verified task still satisfies"
    world.outcome("failed", approach="the archive export")
    world.update()
    raised = world.level(FRUSTRATION_KEY)
    world.outcome("succeeded", turn="turn-later")
    world.update()
    assert world.level(FRUSTRATION_KEY) == pytest.approx(raised / 2)


def test_an_owner_verified_row_is_one_success_not_two(world):
    world.outcome("failed", hours=1)
    world.outcome("failed", hours=0.5)
    ident = world.intention(status="done", outcome="done", verified="check", completed_at=world.now,
                            result_metadata={"check": {"passed": True}})
    world.update()
    halved = world.level(FRUSTRATION_KEY)
    world.store.update(ident, verdict="useful", verified="owner")   # the owner rates it afterwards
    assert world.update()["applied"] == 0
    assert world.level("affect.satisfaction") == pytest.approx(0.3)
    assert world.level(FRUSTRATION_KEY) == halved


def test_a_task_blocked_then_failed_is_one_failed_attempt(world):
    """``blocked`` is not terminal: the same task can later fail. One task is one failure, whichever
    of its reports arrives first, in the state and in the rules."""
    ident = world.intention(status="dispatched", outcome="blocked", assigned_at=world.now, result="waiting on access")
    world.update()
    assert world.level(FRUSTRATION_KEY) == pytest.approx(0.3)
    world.shift(minutes=5)
    world.store.transition(ident, "failed", action="outcome_failed", at=world.now, outcome="failed",
                           failed_at=world.now, failed_reason="still no access")
    assert world.update()["applied"] == 0
    assert world.level(FRUSTRATION_KEY) == pytest.approx(0.3, abs=0.01)
    [failed] = [event for event in world.affect.gather(world.now).events if event.kind == "failed"]
    assert failed.ref == f"intention:{ident}:failed" and failed.reason == "still no access"
    assert world.affect.view().frustrations == ()
    world.intention(status="failed", outcome="failed", failed_at=world.now, failed_reason="stale again")
    world.update()
    [frustration] = world.affect.view().frustrations
    assert frustration.failures == 2, "a second task is a second failure"


def test_contact_facing_dismissals_and_notices_and_notes_are_not_the_agents_feelings(world):
    world.intention(kind="message", recipient="p-07", status="cancelled", outcome="cancelled", verdict="dismissed",
                    cancelled_at=world.now)
    row, _ = world.store.create_intention(kind="note", type="deliberation", title="thought", drive="duty",
                                          cls="internal", decision="act", decision_reason="x", status="done",
                                          dedup_key=None, created_at=world.now)
    world.store.transition(row.id, "failed", action="x", outcome="failed", failed_at=world.now)
    notice, _ = world.store.create_intention(kind="message", type="ask_notice", title="asks", drive="duty",
                                             cls="owner", decision="act", decision_reason="x", status="done",
                                             dedup_key=None, recipient=OWNER, created_at=world.now)
    world.store.transition(notice.id, "cancelled", action="x", outcome="cancelled", verdict="dismissed")
    world.miss("intention")
    world.misses[-1].subject = "intention:abc"
    world.intention(status="expired", outcome="expired", verdict="ignored", cancelled_at=world.now)  # never dispatched
    assert world.update()["applied"] == 0 and not any(row["level"] for row in world.rows().values())
    lapsed, _ = world.store.create_intention(kind="task", type="research", title="an ask", drive="duty", cls="owner",
                                             decision="ask", decision_reason="x", status="asked", dedup_key="lapsed",
                                             created_at=world.now)
    world.store.transition(lapsed.id, "expired", action="x", outcome="expired", verdict="ignored",
                           cancelled_at=world.now)
    assert world.update()["applied"] == 1 and world.level("affect.dismissed") == pytest.approx(0.25), \
        "an ask the owner let lapse is a dismissal"


def test_small_talk_is_no_novel_topic_and_novelty_alone_stays_calm(world):
    """A novel topic is an owner turn about something (three or more content words) memory knew nothing
    about: acknowledgements are not topics. Novelty alone never lifts curiosity above 0.3, so a chatty
    owner on a fresh memory leaves the agent at most somewhat curious; other inputs still add."""
    for text in ("ok", "thanks", "yes K7F", "sounds good", "lol", "morning", "sure, go ahead", "cool", "no worries"):
        world.affect.note_novel_topic(text, at=world.now)
        world.shift(minutes=3)
        world.update()
    assert world.level("affect.curiosity") == 0.0 and world.affect.view().line == ""
    for n in range(9):
        world.affect.note_novel_topic(f"When does the ferry number {n} leave the harbour on Fridays?", at=world.now)
        world.shift(minutes=3)
        world.update()
    assert world.level("affect.curiosity") == pytest.approx(0.3, abs=1e-6)
    view = world.affect.view()
    assert view.line == "Mood: somewhat curious." and view.score_factor(candidate(drive="curiosity")) <= 1.3 + 1e-9
    world.record("interest", "moderate", "ferry timetables")
    world.update()
    assert world.level("affect.curiosity") == pytest.approx(0.5, abs=1e-6), "an interest appraisal adds above it"


# -- 2-4. cap, same-turn dedupe, age adjustment -------------------------------------------------------

def test_levels_never_exceed_the_cap_and_causes_stay_at_five(world):
    rng = random.Random(7)
    topics = [TOPIC, "budget draft", "the vendor email", "tide tables"]
    for step in range(10):
        for _ in range(30):
            choice = rng.random()
            topic, hours = rng.choice(topics), rng.uniform(0, 70)
            if choice < 0.4:
                world.outcome(rng.choice(["failed", "corrected", "dismissed"]), topic, hours=hours,
                              approach=rng.choice(["", "the archive export", "the scrape"]))
            elif choice < 0.8:
                world.record(rng.choice(["frustration", "annoyance", "interest", "satisfaction"]),
                             rng.choice(["low", "moderate"]), topic, hours=hours)
            else:
                world.miss(rng.choice(["commitment", "weather"]), ident=f"e-{step}-{rng.random()}", hours=hours / 4)
        world.update()
        world.shift(minutes=13)
    rows = [row for row in world.rows().values() if row["level"] is not None]
    assert rows and all(row["level"] <= CAP + 1e-9 for row in rows)
    assert all(len(row["causes"]) <= 5 for row in world.rows().values())
    assert max(row["level"] for row in rows) == pytest.approx(CAP), "the cap is reached, not avoided"


def test_an_outcome_and_an_appraisal_from_one_turn_count_once_but_occurrences_all_count(world, tmp_path):
    world.outcome("failed", turn="turn-a")
    world.record("frustration", "moderate", turn="turn-a")
    world.update()
    assert world.level(FRUSTRATION_KEY) == pytest.approx(0.3), "the larger of the two, once"

    late = World(tmp_path / "late")
    late.record("frustration", "moderate", turn="turn-a")
    late.update()
    late.outcome("failed", turn="turn-a")          # the outcome arrives after the record was applied
    late.update()
    assert late.level(FRUSTRATION_KEY) == pytest.approx(0.3), "the same total whatever the ingestion order"

    twice = World(tmp_path / "twice")
    twice.outcome("failed", turn="turn-b")
    twice.outcome("failed", turn="turn-b")        # "it failed twice today": two occurrences in one turn
    twice.record("annoyance", "low", turn="turn-b")
    twice.update()
    assert twice.level(FRUSTRATION_KEY) == pytest.approx(0.6)
    for instance in (late, twice):
        instance.store.close()


def test_a_late_event_is_applied_as_if_at_its_time_and_then_decayed(world, tmp_path):
    world.outcome("failed", hours=30)
    world.update()
    late = world.level(FRUSTRATION_KEY)
    assert late == pytest.approx(0.3 * 0.5 ** (30 / 24))

    timely = World(tmp_path / "timely")
    timely.now = NOW - timedelta(hours=30)
    timely.outcome("failed")
    timely.update()
    timely.now = NOW
    timely.update()
    assert timely.level(FRUSTRATION_KEY) == pytest.approx(late, abs=1e-9)
    timely.store.close()


# -- 5-6. the family's shapes on the state, topic matching ----------------------------------------------

def test_two_failures_today_switch_with_the_architecture_wording(world):
    world.outcome("failed", hours=3, approach="the archive export")
    world.outcome("failed", "quarterly figure", hours=1, approach="the archive export")
    result = world.update()
    assert result["switch"] == [TOPIC]
    [frustration] = world.affect.view().frustrations
    assert frustration.failures == 2 and frustration.level >= SWITCH_AT and frustration.note() == NOTE
    assert world.affect.failing("the stale quarterly figure from the archive export") == frustration
    assert world.affect.note_for("send the quarterly figures") == NOTE
    assert world.affect.note_for("the budget draft") == ""
    assert world.affect.section_lines()[0] == NOTE


def test_the_same_failures_three_days_old_or_followed_by_a_success_do_not_switch(world, tmp_path):
    world.outcome("failed", hours=74, approach="the archive export")
    world.outcome("failed", hours=72, approach="the archive export")
    world.update()
    assert world.affect.view().frustrations == () and world.affect.note_for(TOPIC) == ""

    recovered = World(tmp_path / "recovered")
    recovered.outcome("failed", hours=4)
    recovered.outcome("failed", hours=3)
    recovered.outcome("succeeded", hours=1)
    recovered.update()
    assert recovered.affect.view().frustrations == ()
    recovered.store.close()


def test_the_switch_needs_two_failures_since_the_topics_last_success(world, tmp_path):
    """A success halves frustration, and the next single failure lifts it back over 0.5; that is one
    failure since the success, so it does not switch (the rule does not either) and the approach that
    just worked is not abandoned after one miss. One failure and two corrections do not switch either."""
    world.outcome("failed", hours=3, approach="the archive export")
    world.outcome("failed", hours=2.5, approach="the archive export")
    world.outcome("succeeded", hours=2, approach="the ledger database")
    world.update()
    world.outcome("failed", approach="the ledger database")
    world.update()
    assert world.level(FRUSTRATION_KEY) >= SWITCH_AT
    assert world.affect.view().frustrations == () and world.affect.note_for(TOPIC) == ""
    assert affect_rules.view(world.affect.gather(world.now)).frustrations == ()
    assert world.affect.state()["levels"]["frustration"][0]["failures"] == 1
    world.shift(minutes=30)
    world.outcome("failed", approach="the ledger database")
    world.update()
    [frustration] = world.affect.view().frustrations
    assert frustration.failures == 2 and frustration.approaches == ("the ledger database",)

    corrected = World(tmp_path / "corrected")
    corrected.outcome("failed", approach="the archive export")
    corrected.outcome("corrected")
    corrected.outcome("corrected")
    corrected.update()
    assert corrected.level(FRUSTRATION_KEY) == pytest.approx(CAP) and corrected.affect.view().frustrations == ()
    corrected.store.close()


def test_the_failure_record_outlasts_the_feeling_until_a_success(world):
    """What failed is a record, not a feeling: a topic with two failed attempts since its last success
    keeps its failed plans (``tried``) for the whole window, after its frustration has decayed below
    the switch, so an identical plan is still refused; a success on the topic clears it."""
    body = "Fetch the quarterly figures.\n\nReport."
    for hours in (8, 7.5):
        world.intention(status="failed", outcome="failed", failed_at=world.now - timedelta(hours=hours),
                        failed_reason="stale", hours=hours, body=body)
    world.update()
    view = world.affect.view()
    assert view.frustrations == () and world.level(FRUSTRATION_KEY) < SWITCH_AT
    [record] = view.tried
    assert record.topic == TOPIC and record.failures == 2 and record.body_hashes == frozenset({plan_hash(body)})
    rules = affect_rules.view(world.affect.gather(world.now))
    assert [item.topic for item in rules.tried] == [TOPIC] and rules.tried == rules.frustrations
    world.shift(days=3)
    world.update()
    assert [item.topic for item in world.affect.view().tried] == [TOPIC]
    world.outcome("succeeded")
    world.update()
    assert world.affect.view().tried == ()


def test_failures_on_one_topic_and_a_success_on_another_do_not_spread(world):
    world.outcome("failed", hours=3, approach="the archive export")
    world.outcome("failed", hours=2, approach="the archive export")
    world.outcome("failed", "budget draft", hours=2)
    world.outcome("succeeded", "budget draft", hours=1)
    world.update()
    assert [f.topic for f in world.affect.view().frustrations] == [TOPIC]
    assert world.affect.note_for("budget draft") == ""


def test_topic_matching_is_word_overlap_of_the_smaller_topic():
    assert topic_matches("quarterly figures", "quarterly figures figure")
    assert topic_matches("quarterly figures", "stale quarterly figure from the archive export")
    assert topic_matches("Quarterly Figures", "the quarterly figures")
    assert not topic_matches("budget draft", "budget figures")
    assert not topic_matches("", "budget") and not topic_matches("budget", "")
    assert topic_matches("report", "the weekly report") and topic_matches("reports", "report")
    assert not topic_matches("report", "send the owner the weekly report"), "one word does not spread"


def test_a_one_word_topic_does_not_spread_to_other_work(world):
    """A one-word topic names the work only when the other side is as short: "the export" failing does
    not switch strategy on an unrelated owed task that mentions exporting, nor does that task's success
    calm it."""
    assert not topic_matches("the export", "Export the family photo album for the printer")
    assert not topic_matches("email", "Reply to Dana's email about Saturday dinner")
    world.outcome("failed", "the export", hours=1, approach="the archive tool")
    world.outcome("failed", "the export", hours=0.5, approach="the archive tool")
    world.update()
    assert world.affect.note_for("the export task").startswith("Prior attempts at the export failed 2 times")
    assert world.affect.note_for("Export the family photo album for the printer") == ""
    world.outcome("succeeded", "export the photo album")
    world.update()
    assert [item.topic for item in world.affect.view().frustrations] == ["the export"]


def test_topics_in_any_script_keep_their_own_rows_and_match_by_their_words(world):
    """A topic with no Latin letters or digits still has its own frustration row (a digest key, not one
    shared "topic" slug), and topics written with spaces match by their words in any script."""
    world.outcome("failed", "四半期の数字", approach="アーカイブ")
    world.outcome("failed", "квартальный отчёт", approach="экспорт")
    world.update()
    rows = {row["key"]: row["text"] for row in world.state.items("affect.frustration:")}
    assert sorted(rows.values()) == ["квартальный отчёт", "四半期の数字"] and "affect.frustration:topic" not in rows
    assert world.affect.view().frustrations == (), "one failure on each of two topics is no switch"
    world.outcome("failed", "квартальный отчёт за сентябрь", approach="экспорт")
    world.update()
    [frustration] = world.affect.view().frustrations
    assert frustration.topic == "квартальный отчёт" and frustration.failures == 2
    assert world.affect.note_for("Отправить квартальный отчёт").startswith("Prior attempts at квартальный отчёт")
    assert world.affect.note_for("四半期の数字") == "" and len(world.state.items("affect.frustration:")) == 2
    assert topic_matches("Straße Plan", "der Straße Plan") and not topic_matches("отчёт", "план")


def test_a_novel_topic_counts_its_words_in_any_script(world):
    for text in ("спасибо", "ありがとう", "了解です"):
        world.affect.note_novel_topic(text, at=world.now)
    world.update()
    assert world.level("affect.curiosity") == 0.0
    world.affect.note_novel_topic("Когда отходит паром до Капри по пятницам?", at=world.now)
    world.affect.note_novel_topic("四半期の数字がアーカイブと台帳で違うのはなぜですか", at=world.now)
    world.update()
    assert world.level("affect.curiosity") == pytest.approx(0.2)


def test_a_frustration_keeps_its_first_topic_and_the_note_names_approaches_and_pitfalls(world):
    world.outcome("failed", hours=2, approach="the archive export")
    world.intention(topic="stale quarterly figure", status="failed", outcome="failed", failed_at=world.now,
                    failed_reason="the scrape returned stale data", hours=1)
    world.update()
    assert [key for key in world.rows() if key.startswith("affect.frustration:")] == [FRUSTRATION_KEY]
    [frustration] = world.affect.view().frustrations
    assert frustration.failures == 2 and frustration.approaches == ("the archive export",)
    assert frustration.pitfalls == ("the scrape returned stale data",)
    assert frustration.body_hashes == frozenset({plan_hash("Fetch the quarterly figures.\n\nReport.")})
    single = Frustration(topic="t", key="k", level=0.5, failures=1)
    assert single.note() == "Prior attempts at t failed once; choose a different approach or ask one question."


def test_plan_hash_ignores_the_note_and_whitespace():
    body = "Fetch the figures.\n\nReport what you did."
    assert plan_hash(body) == plan_hash(body + "\n\n" + NOTE) == plan_hash("Fetch   the figures.\nReport what you did.")
    assert plan_hash(body) != plan_hash("Use the archive export instead.") and len(plan_hash(body)) == 16


# -- 7-8. deadline worry, load ----------------------------------------------------------------------

def test_deadline_worry_rises_per_tick_for_owed_unstarted_work_and_this_rule_stops_at_0_3(world):
    world.commitments.add("c-17", hours=2)
    levels = []
    for _ in range(5):
        world.update()
        levels.append(round(world.level("affect.worry"), 3))
    assert levels == [0.1, 0.2, 0.3, 0.3, 0.3]
    assert "due_soon commitment:c-17" in world.rows()["affect.worry"]["causes"]
    note = world.affect.view().notes()
    assert "Due soon and not started: deliver c-17 (due 11:30 UTC)." in note


def test_started_optional_undated_and_distant_obligations_do_not_worry(world):
    world.commitments.add("started", hours=2)
    world.store.create_intention(kind="task", type="commitment_overdue", title="x", drive="duty", cls="owner",
                                 decision="act", decision_reason="x", status="dispatched", dedup_key="started-k",
                                 source_type="commitment", source_id="started", created_at=world.now)
    world.commitments.add("optional", hours=2, priority=30)
    world.commitments.add("undated")
    world.commitments.add("distant", hours=72)
    world.commitments.add("theirs", hours=2, obligor="p-07")
    world.update()
    assert world.level("affect.worry") == 0.0 and world.affect.view().due_soon == ()
    world.miss("commitment", "e-1")
    world.miss("commitment", "e-2")
    world.commitments.add("due", hours=3)
    world.update()
    assert world.level("affect.worry") == pytest.approx(0.4), "the deadline rule never lifts worry above 0.3"


def test_load_counts_near_owed_obligations_running_work_failures_and_asks(world):
    for ident in ("a", "b", "c"):
        world.commitments.add(ident, hours=30)
    world.commitments.add("theirs", hours=5, obligor="p-07")
    world.commitments.add("optional", hours=5, priority=30)
    world.commitments.add("next week", hours=24 * 6)
    world.commitments.add("long undated", made_hours=30)
    result = world.update()
    view = world.affect.view()
    assert result["load"] == pytest.approx(0.6) and result["overloaded"] is True and view.overloaded
    assert [o.id for o in view.obligations] == ["a", "b", "c"]
    assert view.notes()[-1] == ("Stretched: 3 open obligations (deliver a; deliver b; deliver c); optional work "
                                "waits and replies stay brief.")
    world.commitments.rows = []
    for ident in ("r1", "r2"):
        world.store.create_intention(kind="task", type="research", title=ident, drive="curiosity", cls="internal",
                                     decision="act", decision_reason="x", status="dispatched", dedup_key=ident,
                                     created_at=world.now)
    result = world.update()
    assert result["load"] == pytest.approx(0.3) and result["overloaded"] is False
    world.outcome("failed", hours=0.5)
    world.store.create_intention(kind="task", type="research", title="ask", drive="curiosity", cls="internal",
                                 decision="ask", decision_reason="x", status="asked", dedup_key="asked",
                                 created_at=world.now)
    assert world.update()["load"] == pytest.approx(0.5)


# -- 9. the consumers' factors -----------------------------------------------------------------------

def test_owed_duty_is_never_suppressed_by_any_view():
    rng = random.Random(11)
    owed = [candidate(kind=kind, priority=priority, recipient=recipient)
            for kind in ("task", "message") for priority in (0.5, 0.7, 0.9) for recipient in (OWNER, "p-07", None)]
    for _ in range(300):
        view = AffectView(route={name: "state" for name in CONSUMERS}, owner_id=OWNER,
                          overloaded=rng.random() < 0.5, load=rng.random(), worry=rng.uniform(0, CAP),
                          curiosity=rng.uniform(0, CAP), satiated=rng.random() < 0.5, boost=rng.uniform(0, CAP))
        for item in owed:
            assert not discretionary(item) and not postponable(item)
            assert view.score_factor(item) >= 1.0 and view.threshold_factor(item) == 1.0


def test_optional_nudges_wait_under_load_and_after_dismissals_but_contacts_are_not_satiated(world):
    reminder = candidate(kind="message", priority=0.3)
    to_contact = candidate(kind="message", priority=0.3, recipient="p-07")
    research = candidate(drive="curiosity", kind="task", dedup_base="research:x")
    social = candidate(drive="social", kind="message", recipient="p-07")
    loaded = AffectView(route={}, owner_id=OWNER, overloaded=True, load=0.6)
    assert loaded.threshold_factor(reminder) == math.inf
    assert loaded.threshold_factor(research) == math.inf and loaded.threshold_factor(social) == math.inf
    assert loaded.threshold_factor(candidate(drive="mastery", dedup_base="mastery:x")) == 1.0
    world.outcome("dismissed", "the stretch nudge", hours=2)
    world.outcome("dismissed", "the stretch nudge", hours=1)
    world.update()
    view = world.affect.view()
    assert view.satiated and view.boost == pytest.approx(0.5, abs=0.05) and view.dismissals == 2
    assert view.threshold_factor(reminder) == pytest.approx(1 + view.boost)
    assert view.threshold_factor(to_contact) == 1.0, "satiation holds owner-facing nudges only"
    assert view.threshold_factor(candidate(kind="task", priority=0.3)) == 1.0
    assert "Holding back optional nudges: 2 were waved off recently." in view.notes()
    curious = AffectView(route={}, owner_id=OWNER, curiosity=0.4, worry=0.2)
    assert curious.score_factor(research) == pytest.approx(1.4)
    assert curious.score_factor(candidate()) == pytest.approx(1.1)
    assert curious.score_factor(reminder) == 1.0, "worry lifts owed duty only"


def test_the_step_of_an_adopted_goal_is_owed_whatever_its_drive():
    step = candidate(drive="curiosity", type="goal_step", parent_goal_id="g-1", priority=0.5)
    loaded = AffectView(route={}, owner_id=OWNER, overloaded=True, load=0.8, satiated=True, boost=0.5)
    assert not discretionary(step) and not postponable(step) and loaded.threshold_factor(step) == 1.0
    assert postponable(candidate(drive="curiosity", type="research", dedup_base="research:x"))


# -- 10. calm rendering ------------------------------------------------------------------------------

def test_rendering_is_calm_banded_and_bounded(world):
    world.outcome("failed", hours=3, approach="the archive export")
    world.outcome("failed", hours=1, approach="the archive export")
    world.record("frustration", "low", "the urgent vendor email", hours=1)
    world.miss("commitment")
    world.update()
    view = world.affect.view()
    assert view.line == ("Mood: quite frustrated about quarterly figures; somewhat uneasy; "
                         "a little frustrated about the vendor email.")
    rng = random.Random(3)
    for _ in range(200):
        world.state.set("affect.worry", level=rng.uniform(0, CAP), half_life_s=21600)
        world.state.set("affect.curiosity", level=rng.uniform(0, CAP), half_life_s=43200)
        world.state.set("affect.satisfaction", level=rng.uniform(0, CAP), half_life_s=43200)
        world.update()
        line = world.affect.view().line
        assert len(line) <= 160 and line.startswith("Mood: ")
        words = set(line.replace(";", " ").replace(".", " ").replace(":", " ").split())
        assert not words & set(INTENSE), line
        parts = line[len("Mood: "):-1].split("; ")
        assert 1 <= len(parts) <= 3 and all(part.startswith(("a little ", "somewhat ", "quite ")) for part in parts)


def test_the_section_drops_whole_lines_from_the_end_and_keeps_the_switch_notes_last(world):
    world.outcome("failed", hours=3, approach="the archive export")
    world.outcome("failed", hours=1, approach="the archive export")
    for ident in ("a", "b", "c"):
        world.commitments.add(ident, hours=5, description=f"a long obligation description number {ident} " * 2)
    world.outcome("dismissed", "nudge", hours=1)
    world.outcome("dismissed", "nudge", hours=0.5)
    world.update()
    full = world.affect.section_lines(limit=10_000)
    assert full[0] == NOTE and full[-1].startswith("Mood: ") and len(full) == 5
    bounded = world.affect.section_lines()
    assert len("\n".join(bounded)) <= SECTION_CHARS and bounded == full[:len(bounded)]
    assert world.affect.section_lines(limit=len(NOTE) + 5) == [NOTE]
    assert world.affect.section_lines(limit=0) == [] and world.affect.section_lines(limit=-40) == []


# -- 11. self-report -----------------------------------------------------------------------------------

def test_state_reports_levels_with_cited_causes(world):
    first = world.outcome("failed", hours=3, approach="the archive export")
    second = world.outcome("failed", hours=1.5, approach="the archive export")
    world.commitments.add("c-17", hours=2)
    world.update()
    state = world.affect.state()
    assert set(state) >= {"enabled", "source", "route", "levels", "load", "satiated", "boost", "due_soon", "notes",
                          "line", "updated_at"}
    assert state["enabled"] is True and state["source"] == "state"
    assert state["route"] == {name: "state" for name in CONSUMERS}
    [frustration] = state["levels"]["frustration"]
    assert frustration["topic"] == TOPIC and frustration["level"] >= SWITCH_AT and frustration["failures"] == 2
    assert frustration["approaches"] == ["the archive export"]
    assert frustration["causes"] == [f"failed {first} via the archive export",
                                     f"failed {second} via the archive export"]
    assert state["levels"]["worry"] == {"level": 0.1, "causes": ["due_soon commitment:c-17"]}
    assert set(state["levels"]) == {"frustration", "worry", "curiosity", "satisfaction", "dismissed"}
    assert state["load"] == {"level": 0.2, "overloaded": False, "obligations": 1, "running": 0, "cap": 2,
                             "failures_last_hour": 0, "asks": 0}
    assert state["due_soon"] == [{"id": "c-17", "description": "deliver c-17", "due_at": "2026-09-24T11:30:00+00:00"}]
    assert state["notes"][0] == NOTE and state["line"].startswith("Mood: ") and state["switch"] == [TOPIC]
    assert state["updated_at"] == NOW.isoformat()
    assert all(len(str(level)) <= 5 for level in [frustration["level"], state["levels"]["worry"]["level"]])
    json.dumps(state)
    off = World(world.path / "rules", state_on=False, rules_on=True)
    off.update()
    assert off.affect.state()["levels"] == {} and off.affect.state()["source"] == "rules"
    off.store.close()


def test_local_time_renders_in_the_owners_timezone(world):
    affect = world.build(tz=ZoneInfo("Europe/Berlin"))
    world.commitments.add("c-17", hours=2)
    affect.update(world.now)
    assert "Due soon and not started: deliver c-17 (due 13:30 CEST)." in affect.view().notes()


# -- 12-13. erasure and idempotence ----------------------------------------------------------------------

def test_erased_evidence_takes_its_topic_row_with_it(world):
    first = world.outcome("failed", hours=2, approach="the archive export")
    world.outcome("failed", hours=1, approach="the archive export")
    world.update()
    assert FRUSTRATION_KEY in world.rows()
    world.appraisals.rows = [row for row in world.appraisals.rows if row["ref"] != first]
    world.update()
    assert FRUSTRATION_KEY in world.rows(), "one cause is still in the window"
    world.appraisals.rows = []
    world.update()
    assert FRUSTRATION_KEY not in world.rows()
    assert TOPIC not in json.dumps([dict(row) for row in world.state.items("affect.")])
    assert len(json.loads(world.rows()["affect.applied"]["text"])) == 2, "an applied reference is kept by age"
    world.shift(days=7, hours=1)
    world.update()
    assert json.loads(world.rows()["affect.applied"]["text"]) == {}, "and dropped once it leaves the window"


def test_an_unreadable_source_never_has_its_events_applied_again(world):
    """A tick whose read of one source fails (say, a locked ledger) is not an erasure: every event of
    that source stays applied, its frustration rows keep their evidence, and the next readable tick
    adds nothing."""
    world.outcome("dismissed", "the stretch nudge")
    world.outcome("dismissed", "the stretch nudge")
    world.outcome("failed", hours=1, approach="the archive export")
    world.outcome("failed", hours=0.5, approach="the archive export")
    world.intention(topic="the tide tables", status="done", outcome="done", verdict="useful", completed_at=world.now)
    world.update()
    before = {key: round(row["level"], 3) for key, row in world.rows().items() if row["level"]}
    assert before["affect.dismissed"] == 0.5 and before["affect.satisfaction"] == 0.3

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
    events, intentions = world.appraisals.affect_events, world.store.intentions
    for broken in ("appraisals", "intentions"):
        world.shift(minutes=1)
        if broken == "appraisals":
            world.appraisals.affect_events = locked
        else:
            world.store.intentions = lambda *a, **k: locked() if "since" in k else intentions(*a, **k)
        result = world.update()
        assert result["unread"] == ["appraisal events" if broken == "appraisals" else "intention events"]
        assert FRUSTRATION_KEY in world.rows(), "a failed read is not erased evidence"
        assert result["switch"] == [TOPIC] and world.affect.note_for(TOPIC), "the consumers keep the last full view"
        assert world.affect.view().dismissals == 2
        world.appraisals.affect_events, world.store.intentions = events, intentions
        world.shift(minutes=1)
        assert world.update()["applied"] == 0 and "unread" not in world.update()
    after = {key: round(row["level"], 3) for key, row in world.rows().items() if row["level"]}
    assert after == pytest.approx(before, abs=0.01)
    assert world.affect.view().satiated is True and world.level("affect.satisfaction") < 0.5
    # A source that stays unreadable is not papered over for long: after STALE_VIEW the view is recomposed
    # from what can be read (here without the owner's reports, so no dismissal is counted).
    world.appraisals.affect_events = locked
    for minutes in range(1, 12):
        world.shift(minutes=1)
        world.update()
        assert world.affect.view().dismissals == (2 if minutes <= 10 else 0)
    assert FRUSTRATION_KEY in world.rows()


def test_each_event_applies_once_across_updates_and_restarts(world):
    world.outcome("failed", hours=1)
    world.record("interest", "low", "tide tables", hours=1)
    world.intention(status="failed", outcome="failed", failed_at=world.now, hours=0.5)
    assert world.update()["applied"] == 3
    snapshot = {key: row["level"] for key, row in world.rows().items()}
    assert world.update()["applied"] == 0
    restarted = world.build()
    assert restarted.update(world.now)["applied"] == 0
    assert {key: row["level"] for key, row in world.rows().items()} == snapshot


# -- 14. the switches off ------------------------------------------------------------------------------

def test_both_switches_off_is_inert_and_writes_nothing(tmp_path):
    off = World(tmp_path, state_on=False, rules_on=False)
    off.outcome("failed", hours=1)
    off.outcome("failed", hours=0.5)
    off.commitments.add("c-17", hours=2)
    affect = off.affect
    assert affect.active is False and affect.update(NOW) == {"source": None}
    assert affect.view() is None and affect.failing(TOPIC) is None and affect.note_for(TOPIC) == ""
    assert affect.section_lines() == [] and not hasattr(affect, "wait"), "the appraisal wait is the mind's"
    affect.note_novel_topic("tidal energy", NOW)
    assert affect.state()["enabled"] is False and affect.state()["source"] == "off"
    assert off.state.items("affect.") == []
    off.store.close()


def test_the_rules_alone_write_no_state_and_have_no_tone(tmp_path):
    rules = World(tmp_path, state_on=False, rules_on=True)
    rules.outcome("failed", hours=2, approach="the archive export")
    rules.outcome("failed", hours=1, approach="the archive export")
    rules.affect.note_novel_topic("tidal energy", NOW)
    result = rules.update()
    assert result["source"] == "rules" and result["switch"] == [TOPIC]
    assert rules.affect.note_for(TOPIC) == NOTE and rules.affect.view().line == ""
    assert rules.state.items("affect.") == []
    rules.store.close()


# -- 15. routing --------------------------------------------------------------------------------------

def test_each_consumer_reads_its_routed_source_and_the_tone_always_reads_the_state(world, monkeypatch):
    world.outcome("failed", hours=3)
    world.outcome("failed", hours=2)             # two within a day: both sources switch
    world.commitments.add("a", hours=30)
    world.commitments.add("b", hours=40)
    for code in ("asked-1", "asked-2"):
        world.store.create_intention(kind="task", type="research", title=code, drive="curiosity", cls="internal",
                                     decision="ask", decision_reason="x", status="asked", dedup_key=code,
                                     created_at=world.now)
    world.state.set("affect.dismissed", level=0.6, half_life_s=86400)
    world.update()
    state_view = world.affect.view()
    assert state_view.overloaded is True and state_view.satiated is True, "load 0.4 + 0.2 asks; dismissed 0.6"
    rules = affect_rules.view(world.affect.gather(world.now))
    assert rules.overloaded is False and rules.satiated is False, "2 obligations, nothing running, no dismissals"

    monkeypatch.setattr(affect_rules, "RULE_CONSUMERS", frozenset({"overload"}))
    world.update()
    mixed = world.affect.view()
    assert dict(mixed.route) == {"strategy_switch": "state", "overload": "rules", "priority": "state",
                                 "satiation": "state"}
    assert world.affect.state()["source"] == "mixed"
    assert (mixed.overloaded, mixed.load, mixed.obligations) == (rules.overloaded, rules.load, rules.obligations)
    assert mixed.satiated is True and mixed.frustrations == state_view.frustrations
    assert mixed.line == state_view.line and mixed.line.startswith("Mood: ")

    everything = world.build(rules_on=True)
    everything.update(world.now)
    view = everything.view()
    assert set(view.route.values()) == {"rules"} and view.satiated is False and view.overloaded is False
    assert view.line == state_view.line, "the tone line comes from the state even when every consumer reads rules"
    assert everything.state()["source"] == "rules" and everything.state()["levels"]


# -- 16. nothing raises ----------------------------------------------------------------------------------

def test_nothing_raises_into_the_tick(world):
    class Broken:
        def affect_events(self, **_):
            raise RuntimeError("ledger locked")

    world.outcome("failed", hours=1)
    world.update()
    affect = world.build()
    affect.appraisals = Broken()
    affect.store = None
    result = affect.update(world.now)
    assert result["source"] == "state" and OVERLOAD_AT == 0.6
    assert affect.state()["enabled"] is True and isinstance(affect.section_lines(), list)


def test_the_snapshot_is_one_value_both_sources_read(world):
    world.outcome("failed", hours=1)
    world.commitments.add("c-17", hours=2)
    snapshot = world.affect.gather(world.now)
    assert isinstance(snapshot, AffectInputs) and snapshot.owner_id == OWNER and snapshot.cap == 2
    assert [e.kind for e in snapshot.events] == ["failed"] and isinstance(snapshot.events[0], AffectEvent)
    assert snapshot.due_soon == snapshot.obligations and isinstance(snapshot.obligations[0], Obligation)
    assert snapshot.obligations[0].due_at == NOW + timedelta(hours=2) and snapshot.obligations[0].started is False
