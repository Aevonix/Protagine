"""Mastery investigations as reflectors (architecture 4.8 item 3, build plan M9).

A failure cluster raises the mastery drive; with lessons on, its investigation is a reflector: an
internal kanban task whose body asks for at most three lesson operations as JSON at the end of its
report. The mind validates them when the report comes back and admits what passes as ``candidate``
lessons of the investigated class, which a verified win in that class later activates. A reflector
never supersedes or retires an active lesson.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from protagine.api.routers import mind as mind_router
from protagine.mind import lessons as lessons_module
from protagine.mind.drives import slug
from protagine.mind.rank import Candidate
from test_mind_drives_loop import DeliberationRouter, idle
from test_mind_loop import OWNER, Fixture

SIGNATURE = "research:tide-tables"
ROOMY = {"breaker": {"failures": 50}, "budgets": {"tasks_per_hour": 50, "concurrent_tasks": 50}}


@pytest.fixture
def fx(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fixture = Fixture(tmp_path, config=ROOMY)
    fixture.mind.router = DeliberationRouter()
    yield fixture
    mind_router.set_mind(None)
    fixture.store.close()


def fail_twice(fx):
    failures = []
    for index in range(2):
        row, _ = fx.store.create_intention(kind="task", type="research", title="Research: tide tables", drive="curiosity",
                                           cls="internal", decision="act", decision_reason="r", status="dispatched",
                                           dedup_key=f"research:tide-tables:w{index}", hermes_kind="kanban",
                                           context={"topic": "tide tables"}, created_at=fx.now - timedelta(days=1))
        fx.mind.outcomes.record(row.id, status="failed", summary="no sources reachable", error="timeout")
        failures.append(row.id)
    return failures


async def reflector(fx):
    fail_twice(fx)
    summary, = await idle(fx)
    [formed] = [item for item in summary["formed"] if item["type"] == "mastery_investigation"]
    row = fx.store.get(formed["id"])
    fx.mind.bound(row.id, "kanban:r-1")
    return row


def report(fx, row, ops, *, prose="I read the attempts: the archive times out at midday.", structured=False):
    if structured:
        return fx.mind.outcomes.record(row.id, status="done", hermes_ref="kanban:r-1", summary=prose,
                                       result={"finding": prose, "lesson_ops": ops})
    text = prose if ops is None else f"{prose}\n\n{json.dumps({'lesson_ops': ops})}"
    return fx.mind.outcomes.record(row.id, status="done", hermes_ref="kanban:r-1", summary=text)


def add(**extra):
    return {"op": "add", "kind": "strategy", "title": "Fetch tide tables early",
            "when_to_use": "tide tables are researched", "content": "Use the harbour mirror before noon.", **extra}


async def test_a_failure_cluster_dispatches_a_reflector_that_asks_for_delta_operations(fx):
    row = await reflector(fx)
    assert row.type == "mastery_investigation" and row.source_type == "failure_signature"
    assert row.source_id == SIGNATURE
    assert row.context["reflector"]["signature"] == SIGNATURE
    assert len(row.context["reflector"]["evidence"]) == 2
    assert json.loads(row.success_check) == {"kind": "result_field", "field": "lesson_ops"}
    body = row.context["body"]
    assert 'one JSON object {"lesson_ops": [...]}' in body and "at most 3 operations" in body
    assert body.endswith("the lessons you may change: none.") and "request" not in row.context["reflector"]


async def test_reflector_operations_are_validated_before_they_are_applied_and_start_as_candidate(fx):
    row = await reflector(fx)
    failures = row.context["reflector"]["evidence"]
    done = report(fx, row, [
        add(),
        add(kind="habit"),                                     # no such kind
        add(content=""),                                       # nothing to apply
        add(title="x" * 200),                                  # over the bound
        {"op": "merge"},                                       # no such operation
    ])
    assert done.outcome == "done"
    [lesson] = fx.mind.lessons.all()
    assert lesson.status == "candidate" and lesson.origin == "reflector" and lesson.verified == "none"
    assert lesson.signature == SIGNATURE and lesson.title == "Fetch tide tables early"
    assert set(lesson.evidence) == {*(f"intention:{ident}" for ident in failures), f"intention:{row.id}"}
    ops = fx.store.get(row.id).result_metadata["lesson_ops"]
    assert ops["applied"] == [lesson.id] and len(ops["rejected"]) == 4
    assert all(item["why"] for item in ops["rejected"])
    # At most three operations are read; the rest are refused.
    second = await reflector_again(fx)
    report(fx, second, [add(content=f"Variant {n}.", kind="pitfall" if n % 2 else "strategy") for n in range(5)],
           structured=True)
    ops = fx.store.get(second.id).result_metadata["lesson_ops"]
    assert len(ops["applied"]) + len(ops["rejected"]) == 5 and any("at most 3" in item["why"] for item in ops["rejected"])


async def reflector_again(fx):
    """A second investigation of the same class (a new week)."""
    fx.shift(days=8)
    row, _ = fx.store.create_intention(kind="task", type="mastery_investigation", title="Investigate: tide tables",
                                       drive="mastery", cls="internal", decision="act", decision_reason="r",
                                       status="dispatched", dedup_key="mastery:again", hermes_kind="kanban",
                                       context={"topic": "tide tables", "reflector": {"signature": SIGNATURE,
                                                                                      "evidence": []}},
                                       source_type="failure_signature", source_id=SIGNATURE, created_at=fx.now)
    fx.store.update(row.id, hermes_ref="kanban:r-1")
    return fx.store.get(row.id)


async def test_a_reflector_cannot_supersede_or_retire_an_active_lesson(fx):
    active = fx.mind.lessons.admit({**{k: v for k, v in add().items() if k != "op"}, "signature": SIGNATURE},
                                   verified="owner", origin="night", status="active", evidence=[], lineage=[], now=fx.now)
    trial = fx.mind.lessons.admit({"signature": SIGNATURE, "kind": "pitfall", "title": "Avoid the old archive",
                                   "when_to_use": "tide tables are researched", "content": "The old archive is gone."},
                                  verified="none", origin="reflector", status="candidate", evidence=[], lineage=[],
                                  now=fx.now)
    row = await reflector(fx)
    assert f"{trial.id} (candidate, pitfall)" in row.context["body"]
    assert active.id not in row.context["body"].split("the lessons you may change:")[1]
    report(fx, row, [
        {"op": "retire", "lesson_id": active.id, "why": "it failed"},
        {**add(content="Another rule."), "op": "supersede", "lesson_id": active.id},
        add(content="A second strategy for the class."),              # the class has an active strategy
    ])
    assert fx.mind.lessons.get(active.id).status == "active"
    ops = fx.store.get(row.id).result_metadata["lesson_ops"]
    assert ops["applied"] == [] and len(ops["rejected"]) == 3
    # A candidate of the class is the reflector's to change.
    again = await reflector_again(fx)
    report(fx, again, [{"op": "retire", "lesson_id": trial.id, "why": "the archive is back"}])
    assert fx.mind.lessons.get(trial.id).status == "retired"
    assert fx.store.get(again.id).result_metadata["lesson_ops"]["applied"] == [trial.id]


async def test_a_reflector_report_without_operations_admits_nothing_and_says_so(fx):
    row = await reflector(fx)
    report(fx, row, None, prose="I looked at the attempts and found nothing to change.")
    assert fx.mind.lessons.all(include_closed=True) == []
    ops = fx.store.get(row.id).result_metadata["lesson_ops"]
    assert ops["applied"] == [] and ops["rejected"] == [] and ops["missing"] is True
    from contextlib import closing
    with closing(fx.ledger._connect()) as conn:
        texts = [row_["messages_json"] for row_ in conn.execute(
            "SELECT messages_json FROM turn_sources WHERE turn_id=?", (f"mind:{row.id}:lesson_ops",))]
    assert texts and "returned no lesson operations" in texts[0]


async def test_a_candidate_from_a_reflector_activates_after_a_verified_win_in_its_class(fx):
    row = await reflector(fx)
    report(fx, row, [add()])
    [lesson] = fx.mind.lessons.all()
    assert lesson.status == "candidate"
    # The next task of the class carries it.
    task = await fx.mind._form(Candidate(type="research", drive="curiosity", kind="task", title="Research: tide tables",
                                         dedup_key="research:tide-tables:next", salience=0.9, cost=0.1,
                                         text="Find the tide tables for the harbour.", topic="tide tables",
                                         concern="tide tables", evidence=[]), 0.9, fx.now)
    assert json.loads(task.lesson_ids) == [lesson.id] and f"[lesson {lesson.id}, strategy]" in task.context["body"]
    fx.mind.bound(task.id, "kanban:t-1")
    fx.mind.outcomes.record(task.id, status="done", hermes_ref="kanban:t-1", summary="finding: tables for May")
    fx.mind.rate(task.id, "useful")
    assert fx.mind.lessons.review(fx.now) == {"activated": [lesson.id], "retired": []}
    assert fx.mind.lessons.get(lesson.id).status == "active"


async def test_with_lessons_off_the_investigation_is_the_m8_one(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fixture = Fixture(tmp_path, config={**ROOMY, "faculties": {"lessons": False}})
    fixture.mind.router = DeliberationRouter()
    try:
        fail_twice(fixture)
        summary, = await idle(fixture)
        [formed] = [item for item in summary["formed"] if item["type"] == "mastery_investigation"]
        row = fixture.store.get(formed["id"])
        assert "reflector" not in row.context and "lesson_ops" not in row.context["body"]
        assert json.loads(row.success_check) == {"kind": "result_field", "field": "finding"}
        fixture.mind.bound(row.id, "kanban:r-1")
        report(fixture, row, [add()])
        assert fixture.mind.lessons.all(include_closed=True) == []
        assert "lesson_ops" not in (fixture.store.get(row.id).result_metadata or {})
    finally:
        fixture.store.close()


def test_the_reflector_format_is_fixed_text():
    assert "{\"lesson_ops\": [...]}" in lessons_module.REFLECTOR_FORMAT
    assert lessons_module.REFLECTOR_OPS == 3 and slug("tide tables") == "tide-tables"
