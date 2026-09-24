"""Protagine sidecar host router — ``/v1/host`` API surface.

This is the contract used by external agent harnesses (OpenClaw and any
future shim) to mount Protagine's intelligence as a plugin.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from protagine.goals.store import GoalNotFoundError
from protagine import get_state_dir
from protagine.instance import instance_id
from protagine.events.stream import EventSubscriberBuffer
from protagine.api.auth import (
    owner_person_id,
    request_authority,
    resolve_request_person,
    resolve_turn_person,
)

from protagine.turns.tool_observations import ToolObservation
from protagine.api.schemas.host import (
    HostIdentity,
    HostMessage,
    HostTurnContext,
    HostConfigureRequest,
    HostConfigureResponse,
    ModelInfo,
    ModelListResponse,
    BackfillRequest,
    BackfillResponse,
    BriefingListResponse,
    BriefingResponse,
    ContactCreateRequest,
    ContactIntroRequest,
    ContactIntroResponse,
    ContactListResponse,
    ContactResponse,
    ContactStyleRequest,
    ContactStyleResponse,
    ContactTimezoneRequest,
    ScopeAuthzResponse,
    ScopeCreateRequest,
    ScopeDeactivateRequest,
    ScopePromoteRequest,
    ScopeResponse,
    SourceReference,
    SourceAnnotationCheck,
    SourceInputReference,
    ContextAssembleRequest,
    ContextAssembleResponse,
    ContextSection,
    TemporalConfigRequest,
    TemporalConfigResponse,
    TemporalContact,
    TemporalContactsResponse,
    TimelineEvent,
    TimelineResponse,
    EmbedHealthResponse,
    GoalListResponse,
    GoalResponse,
    GoalUpdateRequest,
    HostHealthResponse,
    ImageBatchEmbedRequest,
    ImageBatchEmbedResponse,
    ImageEmbedRequest,
    ImageEmbedResponse,
    IndexRequest,
    IndexResponse,
    InsightResponse,
    InsightsListResponse,
    LearningCorrectionRequest,
    MemoryEmbedRequest,
    MemoryEmbedResponse,
    MemoryReadRequest,
    MemoryReadResponse,
    MemorySearchRequest,
    MemorySearchResponse,
    MemoryRecentRequest,
    MemoryRecentResponse,
    RerankRequest,
    RerankResponse,
    RerankResult,
    MigrateRequest,
    MigrateResponse,
    MultimodalSearchRequest,
    MultimodalSearchResponse,
    ResearchListResponse,
    ResearchRunResponse,
    ResearchStartRequest,
    SecretDeleteRequest,
    SecretDeleteResponse,
    SecretGetRequest,
    SecretGetResponse,
    SecretListRequest,
    SecretListResponse,
    SecretSetRequest,
    SecretSetResponse,
    SynthesisConnection,
    SynthesisDiscoverRequest,
    SynthesisDiscoverResponse,
    TurnSyncRequest,
    TurnSyncResponse,
    CommitmentCreateRequest,
    CommitmentListResponse,
    CommitmentResponse,
    CommitmentUpdateRequest,
    ConcernResolveRequest,
    AffectEventCreateRequest,
    AffectEventResponse,
    AffectStateResponse,
    AffectEventListResponse,
    SharedFactCreateRequest,
    SharedFactUpdateRequest,
    SharedFactResponse,
    SharedFactListResponse,
    PatternCreateRequest,
    PatternResponse,
    PatternListResponse,
    PatternUpdateRequest,
    PatternExtractResponse,
    # Multi-Agent v0.7.0
    AgentInviteRequest,
    AgentInviteResponse,
    AgentConnectRequest,
    AgentConnectResponse,
    AgentNodeCert,
    AgentRegisterRequest,
    AgentRegisterResponse,
    AgentHeartbeatRequest,
    AgentMetadataSchema,
    AgentResponse,
    AgentListResponse,
    AgentHealthResponse,
    AgentUpdateRequest,
    InitiativeCreateRequest,
    InitiativeResponse,
    InitiativeListResponse,
    InitiativeClaimRequest,
    InitiativeCompleteRequest,
    InitiativeFailRequest,
    InitiativeDelegateRequest,
    InitiativePriorityRequest,
    # Agent Snapshot
    AgentSnapshotInitiative,
    AgentSnapshotResponse,
    RecordOutreachRequest,
    RecordOutreachResponse,
    # Session Context Architecture
    AgentSnapshotSystemState,
    SessionReportRequest,
    SessionReportResponse,
    ContextDigestSessionReport,
    ContextDigestResponse,
)

logger = logging.getLogger(__name__)

# Background task bookkeeping — prevents garbage-collection of fire-and-forget
# asyncio tasks (see https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task)
_background_tasks: set[asyncio.Task] = set()


def _spawn_task(coro) -> asyncio.Task:
    """Create an asyncio task, retain a reference, and auto-discard on completion."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _to_dict(obj):
    """Convert Pydantic models or other objects to plain dicts."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in obj.__dict__.items() if not k.startswith("_")}
    if isinstance(obj, dict):
        return obj
    return {}

router = APIRouter(prefix="/v1/host", tags=["host"])
v2_router = APIRouter(prefix="/v2/host", tags=["host-v2"])

# ---------------------------------------------------------------------------
# Module-level wiring — subsystems are injected by the server lifespan
# ---------------------------------------------------------------------------

_embedder = None
_event_subscribers: list[EventSubscriberBuffer] = []
_event_broadcast_lock = threading.RLock()


def _event_subscriber_queue_size() -> int:
    """Configured bound for each live event subscriber."""
    raw = os.environ.get("PROTAGINE_EVENT_SUBSCRIBER_QUEUE_SIZE", "256")
    try:
        size = int(raw)
    except ValueError:
        logger.warning(
            "Invalid PROTAGINE_EVENT_SUBSCRIBER_QUEUE_SIZE=%r; using 256", raw
        )
        return 256
    if size < 1:
        logger.warning(
            "PROTAGINE_EVENT_SUBSCRIBER_QUEUE_SIZE must be positive; using 256"
        )
        return 256
    return min(size, 10_000)


def broadcast_event(event: dict) -> Optional[dict]:
    """Persist, canonicalize, then publish an event to live subscribers.

    Called by the autonomy loop and other subsystems
    when state changes that the host should know about (proactive
    messages, briefings, anomalies, etc.).  A journal failure suppresses the
    live frame: clients must never observe an event which cannot be replayed.
    """
    from protagine.events.journal import append_event_record

    event_type = str(event.get("type", "unknown"))
    payload = event.get("payload") or {}
    occurred_at = str(event.get("occurred_at") or "") or None

    # This lock preserves journal-sequence order in local live buffers when
    # emitters run concurrently on request handlers or worker threads.
    with _event_broadcast_lock:
        record = append_event_record(
            event_type,
            payload,
            occurred_at=occurred_at,
        )
        if record is None:
            logger.error(
                "Suppressing live event %s because journal append failed",
                event_type,
            )
            return None

        frame = {
            **event,
            "type": event_type,
            "payload": payload,
            "occurred_at": record["occurredAt"],
            "recordedAt": record["recordedAt"],
            "seq": record["seq"],
            "eventId": record["ulid"],
        }
        for subscriber in tuple(_event_subscribers):
            try:
                subscriber.publish(dict(frame))
            except Exception:
                logger.exception(
                    "Failed to enqueue event seq=%s for one subscriber",
                    record["seq"],
                )
        return frame


#: Why semantic recall is off although it was configured: set by the server when the
#: embedder or the vector store failed to initialise, cleared when they come up.
_embed_failure: Optional[str] = None


def set_embed_failure(reason: Optional[str]) -> None:
    global _embed_failure
    _embed_failure = str(reason).strip() if reason else None


def set_embedder(embedder) -> None:
    global _embedder
    _embedder = embedder


def _mind():
    """The running mind, or None (wired by the server lifespan)."""
    from protagine.api.routers.mind import get_mind
    return get_mind()


def _mind_section() -> str:
    """The owner's Mind section: broadcast concerns, open goals and open asks, or nothing."""
    mind = _mind()
    if mind is None:
        return ""
    try:
        return str(mind.section() or "")
    except Exception:
        logger.debug("mind section unavailable", exc_info=True)
        return ""


def _mind_note_novel(query_text: str) -> None:
    """An owner turn memory recalled nothing for: a new topic, a little curiosity for the agent's own
    affect at the next tick (architecture 4.3). A no-op without a mind; never breaks the turn."""
    feelings = getattr(_mind(), "feelings", None)
    if feelings is None:
        return
    try:
        feelings.note_novel_topic(query_text)
    except Exception:
        logger.debug("novel topic not noted", exc_info=True)


def _mind_stances(query_text: str, *, viewer_contact_id: str, viewer_is_owner: bool, session_id: str) -> str:
    """Recorded views for this turn (architecture 4.4), audience-filtered for the viewer; nothing when the
    mind is off. Who the viewer is comes from ``_assemble_sections`` (the viewer identity every other
    owner-only section uses), so a recipient packet gets only the views meant for that recipient."""
    mind = _mind()
    opinions = getattr(mind, "opinions", None) if mind is not None else None
    if opinions is None or not mind.enabled or not viewer_contact_id:
        return ""
    try:
        return str(opinions.context(query_text, viewer_contact_id=viewer_contact_id,
                                    viewer_is_owner=bool(viewer_is_owner),
                                    session_id=session_id or "") or "")
    except Exception:
        logger.debug("stance section unavailable", exc_info=True)
        return ""


def _mind_lessons(query_text: str, *, viewer_is_owner: bool, owner_turn: bool, session_id: str) -> str:
    """The one lesson relevant to the owner's own turn (architecture 4.8), or nothing: never for a guest,
    a recipient packet (session ``mind:<contact>``), with the mind off or with ``faculties.lessons`` off.
    Rendering it logs the lesson's use in this session, which the owner's verdict later scores."""
    mind = _mind()
    lessons = getattr(mind, "lessons", None) if mind is not None else None
    if (lessons is None or not mind.enabled or not lessons.enabled or not viewer_is_owner or not owner_turn
            or not session_id or str(session_id).startswith("mind:")):
        return ""
    try:
        text, _ = lessons.for_turn(query_text, session_id=str(session_id))
        return str(text or "")
    except Exception:
        logger.debug("lesson section unavailable", exc_info=True)
        return ""


def _mind_recall_query(query_text: str) -> str:
    """The recall query plus the broadcast concerns (the workspace expands recall; broadcast flag)."""
    mind = _mind()
    if mind is None or not query_text:
        return query_text
    try:
        extra = " ".join(concern.summary for concern in mind.broadcast())[:400]
    except Exception:
        logger.debug("mind broadcast unavailable", exc_info=True)
        return query_text
    return f"{query_text} {extra}".strip() if extra else query_text


def _mind_posture() -> tuple[str, bool]:
    """(autonomy level, running) for the status routes that used to read the loop."""
    mind = _mind()
    if mind is None:
        return "unknown", False
    return str(mind.level), bool(mind.enabled)


_llm_router = None


def set_llm_router(router) -> None:
    global _llm_router
    _llm_router = router


def get_llm_router():
    """Current LLMRouter instance (None until the host configures one)."""
    return _llm_router


_telemetry = None


def set_telemetry(telemetry) -> None:
    global _telemetry
    _telemetry = telemetry


def supported_capabilities() -> List[str]:
    """Return the list of capabilities this sidecar advertises."""
    caps: list[str] = ["memory"]
    if _embedder is not None:
        caps.append("embed")
    if _goals_store is not None:
        caps.append("goals")
    if _contacts_store is not None:
        caps.append("contacts")
    if _briefings_engine is not None:
        caps.append("briefings")
    if _metalearner is not None:
        caps.append("cognition")
    if _situation_store is not None and _situation_reducer is not None:
        caps.append("situation")
    if _research_pipeline is not None:
        caps.append("research")
    if _connection_discoverer is not None:
        caps.append("synthesis")
    if _secrets_manager is not None:
        caps.append("secrets")
    if _mind() is not None:
        caps.append("mind")
    if _session_store is not None:
        caps.append("sessions")
    caps.append("events")
    if _commitment_store is not None:
        caps.append("commitments")
        readiness = getattr(
            _commitment_store, "resolution_recovery_readiness", None,
        )
        if readiness is not None:
            try:
                status = readiness()
                capability = status.get("capability")
                if status.get("ready") is True and capability:
                    caps.append(str(capability))
            except Exception:
                pass
    if _affect_store is not None:
        caps.append("affect")
    if _facts_store is not None:
        caps.append("shared_facts")
    if _pattern_store is not None:
        caps.append("patterns")
    if _reranker is not None:
        caps.append("rerank")
    caps.append("context")
    caps.append("event_journal")
    return caps


# ---------------------------------------------------------------------------
# Host Configuration (LLM from host)
# ---------------------------------------------------------------------------


@router.post("/configure", response_model=HostConfigureResponse)
async def configure_host(body: HostConfigureRequest) -> HostConfigureResponse:
    """Receive LLM configuration from the host.

    The host (OpenClaw, Hermes, etc.) calls this on startup to provide
    its LLM provider credentials and model assignments. Protagine does not
    manage its own LLM keys — it inherits them from the host.

    Validate and persist one configuration, then atomically update the shared
    router. Existing consumer references and in-flight snapshots are retained.
    """
    if body.llm is None:
        return HostConfigureResponse(configured=False)

    from protagine.router.router import LLMRouter
    import os
    import tempfile

    try:
        prepared = LLMRouter(tiers={})
        prepared.configure(body.llm)
        config_path = get_state_dir() / ".protagine-llm-config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        fd, pending = tempfile.mkstemp(prefix=".llm-config-", dir=config_path.parent)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump(body.llm, output, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(pending, config_path)
        finally:
            if os.path.exists(pending):
                os.unlink(pending)
        # Keep every extractor/thinker/worker reference live. In-flight calls
        # retain their immutable snapshot; subsequent calls see this revision.
        if _llm_router is not None:
            _llm_router.adopt_configuration(prepared, config_path=config_path)
            new_router = _llm_router
        else:
            prepared.watch_config(config_path)
            new_router = prepared
            set_llm_router(new_router)
        return HostConfigureResponse(
            configured=True, provider=body.llm.get("provider"),
            models=body.llm.get("models", {}), routing=new_router.routing_status())
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Invalid host model configuration") from exc
    except Exception as exc:
        logger.error("configure_host failed: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Model configuration update failed") from exc


@router.get("/models", response_model=ModelListResponse)
async def list_models() -> ModelListResponse:
    """List available LLM models for the currently configured provider.

    For local providers (Ollama, vLLM, LM Studio, etc.), this queries the
    local server and returns the actual models that are installed.  For
    cloud providers an empty list is returned — the host is expected to
    know which cloud models exist.
    """
    # Load persisted host config to know the current provider/base_url
    from protagine.router.tiers import discover_local_models

    config_path = get_state_dir() / ".protagine-llm-config.json"
    provider = ""
    base_url = ""
    api_key = ""
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text())
            provider = cfg.get("provider", "")
            base_url = cfg.get("baseUrl", "")
            api_key = cfg.get("apiKey", "")
        except Exception as exc:
            logger.debug("Could not read persisted LLM config: %s", exc)

    if _llm_router is not None and _llm_router.supports_function_routing:
        observed = await _llm_router.discover_models()
        if observed is not None:
            models = observed['models']
            return ModelListResponse(provider=observed['provider'], base_url=observed['base_url'],
                models=[ModelInfo(id=row['id'], provider=observed['provider'], owned_by=row.get('owned_by')) for row in models],
                discovered=bool(models), routing=observed['routing'],
                error=None if models else 'No fresh model advertisements are available; configured completion aliases remain unchanged.')

    if not provider:
        return ModelListResponse(
            routing=_llm_router.routing_status() if _llm_router is not None else None,
            provider="",
            error="No LLM provider configured. Call POST /v1/host/configure first.",
        )

    if provider not in ("ollama", "local", "custom", "lmstudio", "vllm"):
        return ModelListResponse(
            routing=_llm_router.routing_status() if _llm_router is not None else None,
            provider=provider,
            error="Model listing is only supported for local providers (ollama, local, custom, lmstudio, vllm).",
        )

    discovered = discover_local_models(provider, base_url, api_key)
    if discovered:
        return ModelListResponse(
            routing=_llm_router.routing_status() if _llm_router is not None else None,
            provider=provider,
            base_url=base_url or None,
            models=[
                ModelInfo(
                    id=m.get("name") or m.get("id", ""),
                    provider=provider,
                    size=m.get("size"),
                    owned_by=m.get("owned_by"),
                )
                for m in discovered
                if (m.get("name") or m.get("id"))
            ],
            discovered=True,
        )

    return ModelListResponse(
        routing=_llm_router.routing_status() if _llm_router is not None else None,
        provider=provider,
        base_url=base_url or None,
        error="Could not discover models from the local server. Is it running?",
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

_INDEX_HEALTH_TIMEOUT_SECONDS = 5.0
#: The mind's tick is stale after ten of its intervals, and never sooner than this.
TICK_STALE_FLOOR_HOURS = 0.25
#: A capture job still unfinished after this long means capture is not landing.
CAPTURE_STALE_HOURS = 1.0


def _tick_stale_hours(mind) -> float:
    """How long the mind's tick may be silent: ``PROTAGINE_STALE_TICK_HOURS`` when pinned,
    else ten of the mind's own intervals with a quarter-hour floor."""
    pinned = os.environ.get("PROTAGINE_STALE_TICK_HOURS", "").strip()
    if pinned:
        try:
            return float(pinned)
        except ValueError:
            pass
    try:
        interval = float(getattr(mind, "interval", 60.0) or 60.0)
    except (TypeError, ValueError):
        interval = 60.0
    return max(10 * interval / 3600.0, TICK_STALE_FLOOR_HOURS)


def _capture_stale_hours() -> float:
    pinned = os.environ.get("PROTAGINE_STALE_CAPTURE_HOURS", "").strip()
    if pinned:
        try:
            return float(pinned)
        except ValueError:
            pass
    return CAPTURE_STALE_HOURS


def _capture_backlog_hours(mind) -> Optional[float]:
    """How long the oldest unfinished capture job has waited, or None when nothing waits
    (or no capture queue is wired)."""
    probe = getattr(getattr(mind, "capture", None), "oldest_unfinished_seconds", None)
    if probe is None:
        return None
    try:
        age = probe()
    except Exception as exc:
        logger.warning("capture backlog probe failed: %s", type(exc).__name__)
        return None
    return None if age is None else float(age) / 3600.0


@router.get("/health", response_model=HostHealthResponse)
async def health() -> HostHealthResponse:
    caps = supported_capabilities()
    notes: dict[str, str] = {}
    problems: list[str] = []   # every reason the status is not "ok", in words
    embed_model = ""

    # the sidecar's own open-file limit (doctor reads this; a low limit makes
    # LanceDB vector recall fail under load — see check_server_fd_limit)
    try:
        import resource
        _fd_soft, _fd_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        notes["fd_limit"] = (
            "unlimited" if _fd_soft == resource.RLIM_INFINITY else str(_fd_soft))
    except Exception:
        pass

    memory_backend_down = False
    try:
        from protagine.turns import get_turn_idempotency_ledger
        from contextlib import closing
        ledger = get_turn_idempotency_ledger(get_state_dir())
        with closing(ledger._connect()) as conn:
            conn.execute('SELECT 1 FROM turn_sources LIMIT 1').fetchone()
        notes['memory'] = 'Canonical source ledger readable; semantic projection is optional'
    except Exception as exc:
        memory_backend_down = True
        caps = [c for c in caps if c != 'memory']
        notes['memory'] = 'Canonical source ledger unavailable (' + type(exc).__name__ + ')'
        problems.append(f"the source ledger is unreadable ({type(exc).__name__}: {exc})")
    if _goals_store is not None:
        notes["goals"] = "Goal records available"
    if _contacts_store is not None:
        notes["contacts"] = "ContactsStore wired"
    if _briefings_engine is not None:
        notes["briefings"] = "BriefingEngine wired"
    if _metalearner is not None:
        notes["cognition"] = "MetaLearner wired"
    embed_degraded = False
    if _embed_failure:
        # Semantic recall was configured and is not running: say so in words, so the
        # keyword fallback is never mistaken for the configured recall.
        embed_degraded = True
        problems.append("semantic recall is off: " + _embed_failure)
        if _embedder is None:
            notes["embed"] = "semantic recall is off: " + _embed_failure
    if _embedder is not None:
        # Get embed model info
        if hasattr(_embedder, "_provider") and hasattr(_embedder._provider, "_config"):
            embed_model = _embedder._provider._config.model_id
        embed_note = f"EmbeddingPipeline wired (model={embed_model})"

        # Managed generation identity governs semantic reads/writes. Verify it
        # and a bounded physical read; per-row legacy metadata discovery is an
        # explicit audit, not a prerequisite for every readiness request.
        try:
            from protagine.vector import get_store
            store = get_store()
            if store is None:
                raise RuntimeError("Managed vector store is unavailable")
            await asyncio.wait_for(
                store.check_index_health(_embedder.index_identity),
                timeout=_INDEX_HEALTH_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            embed_degraded = True
            embed_note += f" [index-check failed: {type(exc).__name__}: {exc}]"
            problems.append(f"the semantic index check failed: {type(exc).__name__}: {exc}")
            logger.warning("embedding index health probe failed: %s", type(exc).__name__)

        # Check embedder health
        try:
            hc = await _embedder.health_check()
            if hc.get("status") != "ok":
                embed_degraded = True
                embed_note += f" [health: {hc.get('status', 'unknown')}"
                if hc.get("error"):
                    embed_note += f": {hc['error']}"
                embed_note += "]"
                problems.append(f"the embedder is not answering correctly: {hc.get('error') or hc.get('status')}")
        except Exception as exc:
            embed_degraded = True
            embed_note += f" [health probe failed: {exc}]"
            problems.append(f"the embedder health probe failed: {exc}")
            logger.warning("embedder health probe failed: %s", exc)

        notes["embed"] = embed_note
    if _secrets_manager is not None:
        notes["secrets"] = "SecretsManager wired"
    if _research_pipeline is not None:
        notes["research"] = "ResearchPipeline wired"
    if _connection_discoverer is not None:
        notes["synthesis"] = "ConnectionDiscoverer wired"
    mind = _mind()
    if mind is not None:
        notes["mind"] = f"mind {'on' if mind.enabled else 'off'} (autonomy {mind.level}, ticks={mind.ticks})"
    if _session_store is not None:
        notes["sessions"] = "InMemorySessionStore wired"
    if _commitment_store is not None:
        if "commitment_resolution_recovery_v1" in caps:
            notes["commitments"] = (
                "CommitmentStore wired (resolution recovery v1 ready)"
            )
        elif hasattr(_commitment_store, "resolution_recovery_readiness"):
            notes["commitments"] = (
                "CommitmentStore wired (resolution recovery unavailable)"
            )
        else:
            notes["commitments"] = "CommitmentStore wired"
    if _affect_store is not None:
        notes["affect"] = "AffectStore wired"
    if _facts_store is not None:
        notes["shared_facts"] = "SharedFactsStore wired"
    if _pattern_store is not None:
        notes["patterns"] = "PatternStore wired"

    health_status = "ok"
    if embed_degraded or memory_backend_down:
        health_status = "degraded"
    if (
        _commitment_store is not None
        and hasattr(_commitment_store, "resolution_recovery_readiness")
        and "commitment_resolution_recovery_v1" not in caps
    ):
        health_status = "degraded"
        problems.append("commitment resolution recovery is unavailable")

    # Temporal metrics. What the sidecar runs on its own is tracked for staleness: the
    # mind's tick (it beats whether the mind is on or off) and the capture queue (jobs
    # that sit for hours are not landing). What inbound traffic drives (sync =
    # turns/sync, prefetch = context/assemble) is reported as silence and never flags:
    # a quiet day is not a failure, and a fresh install must be able to be ready.
    temporal = None
    try:
        if _telemetry is not None:
            temporal_data = await _telemetry.to_dict({"tick": _tick_stale_hours(mind)})
            flags = list(temporal_data.get("stale_flags") or [])
            silence = dict(temporal_data.get("silence_hours") or {})
            for flag in flags:
                if flag == "tick:never_ran":
                    problems.append("the mind's tick has not run since the sidecar started")
                elif flag == "tick":
                    problems.append(f"the mind's tick has not run for {silence.get('tick') or 0:.1f} h")
            backlog = _capture_backlog_hours(mind)
            if backlog is not None:
                silence["capture"] = backlog
                if backlog > _capture_stale_hours():
                    flags.append("capture")
                    problems.append("capture jobs are not landing: the oldest unfinished job has waited "
                                    f"{backlog:.1f} h")
            temporal_data["stale_flags"], temporal_data["silence_hours"] = flags, silence
            if flags:
                health_status = "degraded"
            from protagine.api.schemas.host import TemporalMetrics
            temporal = TemporalMetrics(**temporal_data)
    except Exception as exc:
        # If staleness cannot even be computed, the one probe that catches a
        # dead loop is gone — that is degradation, not silent health.
        health_status = "degraded"
        notes["temporal"] = f"staleness computation failed: {exc}"
        problems.append(f"staleness computation failed: {exc}")
        logger.warning("temporal staleness computation failed: %s", exc)

    return HostHealthResponse(
        status=health_status,
        capabilities=caps,
        notes=notes,
        temporal=temporal,
        problems=problems,
    )


def _require_person_authority(request: Request | None, person_id: str) -> None:
    """Memory reads need an authenticated key (never development mode) and a resolved person."""
    authority = request_authority(request)
    if (not authority.authenticated or authority.anonymous or not authority.principal_id
            or not str(person_id or "").strip()):
        raise HTTPException(status_code=403, detail={
            "code": "person_authority_required",
            "message": "memory reads require the API key and a resolved person",
        })


def _viewer_is_guest(request: Request | None, person_id: Optional[str]) -> bool:
    """A viewer other than the owner, which fails closed.

    A guest's context is contact-scoped: their own canonical sources, the
    commitments their sources prove shared, and their digest; never the
    owner's global or person-store context. Development mode (no key) is
    never the owner (``resolve_request_person`` refuses the owner's lane
    there), so it is a guest for anyone it names and for no one. With the key,
    no person is the key holder's own unscoped view.
    """
    authority = request_authority(request)
    person = str(person_id or "").strip()
    if authority.anonymous or not authority.authenticated:
        return True
    return bool(person and person != owner_person_id())


def _canonical_shared_commitments(rows, contact_id):
    """Expose only descriptions already present in this person's source evidence.

    person_id is a subject selector, not an audience grant. A metadata link
    alone is insufficient: the current person-scoped source must contain the
    displayed description. Unclassified legacy tasks stay owner-private.
    """
    from contextlib import closing
    from protagine.turns import get_turn_idempotency_ledger
    if not contact_id or not (Path(get_state_dir()) / "turn-idempotency.db").exists():
        return []
    visible = []
    ledger = get_turn_idempotency_ledger(get_state_dir())
    with closing(ledger._connect()) as conn:
        for row in rows:
            metadata = row.get("metadata")
            source_id = metadata.get("source_turn_id") if isinstance(metadata, dict) else None
            description = str(row.get("description") or "").strip()
            if row.get("person_id") != contact_id or not isinstance(source_id, str) or not description:
                continue
            source = conn.execute(
                "SELECT messages_json FROM turn_sources WHERE turn_id=? AND contact_id=? AND scope='person'",
                (source_id, contact_id)).fetchone()
            if source is None:
                continue
            for message in json.loads(source["messages_json"]):
                content = message.get("content")
                texts = ([content] if isinstance(content, str) else
                         [block["text"] for block in content if isinstance(block, dict)
                          and block.get("type") in {"text", "input_text", "output_text"}
                          and isinstance(block.get("text"), str)] if isinstance(content, list) else [])
                if any(description.casefold() in text.casefold() for text in texts):
                    visible.append(row)
                    break
    return visible[:5]


@router.get("/health/llm")
async def llm_health() -> dict:
    """Live-fire the LLM router with one tiny SMALL-tier completion (v0.19.0).

    The cheapest faithful proxy for "can the cognition stack call a
    model at all" — it exercises the exact router path that dies with
    "all tiers exhausted" when the persisted baseUrl/apiKey are wrong.
    Defensive: never raises, always returns {ok, tier, latency_ms, error}.
    """
    if _llm_router is None:
        return {"ok": False, "tier": None, "latency_ms": 0,
                "error": "LLM router not wired"}
    try:
        from protagine.router.tiers import ModelTier
        resp = await _llm_router.complete(
            [{"role": "user", "content": "Say OK"}],
            force_tier=ModelTier.SMALL,
        )
        tier = resp.tier_used.value if getattr(resp, "tier_used", None) else None
        return {"ok": True, "tier": tier,
                "latency_ms": getattr(resp, "latency_ms", 0), "error": None}
    except Exception as exc:  # noqa: BLE001 — diagnostics must not 500
        return {"ok": False, "tier": None, "latency_ms": 0, "error": str(exc)}


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

_NOT_WIRED = {"error": {"code": "not_wired", "message": "Backend not configured"}}


@router.post("/memory/read", response_model=MemoryReadResponse)
async def memory_read(body: MemoryReadRequest, request: Request = None) -> MemoryReadResponse:
    """Open one exact canonical revision in the authenticated participant scope."""
    person_id = resolve_request_person(request, claimed_person_id=body.person_id)
    _require_person_authority(request, person_id)
    from protagine.turns import get_turn_idempotency_ledger
    from protagine.turns.source_read import read, read_video
    try:
        ledger = get_turn_idempotency_ledger(get_state_dir())
        if body.source_view == 'video':
            return MemoryReadResponse(source=await read_video(ledger,
                contact_id=person_id, session_id=body.session_id, source_id=body.source_id,
                source_version=body.source_version, asset_hash=body.asset_hash,
                requested_ms=body.requested_ms, read_revision=body.read_revision))
        return MemoryReadResponse(source=read(ledger,
            contact_id=person_id, session_id=body.session_id, source_id=body.source_id,
            source_version=body.source_version, view=body.source_view, claim_id=body.claim_id,
            offset=body.offset, read_revision=body.read_revision, asset_hash=body.asset_hash, page=body.page))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except Exception as exc:
        logger.warning('Canonical source read failed (%s)', type(exc).__name__)
        raise HTTPException(status_code=503, detail={
            'code': 'memory_backend_unavailable', 'message': 'Canonical source could not be read'}) from None


@router.post("/memory/search", response_model=MemorySearchResponse)
async def memory_search(body: MemorySearchRequest, request: Request) -> MemorySearchResponse:
    """Search current canonical evidence for an authenticated participant.

    The body names the person (the schema refuses a missing or blank one);
    there is no owner default. Development mode never resolves to the owner
    (``resolve_request_person``) and never passes the authority check. A guest
    searches their own canonical sources only.
    """
    person = resolve_request_person(request, claimed_person_id=body.person_id)
    _require_person_authority(request, person)
    canonical_only = _viewer_is_guest(request, person)
    try:
        facts = None if canonical_only or _facts_store is None else _facts_store.automatic_view()
        from protagine.turns import get_turn_idempotency_ledger
        from protagine.vector import get_store, get_pipeline
        from protagine.memory.search import collect_sources, select_memory
        from protagine.util.temporal import resolve_communication_timezone
        ledger = get_turn_idempotency_ledger(get_state_dir())
        collected = await collect_sources(ledger, query=body.query, contact_id=person,
            session_id=body.session_id or "", vector_store=get_store(), embedding_pipeline=get_pipeline())
        contact_tz = None
        if not canonical_only and _contacts_store is not None:
            try:
                contact = await _contacts_store.get(person)
                contact_tz = getattr(contact, "timezone", None)
            except Exception:
                logger.debug("contact timezone unavailable for memory search", exc_info=True)
        packet = await select_memory(collected, query=body.query, selector=_memory_context_selector(),
            contact_facts=facts, contact_facts_allowed=not canonical_only,
            timezone_name=resolve_communication_timezone(
                contact_tz, body.timezone or ("UTC" if canonical_only else None)), limit=body.limit)
        return MemorySearchResponse(**packet.public())
    except Exception as exc:
        logger.warning("canonical memory search failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail={
            "code": "memory_backend_unavailable",
            "message": "Canonical memory could not be read or selected",
        }) from None


@router.post('/memory/recent', response_model=MemoryRecentResponse)
async def memory_recent(body: MemoryRecentRequest, request: Request) -> MemoryRecentResponse:
    """Read the caller's latest recorded conversation without semantic ranking."""
    person = resolve_request_person(request, claimed_person_id=body.person_id)
    _require_person_authority(request, person)
    from protagine.turns import get_turn_idempotency_ledger
    from protagine.memory.recent import read_recent
    def load_recent():
        ledger = get_turn_idempotency_ledger(get_state_dir())
        return read_recent(ledger, contact_id=person,
            session_id=body.session_id, platform=body.platform, limit=body.limit,
            comms_log=_comms_log)
    try:
        return MemoryRecentResponse(**await asyncio.to_thread(load_recent))
    except Exception as exc:
        logger.warning('Recent canonical conversation unavailable (%s)', type(exc).__name__)
        raise HTTPException(status_code=503, detail={
            'code': 'memory_backend_unavailable',
            'message': 'Recent canonical conversation could not be read'}) from None


@router.post("/memory/embed", response_model=MemoryEmbedResponse)
async def memory_embed(body: MemoryEmbedRequest) -> MemoryEmbedResponse:
    if _embedder is None:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=_NOT_WIRED)
    try:
        # Support both old `inputs` field and new `texts` field
        texts = body.texts or body.inputs
        if not texts:
            raise HTTPException(status_code=400, detail="No texts provided")
        if len(texts) > 128:
            raise HTTPException(status_code=400, detail=f"Batch size {len(texts)} exceeds limit of 128")
        vectors = await _embedder.embed_batch(texts)
        # Determine model_id from the underlying provider config
        model_id = ""
        if hasattr(_embedder, "_provider") and hasattr(_embedder._provider, "_config"):
            model_id = _embedder._provider._config.model_id
        return MemoryEmbedResponse(model=model_id or body.model or "unknown", vectors=vectors)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("memory_embed failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/memory/rerank", response_model=RerankResponse)
async def memory_rerank(body: RerankRequest) -> RerankResponse:
    """Rerank documents by relevance to a query.

    Requires the reranker to be initialized (see PROTAGINE_RERANKER_MODEL env var).
    """
    if _reranker is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Reranker not initialized. Set PROTAGINE_RERANKER_MODEL to enable.",
        )
    try:
        if not body.documents:
            raise HTTPException(status_code=400, detail="No documents provided")
        if len(body.documents) > 256:
            raise HTTPException(
                status_code=400,
                detail=f"Document count {len(body.documents)} exceeds limit of 256",
            )
        results = await _reranker.rerank(
            query=body.query,
            documents=body.documents,
            top_k=body.top_k or 10,
        )
        return RerankResponse(
            results=[
                RerankResult(index=r.index, score=r.score, text=r.text)
                for r in results
            ],
            model=getattr(_reranker, "_model_id", "unknown"),
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("memory_rerank failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/embed/health", response_model=EmbedHealthResponse)
async def embed_health() -> EmbedHealthResponse:
    """Check embedder health — verify model is loaded and producing valid output."""
    if _embedder is None:
        return EmbedHealthResponse(status="error", error=_embed_failure or "embedder not initialized")
    try:
        result = await _embedder.health_check()
        # Add multimodal status
        result["modalities"] = _embedder.modalities if hasattr(_embedder, "modalities") else ["text"]
        result["multimodal_enabled"] = _embedder.is_multimodal if hasattr(_embedder, "is_multimodal") else False
        return EmbedHealthResponse(**result)
    except Exception as exc:
        return EmbedHealthResponse(status="error", error=str(exc))


@router.post("/memory/embed/image", response_model=ImageEmbedResponse)
async def memory_embed_image(body: ImageEmbedRequest) -> ImageEmbedResponse:
    """Embed a single image and optionally store it."""
    if _embedder is None:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=_NOT_WIRED)
    if not _embedder.is_multimodal:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Multimodal not enabled")

    try:
        # Determine image source
        source = body.image or body.image_url
        if not source:
            raise HTTPException(status_code=400, detail="No image provided (use image or image_url)")

        vector, meta = await _embedder.embed_image(
            source,
            mime_type=body.mime_type or "",
            caption=body.caption or "",
        )

        # If collection and id provided, also index it
        if body.collection and body.id:
            from protagine.vector import get_store
            from protagine.vector.collections import Collection
            from protagine.vector.query import VectorItem

            store = get_store()
            if store:
                try:
                    col = Collection(body.collection)
                except ValueError:
                    col = Collection.MEMORIES
                vi = VectorItem(
                    id=body.id,
                    text=meta.get("caption", ""),
                    vector=vector,
                    metadata=meta,
                )
                await store.add_batch(col, [vi])

        model_id = meta.get("model_id", "")
        return ImageEmbedResponse(
            model=model_id,
            vector=vector,
            image_hash=meta.get("image_hash", ""),
            image_ref=meta.get("image_ref", ""),
            thumbnail_ref=meta.get("thumbnail_ref", ""),
            caption=meta.get("caption", ""),
            width=meta.get("width", 0),
            height=meta.get("height", 0),
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.warning("image embed failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/memory/embed/image/batch", response_model=ImageBatchEmbedResponse)
async def memory_embed_image_batch(body: ImageBatchEmbedRequest) -> ImageBatchEmbedResponse:
    """Embed multiple images."""
    if _embedder is None:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=_NOT_WIRED)
    if not _embedder.is_multimodal:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Multimodal not enabled")
    if len(body.images) > 32:
        raise HTTPException(status_code=400, detail=f"Batch size {len(body.images)} exceeds limit of 32")

    try:
        results = []
        for img_item in body.images:
            source = img_item.get("image") or img_item.get("image_url")
            if not source:
                continue
            vector, meta = await _embedder.embed_image(
                source,
                mime_type=img_item.get("mime_type", ""),
                caption=img_item.get("caption", ""),
            )
            results.append({"vector": vector, **meta})

        model_id = results[0].get("model_id", "") if results else ""
        return ImageBatchEmbedResponse(model=model_id, results=results)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.warning("image batch embed failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/memory/embed/async")
async def memory_embed_async(body: dict) -> dict:
    """Async embedding for large collections — returns task_id immediately.

    Accepts the same format as /memory/embed, /memory/embed/image/batch,
    or /memory/index but runs in the background.
    Poll GET /memory/embed/async/{task_id} for status.
    """
    if _embedder is None:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=_NOT_WIRED)

    from protagine.vector import get_store
    store = get_store()

    task_id = str(uuid.uuid4())
    _async_embed_tasks: dict = getattr(router, "_async_embed_tasks", {})
    router._async_embed_tasks = _async_embed_tasks

    embed_type = body.get("type", "texts")  # texts | images | index

    async def _run():
        try:
            _async_embed_tasks[task_id] = {"status": "running", "processed": 0, "failed": 0}

            if embed_type == "texts":
                texts = body.get("texts", body.get("inputs", []))
                if len(texts) > 1024:
                    _async_embed_tasks[task_id] = {"status": "failed", "error": f"Batch size {len(texts)} exceeds 1024"}
                    return
                vectors = await _embedder.embed_batch(texts)
                _async_embed_tasks[task_id] = {"status": "completed", "processed": len(vectors), "failed": 0}

            elif embed_type == "images":
                images = body.get("images", [])
                if len(images) > 128:
                    _async_embed_tasks[task_id] = {"status": "failed", "error": f"Batch size {len(images)} exceeds 128"}
                    return
                results = []
                failed = 0
                for img_item in images:
                    try:
                        source = img_item.get("image") or img_item.get("image_url")
                        if not source:
                            failed += 1
                            continue
                        vector, meta = await _embedder.embed_image(
                            source, mime_type=img_item.get("mime_type", ""),
                            caption=img_item.get("caption", ""),
                        )
                        results.append({"vector": vector, **meta})
                    except Exception:
                        failed += 1
                _async_embed_tasks[task_id] = {"status": "completed", "processed": len(results), "failed": failed}

            elif embed_type == "index":
                if store is None:
                    _async_embed_tasks[task_id] = {"status": "failed", "error": "VectorStore not initialized"}
                    return
                items = body.get("items", [])
                indexed = 0
                failed = 0
                for item in items:
                    try:
                        from protagine.vector.collections import Collection
                        from protagine.vector.query import VectorItem

                        if item.get("image") or item.get("image_url"):
                            source = item.get("image") or item.get("image_url")
                            vector, meta = await _embedder.embed_image(
                                source, mime_type=item.get("mime_type", ""),
                                caption=item.get("caption", ""),
                            )
                            col_name = item.get("collection", "memories")
                            try: col = Collection(col_name)
                            except ValueError: col = Collection.MEMORIES
                            vi = VectorItem(id=item.get("id", str(uuid.uuid4())), text=meta.get("caption", ""), vector=vector, metadata=meta)
                        else:
                            text = item.get("text", "")
                            vector = await _embedder.embed(text)
                            col_name = item.get("collection", "memories")
                            try: col = Collection(col_name)
                            except ValueError: col = Collection.MEMORIES
                            meta = item.get("metadata", {})
                            meta["model_id"] = _embedder._provider._config.model_id if hasattr(_embedder, "_provider") else ""
                            vi = VectorItem(id=item.get("id", str(uuid.uuid4())), text=text, vector=vector, metadata=meta)

                        await store.add_batch(col, [vi])
                        indexed += 1
                    except Exception:
                        failed += 1
                _async_embed_tasks[task_id] = {"status": "completed", "indexed": indexed, "failed": failed}

        except Exception as exc:
            _async_embed_tasks[task_id] = {"status": "failed", "error": str(exc)}

    _spawn_task(_run())
    return {"task_id": task_id, "status": "started"}


@router.get("/memory/embed/async/{task_id}")
async def async_embed_status(task_id: str) -> dict:
    """Poll status of an async embed task."""
    _async_embed_tasks: dict = getattr(router, "_async_embed_tasks", {})
    result = _async_embed_tasks.get(task_id)
    if result is None:
        return {"task_id": task_id, "status": "running"}
    return {"task_id": task_id, **result}


@router.post("/memory/search/multimodal", response_model=MultimodalSearchResponse)
async def memory_search_multimodal(body: MultimodalSearchRequest) -> MultimodalSearchResponse:
    """Cross-modal search — text query finds images, image query finds text."""
    if _embedder is None:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=_NOT_WIRED)

    from protagine.vector import get_store
    store = get_store()
    if store is None:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="VectorStore not initialized")

    try:
        from protagine.vector.collections import Collection

        col_name = body.collection or "memories"
        try:
            col = Collection(col_name)
        except ValueError:
            col = Collection.MEMORIES

        # Get query vector
        if body.query:
            if _embedder.is_multimodal:
                query_vector = await _embedder._multimodal_provider.embed_text(body.query)
            else:
                query_vector = await _embedder.embed(body.query)
        elif body.query_image:
            if not _embedder.is_multimodal:
                raise HTTPException(status_code=400, detail="Image query requires multimodal to be enabled")
            vector, _ = await _embedder.embed_image(body.query_image)
            query_vector = vector
        else:
            raise HTTPException(status_code=400, detail="No query provided (use query or query_image)")

        results = await store.search_cross_modal(
            col, query_vector,
            limit=body.limit,
            filter_modality=body.filter_modality,
            min_score=body.min_score,
        )

        model_id = ""
        if hasattr(_embedder, "_provider") and hasattr(_embedder._provider, "_config"):
            model_id = _embedder._provider._config.model_id

        return MultimodalSearchResponse(results=results, model=model_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("multimodal search failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/memory/backfill", response_model=BackfillResponse)
async def memory_backfill(body: BackfillRequest) -> BackfillResponse:
    """Re-embed all vectors using the current embedding pipeline.

    Returns a task_id immediately; backfill runs in the background.
    """
    if _embedder is None:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=_NOT_WIRED)

    from protagine.vector import get_store
    store = get_store()
    if store is None:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="VectorStore not initialized")

    task_id = str(uuid.uuid4())

    async def _run():
        from protagine.vector.backfill import backfill
        try:
            result = await backfill(store, _embedder, collection=body.collection, batch_size=body.batch_size)
            # Store result in app state for polling
            _backfill_results[task_id] = result
        except Exception as exc:
            logger.error("Backfill failed: %s", exc)

    _backfill_results: dict = getattr(router, "_backfill_results", {})
    router._backfill_results = _backfill_results

    _spawn_task(_run())
    return BackfillResponse(task_id=task_id, status="started")


@router.get("/memory/backfill/{task_id}", response_model=BackfillResponse)
async def backfill_status(task_id: str) -> BackfillResponse:
    """Check the status of a running backfill task."""
    _backfill_results: dict = getattr(router, "_backfill_results", {})
    result = _backfill_results.get(task_id)
    if result is None:
        return BackfillResponse(task_id=task_id, status="running")
    return BackfillResponse(
        task_id=task_id,
        status="completed",
        total=result.total,
        processed=result.processed,
        failed=result.failed,
        skipped=result.skipped,
        duration_s=round(result.duration_s, 2),
        errors=result.errors,
    )


@router.post("/memory/migrate", response_model=MigrateResponse)
async def memory_migrate(body: MigrateRequest) -> MigrateResponse:
    """Migrate all vectors from an old model to the current embedding model."""
    if _embedder is None:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=_NOT_WIRED)

    from protagine.vector import get_store
    store = get_store()
    if store is None:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="VectorStore not initialized")

    if getattr(router, '_migrate_running', False):
        raise HTTPException(status_code=409, detail='A vector rebuild is already running')
    if not 1 <= body.batch_size <= 128:
        raise HTTPException(status_code=422, detail='Rebuild batch size must be between 1 and 128')
    if getattr(store, 'catalog', None) is not None and body.old_model_id:
        raise HTTPException(status_code=422, detail='A generation rebuild must include all retained rows')

    task_id = str(uuid.uuid4())
    if getattr(store, 'catalog', None) is not None:
        task_id = store.catalog.begin(store.identity)['id']
    router._migrate_running = True
    router._migrate_task_id = task_id

    async def _run():
        from protagine.vector.migrate import migrate_tier
        try:
            result = await migrate_tier(store, _embedder, old_model_id=body.old_model_id,
                                        batch_size=body.batch_size)
            _migrate_results[task_id] = result
        except Exception as exc:
            logger.error("Migration failed: %s", exc)
            from protagine.vector.migrate import MigrationResult
            _migrate_results[task_id] = MigrationResult(errors=[str(exc)], generation_id=task_id)
            if getattr(store, 'catalog', None) is not None:
                store.catalog.finish(task_id, error=str(exc))
        finally:
            router._migrate_running = False

    _migrate_results: dict = getattr(router, "_migrate_results", {})
    router._migrate_results = _migrate_results
    _migrate_results.pop(task_id, None)

    _spawn_task(_run())
    return MigrateResponse(task_id=task_id, status="started")


@router.get("/memory/migrate/{task_id}", response_model=MigrateResponse)
async def migrate_status(task_id: str) -> MigrateResponse:
    """Check the status of a running migration task."""
    if getattr(router, '_migrate_running', False) and getattr(router, '_migrate_task_id', None) == task_id:
        return MigrateResponse(task_id=task_id, status='running', generation_id=task_id)
    _migrate_results: dict = getattr(router, "_migrate_results", {})
    result = _migrate_results.get(task_id)
    if result is None:
        from protagine.vector import get_store
        store = get_store()
        if store is not None and getattr(store, 'catalog', None) is not None:
            generation = next((g for g in store.catalog.generations() if g['id'] == task_id), None)
            if generation:
                active = store.catalog.active()
                return MigrateResponse(task_id=task_id, generation_id=task_id,
                    fingerprint=generation['fingerprint'] or '',
                    status='completed' if generation['status'] in {'ready', 'retained'} else 'resumable',
                    errors=[generation['error']] if generation.get('error') else [])
        raise HTTPException(status_code=404, detail='Unknown vector rebuild')
    return MigrateResponse(
        task_id=task_id,
        status="failed" if result.errors else "completed",
        collections_migrated=result.collections_migrated,
        vectors_migrated=result.vectors_migrated,
        vectors_failed=result.vectors_failed,
        duration_s=round(result.duration_s, 2),
        errors=result.errors,
        generation_id=getattr(result, 'generation_id', ''),
        fingerprint=getattr(result, 'fingerprint', ''),
    )


@router.post("/memory/index", response_model=IndexResponse)
async def memory_index(body: IndexRequest) -> IndexResponse:
    """Embed and store items in one call."""
    if _embedder is None:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=_NOT_WIRED)

    from protagine.vector import get_store
    store = get_store()
    if store is None:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="VectorStore not initialized")

    if not body.items:
        return IndexResponse(model="unknown", indexed=0, failed=0)
    if len(body.items) > 128:
        raise HTTPException(status_code=400, detail=f"Batch size {len(body.items)} exceeds limit of 128")

    try:
        from protagine.vector.collections import Collection
        from protagine.vector.query import VectorItem

        # Determine current model_id
        model_id = ""
        if hasattr(_embedder, "_provider") and hasattr(_embedder._provider, "_config"):
            model_id = _embedder._provider._config.model_id

        # Separate text items from image items
        text_items = []
        image_items = []
        for item in body.items:
            if item.get("image") or item.get("image_url"):
                image_items.append(item)
            else:
                text_items.append(item)

        indexed = 0
        failed = 0

        # Process text items
        if text_items:
            texts = [item.get("text", "") for item in text_items]
            vectors = await _embedder.embed_batch(texts)
            for item, vector in zip(text_items, vectors):
                try:
                    col_name = item.get("collection", "memories")
                    try:
                        col = Collection(col_name)
                    except ValueError:
                        col = Collection.MEMORIES
                    meta = item.get("metadata", {})
                    meta["model_id"] = model_id
                    vi = VectorItem(id=item.get("id", str(uuid.uuid4())), text=item.get("text", ""), vector=vector, metadata=meta)
                    await store.add_batch(col, [vi])
                    indexed += 1
                except Exception as exc:
                    logger.warning("index text item failed: %s", exc)
                    failed += 1

        # Process image items
        for item in image_items:
            try:
                source = item.get("image") or item.get("image_url")
                if not source:
                    failed += 1
                    continue
                vector, meta = await _embedder.embed_image(
                    source,
                    mime_type=item.get("mime_type", ""),
                    caption=item.get("caption", ""),
                )
                col_name = item.get("collection", "memories")
                try:
                    col = Collection(col_name)
                except ValueError:
                    col = Collection.MEMORIES
                vi = VectorItem(id=item.get("id", str(uuid.uuid4())), text=meta.get("caption", ""), vector=vector, metadata=meta)
                await store.add_batch(col, [vi])
                indexed += 1
            except Exception as exc:
                logger.warning("index image item failed: %s", exc)
                failed += 1

        return IndexResponse(model=model_id or "unknown", indexed=indexed, failed=failed)
    except Exception as exc:
        logger.warning("memory_index failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


async def _build_temporal_section(
    contact_id: Optional[str], override_tz: Optional[str] = None,
    *, include_global_heads_up: bool = True,
) -> ContextSection:
    """The agent's reference frame for "now" (v0.21.0, factored for per-turn use).

    The host is the authoritative clock; this fulfils the promise the
    protagine-memory plugin makes ("prefer the real current time provided by the
    host"). Agent home tz + the contact's local tz + elapsed-since-last-contact
    + time-sensitive heads-up items. Cheap (no memory search): safe per turn.
    """
    from protagine.util import temporal as _temporal
    agent_tz = _temporal.agent_timezone()
    contact_tz = None
    contact_label = "the contact"
    contact_obj = None
    if _contacts_store is not None and contact_id:
        try:
            contact_obj = await _contacts_store.get(contact_id)
            if contact_obj is not None:
                contact_tz = getattr(contact_obj, "timezone", None)
                contact_label = (
                    contact_obj.display_name or contact_obj.given_name or "the contact"
                )
        except Exception:
            pass
    # A fallback communication frame is not evidence of a contact's timezone.
    t_lines = [_temporal.describe_now(agent_tz, contact_tz, contact_label, override_tz=override_tz)]
    if contact_obj is not None and getattr(contact_obj, "last_interaction_at", None):
        li = contact_obj.last_interaction_at
        t_lines.append(
            f"Last exchange with {contact_label}: {_temporal.humanize_delta(li)} "
            f"({_temporal.bucket(li, agent_tz)})."
        )
    if include_global_heads_up:
        try:
            from protagine.util.session_safety import load_last_user_message_at
            last_owner = load_last_user_message_at()
            since = _temporal.humanize_delta(last_owner) if last_owner else ""
            if since and since != "just now":  # in a live exchange this line says nothing
                t_lines.append(f"Owner last messaged {since}.")
        except Exception:
            pass
    # Heads-up: time-sensitive items (overdue commitments + cadence-overdue contacts)
    heads = []
    if include_global_heads_up:
        try:
            if _commitment_store is not None:
                for c in (_commitment_store.get_overdue() or [])[:3]:
                    desc = (c.get("description") or "a commitment")[:80]
                    heads.append(
                        f"⚠️ Overdue: {desc} (was due {_temporal.humanize_delta(c.get('due_at'))})"
                    )
        except Exception:
            pass
        try:
            if _contacts_store is not None:
                exclude = {contact_id} if contact_id else set()
                overdue_contacts = await _contacts_store.compute_cadence_overdue(
                    overdue_only=True, limit=3, exclude_ids=exclude,
                )
                for o in overdue_contacts[:2]:
                    heads.append(
                        f"🕰️ Haven't talked to {o['name']} in {int(o['days_since'])}d "
                        f"(usually ~{o['cadence_days']:g}d)."
                    )
        except Exception:
            pass
    if heads:
        t_lines.append("Heads-up:")
        t_lines.extend("  " + h for h in heads)
    return ContextSection(
        id="temporal-context",
        title="Current Time",
        body="\n".join(t_lines),
        priority=100,
    )


@router.get("/context/temporal")
async def context_temporal(contact_id: Optional[str] = None,
                           tz: Optional[str] = None,
                           request: Request = None) -> dict:
    """Always-fresh temporal brief for per-turn injection (no caching layer).

    The memory provider calls this every turn so the agent's Current Time
    block can never go stale inside a long-running session (the full
    /context/assemble result is session-cached by design; time must not be).
    The owner's heads-up (their overdue commitments, contacts they have not
    talked to) is owner context: a guest's brief never carries it.
    """
    section = await _build_temporal_section(
        contact_id, tz, include_global_heads_up=not _viewer_is_guest(request, contact_id))
    return {"id": section.id, "title": section.title, "body": section.body}


@router.post("/context/assemble", response_model=ContextAssembleResponse)
async def context_assemble(
    body: ContextAssembleRequest,
    request: Request = None,
) -> ContextAssembleResponse:
    body.context.contact_id = resolve_request_person(
        request,
        context_person_id=body.context.contact_id,
        audience=body.audience,
    ) or body.context.contact_id
    authority = request_authority(request)
    # Only the key's own view with no person named is unscoped; every other caller, including a
    # development caller without the key (never the owner), sees the person it names as a viewer.
    viewer = body.context.contact_id if authority.authenticated and not authority.anonymous else (
        body.context.contact_id or "dev-anonymous")
    # Stamp before reading any producer. A concurrent forget makes the entire
    # packet stale at the native request boundary, including derived sections.
    source_erasure_watermark = None
    try:
        from protagine.turns import get_turn_idempotency_ledger
        source_erasure_watermark = get_turn_idempotency_ledger(get_state_dir()).erasure_watermark(body.context.contact_id)
    except Exception:
        logger.warning("context erasure freshness unavailable")
    sections = await _assemble_sections(body, viewer_person_id=viewer, request=request)

    if _telemetry is not None:
        try:
            await _telemetry.touch("last_prefetch_at")
        except Exception:
            pass

    return ContextAssembleResponse(
        sections=sections,
        notices=([_GUEST_CONTEXT_NOTICE] if _viewer_is_guest(request, body.context.contact_id) else None),
        source_erasure_watermark=source_erasure_watermark,
    )


_GUEST_CONTEXT_NOTICE = (
    "Contact-scoped context provides their own source evidence, claims and media, the commitments "
    "proven shared in their source evidence, and their digest. The owner's tasks, graph, "
    "relationship, shared-fact and global context are omitted.")


async def _assemble_sections(
    body: ContextAssembleRequest,
    *,
    viewer_person_id: Optional[str],
    request: Request | None = None,
) -> list[ContextSection]:
    """The context sections for ``body``'s contact as ``viewer_person_id`` may see them.

    A viewer other than the owner gets the contact-scoped set: their own
    canonical recall, the commitments their sources prove shared, their
    appraisal perspective and their digest; never an owner-only or global
    section. It fails closed: with no owner configured the owner is the
    reserved ``owner`` person, so every named viewer is a guest. ``None`` is
    the key holder asking about no one (the owner's own API), which keeps the
    unscoped context. The route and the mind's recipient packet
    (``assemble_packet``) share this one assembly.
    """
    owner_id = owner_person_id()
    _canonical_only = bool(viewer_person_id) and viewer_person_id != owner_id
    _viewer_is_owner = bool(viewer_person_id) and viewer_person_id == owner_id
    # Legacy person stores can contain private owner observations ABOUT a
    # guest. Exact subject identity does not make those observations shareable.
    _legacy_global_allowed = _exact_person_allowed = not _canonical_only
    _tom_context_facts = (None if _canonical_only or _facts_store is None
                          else _facts_store.automatic_view())
    sections: list[ContextSection] = []
    query_text = body.incoming_message.content if body.incoming_message else ""

    # Read authenticated work before recall selection. Native requests still
    # refresh this observation at their existing model boundary.
    current_work_available = False
    _owner_turn = False
    # --- Scoped execution observations (not a commitment lock) ---
    try:
        from protagine.api.routers.executions import authorized_viewer, with_queue_work
        from protagine.turns.executions import has_work_records, registry, request_work_context
        person, owner = authorized_viewer(request, body.context.contact_id, scope="context:read")
        # Public/guest turns do not get cross-session activity. The owner view
        # is sealed from existing exact person grants, never a body owner flag.
        _owner_turn = bool(owner)
        if owner:
            # Initial work is metadata only. Source-backed input excerpts belong
            # to the native request refresh, which carries their freshness guards.
            work = registry().view(contact_id=person, owner=True, limit=8,
                                   include_ancestors=True, include_inputs=False)
            work = await with_queue_work(work, owner=True, limit=8)
            current_work_available = True
            if has_work_records(work):  # an all-idle or all-unavailable view is not worth a section
                observed = request_work_context(work, session_id=body.context.session_id,
                                                limit=8, max_chars=4000)
                sections.append(ContextSection(id="protagine-executions", title="Work observed at turn start", body=observed['text'], priority=73))
    except HTTPException:
        pass
    except Exception:
        logger.debug("execution observation view unavailable", exc_info=True)

    # --- Temporal Context: see _build_temporal_section ---
    try:
        cid = (
            body.context.contact_id
            if body.context and _exact_person_allowed else None
        )
        override_tz = getattr(body.context, "timezone", None) if body.context else None
        if _canonical_only:
            sections.append(ContextSection(
                id="temporal-context", title="Current Time", priority=100,
                body="Current UTC time: " + datetime.now(timezone.utc).isoformat()))
        else:
            sections.append(await _build_temporal_section(
                cid,
                override_tz,
                include_global_heads_up=_legacy_global_allowed,
            ))
    except Exception as exc:
        logger.debug("context_assemble temporal section failed: %s", exc)

    # --- Memory: authorized candidates, one selection and one budget ---
    if query_text:
        from protagine.memory.search import collect_sources, select_memory
        try:
            from protagine.turns import get_turn_idempotency_ledger
            from protagine.vector import get_store, get_pipeline
            source_ledger = (get_turn_idempotency_ledger(get_state_dir())
                if (Path(get_state_dir()) / "turn-idempotency.db").exists() else None)
            # The broadcast set expands the owner's recall query (architecture 4.5); the
            # section itself is rendered below, never for a guest.
            recall_query = _mind_recall_query(query_text) if _owner_turn else query_text
            collected = await collect_sources(source_ledger, query=recall_query,
                contact_id=body.context.contact_id, session_id=body.context.session_id,
                vector_store=get_store(), embedding_pipeline=get_pipeline())
            contact_tz = None
            if not _canonical_only and _contacts_store is not None:
                try:
                    contact = await _contacts_store.get(body.context.contact_id)
                    contact_tz = getattr(contact, "timezone", None)
                except Exception:
                    logger.debug("contact timezone unavailable for memory recall", exc_info=True)
            from protagine.util.temporal import resolve_communication_timezone
            packet = await select_memory(collected, query=query_text, selector=_memory_context_selector(),
                contact_facts=_tom_context_facts,
                contact_facts_allowed=not _canonical_only,
                timezone_name=resolve_communication_timezone(
                    contact_tz, body.context.timezone or ("UTC" if _canonical_only else None)),
                current_work_available=current_work_available)
            if packet.content:
                sections.append(ContextSection(
                    id="protagine-memory", title="Relevant Memories", body=packet.content,
                    priority=90, citations=packet.source_refs or None))
            elif _owner_turn and _viewer_is_owner:
                # The owner's own turn, by the viewer identity every other owner-only section uses.
                _mind_note_novel(query_text)
        except Exception as exc:
            logger.warning("combined memory selection failed (%s)", type(exc).__name__)

    # --- Active Goals ---
    if _legacy_global_allowed and _goals_store is not None:
        try:
            from protagine.goals.models import GoalStatus
            goals = _goals_store.list_goals(status=GoalStatus.ACTIVE)
            if goals:
                body_text = "\n".join(
                    f"- [{g.priority.name.lower()}] {g.title}: {g.description} (progress: {g.progress_pct:.0%})"
                    for g in goals[:5]
                )
                sections.append(ContextSection(
                    id="protagine-goals",
                    title="Active Goals",
                    body=body_text,
                    priority=80,
                ))
        except Exception as exc:
            logger.warning("context_assemble goals failed: %s", exc)

    # --- Pending Initiatives (v0.13.0) ---
    if _legacy_global_allowed and body.include_initiatives\
            and _initiative_store is not None:
        try:
            pending = _initiative_store.list(status=["pending"], limit=10)
            if pending:
                body_text = "\n".join(
                    f"• [{i.type}] {i.description} (priority: {i.priority:.0%})"
                    for i in pending
                )
                sections.append(ContextSection(
                    id="protagine-initiatives",
                    title="Pending Initiatives",
                    body=body_text,
                    priority=50,
                ))
        except Exception as exc:
            logger.warning("context_assemble initiatives failed: %s", exc)

    # Stored global briefings have no query or participant relevance contract.
    # Keep them available through /briefings;
    # do not prepend the latest three (possibly old dataclass dumps) to every turn.

    # The registry contains internal initiative executors, not instruction
    # skills installed in the requesting runtime. The host owns its actual
    # skill catalog and discovery tools; do not advertise these Python
    # executor names as callable skills in ordinary turn context.

    # --- Pending Commitments ---
    contact_id = body.context.contact_id if body.context else None
    if _commitment_store is not None:
        try:
            commitments = _commitment_store.list(
                person_id=contact_id, status=["pending", "overdue"], limit=50 if _canonical_only else 5,
            )
            # The exact-person list already includes overdue rows. A sealed
            # guest must not trigger a global get-then-filter query.
            overdue = (
                _commitment_store.get_overdue()
                if _legacy_global_allowed else []
            )
            if contact_id and overdue:
                overdue = [
                    c for c in overdue
                    if c.get("person_id") == contact_id
                ]
            _listed = (commitments if isinstance(commitments, list)
                       else commitments.get("commitments", []))
            if _canonical_only:
                _listed = _canonical_shared_commitments(_listed, contact_id)
            _seen_ids = {c.get("id") for c in _listed}
            all_comms = _listed + [c for c in overdue[:5]
                                   if c.get("id") not in _seen_ids]
            if all_comms:
                from protagine.commitments.work import CommitmentWork
                reservations = {}
                reservations_available = True
                try:
                    reservations = CommitmentWork(_commitment_store).for_commitments(
                        [c['id'] for c in all_comms], contact_id=contact_id)
                except Exception:
                    reservations_available = False
                    logger.debug('commitment reservation view unavailable', exc_info=True)
                lines = ["Open commitments (a live reservation held by another session is that session's work):"]
                for c in all_comms:
                    status_tag = "[OVERDUE]" if c.get("status") == "overdue" or c['id'] in {item['id'] for item in overdue} else "[pending]"
                    due = f" (due: {c.get('due_at', '')})" if c.get('due_at') else ""
                    reservation = reservations.get(c['id'])
                    work_tag = ('; work=' + reservation['work_state']
                                + ('' if _canonical_only else '; session=' + reservation.get('session_id', ''))) if reservation else ('; work=unclaimed' if reservations_available else '; work=unknown')
                    lines.append(f"- {status_tag} id={c['id']}; {c.get('description', '')}{due}{work_tag}")
                sections.append(ContextSection(
                    id="protagine-commitments",
                    title="Pending Commitments",
                    body="\n".join(lines),
                    priority=72,
                ))
        except Exception as exc:
            logger.warning("context_assemble commitments failed: %s", exc)

    # --- Source-backed appraisals and contact-specific decision guidance ---
    if contact_id:
        try:
            from protagine.api.routers.social_state import appraisal_context
            from protagine.api.routers.executions import authorized_viewer
            person, _ = authorized_viewer(request, contact_id, scope='context:read')
            social_brief, social_refs = appraisal_context(contact_id=person,
                session_id=body.context.session_id, query=query_text)
            if social_brief:
                sections.append(ContextSection(id='protagine-appraisals',
                    title='Relevant working perspective', body=social_brief,
                    priority=85, citations=social_refs))
        except HTTPException:
            pass
        except Exception:
            logger.debug('appraisal context unavailable', exc_info=True)

    # --- Recorded views (opinions): any viewer, audience-filtered (architecture 4.4) ---
    stance_text = _mind_stances(query_text, viewer_contact_id=viewer_person_id or contact_id or '',
                                viewer_is_owner=_viewer_is_owner, session_id=body.context.session_id)
    if stance_text:
        sections.append(ContextSection(id='protagine-stances', title='Your recorded views',
                                       body=stance_text, priority=87))
    # --- What the mind learned (lessons): the owner's own turn only, at most one (architecture 4.8) ---
    lesson_text = _mind_lessons(query_text, viewer_is_owner=_viewer_is_owner, owner_turn=_owner_turn,
                                session_id=body.context.session_id)
    if lesson_text:
        sections.append(ContextSection(id='protagine-lessons', title='What you learned',
                                       body=lesson_text, priority=86))

    if contact_id:
        try:
            from protagine.api.routers.executions import authorized_viewer
            person, owner = authorized_viewer(request, contact_id, scope='context:read')
            if owner:
                from protagine.api.routers.social_state import waiting_context
                waiting_brief = waiting_context(person)
                if waiting_brief:
                    sections.append(ContextSection(id='protagine-waiting', title='Expected replies',
                        body=waiting_brief, priority=74))
                # --- The Mind section (architecture 3.1): at most 600 characters, owner only ---
                mind_text = _mind_section()
                if mind_text:
                    sections.append(ContextSection(id='protagine-mind', title='Mind', body=mind_text, priority=77))
                if _situation_store is not None and re.search(r'\b(hardware|machine|server|model|endpoint|cluster|offline|online|running|doing|status)\b', query_text, re.I):
                    from protagine.self_model.situation import compact_situation
                    snapshot = _situation_store.snapshot(subject_person_id=person, viewer_scope='owner')
                    current = compact_situation(snapshot, limit=8)
                    if current['facts'] or current['stale']:
                        sections.append(ContextSection(id='protagine-current-state',
                            title='Current observed state and stale observations',
                            body=json.dumps(current, ensure_ascii=False), priority=76))
        except HTTPException:
            pass
        except Exception:
            logger.debug('temporal and current-state context unavailable', exc_info=True)

    # Conversation state uses source-cited appraisals and working judgments.
    # Legacy numeric mood estimates remain inspectable through their explicit API.

    # --- Recorded relationship context ---
    if _exact_person_allowed and _contacts_store is not None and contact_id:
        try:
            _rc = await _contacts_store.get(contact_id)
            if _rc is not None:
                _rt = getattr(_rc, "trust_tier", "") or ""
                _count = getattr(_rc, "interaction_count", None)
                _bits = [f"Recorded interactions: {_count}" if _count is not None
                         else "Interaction count not recorded"]
                if _rt:
                    _bits.append(f"recorded contact tier: {_rt.replace('_', ' ')}")
                _rl = getattr(_rc, "last_interaction_at", None)
                if _rl:
                    _bits.append(f"last recorded interaction: {str(_rl)[:10]}")
                sections.append(ContextSection(
                    id="protagine-relationship",
                    title="Recorded relationship context",
                    body=" · ".join(_bits),
                    priority=86,
                ))
        except Exception as exc:
            logger.debug("context_assemble relationship failed: %s", exc)

    # --- About this person: their digest, for any viewer but the owner; the people faculty's ---
    # At most the template digest's length on every turn, whichever writer stored it.
    from protagine.api.routers.mind import faculty_on
    if _contacts_store is not None and contact_id and contact_id != owner_id and faculty_on("people"):
        try:
            from protagine.contacts.digest import MAX_CHARS as _DIGEST_CHARS
            _digest = str(getattr(await _contacts_store.get(contact_id), "digest", None) or "").strip()
            if _digest:
                sections.append(ContextSection(
                    id="protagine-person", title="About this person",
                    body=_digest if len(_digest) <= _DIGEST_CHARS else _digest[:_DIGEST_CHARS - 1] + "…",
                    priority=84))
        except Exception as exc:
            logger.debug("context_assemble person digest failed: %s", exc)

    # --- Owner's stated preferences (explicit directives the owner gave me) ---
    if _exact_person_allowed and _preference_learner is not None\
            and contact_id:
        try:
            from protagine.identity import get_owner_contact_id
            if get_owner_contact_id() == contact_id:
                perspective = getattr(_preference_learner, 'perspective', None)
                preference_sources = []
                _brief = _preference_learner.build_brief(source_ids=preference_sources)
                if _brief:
                    sections.append(ContextSection(
                        id="protagine-owner-preferences",
                        title="How they want me to communicate",
                        body=_brief,
                        priority=88,
                        citations=(perspective.ledger.source_references(preference_sources,
                            contact_id=contact_id, session_id=body.context.session_id)
                            if perspective is not None else None),
                    ))
                if perspective is not None:
                    working_sources = []
                    working_brief = perspective.brief(query=(
                        query_text if _viewer_is_owner else ''),
                        source_ids=working_sources)
                    if working_brief:
                        sections.append(ContextSection(id='protagine-self-perspective',
                            title='Owner priority corrections', body=working_brief, priority=87,
                            citations=perspective.ledger.source_references(working_sources,
                                contact_id=contact_id, session_id=body.context.session_id)))
        except Exception as exc:
            logger.debug("context_assemble owner preferences failed: %s", exc)

    # --- Communication landscape (cross-channel awareness) ---
    if _exact_person_allowed and _comms_log is not None\
            and _contacts_store is not None and contact_id:
        try:
            _lc = await _contacts_store.get(contact_id)
            if _lc is not None:
                _bits = []
                _per = _comms_log.last_per_channel(contact_id)
                if _per:
                    _chs = ", ".join(f"{ch} {str(v['ts'])[:10]}" for ch, v in _per.items())
                    _bits.append(f"Recorded channels: {_chs}.")
                _lo = _comms_log.last_outbound(contact_id)
                if _lo:
                    _bits.append(
                        f"Last recorded outgoing message: {_lo['channel']} on {str(_lo['ts'])[:10]}. "
                        "This summary includes conversation replies; it does not establish "
                        "proactive outreach or delivery."
                    )
                if _commitment_store is not None:
                    try:
                        _cm = _commitment_store.list(person_id=contact_id, status=["pending", "overdue"], limit=5)
                        _cm_items = _cm.get("commitments", []) if isinstance(_cm, dict) else (_cm or [])
                        if _cm_items:
                            _descs = [c.get("description", "") for c in _cm_items if c.get("description")]
                            if _descs:
                                _bits.append("Open follow-ups: " + "; ".join(_descs[:3]) + ".")
                    except Exception:
                        pass
                if _bits:
                    from protagine.identity import get_owner_contact_id
                    _is_owner = (get_owner_contact_id() == contact_id)
                    if not _is_owner:
                        _bits.append(
                            "Proactively reaching out to them needs owner "
                            "approval first."
                        )
                    sections.append(ContextSection(
                        id="protagine-comms-landscape",
                        title="Communication landscape",
                        body=" ".join(_bits),
                        priority=83,
                    ))
        except Exception as exc:
            logger.debug("context_assemble comms landscape failed: %s", exc)

    return sections


async def assemble_packet(contact_id: str, *, query: str = "", limit_chars: int = 2000) -> str:
    """The recipient-scoped packet the mind composes a message to ``contact_id`` from.

    It is what /context/assemble gives that contact as its own viewer (for
    anyone but the owner: their own recall, the commitments their sources
    prove shared, their digest; never an owner-only section), rendered the
    way the memory provider renders sections, most important first, and cut
    to ``limit_chars``.
    """
    contact_id = str(contact_id or "").strip()
    if not contact_id or limit_chars <= 0:
        return ""
    body = ContextAssembleRequest(
        identity=HostIdentity(host_id="protagine-mind"),
        context=HostTurnContext(session_id=f"mind:{contact_id}", contact_id=contact_id),
        incoming_message=HostMessage(role="user", content=query or ""),
        include_initiatives=False,
    )
    sections = await _assemble_sections(body, viewer_person_id=contact_id)
    sections.sort(key=lambda section: -(section.priority or 0))
    text = "\n\n".join(f"## {section.title or section.id}\n{section.body}" for section in sections)
    return text[:limit_chars]


async def claims_for(contact_id: str, limit: int = 8) -> list[str]:
    """The contact's own current source claims, newest first, as ``predicate: value``.

    The digest's "what they have told me" lines: only claims grounded in that
    contact's person-scoped sources, never superseded, retracted, expired or
    from a source whose projections were erased.
    """
    contact_id = str(contact_id or "").strip()
    if not contact_id or limit <= 0 or not (Path(get_state_dir()) / "turn-idempotency.db").exists():
        return []
    from contextlib import closing
    from protagine.beliefs.source_projection import SourceClaimProjection
    from protagine.turns import get_turn_idempotency_ledger

    def read() -> list[str]:
        projection = SourceClaimProjection(get_turn_idempotency_ledger(get_state_dir()))
        now = datetime.now(timezone.utc).isoformat()
        with closing(projection.ledger._connect()) as conn:
            rows = projection._rows(conn, contact_id, "", distinct_values=True, limit=limit * 4)
            erased = {row[0] for row in conn.execute(
                "SELECT turn_id FROM source_projection_erasures WHERE turn_id IN ("
                + ",".join("?" for _ in rows) + ")", [row["turn_id"] for row in rows])} if rows else set()
        lines: list[str] = []
        for row in rows:
            value = str(row.get("value") or "").strip()
            if (row.get("superseded_by") or row.get("retracted_by") or row["turn_id"] in erased
                    or not value or (row.get("valid_to") and row["valid_to"] <= now)):
                continue
            line = f"{row['predicate']}: {value[:160]}"
            if line not in lines:
                lines.append(line)
            if len(lines) >= limit:
                break
        return lines

    return await asyncio.to_thread(read)


# ---------------------------------------------------------------------------
# Turns
# ---------------------------------------------------------------------------

class SourceForgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contact_id: str = Field(min_length=1, max_length=256)
    source_ids: list[str] = Field(default_factory=list, max_length=100)
    old_text: str | None = Field(default=None, min_length=1, max_length=131072)
    metadata: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = Field(default=None, max_length=256)


class SourceAnnotationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    contact_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    annotation_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(min_length=1, max_length=256)
    source_version: str = Field(pattern='^[0-9a-f]{64}$')
    excerpt: str = Field(min_length=1, max_length=4096)
    correction: str = Field(min_length=1, max_length=4096)


class SourceDeadlineRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    contact_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    source_id: str = Field(min_length=1, max_length=256)
    source_version: str = Field(pattern='^[0-9a-f]{64}$')
    claim_id: str = Field(min_length=1, max_length=256)
    timezone_name: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode='after')
    def valid_timezone(self):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        if self.timezone_name is None:
            return self
        try:
            ZoneInfo(self.timezone_name)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError('Unknown timezone') from exc
        return self


@router.post('/memory/sources/deadline')
async def read_source_deadline(body: SourceDeadlineRequest, request: Request):
    authority = request_authority(request)
    if not authority.authenticated or authority.anonymous:
        raise HTTPException(status_code=403, detail={'code': 'source_deadline_not_authorized'})
    person = resolve_request_person(request, claimed_person_id=body.contact_id)
    from protagine.turns import get_turn_idempotency_ledger
    from protagine.beliefs.source_projection import SourceClaimProjection
    from protagine.util.temporal import resolve_communication_timezone
    contact_tz = None
    if body.timezone_name is None and _contacts_store is not None:
        contact = await _contacts_store.get(person)
        contact_tz = getattr(contact, 'timezone', None)
    zone = resolve_communication_timezone(contact_tz, body.timezone_name)
    projection = SourceClaimProjection(get_turn_idempotency_ledger(get_state_dir()))
    return projection.deadline(**{**body.model_dump(), 'contact_id': person,
        'timezone_name': zone, 'timezone_basis': 'caller_override' if body.timezone_name else 'communication_frame'})


@router.post('/memory/sources/annotations')
async def append_source_annotation(body: SourceAnnotationRequest, request: Request):
    authority = request_authority(request)
    if not authority.authenticated or authority.anonymous:
        raise HTTPException(status_code=403, detail={'code': 'source_annotation_not_authorized'})
    person = resolve_request_person(request, claimed_person_id=body.contact_id)
    from protagine.turns import get_turn_idempotency_ledger
    try:
        return get_turn_idempotency_ledger(get_state_dir()).append_source_annotation(
            **{**body.model_dump(), 'contact_id': person}, author_principal=authority.principal_id)
    except ValueError as exc:
        code = str(exc)
        if code not in {'source_erased', 'source_not_found', 'source_version_mismatch',
                        'source_excerpt_mismatch', 'annotation_id_conflict'}:
            code = 'invalid_source_annotation'
        raise HTTPException(status_code=409 if code.endswith(('conflict', 'mismatch')) else 422,
                            detail={'code': code}) from exc


@router.get("/memory/sources/erasures")
async def source_erasure_feed(contact_id: str, after: int = Query(0, ge=0), request: Request = None):
    person = resolve_request_person(request, claimed_person_id=contact_id) or contact_id
    from protagine.turns import get_turn_idempotency_ledger
    try:
        return get_turn_idempotency_ledger(get_state_dir()).erasure_feed(person, after)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "erasure_history_mismatch"}) from exc


class NativeHistoryReference(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: str = Field(min_length=1, max_length=256)
    message_hash: str = Field(pattern=r'^[0-9a-f]{64}$')


class SourceFreshnessRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    contact_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    after: int = Field(default=0, ge=0)
    source_refs: list[SourceReference] = Field(max_length=512)
    unannotated_input_refs: list[SourceInputReference] = Field(default_factory=list, max_length=512)
    native_history_refs: list[NativeHistoryReference] = Field(default_factory=list, max_length=512)
    annotation_checks: list[SourceAnnotationCheck] = Field(default_factory=list, max_length=512)

    @model_validator(mode='after')
    def exact_source_selector(self):
        if not self.source_refs and not self.unannotated_input_refs and not self.native_history_refs:
            raise ValueError('Exact source revisions or captured inputs are required')
        return self


@router.post('/memory/sources/erasures')
async def source_freshness_feed(body: SourceFreshnessRequest, request: Request):
    """The existing feed plus current ownership of the exact supplied sources.

    Corrections change attribution without erasing source bytes or changing
    their content digest. Check canonical ownership and descendant validity;
    an erasure watermark alone cannot certify cached recall after a correction.
    Optional exact input membership also rechecks that an operational excerpt
    has no applicable annotation, including a first note added after selection.
    Exact current input revisions are returned separately so a host that only
    retained the original input hash can pin the canonical media revision, then
    repeat the strict supplied-version check. Missing expected versions never
    make sources_current true. POST adds no persisted state.
    """
    person = resolve_request_person(request, claimed_person_id=body.contact_id) or body.contact_id
    from protagine.turns import get_turn_idempotency_ledger
    ledger = get_turn_idempotency_ledger(get_state_dir())
    try:
        page = ledger.erasure_feed(person, body.after)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={'code': 'erasure_history_mismatch'}) from exc
    expected = {(ref.source_id, ref.source_version) for ref in body.source_refs}
    current = ledger.source_references([ref.source_id for ref in body.source_refs],
                                      contact_id=person, session_id=body.session_id)
    page['sources_current'] = expected == {(ref['source_id'], ref['source_version']) for ref in current}
    if body.annotation_checks:
        from protagine.turns.source_annotations import current_candidates
        checks = [check.model_dump() for check in body.annotation_checks]
        candidates = [{'_annotation_source_refs': check['source_refs'],
                       '_annotation_message_hashes': check['message_hashes'],
                       '_annotation_ids': check['annotation_ids']} for check in checks]
        retained = current_candidates(ledger, candidates, contact_id=person, session_id=body.session_id)
        # Keep byte/ownership validity separate from each search's selected
        # correction set. A stale earlier result must not poison a fresh search
        # later in the same native turn.
        page['annotation_checks_current'] = [candidate in retained and
            {(ref['source_id'], ref['source_version']) for ref in check['source_refs']} <= expected
            for check, candidate in zip(checks, candidates)]
    if body.native_history_refs:
        from protagine.turns.history_references import resolve
        page['native_history_matches'] = resolve(ledger, contact_id=person,
            session_id=body.session_id,
            references=[ref.model_dump() for ref in body.native_history_refs])
    if body.unannotated_input_refs:
        from protagine.turns.source_annotations import inputs_unannotated
        refs = [ref.model_dump() for ref in body.unannotated_input_refs]
        page['input_source_refs'] = []
        try:
            inputs = ledger.resolve_input_dependencies(contact_id=person,
                session_id=body.session_id, refs=refs)
        except ValueError:
            page['sources_current'] = False
        else:
            versions = {(ref['source_id'], ref['source_version']) for ref in inputs}
            current_inputs = ledger.source_references([ref['source_id'] for ref in inputs],
                contact_id=person, session_id=body.session_id)
            inputs_current = (versions == {(ref['source_id'], ref['source_version']) for ref in current_inputs}
                              and inputs_unannotated(ledger, refs))
            if inputs_current:
                page['input_source_refs'] = current_inputs
            page['sources_current'] &= inputs_current and versions <= expected
    return page


@router.get("/memory/sources/claims/status")
async def source_claim_projection_status(contact_id: str, request: Request = None):
    person = resolve_request_person(request, claimed_person_id=contact_id) or contact_id
    from protagine.turns import get_turn_idempotency_ledger
    from protagine.beliefs.source_projection import SourceClaimProjection
    from protagine.turns.media import SourceMedia
    ledger = get_turn_idempotency_ledger(get_state_dir())
    from protagine.turns.source_vectors import SourceVectors
    from protagine.vector import get_store, get_pipeline
    return {"sources": SourceClaimProjection(ledger).status(person), "media": SourceMedia(ledger).status(person),
            "semantic": SourceVectors(ledger, get_store(), get_pipeline()).status(person)}


@router.get("/memory/sources/assets/{asset_hash}")
async def read_source_asset(asset_hash: str, contact_id: str, session_id: str, request: Request = None):
    person = resolve_request_person(request, claimed_person_id=contact_id) or contact_id
    from protagine.turns import get_turn_idempotency_ledger
    from protagine.turns.media import SourceMedia
    try:
        data, mime = SourceMedia(get_turn_idempotency_ledger(get_state_dir())).read(
            asset_hash, contact_id=person, session_id=session_id)
    except (KeyError, FileNotFoundError):
        raise HTTPException(status_code=404, detail="unknown source asset") from None
    return Response(content=data, media_type=mime, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/memory/sources/forget")
async def forget_turn_sources(body: SourceForgetRequest, request: Request = None):
    person = resolve_request_person(request, claimed_person_id=body.contact_id) or body.contact_id
    from protagine.turns import get_turn_idempotency_ledger
    try:
        ledger = get_turn_idempotency_ledger(get_state_dir())
        result = await asyncio.to_thread(ledger.erase_sources,
            contact_id=person, turn_ids=body.source_ids,
            old_text=body.old_text, session_id=body.session_id,
        )
    except ValueError as exc:
        code = str(exc) if str(exc) in {"ambiguous_source", "source_not_found"} else "invalid_source_selection"
        raise HTTPException(status_code=409 if code == "ambiguous_source" else 422, detail={"code": code}) from exc
    # The durable tombstone precedes external projection deletion. A failed
    # cleanup remains a truthful pending result, and recall stays fenced.
    fact_cleanup = "unavailable" if _facts_store is None else "pending"
    if _facts_store is not None:
        try:
            _facts_store.purge_erased_sources(list(dict.fromkeys(result["source_ids"] + result["affected_source_ids"])))
            fact_cleanup = "complete"
        except Exception:
            logger.warning("source erasure shared-fact cleanup is pending", exc_info=True)
    tom_cleanup = {'affect_cleanup': 'unavailable' if _affect_store is None else 'pending'}
    if _affect_store is not None:
        try:
            _affect_store.purge_erased_sources(list(dict.fromkeys(result['source_ids'] + result['affected_source_ids'])))
            tom_cleanup['affect_cleanup'] = 'complete'
        except Exception:
            logger.warning('source erasure affect cleanup is pending', exc_info=True)
    vector_cleanup = ('disabled_not_checked' if os.environ.get('PROTAGINE_EMBED_PROVIDER') == 'skip'
                      else 'unavailable')
    vector_purge = 'not_run'
    from protagine.vector import get_store
    vector_store = get_store()
    if vector_store is not None and getattr(vector_store, 'catalog', None) is not None:
        vector_cleanup = 'pending'
        try:
            await vector_store.erase_source_projections(
                list(dict.fromkeys(result['source_ids'] + result['affected_source_ids'])), purge=False)
            vector_cleanup = 'complete'
            # The rows are out of the served view now. Compacting the tables, which takes
            # the text out of the data files and old versions, runs after the answer: on a
            # large store never compacted before it takes minutes (operability-11).
            vector_store.schedule_purge()
            vector_purge = 'scheduled'
        except Exception:
            logger.warning('source erasure vector generation cleanup is pending', exc_info=True)
    transport_cleanup = 'pending'
    try:
        from protagine.api.routers.transport_ingress_api import forget_sources
        transport_cleanup = forget_sources(list(dict.fromkeys(result['source_ids'] + result['affected_source_ids'])))
    except Exception:
        logger.warning('Source erasure transport cleanup remains pending', exc_info=True)
    communications_cleanup, communications_unlinked_rows = 'unavailable', None
    if _comms_log is not None:
        communications_cleanup = 'pending'
        try:
            _comms_log.purge_erased_sources(list(dict.fromkeys(result['source_ids'] + result['affected_source_ids'])),
                                           contact_id=person)
            communications_unlinked_rows = _comms_log.unlinked_summary_count(person)
            communications_cleanup = 'complete'
        except Exception:
            logger.warning('Source erasure communication summary cleanup remains pending', exc_info=True)
    return {"source_erased": True, **result,
            "shared_facts_cleanup": fact_cleanup, "vector_cleanup": vector_cleanup, "vector_purge": vector_purge,
            "transport_cleanup": transport_cleanup, **tom_cleanup,
            "communications_cleanup": communications_cleanup,
            "communications_scope": "source_linked_summaries_only",
            "communications_unlinked_rows": communications_unlinked_rows,
            "scope": "canonical_turn_sources_and_linked_projections",
            "host_reconciliation": "not_observed",
            "host_reconciliation_detail": "The erasure feed is available at this watermark; this response does not measure which hosts have applied it."}


async def _ingest_turn_idempotently(
    body: TurnSyncRequest,
    request: Request | None = None,
) -> tuple[TurnSyncResponse, str]:
    """Run one turn's effects, or replay its durable result.

    Legacy callers without a turn ID retain the v1 behavior. A supplied ID is
    reserved before attribution, memory, cognition, journal, or relationship
    effects run. This is the server's final defense even when a host retries or
    two host integrations accidentally submit the same envelope.
    """
    resolved_sender_contact_id = None
    turn_id = (body.context.turn_id or "").strip()
    if not turn_id:
        return await _process_turn_sync(body, request=request,
            resolved_sender_contact_id=resolved_sender_contact_id), "unkeyed"
    if len(turn_id) > 256:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_turn_id", "message": "turn_id exceeds 256 characters"},
        )
    body.context.turn_id = turn_id

    from protagine.turns import (
        ReservationOutcome,
        canonical_turn_digest,
        get_turn_idempotency_ledger,
    )

    digest = canonical_turn_digest(body)
    ledger = get_turn_idempotency_ledger(get_state_dir())
    if ledger.is_source_erased(turn_id, body.context.contact_id):
        return TurnSyncResponse(accepted=False, source_recorded=False, continuity_updated=False, skipped_reason="source_erased"), "erased"
    if body.assistant_input_refs:
        from protagine.turns.idempotency import SourceInputPending
        try:
            ledger.resolve_input_dependencies(contact_id=body.context.contact_id,
                session_id=body.context.session_id, turn_id=turn_id,
                refs=[ref.model_dump() for ref in body.assistant_input_refs])
        except SourceInputPending:
            # Ordinary outbox retry, with no reserved or ambiguous effect row.
            return TurnSyncResponse(accepted=False, source_recorded=False, continuity_updated=False,
                                    skipped_reason='source_input_parent_pending'), 'in_progress'
        except ValueError as exc:
            raise HTTPException(422, detail={'code': 'invalid_source_input_dependency'}) from exc
    if body.checkpoint_messages is not None:
        # One atomic source+index commit, no ordinary conversation effects.
        # A retry after an interrupted response can safely repeat this write.
        try:
            created = ledger.record_source(
                turn_id, contact_id=body.context.contact_id,
                session_id=body.context.session_id, scope="session",
                messages=[dict(message.model_dump(mode="json"), **(
                    {'_supplied_sources': [ref.model_dump() for ref in body.assistant_source_refs]}
                    if message.role == 'assistant' and body.assistant_source_refs else {}))
                    for message in body.checkpoint_messages],
                occurred_at=(body.context.metadata or {}).get("occurred_at"),
                timezone_name=body.context.timezone,
                channel_id=body.context.channel_id,
            )
        except ValueError as exc:
            from protagine.turns.idempotency import SourceErased
            if isinstance(exc, SourceErased):
                return TurnSyncResponse(accepted=False, source_recorded=False, continuity_updated=False, skipped_reason="source_erased"), "erased"
            raise HTTPException(status_code=409, detail={"code": "checkpoint_source_conflict"}) from exc
        return TurnSyncResponse(
            accepted=True, continuity_updated=False, source_recorded=True,
            skipped_reason="checkpoint_source_only",
        ), "created" if created else "replayed"
    reservation = ledger.reserve(turn_id, digest)
    if reservation.outcome == ReservationOutcome.CONFLICT:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "turn_id_content_conflict",
                "turn_id": turn_id,
                "message": "turn_id is already bound to different canonical content",
            },
        )
    if reservation.outcome == ReservationOutcome.AMBIGUOUS:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "turn_ingestion_ambiguous",
                "turn_id": turn_id,
                "message": "the first attempt was interrupted; inspect before replaying effects",
            },
        )
    if reservation.outcome == ReservationOutcome.REPLAYED:
        cached = reservation.response or {
            "accepted": True,
            "continuity_updated": False,
            "skipped_reason": "identical_replay",
        }
        return TurnSyncResponse.model_validate(cached), "replayed"
    if reservation.outcome == ReservationOutcome.IN_PROGRESS:
        return TurnSyncResponse(
            # Truthful pending semantics: the first writer has not committed a
            # result yet and may still fail or become ambiguous. A retry must
            # not look like a completed successful ingestion.
            accepted=False,
            continuity_updated=False,
            skipped_reason="identical_retry_in_progress",
        ), "in_progress"

    try:
        result = await _process_turn_sync(body, request=request,
            resolved_sender_contact_id=resolved_sender_contact_id)
    except BaseException as exc:
        ledger.mark_ambiguous(turn_id, digest, exc)
        raise
    ledger.complete(turn_id, digest, result.model_dump(mode="json"))
    return result, "created"


@router.post("/turns/sync", response_model=TurnSyncResponse)
async def turns_sync(
    body: TurnSyncRequest,
    request: Request = None,
    response: Response = None,
) -> TurnSyncResponse:
    body.context.contact_id = resolve_turn_person(
        request,
        context_person_id=body.context.contact_id,
        has_sender=body.sender is not None,
    ) or body.context.contact_id
    result, outcome = await _ingest_turn_idempotently(body, request=request)
    from protagine.api.routers.transport_ingress_api import reconcile_source
    reconcile_source(body, result)
    if response is not None:
        response.headers["Idempotency-Status"] = outcome
        if outcome == "in_progress":
            response.status_code = status.HTTP_202_ACCEPTED
            response.headers["Retry-After"] = "1"
    return result


@v2_router.put('/turns/source-linked/input-parent/{turn_id:path}', response_model=TurnSyncResponse)
async def source_input_linked_sync(turn_id: str, body: TurnSyncRequest, response: Response, request: Request = None):
    """Older linked-source routes reject this suffix before persisting anything."""
    if body.context.turn_id == 'source-linked/input-parent/' + turn_id:
        return await turns_sync_v2(body.context.turn_id, body, response, request)
    if not body.assistant_input_refs or body.context.turn_id != turn_id:
        raise HTTPException(422, detail={'code': 'invalid_input_linked_source'})
    return await turns_sync_v2(turn_id, body, response, request)


@v2_router.put('/turns/source-media/transport/{turn_id:path}', response_model=TurnSyncResponse)
async def transport_media_source_sync(turn_id: str, body: TurnSyncRequest, response: Response, request: Request):
    """Same turn authority; older receivers reject this capability-specific path."""
    if body.context.turn_id == 'source-media/transport/' + turn_id:
        return await turns_sync_v2(body.context.turn_id, body, response, request)
    if body.context.turn_id != turn_id or body.transport_media is None:
        raise HTTPException(422, detail={'code': 'invalid_transport_media_source'})
    result = await turns_sync_v2(turn_id, body, response, request)
    if result.source_recorded:
        from protagine.turns import get_turn_idempotency_ledger
        from protagine.turns.transport_media import receipt
        result = result.model_copy(update={'transport_media': receipt(get_turn_idempotency_ledger(get_state_dir()), turn_id)})
    return result


@v2_router.put('/turns/source-media/audio/{turn_id:path}', response_model=TurnSyncResponse)
async def audio_source_sync(turn_id: str, body: TurnSyncRequest, response: Response, request: Request = None):
    """An older generic turn route must not persist unowned audio bytes."""
    if body.context.turn_id == 'source-media/audio/' + turn_id:
        return await turns_sync_v2(body.context.turn_id, body, response, request)
    messages = [body.user_message, body.assistant_message, *(body.checkpoint_messages or [])]
    if body.context.turn_id != turn_id or not any(message and isinstance(message.content, list)
            and any(isinstance(block, dict) and block.get('type') == 'input_audio' for block in message.content)
            for message in messages):
        raise HTTPException(422, detail={'code': 'invalid_audio_source'})
    return await turns_sync_v2(turn_id, body, response, request)


@v2_router.put('/turns/source-media/video/{turn_id:path}', response_model=TurnSyncResponse)
async def video_source_sync(turn_id: str, body: TurnSyncRequest, response: Response, request: Request = None):
    """Selected inline MP4 ingestion; predecessors reject unequal path IDs."""
    if body.context.turn_id == 'source-media/video/' + turn_id:
        return await turns_sync_v2(body.context.turn_id, body, response, request)
    messages = [body.user_message, body.assistant_message, *(body.checkpoint_messages or [])]
    if body.context.turn_id != turn_id or not any(message and isinstance(message.content, list)
            and any(isinstance(block, dict) and block.get('type') == 'input_video' for block in message.content)
            for message in messages):
        raise HTTPException(422, detail={'code': 'invalid_video_source'})
    return await turns_sync_v2(turn_id, body, response, request)


@v2_router.put('/turns/source-media/document/{turn_id:path}', response_model=TurnSyncResponse)
async def document_source_sync(turn_id: str, body: TurnSyncRequest, response: Response, request: Request = None):
    """Explicit inline document ingestion; predecessors reject unequal path IDs."""
    if body.context.turn_id == 'source-media/document/' + turn_id:
        return await turns_sync_v2(body.context.turn_id, body, response, request)
    messages = [body.user_message, body.assistant_message, *(body.checkpoint_messages or [])]
    if body.context.turn_id != turn_id or not any(message and isinstance(message.content, list)
            and any(isinstance(block, dict) and block.get('type') == 'input_document' for block in message.content)
            for message in messages):
        raise HTTPException(422, detail={'code': 'invalid_document_source'})
    return await turns_sync_v2(turn_id, body, response, request)


@v2_router.put("/turns/source-linked/{turn_id:path}", response_model=TurnSyncResponse)
async def source_linked_sync(turn_id: str, body: TurnSyncRequest, response: Response, request: Request = None):
    """An old backend must not silently drop a new answer's source references."""
    if body.context.turn_id == 'source-linked/' + turn_id:
        return await turns_sync_v2(body.context.turn_id, body, response, request)
    if not body.assistant_source_refs or body.context.turn_id != turn_id:
        raise HTTPException(status_code=422, detail={'code': 'invalid_linked_source'})
    return await turns_sync_v2(turn_id, body, response, request)


@v2_router.put("/turns/source-survivors/{turn_id:path}", response_model=TurnSyncResponse)
async def source_survivor_sync(turn_id: str, body: TurnSyncRequest, response: Response, request: Request = None):
    """Preserve attributed direct survivors without replaying ordinary effects.

    A predecessor matches its generic turn route and rejects the unequal path
    and envelope IDs. Hosts must leave the survivor queued, never fall back.
    """
    if body.context.turn_id == 'source-survivors/' + turn_id:
        return await turns_sync_v2(body.context.turn_id, body, response, request)
    if body.source_only is not True or body.context.turn_id != turn_id:
        raise HTTPException(status_code=422, detail={'code': 'invalid_source_survivor'})
    return await turns_sync_v2(turn_id, body, response, request)


@v2_router.put('/turns/task-instruction/{turn_id:path}', response_model=TurnSyncResponse)
async def task_instruction_sync(turn_id: str, body: TurnSyncRequest, response: Response, request: Request):
    """Retain a direct owner instruction without duplicating ordinary learning."""
    from protagine.api.routers.executions import authorized_viewer
    _, owner = authorized_viewer(request, body.context.contact_id, scope='turns:write')
    if (not owner or not turn_id.startswith('task-instruction:') or body.context.turn_id != turn_id
            or body.source_only is not True or body.user_message is None or body.assistant_message is not None
            or body.checkpoint_messages is not None or body.assistant_source_refs):
        raise HTTPException(422, detail='direct_owner_instruction_required')
    request.state.task_instruction_only = True
    return await turns_sync_v2(turn_id, body, response, request)


class ToolObservationEnvelope(BaseModel):
    model_config = ConfigDict(extra='forbid')
    identity: HostIdentity
    context: HostTurnContext
    observation: ToolObservation


@v2_router.put('/turns/source-observation/{turn_id:path}', response_model=TurnSyncResponse)
def source_observation_sync(turn_id: str, body: ToolObservationEnvelope, response: Response, request: Request):
    from protagine.api.routers.executions import authorized_viewer
    from protagine.turns import get_turn_idempotency_ledger
    from protagine.turns.idempotency import SourceErased
    from protagine.turns.tool_observations import record
    person, owner = authorized_viewer(request, body.context.contact_id, scope='turns:write')
    if not owner:
        raise HTTPException(403, detail='owner_observation_required')
    if body.context.turn_id != turn_id:
        raise HTTPException(422, detail='observation_identity_mismatch')
    try:
        created = record(get_turn_idempotency_ledger(get_state_dir()), body.observation,
            contact_id=person, session_id=body.context.session_id, source_id=turn_id)
    except SourceErased:
        return TurnSyncResponse(accepted=False, source_recorded=False, continuity_updated=False,
                                skipped_reason='source_erased')
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    response.status_code = 201 if created else 200
    return TurnSyncResponse(accepted=True, source_recorded=True, continuity_updated=False,
                            skipped_reason='tool_observation_only')


@v2_router.put("/turns/{turn_id:path}", response_model=TurnSyncResponse)
async def turns_sync_v2(
    turn_id: str,
    body: TurnSyncRequest,
    response: Response,
    request: Request = None,
) -> TurnSyncResponse:
    """Idempotent TurnEnvelopeV2 ingestion compatibility slice.

    The path ID is canonical. Initial acceptance is ``201``; an identical
    replay is ``200``; changed content under the same ID is ``409``.
    """
    path_turn_id = (turn_id or "").strip()
    if not path_turn_id or len(path_turn_id) > 256:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_turn_id", "message": "turn_id must be 1..256 characters"},
        )
    body_turn_id = (body.context.turn_id or "").strip()
    if body_turn_id and body_turn_id != path_turn_id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "turn_id_mismatch",
                "path_turn_id": path_turn_id,
                "body_turn_id": body_turn_id,
            },
        )
    body.context.turn_id = path_turn_id
    body.context.contact_id = resolve_turn_person(
        request,
        context_person_id=body.context.contact_id,
        has_sender=body.sender is not None,
    ) or body.context.contact_id
    result, outcome = await _ingest_turn_idempotently(body, request=request)
    from protagine.api.routers.transport_ingress_api import reconcile_source
    reconcile_source(body, result)
    response.headers["Idempotency-Status"] = outcome
    if outcome == "created":
        response.status_code = status.HTTP_201_CREATED
    elif outcome == "in_progress":
        response.status_code = status.HTTP_202_ACCEPTED
        response.headers["Retry-After"] = "1"
    else:
        response.status_code = status.HTTP_200_OK
    return result


def _occurred_at(metadata: Optional[Dict[str, Any]]) -> Optional[str]:
    """When an inbound turn happened: the capture's ``occurred_at``, never later than now (a skewed
    host cannot date a conversation into the future). None when absent or unreadable: the contact
    store then stamps its own now. Both follow ``time.time``, the clock the mind ticks on."""
    from protagine.util.temporal import parse_iso
    stamp = parse_iso((metadata or {}).get("occurred_at"))
    if stamp is None:
        return None
    now = datetime.fromtimestamp(time.time(), timezone.utc)
    return min(stamp.astimezone(timezone.utc), now).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _process_turn_sync(
    body: TurnSyncRequest,
    request: Request | None = None,
    *,
    resolved_sender_contact_id: str | None = None,
) -> TurnSyncResponse:
    # Keep direct blocks for canonical source storage. All existing text-only
    # cognition consumers receive only explicit text, never repr(base64/URLs).
    from protagine.turns import canonical_turn_digest
    source_body = body
    source_messages = [{"role": message.role, "content": message.content}
                       for message in (body.user_message, body.assistant_message)
                       if message is not None and (message.content.strip() if isinstance(message.content, str) else message.content)]
    if body.transport_media is not None:
        from protagine.turns.transport_media import apply
        source_messages = apply(source_messages, body.transport_media, session_id=body.context.session_id)
    if body.assistant_source_refs:
        for message in source_messages:
            if message['role'] == 'assistant':
                message['_supplied_sources'] = [ref.model_dump() for ref in body.assistant_source_refs]
    if body.assistant_input_refs:
        for message in source_messages:
            if message['role'] == 'assistant':
                message['_supplied_inputs'] = [ref.model_dump() for ref in body.assistant_input_refs]
    body = body.model_copy(deep=True)
    if body.transport_media is not None:
        # Every ordinary cognition consumer sees the actual human caption.
        # Runtime vision enrichment remains explicitly labeled source metadata.
        body.user_message.content = body.transport_media.caption
        body.summary = None
    for field in ("user_message", "assistant_message"):
        message = getattr(body, field)
        if message is not None and isinstance(message.content, list):
            message.content = message.text_content()
    # Auto-derive channel_id when the host does not provide one, so
    # context provenance and cross-context leak detection always work.
    body.context.channel_id = await _ensure_channel_id(
        body.context, identity=body.identity,
    )
    # ── Attribution chokepoint (docs/RELATIONSHIPS.md) ──────────────────
    # Resolve WHO said this server-side. A supplied sender overrides the
    # client's contact_id (which goes stale in group sessions); a senderless
    # machine turn (cron/api channel or system-origin text) attributes to
    # the reserved "system" sentinel so it can never pollute a person's
    # affect/facts/psyche/interactions. Rewriting context.contact_id here
    # means every downstream consumer in this handler sees the truth.
    _resolved_human_sender = False
    _contact_grant_attested = False
    try:
        from protagine.identity.participants import (
            SYSTEM_CONTACT_ID, ParticipantResolver, is_machine_turn,
        )
        if resolved_sender_contact_id is not None:
            body.context.contact_id = resolved_sender_contact_id
            _resolved_human_sender = True
        elif body.sender is not None and _contacts_store is not None:
            _res = await ParticipantResolver(_contacts_store).resolve(
                platform=body.sender.platform,
                user_id=body.sender.user_id,
                display_name=body.sender.display_name,
                group_id=body.sender.group_id,
                channel_id=body.context.channel_id or "",
            )
            if _res.contact_id:
                if _res.contact_id != body.context.contact_id:
                    logger.info(
                        "turn attribution: %s -> %s (%s%s)",
                        body.context.contact_id, _res.contact_id, _res.method,
                        ", shadow-created" if _res.created else "")
                body.context.contact_id = _res.contact_id
                _resolved_human_sender = True
                # The server resolved the sender itself; that is the attestation.
                _contact_grant_attested = True
        if not _resolved_human_sender and is_machine_turn(
                body.context.channel_id or "",
                (getattr(body.user_message, "content", "") or "")
                if body.user_message else "",
                has_sender=body.sender is not None):
            body.context.contact_id = SYSTEM_CONTACT_ID
    except Exception:
        logger.debug("participant attribution failed; keeping client contact",
                     exc_info=True)
    if not source_body.context.channel_id and _resolved_human_sender and body.sender is not None:
        # A stale claimed contact can have another platform's primary handle.
        # The resolved sender establishes the fallback conversation platform;
        # explicit conversation keys (including derived work) stay unchanged.
        body.context.channel_id = f'{body.sender.platform.strip().lower()}:{body.context.contact_id}'
    # Observe only the final attributed channel, never a stale fallback.
    _observe_channel(body.context.channel_id)
    _is_system_turn = body.context.contact_id == "system"

    # Persist complete attributed messages before derived graph/mining effects.
    # This is the central source of truth even when extractors are disabled.
    source_id = body.context.turn_id or "unkeyed:" + canonical_turn_digest(
        source_body.model_copy(update={"context": body.context}))
    source_recorded = False
    if source_messages:
        from protagine.turns import canonical_turn_digest, get_turn_idempotency_ledger
        ledger = get_turn_idempotency_ledger(get_state_dir())
        if ledger.is_source_erased(source_id, body.context.contact_id):
            return TurnSyncResponse(accepted=False, continuity_updated=False, skipped_reason="source_erased")
        retained = ledger.retained_messages(contact_id=body.context.contact_id, session_id=body.context.session_id, messages=source_messages)
        if not retained:
            return TurnSyncResponse(accepted=False, continuity_updated=False, skipped_reason="source_erased")
        from protagine.turns.idempotency import SourceErased
        try:
            ledger.record_source(
                source_id, contact_id=body.context.contact_id,
                session_id=body.context.session_id, messages=source_messages,
                occurred_at=(body.context.metadata or {}).get("occurred_at"),
                timezone_name=body.context.timezone,
                derive_claims=not getattr(getattr(request, 'state', None), 'task_instruction_only', False),
                channel_id=body.context.channel_id,
            )
        except SourceErased:
            return TurnSyncResponse(accepted=False, continuity_updated=False, skipped_reason="source_erased")
        except ValueError as exc:
            if str(exc) == 'invalid_source_dependency':
                raise HTTPException(status_code=422, detail={'code': 'invalid_source_dependency'}) from exc
            raise
        source_recorded = True
        if retained != source_messages or ledger.is_projection_erased(source_id):
            # Preserve unrelated evidence, but never run extractors on an
            # envelope whose summary or tools may repeat erased content.
            return TurnSyncResponse(accepted=False, source_recorded=True, continuity_updated=False, skipped_reason="source_erased")
        if body.source_only:
            # record_source still schedules grounded USER claim projection.
            # Only the ordinary summary/tool/relationship effects are skipped.
            return TurnSyncResponse(accepted=True, source_recorded=True, continuity_updated=False, skipped_reason='source_survivor_only')

    # If structured fields are empty but raw messages are present,
    # extract topics/entities/summary from the raw messages.
    if not body.topics and not body.entities and not body.summary:
        if body.user_message is not None or body.assistant_message is not None:
            user_text = body.user_message.content if body.user_message else ""
            asst_text = body.assistant_message.content if body.assistant_message else ""
            combined = f"User: {user_text}\nAssistant: {asst_text}".strip()
            if combined and combined != "User: \nAssistant:":
                body.summary = combined[:2000]
                # Extract rough topics from user message
                words = user_text.split()
                body.topics = [w.lower().strip(".,!?;:") for w in words if len(w) > 4][:10]

    # Commitment capture runs in the projection worker (commitments/extract.py) on the router.

    # Track last user message for concurrent-session safety (v0.13.0)
    if body.user_message is not None:
        try:
            from protagine.util.session_safety import save_last_user_message_at
            save_last_user_message_at()
        except Exception:
            pass

    # Owner directive learning: when the owner explicitly states how they want
    # their assistant to communicate ("be concise", "use bullets", "no emoji"),
    # capture it deterministically at high confidence. Owner-only; ordinary
    # conversation never trips this (requires a style keyword + a directive cue).
    if _preference_learner is not None and body.user_message is not None:
        try:
            from protagine.identity import get_owner_contact_id
            owner_id = get_owner_contact_id()
            if owner_id and body.context.contact_id == owner_id:
                if getattr(_preference_learner, 'perspective', None) is not None:
                    changes = _preference_learner.learn_source(source_id) if source_recorded else []
                    hit = (changes[0][0].split('.', 1)[0], changes[0][0], changes[0][1]) if changes else None
                else:
                    hit = await _preference_learner.learn_directive(
                        getattr(body.user_message, "content", "") or ""
                    )
                if hit is not None:
                    try:
                        from protagine.events.broadcaster import emit as _emit
                        _emit("preference.directive_learned",
                              {"category": hit[0], "key": hit[1], "value": hit[2]})
                    except Exception:
                        pass
        except Exception:
            logger.debug("owner directive learning failed", exc_info=True)

    if _telemetry is not None:
        try:
            await _telemetry.touch("last_sync_at")
        except Exception:
            pass

    # Timeline spine (v0.21.0): journal the conversation turn so it lands on
    # the unified timeline, and bump the contact's recency (last_interaction_at).
    if body.summary:
        try:
            from protagine.events.journal import append_event
            turn_event_data = {
                "contact_id": body.context.contact_id,
                "session_id": body.context.session_id,
                "channel_id": body.context.channel_id,   # cross-channel provenance for the timeline / handoff
                "summary": (body.summary or "")[:300],
                "topics": (body.topics or [])[:10],
                "tools_used": (body.tools_used or [])[:20],
            }
            append_event("conversation.turn", turn_event_data)
        except Exception:
            logger.debug("journal conversation.turn failed", exc_info=True)
    # Mining: verbatim turn capture + escalation detection (best-effort; the
    # miner mode gates everything internally, see protagine/mining/).
    try:
        from protagine.api.routers.mining import get_mining_engine as _get_miner
        _miner = _get_miner()
        if _miner is not None:
            _miner.observe_turn(
                session_id=body.context.session_id,
                contact_id=body.context.contact_id,
                channel_id=body.context.channel_id or "",
                user_text=(getattr(body.user_message, "content", "") or "")
                          if body.user_message else "",
                assistant_text=(getattr(body.assistant_message, "content", "") or "")
                               if body.assistant_message else "",
                summary=body.summary or "",
                tools_used=body.tools_used,
                model=body.model or "",
            )
    except Exception:
        logger.debug("mining observe_turn failed", exc_info=True)
    # Opt-out: a contact who asks not to be messaged lowers their own may_contact
    # to never (only the owner ever raises it). Runs on the resolved sender.
    if (_contacts_store is not None and body.context.contact_id and not _is_system_turn
            and body.user_message is not None):
        try:
            from protagine.contacts.optout import apply_opt_out
            from protagine.identity import get_owner_contact_id
            await apply_opt_out(_contacts_store, body.context.contact_id,
                                getattr(body.user_message, "content", "") or "",
                                source_ref=f"turn:{source_id}", owner_id=get_owner_contact_id())
        except Exception:
            logger.warning("opt-out detection failed", exc_info=True)
    try:
        if _contacts_store is not None and body.context.contact_id and not _is_system_turn:
            await _contacts_store.record_interaction(body.context.contact_id,
                                                     at_iso=_occurred_at(body.context.metadata))
        # Cross-channel communication ledger: record this exchange under the
        # CONVERSATION's channel (group vs DM vs voice provenance), never the
        # contact's primary-handle gateway (which collapsed everything to one
        # channel). New prose requires a canonical person source; transport
        # receipts retain their separate metadata-only ingestion path.
        try:
            if _comms_log is not None and body.context.contact_id and source_recorded:
                _ch = body.context.channel_id or "direct"
                _sess = body.context.session_id or ""
                _lineage, _ = _comms_log.source_input(source_id, body.context.contact_id)
                _comms_log.log(body.context.contact_id, channel=_ch,
                               direction="in",
                               summary=(body.summary or "")[:300],
                               session_id=_sess, source_lineage=_lineage)
                # Record the assistant's reply as an OUTBOUND exchange on the
                # SAME resolved contact + conversation channel. Without this
                # the ledger sees only half of every conversation, so
                # reciprocity and "when did we last talk each way" (which the
                # reachout recommendation depends on) read as never-replying.
                _asst = (getattr(body.assistant_message, "content", "") or ""
                         ) if body.assistant_message else ""
                if _asst.strip():
                    _comms_log.log(body.context.contact_id, channel=_ch,
                                   direction="out",
                                   summary=_asst[:300], session_id=_sess, source_lineage=_lineage)
        except Exception:
            logger.debug("comms ledger log failed", exc_info=True)
    except Exception:
        logger.debug("record_interaction failed", exc_info=True)

    # A turn without messages (the legacy summary-only shape) records nothing:
    # the source ledger is the one memory, and it keeps what was said.
    return TurnSyncResponse(
        accepted=True, continuity_updated=source_recorded, source_recorded=source_recorded,
        skipped_reason=None if source_recorded else "no_source_messages",
    )


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Events (WebSocket)
# ---------------------------------------------------------------------------

@router.websocket("/events")
async def events_ws(ws: WebSocket) -> None:
    await ws.accept()

    # Read auth message. New clients send an exact sequence plus the journal
    # record time; ``lastEventId`` remains accepted as the legacy time cursor.
    last_event_seq: Optional[int] = None
    last_event_time = ""
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
        import json as _json
        msg = _json.loads(raw)
        if msg.get("type") != "auth":
            await ws.close(code=4001, reason="Expected auth message")
            return
        token = msg.get("token", "")
        from protagine.api.auth import configured_api_key, token_matches
        expected = configured_api_key()
        if not expected:
            # Fail closed: without a key we cannot authenticate event-stream
            # subscribers, and this socket carries live state changes.
            await ws.close(
                code=4003,
                reason="API authentication not configured on server",
            )
            return
        if not token_matches(str(token), expected):
            await ws.close(code=4003, reason="Invalid API key")
            return

        raw_seq = msg.get("lastEventSeq")
        legacy_cursor = str(msg.get("lastEventId") or "")
        if raw_seq is None and legacy_cursor.isdigit():
            raw_seq = legacy_cursor
        if raw_seq is not None:
            last_event_seq = int(raw_seq)
            if last_event_seq < 0:
                raise ValueError("lastEventSeq must be non-negative")
        last_event_time = str(msg.get("lastEventTime") or "")
        if not last_event_time and legacy_cursor and not legacy_cursor.isdigit():
            last_event_time = legacy_cursor
    except asyncio.TimeoutError:
        await ws.close(code=4001, reason="Auth timeout")
        return
    except Exception:
        await ws.close(code=4001, reason="Invalid auth")
        return

    subscriber = EventSubscriberBuffer(
        _event_subscriber_queue_size(), loop=asyncio.get_running_loop()
    )

    # Subscribe before capturing the durable high-water mark. Events committed
    # during replay are buffered and later filtered by sequence, closing the
    # replay/live race without duplicate delivery.
    from protagine.events.journal import current_sequence, replay_events
    with _event_broadcast_lock:
        _event_subscribers.append(subscriber)
        replay_through_seq = current_sequence()

    disconnect_event = asyncio.Event()
    receive_task: Optional[asyncio.Task] = None

    async def _watch_disconnect() -> None:
        """Consume post-auth client frames so quiet disconnects are observed."""
        try:
            while True:
                message = await ws.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                # Application-level pings from legacy clients are deliberately
                # consumed. Modern clients use protocol-level WebSocket ping.
        except (WebSocketDisconnect, RuntimeError):
            return
        except Exception:
            logger.debug("Event WebSocket receive watcher stopped", exc_info=True)
            return
        finally:
            disconnect_event.set()

    try:
        cursor_reset = (
            last_event_seq is not None
            and last_event_seq > replay_through_seq
        )
        await ws.send_json({
            "type": "connected",
            "journalHighWaterSeq": replay_through_seq,
            "queueCapacity": subscriber.queue.maxsize,
            "cursorReset": cursor_reset,
        })
        if cursor_reset:
            await ws.send_json({
                "type": "replay_reset",
                "requestedAfterSeq": last_event_seq,
                "journalHighWaterSeq": replay_through_seq,
                "reason": "cursor_ahead_of_journal",
            })

        # A brand-new subscriber starts at the captured high-water mark rather
        # than receiving an arbitrary retention-window history. Reconnects use
        # the exact processed sequence, with timestamp fallback for old clients.
        replay_after_seq = 0 if cursor_reset else last_event_seq
        if replay_after_seq is None and not last_event_time:
            replay_after_seq = replay_through_seq

        replayed_count = 0
        replay_cursor = replay_after_seq
        replay_since = last_event_time
        first_page = True
        last_replayed_seq = min(replay_after_seq or 0, replay_through_seq)

        while True:
            result = await asyncio.to_thread(
                replay_events,
                replay_since,
                1000,
                None,
                False,
                after_seq=replay_cursor,
                until_seq=replay_through_seq,
            )
            if result.get("replayError"):
                await ws.send_json({
                    "type": "replay_error",
                    "reason": result["replayError"],
                })
                await ws.close(code=1011, reason="Event replay unavailable")
                return

            first_available = int(result.get("firstAvailableSeq") or 0)
            if (
                first_page
                and replay_cursor is not None
                and first_available > 0
                and replay_cursor + 1 < first_available
            ):
                await ws.send_json({
                    "type": "replay_gap",
                    "requestedAfterSeq": replay_cursor,
                    "firstAvailableSeq": first_available,
                    "reason": "cursor_precedes_retention_window",
                })
            corrupt_count = int(result.get("corruptCount") or 0)
            if first_page and corrupt_count:
                await ws.send_json({
                    "type": "replay_integrity_warning",
                    "corruptRecordCount": corrupt_count,
                })

            page = result.get("events", [])
            for event in page:
                frame = {
                    "type": event["type"],
                    "occurred_at": event.get("occurredAt") or event["recordedAt"],
                    "recordedAt": event["recordedAt"],
                    "payload": event.get("data", {}),
                    "seq": event["seq"],
                    "eventId": event.get("ulid", ""),
                }
                await ws.send_json(frame)
                subscriber.mark_delivered(frame)
                replayed_count += 1
                last_replayed_seq = max(last_replayed_seq, int(event["seq"]))

            if not result.get("hasMore") or not page:
                break
            replay_cursor = int(result["lastSeq"])
            replay_since = ""
            first_page = False

        # Always terminate the handshake explicitly. This means an idle stream
        # never requires a client to guess whether replay has completed.
        await ws.send_json({
            "type": "replay_complete",
            "replayedCount": replayed_count,
            "lastSeq": last_replayed_seq,
            "replayThroughSeq": replay_through_seq,
        })
        subscriber.mark_delivered({"seq": replay_through_seq})

        receive_task = asyncio.create_task(_watch_disconnect())
        while True:
            event_task = asyncio.create_task(subscriber.get())
            disconnect_task = asyncio.create_task(disconnect_event.wait())
            done, _ = await asyncio.wait(
                {event_task, disconnect_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if disconnect_task in done:
                event_task.cancel()
                try:
                    await event_task
                except asyncio.CancelledError:
                    pass
                return
            disconnect_task.cancel()
            try:
                await disconnect_task
            except asyncio.CancelledError:
                pass
            event = event_task.result()
            if subscriber.is_overflow(event):
                await ws.send_json(subscriber.overflow_frame())
                await ws.close(
                    code=1013,
                    reason="Event subscriber overflow; reconnect and replay",
                )
                return
            # Frames at or below the replay snapshot were both journaled and
            # buffered while the replay ran. They have already been delivered.
            try:
                seq = int(event.get("seq", 0))
            except (TypeError, ValueError):
                seq = 0
            if seq and seq <= replay_through_seq:
                continue
            await ws.send_json(event)
            subscriber.mark_delivered(event)
    except WebSocketDisconnect:
        pass
    finally:
        if receive_task is not None and not receive_task.done():
            receive_task.cancel()
            try:
                await receive_task
            except asyncio.CancelledError:
                pass
        subscriber.close()
        with _event_broadcast_lock:
            try:
                _event_subscribers.remove(subscriber)
            except ValueError:
                pass


@router.get("/events/replay")
async def events_replay(
    since: str = Query("", description="ISO 8601 journal time — replay events after this time"),
    limit: int = Query(500, ge=1, le=1000, description="Max events to return"),
    types: Optional[str] = Query(None, description="Comma-separated event type filter"),
    after_seq: Optional[int] = Query(
        None,
        alias="afterSeq",
        ge=0,
        description="Exact sequence cursor; takes precedence over since",
    ),
    until_seq: Optional[int] = Query(
        None,
        alias="untilSeq",
        ge=0,
        description="Optional inclusive replay high-water sequence",
    ),
) -> dict:
    """Replay journal events for disconnected clients.

    Returns events in sequential order. Prefer ``afterSeq`` from the last
    processed WebSocket frame; ``since`` supports legacy timestamp clients.
    """
    from protagine.events.journal import replay_events

    type_list = [t.strip() for t in types.split(",")] if types else None
    return replay_events(
        since=since,
        limit=limit,
        types=type_list,
        after_seq=after_seq,
        until_seq=until_seq,
    )


# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------

_goals_store = None

def set_goals_store(store) -> None:
    global _goals_store
    _goals_store = store


@router.get("/goals", response_model=GoalListResponse)
async def list_goals(person_id: Optional[str] = None, status_filter: Optional[str] = None) -> GoalListResponse:
    if _goals_store is None:
        raise HTTPException(status_code=501, detail=_NOT_WIRED)
    try:
        from protagine.goals.models import GoalStatus
        status_enum = None
        if status_filter:
            try:
                status_enum = GoalStatus(status_filter)
            except ValueError:
                pass
        goals = _goals_store.list_goals(status=status_enum)
        return GoalListResponse(goals=[
            GoalResponse(
                id=g.goal_id,
                title=g.title,
                description=g.description,
                status=g.status.value if hasattr(g.status, "value") else str(g.status),
                priority=g.priority.name.lower() if hasattr(g.priority, "name") else str(g.priority),
                progress=g.progress_pct,
                parent_goal_id=g.parent_goal_id,
                person_id=None,
                created_at=str(g.created_at) if g.created_at else None,
                updated_at=str(g.updated_at) if g.updated_at else None,
                dispatch_unavailable=g.context.get('dispatch_unavailable'),
                completion_basis=g.context.get('completion_basis'),
            ) for g in goals
        ])
    except Exception as exc:
        logger.warning("list_goals failed (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "code": "goals_unavailable",
                    "message": "Goals backend unavailable",
                },
            },
        ) from None


@router.get("/goals/{goal_id}", response_model=GoalResponse)
async def get_goal(goal_id: str) -> GoalResponse:
    if _goals_store is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    try:
        goal = _goals_store.get_goal(goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        return GoalResponse(
            id=goal.goal_id,
            title=goal.title,
            description=goal.description,
            status=goal.status.value if hasattr(goal.status, "value") else str(goal.status),
            priority=goal.priority.name.lower() if hasattr(goal.priority, "name") else str(goal.priority),
            progress=goal.progress_pct,
            parent_goal_id=goal.parent_goal_id,
            person_id=None,
            created_at=str(goal.created_at) if goal.created_at else None,
            updated_at=str(goal.updated_at) if goal.updated_at else None,
            dispatch_unavailable=goal.context.get('dispatch_unavailable'),
            completion_basis=goal.context.get('completion_basis'),
        )
    except HTTPException:
        raise
    except GoalNotFoundError:
        raise HTTPException(status_code=404, detail="Goal not found")
    except Exception as exc:
        logger.warning("get_goal failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.patch("/goals/{goal_id}", response_model=GoalResponse)
async def update_goal(goal_id: str, body: GoalUpdateRequest) -> GoalResponse:
    if _goals_store is None:
        raise HTTPException(status_code=501, detail=_NOT_WIRED)
    try:
        # Map status string to the appropriate state transition
        if body.status:
            status_lower = body.status.lower()
            if status_lower in ("completed", "done"):
                # Completion is an explicit report through this API, not a
                # request to accept or dispatch the goal again.
                goal = _goals_store.get_goal(goal_id)
                if not _goals_store.complete_task(goal_id):
                    raise HTTPException(status_code=409, detail="Goal is already abandoned")
                goal = _goals_store.get_goal(goal_id)
            elif status_lower == "blocked":
                goal = _goals_store.block_goal(
                    goal_id, reason=body.notes or "Blocked via API",
                    condition_type=body.condition_type,
                    condition_params=body.condition_params)
            elif status_lower == "unblocked":
                goal = _goals_store.unblock_goal(goal_id)
            elif status_lower == "abandoned":
                goal = _goals_store.abandon_goal(goal_id, reason=body.notes or "Abandoned via API")
            else:
                goal = _goals_store.get_goal(goal_id)
        else:
            goal = _goals_store.get_goal(goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        return GoalResponse(
            id=goal.goal_id,
            title=goal.title,
            description=goal.description,
            status=goal.status.value if hasattr(goal.status, "value") else str(goal.status),
            priority=goal.priority.name.lower() if hasattr(goal.priority, "name") else str(goal.priority),
            progress=goal.progress_pct,
            parent_goal_id=goal.parent_goal_id,
            person_id=None,
            created_at=str(goal.created_at) if goal.created_at else None,
            updated_at=str(goal.updated_at) if goal.updated_at else None,
            dispatch_unavailable=goal.context.get('dispatch_unavailable'),
            completion_basis=goal.context.get('completion_basis'),
        )
    except HTTPException:
        raise
    except GoalNotFoundError:
        raise HTTPException(status_code=404, detail="Goal not found") from None
    except Exception as exc:
        logger.warning("update_goal failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

_contacts_store = None

def set_contacts_store(store) -> None:
    global _contacts_store
    _contacts_store = store


@router.get("/contacts", response_model=ContactListResponse)
async def list_contacts(
    source: Optional[str] = None,
    trust_tier: Optional[str] = None,
    include_discovered: bool = True,
    limit: int = 100,
) -> ContactListResponse:
    if _contacts_store is None:
        return ContactListResponse(contacts=[])
    try:
        contacts: List[Any] = []

        # Determine which sources to include
        include_curated = source in (None, "all", "curated")
        include_world = source in (None, "all", "world_model")
        if source is None:
            include_world = include_discovered

        if include_curated:
            curated = await _contacts_store.list(
                trust_tier=trust_tier,
                limit=limit,
            )
            contacts.extend(curated)

        if include_world:
            world = await _contacts_store.list(
                trust_tier=trust_tier or "acquaintance",
                limit=limit,
            )
            # Exclude already-included curated contacts
            curated_ids = {c.contact_id for c in contacts}
            for c in world:
                if c.contact_id not in curated_ids and c.import_source == "world_model":
                    contacts.append(c)

        # Sort: curated first (by trust tier rank desc), then world model
        from protagine.contacts.models import _TIER_RANK
        def _sort_key(c):
            is_curated = 1 if c.import_source != "world_model" else 0
            tier_rank = _TIER_RANK.get(c.trust_tier, 0)
            last_int = c.last_interaction_at or ""
            return (is_curated, tier_rank, last_int)

        contacts.sort(key=_sort_key, reverse=True)
        contacts = contacts[:limit]

        return ContactListResponse(
            contacts=[ContactResponse(**c.to_dict()) for c in contacts],
            source_filter=source or "all",
            total=len(contacts),
        )
    except Exception as exc:
        logger.warning("list_contacts failed: %s", exc)
        return ContactListResponse(contacts=[])


@router.post("/contacts", response_model=ContactResponse, status_code=201)
async def create_contact(body: ContactCreateRequest) -> ContactResponse:
    """Create a curated contact (with optional handles) via the API.

    Exists primarily so deployments can bootstrap the OWNER contact; before
    this, contacts could only appear as side effects of message ingestion.
    """
    if _contacts_store is None:
        raise HTTPException(status_code=501, detail="Contact store not initialized")
    try:
        contact = await _contacts_store.create(
            display_name=body.display_name,
            given_name=body.given_name,
            family_name=body.family_name,
            organization=body.organization,
            trust_tier=body.trust_tier,
            may_contact=body.may_contact,
            cadence_minutes=body.cadence_minutes,
            tags=body.tags,
            notes=body.notes,
            import_source="manual",
        )
        for handle in body.handles:
            await _contacts_store.add_handle(
                contact.contact_id,
                gateway=handle.gateway,
                address=handle.address,
                is_primary=handle.is_primary,
                verified=handle.verified,
                source="manual",
            )
        created = await _contacts_store.get(contact.contact_id)
        return ContactResponse(**(created or contact).to_dict())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.warning("create_contact failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/contacts/intro", response_model=ContactIntroResponse, status_code=201)
async def capture_introduction(body: ContactIntroRequest) -> ContactIntroResponse:
    """Capture an organic introduction (social-graph autonomy, generic).

    The agent met or learned of a person; record them as a durable, queryable
    graph node WITH provenance (introduced_by + met_via). If the handle already
    resolves to a known contact, the provenance is recorded on that contact
    instead of duplicating it. A new contact gets may_contact='ask': an intro
    never grants unprompted outreach; only the owner raises it.
    """
    if _contacts_store is None:
        raise HTTPException(status_code=501, detail="Contact store not initialized")
    try:
        # If a handle was given and already resolves to a known person, annotate
        # rather than duplicate (a number is one identity across phone gateways).
        existing = None
        if body.gateway and body.address:
            existing = await _contacts_store.resolve_messaging_handle(
                body.gateway, body.address)
        if existing is not None:
            updated = await _contacts_store.record_introduction(
                existing.contact_id,
                introduced_by=body.introduced_by,
                met_via=body.met_via,
            )
            return ContactIntroResponse(
                contact=ContactResponse(**(updated or existing).to_dict()),
                created=False,
            )

        contact = await _contacts_store.create(
            display_name=body.name,
            trust_tier=body.trust_tier,
            may_contact="ask",
            import_source="agent_intro",
            notes=body.note,
            introduced_by=body.introduced_by,
            met_via=body.met_via,
        )
        if body.gateway and body.address:
            try:
                # The handle keeps its transport; the store matches phone numbers
                # across gateways on the canonical phone identity.
                await _contacts_store.add_handle(
                    contact.contact_id, gateway=body.gateway,
                    address=body.address, source="agent_intro")
            except ValueError as exc:
                # Handle raced onto another contact between resolve and create.
                logger.info("intro add_handle conflict for %s: %s",
                            contact.contact_id, exc)
        created = await _contacts_store.get(contact.contact_id)
        return ContactIntroResponse(
            contact=ContactResponse(**(created or contact).to_dict()),
            created=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.warning("capture_introduction failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/contacts/resolve", response_model=ContactResponse)
async def resolve_contact_by_handle(gateway: str, address: str, request: Request, create: bool = False) -> ContactResponse:
    """Resolve a contact from a messaging handle (v0.21.2).

    Registered BEFORE /contacts/{contact_id} so it isn't shadowed by the
    parameterized route. Lets the host plugin map an inbound sender
    (platform + address) to the real Protagine contact, so per-contact
    memory/affect/facts engage instead of pooling everything under 'default'.

    With ``create=true`` an unknown messaging sender becomes a shadow contact
    through the participant ladder, the one shadow creator (trust_tier=unknown,
    may_contact='ask'), so its memory attributes to a real person instead of
    being lost; merge and link proposals reconcile it later.
    """
    if _contacts_store is None:
        raise HTTPException(status_code=404, detail="Contact store not initialized")
    try:
        # Normalized, cross-gateway phone-identity resolution (a number is one contact regardless of
        # the transport it arrived on). find_by_handle stays exact-match for dedup callers.
        contact = await _contacts_store.resolve_messaging_handle(gateway, address)
        if contact is None and create and gateway and address:
            from protagine.identity.participants import ParticipantResolver
            resolution = await ParticipantResolver(_contacts_store).resolve(
                platform=gateway, user_id=address, channel_id=gateway)
            if resolution.contact_id:
                contact = await _contacts_store.get(resolution.contact_id)
        if contact is None:
            raise HTTPException(status_code=404, detail="No contact for that handle")
        return ContactResponse(**contact.to_dict())
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("resolve_contact_by_handle failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/contacts/{contact_id}", response_model=ContactResponse)
async def get_contact(contact_id: str) -> ContactResponse:
    if _contacts_store is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    try:
        contact = await _contacts_store.get(contact_id)
        if contact is None:
            raise HTTPException(status_code=404, detail="Contact not found")
        return ContactResponse(**contact.to_dict())
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("get_contact failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/authz/scope", response_model=ScopeAuthzResponse)
async def authz_scope(platform: str, external_id: str, gateway: str, address: str) -> ScopeAuthzResponse:
    """Is this sender authorized WITHIN this group scope? Context-scoped only — it
    says nothing about 1:1 DM rights. Used by the messaging bridge to admit group
    members of an agent-created/joined group without granting them per-user access."""
    if _contacts_store is None:
        return ScopeAuthzResponse(authorized=False)
    try:
        scope = await _contacts_store.get_scope(platform=platform, external_id=external_id)
        if scope is None or not scope.active:
            return ScopeAuthzResponse(
                authorized=False,
                scope_id=(scope.scope_id if scope else None),
                active=bool(scope and scope.active),
            )
        contact = await _contacts_store.resolve_messaging_handle(gateway, address)
        if contact is None:
            return ScopeAuthzResponse(
                authorized=False, scope_id=scope.scope_id,
                granted_tier=scope.granted_tier, active=True,
            )
        ok = await _contacts_store.is_authorized_in_scope(contact.contact_id, scope.scope_id)
        return ScopeAuthzResponse(
            authorized=ok, scope_id=scope.scope_id, granted_tier=scope.granted_tier,
            contact_id=contact.contact_id, active=True,
        )
    except Exception as exc:
        logger.warning("authz_scope failed: %s", exc)
        return ScopeAuthzResponse(authorized=False)


@router.post("/authz/scope", response_model=ScopeResponse, status_code=201)
async def create_authz_scope(body: ScopeCreateRequest) -> ScopeResponse:
    """Create (idempotent by platform+external_id) a trust scope and add members.
    Unknown member handles are auto-created as shadow contacts on first sight
    (acquaintance tier, no 1:1 interaction) — see social-graph-autonomy spec."""
    if _contacts_store is None:
        raise HTTPException(status_code=501, detail="Contact store not initialized")
    try:
        scope = await _contacts_store.create_scope(
            scope_type=body.scope_type, platform=body.platform, external_id=body.external_id,
            label=body.label, granted_tier=body.granted_tier, created_by=body.created_by,
        )
        member_ids: List[str] = []
        for m in body.members:
            cid = m.contact_id
            if cid is None and m.gateway and m.address:
                contact = await _contacts_store.resolve_messaging_handle(m.gateway, m.address)
                if contact is None:
                    contact = await _contacts_store.create(
                        display_name=(m.name or m.address),
                        trust_tier="acquaintance", import_source="agent_scope",
                    )
                    # rcs is a transport over the phone identity; store the handle under the
                    # canonical phone gateway (sms) so it resolves across phone-bearing channels.
                    store_gw = "sms" if m.gateway == "rcs" else m.gateway
                    await _contacts_store.add_handle(
                        contact.contact_id, gateway=store_gw, address=m.address,
                        source="agent_scope", confidence=0.6,
                    )
                cid = contact.contact_id
            if cid:
                await _contacts_store.add_scope_member(scope.scope_id, cid, role=m.role)
                member_ids.append(cid)
        return ScopeResponse(
            scope_id=scope.scope_id, scope_type=scope.scope_type, platform=scope.platform,
            external_id=scope.external_id, label=scope.label, granted_tier=scope.granted_tier,
            active=scope.active, members=member_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.warning("create_authz_scope failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/authz/scope/deactivate")
async def deactivate_authz_scope(body: ScopeDeactivateRequest) -> Dict[str, Any]:
    """Revoke group-trust for a whole scope at once (e.g. owner mutes/leaves the group)."""
    if _contacts_store is None:
        raise HTTPException(status_code=501, detail="Contact store not initialized")
    try:
        scope_id = body.scope_id
        if scope_id is None and body.platform and body.external_id:
            scope = await _contacts_store.get_scope(platform=body.platform, external_id=body.external_id)
            scope_id = scope.scope_id if scope else None
        if scope_id is None:
            raise HTTPException(status_code=404, detail="scope not found")
        await _contacts_store.deactivate_scope(scope_id)
        return {"ok": True, "scope_id": scope_id, "active": False}
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("deactivate_authz_scope failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/scopes/promotion-candidates")
async def scope_promotion_candidates() -> Dict[str, Any]:
    """Group-scope members with sustained contact but no 1:1 rights yet: the people the owner
    can promote (group_guest -> regular). They are proposals; only the owner promotes."""
    if _contacts_store is None:
        raise HTTPException(status_code=501, detail="Contact store not initialized")
    cfg = getattr(_contacts_store, "_config", None)
    min_int = int(getattr(cfg, "group_promote_min_interactions", 5))
    cands = await _contacts_store.group_promotion_candidates(min_interactions=min_int)
    return {
        "min_interactions": min_int,
        "candidates": [
            {"contact_id": c.contact_id, "display_name": c.display_name,
             "trust_tier": c.trust_tier, "interaction_count": c.interaction_count}
            for c in cands
        ],
    }


@router.post("/scopes/promote")
async def scope_promote(body: ScopePromoteRequest) -> Dict[str, Any]:
    """Promote one group-scope member to global 1:1 (tier >= to_tier). Only ever raises the
    tier, never the permission to message them. Called after the owner's approval."""
    if _contacts_store is None:
        raise HTTPException(status_code=501, detail="Contact store not initialized")
    try:
        changed = await _contacts_store.promote_scope_member(body.contact_id, to_tier=body.to_tier)
    except Exception as exc:
        logger.warning("scope_promote failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True, "contact_id": body.contact_id, "changed": changed}


@router.post("/contacts/{contact_id}/timezone", response_model=ContactResponse)
async def set_contact_timezone(contact_id: str, body: ContactTimezoneRequest) -> ContactResponse:
    """Set (or clear, with null) a contact's IANA timezone (v0.21.0, editable)."""
    if _contacts_store is None:
        raise HTTPException(status_code=501, detail="Contact store not initialized")
    try:
        await _contacts_store.set_timezone(contact_id, body.timezone)
        contact = await _contacts_store.get(contact_id)
        if contact is None:
            raise HTTPException(status_code=404, detail="Contact not found")
        return ContactResponse(**contact.to_dict())
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.warning("set_contact_timezone failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/temporal/config", response_model=TemporalConfigResponse)
async def get_temporal_config() -> TemporalConfigResponse:
    """Current temporal reference frame (agent home tz + defaults). v0.21.0."""
    from protagine.util import temporal as _temporal
    atz = _temporal.agent_timezone()
    return TemporalConfigResponse(
        agent_timezone=atz,
        default_contact_timezone=_temporal.default_contact_timezone(),
        now_utc=_temporal.now_utc().isoformat(),
        now_agent_local=_temporal.now_in(atz).isoformat(),
        agent_local_clock=_temporal.format_clock(_temporal.now_in(atz)),
    )


@router.post("/temporal/config", response_model=TemporalConfigResponse)
async def set_temporal_config(body: TemporalConfigRequest) -> TemporalConfigResponse:
    """Edit the agent home tz and/or the default contact tz. v0.21.0."""
    from protagine.util import temporal as _temporal
    try:
        if body.agent_timezone is not None:
            _temporal.set_agent_timezone(body.agent_timezone)
        if body.clear_default_contact_timezone:
            _temporal.set_default_contact_timezone(None)
        elif body.default_contact_timezone is not None:
            _temporal.set_default_contact_timezone(body.default_contact_timezone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    atz = _temporal.agent_timezone()
    return TemporalConfigResponse(
        agent_timezone=atz,
        default_contact_timezone=_temporal.default_contact_timezone(),
        now_utc=_temporal.now_utc().isoformat(),
        now_agent_local=_temporal.now_in(atz).isoformat(),
        agent_local_clock=_temporal.format_clock(_temporal.now_in(atz)),
    )


_TIMELINE_TYPE_LABELS = {
    "conversation.turn": "💬 talked",
    "outreach.sent": "📤 reached out",
    "initiative.generated": "💡 initiative",
    "initiative.completed": "✅ initiative done",
    "task.created": "📋 task created",
    "task.completed": "✅ task done",
    "commitment.made": "🤝 promised",
    "commitment.fulfilled": "✅ kept promise",
    "memory.written": "🧠 remembered",
}


@router.get("/timeline", response_model=TimelineResponse)
async def get_timeline(
    since: str = Query("24h", description="Relative ('24h','7d') or ISO, or today/yesterday"),
    types: Optional[str] = Query(None, description="Comma-separated event types to include"),
    contact_id: Optional[str] = Query(None, description="Only events involving this contact"),
    limit: int = Query(100, ge=1, le=500),
) -> TimelineResponse:
    """Chronological timeline across all Protagine subsystems (v0.21.0).

    Backed by the event journal. Lets the agent answer 'what happened recently',
    'what's been going on with X', 'what changed since I last looked'.
    """
    from protagine.events.journal import replay_events
    from protagine.util import temporal as _t

    since_iso = _t.parse_relative_since(since)
    type_list = [t.strip() for t in types.split(",") if t.strip()] if types else None
    # Walk the journal newest-first so the cap drops the OLD end of a large
    # window — an oldest-first walk here showed stale activity while the
    # endpoint claimed "newest first" whenever the window exceeded the cap.
    raw = replay_events(since_iso, limit=max(500, limit), types=type_list,
                        newest_first=True)

    all_events: list[TimelineEvent] = []
    for e in raw.get("events", []):
        data = e.get("data", {}) or {}
        cid = data.get("contact_id") or data.get("person_id")
        if contact_id and cid != contact_id:
            continue
        at = e.get("recordedAt", "")
        all_events.append(TimelineEvent(
            seq=e.get("seq", 0),
            type=e.get("type", "unknown"),
            at=at,
            when=_t.humanize_delta(at),
            bucket=_t.bucket(at),
            summary=(data.get("summary") or data.get("title") or data.get("text")
                     or data.get("reason")),
            contact_id=cid,
            data=data,
        ))

    all_events.sort(key=lambda x: x.seq, reverse=True)  # newest first
    events = all_events[:limit]
    has_more = len(all_events) > limit or raw.get("hasMore", False)

    # Resolve contact ids -> names for a readable digest (cached).
    _name_cache: dict = {}
    async def _contact_name(cid):
        if not cid or cid == "default":
            return None
        if cid in _name_cache:
            return _name_cache[cid]
        nm = cid
        if _contacts_store is not None:
            try:
                c = await _contacts_store.get(cid)
                if c is not None:
                    nm = c.display_name or c.given_name or cid
            except Exception:
                pass
        _name_cache[cid] = nm
        return nm

    digest_lines = []
    for ev in events[:40]:
        label = _TIMELINE_TYPE_LABELS.get(ev.type, ev.type)
        snippet = (ev.summary or "").replace("\n", " ").strip()
        if len(snippet) > 120:
            snippet = snippet[:117] + "…"
        nm = await _contact_name(ev.contact_id)
        who = f" with {nm}" if nm else ""
        digest_lines.append(f"• {ev.when} — {label}{who}{': ' + snippet if snippet else ''}")
    digest = ("\n".join(digest_lines)
              if digest_lines else f"No journaled events since {since_iso}.")

    return TimelineResponse(
        since=since_iso,
        count=len(events),
        digest=digest,
        events=events,
        has_more=has_more,
    )


@router.get("/temporal/contacts", response_model=TemporalContactsResponse)
async def temporal_contacts(
    overdue_only: bool = Query(True, description="Only contacts overdue vs their own cadence"),
    limit: int = Query(20, ge=1, le=100),
) -> TemporalContactsResponse:
    """Per-contact cadence + silence (v0.21.0).

    Estimates each contact's typical rhythm and flags those overdue relative to
    *their* cadence. Powers cadence-aware proactive outreach (the worker/agent
    queries this) and the per-turn 'heads-up' line.
    """
    from protagine.util import temporal as _t
    if _contacts_store is None:
        return TemporalContactsResponse(now=_t.now_utc().isoformat(), count=0, contacts=[])
    rows = await _contacts_store.compute_cadence_overdue(
        overdue_only=overdue_only, limit=limit,
    )
    return TemporalContactsResponse(
        now=_t.now_utc().isoformat(),
        count=len(rows),
        contacts=[TemporalContact(**r) for r in rows],
    )


@router.post("/contacts/{contact_id}/style", response_model=ContactStyleResponse)
async def get_contact_style(contact_id: str, body: ContactStyleRequest) -> ContactStyleResponse:
    if _contacts_store is None:
        return ContactStyleResponse(person_id=contact_id)
    try:
        style = await _contacts_store.get_style(contact_id)
        return ContactStyleResponse(person_id=contact_id, **style)
    except Exception as exc:
        logger.warning("get_contact_style failed: %s", exc)
        return ContactStyleResponse(person_id=contact_id)


# ---------------------------------------------------------------------------
# Briefings
# ---------------------------------------------------------------------------

_briefings_engine = None

def set_briefings_engine(engine) -> None:
    global _briefings_engine
    _briefings_engine = engine


def _briefing_to_response(b) -> BriefingResponse:
    """Map a stored briefing onto the API schema.

    The engine returns ``briefings.models.Briefing`` dataclasses, not dicts —
    unpacking them with ``**`` was the failure that silently emptied this
    endpoint. Plain dicts are still accepted for forward compatibility.
    """
    if isinstance(b, dict):
        return BriefingResponse(**b)
    sections = [
        s for s in (getattr(b, "sections", None) or [])
        if not getattr(s, "suppressed", False)
    ]
    body = "\n\n".join(
        n for n in (getattr(s, "narrative", "") for s in sections) if n
    )
    btype = getattr(b, "briefing_type", None)
    btype_str = getattr(btype, "value", btype) if btype is not None else None
    created = getattr(b, "created_at", None)
    return BriefingResponse(
        id=str(getattr(b, "briefing_id", "") or ""),
        title=f"{btype_str} briefing" if btype_str else None,
        body=body,
        briefing_type=btype_str,
        created_at=created.isoformat() if hasattr(created, "isoformat")
                   else (str(created) if created else None),
    )


@router.get("/briefings", response_model=BriefingListResponse)
async def list_briefings(limit: int = 10) -> BriefingListResponse:
    if _briefings_engine is None:
        return BriefingListResponse(briefings=[])
    try:
        briefings = _briefings_engine.get_recent(limit=limit)
        return BriefingListResponse(
            briefings=[_briefing_to_response(b) for b in briefings])
    except Exception as exc:
        # A store/mapping failure must surface, never masquerade as an empty
        # briefing list (200 [] is indistinguishable from "no briefings").
        logger.warning("list_briefings failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"list_briefings failed: {type(exc).__name__}: {exc}",
        )


# ---------------------------------------------------------------------------
# Cognition
# ---------------------------------------------------------------------------

_metalearner = None

def set_metalearner(learner) -> None:
    global _metalearner
    _metalearner = learner


# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------

_research_pipeline = None

def set_research_pipeline(pipeline) -> None:
    global _research_pipeline
    _research_pipeline = pipeline


_search_orchestrator = None


def set_search_orchestrator(orchestrator) -> None:
    global _search_orchestrator
    _search_orchestrator = orchestrator


@router.get("/search/providers")
async def list_search_providers():
    if _search_orchestrator is None:
        return {"providers": [], "available": False}
    return {
        "providers": _search_orchestrator.list_providers(),
        "available": _search_orchestrator.has_providers,
    }


@router.post("/search")
async def search(body: dict):
    if _search_orchestrator is None or not _search_orchestrator.has_providers:
        raise HTTPException(status_code=501, detail="No search provider configured")
    query = body.get("query", "")
    max_results = body.get("max_results", 5)
    provider = body.get("provider", "")
    results = await _search_orchestrator.search(query, max_results, provider)
    return {
        "results": [
            {"title": r.title, "url": r.url, "snippet": r.snippet, "source": r.source}
            for r in results
        ],
        "count": len(results),
    }


@router.post("/research/start", response_model=ResearchRunResponse)
async def start_research(body: ResearchStartRequest) -> ResearchRunResponse:
    if _research_pipeline is None:
        raise HTTPException(status_code=501, detail=_NOT_WIRED)
    try:
        depth_map = {"quick": 1, "standard": 3, "deep": 5}
        depth_map.get(body.depth or "standard", 3)
        run = await _research_pipeline.run(goal=body.topic, metadata={"depth": body.depth, "person_id": body.person_id})
        return ResearchRunResponse(
            run_id=run.id,
            topic=body.topic,
            status=run.status.value if hasattr(run.status, "value") else str(run.status),
            stages_completed=[run.current_stage.value if hasattr(run.current_stage, "value") else str(run.current_stage)],
            artifact=run.artifact.__dict__ if run.artifact and hasattr(run.artifact, "__dict__") else (run.artifact if isinstance(run.artifact, dict) else None),
        )
    except Exception as exc:
        logger.warning("start_research failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/research", response_model=ResearchListResponse)
async def list_research(limit: int = 20, status_filter: Optional[str] = Query(None, alias="status")) -> ResearchListResponse:
    if _research_pipeline is None:
        return ResearchListResponse(runs=[])
    try:
        runs = _research_pipeline.list_runs(status=status_filter, limit=limit)
        return ResearchListResponse(runs=[
            ResearchRunResponse(
                run_id=r.id,
                topic=r.goal,
                status=r.status.value if hasattr(r.status, "value") else str(r.status),
                stages_completed=[
                    r.current_stage.value if hasattr(r.current_stage, "value") else str(r.current_stage)
                ],
                artifact=(
                    r.artifact.__dict__ if r.artifact and hasattr(r.artifact, "__dict__")
                    else (r.artifact if isinstance(r.artifact, dict) else None)
                ),
                created_at=r.created_at.isoformat() if r.created_at else None,
            )
            for r in runs
        ])
    except Exception as exc:
        logger.warning("list_research failed: %s", exc)
        return ResearchListResponse(runs=[])


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------

_connection_discoverer = None
_insight_store = None

def set_connection_discoverer(discoverer) -> None:
    global _connection_discoverer
    _connection_discoverer = discoverer


def set_insight_store(store) -> None:
    global _insight_store
    _insight_store = store


@router.post("/synthesis/discover", response_model=SynthesisDiscoverResponse)
async def discover_connections(body: SynthesisDiscoverRequest) -> SynthesisDiscoverResponse:
    if _connection_discoverer is None:
        return SynthesisDiscoverResponse(connections=[])
    try:
        connections = await _connection_discoverer.discover_connections(
            person_id=body.person_id,
            min_novelty=body.min_novelty or 0.3,
        )
        results = []
        for c in connections:
            results.append(SynthesisConnection(
                id=getattr(c, "id", str(uuid.uuid4())),
                connection_type=getattr(c, "connection_type", "unknown"),
                entities=getattr(c, "entities", []),
                novelty=getattr(c, "novelty", 0.0),
                description=getattr(c, "description"),
            ))
        return SynthesisDiscoverResponse(connections=results)
    except Exception as exc:
        logger.warning("discover_connections failed: %s", exc)
        return SynthesisDiscoverResponse(connections=[])


# ---------------------------------------------------------------------------
# Learning
# ---------------------------------------------------------------------------

_learning_feedback_store = None


def set_learning_feedback_store(store) -> None:
    """Wire the one durable correction ledger shared by learning and P4."""
    global _learning_feedback_store
    _learning_feedback_store = store


@router.post("/learning/correction")
async def submit_correction(
    body: LearningCorrectionRequest,
    request: Request,
) -> dict:
    if body.judgment_id is not None or body.judgment_action is not None:
        from protagine.identity import get_owner_contact_id
        owner = get_owner_contact_id()
        person = resolve_request_person(request, context_person_id=body.context.contact_id)
        if not owner or person != owner:
            raise HTTPException(status_code=403, detail='Only the owner can correct an agent judgment')
        perspective = getattr(_self_model, 'perspective', None)
        if perspective is None:
            raise HTTPException(status_code=503, detail='Self perspective is unavailable')
        try:
            outcome = perspective.judgments.correct(body.judgment_id, action=body.judgment_action,
                correction_id=body.correction_id, reason=body.correction, source_id=body.source_id,
                control_turn_id=body.context.turn_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {'accepted': True, 'learned': False, 'judgment': outcome,
                'authority_changed': False}
    if _learning_feedback_store is None:
        return {"accepted": False}
    try:
        from protagine.intelligence.learning.feedback_store import (
            UserCorrection,
        )

        person_id = resolve_request_person(
            request, context_person_id=body.context.contact_id) or ""
        correction = UserCorrection.create(
            original_response=body.original,
            correction_text=body.correction,
            correction_type=body.correction_type,
            context_hash=(body.external_ref or "").strip(),
            person_id=person_id,
        )
        if body.correction_id:
            correction_id = body.correction_id.strip()
            if not correction_id or len(correction_id) > 192:
                raise ValueError("correction_id is malformed")
            correction.correction_id = correction_id
        _learning_feedback_store.record_correction(correction)
    except Exception as exc:
        logger.warning("submit_correction persistence failed: %s", exc)
        return {"accepted": False}
    return {"accepted": True, "correction_id": correction.correction_id}


_commitment_store = None


def set_commitment_store(store):
    global _commitment_store
    _commitment_store = store


_affect_store = None


def set_affect_store(store):
    global _affect_store
    _affect_store = store


_facts_store = None


def set_facts_store(store):
    global _facts_store
    _facts_store = store


_channel_store = None


def set_channel_store(store) -> None:
    """Wire the channel registration store so turn traffic keeps it alive."""
    global _channel_store
    _channel_store = store


def _observe_channel(channel_id: str) -> None:
    """Auto-register a channel on first sighting and keep last_seen_at fresh.

    Channels become first-class registered entities from real traffic alone: a
    surface that has never called /v1/channels/register still appears in the
    registry (as an observed, minimally-described channel) the moment a turn
    flows through it. Explicit registration with a fuller manifest can later
    upsert over this using the channel token.
    """
    if _channel_store is None or not channel_id:
        return
    try:
        if _channel_store.get(channel_id) is None:
            from protagine.channels.manifest import ChannelManifest

            gateway = channel_id.split(":", 1)[0] if ":" in channel_id else channel_id
            _channel_store.register(
                ChannelManifest(
                    channel_key=channel_id,
                    display_name=channel_id,
                    gateway_family=gateway,
                    platform_hint=gateway,
                )
            )
            logger.info("channel observed + auto-registered: %s", channel_id)
        else:
            _channel_store.touch(channel_id)
    except Exception:
        # registration must never affect turn processing
        logger.debug("channel observe failed for %s", channel_id, exc_info=True)


async def _ensure_channel_id(
    context,
    identity=None,
) -> str:
    """Derive a stable channel_id when the host does not provide one.

    Resolution order:
    1. Host-provided channel_id (pass-through)
    2. Active session's gateway + contact_id
    3. Contact's primary handle gateway + contact_id
    4. host_id + contact_id
    5. "unknown:" + contact_id
    """
    if context.channel_id:
        return context.channel_id

    contact = context.contact_id or "anonymous"
    gateway = None

    if _session_store is not None and context.contact_id:
        try:
            session = await _session_store.get_by_contact(context.contact_id)
            if session is not None:
                gateway = session.gateway
        except Exception:
            pass

    if not gateway and _contacts_store is not None and context.contact_id:
        try:
            handles = await _contacts_store.get_handles(context.contact_id)
            primary = next(
                (h for h in handles if getattr(h, "is_primary", False)),
                handles[0] if handles else None,
            )
            if primary is not None:
                gateway = getattr(primary, "gateway", None)
        except Exception:
            pass

    if not gateway and identity is not None:
        gateway = getattr(identity, "host_id", None)

    return f"{gateway or 'unknown'}:{contact}"


_comms_log = None


def set_comms_log(store):
    global _comms_log
    _comms_log = store


@router.get("/comms/recent")
async def comms_recent(limit: int = 50, window_days: int = 30) -> dict:
    """Cross-channel communication ledger: the newest exchanges across every
    contact and channel, plus an inbound/outbound rollup per channel over the
    window. Read-only view over the same ledger that ``/turns/sync`` writes;
    contact ids are resolved to display names for the ops view. System turns
    are recorded in the ledger and returned tagged, not hidden."""
    if _comms_log is None:
        return {"available": False}
    try:
        entries = _comms_log.recent(limit=limit)
        names: dict = {}
        if _contacts_store is not None:
            for cid in {e.get("contact_id") for e in entries if e.get("contact_id")}:
                try:
                    c = await _contacts_store.get(cid)
                    if c is not None:
                        names[cid] = getattr(c, "display_name", None)
                except Exception:
                    pass
        for e in entries:
            e["display_name"] = names.get(e.get("contact_id"))
        return {"available": True, "window_days": int(window_days),
                "entries": entries,
                "by_channel": _comms_log.rollup(since_days=window_days)}
    except Exception as exc:
        return {"available": True, "error": str(exc)}


_preference_learner = None


def set_preference_learner(learner):
    global _preference_learner
    _preference_learner = learner


# --- Directive / boundary memory (owner standing directives + enforcement) ---
_proposal_store = None


def set_proposal_store(store) -> None:
    global _proposal_store
    _proposal_store = store


_feedback_store = None


def set_feedback_store(store) -> None:
    global _feedback_store
    _feedback_store = store


@router.get("/feedback")
async def get_type_feedback() -> dict:
    """Per-type outcome feedback + priority multipliers (observability)."""
    if _feedback_store is None:
        return {"available": False, "types": []}
    try:
        return {"available": True, "types": _feedback_store.snapshot()}
    except Exception as exc:
        return {"available": True, "error": str(exc), "types": []}


# --- Directed action (option A) + read-only repo mirrors ---
_repo_mirrors = None


def set_repo_mirrors(mgr) -> None:
    global _repo_mirrors
    _repo_mirrors = mgr


@router.get("/repos")
async def repos_status() -> dict:
    if _repo_mirrors is None:
        return {"available": False, "repos": {}}
    cfg = _repo_mirrors.configured()
    return {
        "available": True,
        "repos": {name: {"url": info.get("url", ""),
                         "mirrored": _repo_mirrors.path_for(name) is not None}
                  for name, info in cfg.items()},
    }


@router.post("/repos/refresh")
async def repos_refresh() -> dict:
    if _repo_mirrors is None:
        return {"available": False}
    return {"available": True, "results": _repo_mirrors.refresh_all()}


# --- Cognition program (items 1/3/4/7 + Amendment 1) ---
_self_model = None
_skill_store = None
_sandbox = None
_connector_manager = None


def set_self_model(sm) -> None:
    global _self_model
    _self_model = sm


def set_skill_store(store) -> None:
    global _skill_store
    _skill_store = store


def set_sandbox(s) -> None:
    global _sandbox
    _sandbox = s


def set_connector_manager(m) -> None:
    global _connector_manager
    _connector_manager = m


_benchmark = None


def set_benchmark(b) -> None:
    global _benchmark
    _benchmark = b


@router.get("/self/benchmark")
async def get_benchmark(weeks: int = 8) -> dict:
    """Selfhood benchmark: weekly rollups of derived self-improvement
    metrics plus latest-vs-previous trends. Metrics whose sources were
    unavailable in a week are absent for that week, never zero-filled."""
    if _benchmark is None:
        return {"available": False}
    try:
        out = {"available": True}
        out.update(_benchmark.snapshot(weeks=max(1, min(52, weeks))))
        return out
    except Exception as exc:
        return {"available": True, "error": str(exc)}


class BenchmarkSample(BaseModel):
    metric: str
    value: float
    ts: Optional[float] = None
    meta: Optional[Dict[str, Any]] = None
    definition_version: Optional[str] = None
    source_ref: Optional[str] = None
    receipt_ref: Optional[str] = None
    sample_id: Optional[str] = None
    exposure_id: Optional[str] = None
    effect_claim: bool = False


class BenchmarkSamplesRequest(BaseModel):
    samples: List[BenchmarkSample]
    source: str = "host"


@router.post("/self/benchmark/samples")
async def post_benchmark_samples(
    body: BenchmarkSamplesRequest,
    request: Request,
) -> dict:
    """Ingest measured samples from deployment surfaces (e.g. voice TTFB as
    latency.voice_ttfb_ms).

    P4 shadow/live samples are attributed to the authenticated request
    principal and bound to a versioned evidence definition.  The historical
    body ``source`` label survives only in P4-off compatibility mode.
    """
    if _benchmark is None:
        return {"available": False, "accepted": 0}
    from protagine.self_model.benchmark import cognition_p4_mode

    mode = cognition_p4_mode()
    principal = request_authority(request).principal_id
    accepted = 0
    rejection_reasons: Dict[str, int] = {}
    for s in body.samples[:500]:
        try:
            if mode == "off":
                saved = _benchmark.store.add_sample(
                    s.metric, s.value, source=body.source or "host",
                    ts=s.ts, meta=s.meta)
            else:
                if not (s.definition_version or "").strip():
                    raise ValueError("definition_version is required")
                if not (s.source_ref or "").strip():
                    raise ValueError("source_ref is required")
                if (s.effect_claim or s.exposure_id) and not (
                        s.receipt_ref or "").strip():
                    raise ValueError(
                        "receipt_ref is required for an effect claim")
                saved = _benchmark.store.add_evidence_sample(
                    s.metric,
                    s.value,
                    definition_version=s.definition_version or "",
                    sample_principal=principal,
                    source_ref=s.source_ref or "",
                    receipt_ref=s.receipt_ref,
                    sample_id=s.sample_id,
                    exposure_id=s.exposure_id,
                    ts=s.ts,
                    meta=s.meta,
                )
            if saved:
                accepted += 1
            else:
                rejection_reasons["invalid_sample"] = (
                    rejection_reasons.get("invalid_sample", 0) + 1)
        except ValueError as exc:
            reason = str(exc)[:160] or "invalid_sample"
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
    return {"available": True, "accepted": accepted,
            "rejected": len(body.samples[:500]) - accepted,
            "rejection_reasons": rejection_reasons}


class RecallProbeRequest(BaseModel):
    probes: int = 50
    seed: Optional[int] = None


@router.post("/self/benchmark/recall-probe")
async def post_benchmark_recall_probe(body: RecallProbeRequest) -> dict:
    """On-demand, deterministic recall probe (the graduation gate for recall
    changes): re-queries a seeded sample of high-confidence shared facts
    against graph recall and grades token coverage. Read-only against the
    graph; probe samples are recorded as source="manual-probe" and are never
    read back into weekly rollups, so probing cannot distort the scorecard."""
    if _benchmark is None:
        return {"available": False}
    try:
        result = await _benchmark.run_recall_probe(
            probes=body.probes, seed=body.seed)
        if result is None:
            return {"available": True, "ran": False,
                    "reason": "graph/facts unavailable or no "
                              "high-confidence facts to probe"}
        return {"available": True, "ran": True, **result}
    except Exception as exc:
        return {"available": True, "error": str(exc)}


_situation_store = None
_situation_reducer = None


def set_situation_spine(store, reducer) -> None:
    """Publish or clear the complete P6 observer graph atomically."""

    global _situation_store, _situation_reducer
    _situation_store = store
    _situation_reducer = reducer


_expectations = None


def set_expectations(e) -> None:
    global _expectations
    _expectations = e


def _owner_person_id() -> str:
    return (
        os.environ.get("PROTAGINE_OWNER_PERSON_ID", "").strip()
        or os.environ.get("PROTAGINE_OWNER_CONTACT_ID", "").strip()
        or "owner"
    )


def _authority_view(request: Request, *, person_id: str = "") -> tuple:
    """Derive one exact viewer/subject lane from middleware authority."""

    authority = request_authority(request)
    if authority.authenticated and not authority.anonymous:
        # The key acts for the owner; a body person only selects the subject.
        subject = person_id.strip() or _owner_person_id()
        return authority, subject, _owner_person_id(), "owner"
    subject = resolve_request_person(
        request, claimed_person_id=person_id or None,
    )
    if not subject:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "person_binding_required",
                "message": "the credential has no exact viewer binding",
            },
        )
    viewer = authority.viewer_person_id or subject
    viewer_scope = (
        "owner"
        if "owner" in authority.audiences and viewer == _owner_person_id()
        else f"person:{viewer}"
    )
    return authority, subject, viewer, viewer_scope


@router.get("/self/situation")
async def get_situation(request: Request, person_id: str = "") -> dict:
    """Return only the snapshot lane granted by the request credential."""

    if _situation_store is None or _situation_reducer is None:
        return {"available": False}
    _authority, subject, _viewer, viewer_scope = _authority_view(
        request, person_id=person_id,
    )
    try:
        snapshot = _situation_store.snapshot(
            subject_person_id=subject,
            viewer_scope=viewer_scope,
        )
        return {
            "available": True,
            "status": _situation_reducer.status(),
            "snapshot": snapshot.public(),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/self/expectations")
async def get_expectations(limit: int = 50) -> dict:
    """Expectation engine: pending predictions with their horizons and
    per-domain calibration (Mind M3a). A prediction that missed became a
    surprise on her mind."""
    if _expectations is None:
        return {"available": False}
    try:
        out = {"available": True}
        out.update(_expectations.snapshot(limit=max(1, min(200, limit))))
        return out
    except Exception as exc:
        return {"available": True, "error": str(exc)}


@router.post("/self/benchmark/compute")
async def compute_benchmark(week: str = "") -> dict:
    """Compute (or recompute) a week's rollups on demand. Default: the
    previous completed ISO week."""
    if _benchmark is None:
        return {"available": False}
    try:
        return {"available": True,
                **await _benchmark.compute_week(week or None)}
    except Exception as exc:
        return {"available": True, "error": str(exc)}


@router.get("/self")
async def get_self_model(request: Request = None) -> dict:
    """Self-model: per-domain competence, live load, trust stages."""
    if _self_model is None:
        return {"available": False}
    _require_perspective_owner(request)
    try:
        out = {"available": True}
        out.update(_self_model.status())
        out["brief"] = _self_model.brief()
        if getattr(_self_model, 'perspective', None) is not None:
            out['perspective'] = _self_model.perspective.status()
        return out
    except Exception as exc:
        return {"available": True, "error": str(exc)}


@router.get("/autonomy/posture")
async def get_autonomy_posture(request: Request) -> dict:
    """Effective autonomy posture: the resolved value of every mode switch as
    the RUNNING process sees it, so unit-pinned env is never invisible."""
    try:
        from protagine.config import env_bool, env_choice
        posture = {}
        for name, valid, fallback in (
            ("PROTAGINE_INTROSPECT_ENABLED", ("true", "false"), "false"),
            ("PROTAGINE_SKILLS_DISTILL", ("off", "shadow", "live"), "shadow"),
            ("PROTAGINE_ESCALATION_MINING", ("off", "shadow", "live"), "shadow"),
            ("PROTAGINE_CONNECTORS_MODE", ("off", "shadow", "live"), "off"),
            ("PROTAGINE_SANDBOX_MODE", ("off", "dry_run", "live"), "off"),
            ("PROTAGINE_EXPECTATIONS", ("off", "on", "shadow", "live"), "on"),
        ):
            if valid == ("true", "false"):
                posture[name] = str(env_bool(name, fallback == "true")).lower()
            else:
                posture[name] = env_choice(name, valid, fallback)
        mind = _mind()
        posture["mind.autonomy"] = mind.level if mind is not None else "unknown"
        posture["mind.enabled"] = bool(mind is not None and mind.enabled)
        return {"available": True, "posture": posture}
    except Exception as exc:
        return {"available": False, "error": str(exc)}


@router.get("/self/journal")
async def get_action_journal(limit: int = 50, domain: str = "",
                             today: bool = False) -> dict:
    """Unified action journal (what was done, why, with what confidence)."""
    journal = getattr(_self_model, "journal", None) if _self_model else None
    if journal is None:
        return {"available": False, "entries": []}
    try:
        entries = (journal.today(domain=domain or None) if today
                   else journal.recent(limit=limit, domain=domain or None))
        return {"available": True, "count": len(entries), "entries": entries}
    except Exception as exc:
        return {"available": True, "error": str(exc), "entries": []}


@router.get("/skills-memory")
async def get_skills_memory() -> dict:
    """Procedure-memory skills (item 3) observability."""
    if _skill_store is None:
        return {"available": False}
    try:
        return {"available": True, **_skill_store.snapshot()}
    except Exception as exc:
        return {"available": True, "error": str(exc)}


@router.get("/sandbox/status")
async def get_sandbox_status() -> dict:
    """Exploration sandbox (item 6): mode, backend, containment limits."""
    if _sandbox is None:
        return {"available": False}
    try:
        return {"available": True, **_sandbox.status()}
    except Exception as exc:
        return {"available": True, "error": str(exc)}


@router.post("/sandbox/run")
async def run_sandbox(
    request: Request,
    body: dict = Body(default={}),
) -> dict:
    """Owner surface: run a script in the sandbox. Owner-directed runs auto-run
    within default limits; still boundary-checked and journaled. The caller
    cannot widen containment (limits are server-side)."""
    if _sandbox is None:
        return {"ran": False, "reason": "sandbox_not_wired"}
    b = body or {}
    authority = request_authority(request)
    owner_person_id = (
        os.environ.get("PROTAGINE_OWNER_PERSON_ID", "").strip()
        or os.environ.get("PROTAGINE_OWNER_CONTACT_ID", "").strip()
        or "owner"
    )
    # Owner direction is derived from authenticated transport authority;
    # request JSON cannot assert either owner direction or approval.
    owner_directed = bool(
        authority.authenticated
        and not authority.anonymous
        and "owner" in authority.audiences
        and owner_person_id in authority.person_ids
    )
    return _sandbox.run(
        b.get("script", ""),
        lang=b.get("lang", "python"),
        purpose=b.get("purpose", ""),
        owner_directed=owner_directed,
        approved=owner_directed)


@router.get("/connectors/status")
async def get_connectors_status() -> dict:
    """Connector framework (item 2): mode + per-connector cadence/last-poll."""
    if _connector_manager is None:
        return {"available": False}
    try:
        return {"available": True, **_connector_manager.status()}
    except Exception as exc:
        return {"available": True, "error": str(exc)}


@router.post("/connectors/poll")
async def poll_connectors() -> dict:
    """Manually run one connector ingest pass (ops/verification surface; the
    autonomy phase is the normal cadence)."""
    if _connector_manager is None:
        return {"available": False}
    try:
        report = await _connector_manager.poll_due()
        return {"available": True, "report": report}
    except Exception as exc:
        return {"available": True, "error": str(exc)}


@router.get("/proposals")
async def list_proposals(
    request: Request, status: str = "", limit: int = 30,
) -> dict:
    """List proposals Protagine has generated (observability)."""
    if _proposal_store is None:
        return {"available": False, "proposals": []}
    try:
        items = _proposal_store.list(status=status or None, limit=limit)
        authority = request_authority(request)
        viewer = authority.viewer_person_id or ""
        items = [
            item for item in items
            if item.visible_to(
                viewer_person_id=viewer,
                owner_person_id=_owner_person_id(),
                audiences=authority.audiences,
            )
        ]
        return {
            "available": True,
            "count": len(items),
            "proposals": [
                {
                    "id": p.id, "title": p.title, "finding": p.finding,
                    "why_it_helps": p.why_it_helps, "suggested_action": p.suggested_action,
                    "citations": p.citations, "source": p.source,
                    "type": p.initiative_type, "confidence": p.confidence,
                    "status": p.status, "rendered": p.render(),
                    "route_ref": p.route_ref,
                    "result_ref": p.result_ref,
                    "subject_person_id": p.subject_person_id,
                    "viewer_scope": p.viewer_scope,
                    "shareability": p.shareability,
                    "scope_digest": p.scope_digest,
                }
                for p in items
            ],
        }
    except Exception as exc:
        return {"available": True, "error": str(exc), "proposals": []}


@router.get("/preferences")
async def get_owner_preferences(request: Request = None) -> dict:
    """Return the owner's learned communication preferences and rendered brief."""
    if _preference_learner is None:
        return {"available": False, "brief": "", "preferences": []}
    _require_perspective_owner(request)
    prefs = await _preference_learner.get_all_preferences()
    return {
        "available": True,
        "brief": _preference_learner.build_brief(),
        "sourced": _preference_learner.perspective.status() if getattr(_preference_learner, 'perspective', None) is not None else None,
        "preferences": [
            {
                "category": p.category, "key": p.key, "value": p.value,
                "confidence": p.confidence, "learned_from": p.learned_from,
                "last_updated": p.last_updated.isoformat(),
            }
            for p in prefs
        ],
    }


@router.post("/preferences/learn")
async def learn_owner_preference(body: dict, request: Request = None) -> dict:
    """Teach the owner-preference learner from text.

    Body: ``{"text": "be concise", "explicit": true, "force": false}``
      - explicit (default true): parse as a directive ("be concise"); learned at
        high confidence only if it reads like a communication directive.
      - force (with explicit): if the text isn't a recognized directive, still
        store it as explicit feedback.
      - explicit false: record as an observed behavior signal.
    """
    if _preference_learner is None:
        raise HTTPException(status_code=501, detail=_NOT_WIRED)
    _require_perspective_owner(request)
    if getattr(_preference_learner, 'perspective', None) is not None:
        source_id = body.get('source_id')
        if not isinstance(source_id, str) or not source_id:
            raise HTTPException(status_code=422, detail='Use an attributed owner turn and supply its source_id; free text cannot mint a source-backed correction.')
        changes = _preference_learner.learn_source(source_id)
        return {'learned': changes, 'brief': _preference_learner.build_brief()}
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text required")
    learned: Any = None
    if body.get("explicit", True):
        hit = await _preference_learner.learn_directive(text)
        if hit is not None:
            learned = {"category": hit[0], "key": hit[1], "value": hit[2]}
        elif body.get("force"):
            await _preference_learner.learn_from_feedback("communication_style", text)
            learned = {"category": "communication_style", "key": "parsed"}
    else:
        await _preference_learner.learn_from_behavior(text)
        learned = {"category": "behavior"}
    return {"learned": learned, "brief": _preference_learner.build_brief()}


def _require_perspective_owner(request):
    if request is not None:
        from protagine.identity import get_owner_contact_id
        owner = get_owner_contact_id()
        if not owner:
            raise HTTPException(status_code=503, detail='Owner identity is not configured')
        resolve_request_person(request, claimed_person_id=owner)


_pattern_store = None


def set_pattern_store(store):
    global _pattern_store
    _pattern_store = store


# ---------------------------------------------------------------------------
# Insights
# ---------------------------------------------------------------------------

@router.get("/insights", response_model=InsightsListResponse)
async def list_insights(limit: int = 10, dismissed: bool = False) -> InsightsListResponse:
    # Insights come from the synthesis module's connection discoveries
    if _connection_discoverer is None:
        return InsightsListResponse(insights=[])
    try:
        connections = await _connection_discoverer.discover_connections(min_novelty=0.3)
        dismissed_ids = _insight_store.list_dismissed() if _insight_store is not None else set()
        insights = []
        for c in connections:
            cid = getattr(c, "id", None) or str(uuid.uuid4())
            is_dismissed = cid in dismissed_ids
            if not dismissed and is_dismissed:
                continue
            if dismissed and not is_dismissed:
                continue
            insights.append(InsightResponse(
                id=cid,
                title=getattr(c, "connection_type", "Connection"),
                body=getattr(c, "description", "") or f"Connection between {', '.join(getattr(c, 'entities', []))}",
                insight_type=getattr(c, "connection_type", "unknown"),
                novelty=getattr(c, "novelty", 0.0),
                entities=getattr(c, "entities", []),
                dismissed=is_dismissed,
            ))
            if len(insights) >= limit:
                break
        return InsightsListResponse(insights=insights)
    except Exception as exc:
        logger.warning("list_insights failed: %s", exc)
        return InsightsListResponse(insights=[])


@router.post("/insights/{insight_id}/dismiss")
async def dismiss_insight(insight_id: str) -> dict:
    if _insight_store is None:
        raise HTTPException(status_code=503, detail="insight_store_not_initialized")
    _insight_store.dismiss(insight_id)
    return {"ok": True, "insight_id": insight_id}


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

_secrets_manager = None

def set_secrets_manager(manager) -> None:
    global _secrets_manager
    _secrets_manager = manager


def _memory_context_selector():
    """Keep one selector per active reranker, also usable without graph storage."""
    global _context_recall_selector
    if _context_recall_selector is None or _context_recall_selector[0] is not _reranker:
        from protagine.memory.selection import RecallSelector
        from protagine.memory.recall import provider_calibration_metadata
        provider = _reranker
        selector = RecallSelector(
            provider.rerank if provider is not None else None,
            calibration_metadata=(lambda: provider_calibration_metadata(provider))
                if provider is not None else None,
            logger=logger)
        _context_recall_selector = (provider, selector)
    return _context_recall_selector[1]


def set_reranker(reranker) -> None:
    global _reranker
    _reranker = reranker


def set_session_store(store) -> None:
    global _session_store
    _session_store = store


def set_session_report_store(store) -> None:
    global _session_report_store
    _session_report_store = store


def _map_initiative_to_schema(i) -> AgentSnapshotInitiative:
    """Map an Initiative model to the AgentSnapshotInitiative schema."""
    return AgentSnapshotInitiative(
        id=i.id,
        type=i.type,
        description=i.description,
        priority=i.priority,
        status=i.status,
        rationale=i.rationale,
        action_hint=i.action_hint,
        entity_id=i.entity_id,
        dedup_key=i.dedup_key,
        created_at=i.created_at.isoformat() if i.created_at else "",
        expires_at=i.expires_at.isoformat() if i.expires_at else None,
        assigned_agent_id=i.assigned_agent_id,
        acknowledged_at=i.acknowledged_at.isoformat() if i.acknowledged_at else None,
        completed_at=i.completed_at.isoformat() if i.completed_at else None,
        failed_at=i.failed_at.isoformat() if i.failed_at else None,
        failed_reason=i.failed_reason,
    )


@router.post("/secrets/list", response_model=SecretListResponse)
async def secrets_list(body: SecretListRequest) -> SecretListResponse:
    if _secrets_manager is None:
        return SecretListResponse(keys=[])
    try:
        all_keys = _secrets_manager.list()
        if body.prefix:
            keys = [k for k in all_keys if k.startswith(body.prefix)]
        else:
            keys = all_keys
        return SecretListResponse(keys=keys)
    except Exception as exc:
        logger.warning("secrets_list failed: %s", exc)
        return SecretListResponse(keys=[])


@router.post("/secrets/get", response_model=SecretGetResponse)
async def secrets_get(body: SecretGetRequest) -> SecretGetResponse:
    if _secrets_manager is None:
        return SecretGetResponse(key=body.key, exists=False)
    try:
        value = _secrets_manager.get(body.key)
        if value is None:
            return SecretGetResponse(key=body.key, exists=False)
        return SecretGetResponse(key=body.key, value=value, exists=True)
    except Exception as exc:
        logger.warning("secrets_get failed: %s", exc)
        return SecretGetResponse(key=body.key, exists=False)


@router.post("/secrets/set", response_model=SecretSetResponse)
async def secrets_set(body: SecretSetRequest) -> SecretSetResponse:
    if _secrets_manager is None:
        return SecretSetResponse(key=body.key, stored=False)
    try:
        _secrets_manager.set(body.key, body.value, secret_type=body.secret_type)
        return SecretSetResponse(key=body.key, stored=True)
    except Exception as exc:
        logger.warning("secrets_set failed: %s", exc)
        return SecretSetResponse(key=body.key, stored=False)


@router.post("/secrets/delete", response_model=SecretDeleteResponse)
async def secrets_delete(body: SecretDeleteRequest) -> SecretDeleteResponse:
    if _secrets_manager is None:
        return SecretDeleteResponse(key=body.key, deleted=False)
    try:
        _secrets_manager.delete(body.key)
        return SecretDeleteResponse(key=body.key, deleted=True)
    except Exception as exc:
        logger.warning("secrets_delete failed: %s", exc)
        return SecretDeleteResponse(key=body.key, deleted=False)


# ---------------------------------------------------------------------------
# Retrieval, sessions and queue wiring
# ---------------------------------------------------------------------------

_reranker = None
_context_recall_selector = None
_session_store = None
_session_report_store = None
# ---------------------------------------------------------------------------
# Agent Bridge status
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Commitment Tracking
# ---------------------------------------------------------------------------

@router.post("/commitments", status_code=status.HTTP_201_CREATED)
async def create_commitment(body: CommitmentCreateRequest) -> CommitmentResponse:
    """Create a new commitment. With `dedupe` set, an open commitment for the
    same person that already says the same thing is returned instead of
    creating a twin (response carries deduped=true)."""
    if _commitment_store is None:
        raise HTTPException(status_code=501, detail="Commitment tracking not initialized")

    try:
        result = _commitment_store.create(
            person_id=body.person_id,
            description=body.description,
            due_at=body.due_at,
            priority=body.priority,
            source_type=body.source_type,
            source_context=body.source_context,
            metadata=body.metadata,
            dedupe=body.dedupe,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    if result.get("deduped"):
        return CommitmentResponse(**result)

    try:
        from protagine.events.broadcaster import emit as _emit
        _emit("commitment.created", {
            "commitment_id": result["id"],
            "person_id": result["person_id"],
            "description": result["description"],
        })
    except Exception:
        pass
    return CommitmentResponse(**result)


@router.get("/commitments", response_model=CommitmentListResponse)
async def list_commitments(
    person_id: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    overdue_only: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> CommitmentListResponse:
    """List commitments with optional filters."""
    if _commitment_store is None:
        raise HTTPException(status_code=501, detail="Commitment tracking not initialized")

    statuses = [s.strip() for s in status_filter.split(",")] if status_filter else None

    # When "overdue" is requested, get commitments that are actually overdue
    # (past due_date + still pending), not just ones already transitioned
    if statuses and "overdue" in statuses:
        try:
            overdue = _commitment_store.get_overdue()
            other_statuses = [s for s in statuses if s != "overdue"]
            if other_statuses:
                result = _commitment_store.list(
                    person_id=person_id,
                    status=other_statuses,
                    overdue_only=False,
                    limit=limit,
                    offset=offset,
                )
                # Merge
                other_items = result if isinstance(result, list) else result.get("commitments", [])
                all_items = overdue + other_items
            else:
                all_items = overdue
            return CommitmentListResponse(
                commitments=all_items, total=len(all_items),
                limit=limit, offset=offset,
            )
        except Exception as exc:
            logger.warning("get_overdue failed: %s", exc)

    result = _commitment_store.list(
        person_id=person_id,
        status=statuses,
        overdue_only=overdue_only,
        limit=limit,
        offset=offset,
    )
    return CommitmentListResponse(**result)


@router.get("/commitments/{commitment_id}", response_model=CommitmentResponse)
async def get_commitment(commitment_id: str) -> CommitmentResponse:
    """Get a single commitment by ID."""
    if _commitment_store is None:
        raise HTTPException(status_code=501, detail="Commitment tracking not initialized")

    result = _commitment_store.get(commitment_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Commitment not found")
    return CommitmentResponse(**result)


@router.patch("/commitments/{commitment_id}", response_model=CommitmentResponse)
async def update_commitment(commitment_id: str, body: CommitmentUpdateRequest) -> CommitmentResponse:
    """Update a commitment. With `outcome` set this is a resolution: status
    is derived, the reason is recorded in metadata for the learning loop, and
    any workspace concern raised from this commitment is resolved too."""
    if _commitment_store is None:
        raise HTTPException(status_code=501, detail="Commitment tracking not initialized")

    if body.outcome is not None:
        # Resolution path: store.resolve() maps outcome -> status, records
        # {outcome, note, by, at} in metadata and emits the event itself.
        try:
            result = _commitment_store.resolve(
                commitment_id,
                outcome=body.outcome,
                note=body.reason,
                resolved_by=body.resolved_by or "owner",
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if result is None:
            raise HTTPException(status_code=404, detail="Commitment not found")
        return CommitmentResponse(**result)

    try:
        result = _commitment_store.update(
            commitment_id=commitment_id,
            status=body.status,
            fulfilled_at=body.fulfilled_at,
            description=body.description,
            due_at=body.due_at,
            priority=body.priority,
            metadata=body.metadata,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    if result is None:
        raise HTTPException(status_code=404, detail="Commitment not found")

    # Emit events for status changes
    if body.status == "fulfilled":
        try:
            from protagine.events.broadcaster import emit as _emit
            _emit("commitment.fulfilled", {
                "commitment_id": result["id"],
                "person_id": result["person_id"],
            })
        except Exception:
            pass
    elif body.status == "cancelled":
        try:
            from protagine.events.broadcaster import emit as _emit
            _emit("commitment.cancelled", {
                "commitment_id": result["id"],
                "person_id": result["person_id"],
            })
        except Exception:
            pass

    return CommitmentResponse(**result)


@router.get("/commitments/stats/resolution")
async def commitment_resolution_stats(days: int = Query(30, ge=1, le=365)) -> dict:
    """How commitments created in the window got resolved, per source_type —
    the calibration signal for whatever generates items (introspection,
    cognition, agents): a source whose items keep getting cancelled as
    invalid should get more conservative."""
    if _commitment_store is None:
        raise HTTPException(status_code=501, detail="Commitment tracking not initialized")
    try:
        stats = _commitment_store.resolution_stats(days=days)
        stats["recent_rejections"] = _commitment_store.recent_rejections(limit=10)
        return stats
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.delete("/commitments/{commitment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_commitment(commitment_id: str):
    """Delete a commitment. Only allowed for terminal states (fulfilled/cancelled)."""
    if _commitment_store is None:
        raise HTTPException(status_code=501, detail="Commitment tracking not initialized")

    deleted = _commitment_store.delete(commitment_id)
    if not deleted:
        existing = _commitment_store.get(commitment_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Commitment not found")
        else:
            raise HTTPException(
                status_code=409,
                detail=f"Cannot delete commitment in '{existing['status']}' state. Cancel it first.",
            )


# ---------------------------------------------------------------------------
# Cognition Substrate
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Theory of Mind — Affect
# ---------------------------------------------------------------------------

async def _require_person_contact(contact_id: str) -> None:
    """ToM stores accept only REAL contacts (docs/RELATIONSHIPS.md #5).

    Free-text names, test strings, and the machine sentinel are refused so
    psyche/affect/fact state can never be minted for a non-person. When the
    contact store is unavailable the check degrades open (single-store
    test deployments keep working)."""
    cid = (contact_id or "").strip()
    if not cid or cid in ("system", "default"):
        raise HTTPException(
            status_code=422,
            detail=f"contact_id {cid!r} is not a person contact")
    if _contacts_store is None:
        return
    try:
        exists = await _contacts_store.get(cid) is not None
    except Exception:
        return
    if not exists:
        raise HTTPException(
            status_code=422,
            detail=f"unknown contact_id {cid!r} — create the contact first "
                   "(POST /v1/host/contacts) or resolve the sender handle")


@router.post("/affect/events", response_model=AffectEventResponse, status_code=status.HTTP_201_CREATED)
async def create_affect_event(body: AffectEventCreateRequest) -> AffectEventResponse:
    """Record an affect event for a contact."""
    if _affect_store is None:
        raise HTTPException(status_code=501, detail="Affect tracking not initialized")
    await _require_person_contact(body.contact_id)
    try:
        result = _affect_store.create_event(
            contact_id=body.contact_id,
            valence=body.valence,
            arousal=body.arousal,
            source=body.source,
            trigger=body.trigger,
            session_id=body.session_id,
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))

    try:
        from protagine.events.broadcaster import emit as _emit
        _emit("affect.event_created", {
            "event_id": result["id"],
            "contact_id": result["contact_id"],
            "valence": result["valence"],
        })
    except Exception:
        pass

    # Check for negative spike
    if _affect_store.detect_negative_spike(body.contact_id):
        try:
            from protagine.events.broadcaster import emit as _emit
            _emit("affect.negative_spike", {
                "contact_id": body.contact_id,
                "valence": result["valence"],
            })
        except Exception:
            pass

    return AffectEventResponse(**result)


@router.get("/affect/state/{contact_id}", response_model=AffectStateResponse)
async def get_affect_state(contact_id: str) -> AffectStateResponse:
    """Get the current affect state for a contact."""
    if _affect_store is None:
        raise HTTPException(status_code=501, detail="Affect tracking not initialized")
    state = _affect_store.get_state(contact_id)
    return AffectStateResponse(**state)


@router.get("/affect/history/{contact_id}", response_model=AffectEventListResponse)
async def list_affect_history(
    contact_id: str,
    source: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> AffectEventListResponse:
    """Get affect event history for a contact."""
    if _affect_store is None:
        raise HTTPException(status_code=501, detail="Affect tracking not initialized")
    events = _affect_store.list_events(contact_id=contact_id, source=source, limit=limit, offset=offset)
    try:
        total = _affect_store.count_events(contact_id=contact_id, source=source)
    except Exception:
        total = offset + len(events)
    return AffectEventListResponse(events=[AffectEventResponse(**e) for e in events], total=total, limit=limit, offset=offset)


@router.delete("/affect/events/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_affect_event(event_id: str):
    """Delete an affect event."""
    if _affect_store is None:
        raise HTTPException(status_code=501, detail="Affect tracking not initialized")
    if not _affect_store.delete_event(event_id):
        raise HTTPException(status_code=404, detail="Affect event not found")


# ---------------------------------------------------------------------------
# Theory of Mind — Shared Facts
# ---------------------------------------------------------------------------

@router.post("/mind/facts", response_model=SharedFactResponse, status_code=status.HTTP_201_CREATED)
async def create_shared_fact(
    body: SharedFactCreateRequest,
    request: Request = None,
) -> SharedFactResponse:
    """Add a shared fact about what a contact knows."""
    if _facts_store is None:
        raise HTTPException(status_code=501, detail="Shared facts not initialized")
    body.contact_id = resolve_request_person(
        request, claimed_person_id=body.contact_id) or body.contact_id
    await _require_person_contact(body.contact_id)
    try:
        result = _facts_store.create_fact(
            contact_id=body.contact_id,
            fact=body.fact,
            source=body.source,
            confidence=body.confidence,
            expires_at=body.expires_at,
            metadata=body.metadata,
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))

    try:
        from protagine.events.broadcaster import emit as _emit
        _emit("mind.fact_created", {
            "fact_id": result["id"],
            "contact_id": result["contact_id"],
            "source": result["source"],
        })
    except Exception:
        pass

    return SharedFactResponse(**result)


@router.get("/mind/facts", response_model=SharedFactListResponse)
async def list_shared_facts(
    contact_id: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    min_confidence: float = Query(0.0, ge=0.0, le=1.0),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> SharedFactListResponse:
    """List canonical shared facts with source visibility and optional filters."""
    if _facts_store is None:
        raise HTTPException(status_code=501, detail="Shared facts not initialized")
    result = _facts_store.list_facts(
        contact_id=contact_id,
        source=source,
        min_confidence=min_confidence,
        limit=limit,
        offset=offset,
    )
    return SharedFactListResponse(
        facts=[SharedFactResponse(**f) for f in result["facts"]],
        total=result["total"],
        limit=result["limit"],
        offset=result["offset"],
    )


@router.get("/mind/facts/{fact_id}", response_model=SharedFactResponse)
async def get_shared_fact(fact_id: str) -> SharedFactResponse:
    """Get a specific shared fact."""
    if _facts_store is None:
        raise HTTPException(status_code=501, detail="Shared facts not initialized")
    result = _facts_store.get_fact(fact_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Shared fact not found")
    return SharedFactResponse(**result)


@router.patch("/mind/facts/{fact_id}", response_model=SharedFactResponse)
async def update_shared_fact(
    fact_id: str,
    body: SharedFactUpdateRequest,
    request: Request = None,
) -> SharedFactResponse:
    """Update a shared fact."""
    if _facts_store is None:
        raise HTTPException(status_code=501, detail="Shared facts not initialized")
    existing = _facts_store.get_fact(fact_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Shared fact not found")
    resolve_request_person(request, claimed_person_id=existing.get("contact_id"))
    result = _facts_store.update_fact(
        fact_id,
        confidence=body.confidence,
        expires_at=body.expires_at,
        fact=body.fact,
        metadata=body.metadata,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Shared fact not found")
    return SharedFactResponse(**result)


@router.delete("/mind/facts/{fact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_shared_fact(fact_id: str):
    """Delete a shared fact."""
    if _facts_store is None:
        raise HTTPException(status_code=501, detail="Shared facts not initialized")
    if not _facts_store.delete_fact(fact_id):
        raise HTTPException(status_code=404, detail="Shared fact not found")


# ---------------------------------------------------------------------------
# Pattern Extraction
# ---------------------------------------------------------------------------

@router.post("/patterns", response_model=PatternResponse, status_code=status.HTTP_201_CREATED)
async def create_pattern(body: PatternCreateRequest) -> PatternResponse:
    """Register a pattern (manual or extraction)."""
    if _pattern_store is None:
        raise HTTPException(status_code=501, detail="Pattern extraction not initialized")
    try:
        result = _pattern_store.create_pattern(
            pattern_type=body.pattern_type,
            description=body.description,
            pattern_key=body.pattern_key,
            frequency=body.frequency,
            confidence=body.confidence,
            metadata=body.metadata,
            source=body.source,
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))
    try:
        from protagine.events.broadcaster import emit as _emit
        _emit("pattern.created", {"pattern_id": result["id"], "pattern_type": result["pattern_type"]})
    except Exception:
        pass
    return PatternResponse(**result)


@router.get("/patterns", response_model=PatternListResponse)
async def list_patterns(
    pattern_type: Optional[str] = Query(None),
    min_frequency: int = Query(1, ge=1),
    source: Optional[str] = Query(None),
    active_only: bool = Query(True),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> PatternListResponse:
    """List patterns with optional filters."""
    if _pattern_store is None:
        raise HTTPException(status_code=501, detail="Pattern extraction not initialized")
    result = _pattern_store.list_patterns(
        pattern_type=pattern_type,
        min_frequency=min_frequency,
        source=source,
        active_only=active_only,
        limit=limit,
        offset=offset,
    )
    return PatternListResponse(
        patterns=[PatternResponse(**p) for p in result["patterns"]],
        total=result["total"],
        limit=result["limit"],
        offset=result["offset"],
    )


@router.get("/patterns/{pattern_id}", response_model=PatternResponse)
async def get_pattern(pattern_id: str) -> PatternResponse:
    """Get a specific pattern."""
    if _pattern_store is None:
        raise HTTPException(status_code=501, detail="Pattern extraction not initialized")
    result = _pattern_store.get_pattern(pattern_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Pattern not found")
    return PatternResponse(**result)


@router.patch("/patterns/{pattern_id}", response_model=PatternResponse)
async def update_pattern(pattern_id: str, body: PatternUpdateRequest) -> PatternResponse:
    """Update a pattern."""
    if _pattern_store is None:
        raise HTTPException(status_code=501, detail="Pattern extraction not initialized")
    result = _pattern_store.update_pattern(
        pattern_id,
        description=body.description,
        confidence=body.confidence,
        metadata=body.metadata,
        active=body.active,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Pattern not found")
    return PatternResponse(**result)


@router.delete("/patterns/{pattern_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pattern(pattern_id: str):
    """Delete a pattern."""
    if _pattern_store is None:
        raise HTTPException(status_code=501, detail="Pattern extraction not initialized")
    if not _pattern_store.delete_pattern(pattern_id):
        raise HTTPException(status_code=404, detail="Pattern not found")


@router.post("/patterns/extract", response_model=PatternExtractResponse)
async def extract_patterns_endpoint() -> PatternExtractResponse:
    """Trigger a pattern extraction run (no entity source is wired since M8)."""
    if _pattern_store is None:
        raise HTTPException(status_code=501, detail="Pattern extraction not initialized")
    from protagine.patterns.extract import extract_patterns
    result = extract_patterns(world_store=None, pattern_store=_pattern_store)
    try:
        from protagine.events.broadcaster import emit as _emit
        _emit("pattern.extracted", {"new": result["new"], "updated": result["updated"], "total": result["total"]})
    except Exception:
        pass
    return PatternExtractResponse(**result)


# ============================================================================
# Multi-Agent — Agent Management (v0.7.0)
# ============================================================================

_agent_store = None
_invite_store = None
_initiative_store = None
_assignment_engine = None
_websocket_manager = None


def set_agent_store(store) -> None:
    global _agent_store
    _agent_store = store


def set_invite_store(store) -> None:
    global _invite_store
    _invite_store = store


def set_initiative_store(store) -> None:
    global _initiative_store
    _initiative_store = store


def set_assignment_engine(engine) -> None:
    global _assignment_engine
    _assignment_engine = engine


def set_websocket_manager(manager) -> None:
    global _websocket_manager
    _websocket_manager = manager


# --- Agent Onboarding ---

@router.post("/agents/invite", response_model=AgentInviteResponse)
async def create_agent_invite(body: AgentInviteRequest) -> AgentInviteResponse:
    """Generate a setup code for remote agent onboarding."""
    if _invite_store is None:
        raise HTTPException(status_code=501, detail="Invite store not initialized")

    protagine_id = instance_id(get_state_dir())

    invite = _invite_store.create(
        protagine_id=protagine_id,
        capabilities=body.granted_capabilities,
        is_primary=body.granted_is_primary,
        max_concurrent=body.granted_max_concurrent,
        expires_seconds=body.expires_in_seconds,
        label=body.label,
    )

    # Build setup command
    protagine_url = os.environ.get("PROTAGINE_URL", "http://localhost:7777")
    setup_command = f"protagine agent connect --setup-code {invite['setup_code']} --protagine-url {protagine_url}"

    return AgentInviteResponse(
        code=invite["setup_code"],
        expires_at=invite["expires_at"],
        max_uses=1,  # Single use by default
        setup_command=setup_command,
    )


@router.post("/agents/connect", response_model=AgentConnectResponse)
async def connect_remote_agent(body: AgentConnectRequest) -> AgentConnectResponse:
    """Connect a remote agent using setup code."""
    if _invite_store is None or _agent_store is None:
        raise HTTPException(status_code=501, detail="Agent system not initialized")

    # Generate agent ID and node ID
    agent_id = str(uuid.uuid4())
    node_id = body.node_id or str(uuid.uuid4())
    protagine_id = instance_id(get_state_dir())

    # Validate and use setup code
    try:
        invite = _invite_store.use(body.setup_code, node_id, agent_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # The node certificate is UNSIGNED: this instance holds no signing key (the
    # chain went with M8), so the remote-agent handshake cannot verify it. The
    # remote multi-agent surface stays experimental; see docs/MULTI_AGENT.md.
    issued_at = datetime.now(timezone.utc)
    node_cert = AgentNodeCert(
        protagine_id=protagine_id,
        node_id=node_id,
        public_key=body.node_public_key,
        signature="",
        issued_at=issued_at.isoformat(),
    )

    # Register agent
    agent = _agent_store.create({
        "agent_id": agent_id,
        "node_id": node_id,
        "protagine_id": protagine_id,
        "name": body.name,
        "connection_mode": "remote",
        "capabilities": invite.get("capabilities", []),
        "is_primary": invite.get("is_primary", False),
        "max_concurrent": invite.get("max_concurrent", 5),
        "metadata": body.metadata,
    })

    # Build websocket URL
    protagine_url = os.environ.get("PROTAGINE_URL", "ws://localhost:7777")
    ws_url = f"{protagine_url.replace('http', 'ws')}/v1/host/agents/{agent_id}/stream"

    return AgentConnectResponse(
        agent_id=agent_id,
        node_id=node_id,
        protagine_id=protagine_id,
        node_cert=node_cert,
        websocket_url=ws_url,
        capabilities=agent.capabilities,
        is_primary=agent.is_primary,
        max_concurrent=agent.max_concurrent,
    )


@router.post("/agents/register", response_model=AgentRegisterResponse)
async def register_local_agent(body: AgentRegisterRequest) -> AgentRegisterResponse:
    """Register a local agent (same network, no setup code)."""
    if _agent_store is None:
        raise HTTPException(status_code=501, detail="Agent store not initialized")

    agent_id = body.agent_id or str(uuid.uuid4())
    node_id = body.node_id or str(uuid.uuid4())
    protagine_id = instance_id(get_state_dir())

    _agent_store.create({
        "agent_id": agent_id,
        "node_id": node_id,
        "protagine_id": protagine_id,
        "name": body.name,
        "connection_mode": body.connection_mode,
        "gateway_url": body.gateway_url,
        "capabilities": body.capabilities,
        "is_primary": body.is_primary,
        "priority": body.priority,
        "max_concurrent": body.max_concurrent,
        "excluded_types": body.excluded_types,
        "metadata": body.metadata,
    })

    ws_url = None
    if body.connection_mode == "remote":
        protagine_url = os.environ.get("PROTAGINE_URL", "ws://localhost:7777")
        ws_url = f"{protagine_url.replace('http', 'ws')}/v1/host/agents/{agent_id}/stream"

    return AgentRegisterResponse(
        agent_id=agent_id,
        node_id=node_id,
        protagine_id=protagine_id,
        websocket_url=ws_url,
    )


# --- Agent Management ---

@router.post("/agents/{agent_id}/heartbeat")
async def agent_heartbeat(agent_id: str, body: AgentHeartbeatRequest) -> Dict[str, Any]:
    """Update agent status with heartbeat."""
    if _agent_store is None:
        raise HTTPException(status_code=501, detail="Agent store not initialized")

    agent = _agent_store.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Update status and metadata
    updates = {
        "status": body.status,
        "current_assignments": body.current_assignments,
        "last_seen_at": datetime.now(timezone.utc),
    }
    if body.metadata:
        updates["metadata"] = body.metadata

    _agent_store.update(agent_id, **updates)

    return {"status": "ok", "agent_id": agent_id}


@router.get("/agents", response_model=AgentListResponse)
async def list_agents(
    status: Optional[str] = Query(None),
    capability: Optional[str] = Query(None),
) -> AgentListResponse:
    """List all registered agents."""
    if _agent_store is None:
        raise HTTPException(status_code=501, detail="Agent store not initialized")

    agents = _agent_store.list(status=status, capability=capability)

    return AgentListResponse(
        agents=[
            AgentResponse(
                agent_id=a.agent_id,
                node_id=a.node_id,
                name=a.name,
                protagine_id=a.protagine_id,
                connection_mode=a.connection_mode,
                gateway_url=a.gateway_url,
                capabilities=a.capabilities,
                is_primary=a.is_primary,
                priority=a.priority,
                max_concurrent=a.max_concurrent,
                excluded_types=a.excluded_types,
                status=a.status,
                current_assignments=a.current_assignments,
                metadata=AgentMetadataSchema(**a.metadata.to_dict()) if hasattr(a.metadata, 'to_dict') else AgentMetadataSchema(),
                registered_at=a.registered_at.isoformat() if a.registered_at else "",
                last_seen_at=a.last_seen_at.isoformat() if a.last_seen_at else None,
            )
            for a in agents
        ],
        total=len(agents),
    )


@router.get("/agents/{agent_id}", response_model=AgentResponse)
async def get_agent(agent_id: str) -> AgentResponse:
    """Get agent details."""
    if _agent_store is None:
        raise HTTPException(status_code=501, detail="Agent store not initialized")

    agent = _agent_store.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    return AgentResponse(
        agent_id=agent.agent_id,
        node_id=agent.node_id,
        name=agent.name,
        protagine_id=agent.protagine_id,
        connection_mode=agent.connection_mode,
        gateway_url=agent.gateway_url,
        capabilities=agent.capabilities,
        is_primary=agent.is_primary,
        priority=agent.priority,
        max_concurrent=agent.max_concurrent,
        excluded_types=agent.excluded_types,
        status=agent.status,
        current_assignments=agent.current_assignments,
        metadata=AgentMetadataSchema(**agent.metadata.to_dict()) if hasattr(agent.metadata, 'to_dict') else AgentMetadataSchema(),
        registered_at=agent.registered_at.isoformat() if agent.registered_at else "",
        last_seen_at=agent.last_seen_at.isoformat() if agent.last_seen_at else None,
    )


@router.delete("/agents/{agent_id}")
async def revoke_agent(agent_id: str) -> Dict[str, Any]:
    """Revoke an agent's access."""
    if _agent_store is None:
        raise HTTPException(status_code=501, detail="Agent store not initialized")

    agent = _agent_store.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    _agent_store.revoke(agent_id)

    return {"status": "revoked", "agent_id": agent_id}


@router.patch("/agents/{agent_id}", response_model=AgentResponse)
async def update_agent(agent_id: str, body: AgentUpdateRequest) -> AgentResponse:
    """Update agent configuration."""
    if _agent_store is None:
        raise HTTPException(status_code=501, detail="Agent store not initialized")

    agent = _agent_store.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    updates = body.dict(exclude_unset=True)
    if updates:
        agent = _agent_store.update(agent_id, **updates)

    return AgentResponse(
        agent_id=agent.agent_id,
        node_id=agent.node_id,
        name=agent.name,
        protagine_id=agent.protagine_id,
        connection_mode=agent.connection_mode,
        gateway_url=agent.gateway_url,
        capabilities=agent.capabilities,
        is_primary=agent.is_primary,
        priority=agent.priority,
        max_concurrent=agent.max_concurrent,
        excluded_types=agent.excluded_types,
        status=agent.status,
        current_assignments=agent.current_assignments,
        metadata=AgentMetadataSchema(**agent.metadata.to_dict()) if hasattr(agent.metadata, 'to_dict') else AgentMetadataSchema(),
        registered_at=agent.registered_at.isoformat() if agent.registered_at else "",
        last_seen_at=agent.last_seen_at.isoformat() if agent.last_seen_at else None,
    )


@router.get("/agents/health", response_model=AgentHealthResponse)
async def get_agents_health() -> AgentHealthResponse:
    """Get health status of all agents."""
    if _agent_store is None:
        raise HTTPException(status_code=501, detail="Agent store not initialized")

    agents = _agent_store.list()

    return AgentHealthResponse(
        agents=[
            {
                "agent_id": a.agent_id,
                "name": a.name,
                "status": a.status,
                "last_seen_at": a.last_seen_at.isoformat() if a.last_seen_at else None,
                "current_initiatives": a.current_assignments,
            }
            for a in agents
        ],
        websocket_endpoint="/v1/host/agents/{agent_id}/stream",
    )


# --- Initiative Management ---

@router.post("/initiatives", response_model=InitiativeResponse)
async def create_initiative(body: InitiativeCreateRequest) -> InitiativeResponse:
    """Create a new initiative."""
    if _initiative_store is None:
        raise HTTPException(status_code=501, detail="Initiative store not initialized")

    initiative = _initiative_store.create(
        type=body.initiative_type,
        description=body.description,
        # Request priority is 0-100; the store holds 0.0-1.0.
        priority=body.priority / 100.0,
        timeout_seconds=body.timeout_seconds,
        dedup_key=body.dedup_key,
        entity_id=body.entity_id,
        preferred_agent_id=body.target_agent_id,
        context=body.context or None,
    )

    try:  # timeline (v0.21.0)
        from protagine.events.journal import append_event
        append_event("initiative.generated", {
            "initiative_id": getattr(initiative, "id", None),
            "contact_id": body.entity_id,
            "summary": body.description,
            "initiative_type": body.initiative_type,
        })
    except Exception:
        logger.debug("journal initiative.generated failed", exc_info=True)

    return _initiative_to_response(initiative)


@router.get("/initiatives", response_model=InitiativeListResponse)
async def list_initiatives(
    status: Optional[str] = Query(None),
    agent_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=1000),
) -> InitiativeListResponse:
    """List initiatives with one optional status; omitted or empty is unfiltered."""
    if _initiative_store is None:
        raise HTTPException(status_code=501, detail="Initiative store not initialized")

    initiatives = _initiative_store.list(
        status=[status] if status else None,
        assigned_agent_id=agent_id,
        limit=limit,
    )

    return InitiativeListResponse(
        initiatives=[_initiative_to_response(i) for i in initiatives],
        total=len(initiatives),
    )


@router.get("/initiatives/{initiative_id}", response_model=InitiativeResponse)
async def get_initiative(initiative_id: str) -> InitiativeResponse:
    """Get initiative details."""
    if _initiative_store is None:
        raise HTTPException(status_code=501, detail="Initiative store not initialized")

    initiative = _initiative_store.get(initiative_id)
    if initiative is None:
        raise HTTPException(status_code=404, detail="Initiative not found")

    return _initiative_to_response(initiative)


@router.post("/initiatives/{initiative_id}/claim")
async def claim_initiative(
    initiative_id: str,
    body: InitiativeClaimRequest,
) -> Dict[str, Any]:
    """Claim an initiative for an agent."""
    if _initiative_store is None or _agent_store is None:
        raise HTTPException(status_code=501, detail="System not initialized")

    initiative = _initiative_store.get(initiative_id)
    if initiative is None:
        raise HTTPException(status_code=404, detail="Initiative not found")

    if initiative.status != "pending":
        raise HTTPException(status_code=400, detail=f"Initiative already {initiative.status}")

    agent = _agent_store.get(body.agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    _initiative_store.assign(initiative_id, body.agent_id)

    return {"status": "claimed", "initiative_id": initiative_id, "agent_id": body.agent_id}

    return {"status": "claimed", "initiative_id": initiative_id, "agent_id": body.agent_id}


@router.post("/initiatives/{initiative_id}/complete")
async def complete_initiative(
    initiative_id: str,
    body: InitiativeCompleteRequest,
) -> Dict[str, Any]:
    """Mark initiative as completed."""
    if _initiative_store is None:
        raise HTTPException(status_code=501, detail="Initiative store not initialized")

    initiative = _initiative_store.get(initiative_id)
    if initiative is None:
        raise HTTPException(status_code=404, detail="Initiative not found")

    if initiative.assigned_agent_id != body.agent_id:
        raise HTTPException(status_code=403, detail="Not assigned to this agent")

    _initiative_store.complete(initiative_id, body.agent_id, body.result.get("result"), body.result)

    return {"status": "completed", "initiative_id": initiative_id}


@router.post("/initiatives/{initiative_id}/fail")
async def fail_initiative(
    initiative_id: str,
    body: InitiativeFailRequest,
) -> Dict[str, Any]:
    """Mark initiative as failed."""
    if _initiative_store is None:
        raise HTTPException(status_code=501, detail="Initiative store not initialized")

    initiative = _initiative_store.get(initiative_id)
    if initiative is None:
        raise HTTPException(status_code=404, detail="Initiative not found")

    if initiative.assigned_agent_id != body.agent_id:
        raise HTTPException(status_code=403, detail="Not assigned to this agent")

    _initiative_store.fail(initiative_id, body.agent_id, body.error_message)

    return {"status": "failed", "initiative_id": initiative_id}


@router.post("/initiatives/{initiative_id}/delegate")
async def delegate_initiative(
    initiative_id: str,
    body: InitiativeDelegateRequest,
) -> Dict[str, Any]:
    """Delegate initiative to another agent."""
    if _initiative_store is None or _agent_store is None:
        raise HTTPException(status_code=501, detail="System not initialized")

    initiative = _initiative_store.get(initiative_id)
    if initiative is None:
        raise HTTPException(status_code=404, detail="Initiative not found")

    agent = _agent_store.get(body.target_agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Target agent not found")

    _initiative_store.update(
        initiative_id,
        assigned_agent_id=body.target_agent_id,
    )
    _initiative_store.log_history(
        initiative_id,
        action="delegated",
        agent_id=initiative.assigned_agent_id,
        details={"target_agent_id": body.target_agent_id, "reason": body.reason},
    )

    return {"status": "delegated", "initiative_id": initiative_id, "target_agent_id": body.target_agent_id}


@router.patch("/initiatives/{initiative_id}/priority")
async def update_initiative_priority(
    initiative_id: str,
    body: InitiativePriorityRequest,
) -> Dict[str, Any]:
    """Update initiative priority."""
    if _initiative_store is None:
        raise HTTPException(status_code=501, detail="Initiative store not initialized")

    initiative = _initiative_store.get(initiative_id)
    if initiative is None:
        raise HTTPException(status_code=404, detail="Initiative not found")

    _initiative_store.update(initiative_id, priority=body.priority)

    return {"status": "updated", "initiative_id": initiative_id, "priority": body.priority}


@router.post("/initiatives/{initiative_id}/retry")
async def retry_initiative(initiative_id: str, request: Request = None) -> Dict[str, Any]:
    """Retry a failed initiative."""
    if _initiative_store is None:
        raise HTTPException(status_code=501, detail="Initiative store not initialized")

    initiative = _initiative_store.get(initiative_id)
    if initiative is None:
        raise HTTPException(status_code=404, detail="Initiative not found")

    if initiative.status != "failed":
        raise HTTPException(status_code=400, detail="Can only retry failed initiatives")

    retried = _initiative_store.retry(initiative_id, request_authority(request).principal_id)
    if retried is None:
        raise HTTPException(status_code=400, detail="Can only retry failed initiatives")

    return {"status": "pending", "initiative_id": initiative_id}


@router.post("/initiatives/{initiative_id}/context/refresh", response_model=InitiativeResponse)
async def refresh_initiative_context(initiative_id: str) -> InitiativeResponse:
    """Rebuild the context snapshot for one initiative's subject (v0.16.0).

    Volatile initiative types (calendar, coding, system, agent_action)
    carry context that can go stale while the initiative sits in the
    queue. The agent calls this before acting when the snapshot's
    ``context_captured_at`` is older than the type's freshness TTL.
    Durable types return their stored snapshot unchanged.
    """
    if _initiative_store is None:
        raise HTTPException(status_code=501, detail="Initiative store not initialized")

    initiative = _initiative_store.get(initiative_id)
    if initiative is None:
        raise HTTPException(status_code=404, detail="Initiative not found")

    from protagine.initiatives.context_freshness import DURABLE, durability_for

    # No per-entity context loader remains: the mind's concerns carry their own evidence.
    fresh = None

    if fresh is None:
        if durability_for(initiative.type) == DURABLE:
            # Durable context: the creation-time snapshot is still valid.
            return _initiative_to_response(initiative)
        raise HTTPException(
            status_code=501,
            detail=(
                f"No per-entity context loader registered for volatile "
                f"type '{initiative.type}' — context cannot be refreshed"
            ),
        )

    # Volatile auto-close: if the refreshed snapshot shows the condition
    # has cleared (CI green again, service recovered, meeting over), the
    # initiative retires itself instead of surfacing stale work.
    condition_cleared = bool(fresh.pop("condition_cleared", False))
    if condition_cleared and initiative.is_active:
        _initiative_store.update(
            initiative_id,
            context=fresh,
            status="cancelled",
            cancelled_at=datetime.now(timezone.utc).isoformat(),
            cancelled_by="context_refresh",
            cancelled_reason="condition_cleared",
            stale_reason="condition_cleared",
        )
        updated = _initiative_store.get(initiative_id)
        return _initiative_to_response(updated or initiative)

    updated = _initiative_store.update(initiative_id, context=fresh)
    return _initiative_to_response(updated or initiative)


@router.delete("/initiatives/{initiative_id}")
async def cancel_initiative(initiative_id: str) -> Dict[str, Any]:
    """Cancel an initiative."""
    if _initiative_store is None:
        raise HTTPException(status_code=501, detail="Initiative store not initialized")

    initiative = _initiative_store.get(initiative_id)
    if initiative is None:
        raise HTTPException(status_code=404, detail="Initiative not found")

    _initiative_store.cancel(initiative_id, cancelled_by="api")

    return {"status": "cancelled", "initiative_id": initiative_id}


# --- Initiative Helpers ---

def _initiative_to_response(initiative) -> InitiativeResponse:
    """Convert StoredInitiative to InitiativeResponse."""
    # Handle result as dict if it's a string or None
    result_dict = None
    if initiative.result:
        if isinstance(initiative.result, dict):
            result_dict = initiative.result
        elif isinstance(initiative.result, str):
            result_dict = {"result": initiative.result}

    from protagine.initiatives.context_freshness import durability_for

    return InitiativeResponse(
        id=initiative.id,
        initiative_type=initiative.type,
        # Title is the ACTION ("Check in with Jordan"), not the reason.
        # The rationale lives in the context dict.
        title=initiative.description[:100],
        description=initiative.description,
        priority=int(initiative.priority * 100) if initiative.priority else 0,
        status=initiative.status,
        timeout_seconds=initiative.timeout_seconds,
        # NULL for rows created before the v0.16.0 context migration.
        context=initiative.context or {},
        context_durability=durability_for(initiative.type),
        entity_id=initiative.entity_id,
        target_agent_id=initiative.assigned_agent_id or initiative.preferred_agent_id,
        assigned_agent_id=initiative.assigned_agent_id,
        dedup_key=initiative.dedup_key,
        result=result_dict,
        error_message=initiative.failed_reason,
        created_at=initiative.created_at.isoformat() if initiative.created_at else "",
        acknowledged_at=initiative.acknowledged_at.isoformat() if initiative.acknowledged_at else None,
        completed_at=initiative.completed_at.isoformat() if initiative.completed_at else None,
        failed_at=initiative.failed_at.isoformat() if initiative.failed_at else None,
        expires_at=initiative.expires_at.isoformat() if initiative.expires_at else None,
    )


# --- Task Management Endpoints (v0.7.10) ---

@router.post("/tasks/{task_id}/complete")
async def complete_task(task_id: str) -> Dict[str, Any]:
    """Mark a task/goal as completed."""
    if _goals_store is None:
        raise HTTPException(status_code=501, detail="Goals store not initialized")
    success = _goals_store.complete_task(task_id)
    if not success:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"success": True, "task_id": task_id}


@router.post("/tasks/{task_id}/snooze")
async def snooze_task(
    task_id: str,
    hours: int = Body(24, ge=1, le=168),
    reason: str = Body(""),
) -> Dict[str, Any]:
    """Snooze a task for N hours (1-168)."""
    if _goals_store is None:
        raise HTTPException(status_code=501, detail="Goals store not initialized")
    success = _goals_store.snooze_task(task_id, hours, reason)
    if not success:
        raise HTTPException(status_code=404, detail="Task not found")
    snoozed_until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    return {"success": True, "task_id": task_id, "snoozed_until": snoozed_until}


@router.post("/tasks/{task_id}/dismiss")
async def dismiss_task(
    task_id: str,
    reason: str = Body("stale"),
) -> Dict[str, Any]:
    """Dismiss a task as no longer relevant."""
    if _goals_store is None:
        raise HTTPException(status_code=501, detail="Goals store not initialized")
    success = _goals_store.dismiss_task(task_id, reason)
    if not success:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"success": True, "task_id": task_id, "reason": reason}


@router.post("/initiatives/{initiative_id}/respond")
async def respond_to_initiative(
    initiative_id: str,
    action: str = Body(...),
    details: Optional[dict] = Body(None),
    request: Request = None,
) -> Dict[str, Any]:
    """Record the owner's response to an initiative as feedback."""
    if _initiative_store is None:
        raise HTTPException(status_code=501, detail="Initiative store not initialized")
    initiative = _initiative_store.get(initiative_id)
    if initiative is None:
        raise HTTPException(status_code=404, detail="Initiative not found")

    # Update status based on action
    status_map = {
        "acknowledged": "acknowledged",
        "dismissed": "cancelled",
        "snoozed": "pending",
        "approved": "acknowledged",
        "actioned": "completed",
    }
    new_status = status_map.get(action)
    if new_status:
        _initiative_store.update(initiative_id, status=new_status)

    # Close the loop into TypeFeedbackStore. The caller is the model reporting what
    # became of an initiative it was shown. A disposal (dismissed, snoozed,
    # acknowledged) is the outcome this store just recorded and counts, keyed by
    # the initiative so repeating it changes nothing; a claim that the owner
    # approved or acted on it is unverified and is not the owner's verdict, so it
    # does not boost the type. Best-effort: recording never fails the respond.
    _FEEDBACK_OUTCOME_MAP = {
        "dismissed": "dismissed",
        "snoozed": "snoozed",
        "acknowledged": "acknowledged",
    }
    try:
        outcome = _FEEDBACK_OUTCOME_MAP.get(action)
        itype = getattr(initiative, "type", None)
        if _feedback_store is not None and outcome is not None and itype:
            _feedback_store.record(itype, outcome, source=initiative_id)
    except Exception as exc:
        logger.warning(
            "Failed to record type feedback for initiative %s (action=%s): %s",
            initiative_id, action, exc,
        )

    _initiative_store.log_history(
        initiative_id,
        action=f"llm_{action}",
        agent_id="openclaw",
        details=details or {},
    )
    return {
        "success": True,
        "initiative_id": initiative_id,
        "status": new_status or initiative.status,
    }


# --- Agent Snapshot Endpoints ---

@router.get("/agent-snapshot", response_model=AgentSnapshotResponse)
async def agent_snapshot() -> AgentSnapshotResponse:
    """Return a comprehensive snapshot of Protagine state for agent evaluation."""
    now = datetime.now(timezone.utc)

    # Telemetry
    thresholds = {"tick": _tick_stale_hours(_mind())}
    telemetry_dict = await _telemetry.to_dict(thresholds) if _telemetry else {}

    # Pending initiatives (top 20 by priority)
    pending = []
    if _initiative_store is not None:
        pending = _initiative_store.list(status=["pending"], limit=20)

    # Recently completed (top 10 by priority — store orders by priority DESC)
    recent = []
    if _initiative_store is not None:
        recent = _initiative_store.list(status=["completed"], limit=10)

    # Failed initiatives
    failed = []
    if _initiative_store is not None:
        failed = _initiative_store.list(status=["failed"], limit=10)

    # Compute last tick age
    tick_age = None
    if _telemetry is not None and _telemetry.last_tick_at is not None:
        tick_age = (now - _telemetry.last_tick_at).total_seconds() / 60

    # Flags: high-signal items the agent should know about
    flags = []
    if failed:
        flags.append("failed_initiatives")
    if pending and any(i.priority > 0.8 for i in pending):
        flags.append("high_priority_pending")
    if tick_age and tick_age > 30:
        flags.append("stale_autonomy_loop")

    return AgentSnapshotResponse(
        timestamp=now.isoformat(),
        telemetry=telemetry_dict,
        pending_initiatives=[_map_initiative_to_schema(i) for i in pending],
        pending_count=len(pending),
        assigned_count=(
            _initiative_store.count(status=["assigned"]) if _initiative_store else 0
        ),
        failed_count=len(failed),
        recently_completed=[_map_initiative_to_schema(i) for i in recent],
        autonomy_mode=_mind_posture()[0],
        autonomy_running=_mind_posture()[1],
        last_tick_age_minutes=tick_age,
        flags=flags,
    )


@router.post("/agent-snapshot/record-outreach", response_model=RecordOutreachResponse)
async def record_outreach(body: RecordOutreachRequest) -> RecordOutreachResponse:
    """Record that the agent proactively messaged the owner."""
    now = datetime.now(timezone.utc)
    outreach_at = now.isoformat()
    if _telemetry is not None:
        await _telemetry.touch("last_agent_outreach_at")
        if _telemetry.last_agent_outreach_at is not None:
            outreach_at = _telemetry.last_agent_outreach_at.isoformat()
    logger.info(
        "Agent outreach recorded: agent=%s channel=%s reason=%s",
        body.agent_id, body.channel, body.reason,
    )
    try:  # timeline (v0.21.0)
        from protagine.events.journal import append_event
        append_event("outreach.sent", {
            "contact_id": getattr(body, "contact_id", None),
            "channel": body.channel,
            "reason": body.reason,
            "summary": body.reason,
        })
    except Exception:
        logger.debug("journal outreach.sent failed", exc_info=True)
    try:
        # Proactive outreach carries an already-resolved target contact; log
        # it outbound so reciprocity accounting stays whole. Skip entirely
        # without a contact (never attribute an outbound to a placeholder).
        _oc = getattr(body, "contact_id", None)
        if _comms_log is not None and _oc and _oc not in ("system", "default"):
            _comms_log.log(_oc, channel=body.channel or "direct",
                           direction="out", summary=(body.reason or "")[:300])
    except Exception:
        logger.debug("comms ledger outbound log failed", exc_info=True)
    return RecordOutreachResponse(
        recorded_at=now.isoformat(),
        last_agent_outreach_at=outreach_at,
    )


@router.post("/session-report", response_model=SessionReportResponse)
async def session_report(body: SessionReportRequest) -> SessionReportResponse:
    """Store a session summary from the agent for future context retrieval."""
    if _session_report_store is None:
        raise HTTPException(
            status_code=501, detail="Session report store not initialized"
        )

    from protagine.sessions.reports import SessionReport

    # Parse ISO datetimes, ensuring timezone awareness
    def _parse_iso(iso_str: str) -> datetime:
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    report = SessionReport(
        report_id=str(uuid.uuid4()),
        session_id=body.session_id,
        contact_id=body.contact_id,
        started_at=_parse_iso(body.started_at),
        ended_at=_parse_iso(body.ended_at) if body.ended_at else None,
        summary=body.summary,
        topics=body.topics,
        resolutions=body.resolutions,
        pending=body.pending,
        notified_user=body.notified_user,
        metadata=body.metadata,
    )
    await _session_report_store.add_report(report)
    return SessionReportResponse(stored=True, report_id=report.report_id)


@router.get("/context-digest", response_model=ContextDigestResponse)
async def context_digest(
    contact_id: Optional[str] = None,
    hours: int = Query(24, ge=1, le=168),
    initiative_limit: int = Query(10, ge=1, le=100),
) -> ContextDigestResponse:
    """Return a comprehensive context digest for agent session boot.

    Combines recent session reports, pending initiatives, system state,
    and outreach history into a single response.
    """
    now = datetime.now(timezone.utc)

    # Session reports
    session_reports = []
    if _session_report_store is not None and contact_id:
        reports = await _session_report_store.get_recent(
            contact_id, hours=hours, limit=10
        )
        session_reports = [
            ContextDigestSessionReport(
                report_id=r.report_id,
                started_at=r.started_at.isoformat() if r.started_at else "",
                ended_at=r.ended_at.isoformat() if r.ended_at else None,
                summary=r.summary,
                topics=r.topics,
                resolutions=r.resolutions,
                pending=r.pending,
                notified_user=r.notified_user,
            )
            for r in reports
        ]

    # Pending initiatives (reuse agent-snapshot logic)
    pending = []
    if _initiative_store is not None:
        pending = _initiative_store.list(status=["pending"], limit=initiative_limit)

    # System state (reuse agent-snapshot logic)
    thresholds = {"tick": _tick_stale_hours(_mind())}
    telemetry_dict = await _telemetry.to_dict(thresholds) if _telemetry else {}

    tick_age = None
    if _telemetry is not None and _telemetry.last_tick_at is not None:
        tick_age = (now - _telemetry.last_tick_at).total_seconds() / 60

    silence_flags = telemetry_dict.get("silence_hours", {})
    stale_flags = telemetry_dict.get("stale_flags", [])

    # Last outreach
    last_outreach = {"at": None, "reason": None}
    if _telemetry is not None and _telemetry.last_agent_outreach_at is not None:
        last_outreach = {
            "at": _telemetry.last_agent_outreach_at.isoformat(),
            "reason": None,
        }

    # Map initiatives (module-level helper extracted from agent-snapshot)
    system_state = AgentSnapshotSystemState(
        autonomy_running=_mind_posture()[1],
        mode=_mind_posture()[0],
        last_tick_age_minutes=tick_age,
        silence_hours=silence_flags,
        stale_flags=stale_flags,
    )

    return ContextDigestResponse(
        generated_at=now.isoformat(),
        contact_id=contact_id,
        session_reports=session_reports,
        pending_initiatives=[_map_initiative_to_schema(i) for i in pending],
        system_state=system_state,
        last_outreach=last_outreach,
    )


# --- WebSocket Endpoint ---

@router.websocket("/agents/{agent_id}/stream")
async def agent_websocket_stream(ws: WebSocket, agent_id: str) -> None:
    """WebSocket endpoint for real-time initiative delivery."""
    if _websocket_manager is None:
        await ws.close(code=1011, reason="WebSocket manager not initialized")
        return

    await _websocket_manager.handle_connection(ws, agent_id)
