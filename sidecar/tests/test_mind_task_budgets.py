"""Task budgets by kind, a proposer that knows the worker's budget, and a timeout that is not a wrong act.

The live deployment's first autonomous task was a curiosity research brief with four steps. It got
the one flat budget every task got (one run of 600 s, one attempt), timed out, and was blocked; two
more failures like it within 24 hours would have demoted the mind for 72 hours.
"""
from __future__ import annotations

import pytest

from protagine.config import ConfigError, DEFAULTS, validate
from protagine.mind.authority import Budgets, Policy
from protagine.mind.concerns import Concern
from protagine.mind.deliberate import GOAL_TASKS, RESPONSE_SCHEMA, SYSTEM, apply_proposal, build_prompt
from protagine.mind.rank import Candidate
from test_mind_drives_loop import DeliberationRouter, fx, idle, research  # noqa: F401  (fixture)

RESEARCH_TYPES = ("research", "question", "mastery_investigation", "goal_step")


# ---------------------------------------------------------------------------
# Budgets by kind
# ---------------------------------------------------------------------------

def test_research_shaped_work_gets_a_longer_run_and_a_retry_and_the_rest_keep_the_flat_budget():
    budgets = Budgets()
    for kind in RESEARCH_TYPES:
        assert budgets.for_task(kind) == (1800, 2), kind
    for kind in ("commitment_overdue", "reply_wait", "upkeep_task", "something_new"):
        assert budgets.for_task(kind) == (600, 1), kind


def test_protagine_yaml_overrides_one_kind_and_the_flat_default():
    policy = Policy.from_config({"budgets": {"task_max_runtime_s": 900,
                                             "task_types": {"research": {"max_runtime_s": 2400}}}})
    assert policy.budgets.for_task("research") == (2400, 2)            # the retry default stays
    assert policy.budgets.for_task("question") == (1800, 2)            # the other kinds keep theirs
    assert policy.budgets.for_task("upkeep_task") == (900, 1)
    import copy
    data = copy.deepcopy(DEFAULTS)
    assert validate(data)["mind"]["budgets"]["task_types"]["research"] == {"max_runtime_s": 1800, "max_retries": 2}
    data = copy.deepcopy(DEFAULTS)
    data["mind"]["budgets"]["task_types"]["research"]["max_runtime_s"] = 0
    with pytest.raises(ConfigError, match="task_types.research.max_runtime_s"):
        validate(data)
    data = copy.deepcopy(DEFAULTS)
    data["mind"]["budgets"]["task_types"] = {"research": 1800}
    with pytest.raises(ConfigError, match="task_types.research"):
        validate(data)


async def test_a_research_task_is_dispatched_with_its_kinds_budget(fx):
    fx.mind.router = DeliberationRouter({"local history": research("local history")})
    fx.mind.add_interest("local history")
    await idle(fx)
    item, = fx.mind.dispatch()
    assert (item["type"], item["max_runtime_seconds"], item["max_retries"]) == ("research", 1800, 2)


def _row(fx, type, context=None, **fields):
    row, created = fx.store.create_intention(
        kind="task", type=type, title=f"{type} task", drive=fields.pop("drive", "upkeep"), cls="internal",
        decision="act", decision_reason="test", status="approved", dedup_key=f"{type}:{len(fx.store.intentions())}",
        context=context or {"body": "Do the thing."}, hermes_kind="none", created_at=fx.now, **fields)
    assert created == "created"
    return row


def test_dispatch_reads_the_budget_of_a_rows_kind_when_its_context_has_none(fx):
    _row(fx, "upkeep_task")
    _row(fx, "mastery_investigation", drive="mastery")
    _row(fx, "research", context={"body": "Look.", "max_runtime_seconds": 1200, "max_retries": 0}, drive="curiosity")
    budgets = {item["type"]: (item["max_runtime_seconds"], item["max_retries"]) for item in fx.mind.dispatch()}
    assert budgets == {"upkeep_task": (600, 1), "mastery_investigation": (1800, 2),
                       "research": (1200, 0)}                        # what the row was formed with, 0 included


# ---------------------------------------------------------------------------
# The proposer knows the budget; multi-step work becomes a goal
# ---------------------------------------------------------------------------

def _concern():
    return Concern(id="c-1", drive="curiosity", kind="interest", summary="the history of the old mill",
                   dedup_key="interest:old-mill", salience=0.8)


def _research(**fields):
    return Candidate(type=fields.pop("type", "research"), drive="curiosity", kind="task", title="Research the mill",
                     dedup_key="research:old-mill", open_ended=True, topic="the old mill", **fields)


def test_the_proposer_is_told_one_run_of_its_kinds_minutes_and_asked_for_one_bounded_deliverable():
    assert "one bounded first deliverable" in SYSTEM and "steps" in SYSTEM
    assert "steps" in RESPONSE_SCHEMA["schema"]["properties"]
    prompt = build_prompt(_concern(), _research(), open_goals=0, may_adopt_goal=True, budgets=Budgets())
    assert "one run of at most 30 minutes" in prompt
    assert f"at most {GOAL_TASKS} steps" in prompt
    step = build_prompt(_concern(), _research(type="goal_step", parent_goal_id="g-1"), open_goals=1,
                        may_adopt_goal=False, budgets=Budgets(task_types={"goal_step": {"max_runtime_s": 900}}))
    assert "one run of at most 15 minutes" in step


def test_multi_step_work_becomes_a_goal_of_at_most_goal_tasks_steps():
    steps = ["Collect the mill's records", "Read the parish history", "Compare the dates", "Write the brief",
             "Check the brief", "Send it"]
    proposal = {"kind": "task", "title": "Mill brief", "body": "Collect the mill's records and report the sources.",
                "steps": steps}
    shaped = apply_proposal(_research(), _concern(), proposal, budgets=Budgets(), may_adopt_goal=True)
    assert shaped.kind == "goal" and shaped.goal["tasks"] == GOAL_TASKS
    assert shaped.goal["success_check"] == {"kind": "steps_done", "count": GOAL_TASKS}
    assert "Read the parish history" in shaped.goal["description"] and "Send it" not in shaped.goal["description"]
    assert "Collect the mill's records and report the sources." in shaped.text   # the first run, bounded
    assert "Write the brief" not in shaped.text
    # No room for a goal: the first deliverable alone, as one task.
    task = apply_proposal(_research(), _concern(), proposal, budgets=Budgets(), may_adopt_goal=False)
    assert task.kind == "task" and task.goal is None and "Write the brief" not in task.text
    # A goal's own step is one run: never a goal inside a goal.
    step = apply_proposal(_research(type="goal_step", parent_goal_id="g-1"), _concern(), proposal,
                          budgets=Budgets(), may_adopt_goal=True)
    assert step.kind == "task" and "Write the brief" not in step.text
    # A goal the model asked for keeps the cap too.
    wide = apply_proposal(_research(), _concern(), {**proposal, "kind": "goal", "goal": {"tasks": 9}},
                          budgets=Budgets(), may_adopt_goal=True)
    assert wide.goal["tasks"] == GOAL_TASKS


async def test_a_four_step_research_brief_is_adopted_as_a_goal_not_run_as_one_task(fx):
    brief = {"kind": "task", "title": "Research brief on local history",
             "body": "Find three primary sources on the founding and report them as finding: <list>.",
             "steps": ["Find three primary sources", "Read and note the founding dates", "Resolve conflicts",
                       "Write the brief"]}
    router = DeliberationRouter({"local history": brief})
    fx.mind.router = router
    fx.mind.add_interest("local history")
    first, = await idle(fx)
    assert [item["kind"] for item in first["formed"]] == ["goal"]
    assert "one run of at most 30 minutes" in router.prompts[0]
    goal = fx.store.get(first["formed"][0]["id"])
    assert goal.context["tasks"] == 4 and "Resolve conflicts" in goal.context["description"]


# ---------------------------------------------------------------------------
# A timeout fails the task; it trips the breaker only when a retry made no progress
# ---------------------------------------------------------------------------

TIMEOUT = {"outcome": "timed_out", "status": "timed_out", "profile": "protagine-act"}


def _dispatched(fx, number):
    row = _row(fx, "research", drive="curiosity", context={"body": f"Research topic {number}."})
    fx.mind.bound(row.id, f"kanban:t{number}")
    return row


def test_a_timed_out_run_fails_its_task_but_is_not_counted_toward_the_breaker(fx):
    for number in range(3):
        row = _dispatched(fx, number)
        settled = fx.mind.outcomes.record(row.id, status="blocked", outcome="failed", final=True,
                                          hermes_ref=f"kanban:t{number}", error="elapsed 1801s > limit 1800s",
                                          run=dict(TIMEOUT, id=number))
        assert settled.outcome == "failed" and settled.verified == "hermes_failure"
        assert settled.result_metadata["breaker"] == {"counted": False, "reason": "timed out on its only run"}
        fx.shift(hours=1)
    assert fx.mind.authority.breaker_state("internal")["tripped"] is False


def _retried(fx, number, *, final_summary=""):
    row = _dispatched(fx, number)
    fx.mind.outcomes.record(row.id, status="ready", outcome="failed", final=False, hermes_ref=f"kanban:t{number}",
                            error="elapsed 1801s > limit 1800s", run=dict(TIMEOUT, id=10 * number))
    fx.shift(minutes=31)
    return fx.mind.outcomes.record(row.id, status="blocked", outcome="failed", final=True,
                                   hermes_ref=f"kanban:t{number}", summary=final_summary,
                                   error="elapsed 1801s > limit 1800s", run=dict(TIMEOUT, id=10 * number + 1))


def test_a_retry_that_timed_out_again_with_nothing_to_show_counts(fx):
    for number in range(3):
        settled = _retried(fx, number)
        assert settled.result_metadata["breaker"]["counted"] is True
        fx.shift(hours=1)
    assert fx.mind.authority.breaker_state("internal")["tripped"] is True


def test_a_retry_that_made_progress_before_timing_out_is_not_counted(fx):
    for number in range(3):
        settled = _retried(fx, number, final_summary="Found two of the three sources; the parish record is next.")
        assert settled.result_metadata["breaker"] == {"counted": False,
                                                      "reason": "timed out after progress on its retry"}
        fx.shift(hours=1)
    assert fx.mind.authority.breaker_state("internal")["tripped"] is False


def test_other_failures_still_count(fx):
    for number in range(3):
        row = _dispatched(fx, number)
        fx.mind.outcomes.record(row.id, status="blocked", outcome="failed", final=True, hermes_ref=f"kanban:t{number}",
                                error="worker crashed", run={"outcome": "crashed", "status": "crashed", "id": number})
        fx.shift(hours=1)
    assert fx.mind.authority.breaker_state("internal")["tripped"] is True
