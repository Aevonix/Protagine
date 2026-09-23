"""Initiative data models for multi-agent Protagine.

Defines:
- InitiativeStatus enum
- StoredInitiative dataclass (SQLite-persisted initiative)
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class InitiativeStatus(str, Enum):
    """Initiative status values."""

    PENDING = "pending"
    ASSIGNED = "assigned"
    ACKNOWLEDGED = "acknowledged"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"

    def is_active(self) -> bool:
        """Is this initiative still being worked on?"""
        return self in (
            InitiativeStatus.PENDING,
            InitiativeStatus.ASSIGNED,
            InitiativeStatus.ACKNOWLEDGED,
        )

    def can_assign(self) -> bool:
        """Can this initiative be assigned?"""
        return self == InitiativeStatus.PENDING

    def can_complete(self) -> bool:
        """Can this initiative be completed?"""
        return self in (
            InitiativeStatus.ASSIGNED,
            InitiativeStatus.ACKNOWLEDGED,
        )


# Intention lifecycle (architecture 3.3): proposed -> asked -> approved ->
# dispatched -> done | failed | expired | cancelled; messages add
# sending -> sent | uncertain.
MIND_ACTIVE_STATUSES = frozenset({"proposed", "asked", "approved", "dispatched", "sending"})
MIND_TERMINAL_STATUSES = frozenset({"done", "failed", "expired", "cancelled", "sent", "uncertain", "dropped"})
MIND_STATUSES = MIND_ACTIVE_STATUSES | MIND_TERMINAL_STATUSES


@dataclass
class StoredInitiative:
    """Persisted initiative with full tracking.

    This is the SQLite-persisted version of Initiative with
    assignment, retry, and delivery tracking fields.
    """

    # === Core (from Initiative) ===
    id: str
    type: str  # InitiativeType.value
    description: str
    priority: float  # 0.0-1.0
    rationale: str
    action_hint: Optional[str] = None
    entity_id: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # === Deduplication ===
    # dedup_key is the PERIOD key (recurring types append a time bucket, e.g.
    # "relationship:{id}:{week}") — re-arm across periods + idempotency within one.
    # dedup_base is the LOGICAL key without the bucket ("relationship:{id}"); the store
    # suppresses a new instance whenever ANY active one shares the base, so a still-pending
    # instance is never duplicated when the period rolls over. None for non-recurring types.
    dedup_key: Optional[str] = None
    dedup_base: Optional[str] = None

    # === Situational context (v0.16.0) ===
    # Snapshot built at generation time (contact name, days since contact,
    # CI status, ...). None for rows created before the migration.
    # Volatile types carry a "context_captured_at" stamp inside the dict.
    context: Optional[Dict[str, Any]] = None

    # === Source tracking ===
    source_type: Optional[str] = None  # blocked_goal, neglected_contact, manual
    source_id: Optional[str] = None
    created_by: Optional[str] = None  # autonomy_loop, user_request, agent:<agent_id>

    # === Assignment tracking ===
    status: str = "pending"
    assigned_agent_id: Optional[str] = None
    assigned_agent_name: Optional[str] = None
    assigned_at: Optional[datetime] = None
    acknowledged_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    cancelled_by: Optional[str] = None
    cancelled_reason: Optional[str] = None
    failed_at: Optional[datetime] = None
    failed_reason: Optional[str] = None

    # === Retry/reassignment ===
    attempt_count: int = 0
    max_attempts: int = 3
    timeout_seconds: int = 300
    last_attempt_at: Optional[datetime] = None

    # === Lifecycle ===
    expires_at: Optional[datetime] = None

    # === Delivery ===
    delivery_mode: str = "websocket"  # 'websocket' or 'http'
    delivery_attempts: int = 0
    last_delivery_at: Optional[datetime] = None
    delivery_failed_at: Optional[datetime] = None
    delivery_failed_reason: Optional[str] = None

    # === Results ===
    result: Optional[str] = None
    result_metadata: Dict[str, Any] = field(default_factory=dict)

    # === Preferred agent (hint) ===
    preferred_agent_id: Optional[str] = None

    # === Task queue link (v0.13.0) ===
    job_id: Optional[str] = None

    # === Stale/cleanup tracking ===
    stale_reason: Optional[str] = None
    recovery_reason: Optional[str] = None

    # === Intention and audit columns (architecture 5.2) ===
    # A row with a ``kind`` is a mind intention and an audit entry; rows
    # without one predate the mind and keep their old lifecycle.
    cls: Optional[str] = None            # internal | owner | contact | external | floor
    decision: Optional[str] = None       # act | ask | drop | defer
    decision_reason: Optional[str] = None
    drive: Optional[str] = None          # duty | social | curiosity | mastery | upkeep
    kind: Optional[str] = None           # task | goal | message | note
    ask_code: Optional[str] = None
    invalidates_if: Optional[str] = None
    success_check: Optional[str] = None  # JSON
    expectation_id: Optional[str] = None
    parent_goal_id: Optional[str] = None
    hermes_kind: Optional[str] = None    # kanban | message | none
    hermes_ref: Optional[str] = None
    outcome: Optional[str] = None        # done | blocked | failed | expired | denied | cancelled | uncertain
    verified: Optional[str] = None       # owner | check | hermes_failure | none
    verdict: Optional[str] = None        # actioned | dismissed | ignored | useful | not_useful | wrong
    lesson_ids: Optional[str] = None     # JSON list
    cost_tokens: int = 0
    due_at: Optional[datetime] = None

    @property
    def is_intention(self) -> bool:
        return bool(self.kind)

    @property
    def is_active(self) -> bool:
        """Is initiative still being worked on?"""
        if self.kind:
            return self.status in MIND_ACTIVE_STATUSES
        return self.status in ("pending", "assigned", "acknowledged")

    @property
    def is_expired(self) -> bool:
        """Has initiative expired?"""
        if not self.expires_at:
            return False
        return datetime.now(timezone.utc) > self.expires_at

    @property
    def is_timed_out(self) -> bool:
        """Has initiative exceeded timeout?"""
        if not self.assigned_at or self.status not in ("assigned", "acknowledged"):
            return False
        elapsed = (datetime.now(timezone.utc) - self.assigned_at).total_seconds()
        return elapsed > self.timeout_seconds

    @property
    def can_retry(self) -> bool:
        """Can this failed initiative be retried?"""
        return (
            self.status == "failed"
            and self.attempt_count < self.max_attempts
        )

    @classmethod
    def from_row(cls, row: dict) -> "StoredInitiative":
        """Create from SQLite row dict."""
        import json

        def parse_dt(value: Optional[str]) -> Optional[datetime]:
            if not value:
                return None
            try:
                if value.endswith("Z"):
                    value = value[:-1] + "+00:00"
                return datetime.fromisoformat(value)
            except (ValueError, TypeError):
                return None

        result_metadata_raw = row.get("result_metadata", "{}")
        if isinstance(result_metadata_raw, str):
            result_metadata = json.loads(result_metadata_raw)
        else:
            result_metadata = result_metadata_raw

        context_raw = row.get("context")
        if isinstance(context_raw, str):
            try:
                context = json.loads(context_raw)
            except (ValueError, TypeError):
                context = None
        else:
            context = context_raw

        return cls(
            id=row["id"],
            type=row["type"],
            description=row["description"],
            priority=row.get("priority", 0.5),
            rationale=row.get("rationale", ""),
            action_hint=row.get("action_hint"),
            entity_id=row.get("entity_id"),
            created_at=parse_dt(row.get("created_at")) or datetime.now(timezone.utc),
            dedup_key=row.get("dedup_key"),
            dedup_base=row.get("dedup_base"),
            context=context,
            source_type=row.get("source_type"),
            source_id=row.get("source_id"),
            created_by=row.get("created_by"),
            status=row.get("status", "pending"),
            assigned_agent_id=row.get("assigned_agent_id"),
            assigned_agent_name=row.get("assigned_agent_name"),
            assigned_at=parse_dt(row.get("assigned_at")),
            acknowledged_at=parse_dt(row.get("acknowledged_at")),
            completed_at=parse_dt(row.get("completed_at")),
            cancelled_at=parse_dt(row.get("cancelled_at")),
            cancelled_by=row.get("cancelled_by"),
            cancelled_reason=row.get("cancelled_reason"),
            failed_at=parse_dt(row.get("failed_at")),
            failed_reason=row.get("failed_reason"),
            attempt_count=row.get("attempt_count", 0),
            max_attempts=row.get("max_attempts", 3),
            timeout_seconds=row.get("timeout_seconds", 300),
            last_attempt_at=parse_dt(row.get("last_attempt_at")),
            expires_at=parse_dt(row.get("expires_at")),
            delivery_mode=row.get("delivery_mode", "websocket"),
            delivery_attempts=row.get("delivery_attempts", 0),
            last_delivery_at=parse_dt(row.get("last_delivery_at")),
            delivery_failed_at=parse_dt(row.get("delivery_failed_at")),
            delivery_failed_reason=row.get("delivery_failed_reason"),
            result=row.get("result"),
            result_metadata=result_metadata,
            preferred_agent_id=row.get("preferred_agent_id"),
            job_id=row.get("job_id"),
            stale_reason=row.get("stale_reason"),
            recovery_reason=row.get("recovery_reason"),
            cls=row.get("cls"),
            decision=row.get("decision"),
            decision_reason=row.get("decision_reason"),
            drive=row.get("drive"),
            kind=row.get("kind"),
            ask_code=row.get("ask_code"),
            invalidates_if=row.get("invalidates_if"),
            success_check=row.get("success_check"),
            expectation_id=row.get("expectation_id"),
            parent_goal_id=row.get("parent_goal_id"),
            hermes_kind=row.get("hermes_kind"),
            hermes_ref=row.get("hermes_ref"),
            outcome=row.get("outcome"),
            verified=row.get("verified"),
            verdict=row.get("verdict"),
            lesson_ids=row.get("lesson_ids"),
            cost_tokens=int(row.get("cost_tokens") or 0),
            due_at=parse_dt(row.get("due_at")),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        def format_dt(dt: Optional[datetime]) -> Optional[str]:
            return dt.isoformat() if dt else None

        return {
            "id": self.id,
            "type": self.type,
            "description": self.description,
            "priority": self.priority,
            "rationale": self.rationale,
            "action_hint": self.action_hint,
            "entity_id": self.entity_id,
            "created_at": format_dt(self.created_at),
            "dedup_key": self.dedup_key,
            "context": self.context,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "created_by": self.created_by,
            "status": self.status,
            "assigned_agent_id": self.assigned_agent_id,
            "assigned_agent_name": self.assigned_agent_name,
            "assigned_at": format_dt(self.assigned_at),
            "acknowledged_at": format_dt(self.acknowledged_at),
            "completed_at": format_dt(self.completed_at),
            "cancelled_at": format_dt(self.cancelled_at),
            "cancelled_by": self.cancelled_by,
            "cancelled_reason": self.cancelled_reason,
            "failed_at": format_dt(self.failed_at),
            "failed_reason": self.failed_reason,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "timeout_seconds": self.timeout_seconds,
            "last_attempt_at": format_dt(self.last_attempt_at),
            "expires_at": format_dt(self.expires_at),
            "delivery_mode": self.delivery_mode,
            "delivery_attempts": self.delivery_attempts,
            "last_delivery_at": format_dt(self.last_delivery_at),
            "delivery_failed_at": format_dt(self.delivery_failed_at),
            "delivery_failed_reason": self.delivery_failed_reason,
            "result": self.result,
            "result_metadata": self.result_metadata,
            "preferred_agent_id": self.preferred_agent_id,
            "job_id": self.job_id,
            "stale_reason": self.stale_reason,
            "recovery_reason": self.recovery_reason,
            "cls": self.cls,
            "decision": self.decision,
            "decision_reason": self.decision_reason,
            "drive": self.drive,
            "kind": self.kind,
            "ask_code": self.ask_code,
            "invalidates_if": self.invalidates_if,
            "success_check": self.success_check,
            "expectation_id": self.expectation_id,
            "parent_goal_id": self.parent_goal_id,
            "hermes_kind": self.hermes_kind,
            "hermes_ref": self.hermes_ref,
            "outcome": self.outcome,
            "verified": self.verified,
            "verdict": self.verdict,
            "lesson_ids": self.lesson_ids,
            "cost_tokens": self.cost_tokens,
            "due_at": format_dt(self.due_at),
        }


@dataclass
class AssignmentHistory:
    """History entry for initiative assignment changes."""

    id: Optional[int] = None
    initiative_id: str = ""
    agent_id: str = ""
    agent_name: Optional[str] = None
    action: str = ""  # assigned, acknowledged, completed, failed, cancelled, delegated, reassigned
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    details: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: dict) -> "AssignmentHistory":
        """Create from SQLite row."""
        import json

        details_raw = row.get("details", "{}")
        if isinstance(details_raw, str):
            details = json.loads(details_raw)
        else:
            details = details_raw

        ts = row.get("timestamp")
        if isinstance(ts, str):
            try:
                if ts.endswith("Z"):
                    ts = ts[:-1] + "+00:00"
                timestamp = datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                timestamp = datetime.now(timezone.utc)
        else:
            timestamp = ts or datetime.now(timezone.utc)

        return cls(
            id=row.get("id"),
            initiative_id=row.get("initiative_id", ""),
            agent_id=row.get("agent_id", ""),
            agent_name=row.get("agent_name"),
            action=row.get("action", ""),
            timestamp=timestamp,
            details=details,
        )
