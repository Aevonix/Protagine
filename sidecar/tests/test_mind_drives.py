"""Drive unit tests over fixture state, and the concern dynamics (build plan M4 acceptance).

The five drives are pure functions of a ``DriveInputs`` snapshot; the
concerns store carries bump-by-key, half-life decay, capacity, the
broadcast set, the thought budget and anti-rumination.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from protagine.mind import concerns as concerns_module
from protagine.mind.concerns import CAPACITY, Concerns, MindState, decayed, salience_after
from protagine.mind.drives import (
    DRIVES, HEADS_UP_GRACE, DriveInputs, commitment_candidate, curiosity, duty, effective_weights, enabled,
    failure_signature, heads_up_at, mastery, period, run, schedule_key, social, upkeep, weights,
)
from protagine.mind.rank import Candidate, eligible, pick, satiable

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
OWNER = "p-01"


def inputs(**fields) -> DriveInputs:
    return DriveInputs(now=NOW, owner_id=OWNER, **fields)


# ---------------------------------------------------------------------------
# duty
# ---------------------------------------------------------------------------

def test_duty_rises_with_overdue_commitments_reply_waits_stale_tasks_and_stalled_goals():
    level, candidates = duty(inputs(
        commitments=[{"id": "c-1", "person_id": OWNER, "description": "send the report", "priority": 90,
                      "due_at": (NOW - timedelta(hours=3)).isoformat(), "status": "overdue"},
                     {"id": "c-2", "person_id": OWNER, "description": "not due yet", "priority": 50,
                      "due_at": (NOW + timedelta(hours=3)).isoformat(), "status": "pending"},
                     {"id": "c-3", "person_id": "p-02", "description": "deliver the draft", "priority": 50,
                      "due_at": (NOW - timedelta(minutes=5)).isoformat(), "status": "pending",
                      "metadata": {"kind": "deliverable", "content": "Here is the draft."}}],
        reply_waits=[{"wait_id": "w-1", "contact_id": "p-02", "commitment_id": "c-9", "eligibility": "due",
                      "original_local_text": "the invoice question"},
                     {"wait_id": "w-2", "contact_id": "p-03", "commitment_id": "c-8", "eligibility": "due",
                      "native_task_id": "already-bound"}],
        stale_tasks=[{"id": "t-1", "title": "Renew the domain", "assignee": "default", "age_hours": 80},
                     {"id": "t-2", "title": "a mind child", "assignee": "protagine-act", "age_hours": 80},
                     {"id": "t-3", "title": "fresh", "assignee": "default", "age_hours": 5}],
        hermes_goals=[{"id": "g-1", "title": "finish the site", "assignee": "default", "age_hours": 30},
                      {"id": "g-2", "title": "just started", "assignee": "default", "age_hours": 2}]))
    by_type = {c.type: c for c in candidates}
    assert set(by_type) == {"commitment_overdue", "commitment_deliverable", "reply_wait", "stale_task", "goal_stalled"}
    overdue = by_type["commitment_overdue"]
    assert overdue.kind == "task" and overdue.salience == pytest.approx(0.9)
    assert overdue.dedup_key == schedule_key("c-1", "overdue", NOW - timedelta(hours=3))
    assert overdue.success_check == {"kind": "commitment_resolved", "commitment_id": "c-1"}
    assert overdue.invalidates_if == "commitment:c-1:resolved" and "Report what you did" in overdue.text
    deliverable = by_type["commitment_deliverable"]
    assert deliverable.kind == "message" and deliverable.recipient == "p-02" and deliverable.text == "Here is the draft."
    assert by_type["reply_wait"].recipient == "p-02" and by_type["reply_wait"].dedup_key == "reply_wait:w-1"
    stale = by_type["stale_task"]
    assert stale.kind == "message" and stale.recipient == OWNER and stale.salience == pytest.approx(0.5 + 80 / 336)
    assert by_type["goal_stalled"].recipient == OWNER and "finish the site" in by_type["goal_stalled"].text
    assert 0 < level <= 1.0
    assert all(c.drive == "duty" and c.concern_kind == "obligation" for c in candidates)


def test_duty_skips_settled_keys_and_counts_duty_misses_in_its_level():
    state = inputs(commitments=[{"id": "c-1", "person_id": OWNER, "description": "x", "priority": 50,
                                 "due_at": (NOW - timedelta(hours=1)).isoformat(), "status": "overdue"}],
                   expectation_misses=[{"domain": "commitment", "subject": "commitment:c-7", "expectation": "kept"}],
                   settled={schedule_key("c-1", "overdue", NOW - timedelta(hours=1))})
    level, candidates = duty(state)
    assert candidates == [] and level == pytest.approx(min(1.0, 0.8 / 2 + 0.1))


# ---------------------------------------------------------------------------
# curiosity, mastery, upkeep, social
# ---------------------------------------------------------------------------

def test_curiosity_researches_interests_questions_and_knowledge_misses_but_not_duty_misses():
    level, candidates = curiosity(inputs(
        interests=[{"topic": "local history", "weight": 1.0, "sources": ["owner: seeded"]},
                   {"topic": "tide tables", "weight": 3.0}],
        questions=[{"topic": "why did the build fail twice", "sources": ["mind_state"]}],
        expectation_misses=[{"id": "e-1", "domain": "knowledge", "subject": "fact:x", "expectation": "the shop opens at 9"},
                            {"id": "e-2", "domain": "commitment", "subject": "commitment:c", "expectation": "kept"}]))
    week = period(NOW)
    assert week == "2026w39"
    by_key = {c.dedup_key.removesuffix(f":{week}"): c for c in candidates}
    assert set(by_key) == {"research:local-history", "research:tide-tables", "question:why-did-the-build-fail-twice",
                           "question:the-shop-opens-at-9"}
    assert all(c.dedup_key.endswith(f":{week}") for c in candidates)
    assert all(c.dedup_base == base for base, c in by_key.items())          # the stable key under the week
    assert by_key["research:local-history"].salience == pytest.approx(0.8)
    assert by_key["research:tide-tables"].salience == pytest.approx(1.0)
    assert all(c.open_ended and c.kind == "task" and c.text == "" for c in candidates)
    assert by_key["research:local-history"].concern_kind == "interest"
    assert by_key["question:the-shop-opens-at-9"].concern_kind == "question"
    assert level > 0


def test_curiosity_is_satisfied_while_the_finding_is_recent():
    _, candidates = curiosity(inputs(interests=[{"topic": "local history", "weight": 1.0}],
                                     settled={f"research:local-history:{period(NOW)}"}))
    assert candidates == []
    _, faded = curiosity(inputs(interests=[{"topic": "local history", "weight": 0.1}]))
    assert faded == []
    # a new week does not start a second instance while the base is under way or settled
    _, next_week = curiosity(inputs(interests=[{"topic": "local history", "weight": 1.0}], settled={"research:local-history"}))
    assert next_week == []
    later = DriveInputs(now=NOW + timedelta(days=7), owner_id=OWNER, interests=[{"topic": "local history", "weight": 1.0}],
                        settled={f"research:local-history:{period(NOW)}"})
    _, re_armed = curiosity(later)
    assert [c.dedup_key for c in re_armed] == [f"research:local-history:{period(later.now)}"]


def test_mastery_needs_the_same_signature_twice_in_the_window():
    failed = [{"id": "i-1", "type": "research", "description": "Research: tide tables", "failed_at": (NOW - timedelta(days=1)).isoformat(),
               "failed_reason": "no sources", "context": {"topic": "tide tables"}},
              {"id": "i-2", "type": "research", "description": "Research: tide tables", "failed_at": (NOW - timedelta(days=2)).isoformat(),
               "failed_reason": "timed out", "context": {"topic": "tide tables"}},
              {"id": "i-3", "type": "research", "description": "Research: other", "failed_at": (NOW - timedelta(days=1)).isoformat(),
               "context": {"topic": "other"}},
              {"id": "i-4", "type": "research", "description": "Research: tide tables", "failed_at": (NOW - timedelta(days=20)).isoformat(),
               "context": {"topic": "tide tables"}}]
    assert failure_signature(failed[0]) == "research:tide-tables" == failure_signature(failed[3])
    level, candidates = mastery(inputs(failures=failed))
    assert [c.dedup_key for c in candidates] == [f"mastery:research:tide-tables:{period(NOW)}"]
    investigation = candidates[0]
    assert investigation.dedup_base == "mastery:research:tide-tables"
    assert investigation.type == "mastery_investigation" and investigation.drive == "mastery" and investigation.open_ended
    assert investigation.salience == pytest.approx(0.75) and "intention:i-1" in investigation.evidence
    assert level > 0
    _, none = mastery(inputs(failures=failed[:1]))
    assert none == []


def test_mastery_also_rises_with_repeated_corrections():
    corrections = [{"id": "i-1", "type": "stale_task", "verdict": "wrong"}, {"id": "i-2", "type": "stale_task", "verdict": "not_useful"}]
    _, candidates = mastery(inputs(corrections=corrections))
    assert [c.dedup_key for c in candidates] == [f"mastery:corrections:stale-task:{period(NOW)}"]


def test_upkeep_notices_a_failing_store_after_three_strikes_and_a_backlog():
    _, quiet = upkeep(inputs(health={"ledger": 2}))
    assert quiet == []
    level, candidates = upkeep(inputs(health={"ledger": 3, "commitments": 0}, backlog={"projection_lag": 150, "consolidation": 3}))
    assert {c.type for c in candidates} == {"health_notice", "upkeep_task"}
    notice = next(c for c in candidates if c.type == "health_notice")
    assert notice.recipient == OWNER and notice.salience == 0.9 and notice.dedup_key.startswith("health:ledger:")
    task = next(c for c in candidates if c.type == "upkeep_task")
    assert task.kind == "task" and "projection lag" in task.title and task.dedup_base == "upkeep:projection_lag"
    assert task.salience == pytest.approx(0.875)                            # 150 of a 100 threshold
    # at the threshold the task is just eligible under the defaults; twice the threshold is full salience
    _, at_threshold = upkeep(inputs(backlog={"consolidation": 50}))
    assert at_threshold[0].salience == 0.75 and eligible(at_threshold, threshold=0.6, drives={"upkeep": 1.0}) != []
    _, doubled = upkeep(inputs(backlog={"consolidation": 100, "link_proposals": 4}))
    assert [(c.dedup_base, c.salience) for c in doubled] == [("upkeep:consolidation", 1.0)]
    _, held = upkeep(inputs(backlog={"consolidation": 100}, settled={"upkeep:consolidation"}))
    assert held == []


def test_social_proposes_nothing_until_the_people_milestone():
    assert social(inputs(interests=[{"topic": "x"}])) == (0.0, [])


# ---------------------------------------------------------------------------
# weights, satiation and the ranker
# ---------------------------------------------------------------------------

def test_weights_are_the_config_or_flat_when_the_faculty_is_off_and_zero_turns_a_drive_off():
    assert weights({"curiosity": 0.0, "duty": "2"}, faculty_on=True) == {"duty": 2.0, "social": 0.5, "curiosity": 0.0,
                                                                          "mastery": 1.0, "upkeep": 1.0}
    assert weights({"curiosity": 0.0}, faculty_on=False) == {name: 1.0 for name in DRIVES}
    assert enabled({"duty": 1.0, "curiosity": 0.0, "social": 0.5}) == ["duty", "social"]
    results = run(inputs(interests=[{"topic": "x", "weight": 1}]), {"curiosity": 0.0, "duty": 1.0})
    assert "curiosity" not in results and results["duty"] == (0.0, [])


def test_satiation_damps_the_effective_weight_only_when_the_faculty_is_on():
    base = {"duty": 1.0, "curiosity": 0.5, "social": 0.5, "mastery": 1.0, "upkeep": 1.0}
    assert effective_weights(base, {"curiosity": 1.0})["curiosity"] == pytest.approx(0.25)
    assert effective_weights(base, {"curiosity": 1.0})["duty"] == 1.0
    assert effective_weights(base, {"curiosity": 1.0}, faculty_on=False)["curiosity"] == 1.0


def test_the_weight_orders_but_the_threshold_is_on_the_drive_free_score():
    research = Candidate(type="research", drive="curiosity", kind="task", title="r", dedup_key="research:x",
                         salience=0.8, cost=0.15)
    overdue = Candidate(type="commitment_overdue", drive="duty", kind="task", title="o", dedup_key="commitment:c:overdue",
                        salience=0.8, cost=0.15)
    weights_ = {"duty": 1.0, "curiosity": 0.5}
    ranked = eligible([research, overdue], threshold=0.6, drives=weights_)
    assert [c.dedup_key for c, _ in ranked] == ["commitment:c:overdue", "research:x"]   # duty first
    assert ranked[1][1] == pytest.approx(0.8 * 0.5 * 0.85)                            # the weight is in the score
    assert eligible([research], threshold=0.6, drives={"curiosity": 0.0}) == []       # 0 turns it off
    assert pick([research], threshold=0.6, drives=weights_)[0] is research


def test_satiation_holds_self_chosen_work_and_not_obligations():
    research = Candidate(type="research", drive="curiosity", kind="task", title="r", dedup_key="research:x:2026w39",
                         dedup_base="research:x", salience=0.8, cost=0.2)            # 0.64 drive-free: eligible
    base = weights({"curiosity": 0.5})
    assert satiable(research) and eligible([research], threshold=0.6, drives=base, base=base) != []
    satiated = effective_weights(base, {"curiosity": 1.0})
    assert eligible([research], threshold=0.6, drives=satiated, base=base) == []      # 0.64 x 0.5 < 0.6
    assert pick([research], threshold=0.6, drives=satiated, base=base) == (None, pytest.approx(0.8 * 0.25 * 0.8))
    fading = effective_weights(base, {"curiosity": 0.1})                              # 0.64 x 0.95 >= 0.6
    assert [c.dedup_base for c, _ in eligible([research], threshold=0.6, drives=fading, base=base)] == ["research:x"]
    # without a base the threshold scales with the same weights (the flat-priority arm: nothing satiates)
    assert eligible([research], threshold=0.6, drives=satiated) != []
    # an obligation is owed whatever the drive's satiety: satiation only orders it
    overdue = Candidate(type="commitment_overdue", drive="duty", kind="task", title="o", dedup_key="commitment:c:overdue",
                        salience=0.8, cost=0.15)
    notice = Candidate(type="health_notice", drive="upkeep", kind="message", title="h", dedup_key="health:ledger:d",
                       salience=0.9, cost=0.0, recipient=OWNER)
    step = Candidate(type="goal_step", drive="curiosity", kind="task", title="s", dedup_key="goal:g:step:2",
                     salience=0.8, cost=0.2, parent_goal_id="g")
    full = effective_weights(weights(None), {"duty": 1.0, "upkeep": 1.0, "curiosity": 1.0})
    assert not any(satiable(c) for c in (overdue, notice, step))
    assert [c.type for c, _ in eligible([overdue, notice, step, research], threshold=0.6, drives=full, base=weights(None))] == [
        "health_notice", "commitment_overdue", "goal_step"]                        # ordered by the satiated score


# ---------------------------------------------------------------------------
# concerns: bump, decay, capacity, the broadcast set, anti-rumination
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    clock = {"now": NOW}
    concerns = Concerns(tmp_path / "mind.db", clock=lambda: clock["now"])
    concerns.tick = lambda **delta: clock.__setitem__("now", clock["now"] + timedelta(**delta))  # type: ignore[attr-defined]
    yield concerns
    concerns.close()


def test_bump_merges_repeats_by_dedup_key_and_keeps_the_higher_salience(store):
    first, outcome = store.bump(drive="duty", kind="obligation", summary="overdue: report", dedup_key="commitment:c:overdue",
                                salience=0.7, sources=["commitment:c"], detail={"type": "commitment_overdue"})
    assert outcome == "created" and first.status == "open" and first.salience == 0.7
    again, outcome = store.bump(drive="duty", kind="obligation", summary="overdue: report", dedup_key="commitment:c:overdue",
                                salience=0.5, sources=["overdue by 2 h"])
    assert outcome == "bumped" and again.id == first.id and again.salience == 0.7
    assert again.sources == ["commitment:c", "overdue by 2 h"] and store.count("open") == 1


def test_decay_halves_salience_every_twelve_hours_and_evicts_the_floor_and_beyond_capacity(store):
    for index in range(CAPACITY + 3):
        store.bump(drive="curiosity", kind="interest", summary=f"topic {index}", dedup_key=f"research:{index}",
                   salience=0.3 + index / 100.0)
    store.decay()                                   # the first decay only anchors the clock
    assert store.count("open") == CAPACITY and store.count("dropped") == 3
    top_before = [c.dedup_key for c in store.top(k=3)]
    store.tick(hours=12)
    result = store.decay()
    concern = store.by_key(f"research:{CAPACITY + 2}")
    assert concern.salience == pytest.approx((0.3 + (CAPACITY + 2) / 100.0) / 2, rel=1e-3)
    assert [c.dedup_key for c in store.top(k=3)] == top_before and result["evicted"] == 0
    store.tick(hours=12 * 5)
    store.decay()
    assert store.count("open") == 0                 # everything faded below the floor
    assert decayed(0.8, timedelta(hours=24)) == pytest.approx(0.2)


def test_the_broadcast_set_is_the_top_three_with_thought_budget_left(store):
    for index, salience in enumerate((0.9, 0.8, 0.7, 0.6)):
        store.bump(drive="curiosity", kind="interest", summary=f"c{index}", dedup_key=f"k{index}", salience=salience,
                   max_thoughts=1)
    assert [c.dedup_key for c in store.top()] == ["k0", "k1", "k2"]
    exhausted = store.by_key("k0")
    store.progress(exhausted.id, progressed=False)   # one thought spent, none left
    assert [c.dedup_key for c in store.top()] == ["k1", "k2", "k3"]
    assert store.by_key("k0").salience == pytest.approx(salience_after(0.9, progressed=False))


def test_anti_rumination_and_the_intended_resolved_settled_cycle(store):
    concern, _ = store.bump(drive="curiosity", kind="interest", summary="tides", dedup_key="research:tides", salience=0.8)
    assert store.intended(concern.id, "intention-1").status == "intended"
    assert store.by_intention("intention-1").id == concern.id
    assert store.top() == []                                   # under way: not a candidate
    bumped, outcome = store.bump(drive="curiosity", kind="interest", summary="tides", dedup_key="research:tides",
                                 salience=0.9, sources=["new evidence"])
    assert outcome == "bumped" and bumped.status == "intended" and "new evidence" in bumped.sources
    after = store.progress(concern.id, progressed=True, note="partial")
    assert after.status == "open" and after.intention_id is None and after.thoughts_spent == 1
    assert after.salience == pytest.approx(0.8 * 0.9) and after.detail["last_note"] == "partial"
    store.resolve(concern.id, note="finding stored")
    resolved, outcome = store.bump(drive="curiosity", kind="interest", summary="tides", dedup_key="research:tides", salience=0.8)
    assert outcome == "settled" and resolved.status == "resolved"
    assert store.settled_keys(NOW - timedelta(days=1)) == {"research:tides"}
    store.tick(days=8)
    reopened, outcome = store.bump(drive="curiosity", kind="interest", summary="tides", dedup_key="research:tides", salience=0.8)
    assert outcome == "reopened" and reopened.status == "open" and reopened.thoughts_spent == 0


def test_a_goal_concern_never_decays_below_the_broadcast_set(store):
    store.bump(drive="curiosity", kind="goal", summary="goal", dedup_key="goal:x", salience=0.9)
    for index in range(4):
        store.bump(drive="duty", kind="obligation", summary=f"d{index}", dedup_key=f"d{index}", salience=0.95)
    store.decay()
    store.tick(hours=36)
    store.decay()
    third = store.open(limit=10)[2].salience
    goal = store.by_key("goal:x")
    assert goal.status == "open" and goal.salience >= third - 1e-9


def test_mind_state_levels_relax_toward_their_baseline_with_cited_causes(tmp_path):
    clock = {"now": NOW}
    state = MindState(concerns_module.open_mind_db(tmp_path / "mind.db"), clock=lambda: clock["now"])
    assert state.bump("satiety.curiosity", 1.0, half_life_s=3600, causes=["intention:i-1"]) == 1.0
    for index in range(7):
        state.bump("satiety.curiosity", 0.0, causes=[f"intention:i-{index}"])
    assert len(state.get("satiety.curiosity")["causes"]) == 5
    clock["now"] += timedelta(hours=1)
    state.decay()
    assert state.get("satiety.curiosity")["level"] == pytest.approx(0.5, rel=1e-3)
    state.set("interest:tides", level=1.0, text="tide tables")
    assert state.items("interest:")[0]["text"] == "tide tables" and state.get("interest:tides")["half_life_s"] is None
    assert state.delete("interest:tides") and state.items("interest:") == []


# ---------------------------------------------------------------------------
# duty: owner reminders are messages, heads-ups before a deadline, the grace after one
# ---------------------------------------------------------------------------

def _owner_row(**fields):
    return {"id": "c-7", "person_id": OWNER, "description": "send the report", "priority": 50,
            "due_at": (NOW - timedelta(hours=2, minutes=5)).isoformat(), "status": "overdue",
            "source_type": "cognition", **fields}


def test_an_owner_commitment_from_conversation_is_a_reminder_message_not_a_worker_task():
    due = NOW - timedelta(hours=2, minutes=5)
    reminder = commitment_candidate(_owner_row(), due, NOW, owner_id=OWNER)
    assert reminder.kind == "message" and reminder.type == "commitment_reminder" and reminder.recipient == OWNER
    assert reminder.dedup_key == schedule_key("c-7", "overdue", due)          # the same key as the task form has
    assert "send the report" in reminder.text and due.strftime("%Y-%m-%d %H:%M UTC") in reminder.text
    assert reminder.text.endswith("2 h ago.") and "Report what you did" not in reminder.text
    assert reminder.invalidates_if == "commitment:c-7:resolved" and reminder.source_id == "c-7"
    assert reminder.success_check == {"kind": "commitment_resolved", "commitment_id": "c-7"}
    assert reminder.salience == pytest.approx(0.8) and reminder.cost == pytest.approx(0.05)
    assert commitment_candidate(_owner_row(priority=90), due, NOW, owner_id=OWNER).salience == pytest.approx(0.9)
    # Work the agent itself must do (a row not spoken in a conversation) stays a task.
    assert commitment_candidate(_owner_row(source_type="manual"), due, NOW, owner_id=OWNER).kind == "task"
    # A deliverable keeps its own message form.
    deliverable = commitment_candidate(_owner_row(metadata={"kind": "deliverable", "content": "Here."}), due, NOW,
                                       owner_id=OWNER)
    assert deliverable.type == "commitment_deliverable" and deliverable.text == "Here."
    _, candidates = duty(inputs(commitments=[_owner_row()]))
    assert [c.type for c in candidates] == ["commitment_reminder"]


def test_who_owes_the_work_decides_between_a_reminder_and_a_task():
    """``metadata.obligor`` says who owes the work. The owner's own promise, and a third party's
    promise the owner is tracking, come back to the owner as a reminder; the assistant's own
    promise ("I'll send you the report by 3pm") is work the body performs, as it was before
    reminders existed. A row without the field (an extractor that does not record it) reads as
    the owner's, exactly as before."""
    due = NOW - timedelta(hours=2, minutes=5)
    for metadata in (None, {}, {"obligor": "owner"}, {"obligor": "p-02"}):
        candidate = commitment_candidate(_owner_row(metadata=metadata), due, NOW, owner_id=OWNER)
        assert candidate.type == "commitment_reminder" and candidate.recipient == OWNER, metadata
    promised = commitment_candidate(_owner_row(metadata={"obligor": "assistant"}), due, NOW, owner_id=OWNER)
    assert promised.kind == "task" and promised.type == "commitment_overdue" and promised.recipient == OWNER
    assert "Fulfil the overdue commitment to the owner: send the report" in promised.text
    assert promised.dedup_key == commitment_candidate(_owner_row(), due, NOW, owner_id=OWNER).dedup_key
    _, candidates = duty(inputs(commitments=[_owner_row(metadata={"obligor": "assistant"})]))
    assert [c.type for c in candidates] == ["commitment_overdue"]


def test_a_row_from_a_contacts_turn_is_routed_by_its_obligor_not_by_whose_turn_it_was():
    """Capture files a row under the speaker, so a contact's turn yields rows the contact owes, rows
    the owner owes and rows the assistant took on. Only the assistant's own work is a task (owed to
    that contact); a promise anyone else made comes back to the owner as a reminder, never as a
    worker task to "fulfil" someone else's promise."""
    due = NOW - timedelta(hours=2, minutes=5)
    for metadata in (None, {"obligor": "p-02"}, {"obligor": "owner"}, {"obligor": OWNER}, {"obligor": "Kim"}):
        candidate = commitment_candidate(_owner_row(person_id="p-02", description="p-02 sends the signed form",
                                                    metadata=metadata), due, NOW, owner_id=OWNER)
        assert (candidate.type, candidate.kind, candidate.recipient) == ("commitment_reminder", "message", OWNER), \
            metadata
        assert "Fulfil" not in candidate.text and "p-02 sends the signed form" in candidate.text
    owed = commitment_candidate(_owner_row(person_id="p-02", metadata={"obligor": "assistant"}), due, NOW,
                                owner_id=OWNER)
    assert (owed.type, owed.kind, owed.recipient) == ("commitment_overdue", "task", "p-02")
    assert "Fulfil the overdue commitment to contact p-02: send the report" in owed.text


def test_due_intentions_are_keyed_by_the_schedule_so_a_moved_deadline_earns_one_more():
    """The dedup key of a reminder, an overdue task or a heads-up carries the deadline it was raised
    for: the same deadline is reported once; a deadline the owner moves after the report went out
    ("remind me again tomorrow") is a new schedule with its own key."""
    due = NOW - timedelta(hours=2, minutes=5)
    reminder = commitment_candidate(_owner_row(), due, NOW, owner_id=OWNER)
    assert reminder.dedup_key == f"commitment:c-7:overdue:{due.strftime('%Y%m%dT%H%M%SZ')}"
    assert reminder.dedup_key == schedule_key("c-7", "overdue", due)
    moved = due + timedelta(days=1)
    again = commitment_candidate(_owner_row(due_at=moved.isoformat()), moved, NOW + timedelta(days=1), owner_id=OWNER)
    assert again.dedup_key == schedule_key("c-7", "overdue", moved) != reminder.dedup_key
    # The stamp is UTC whatever zone the deadline was stated in; the heads-up has its own event.
    local = due.astimezone(timezone(timedelta(hours=2)))
    assert schedule_key("c-7", "overdue", local) == reminder.dedup_key
    assert schedule_key("c-7", "heads_up", due) == f"commitment:c-7:heads_up:{due.strftime('%Y%m%dT%H%M%SZ')}"


def test_heads_up_at_reads_an_iso_time_or_a_lead_and_needs_a_deadline():
    due = NOW + timedelta(minutes=20)
    row = {"id": "c-8", "due_at": due.isoformat(),
           "metadata": {"heads_up_at": (due - timedelta(minutes=5)).isoformat()}}
    assert heads_up_at(row) == due - timedelta(minutes=5)
    assert heads_up_at({**row, "metadata": {"lead_minutes": 8}}) == due - timedelta(minutes=8)
    assert heads_up_at({**row, "metadata": {"lead_minutes": "8"}}) == due - timedelta(minutes=8)
    for metadata in (None, {}, {"lead_minutes": 0}, {"lead_minutes": "soon"}, {"lead_minutes": True},
                     {"heads_up_at": "not a time"}, {"heads_up_at": (due + timedelta(minutes=1)).isoformat()}):
        assert heads_up_at({**row, "metadata": metadata}) is None, metadata
    assert heads_up_at({**row, "due_at": None}) is None


def test_duty_raises_a_heads_up_between_the_asked_time_and_the_deadline_and_the_reminder_after():
    due = NOW + timedelta(minutes=10)
    row = {"id": "c-9", "person_id": OWNER, "description": "file the return", "priority": 60, "status": "pending",
           "due_at": due.isoformat(), "source_type": "cognition", "metadata": {"lead_minutes": 15}}
    _, candidates = duty(inputs(commitments=[row]))
    heads_up, = candidates
    assert heads_up.type == "commitment_due_soon" and heads_up.kind == "message" and heads_up.recipient == OWNER
    assert heads_up.dedup_key == schedule_key("c-9", "heads_up", due)
    assert heads_up.invalidates_if == "commitment:c-9:resolved"
    assert heads_up.success_check is None and heads_up.source_id == "c-9"
    assert "file the return" in heads_up.text and due.strftime("%H:%M UTC") in heads_up.text
    assert heads_up.text.endswith("10 min from now.")
    # Before the asked time: nothing. A row that was never asked for a heads-up: nothing until due.
    early = DriveInputs(now=NOW - timedelta(minutes=6), owner_id=OWNER, commitments=[row])
    assert duty(early)[1] == []
    assert duty(inputs(commitments=[{**row, "metadata": None}]))[1] == []
    # Past the deadline the reminder takes over on its own key; the heads-up is not raised again.
    late = DriveInputs(now=due + timedelta(minutes=1), owner_id=OWNER, commitments=[row])
    assert [c.type for c in duty(late)[1]] == ["commitment_reminder"]


def test_a_delivered_heads_up_holds_the_overdue_reminder_for_the_grace_and_no_longer():
    due = NOW - timedelta(minutes=5)
    row = {"id": "c-10", "person_id": OWNER, "description": "call the bank", "priority": 60, "status": "overdue",
           "due_at": due.isoformat(), "source_type": "cognition", "metadata": {"lead_minutes": 15}}
    assert HEADS_UP_GRACE == timedelta(minutes=30)
    held = inputs(commitments=[row], heads_ups={"c-10": NOW - timedelta(minutes=20)})
    assert duty(held)[1] == []
    lapsed = inputs(commitments=[row], heads_ups={"c-10": NOW - timedelta(minutes=31)})
    assert [c.type for c in duty(lapsed)[1]] == ["commitment_reminder"]
    shorter = inputs(commitments=[row], heads_ups={"c-10": NOW - timedelta(minutes=20)},
                     heads_up_grace=timedelta(minutes=10))
    assert [c.type for c in duty(shorter)[1]] == ["commitment_reminder"]
    # The grace is per row: another row's heads-up holds nothing here.
    other = inputs(commitments=[row], heads_ups={"c-11": NOW - timedelta(minutes=1)})
    assert [c.type for c in duty(other)[1]] == ["commitment_reminder"]
