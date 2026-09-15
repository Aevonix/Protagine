"""Execution observations reuse turn-writer and context-reader authority."""
import os
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from protagine.api.authority import request_authority, resolve_request_person
from protagine.turns.executions import registry
from protagine.api.schemas.host import SourceAnnotationCheck, SourceReference

router = APIRouter(prefix="/v1/host/executions", tags=["executions"])


class ExecutionRuntimeObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal['start', 'response', 'error']
    request_id: str = Field(default='', max_length=256, pattern=r'^[^\x00-\x1f]*$')
    requested_model: str = Field(default='', max_length=256, pattern=r'^[^\x00-\x1f]*$')
    provider: str = Field(default='', max_length=256, pattern=r'^[^\x00-\x1f]*$')
    response_model: str = Field(default='', max_length=256, pattern=r'^[^\x00-\x1f]*$')
    api_mode: str = Field(default='', max_length=256, pattern=r'^[^\x00-\x1f]*$')
    profile_id: str = Field(default='', pattern=r'^(?:[a-f0-9]{64})?$')
    runtime_kind: Literal['turn', 'cron', 'kanban_worker', 'delegated_child', 'unknown'] = 'unknown'
    approx_input_tokens: int | None = Field(default=None, ge=0, le=2147483647)
    max_tokens: int | None = Field(default=None, ge=0, le=2147483647)
    output_limit_kind: Literal['request', 'provider_default', 'unknown'] = 'unknown'
    tool_count: int | None = Field(default=None, ge=0, le=2147483647)
    api_call_count: int | None = Field(default=None, ge=0, le=2147483647)
    retry_count: int | None = Field(default=None, ge=0, le=2147483647)
    started_at: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    ended_at: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class ExecutionInputReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=256)
    input_message_hash: str = Field(pattern=r'^[a-f0-9]{64}$')


class ExecutionTaskExperience(BaseModel):
    model_config = ConfigDict(extra='forbid')
    task_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    purpose: Literal['operational', 'qualification', 'unclassified']
    origin_platform: str = Field(min_length=1, max_length=64, pattern=r'^[a-z0-9_.-]+$')


class ExecutionObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    execution_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    contact_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f]+$")
    turn_id: str = Field(min_length=1, max_length=256)
    parent_execution_id: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    platform: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_.-]+$")
    state: Literal["observed", "completed", "failed", "interrupted", "ended"] = "observed"
    phase: Literal["turn", "model", "tool", "between_calls", "ended"] = "turn"
    tool_name: str = Field(default="", max_length=128, pattern=r"^[a-zA-Z0-9_.:-]*$")
    sequence: int = Field(ge=1, le=2147483647)
    runtime: ExecutionRuntimeObservation | None = None
    # Exact already-admitted human inputs, never native task wrappers or titles.
    input_refs: list[ExecutionInputReference] | None = Field(default=None, min_length=1, max_length=64)
    task_experience: ExecutionTaskExperience | None = None


class AssessmentDocument(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=256, pattern=r'^[^\x00-\x1f]+$')
    content: str = Field(min_length=1, max_length=16000)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class ExecutionAssessment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    execution_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    task_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    contact_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    turn_id: str = Field(min_length=1, max_length=256)
    input_refs: list[ExecutionInputReference] = Field(min_length=1, max_length=64)
    runtime_source_ref: SourceReference
    # Preserve the complete recorded output lineage, as ordinary turn capture
    # does; the canonical source envelope retains its existing 8 MiB bound.
    source_refs: list[SourceReference] = Field(min_length=1)
    # Exact host-observed message membership, using the ordinary source reader
    # contract. Omitted sources retain the conservative whole-source check.
    annotation_checks: list[SourceAnnotationCheck] = Field(default_factory=list, max_length=512)
    assessed_at: AwareDatetime
    reviewer_identity: str = Field(default='unknown', min_length=1, max_length=256)
    reviewer_model: str = Field(default='unknown', min_length=1, max_length=256)
    artifact: AssessmentDocument
    assessment: AssessmentDocument
    context_documents: list[AssessmentDocument] = Field(default_factory=list, max_length=4)


class AssessmentRead(BaseModel):
    model_config = ConfigDict(extra='forbid')
    contact_id: str = Field(min_length=1, max_length=256)
    source_refs: list[SourceReference] = Field(default_factory=list, max_length=16)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=16, ge=1, le=16)


def authorized_viewer(request: Request, contact_id: str, *, scope: str) -> tuple[str, bool]:
    authority = request_authority(request)
    # Legacy body-selected identity is deliberately not sufficient for this new
    # shared surface. Use the existing exact person grants, never an owner flag.
    if not authority.authenticated or authority.anonymous or authority.legacy or not authority.has_scope(scope):
        raise HTTPException(403, detail={"code": "scoped_execution_authority_required"})
    person = resolve_request_person(request, claimed_person_id=contact_id)
    owner = os.environ.get("PROTAGINE_OWNER_PERSON_ID", "").strip() or os.environ.get("PROTAGINE_OWNER_CONTACT_ID", "").strip()
    return person, bool(owner and person == owner)


@router.post("/observe")
def observe(body: ExecutionObservation, request: Request):
    person, _ = authorized_viewer(request, body.contact_id, scope="turns:write")
    try:
        return registry().observe(body.model_dump(), principal_id=request_authority(request).principal_id, contact_id=person)
    except ValueError as exc:
        raise HTTPException(409, detail={"code": str(exc)}) from exc


@router.post('/assess')
def assess(body: ExecutionAssessment, request: Request):
    person, owner = authorized_viewer(request, body.contact_id, scope='turns:write')
    if not owner:
        raise HTTPException(403, detail={'code': 'owner_task_assessment_required'})
    from protagine.self_model.task_assessments import admit
    try:
        return admit(registry(), body.model_dump(mode='json'),
            principal_id=request_authority(request).principal_id, contact_id=person)
    except ValueError as exc:
        raise HTTPException(409, detail={'code': str(exc)}) from exc


@router.post('/assessments/read')
def assessments(body: AssessmentRead, request: Request):
    person, owner = authorized_viewer(request, body.contact_id, scope='context:read')
    if not owner:
        raise HTTPException(403, detail={'code': 'owner_task_assessment_required'})
    from protagine.self_model.task_assessments import read_assessments
    return read_assessments(registry(), contact_id=person, offset=body.offset, limit=body.limit,
        source_refs=[ref.model_dump() for ref in body.source_refs])


@router.get("")
async def active(request: Request, contact_id: str, session_id: str = "", limit: int = Query(20, ge=1, le=100),
                 projection: Literal['full', 'request'] = 'full', input_context: bool = False,
                 reserve_chars: int = Query(0, ge=0, le=1200)):
    person, owner = authorized_viewer(request, contact_id, scope="context:read")
    if projection == 'request' and not owner:
        raise HTTPException(403, detail='owner_work_context_required')
    if projection == 'request':
        limit = min(limit, 8)
    view = registry().view(contact_id=person, owner=owner, session_id=session_id, limit=limit,
                           include_ancestors=projection == 'request',
                           include_inputs=projection == 'full' or input_context)
    view = await with_queue_work(view, owner=owner, limit=limit)
    if projection == 'request':
        from protagine.turns.executions import request_work_context
        result = request_work_context(view, limit=limit, session_id=session_id)
        if reserve_chars:
            # One scoped read supplies both choices. The adapter uses the
            # smaller view only when an authorized local task revision exists.
            result['reserved'] = request_work_context(view, limit=max(1, limit-1),
                max_chars=4000-reserve_chars, session_id=session_id)
        return result
    return view


async def with_queue_work(view, *, owner, limit=8):
    if not owner:
        return view
    import asyncio
    from protagine.turns.hermes_work import cron_view
    from protagine.turns.local_work import local_work_view
    from protagine.turns.hermes_kanban import kanban_view
    from protagine.turns.reported_workers import reported_worker_view
    from protagine.api.routers import host
    from protagine.turns.executions import work_source_coverage
    import time

    queue = getattr(host._task_queue, 'queue', host._task_queue)

    async def read(reader, *, asynchronous=False, **reader_kwargs):
        # These are independent read-only snapshots. One unavailable ledger
        # must not hide work observed by all the other readers. Cancellation of
        # a thread await does not stop its read; native readers bound SQLite work.
        try:
            operation = reader(limit=limit, **reader_kwargs) if asynchronous else asyncio.to_thread(reader, limit=limit, **reader_kwargs)
            value = await asyncio.wait_for(operation, timeout=.2)
            if value is None:
                return None
            return {**value, 'observed_at': time.time()}
        except Exception:
            return {'items': [], 'recent': [], 'available': False, 'unavailable': True,
                    'reason': 'work_source_unavailable', 'observed_at': time.time()}

    async def queue_view():
        if queue is None or not callable(getattr(queue, 'current_work', None)):
            return {'items': [], 'available': False, 'unavailable': True,
                    'reason': 'queue_not_attached', 'source': 'canonical_task_queue'}
        return await read(queue.current_work, asynchronous=True)

    # All sources share the existing request budget, rather than each taking
    # another sequential budget after the native ledgers finish.
    view['native_cron'], view['local_work'], view['native_kanban'], reported, view['worker_work'] = await asyncio.gather(
        # Leave executor/return slack for the multi-board reader to retain
        # faster boards when its own partial-read budget is exhausted.
        read(cron_view), read(local_work_view), read(kanban_view, read_budget=.15),
        read(reported_worker_view), queue_view())
    if reported is not None:
        view['reported_worker'] = reported
    view['work_sources'] = work_source_coverage(view)
    view['coverage'] = 'registered Hermes turns and selected work ledgers; see work_sources for read coverage'
    return view
