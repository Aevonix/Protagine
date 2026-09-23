"""GoalStore — SQLite-backed persistence for Goal records.

Uses synchronous sqlite3 for simplicity (goals are not high-throughput).
WAL mode enabled for safe concurrent reads.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

from protagine import get_state_dir

from protagine.goals.models import (
    Goal,
    GoalOutcome,
    GoalPriority,
    GoalSource,
    GoalStatus,
    GoalTransitionRecord,
)

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

_goal_store_instance: Optional["GoalStore"] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if s is None:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


class GoalNotFoundError(KeyError):
    """Raised when a goal_id is not found in the store."""


class GoalStore:
    """Persistent goal records and their saved DAG history.

    This store does not plan, dispatch or execute work.

    Thread-safe for single-process use (sqlite3 serialised mode).
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        # ":memory:" for tests; actual path for production
        self._db_path = db_path or ":memory:"
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        conn = self._get_conn()
        schema = _SCHEMA_PATH.read_text()
        conn.executescript(schema)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,
                detect_types=sqlite3.PARSE_DECLTYPES,
            )
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    @classmethod
    def get_instance(cls) -> "GoalStore":
        """Return the process-wide singleton backed by ~/.protagine/data/goals.db."""
        global _goal_store_instance
        if _goal_store_instance is None:
            protagine_home = get_state_dir()
            _goal_store_instance = cls(str(protagine_home / "goals.db"))
        return _goal_store_instance

    def list(
        self,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> dict:
        """Return a paginated dict of goals for the API router."""
        offset = 0
        if cursor:
            try:
                offset = int(cursor)
            except ValueError:
                pass

        goal_status = None
        if status:
            try:
                goal_status = GoalStatus(status)
            except ValueError:
                pass

        goals = self.list_goals(status=goal_status, limit=limit + 1, offset=offset)

        if priority:
            goals = [g for g in goals if (g.priority.value if hasattr(g.priority, 'value') else g.priority) == priority]

        has_more = len(goals) > limit
        if has_more:
            goals = goals[:limit]

        items = [
            {
                "goal_id": g.goal_id,
                "title": g.title,
                "description": g.description,
                "status": g.status.value if hasattr(g.status, 'value') else g.status,
                "priority": g.priority.value if hasattr(g.priority, 'value') else g.priority,
                "source": g.source.value if hasattr(g.source, 'value') else g.source,
                "created_at": g.created_at.isoformat(),
                "updated_at": g.updated_at.isoformat(),
                "tags": g.tags,
                "progress_pct": g.progress_pct,
            }
            for g in goals
        ]
        return {
            "data": items,
            "meta": {
                "total": len(items),
                "page_size": limit,
                "has_more": has_more,
                "cursor": str(offset + limit) if has_more else None,
            },
        }

    @contextmanager
    def _tx(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self._get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ── Goal CRUD ──────────────────────────────────────────────────────────────

    def save_goal(self, goal: Goal) -> None:
        """Insert or update a goal record."""
        goal.updated_at = datetime.now(timezone.utc)
        outcome_json = None
        if goal.outcome:
            outcome_json = json.dumps({
                "description": goal.outcome.description,
                "success_criteria": goal.outcome.success_criteria,
                "measurable": goal.outcome.measurable,
                "target_value": goal.outcome.target_value,
                "target_unit": goal.outcome.target_unit,
            })

        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO goals (
                    goal_id, title, description, source, status, priority,
                    outcome_json, deadline, parent_goal_id,
                    tags_json, context_json,
                    created_at, updated_at, accepted_at, completed_at,
                    abandoned_at, abandon_reason,
                    replan_count, estimated_hours, progress_pct,
                    last_initiative_at, snoozed_until, snooze_count, dismissal_reason
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(goal_id) DO UPDATE SET
                    title=excluded.title,
                    description=excluded.description,
                    source=excluded.source,
                    status=excluded.status,
                    priority=excluded.priority,
                    outcome_json=excluded.outcome_json,
                    deadline=excluded.deadline,
                    parent_goal_id=excluded.parent_goal_id,
                    tags_json=excluded.tags_json,
                    context_json=excluded.context_json,
                    updated_at=excluded.updated_at,
                    accepted_at=excluded.accepted_at,
                    completed_at=excluded.completed_at,
                    abandoned_at=excluded.abandoned_at,
                    abandon_reason=excluded.abandon_reason,
                    replan_count=excluded.replan_count,
                    estimated_hours=excluded.estimated_hours,
                    progress_pct=excluded.progress_pct,
                    last_initiative_at=excluded.last_initiative_at,
                    snoozed_until=excluded.snoozed_until,
                    snooze_count=excluded.snooze_count,
                    dismissal_reason=excluded.dismissal_reason
                """,
                (
                    goal.goal_id,
                    goal.title,
                    goal.description,
                    goal.source.value if hasattr(goal.source, 'value') else goal.source,
                    goal.status.value if hasattr(goal.status, 'value') else goal.status,
                    goal.priority.value if hasattr(goal.priority, 'value') else goal.priority,
                    outcome_json,
                    goal.deadline.isoformat() if goal.deadline else None,
                    goal.parent_goal_id,
                    json.dumps(goal.tags),
                    json.dumps(goal.context),
                    goal.created_at.isoformat(),
                    goal.updated_at.isoformat(),
                    goal.accepted_at.isoformat() if goal.accepted_at else None,
                    goal.completed_at.isoformat() if goal.completed_at else None,
                    goal.abandoned_at.isoformat() if goal.abandoned_at else None,
                    goal.abandon_reason,
                    goal.replan_count,
                    goal.estimated_hours,
                    goal.progress_pct,
                    goal.last_initiative_at.isoformat() if goal.last_initiative_at else None,
                    goal.snoozed_until.isoformat() if goal.snoozed_until else None,
                    goal.snooze_count,
                    goal.dismissal_reason,
                ),
            )
        try:
            from protagine.events.broadcaster import emit as _emit
            _emit("goal_update", {
                "goal_id": goal.goal_id,
                "status": goal.status.value if hasattr(goal.status, 'value') else goal.status,
                "progress_pct": goal.progress_pct,
                "title": goal.title,
            })
        except Exception:
            logger.debug("Goal update event broadcast failed", exc_info=True)

    def get_goal(self, goal_id: str) -> Goal:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM goals WHERE goal_id = ?", (goal_id,)
        ).fetchone()
        if row is None:
            raise GoalNotFoundError(f"Goal not found: {goal_id}")
        return self._goal_from_row(row)

    def list_goals(
        self,
        status: Optional[GoalStatus] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Goal]:
        conn = self._get_conn()
        if status:
            rows = conn.execute(
                "SELECT * FROM goals WHERE status = ? ORDER BY priority DESC, created_at DESC LIMIT ? OFFSET ?",
                (status.value if hasattr(status, 'value') else status, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM goals ORDER BY priority DESC, created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [self._goal_from_row(r) for r in rows]

    def delete_goal(self, goal_id: str) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM subtasks WHERE goal_id = ?", (goal_id,))
            conn.execute("DELETE FROM goal_audit_log WHERE goal_id = ?", (goal_id,))
            conn.execute("DELETE FROM goal_dag_versions WHERE goal_id = ?", (goal_id,))
            conn.execute("DELETE FROM goals WHERE goal_id = ?", (goal_id,))

    def _goal_from_row(self, row: sqlite3.Row) -> Goal:
        outcome = None
        if row["outcome_json"]:
            d = json.loads(row["outcome_json"])
            outcome = GoalOutcome(
                description=d.get("description", ""),
                success_criteria=d.get("success_criteria", []),
                measurable=d.get("measurable", False),
                target_value=d.get("target_value"),
                target_unit=d.get("target_unit"),
            )
        return Goal(
            goal_id=row["goal_id"],
            title=row["title"],
            description=row["description"],
            source=GoalSource(row["source"]),
            status=GoalStatus(row["status"]),
            priority=GoalPriority(row["priority"]),
            outcome=outcome,
            deadline=_parse_dt(row["deadline"]),
            parent_goal_id=row["parent_goal_id"],
            tags=json.loads(row["tags_json"] or "{}"),
            context=json.loads(row["context_json"] or "{}"),
            created_at=_parse_dt(row["created_at"]) or datetime.now(timezone.utc),
            updated_at=_parse_dt(row["updated_at"]) or datetime.now(timezone.utc),
            accepted_at=_parse_dt(row["accepted_at"]),
            completed_at=_parse_dt(row["completed_at"]),
            abandoned_at=_parse_dt(row["abandoned_at"]),
            abandon_reason=row["abandon_reason"],
            replan_count=row["replan_count"],
            estimated_hours=row["estimated_hours"],
            progress_pct=row["progress_pct"],
            last_initiative_at=_parse_dt(row["last_initiative_at"]),
            snoozed_until=_parse_dt(row["snoozed_until"]),
            snooze_count=row["snooze_count"],
            dismissal_reason=row["dismissal_reason"],
        )

    # ── Audit Log ──────────────────────────────────────────────────────────────

    def log_transition(
        self,
        goal_id: str,
        from_status: GoalStatus,
        to_status: GoalStatus,
        trigger: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO goal_audit_log (goal_id, from_status, to_status, trigger, created_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    goal_id,
                    from_status.value if hasattr(from_status, 'value') else from_status,
                    to_status.value if hasattr(to_status, 'value') else to_status,
                    trigger,
                    _now_iso(),
                    json.dumps(metadata or {}),
                ),
            )

    def get_audit_trail(self, goal_id: str) -> List[GoalTransitionRecord]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM goal_audit_log WHERE goal_id = ? ORDER BY created_at ASC",
            (goal_id,),
        ).fetchall()
        return [
            GoalTransitionRecord(
                goal_id=r["goal_id"],
                from_status=r["from_status"],
                to_status=r["to_status"],
                trigger=r["trigger"],
                created_at=_parse_dt(r["created_at"]) or datetime.now(timezone.utc),
                metadata=json.loads(r["metadata_json"] or "{}"),
            )
            for r in rows
        ]

    # ── Initiative Task Management (v0.7.10) ──────────────────────────────────

    def complete_task(self, goal_id: str) -> bool:
        """Record reported completion, not proof of an external worker's effect."""
        with self._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status,context_json FROM goals WHERE goal_id=?", (goal_id,)).fetchone()
            if row is None or row['status'] == 'abandoned':
                return False
            if row['status'] == 'completed':
                return True
            now = _now_iso()
            context = json.loads(row['context_json'])
            context.pop('dispatch_unavailable', None)
            context['completion_basis'] = 'reported_completion'
            conn.execute("""UPDATE goals SET status='completed',completed_at=?,updated_at=?,
                progress_pct=1.0,context_json=? WHERE goal_id=?""",
                         (now, now, json.dumps(context), goal_id))
            conn.execute("""INSERT INTO goal_audit_log
                (goal_id,from_status,to_status,trigger,created_at,metadata_json)
                VALUES (?,?,'completed','reported_completion',?,'{}')""",
                         (goal_id, row['status'], now))
            return True

    def snooze_task(self, goal_id: str, hours: int, reason: str = "") -> bool:
        """Postpone attention without changing the goal's lifecycle state."""
        try:
            goal = self.get_goal(goal_id)
        except GoalNotFoundError:
            return False

        hours = min(hours, 168)  # Cap at 1 week

        goal.snooze_count += 1
        goal.snoozed_until = datetime.now(timezone.utc) + timedelta(hours=hours)
        goal.updated_at = datetime.now(timezone.utc)
        self.save_goal(goal)
        return True

    def dismiss_task(self, goal_id: str, reason: str = "stale") -> bool:
        """Dismiss a goal/task as no longer relevant."""
        try:
            goal = self.get_goal(goal_id)
        except GoalNotFoundError:
            return False

        previous_status = goal.status
        goal.status = GoalStatus.ABANDONED
        goal.abandoned_at = datetime.now(timezone.utc)
        goal.abandon_reason = reason
        goal.dismissal_reason = reason
        goal.updated_at = datetime.now(timezone.utc)
        self.save_goal(goal)
        self.log_transition(
            goal_id, previous_status, GoalStatus.ABANDONED,
            trigger="llm_dismiss", metadata={"reason": reason},
        )
        return True

    def get_active_tasks(self, cooldown_hours: float = 12.0) -> List[Goal]:
        """Get goals that should generate initiatives.

        Filters out:
        - Non-pending/proposed/accepted/active goals
        - Snoozed goals (snoozed_until > now)
        - Goals that had an initiative within cooldown period
        """
        now = datetime.now(timezone.utc)
        cooldown_delta = timedelta(hours=cooldown_hours)

        candidates = []
        for status in (GoalStatus.PROPOSED, GoalStatus.ACCEPTED, GoalStatus.ACTIVE, GoalStatus.BLOCKED):
            candidates.extend(self.list_goals(status=status, limit=200))

        active = []
        for goal in candidates:
            # Skip snoozed
            if goal.snoozed_until and goal.snoozed_until > now:
                continue
            # Skip if initiative generated within cooldown
            if goal.last_initiative_at and (now - goal.last_initiative_at) < cooldown_delta:
                continue
            active.append(goal)

        return active

    def mark_initiative_generated(self, goal_id: str) -> bool:
        """Mark that an initiative was just generated for this goal."""
        try:
            goal = self.get_goal(goal_id)
        except GoalNotFoundError:
            return False
        goal.last_initiative_at = datetime.now(timezone.utc)
        goal.updated_at = datetime.now(timezone.utc)
        self.save_goal(goal)
        return True

    def abandon_goal(self, goal_id: str, reason: str) -> Goal:
        """Transition any non-terminal goal to ABANDONED."""
        goal = self.get_goal(goal_id)
        if goal.is_terminal():
            raise ValueError(
                f"Cannot abandon terminal goal {goal_id} (status={goal.status.value})"
            )

        old_status = goal.status
        goal.status = GoalStatus.ABANDONED
        goal.abandoned_at = datetime.now(timezone.utc)
        goal.abandon_reason = reason
        self.save_goal(goal)
        self.log_transition(goal_id, old_status, GoalStatus.ABANDONED, "user_abandoned",
                                   metadata={"reason": reason})
        logger.info("Abandoned goal %s: %s", goal_id, reason)

        return goal

    def block_goal(
        self,
        goal_id: str,
        reason: str,
        condition_type: Optional[str] = None,
        condition_params: Optional[Dict[str, Any]] = None,
    ) -> Goal:
        """Transition an ACTIVE goal to BLOCKED.

        With a ``condition_type`` (email_reply | deployment_health |
        delivery_status | api_response | custom), the goal blocks on an
        EXTERNAL condition: the autonomy loop's condition sweep polls it at
        the type's cadence and unblocks the goal automatically when it's met.
        Without one, the goal stays blocked until something explicitly
        unblocks it."""
        goal = self.get_goal(goal_id)
        if goal.status != GoalStatus.ACTIVE:
            raise ValueError(
                f"Cannot block goal {goal_id} in state {goal.status.value}"
            )
        old_status = goal.status
        goal.status = GoalStatus.BLOCKED
        goal.context["block_reason"] = reason
        if condition_type:
            goal.context["condition_type"] = condition_type
            goal.context["condition_params"] = condition_params or {}
            goal.context.pop("condition_last_check", None)
        self.save_goal(goal)
        self.log_transition(goal_id, old_status, GoalStatus.BLOCKED, "blocked",
                                   metadata={"reason": reason,
                                             **({"condition_type": condition_type}
                                                if condition_type else {})})
        logger.warning("Goal %s blocked: %s%s", goal_id, reason,
                       f" (awaiting {condition_type})" if condition_type else "")
        return goal

    def unblock_goal(self, goal_id: str) -> Goal:
        """Transition a BLOCKED goal back to ACTIVE."""
        goal = self.get_goal(goal_id)
        if goal.status != GoalStatus.BLOCKED:
            raise ValueError(
                f"Cannot unblock goal {goal_id} in state {goal.status.value}"
            )
        old_status = goal.status
        goal.status = GoalStatus.ACTIVE
        goal.context.pop("block_reason", None)
        goal.context.pop("condition_type", None)
        goal.context.pop("condition_params", None)
        goal.context.pop("condition_last_check", None)
        self.save_goal(goal)
        self.log_transition(goal_id, old_status, GoalStatus.ACTIVE, "unblocked")

        logger.info("Unblocked goal %s", goal_id)
        return goal
