"""Task Queue API — ``/v1/host/queue`` endpoints for distributed job scheduling.

Exposes the TaskQueueManager / QueueManager surface to external workers
(including the host agent's cron-driven worker).
"""

from __future__ import annotations

import logging
import math
import os
import hashlib
import json
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, model_validator

from protagine.api.auth import request_authority
from protagine.task_queue.contract import worker_authority_mode
from protagine.task_queue.models import (
    Job,
    JobCapabilityRequirement,
    JobPriority,
    JobStatus,
    JobType,
    WorkerCapabilities,
    is_canonical_job_id,
)
from protagine.task_queue.queue_manager import (
    QueueExecutionUnavailable,
    TaskQueueManager,
    decision_tags,
)
from protagine.util.session_safety import (
    load_last_user_message_at,
    save_last_user_message_at,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/host/queue", tags=["task_queue"])

_RESERVED_JOB_TAGS = frozenset({
    "approved_by", "approved_at", "auto_approved_by_policy",
    "action_digest", "bounded_grant_id", "bounded_grant_expires_at",
    "bounded_grant_ttl_state", "bounded_grant_uses_state",
    "rejected_by", "rejected_at",
    "rejected_reason", "hold_kind", "blocked_reason",
    "agent_action_route", "agent_action_route_node",
    "thought_route", "thought_route_node",
    "outbound_target",
    "action_result_contract", "operational_completion_only",
    "verification_pending", "worker_completion_terminalized",
})
_RESERVED_JOB_TAG_PREFIXES = (
    "approval_", "governor_", "worker_authority_", "success_",
)


def _reserved_job_tags(tags: Optional[Dict[str, str]]) -> List[str]:
    return sorted(
        key for key in (tags or {})
        if key in _RESERVED_JOB_TAGS
        or any(key.startswith(prefix) for prefix in _RESERVED_JOB_TAG_PREFIXES)
    )

class WorkerRegisterRequest(BaseModel):
    node_id: str
    capabilities: Optional[List[str]] = None
    capacity: Optional[Dict[str, float]] = None
    max_concurrent: Optional[int] = Field(None, ge=1, le=1024)
    job_types: Optional[List[str]] = None
    available: bool = True
    load: float = Field(0.0, ge=0.0, le=1.0)


class WorkerHeartbeatRequest(BaseModel):
    job_ids: List[str] = []
    progress: Optional[Dict[str, float]] = None
    claim_attempt_ids: Dict[str, str] = {}
    load: Optional[float] = None

    @model_validator(mode="after")
    def exact_attempts_for_jobs(self):
        job_ids = set(self.job_ids)
        attempt_ids = set(self.claim_attempt_ids)
        if job_ids != attempt_ids or any(
            not str(value).strip()
            for value in self.claim_attempt_ids.values()
        ):
            raise ValueError(
                "claim_attempt_ids must contain one non-empty exact attempt "
                "for every job_id and no other entries"
            )
        return self


class JobPostRequest(BaseModel):
    job_type: str = "agent_action"
    payload: Dict[str, Any] = {}
    priority: str = "normal"
    capabilities: Optional[List[Dict[str, Any]]] = None
    deadline: Optional[str] = None
    max_retries: int = 3
    timeout_secs: float = 3600.0
    depends_on: List[str] = []
    tags: Optional[Dict[str, str]] = None


class JobClaimRequest(BaseModel):
    node_id: str
    capabilities: Optional[List[str]] = None
    capacity: Optional[Dict[str, float]] = None
    max_concurrent: Optional[int] = Field(None, ge=1, le=1024)
    job_types: Optional[List[str]] = None


class JobCompleteRequest(BaseModel):
    output: Dict[str, Any] = {}
    claim_attempt_id: str = Field(min_length=1, max_length=128)
    # Deprecated compatibility telemetry. Queue timing is always derived from
    # the durable server claim/start ledger and this value is ignored.
    started_at: Optional[str] = None


class ActionSuccessAttestationRequest(BaseModel):
    """Independent evidence for one exact generic Action Plane attempt."""

    model_config = {"extra": "forbid", "strict": True}

    schema_name: str = Field(alias="schema")
    version: int
    job_id: str = Field(min_length=1, max_length=128)
    claim_attempt_id: str = Field(min_length=1, max_length=128)
    action_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    effect_class: str
    terminal_outcome: str
    receipt_refs: List[Any] = Field(min_length=1, max_length=32)
    observed_at: str
    summary: str = Field("", max_length=500)

    @model_validator(mode="after")
    def exact_action_receipt(self):
        from protagine.task_queue.action_receipts import (
            ActionReceiptAttestationV1,
        )

        ActionReceiptAttestationV1.from_payload(
            self.model_dump(by_alias=True)
        )
        return self


class JobFailRequest(BaseModel):
    error: str
    claim_attempt_id: str = Field(min_length=1, max_length=128)
    # Deprecated and non-authoritative; retained so older workers still parse.
    started_at: Optional[str] = None


class JobHeartbeatRequest(BaseModel):
    progress: Optional[float] = None
    log_lines: Optional[List[str]] = None
    claim_attempt_id: str = Field(min_length=1, max_length=128)


class JobStartRequest(BaseModel):
    claim_attempt_id: str = Field(min_length=1, max_length=128)


class JobReleaseRequest(BaseModel):
    claim_attempt_id: str = Field(min_length=1, max_length=128)


class WorkControlOperationRequest(BaseModel):
    """Strict caller-authored CAS command for one durable work target."""

    model_config = {"extra": "forbid", "strict": True}

    schema_name: str = Field(alias="schema", pattern=r"^WorkControlOperationV1$")
    version: int = Field(ge=1, le=1)
    operation_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,127}$",
    )
    operation: str
    target_id: str = Field(min_length=1, max_length=192)
    run_id: str = Field(min_length=1, max_length=128)
    attempt_id: Optional[str] = Field(None, max_length=128)
    expected_revision: int = Field(ge=1)
    expected_state_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    parameters: Dict[str, Any] = {}
    reason: str = Field("", max_length=500)


class WorkControlAckRequest(BaseModel):
    """Exact claimant acknowledgement; identity never comes from this body."""

    model_config = {"extra": "forbid", "strict": True}

    schema_name: str = Field(alias="schema", pattern=r"^WorkControlAckV1$")
    version: int = Field(ge=1, le=1)
    attempt_id: str = Field(min_length=1, max_length=128)
    outcome: str
    details: Dict[str, Any] = {}


class WorkControlAckEnvelopeRequest(WorkControlAckRequest):
    """Slash-safe worker acknowledgement representation."""

    node_id: str = Field(min_length=1, max_length=192)
    operation_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/+-]{0,127}$",
    )


class WorkControlWorkerOutcomeRequest(BaseModel):
    """Durable slash-safe steer outcome recorded before acknowledgement."""

    model_config = {"extra": "forbid", "strict": True}

    schema_name: str = Field(
        alias="schema", pattern=r"^WorkControlWorkerOutcomeV1$",
    )
    version: int = Field(ge=1, le=1)
    node_id: str = Field(min_length=1, max_length=192)
    operation_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/+-]{0,127}$",
    )
    attempt_id: str = Field(min_length=1, max_length=128)
    outcome: str
    details: Dict[str, Any] = {}


class WorkEffectReconciliationRequest(BaseModel):
    """Independent exact-attempt applied/not-applied evidence."""

    model_config = {"extra": "forbid", "strict": True}

    schema_name: str = Field(
        alias="schema", pattern=r"^WorkEffectReconciliationV1$",
    )
    version: int = Field(ge=1, le=1)
    reconciliation_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/+-]{0,127}$",
    )
    target_id: str = Field(min_length=1, max_length=192)
    attempt_id: str = Field(min_length=1, max_length=128)
    authority_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    finding: str
    evidence_refs: List[str] = Field(min_length=1, max_length=32)
    observed_at: str
    summary: str = Field("", max_length=1000)


class JobApproveRequest(BaseModel):
    """A direct owner decision. The legacy ledger fields are accepted and ignored:
    the actor is the authenticated principal, never caller prose."""

    model_config = {"extra": "ignore"}

    approved_by: Optional[str] = None
    always: bool = False
    approval_request_id: Optional[str] = None
    expected_action_digest: Optional[str] = None
    decision_id: Optional[str] = None
    grant: Optional[Dict[str, Any]] = None


class JobRejectRequest(BaseModel):
    model_config = {"extra": "ignore"}

    rejected_by: Optional[str] = None
    reason: str = "rejected_by_owner"
    approval_request_id: Optional[str] = None
    expected_action_digest: Optional[str] = None
    decision_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_queue() -> TaskQueueManager:
    try:
        return TaskQueueManager.get_instance()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Task queue not initialized")


def _work_control_http_error(exc: Exception) -> HTTPException:
    from protagine.task_queue.work_control import WorkControlError

    if isinstance(exc, WorkControlError):
        return HTTPException(
            status_code=exc.status_code,
            detail=exc.detail(),
        )
    raise exc


@router.get("/contract")
async def queue_contract(request: Request = None) -> Dict[str, Any]:
    """Return the authenticated, deploy-pinned worker protocol identity."""

    from protagine.task_queue.contract import (
        QueueContractIdentityError,
        queue_contract_identity,
    )

    try:
        contract = queue_contract_identity()
    except QueueContractIdentityError as exc:
        raise HTTPException(status_code=503, detail={
            "code": "queue_contract_identity_unavailable",
            "message": str(exc),
        }) from exc
    try:
        runtime = _get_queue().queue.execution_readiness()
    except HTTPException:
        contract["runtime_readiness"] = {
            "queue_initialized": False,
            "thought": {
                "ready": False,
                "node_id": None,
                "reason": "queue_not_initialized",
            },
        }
    else:
        contract["runtime_readiness"] = {
            "queue_initialized": True,
            "queue_execution_ready": runtime["ready"],
            "queue_execution_reason": runtime["reason"],
            "thought": runtime["typed_routes"]["thought"],
        }
    return contract


def _governor() -> Any:
    """The server-side WorkerGovernor (item 5), or None if not wired."""
    try:
        from protagine.api.routers.host import _worker_governor
        return _worker_governor
    except Exception:
        return None


def _worker_authority_error(code: str, message: str, *, status: int = 403):
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _worker_request_context(
    request: Optional[Request],
    *,
    node_id: str,
    required_scope: str,
    claimant: bool = False,
) -> Dict[str, Any]:
    """Resolve worker authority from transport authentication, never a body claim."""

    mode = worker_authority_mode()
    if mode == "invalid":
        raise _worker_authority_error(
            "worker_authority_configuration_invalid",
            "PROTAGINE_WORKER_AUTHORITY_MODE must be shadow or enforce",
            status=503,
        )
    if request is None:
        # Embedded/in-process queue integrations are a separate trusted lane.
        return {
            "mode": "internal",
            "principal": "trusted-internal",
            "credential": "in_process",
            "grant": None,
            "would_deny": False,
        }

    authority = request_authority(request)
    keyed = bool(authority.authenticated and not authority.anonymous)
    if mode == "enforce" and not keyed:
        raise _worker_authority_error(
            "worker_authority_required",
            "the API key is required for worker registration and claims",
        )
    return {
        "mode": mode,
        "principal": authority.principal_id,
        "credential": authority.credential_id or "none",
        "grant": None,
        "would_deny": not keyed,
    }


def _worker_tags(context: Dict[str, Any]) -> Dict[str, str]:
    return {
        "worker_authority_mode": str(context["mode"]),
        "worker_authority_principal": str(context["principal"]),
        "worker_authority_credential": str(context["credential"]),
        "worker_authority_would_deny": (
            "true" if context.get("would_deny") else "false"
        ),
    }


def _job_type_set(values: List[str], *, field: str) -> set[JobType]:
    result: set[JobType] = set()
    for value in values:
        try:
            result.add(JobType(value))
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_job_type", "field": field, "value": value},
            ) from exc
    return result


def _bounded_worker_capabilities(
    body: WorkerRegisterRequest | JobClaimRequest,
    context: Dict[str, Any],
) -> WorkerCapabilities:
    """Build effective caps from the body; the one key carries no per-node ceiling."""

    return WorkerCapabilities(
        node_id=body.node_id,
        capabilities=set(body.capabilities or []),
        capacity={key: float(value) for key, value in (body.capacity or {}).items()},
        max_concurrent=body.max_concurrent or 4,
        job_types=_job_type_set(body.job_types or [], field="job_types"),
        available=bool(getattr(body, "available", True)),
        load=float(getattr(body, "load", 0.0)),
    )


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    """Parse an ISO timestamp into aware UTC; a value with no zone is UTC."""
    if not s:
        return None
    try:
        parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _decision_actor(request: Optional[Request]) -> str:
    """The actor of a direct decision: the one API key (the owner) or an in-process caller."""

    if request is None:
        # Direct in-process calls are an explicit trusted integration surface.
        return "trusted-internal"
    authority = request_authority(request)
    if not (authority.authenticated and not authority.anonymous):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "approval_scope_required",
                "message": "the API key is required to decide approvals",
            },
        )
    return authority.principal_id


async def _decide_job(
    *,
    job: Job,
    decision: str,
    decision_id: Optional[str],
    request: Optional[Request],
    rejection_reason: str = "rejected_by_owner",
    **_legacy: Any,
) -> Dict[str, Any]:
    """A direct owner decision on an approval-held job: no ledger, no grants.

    The decision is recorded in the job's tags with the actor the request
    authenticated as. An exact retry with the same ``decision_id`` reads the
    recorded decision back instead of deciding twice.
    """
    queue = _get_queue()
    actor = _decision_actor(request)
    recorded_id = str(job.tags.get("approval_decision_id") or "")
    if (
        recorded_id
        and decision_id
        and recorded_id == decision_id
        and str(job.tags.get("approval_decision") or "") == decision
    ):
        return {
            "success": True,
            "job_id": job.job_id,
            "status": job.status.value,
            "decision": decision,
            "decided_by": job.tags.get("approval_authority"),
            "decided_at": job.tags.get("approval_decided_at"),
            "approval_request": None,
            "bounded_grant": None,
            "replayed": True,
            "authority_mode": "direct",
        }
    if job.status != JobStatus.BLOCKED:
        raise HTTPException(
            status_code=409,
            detail=f"Job is {job.status.value}, not blocked",
        )
    if job.tags.get("blocked_reason") != "awaiting_owner_approval":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "job_not_approval_blocked",
                "message": "only owner-approval-blocked jobs can be decided",
            },
        )
    decided_at = datetime.now(timezone.utc).isoformat()
    normalized_decision_id = decision_id or ("compat_" + os.urandom(16).hex())
    tags = decision_tags(job.job_id, decision=decision, decision_id=normalized_decision_id,
                         actor=actor, at=decided_at)
    if decision == "approve":
        new_status = JobStatus.QUEUED
        reason = f"approved_by_principal={actor}"
        if job.depends_on:
            dependencies_ready = True
            for dependency_id in job.depends_on:
                dependency = await queue.queue.get_job(dependency_id)
                if (
                    dependency is None
                    or dependency.status is not JobStatus.COMPLETED
                ):
                    dependencies_ready = False
                    break
            if not dependencies_ready:
                # The decision is durable, but it cannot erase a separate
                # dependency gate. unblock_ready_jobs() queues the job only
                # after every prerequisite independently closes.
                new_status = JobStatus.BLOCKED
                reason = "approved_waiting_for_dependencies"
                tags.update({
                    "hold_kind": "dependency",
                    "blocked_reason": "dependencies_pending",
                })
    else:
        tags["rejected_reason"] = rejection_reason
        new_status = JobStatus.CANCELLED
        reason = rejection_reason
    changed = await queue.queue.update_job_status(
        job.job_id,
        new_status,
        reason=reason,
        tags=tags,
        remove_tags=[
            "hold_kind", "blocked_reason", "awaiting_owner_approval",
            "governor_error", "governor_last_recheck_at",
        ],
    )
    if not changed:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "job_transition_failed",
                "message": "the job transition must be reconciled before it can be decided",
            },
        )
    return {
        "success": True,
        "job_id": job.job_id,
        "status": new_status.value,
        "decision": decision,
        "decided_by": actor,
        "decided_at": decided_at,
        "approval_request": None,
        "bounded_grant": None,
        "replayed": False,
        "authority_mode": "direct",
    }


def _job_to_dict(job: Job) -> Dict[str, Any]:
    return {
        "job_id": job.job_id,
        "job_type": job.job_type.value,
        "payload": job.payload,
        "priority": job.priority.value,
        "status": job.status.value,
        "capabilities": [
            {
                "name": capability.name,
                "minimum": capability.minimum,
                "preferred": capability.preferred,
            }
            for capability in job.capabilities
        ],
        "claimed_by": job.claimed_by,
        "claimed_at": job.claimed_at.isoformat() if job.claimed_at else None,
        "claim_attempt_id": job.claim_attempt_id,
        "claim_expires_at": (
            job.claim_expires_at.isoformat() if job.claim_expires_at else None
        ),
        "posted_at": job.posted_at.isoformat() if job.posted_at else None,
        "deadline": job.deadline.isoformat() if job.deadline else None,
        "max_retries": job.max_retries,
        "retry_count": job.retry_count,
        "timeout_secs": job.timeout_secs,
        "depends_on": job.depends_on,
        "tags": job.tags,
        "result": {
            "worker_node_id": job.result.worker_node_id,
            "status": job.result.status.value,
            "claim_attempt_id": job.result.claim_attempt_id,
            "output": job.result.output,
            "error": job.result.error,
            "started_at": job.result.started_at.isoformat() if job.result and job.result.started_at else None,
            "completed_at": job.result.completed_at.isoformat() if job.result and job.result.completed_at else None,
            "duration_seconds": job.result.duration_seconds if job.result else None,
        } if job.result else None,
    }


def _job_inspection_dict(job: Job) -> Dict[str, Any]:
    """Return authority canary fields without payload/result disclosure."""

    import hashlib
    import json

    route_tag_names = {
        "agent_action_route", "agent_action_route_node",
        "thought_route", "thought_route_node",
        "schema", "risk_class", "idempotency_key",
        "work_order_digest", "work_order_version", "executor_protocol",
        "thought_job_digest", "concern_id", "viewer_scope", "shareability",
        "action_digest", "action_result_contract", "verification_pending",
        "success_attestation_schema", "success_evidence_digest",
        "success_receipt_refs_digest", "success_verifier_identity",
        "success_verifier_type",
    }

    def digest(value: Any) -> str:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    result_projection = None
    if job.result is not None:
        result_projection = {
            "status": job.result.status.value,
            "claim_attempt_id": job.result.claim_attempt_id,
            "worker_node_id": job.result.worker_node_id,
            "output": job.result.output,
            "error": job.result.error,
        }
    return {
        "job_id": job.job_id,
        "job_type": job.job_type.value,
        "status": job.status.value,
        "capabilities": [
            {
                "name": capability.name,
                "minimum": capability.minimum,
                "preferred": capability.preferred,
            }
            for capability in job.capabilities
        ],
        "claimed_by": job.claimed_by,
        "claim_attempt_id": job.claim_attempt_id,
        "claim_expires_at": (
            job.claim_expires_at.isoformat() if job.claim_expires_at else None
        ),
        "tags": {
            key: value for key, value in (job.tags or {}).items()
            if key in route_tag_names
        },
        "payload_sha256": digest(job.payload),
        "result_sha256": (
            digest(result_projection) if result_projection is not None else None
        ),
        "result_status": (
            job.result.status.value if job.result is not None else None
        ),
        "result_claim_attempt_id": (
            job.result.claim_attempt_id if job.result is not None else None
        ),
    }


# ---------------------------------------------------------------------------
# WorkControlV1 — generic operator control over durable queue work
# ---------------------------------------------------------------------------

@router.get("/work")
async def get_work_control_target_query(
    target_id: str = Query(min_length=1, max_length=192),
) -> Dict[str, Any]:
    """Slash-safe target inspection; path form remains compatibility-only."""

    return await get_work_control_target(target_id)


@router.post("/work/operations")
async def apply_work_control_operation_body(
    body: WorkControlOperationRequest,
    request: Request,
) -> Dict[str, Any]:
    """Slash-safe mutation using the exact target ID already in the body."""

    return await apply_work_control_operation(body.target_id, body, request)


@router.get("/work/operations/receipt")
async def get_work_control_receipt_query(
    target_id: str = Query(min_length=1, max_length=192),
    operation_id: str = Query(min_length=1, max_length=128),
) -> Dict[str, Any]:
    """Slash-safe receipt lookup for canonical operation identifiers."""

    return await get_work_control_receipt(target_id, operation_id)


@router.post("/work/reconciliations")
async def reconcile_work_effect(
    body: WorkEffectReconciliationRequest,
    request: Request,
) -> Dict[str, Any]:
    """Close one ambiguous effect using an independent scoped verifier."""

    authority = request_authority(request)
    if not authority.authenticated or authority.anonymous:
        raise HTTPException(status_code=403, detail={
            "code": "independent_verifier_required",
            "message": "the API key is required to reconcile effects",
        })
    queue = _get_queue()
    job = await queue.queue.get_job(body.target_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    executor_node = str(
        job.result.worker_node_id if job.result is not None else ""
    ).strip()
    executor_principal = str(
        (job.tags or {}).get("worker_authority_principal") or ""
    ).strip()
    if authority.principal_id in {executor_node, executor_principal}:
        raise HTTPException(status_code=403, detail={
            "code": "independent_verifier_required",
            "message": "the executor cannot reconcile its own effect",
        })
    payload = body.model_dump(by_alias=True)
    try:
        return await queue.queue.reconcile_work_effect(
            reconciliation_id=payload["reconciliation_id"],
            target_id=payload["target_id"],
            attempt_id=payload["attempt_id"],
            authority_digest=payload["authority_digest"],
            finding=payload["finding"],
            evidence_refs=payload["evidence_refs"],
            observed_at=payload["observed_at"],
            summary=payload.get("summary") or "",
            verifier_identity=authority.principal_id,
            verifier_type="scoped_effect_reconciler",
            verifier_authority={
                "authority_kind": "scoped_principal",
                "principal_id": authority.principal_id,
                "credential_id": authority.credential_id,
                "required_scope": "workers:attest",
            },
        )
    except Exception as exc:
        raise _work_control_http_error(exc) from exc


@router.get("/work/{target_id}")
async def get_work_control_target(target_id: str) -> Dict[str, Any]:
    """Read the exact revision/digest and currently allowed operations."""

    try:
        return await _get_queue().queue.work_control.inspect(target_id)
    except Exception as exc:
        raise _work_control_http_error(exc) from exc


@router.post("/work/{target_id}/operations")
async def apply_work_control_operation(
    target_id: str,
    body: WorkControlOperationRequest,
    request: Request,
) -> Dict[str, Any]:
    """CAS-apply an idempotent command from an exact scoped principal."""

    authority = request_authority(request)
    if not authority.authenticated or authority.anonymous:
        raise HTTPException(status_code=403, detail={
            "code": "exact_work_control_principal_required",
            "message": "WorkControl mutations require the API key",
        })
    payload = body.model_dump(by_alias=True)
    if payload["target_id"] != target_id:
        raise HTTPException(status_code=409, detail={
            "code": "work_target_path_mismatch",
            "message": "body target_id must match the path target",
        })
    try:
        return await _get_queue().queue.work_control.operate(
            operation_id=payload["operation_id"],
            operation=payload["operation"],
            target_id=payload["target_id"],
            run_id=payload["run_id"],
            attempt_id=payload.get("attempt_id"),
            expected_revision=payload["expected_revision"],
            expected_state_digest=payload["expected_state_digest"],
            parameters=payload.get("parameters") or {},
            reason=payload.get("reason") or "",
            requested_by=authority.principal_id,
            request_authority={
                "authority_kind": "scoped_principal",
                "principal_id": authority.principal_id,
                "credential_id": authority.credential_id,
                "required_scope": "work:control",
            },
        )
    except Exception as exc:
        raise _work_control_http_error(exc) from exc


@router.get("/work/{target_id}/operations/{operation_id}")
async def get_work_control_receipt(
    target_id: str,
    operation_id: str,
) -> Dict[str, Any]:
    """Read accepted and immutable outcome receipts for one operation."""

    try:
        return await _get_queue().queue.work_control.receipt(
            target_id, operation_id,
        )
    except Exception as exc:
        raise _work_control_http_error(exc) from exc


@router.get("/workers/{node_id}/controls")
async def pending_worker_controls(
    node_id: str,
    request: Request,
) -> List[Dict[str, Any]]:
    """Deliver pending commands only to their transport-attested worker."""

    context = _worker_request_context(
        request,
        node_id=node_id,
        required_scope="workers:lifecycle",
        claimant=True,
    )
    if context.get("would_deny"):
        raise _worker_authority_error(
            "exact_worker_grant_required",
            "WorkControl delivery requires an exact scoped worker grant",
        )
    return await _get_queue().queue.work_control.pending_for_worker(node_id)


@router.post("/workers/controls/ack")
async def acknowledge_worker_control_body(
    body: WorkControlAckEnvelopeRequest,
    request: Request,
) -> Dict[str, Any]:
    """Slash-safe worker acknowledgement for canonical operation IDs."""

    return await acknowledge_worker_control(
        body.node_id, body.operation_id, body, request,
    )


@router.post("/workers/controls/outcome")
async def record_worker_control_outcome(
    body: WorkControlWorkerOutcomeRequest,
    request: Request,
) -> Dict[str, Any]:
    """Persist an idempotent steer result before the worker sends its ack."""

    context = _worker_request_context(
        request,
        node_id=body.node_id,
        required_scope="workers:lifecycle",
        claimant=True,
    )
    if context.get("would_deny"):
        raise _worker_authority_error(
            "exact_worker_grant_required",
            "durable WorkControl outcome requires an exact worker grant",
        )
    try:
        return await _get_queue().queue.record_work_control_worker_outcome(
            worker_id=body.node_id,
            operation_id=body.operation_id,
            attempt_id=body.attempt_id,
            outcome=body.outcome,
            details=body.details,
        )
    except Exception as exc:
        raise _work_control_http_error(exc) from exc


@router.post("/workers/{node_id}/controls/{operation_id}/ack")
async def acknowledge_worker_control(
    node_id: str,
    operation_id: str,
    body: WorkControlAckRequest,
    request: Request,
) -> Dict[str, Any]:
    """Persist the exact claimant's cooperative acknowledgement."""

    context = _worker_request_context(
        request,
        node_id=node_id,
        required_scope="workers:lifecycle",
        claimant=True,
    )
    if context.get("would_deny"):
        raise _worker_authority_error(
            "exact_worker_grant_required",
            "WorkControl acknowledgement requires an exact scoped worker grant",
        )
    authority = request_authority(request)
    try:
        return await _get_queue().queue.work_control.acknowledge(
            worker_id=node_id,
            operation_id=operation_id,
            attempt_id=body.attempt_id,
            outcome=body.outcome,
            details=body.details,
            ack_authority={
                "authority_kind": "scoped_worker_principal",
                "principal_id": authority.principal_id,
                "credential_id": authority.credential_id,
                "worker_id": node_id,
                "required_scope": "workers:lifecycle",
                "worker_authority_mode": context.get("mode"),
            },
        )
    except Exception as exc:
        raise _work_control_http_error(exc) from exc


# ---------------------------------------------------------------------------
# Worker endpoints
# ---------------------------------------------------------------------------

@router.post("/workers/register")
async def register_worker(
    body: WorkerRegisterRequest,
    request: Request = None,
) -> Dict[str, Any]:
    """Register a worker node with the scheduler."""
    queue = _get_queue()
    context = _worker_request_context(
        request, node_id=body.node_id, required_scope="workers:register",
    )
    caps = _bounded_worker_capabilities(body, context)
    await queue.queue.register_worker(caps)
    logger.info("Worker registered: %s (types=%s)", body.node_id, body.job_types)
    return {
        "success": True,
        "node_id": body.node_id,
        "worker_authority": _worker_tags(context),
    }


@router.post("/workers/{node_id}/heartbeat")
async def worker_heartbeat(
    node_id: str,
    body: WorkerHeartbeatRequest,
    request: Request = None,
) -> Dict[str, Any]:
    """Receive a worker heartbeat."""
    queue = _get_queue()
    context = _worker_request_context(
        request, node_id=node_id, required_scope="workers:lifecycle",
    )
    progress = body.progress or {}
    updated = await queue.queue.send_heartbeat(
        worker_id=node_id,
        job_ids=body.job_ids,
        progress=progress,
        claim_attempt_ids=body.claim_attempt_ids,
    )
    if updated != len(set(body.job_ids)):
        raise HTTPException(status_code=409, detail={
            "code": "worker_heartbeat_claim_mismatch",
            "message": "one or more heartbeat jobs are not claimed by this worker",
        })
    if body.load is not None:
        await queue.queue.update_worker_load(node_id, body.load)
    return {
        "success": True,
        "node_id": node_id,
        "jobs_updated": updated,
        "worker_authority": _worker_tags(context),
    }


@router.post("/workers/{node_id}/deregister")
async def deregister_worker(
    node_id: str,
    request: Request = None,
) -> Dict[str, Any]:
    """Remove a worker from the scheduler."""
    queue = _get_queue()
    context = _worker_request_context(
        request, node_id=node_id, required_scope="workers:register",
    )
    await queue.queue.deregister_worker(node_id)
    logger.info("Worker deregistered: %s", node_id)
    return {
        "success": True,
        "node_id": node_id,
        "worker_authority": _worker_tags(context),
    }


# ---------------------------------------------------------------------------
# Job endpoints
# ---------------------------------------------------------------------------

@router.post("/jobs")
async def create_job(body: JobPostRequest) -> Dict[str, Any]:
    """Post a new job to the queue."""
    queue = _get_queue()
    reserved = _reserved_job_tags(body.tags)
    if reserved:
        raise HTTPException(status_code=400, detail={
            "code": "reserved_job_tags",
            "message": "authority tags may only be set by Protagine control planes",
            "tags": reserved,
        })
    if body.payload.get("schema") == "WorkOrderV1":
        raise HTTPException(status_code=400, detail={
            "code": "work_order_authority_reserved",
            "message": (
                "WorkOrderV1 authority may only be issued by the durable "
                "ProjectEngine adapter"
            ),
        })
    if body.job_type == JobType.THOUGHT.value or (
        body.payload.get("schema") == "ThoughtJobV1"
    ):
        raise HTTPException(status_code=400, detail={
            "code": "thought_job_authority_reserved",
            "message": (
                "ThoughtJobV1 may only be issued by Protagine's cognition spine"
            ),
        })
    from protagine.task_queue.routing import (
        AGENT_ACTION_ROUTE_CAPABILITIES,
    )
    caller_route_capabilities = sorted({
        str(capability.get("name") or "").strip()
        for capability in (body.capabilities or [])
    } & AGENT_ACTION_ROUTE_CAPABILITIES)
    if caller_route_capabilities:
        raise HTTPException(status_code=400, detail={
            "code": "agent_action_route_authority_reserved",
            "message": (
                "agent_action executor routes are derived by Protagine and "
                "cannot be selected by API callers"
            ),
            "capabilities": caller_route_capabilities,
        })
    try:
        job_type = JobType(body.job_type) if body.job_type else JobType.AGENT_ACTION
    except ValueError:
        raise HTTPException(status_code=400, detail={
            "code": "invalid_job_type",
            "message": f"unknown job_type {body.job_type!r}",
            "valid_job_types": sorted(t.value for t in JobType),
        })
    payload = dict(body.payload)
    public_effect = False
    if job_type is JobType.AGENT_ACTION:
        declared_risk = str(payload.get("risk") or "").strip().lower()
        if declared_risk not in {"read_only", "mutating", "outbound"}:
            raise HTTPException(status_code=400, detail={
                "code": "agent_action_risk_required",
                "message": "public agent_action jobs declare risk: read_only, mutating or outbound",
            })
        payload["risk"] = declared_risk
        public_effect = declared_risk != "read_only"
    # JobPriority is an int Enum (NORMAL=50, HIGH=80, ...); look up by NAME, not value —
    # JobPriority("HIGH") tries to match a member whose value is the string "HIGH" and always
    # raises (it even 500'd the default "normal"). Accept the name or the numeric value.
    if body.priority:
        _p = str(body.priority).upper()
        priority = JobPriority[_p] if _p in JobPriority.__members__ else JobPriority(int(body.priority))
    else:
        priority = JobPriority.NORMAL

    caps: List[JobCapabilityRequirement] = []
    if body.capabilities:
        for c in body.capabilities:
            caps.append(JobCapabilityRequirement(
                name=c["name"],
                minimum=c.get("minimum"),
                preferred=c.get("preferred", False),
            ))

    job = Job(
        job_type=job_type,
        payload=payload,
        priority=priority,
        capabilities=caps,
        deadline=_parse_dt(body.deadline),
        max_retries=body.max_retries,
        timeout_secs=body.timeout_secs,
        depends_on=body.depends_on,
        tags=body.tags or {},
        posted_by="api",
    )
    if public_effect:
        job.status = JobStatus.BLOCKED
        job.tags.update({
            "hold_kind": "approval",
            "blocked_reason": "awaiting_owner_approval",
            "awaiting_owner_approval": "true",
            "approval_requested_at": datetime.now(timezone.utc).isoformat(),
        })
    if job_type is JobType.AGENT_ACTION:
        from protagine.task_queue.routing import (
            expected_agent_action_routes,
        )
        try:
            expected_agent_action_routes(job)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={
                "code": "agent_action_routing_invalid",
                "message": str(exc),
            }) from exc
    job_id = await queue.queue.post(job)
    return {"success": True, "job_id": job_id}


@router.post("/jobs/claim")
async def claim_job(
    body: JobClaimRequest,
    request: Request = None,
) -> Optional[Dict[str, Any]]:
    """Claim through QueueManager's mandatory atomic authority boundary."""
    queue = _get_queue()
    context = _worker_request_context(
        request, node_id=body.node_id, required_scope="workers:claim",
    )
    caps = _bounded_worker_capabilities(body, context)
    try:
        job = await queue.queue.claim_job(
            body.node_id,
            caps,
            authority_tags=_worker_tags(context),
        )
    except QueueExecutionUnavailable as exc:
        raise HTTPException(status_code=503, detail={
            "code": "queue_execution_unavailable",
            "message": str(exc),
        }) from exc
    if job is None:
        return None
    out = _job_to_dict(job)
    mode = str(job.tags.get("governor_mode") or "")
    if mode:
        governor = {
            "mode": mode,
            "enforced": str(job.tags.get("governor_enforced") or "false")
            == "true",
            "would_refuse": str(
                job.tags.get("governor_would_refuse") or "false"
            ) == "true",
        }
        error = str(job.tags.get("governor_error") or "")
        if error:
            governor["error"] = error
        out["governor"] = governor
    return out


@router.post("/jobs/{job_id}/start")
async def start_job(
    job_id: str,
    body: JobStartRequest,
    request: Request = None,
) -> Dict[str, Any]:
    """Transition a claimed job to RUNNING."""
    queue = _get_queue()
    job = await queue.queue.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    worker_id = job.claimed_by
    if not worker_id:
        raise HTTPException(status_code=409, detail={
            "code": "job_not_claimed", "message": "job has no active claimant",
        })
    context = _worker_request_context(
        request, node_id=worker_id, required_scope="workers:lifecycle", claimant=True,
    )
    claim_attempt_id = body.claim_attempt_id
    replayed = (
        job.status is JobStatus.RUNNING
        and job.claim_attempt_id == claim_attempt_id
    )
    if not await queue.queue.start_job(
        job_id, worker_id, claim_attempt_id=claim_attempt_id,
    ):
        raise HTTPException(status_code=409, detail={
            "code": "job_transition_failed",
            "message": "job is not claimed by this worker in the expected state",
        })
    return {
        "success": True,
        "job_id": job_id,
        "claim_attempt_id": claim_attempt_id,
        "replayed": replayed,
        "worker_authority": _worker_tags(context),
    }


@router.post("/jobs/{job_id}/complete")
async def complete_job(
    job_id: str,
    body: JobCompleteRequest,
    request: Request = None,
) -> Dict[str, Any]:
    """Mark a job as completed."""
    queue = _get_queue()
    job = await queue.queue.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    worker_id = job.claimed_by or (
        job.result.worker_node_id
        if (
            job.status in {
                JobStatus.COMPLETED, JobStatus.NEUTRAL, JobStatus.FAILED,
            }
            and job.result is not None
            and str(job.tags.get("worker_completion_terminalized") or "")
            == "true"
        )
        else None
    )
    if not worker_id:
        raise HTTPException(status_code=409, detail={
            "code": "job_not_claimed", "message": "job has no active claimant",
        })
    context = _worker_request_context(
        request, node_id=worker_id, required_scope="workers:lifecycle", claimant=True,
    )

    # Completion auditing and trust evidence live in QueueManager so embedded
    # and direct consumers cannot bypass the HTTP-only path.
    audit = await queue.queue.complete_job(
        job_id=job_id,
        worker_id=worker_id,
        output=body.output,
        started_at=None,
        claim_attempt_id=body.claim_attempt_id,
    )

    if not audit.get("transitioned"):
        raise HTTPException(status_code=409, detail={
            "code": "job_transition_failed",
            "message": "job is not running under this worker claim",
        })

    return {
        "success": bool(audit.get("transitioned")),
        "job_id": job_id,
        "claim_attempt_id": body.claim_attempt_id,
        "worker_authority": _worker_tags(context),
        **audit,
    }


@router.post("/attestations/jobs/{job_id}")
async def attest_action_success(
    job_id: str,
    body: ActionSuccessAttestationRequest,
    request: Request = None,
) -> Dict[str, Any]:
    """Promote a generic effect only from an independent scoped verifier."""

    queue = _get_queue()
    job = await queue.queue.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    authority = request_authority(request)
    executor_node = str(
        job.result.worker_node_id if job.result is not None else ""
    ).strip()
    executor_principal = str(
        (job.tags or {}).get("worker_authority_principal") or ""
    ).strip()
    if (
        not authority.authenticated
        or authority.anonymous
        or (
            executor_principal
            and authority.principal_id == executor_principal
        )
        or authority.principal_id == executor_node
    ):
        raise HTTPException(status_code=403, detail={
            "code": "independent_verifier_required",
            "message": (
                "the executor principal cannot attest its own action result"
            ),
        })
    from protagine.task_queue.action_receipts import (
        ActionReceiptAttestationV1,
    )

    receipt = ActionReceiptAttestationV1.from_payload(
        body.model_dump(by_alias=True)
    )
    result = await queue.queue.attest_action_success(
        job_id,
        attestation=receipt,
        verifier_identity=authority.principal_id,
        verifier_type="scoped_receipt_verifier",
    )
    if result is None:
        raise HTTPException(status_code=409, detail={
            "code": "action_attestation_rejected",
            "message": (
                "receipt does not match the pending action and claim attempt"
            ),
        })
    return {
        "success": True,
        "job_id": job_id,
        "claim_attempt_id": body.claim_attempt_id,
        "action_digest": body.action_digest,
        "verifier_identity": authority.principal_id,
        "schema": "ActionReceiptAttestationResultV1",
        "version": 1,
        **result,
    }


@router.post("/jobs/{job_id}/fail")
async def fail_job(
    job_id: str,
    body: JobFailRequest,
    request: Request = None,
) -> Dict[str, Any]:
    """Mark a job as failed."""
    queue = _get_queue()
    job = await queue.queue.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    worker_id = job.claimed_by or (
        job.result.worker_node_id
        if (
            job.status in {JobStatus.QUEUED, JobStatus.FAILED}
            and job.result is not None
            and job.result.status is JobStatus.FAILED
            and job.result.claim_attempt_id == body.claim_attempt_id
        )
        else None
    )
    if not worker_id:
        raise HTTPException(status_code=409, detail={
            "code": "job_not_claimed", "message": "job has no active claimant",
        })
    context = _worker_request_context(
        request, node_id=worker_id, required_scope="workers:lifecycle", claimant=True,
    )
    transitioned = await queue.queue.fail_job(
        job_id=job_id,
        worker_id=worker_id,
        error=body.error,
        started_at=None,
        claim_attempt_id=body.claim_attempt_id,
    )

    if not transitioned:
        raise HTTPException(status_code=409, detail={
            "code": "job_transition_failed",
            "message": "job is not claimed by this worker in a fail-able state",
        })

    stored = await queue.queue.get_job(job_id)

    return {
        "success": True,
        "job_id": job_id,
        "job_status": stored.status.value if stored is not None else "unknown",
        "replayed": job.claimed_by is None,
        "claim_attempt_id": body.claim_attempt_id,
        "worker_authority": _worker_tags(context),
    }


@router.post("/jobs/{job_id}/heartbeat")
async def job_heartbeat(
    job_id: str,
    body: JobHeartbeatRequest,
    request: Request = None,
) -> Dict[str, Any]:
    """Update job progress heartbeat."""
    queue = _get_queue()
    job = await queue.queue.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    worker_id = job.claimed_by
    if not worker_id:
        raise HTTPException(status_code=409, detail={
            "code": "job_not_claimed", "message": "job has no active claimant",
        })
    context = _worker_request_context(
        request, node_id=worker_id, required_scope="workers:lifecycle", claimant=True,
    )
    progress = {job_id: body.progress} if body.progress is not None else None
    updated = await queue.queue.send_heartbeat(
        worker_id,
        [job_id],
        progress=progress,
        claim_attempt_ids=(
            {job_id: body.claim_attempt_id} if body.claim_attempt_id else {}
        ),
    )
    if updated != 1:
        raise HTTPException(status_code=409, detail={
            "code": "worker_heartbeat_claim_mismatch",
            "message": "job is no longer claimed by this worker",
        })
    return {
        "success": True,
        "job_id": job_id,
        "claim_attempt_id": body.claim_attempt_id,
        "worker_authority": _worker_tags(context),
    }


@router.post("/jobs/{job_id}/release")
async def release_job(
    job_id: str,
    body: JobReleaseRequest,
    request: Request = None,
) -> Dict[str, Any]:
    """Release a claimed job back to the queue."""
    queue = _get_queue()
    job = await queue.queue.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    claim_attempt_id = body.claim_attempt_id
    worker_id = job.claimed_by
    replayed = False
    if not worker_id and claim_attempt_id is not None:
        worker_id = await queue.queue.worker_for_claim_attempt(
            job_id, claim_attempt_id,
        )
        replayed = worker_id is not None
    if not worker_id:
        raise HTTPException(status_code=409, detail={
            "code": "job_not_claimed", "message": "job has no active claimant",
        })
    context = _worker_request_context(
        request, node_id=worker_id, required_scope="workers:lifecycle", claimant=True,
    )
    if not await queue.queue.release_job(
        job_id, worker_id, claim_attempt_id=claim_attempt_id,
    ):
        raise HTTPException(status_code=409, detail={
            "code": "job_transition_failed",
            "message": "job is not claimed by this worker in a releasable state",
        })
    return {
        "success": True,
        "job_id": job_id,
        "claim_attempt_id": claim_attempt_id,
        "replayed": replayed,
        "worker_authority": _worker_tags(context),
    }


@router.get("/jobs/blocked")
async def list_blocked_jobs(
    task_type: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    after: Optional[str] = None,
    response: Response = None,
) -> List[Dict[str, Any]]:
    """List BLOCKED jobs awaiting owner approval (v0.17.0), optionally by type.

    Dependency-blocked jobs are excluded — they resolve automatically
    when their dependencies complete.
    """
    if after is not None and not is_canonical_job_id(after):
        raise HTTPException(status_code=422, detail={
            "code": "invalid_blocked_jobs_cursor",
            "message": "after must be a canonical queue job ID",
        })
    queue = _get_queue()
    jobs = await queue.queue.get_jobs_by_status(JobStatus.BLOCKED)
    canonical_jobs = []
    legacy_count = 0
    for job in jobs:
        if job.tags.get("blocked_reason", "") != "awaiting_owner_approval":
            continue
        if task_type and job.job_type.value != task_type:
            continue
        if is_canonical_job_id(job.job_id):
            canonical_jobs.append(job)
        else:
            legacy_count += 1
    if response is not None:
        response.headers["X-Protagine-Blocked-Legacy-Count"] = str(legacy_count)

    items = []
    for job in sorted(canonical_jobs, key=lambda item: item.job_id):
        blocked_reason = job.tags.get("blocked_reason", "")
        if after is not None and job.job_id <= after:
            continue
        # A projection only; polling it never creates or changes authority.
        payload = job.payload if isinstance(job.payload, dict) else {}
        items.append({
            "id": job.job_id,
            "action_hint": payload.get("action_hint"),
            "risk": str(payload.get("risk") or payload.get("risk_class") or "unknown"),
            "description": str(
                payload.get("description") or payload.get("summary") or payload.get("title") or ""
            )[:500],
            "created_at": job.posted_at.isoformat() if job.posted_at else None,
            "blocked_reason": blocked_reason,
            "approval_requested_at": job.tags.get("approval_requested_at"),
            "projection_status": "direct",
        })
        if len(items) >= limit:
            break
    return items


@router.post("/jobs/{job_id}/approve")
async def approve_job(
    job_id: str,
    body: JobApproveRequest,
    request: Request = None,
) -> Dict[str, Any]:
    """Compatibility approval endpoint backed by immutable authority state.

    ``approved_by`` is accepted only so existing clients keep parsing; it is
    ignored. The decision actor is derived from the authenticated principal.
    ``always`` requests the safe grant defaults under the deployment's
    configured exact-scope envelope; it never creates an action-name bypass.
    """
    queue = _get_queue()
    job = await queue.queue.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    result = await _decide_job(
        job=job,
        decision="approve",
        decision_id=body.decision_id,
        request=request,
    )
    # Response aliases keep old integrations operational while exposing the
    # new durable model. Both aliases contain the same exact-scope grant.
    result["approved_by"] = result["decided_by"]
    result["approved_at"] = result["decided_at"]
    result["standing_approval"] = result["bounded_grant"]
    logger.info("Job %s approved by principal %s", job_id, result["decided_by"])
    return result


@router.post("/jobs/{job_id}/reject")
async def reject_job(
    job_id: str,
    body: JobRejectRequest,
    request: Request = None,
) -> Dict[str, Any]:
    """Reject a BLOCKED job using server-derived decision authority."""
    queue = _get_queue()
    job = await queue.queue.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    result = await _decide_job(
        job=job,
        decision="reject",
        decision_id=body.decision_id,
        request=request,
        rejection_reason=body.reason,
    )
    result["rejected_by"] = result["decided_by"]
    result["reason"] = body.reason
    logger.info("Job %s rejected by principal %s", job_id, result["decided_by"])
    return result


@router.get("/jobs/pending")
async def list_pending_jobs(
    limit: int = Query(50, ge=1, le=200),
    task_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List pending (queued + claimed + running + blocked) jobs."""
    queue = _get_queue()
    jobs: List[Job] = []
    jobs.extend(await queue.queue.get_jobs_by_status(JobStatus.QUEUED))
    jobs.extend(await queue.queue.get_jobs_by_status(JobStatus.CLAIMED))
    jobs.extend(await queue.queue.get_jobs_by_status(JobStatus.RUNNING))
    jobs.extend(await queue.queue.get_jobs_by_status(JobStatus.BLOCKED))

    items = []
    for job in jobs:
        if task_type and job.job_type.value != task_type:
            continue
        items.append(_job_to_dict(job))
    return items[:limit]


@router.get("/jobs/completed")
async def list_completed_jobs(
    since: Optional[str] = None,
    task_type: Optional[str] = None,
    limit: int = Query(20, ge=1, le=200),
) -> List[Dict[str, Any]]:
    """List completed jobs since a timestamp, optionally filtered by type."""
    queue = _get_queue()
    since_dt = _parse_dt(since) or datetime.min.replace(tzinfo=timezone.utc)
    completed = await queue.queue.get_completed_jobs_since(
        since_dt, limit=limit, job_type=task_type)
    return completed


@router.get("/jobs/neutral")
async def list_neutral_jobs(
    task_type: Optional[str] = None,
    limit: int = Query(20, ge=1, le=200),
) -> List[Dict[str, Any]]:
    """List terminal jobs that still need independent verification."""

    queue = _get_queue()
    jobs = await queue.queue.get_jobs_by_status(JobStatus.NEUTRAL)
    return [
        _job_to_dict(job) for job in jobs
        if not task_type or job.job_type.value == task_type
    ][:limit]


@router.get("/inspection/jobs/{job_id}")
async def inspect_job(job_id: str) -> Dict[str, Any]:
    """Read one exact job for authenticated executor canary attestation."""

    job = await _get_queue().queue.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_inspection_dict(job)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@router.get("/governor")
async def governor_status() -> Dict[str, Any]:
    """Worker-governor status (item 5): enforcement mode + per-job-type
    earned-trust stages."""
    gov = _governor()
    queue = _get_queue()
    central = await queue.queue.governance_status()
    if gov is None:
        return {"available": False, "central": central}
    try:
        return {"available": True, **gov.status(), "central": central}
    except Exception as exc:
        return {"available": True, "error": str(exc), "central": central}


@router.get("/stats")
async def queue_stats(request: Request = None) -> Dict[str, Any]:
    """Return queue statistics."""
    queue = _get_queue()
    stats = await queue.queue.get_queue_stats()
    scheduler = (
        getattr(request.app.state, "queue_scheduler", None)
        if request is not None else None
    )
    worker_truth = {
        "registered_workers": stats.registered_workers,
        "active_workers": stats.active_workers,
        "stale_workers": stats.stale_workers,
        "available_workers": stats.available_workers,
        "worker_heartbeat_ttl_secs": stats.worker_heartbeat_ttl_secs,
    }
    return {
        "by_status": stats.by_status,
        "by_type": stats.by_type,
        "total_workers": stats.total_workers,
        "available_workers": stats.available_workers,
        "registered_workers": stats.registered_workers,
        "active_workers": stats.active_workers,
        "stale_workers": stats.stale_workers,
        "worker_heartbeat_ttl_secs": stats.worker_heartbeat_ttl_secs,
        "last_user_message_at": load_last_user_message_at(),
        # Reuse the same worker snapshot so a heartbeat on the TTL boundary
        # cannot make top-level and nested readiness disagree in one response.
        "governance": await queue.queue.governance_status(worker_truth),
        "scheduler": (
            scheduler.health
            if scheduler is not None else {
                "running": False,
                "healthy": False,
                "last_tick_at": None,
                "last_error": "scheduler_unavailable",
                "tick_count": 0,
            }
        ),
    }


@router.get("/digest")
async def get_digest(
    hours: int = Query(6, ge=1, le=48),
) -> Dict[str, Any]:
    """Return a digest of completed and failed jobs in the last N hours."""
    queue = _get_queue()
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    digest = await queue.queue.get_digest_jobs(since)

    completed_lines = []
    for job in digest.get("completed", []):
        payload = job.get("payload", {})
        desc = payload.get("description", job["job_id"])
        completed_lines.append(f"✓ {desc}")

    failed_lines = []
    for job in digest.get("failed", []):
        payload = job.get("payload", {})
        desc = payload.get("description", job["job_id"])
        err = job.get("error", "unknown error")
        failed_lines.append(f"⚠ {desc} — {err}")

    neutral_lines = []
    for job in digest.get("neutral", []):
        payload = job.get("payload", {})
        desc = payload.get("description", job["job_id"])
        reason = (job.get("tags") or {}).get(
            "governor_outcome_reason", "needs independent verification"
        )
        neutral_lines.append(f"? {desc} — {reason}")

    return {
        "period_hours": hours,
        "since": since.isoformat(),
        "completed_count": len(digest.get("completed", [])),
        "failed_count": len(digest.get("failed", [])),
        "needs_verification_count": len(digest.get("neutral", [])),
        "completed": completed_lines,
        "failed": failed_lines,
        "needs_verification": neutral_lines,
    }
