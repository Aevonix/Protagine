"""Lessons (architecture 4.8, build plan M9): the mind's own ledger record, used in task bodies and turns,
scored and retired by joins over verified outcomes.

A lesson is not a source claim: it is a family of owner-audience entries in the mind's own session
(``scope='session'``), so ordinary recall never shows it as something the owner said, and it is
erased together with the owner turns it quotes. Everything runs against the real ledger and
initiative store.
"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import timedelta

import pytest

from protagine.api.routers import mind as mind_router
from protagine.mind import lessons as lessons_module
from protagine.mind.lessons import Lesson
from test_mind_loop import OWNER, Fixture

ROOMY = {"breaker": {"failures": 50}, "budgets": {"tasks_per_hour": 50, "concurrent_tasks": 50}}
FIELDS = {"signature": "topic:order-codes", "kind": "strategy", "title": "Order codes follow the channel rule",
          "when_to_use": "an order code is asked for", "content": "Start with the channel letter, then the digits."}


@pytest.fixture
def fx(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fixture = Fixture(tmp_path, config=ROOMY)
    yield fixture
    mind_router.set_mind(None)
    fixture.store.close()


def owner_turn(fx, turn_id, text, session="day-01", reply="Updated."):
    fx.ledger.record_source(turn_id, contact_id=OWNER, session_id=session, derive_claims=False, messages=[
        {"role": "user", "content": text}, {"role": "assistant", "content": reply}], occurred_at=fx.now.isoformat())


def admit(fx, *, fields=None, verified="owner", status="active", lineage=("verdict-1",), origin="night", **extra):
    return fx.mind.lessons.admit(dict(fields or FIELDS), verified=verified, origin=origin, status=status,
                                 evidence=[f"turn:{ref}" for ref in lineage], lineage=list(lineage), now=fx.now,
                                 **extra)


def task(fx, n, *, lesson_ids=(), outcome=None, verified=None, verdict=None, check=None, passed=None,
         reason=None, created=None):
    """A task row that carried ``lesson_ids``, settled as given."""
    row, _ = fx.store.create_intention(kind="task", type="research", title=f"Order code {n}", drive="duty",
                                       cls="internal", decision="act", decision_reason="test", status="approved",
                                       dedup_key=f"task-{n}", context={"topic": "order codes"},
                                       success_check=check, created_at=created or fx.now)
    updates = {"lesson_ids": list(lesson_ids)}
    if outcome is not None:
        metadata = {"check": {"passed": passed, "at": fx.now.isoformat()}} if passed is not None else {}
        updates.update(outcome=outcome, verified=verified, verdict=verdict, result_metadata=metadata,
                       status={"done": "done", "failed": "failed"}[outcome])
        if reason:
            updates["failed_reason"] = reason
    return fx.store.update(row.id, **updates)


def test_a_lesson_is_an_owner_mind_entry_that_recall_never_surfaces(fx):
    owner_turn(fx, "verdict-1", "Verdict: by our procedure the zebra order code is C3403.")
    lesson = admit(fx)
    assert isinstance(lesson, Lesson) and lesson.id.startswith("L-") and len(lesson.id) == 12
    assert lesson.status == "active" and lesson.verified == "owner" and lesson.origin == "night"
    assert fx.mind.lessons.get(lesson.id) == lesson and fx.mind.lessons.all() == [lesson]
    with closing(fx.ledger._connect()) as conn:
        row = dict(conn.execute("SELECT * FROM turn_sources WHERE turn_id=?",
                                (f"mind:lesson:{lesson.id}:admitted",)).fetchone())
    assert (row["contact_id"], row["session_id"], row["scope"]) == (OWNER, "mind", "session")
    [message] = json.loads(row["messages_json"])
    assert message["role"] == "assistant" and message["content"].startswith("Lesson (strategy): when ")
    assert message["metadata"]["origin"] == "mind" and message["metadata"]["memory_kind"] == "procedure"
    assert message["metadata"]["lesson"]["signature"] == "topic:order-codes"
    assert [ref["source_id"] for ref in message["_supplied_sources"]] == ["verdict-1"]
    # Recall from any real session never finds it; no claim is derived from it.
    for session in ("day-02", "later"):
        found = fx.ledger.search_sources("order code channel letter digits", contact_id=OWNER, session_id=session)
        assert all(not hit["turn_id"].startswith("mind:lesson:") for hit in found)
    with closing(fx.ledger._connect()) as conn:
        assert conn.execute("SELECT count(*) FROM source_claim_jobs WHERE turn_id LIKE 'mind:lesson:%'").fetchone()[0] == 0


def test_forgetting_a_quoted_owner_turn_forgets_the_lesson(fx):
    owner_turn(fx, "verdict-1", "Verdict: by our procedure the zebra order code is C3403.")
    lesson = admit(fx)
    fx.mind.lessons.set_status(lesson.id, "retired", reason="the owner retired it", by="owner", now=fx.now)
    fx.ledger.erase_sources(contact_id=OWNER, turn_ids=["verdict-1"])
    assert fx.mind.lessons.get(lesson.id) is None and fx.mind.lessons.all(include_closed=True) == []
    with closing(fx.ledger._connect()) as conn:
        assert conn.execute("SELECT count(*) FROM turn_sources WHERE turn_id LIKE 'mind:lesson:%'").fetchone()[0] == 0


def test_one_current_lesson_per_signature_and_kind_an_add_supersedes(fx):
    owner_turn(fx, "verdict-1", "Verdict: the code starts with the channel letter.")
    owner_turn(fx, "verdict-2", "Verdict: odd orders swap the digit pairs.", session="day-02")
    first = admit(fx)
    second = admit(fx, fields={**FIELDS, "content": "Channel letter, digits; odd orders swap the pairs."},
                   lineage=("verdict-2",))
    assert second.id != first.id and second.supersedes == first.id and second.status == "active"
    assert fx.mind.lessons.get(first.id).status == "superseded"
    assert [item.id for item in fx.mind.lessons.all()] == [second.id]
    assert {item.id for item in fx.mind.lessons.all(include_closed=True)} == {first.id, second.id}
    # A pitfall on the same signature is its own lesson; another signature too.
    pitfall = admit(fx, fields={**FIELDS, "kind": "pitfall", "content": "Never use Z as the channel letter."})
    other = admit(fx, fields={**FIELDS, "signature": "topic:slot-labels", "content": "Morning is before noon."})
    assert pitfall.supersedes is None and other.supersedes is None
    assert {item.id for item in fx.mind.lessons.all()} == {second.id, pitfall.id, other.id}


def test_admitting_the_same_lesson_twice_is_one_lesson(fx):
    owner_turn(fx, "verdict-1", "Verdict: the code starts with the channel letter.")
    first = admit(fx)
    fx.shift(minutes=5)
    again = admit(fx)
    assert again.id == first.id and again.admitted_at == first.admitted_at
    assert len(fx.mind.lessons.all(include_closed=True)) == 1


def test_only_verified_uses_count_and_a_result_field_check_is_not_verification(fx):
    """A worker's completion summary with no check and no owner verdict produces no lesson win (M9 acceptance),
    and neither does a ``result_field`` check: it reads only the worker's own report (F4)."""
    lessons = fx.mind.lessons
    owner_turn(fx, "verdict-1", "Verdict: the code starts with the channel letter.")
    lesson = admit(fx)
    ids = [lesson.id]
    finding = {"kind": "result_field", "field": "finding"}
    external = {"kind": "commitment_resolved", "commitment_id": "c-1"}
    summary_only = task(fx, 1, lesson_ids=ids, outcome="done", verified="none")
    self_checked = task(fx, 2, lesson_ids=ids, outcome="done", verified="check", check=finding, passed=True)
    owner_useful = task(fx, 3, lesson_ids=ids, outcome="done", verified="owner", verdict="useful")
    checked = task(fx, 4, lesson_ids=ids, outcome="done", verified="check", check=external, passed=True)
    hermes = task(fx, 5, lesson_ids=ids, outcome="failed", verified="hermes_failure", reason="the site was down")
    failed_bare = task(fx, 6, lesson_ids=ids, outcome="failed", verified="none")
    owner_wrong = task(fx, 7, lesson_ids=ids, outcome="done", verified="owner", verdict="wrong")
    assert lessons.verified_source(summary_only) == "none" and lessons.verified_source(self_checked) == "none"
    assert lessons.verified_source(checked) == "check" and lessons.verified_source(owner_useful) == "owner"
    assert lessons.verified_source(hermes) == "hermes_failure"
    assert [lessons.use_result(row) for row in (summary_only, self_checked, owner_useful, checked, hermes,
                                                failed_bare, owner_wrong)] == [
        None, None, "win", "win", "loss", None, "loss"]
    assert lessons.tally(fx.now) == {lesson.id: {"uses": 4, "wins": 2, "losses": 2, "applied": 7}}
    # Rows older than the tally window no longer count.
    fx.shift(days=91)
    assert lessons.tally(fx.now) == {}


def test_a_lesson_under_forty_percent_after_five_verified_uses_is_retired(fx):
    lessons = fx.mind.lessons
    owner_turn(fx, "verdict-1", "Verdict: the code starts with the channel letter.")
    lesson = admit(fx)
    for n in range(4):
        task(fx, n, lesson_ids=[lesson.id], outcome="failed", verified="hermes_failure", reason="wrong code")
    task(fx, 9, lesson_ids=[lesson.id], outcome="done", verified="owner", verdict="useful")
    assert lessons.review(fx.now, lessons.tally(fx.now)) == {"activated": [], "retired": [lesson.id]}
    retired = lessons.get(lesson.id)
    assert retired.status == "retired" and retired.closed_reason == "1 wins in 5 verified uses"
    assert lessons.all() == []
    [note] = [row for row in fx.store.intentions(kind=["note"], limit=50) if row.type == "lesson_retired"]
    assert json.loads(note.lesson_ids) == [lesson.id] and "1 wins in 5 verified uses" in note.decision_reason
    # Four verified uses are not enough to judge, and a 40% win rate stays; under it, it goes.
    second = admit(fx, fields={**FIELDS, "signature": "topic:slot-labels"})
    for n in range(3):
        task(fx, 20 + n, lesson_ids=[second.id], outcome="failed", verified="hermes_failure", reason="wrong")
    task(fx, 30, lesson_ids=[second.id], outcome="done", verified="owner", verdict="useful")
    assert lessons.review(fx.now, lessons.tally(fx.now))["retired"] == []          # 1 of 4
    task(fx, 31, lesson_ids=[second.id], outcome="done", verified="owner", verdict="useful")
    assert lessons.review(fx.now, lessons.tally(fx.now))["retired"] == []          # 2 of 5 = 0.4
    task(fx, 32, lesson_ids=[second.id], outcome="failed", verified="owner", verdict="wrong")
    assert lessons.review(fx.now, lessons.tally(fx.now))["retired"] == [second.id]  # 2 of 6
    assert lessons.get(second.id).status == "retired"


def test_a_candidate_becomes_active_after_a_verified_win_in_its_class(fx):
    lessons = fx.mind.lessons
    candidate = admit(fx, status="candidate", verified="none", origin="reflector", lineage=())
    assert candidate.status == "candidate" and lessons.all() == [candidate]
    task(fx, 1, lesson_ids=[candidate.id], outcome="done", verified="check",
         check={"kind": "result_field", "field": "finding"}, passed=True)          # not a verified win
    assert lessons.review(fx.now, lessons.tally(fx.now)) == {"activated": [], "retired": []}
    task(fx, 2, lesson_ids=[candidate.id], outcome="done", verified="owner", verdict="useful")
    assert lessons.review(fx.now, lessons.tally(fx.now)) == {"activated": [candidate.id], "retired": []}
    assert lessons.get(candidate.id).status == "active"
    assert any(row.type == "lesson_activated" for row in fx.store.intentions(kind=["note"], limit=50))
    # Reviewing again changes nothing.
    assert lessons.review(fx.now, lessons.tally(fx.now)) == {"activated": [], "retired": []}


def test_lesson_stats_count_by_status_source_and_use(fx):
    lessons = fx.mind.lessons
    owner_turn(fx, "verdict-1", "Verdict: the code starts with the channel letter.")
    active = admit(fx)
    admit(fx, fields={**FIELDS, "signature": "topic:other"}, status="candidate", verified="none",
          origin="reflector", lineage=())
    task(fx, 1, lesson_ids=[active.id], outcome="done", verified="owner", verdict="useful")
    stats = lessons.stats(fx.now)
    assert stats["active"] == 1 and stats["candidate"] == 1 and stats["retired"] == 0
    assert stats["admitted_by_source"] == {"owner": 1, "none": 1}
    assert stats["uses"] == 1 and stats["wins"] == 1 and stats["losses"] == 0 and stats["use_rate"] == 1.0
    assert lessons_module.TALLY_WINDOW == timedelta(days=90)
