"""Agent-owned goals: a desire the agent pursues across days (architecture 4.5).

A goal is one ``kind='goal'`` row in the intentions table: a description, a
``success_check`` the mind can evaluate over its own state, a task budget
and a horizon. Curiosity and mastery may adopt one; at most
``budgets.open_goals`` are open at once (a third is not adopted while two
are open). Each tick may form the goal's next step as an ordinary ``task``
intention with kanban ``goal_mode``. The goal closes when its check passes
(satisfied), when its task budget is spent, or when its horizon passes
(expired). Authority applies to every step as usual.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

from protagine.initiatives.models import MIND_ACTIVE_STATUSES, StoredInitiative

from .drives import slug
from .rank import Candidate

logger = logging.getLogger(__name__)

GOAL_TYPE = "goal"
STEP_TYPE = "goal_step"
OPEN_STATUSES = ("approved", "asked", "proposed")   # a goal owns no Hermes object; approved means open
DEFAULT_HORIZON_DAYS = 7
DEFAULT_TASKS = 4
DEFAULT_MAX_TURNS = 12


def goal_context(row: StoredInitiative) -> Dict[str, Any]:
    return row.context if isinstance(row.context, dict) else {}


class Goals:
    def __init__(self, store: Any, *, budgets: Any, clock: Callable[[], datetime] | None = None,
                 enabled: bool = True) -> None:
        self.store = store
        self.budgets = budgets
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.enabled = enabled

    # -- reads -----------------------------------------------------------------------

    def open(self) -> List[StoredInitiative]:
        rows = self.store.intentions(status=list(OPEN_STATUSES), kind=[GOAL_TYPE], limit=200)
        return sorted(rows, key=lambda row: row.created_at)

    def approved(self) -> List[StoredInitiative]:
        """The open goals that may own work: one still asked or deferred has no steps yet."""
        return [goal for goal in self.open() if goal.status == "approved"]

    def may_adopt(self) -> bool:
        return bool(self.enabled) and len(self.open()) < int(getattr(self.budgets, "open_goals", 2) or 0)

    def steps(self, goal: StoredInitiative) -> List[StoredInitiative]:
        rows = [row for row in self.store.intentions(kind=["task"], limit=500)
                if row.parent_goal_id == goal.id]
        return sorted(rows, key=lambda row: row.created_at)

    def running_step(self, goal: StoredInitiative) -> Optional[StoredInitiative]:
        for row in self.steps(goal):
            if row.status in MIND_ACTIVE_STATUSES:
                return row
        return None

    def steps_done(self, goal: StoredInitiative) -> List[StoredInitiative]:
        return [row for row in self.steps(goal) if row.outcome == "done"]

    def summaries(self, goal: StoredInitiative) -> List[str]:
        return [str(row.result or row.description)[:300] for row in self.steps_done(goal)]

    def budget_spent(self, goal: StoredInitiative) -> bool:
        limit = int(goal_context(goal).get("tasks") or DEFAULT_TASKS)
        used = [row for row in self.steps(goal) if row.status != "dropped"]
        return len(used) >= limit

    # -- adoption ---------------------------------------------------------------------

    @staticmethod
    def key_for(candidate: Candidate) -> str:
        """The goal key of a concern's topic: one goal per topic, whatever became of an earlier one."""
        return f"goal:{slug(candidate.topic or candidate.concern or candidate.title)}"

    def taken(self, candidate: Candidate) -> bool:
        return self.store.get_by_dedup_key(self.key_for(candidate)) is not None

    def candidate(self, candidate: Candidate) -> Optional[Candidate]:
        """Turn a deliberated goal proposal into the goal intention's candidate, or None when the
        budget is full or a goal for the topic already exists (the tick does the one task instead)."""
        if (candidate.kind != "goal" or not isinstance(candidate.goal, dict) or not self.may_adopt()
                or self.taken(candidate)):
            return None
        proposal = candidate.goal
        horizon_days = int(proposal.get("horizon_days") or DEFAULT_HORIZON_DAYS)
        tasks = int(proposal.get("tasks") or DEFAULT_TASKS)
        topic = candidate.topic or candidate.concern or candidate.title
        return Candidate(
            type=GOAL_TYPE, drive=candidate.drive, kind="goal", title=f"Goal: {candidate.title}"[:160],
            dedup_key=self.key_for(candidate), salience=candidate.salience, cost=candidate.cost,
            text=str(proposal.get("description") or candidate.text)[:2000], rationale=candidate.rationale,
            evidence=list(candidate.evidence), concern=candidate.concern, topic=topic,
            success_check=proposal.get("success_check") or {"kind": "steps_done", "count": 2},
            source_type=candidate.source_type, source_id=candidate.source_id, concern_kind="goal",
            goal={"description": str(proposal.get("description") or candidate.text)[:2000],
                  "horizon_days": horizon_days, "tasks": tasks, "topic": topic},
            due_at=self.clock() + timedelta(days=horizon_days), cost_tokens=candidate.cost_tokens)

    # -- steps ------------------------------------------------------------------------

    def next_step(self, goal: StoredInitiative) -> Optional[Candidate]:
        """The next step as an open-ended task candidate, or None while one runs or the budget is spent."""
        if self.running_step(goal) is not None or self.budget_spent(goal):
            return None
        context = goal_context(goal)
        done = self.steps_done(goal)
        ordinal = len([row for row in self.steps(goal) if row.status != "dropped"]) + 1
        topic = str(context.get("topic") or goal.description)
        return Candidate(
            type=STEP_TYPE, drive=goal.drive or "curiosity", kind="task",
            title=f"Step {ordinal} of goal: {topic}"[:160], dedup_key=f"goal:{goal.id}:step:{ordinal}",
            salience=max(0.8, float(goal.priority or 0.5)), cost=0.2, rationale=f"step {ordinal} of an adopted goal",
            evidence=[f"goal:{goal.id}", *[f"step done: {row.result or row.description}"[:200] for row in done[-3:]]],
            concern=str(context.get("description") or goal.description)[:300], topic=topic, open_ended=True,
            concern_kind="goal_step", parent_goal_id=goal.id, source_type="goal", source_id=goal.id)

    # -- closing ----------------------------------------------------------------------

    def check(self, goal: StoredInitiative, *, evaluate: Callable[..., Optional[bool]]) -> Optional[bool]:
        """The goal's success check over mind-observable state; None when it cannot run."""
        steps_done = self.steps_done(goal)
        latest = steps_done[-1] if steps_done else None
        return evaluate(goal.success_check, summary=str(latest.result or "") if latest else "",
                        result=(latest.result_metadata or {}).get("result") if latest else None,
                        steps_done=len(steps_done))

    def due(self, now: datetime, *, evaluate: Callable[..., Optional[bool]]) -> List[Dict[str, Any]]:
        """Goals to close now: ``satisfied`` (check passed), ``expired`` (horizon or budget)."""
        closing: List[Dict[str, Any]] = []
        for goal in self.open():
            passed = self.check(goal, evaluate=evaluate)
            if passed is True:
                closing.append({"goal": goal, "outcome": "done", "why": "the success check passed"})
                continue
            horizon = goal.due_at or goal.expires_at
            if horizon is not None and horizon <= now:
                closing.append({"goal": goal, "outcome": "expired", "why": "the horizon passed"})
            elif self.budget_spent(goal) and self.running_step(goal) is None:
                closing.append({"goal": goal, "outcome": "expired", "why": "the task budget is spent"})
        return closing

    def render(self, goal: StoredInitiative) -> Dict[str, Any]:
        context = goal_context(goal)
        steps = self.steps(goal)
        return {
            "id": goal.id, "title": goal.description, "drive": goal.drive, "status": goal.status,
            "topic": context.get("topic"), "description": context.get("description"),
            "steps_done": len([row for row in steps if row.outcome == "done"]),
            "steps": len([row for row in steps if row.status != "dropped"]),
            "tasks": int(context.get("tasks") or DEFAULT_TASKS),
            "horizon": (goal.due_at or goal.expires_at).isoformat() if (goal.due_at or goal.expires_at) else None,
            "success_check": goal.success_check, "outcome": goal.outcome, "created_at": goal.created_at.isoformat(),
        }


def goal_lines(goals: Iterable[Dict[str, Any]], *, limit: int = 2) -> List[str]:
    lines = []
    for goal in list(goals)[:limit]:
        lines.append(f"{goal['title']} ({goal['steps_done']}/{goal['tasks']} steps, until {str(goal['horizon'])[:10]})")
    return lines


__all__ = ["DEFAULT_HORIZON_DAYS", "DEFAULT_MAX_TURNS", "DEFAULT_TASKS", "GOAL_TYPE", "Goals", "OPEN_STATUSES",
           "STEP_TYPE", "goal_context", "goal_lines"]
