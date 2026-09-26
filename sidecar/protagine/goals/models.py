"""Persistent goal records and saved subtask history.

These types describe stored state; they do not provide an execution planner.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from protagine.util.temporal import now_utc


class GoalStatus(str, Enum):
    """Goal lifecycle states."""
    PROPOSED   = "proposed"    # Inferred or suggested; awaiting user acceptance
    ACCEPTED   = "accepted"    # Acceptance recorded
    ACTIVE     = "active"      # Marked active
    BLOCKED    = "blocked"     # Cannot proceed; waiting on external factor
    COMPLETED  = "completed"   # Completion recorded
    ABANDONED  = "abandoned"   # Given up; reason recorded


class GoalSource(str, Enum):
    """How the goal was created."""
    EXPLICIT   = "explicit"    # User stated the goal directly
    INFERRED   = "inferred"    # Detected from conversation context
    RECURRING  = "recurring"   # Generated from a recurring schedule
    DELEGATED  = "delegated"   # Sent by another Protagine agent via federation


class GoalPriority(int, Enum):
    """Goal scheduling priority. Higher = more urgent."""
    BACKGROUND = 0
    LOW        = 10
    NORMAL     = 50
    HIGH       = 80
    CRITICAL   = 100


@dataclass
class GoalOutcome:
    """Describes the desired end state of a completed goal."""
    description: str
    success_criteria: List[str] = field(default_factory=list)
    measurable: bool = False
    target_value: Optional[float] = None
    target_unit: Optional[str] = None


@dataclass
class Goal:
    """A user-level objective tracked by the Goal Engine.

    Attributes:
        goal_id:         Globally unique identifier.
        title:           Short human-readable label (≤ 80 chars).
        description:     Full description of the objective.
        source:          How this goal was created.
        status:          Current lifecycle state.
        priority:        Scheduling priority score.
        outcome:         Desired end state description.
        deadline:        Optional absolute deadline.
        parent_goal_id:  If this is a sub-goal, the parent's ID.
        tags:            Arbitrary metadata for filtering and grouping.
        context:         Conversation or event context that generated this goal.
        created_at:      When the goal was created.
        updated_at:      When the goal was last modified.
        accepted_at:     When the user accepted the goal.
        completed_at:    When the goal reached COMPLETED.
        abandoned_at:    When the goal was abandoned.
        abandon_reason:  Why the goal was abandoned.
        replan_count:    Number of times the DAG has been replanned.
        estimated_hours: Current completion time estimate in hours.
        progress_pct:    Fraction of subtasks completed (0.0–1.0).
    """
    goal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    title: str = ""
    description: str = ""
    source: GoalSource = GoalSource.EXPLICIT
    status: GoalStatus = GoalStatus.PROPOSED
    priority: GoalPriority = GoalPriority.NORMAL
    outcome: Optional[GoalOutcome] = None
    deadline: Optional[datetime] = None
    parent_goal_id: Optional[str] = None
    tags: Dict[str, str] = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: now_utc())
    updated_at: datetime = field(default_factory=lambda: now_utc())
    accepted_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    abandoned_at: Optional[datetime] = None
    abandon_reason: Optional[str] = None
    replan_count: int = 0
    estimated_hours: Optional[float] = None
    progress_pct: float = 0.0

    # Initiative management fields (v0.7.10)
    last_initiative_at: Optional[datetime] = None
    snoozed_until: Optional[datetime] = None
    snooze_count: int = 0
    dismissal_reason: Optional[str] = None

    def is_terminal(self) -> bool:
        # Compare by string value so the check is robust if GoalStatus is patched
        # in a test environment (e.g. pytest-xdist workers that share sys.modules).
        status_val = self.status.value if hasattr(self.status, "value") else str(self.status)
        return status_val in ("completed", "abandoned")

    def is_overdue(self) -> bool:
        if self.deadline is None:
            return False
        return now_utc() > self.deadline

    def age_hours(self) -> float:
        return (now_utc() - self.created_at).total_seconds() / 3600.0


@dataclass
class GoalTransitionRecord:
    """An audit record for a goal state transition."""
    goal_id: str
    from_status: str
    to_status: str
    trigger: str
    created_at: datetime
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GoalSummary:
    """Human-readable summary for the briefing system."""
    goal_id: str
    title: str
    status: str
    priority: int
    progress_pct: float
    estimated_hours: Optional[float]
    deadline: Optional[datetime]
    subtask_count: int
    completed_subtasks: int
    is_overdue: bool
    replan_count: int
