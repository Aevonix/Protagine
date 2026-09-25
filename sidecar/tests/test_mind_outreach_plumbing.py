"""The plumbing owner outreach needs, pinned so the types that existed before it behave as they did.

``rank._gated`` and ``Outcomes._feedback`` treat only contact check-ins as check-ins (a social
candidate that is not one is gated by feedback like any other); the store's budget counters take
a type filter; the daily owner budget no longer counts unprompted outreach, which has its own
(``outreach_per_day``), and a requested answer is counted by neither; overload never postpones
care or an answer; people off keeps the social drive's weight and retires pending check-ins.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from protagine.initiatives.store import InitiativeStore
from protagine.mind import affect, rank
from protagine.mind.authority import Authority, Budgets, Policy
from protagine.mind.outcomes import Outcomes
from protagine.mind.rank import CHECK_IN_TYPES, OUTREACH_ANSWER, OUTREACH_TYPES, Candidate, eligible

OWNER, CONTACT = "p-01", "p-02"
NOW = datetime(2031, 5, 6, 12, 0, tzinfo=timezone.utc)


class Feedback:
    def __init__(self, values=None):
        self.values, self.records = dict(values or {}), []

    def multiplier(self, key):
        return self.values.get(key, 1.0)

    def record(self, key, outcome, source=None):
        self.records.append((key, outcome, source))


def candidate(type, drive, **fields):
    base = dict(type=type, drive=drive, kind="message", title=type, dedup_key=f"{type}:x", salience=0.9, cost=0.0,
                recipient=OWNER)
    base.update(fields)
    return Candidate(**base)


def test_the_type_names_are_one_set_each():
    assert CHECK_IN_TYPES == frozenset({"check_in", "commitment_check_in"})
    assert OUTREACH_TYPES == frozenset({"outreach_finding", "outreach_loop", "outreach_care"})
    assert OUTREACH_ANSWER == "outreach_answer" and OUTREACH_ANSWER not in OUTREACH_TYPES
    from protagine.mind import drives
    assert drives.CHECK_IN_TYPES is CHECK_IN_TYPES


def test_a_contact_check_in_still_bypasses_feedback_and_other_social_work_does_not():
    low = Feedback({"check_in:social": 0.5, f"reach_out:{CONTACT}": 0.5, "outreach_finding:social": 0.5})
    check_in = candidate("check_in", "social", recipient=CONTACT)
    granted = candidate("commitment_check_in", "duty", recipient=CONTACT, source_type="commitment")
    assert {c.type for c, _ in eligible([check_in, granted], feedback=low)} == {"check_in", "commitment_check_in"}
    # A social candidate that is not a check-in is gated by the owner's feedback like any other work.
    finding = candidate("outreach_finding", "social")
    assert eligible([finding], feedback=Feedback()) != []
    assert eligible([finding], feedback=low) == []


def row(store, **fields):
    base = dict(kind="message", type="check_in", title="t", drive="social", cls="contact", decision="act",
                decision_reason="r", status="approved", dedup_key=None, recipient=CONTACT, hermes_kind="none",
                created_at=NOW)
    base.update(fields)
    created, _ = store.create_intention(**base)
    store.transition(created.id, base["status"], action="queued", at=NOW)
    return created


@pytest.fixture
def store(tmp_path):
    value = InitiativeStore(state_dir=tmp_path)
    yield value
    value.close()


def test_feedback_keys_a_contact_check_in_on_the_contact_only_and_outreach_on_its_type_and_topic(store):
    feedback = Feedback()
    outcomes = Outcomes(store, feedback=feedback, clock=lambda: NOW)
    check_in = row(store)
    granted = row(store, type="commitment_check_in", drive="duty")
    finding = row(store, type="outreach_finding", recipient=OWNER, cls="owner",
                  context={"topic": "tidal energy", "topic_slug": "tidal-energy", "text": "x"})
    for item in (check_in, granted, finding):
        outcomes.rate(item.id, "not_useful", by="owner")
    keys = [key for key, _, _ in feedback.records]
    assert keys == [f"reach_out:{CONTACT}", "commitment_check_in:duty", f"reach_out:{CONTACT}",
                    "outreach_finding:social", "outreach_topic:tidal-energy"]
    assert f"reach_out:{OWNER}" not in keys, "outreach never lowers every discretionary owner notice"


def test_the_store_counters_filter_by_type(store):
    row(store, type="outreach_finding", recipient=OWNER)
    row(store, type="commitment_reminder", recipient=OWNER)
    since = NOW - timedelta(hours=1)
    assert store.count_transitions("queued", since, kind="message", recipient=OWNER) == 2
    assert store.count_transitions("queued", since, kind="message", include_types=tuple(OUTREACH_TYPES)) == 1
    assert store.count_transitions("queued", since, kind="message", recipient=OWNER,
                                   exclude_types=tuple(OUTREACH_TYPES)) == 1
    assert store.last_transition_at("queued", types=("outreach_loop",)) is None
    assert store.last_transition_at("queued", types=tuple(OUTREACH_TYPES)) == NOW


def authority(store, **budgets):
    return Authority(Policy(level="standard", budgets=Budgets.from_config(budgets)), store, owner_id=OWNER,
                     clock=lambda: NOW)


def test_unprompted_outreach_has_its_own_budget_and_never_takes_a_reminders_slot(store):
    assert Budgets().outreach_per_day == 3 and Budgets.from_config({"outreach_per_day": 1}).outreach_per_day == 1
    gate = authority(store, outreach_per_day=2, owner_messages_per_day=1)
    assert gate.budget_check(kind="message", recipient=OWNER, type="outreach_finding") is None
    row(store, type="outreach_finding", recipient=OWNER)
    row(store, type="outreach_care", recipient=OWNER)
    assert "2 outreach messages per day" in gate.budget_check(kind="message", recipient=OWNER, type="outreach_loop")
    # Two outreach messages went out, and the reminder the owner asked for still has its slot.
    assert gate.budget_check(kind="message", recipient=OWNER, type="commitment_reminder") is None
    row(store, type="commitment_reminder", recipient=OWNER)
    assert "owner messages per day" in gate.budget_check(kind="message", recipient=OWNER, type="commitment_reminder")
    # A reminder spent the owner budget; outreach counts only its own.
    fresh = authority(store, outreach_per_day=5, owner_messages_per_day=1)
    assert fresh.budget_check(kind="message", recipient=OWNER, type="outreach_finding") is None
    # The answer to a follow-up the owner asked for is counted by neither budget.
    assert gate.budget_check(kind="message", recipient=OWNER, type=OUTREACH_ANSWER) is None
    row(store, type=OUTREACH_ANSWER, recipient=OWNER)
    assert "2 outreach messages" in gate.budget_check(kind="message", recipient=OWNER, type="outreach_finding")


def test_a_requested_answer_acts_at_suggest_and_unprompted_outreach_waits_for_the_digest(store):
    assert OUTREACH_ANSWER in Authority.REQUESTED_TYPES
    assert not set(OUTREACH_TYPES) & set(Authority.REQUESTED_TYPES)
    gate = Authority(Policy(level="suggest"), store, owner_id=OWNER, clock=lambda: NOW)
    assert gate.decide(kind="message", recipient=OWNER, text="x", type=OUTREACH_ANSWER).decision == "act"
    held = gate.decide(kind="message", recipient=OWNER, text="x", type="outreach_finding")
    assert held.decision == "ask" and held.notice is False


def test_overload_never_postpones_care_or_an_answer():
    for type in ("outreach_finding", "outreach_loop", "check_in"):
        assert affect.postponable(SimpleNamespace(type=type, drive="social", kind="message")) is True
    for type in ("outreach_care", OUTREACH_ANSWER):
        assert affect.postponable(SimpleNamespace(type=type, drive="social", kind="message")) is False


def test_rank_names_the_owed_outreach_types_that_feedback_never_weighs():
    answer = candidate(OUTREACH_ANSWER, "social", topic="tidal energy")
    assert answer.multiplier_keys() == []
    followup = candidate("outreach_followup", "duty", kind="task", recipient=None)
    assert followup.multiplier_keys() == []
    finding = candidate("outreach_finding", "social", topic="Tidal energy")
    assert finding.multiplier_keys() == ["outreach_finding:social", "outreach_topic:tidal-energy"]
    assert rank.feedback_multiplier(Feedback({"outreach_topic:tidal-energy": 0.5}), finding) == 0.5


def test_satiation_never_holds_back_an_answer_the_owner_asked_for():
    view = affect.AffectView(route={}, owner_id=OWNER, satiated=True, boost=0.5)
    answer = candidate(OUTREACH_ANSWER, "social")
    finding = candidate("outreach_finding", "social")
    assert affect.discretionary(finding) and not affect.discretionary(answer)
    assert view.threshold_factor(finding) == 1.5 and view.threshold_factor(answer) == 1.0
