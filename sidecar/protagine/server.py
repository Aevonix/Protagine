"""Protagine sidecar FastAPI server.

Intelligence sidecar server mounted by agent frameworks (OpenClaw, Hermes,
etc.) as a plugin via the ``/v1/host`` API surface.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

# CLI and direct service starts use the same selected private instance.
from protagine.util.instance import load_environment
load_environment()

from fastapi import FastAPI

from protagine.api.routers.host import (
    router as host_router,
    v2_router as host_v2_router,
    set_llm_router,
    set_embedder,
    set_reranker,
    set_goals_store,
    set_contacts_store,
    set_briefings_engine,
    set_research_pipeline,
    set_search_orchestrator,
    set_insight_store,
    set_secrets_manager,
    set_session_store,
    set_commitment_store,
    set_affect_store,
    set_facts_store,
    set_comms_log,
    set_preference_learner,
    set_pattern_store,
    # Multi-Agent v0.7.0
    set_agent_store,
    set_invite_store,
    set_initiative_store,
    set_assignment_engine,
    set_websocket_manager,
    set_telemetry,
    set_session_report_store,
    set_situation_spine,
    supported_capabilities,
)

from protagine import get_state_dir

logger = logging.getLogger(__name__)


def _state_dir() -> Path:
    """Resolve the Protagine state directory (wrapper for get_state_dir)."""
    return get_state_dir()


def _attach_situation_spine(*, state_dir: Path):
    """Attach the P6 situation observer when its migration flag is on.

    The off path clears stale in-process handles and constructs nothing. Any
    other mode attaches the store, the reducer and the gate as an observer;
    there is no second decision path left for it to compose with.
    """

    from protagine.self_model.situation import (
        AppropriatenessGate,
        SituationReducer,
        SituationStore,
        situation_spine_enabled,
        situation_spine_mode,
    )

    set_situation_spine(None, None)
    if not situation_spine_enabled():
        return None

    mode = situation_spine_mode()
    store = SituationStore(str(state_dir / "protagine-situation.db"))
    try:
        reducer = SituationReducer(store)
        gate = AppropriatenessGate()
        try:
            initial_status = reducer.run_once(limit=100)
        except Exception as exc:
            initial_status = {
                "enabled": True,
                "mode": mode,
                "processed": 0,
                "error": f"initial_reduce_failed:{type(exc).__name__}",
            }
    except Exception:
        store.close()
        raise
    set_situation_spine(store, reducer)
    return {
        "mode": mode,
        "store": store,
        "reducer": reducer,
        "gate": gate,
        "initial_status": initial_status,
    }


def _initialize_learning_feedback(state_dir: Path) -> dict:
    """The owner's corrections ledger (``/v1/host/learning/correction``) and the selfhood benchmark that
    reads it (deleted in M10). Setters are cleared before construction so a second lifespan in the same
    interpreter cannot keep a stale store."""

    from protagine.api.routers.host import set_benchmark, set_learning_feedback_store
    from protagine.intelligence.learning.feedback_store import FeedbackStore
    from protagine.self_model.benchmark import (
        BenchmarkStore,
        SelfhoodBenchmark,
        benchmark_enabled,
        canonical_probe_recall,
    )

    set_learning_feedback_store(None)
    set_benchmark(None)

    correction_store = FeedbackStore(
        db_path=str(state_dir / "protagine-learning-feedback.db"))

    benchmark = None
    if benchmark_enabled():
        benchmark = SelfhoodBenchmark(
            BenchmarkStore(db_path=str(state_dir / "protagine-benchmark.db")),
            corrections=correction_store,
            recall=canonical_probe_recall(state_dir),
        )

    set_learning_feedback_store(correction_store)
    set_benchmark(benchmark)
    return {"corrections": correction_store, "benchmark": benchmark}


async def _initialize_contacts_store():
    """Open the canonical contact store without graph backfill or pruning.

    A merge moves the person's ledger sources, comms and affect with their
    handles whoever starts it: the people router passes these itself; the
    owner confirming a link (which folds the shadow contact that held the
    handle) goes through the store's defaults, set here. The comms log and
    affect store are opened before the contacts.
    """
    from protagine.api.routers import people as people_router
    from protagine.contacts.config import ContactsConfig
    from protagine.contacts.store import SQLiteContactStore

    config = ContactsConfig.from_env()
    store = SQLiteContactStore(config=config, sources_of=people_router.person_sources,
                               reattribute=people_router.reattribute_hooks())
    await store.connect()
    set_contacts_store(store)
    logger.info("ContactsStore initialized (path=%s)", config.sqlite_path)
    return store


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize subsystems on startup, tear down on shutdown."""
    # Before any store opens: the vector store needs more open files than a user
    # process starts with on macOS, whether or not a service unit asked for them.
    from protagine.resources import raise_open_file_limit
    raise_open_file_limit()
    state_dir = _state_dir()

    # --- 1. LLM Router ---
    llm_router = None
    try:
        from protagine.router.router import LLMRouter
        from protagine.router.tiers import build_tiers_from_host
        import json as _json

        config_path = state_dir / ".protagine-llm-config.json"
        if config_path.exists():
            try:
                host_llm_config = _json.loads(config_path.read_text())
                llm_router = LLMRouter(tiers={})
                llm_router.configure(host_llm_config, config_path=config_path)
                logger.info(
                    "LLMRouter initialized from persisted host config (provider=%s)",
                    host_llm_config.get("provider", "unknown"),
                )
            except Exception as cfg_exc:
                logger.warning("Persisted LLM config unavailable: %s", type(cfg_exc).__name__)
                llm_router = LLMRouter(tiers={})
                llm_router.watch_config(config_path)
        else:
            llm_router = LLMRouter(tiers={})
            llm_router.watch_config(config_path)
            logger.info("LLMRouter awaiting explicit host config")
    except Exception as exc:
        logger.warning("LLMRouter init failed — model calls will not be available: %s", exc)

    if llm_router is not None:
        set_llm_router(llm_router)

    # --- 6. Embedding pipeline ---
    embed_provider = os.environ.get("PROTAGINE_EMBED_PROVIDER", "")
    embed_model = os.environ.get("PROTAGINE_EMBED_MODEL", "")
    embed_dims = os.environ.get("PROTAGINE_EMBED_DIMS", "")
    reranker_model = os.environ.get("PROTAGINE_RERANKER_MODEL", "")

    # Auto-detect tier if not explicitly configured
    if embed_provider != "skip" and (not embed_provider or not embed_model):
        try:
            from protagine.vector.scanner import scan
            from protagine.vector.tiers import get_tier_by_memory
            hw = scan()
            tier = get_tier_by_memory(hw.vram_gb, hw.ram_gb)
            spec = tier.text_embedder
            if spec:
                if hw.gpu_type == "cuda":
                    embed_provider = embed_provider or "cuda"
                elif hw.gpu_type == "mlx":
                    # Prefer native MLX when the package is available
                    try:
                        import mlx_embeddings  # noqa: F401
                        embed_provider = embed_provider or "native_mlx"
                    except ImportError:
                        embed_provider = embed_provider or "mlx"
                else:
                    embed_provider = embed_provider or "cpu"
                embed_model = embed_model or spec.model_id
                embed_dims = embed_dims or str(spec.dims)
                reranker_model = reranker_model or (tier.text_reranker.model_id if tier.text_reranker else "")
                logger.info(
                    "Auto-detected embedding tier: %s (GPU=%s %dGB, RAM=%dGB) -> %s",
                    tier.label, hw.gpu_name, hw.vram_gb, hw.ram_gb, spec.model_id,
                )
        except Exception as exc:
            logger.warning("Hardware scan failed, using defaults: %s", exc)
            embed_provider = embed_provider or "cpu"
            embed_model = embed_model or "sentence-transformers/all-MiniLM-L6-v2"
            embed_dims = embed_dims or "384"

    from protagine.api.routers.host import set_embed_failure
    set_embed_failure(None)
    try:
        from protagine.vector.embedder import EmbeddingPipeline
        from protagine.vector.config import EmbeddingConfig
        request_dims = (int(os.environ["PROTAGINE_EMBED_REQUEST_DIMS"])
                        if os.environ.get("PROTAGINE_EMBED_REQUEST_DIMS") else None)
        if embed_dims:
            declared_dims = int(embed_dims)
        elif request_dims:
            declared_dims = request_dims
        elif embed_provider == "openai_api":
            declared_dims = 0   # learned from the endpoint's first embedding
        else:
            declared_dims = 384
        embed_config = EmbeddingConfig(
            provider=embed_provider,
            model_id=embed_model,
            dimensions=declared_dims,
            revision=os.environ.get("PROTAGINE_EMBED_REVISION") or None,
            request_dimensions=request_dims,
        )
        from protagine.vector.embedder import make_provider
        provider = make_provider(embed_config)
        if provider is not None and embed_provider == "openai_api" and hasattr(provider, "configure"):
            # openai_api needs explicit endpoint config (the text-only path
            # never called configure(); only the multimodal branch passed these).
            provider.configure(
                os.environ.get("PROTAGINE_EMBED_BASE_URL", ""),
                os.environ.get("PROTAGINE_EMBED_API_KEY", ""),
            )
        if provider is None:
            # 'skip' provider — embeddings disabled entirely
            logger.info("EmbeddingPipeline skipped (provider=skip) — embeddings disabled")
        else:
            pipeline = EmbeddingPipeline(provider)

            # Wire up multimodal if enabled
            multimodal_enabled = os.environ.get("PROTAGINE_MULTIMODAL", "false").lower() == "true"
            if multimodal_enabled:
                try:
                    from protagine.vector.multimodal_provider import make_multimodal_provider
                    from protagine.vector.image_store import make_image_store

                    mm_config = EmbeddingConfig(
                        provider=embed_provider,
                        model_id=embed_model,
                        dimensions=int(embed_dims) if embed_dims else 1024,
                        base_url=os.environ.get("PROTAGINE_EMBED_BASE_URL"),
                        api_key=os.environ.get("PROTAGINE_EMBED_API_KEY"),
                        request_dimensions=embed_config.request_dimensions,
                    )
                    mm_provider = make_multimodal_provider(mm_config)
                    img_store = make_image_store(
                        mode=os.environ.get("PROTAGINE_IMAGE_STORAGE", "local"),
                        state_dir=os.environ.get("PROTAGINE_STATE_DIR", "."),
                    )
                    pipeline = EmbeddingPipeline(
                        provider=provider,
                        multimodal_provider=mm_provider,
                        image_store=img_store,
                    )
                    logger.info("Multimodal enabled (model=%s, storage=%s)", embed_model, os.environ.get("PROTAGINE_IMAGE_STORAGE", "local"))
                except Exception as exc:
                    logger.warning("Multimodal init failed, falling back to text-only: %s", exc)

            await pipeline.warmup()
            set_embedder(pipeline)
            logger.info("EmbeddingPipeline initialized (provider=%s model=%s)", embed_provider, embed_model)

            # Open the vector store the source projections and semantic recall share
            try:
                from protagine.vector.store import VectorStore
                from protagine.vector.indexes import IndexCatalog
                from protagine.turns import get_turn_idempotency_ledger
                from protagine.vector import set_store, set_pipeline
                vector_db_path = os.path.join(state_dir, "lancedb")
                vs = VectorStore(data_dir=vector_db_path, identity=pipeline.index_identity,
                    catalog=IndexCatalog(get_turn_idempotency_ledger(state_dir)))
                embed_dims = int(os.environ.get("PROTAGINE_EMBED_DIMS") or pipeline.dimensions or 384)
                await vs.connect(dimensions=embed_dims)
                await vs.ensure_collections(dimensions=embed_dims)
                set_store(vs)
                set_pipeline(pipeline)
                logger.info("Vector store wired for semantic recall (path=%s)", vector_db_path)
            except Exception as vexc:
                logger.warning("Vector store wiring failed (recall will use keyword fallback): %s", vexc)
                set_embed_failure(f"the vector store did not open: {type(vexc).__name__}: {vexc}")

            # Pass LLM config to pipeline for auto-captioning
            llm_config_path = Path(os.environ.get("PROTAGINE_STATE_DIR", ".")) / ".protagine-llm-config.json"
            if llm_config_path.exists() and hasattr(pipeline, "set_llm_config"):
                try:
                    llm_cfg = _json.loads(llm_config_path.read_text())
                    pipeline.set_llm_config(llm_cfg)
                    logger.info("LLM config passed to EmbeddingPipeline for auto-captioning")
                except Exception as exc:
                    logger.debug("Could not pass LLM config to pipeline: %s", exc)

            # Health check + model mismatch detection
            try:
                hc = await pipeline.health_check()
                if hc.get("status") != "ok":
                    logger.warning("Embedder health check failed: %s", hc.get("error", "unknown"))
                else:
                    logger.info("Embedder health check passed (latency=%.1fms)", hc.get("latency_ms", 0))
            except Exception as exc:
                logger.warning("Embedder health check exception: %s", exc)
    except Exception as exc:
        logger.warning("EmbeddingPipeline init failed: %s", exc)
        # The sidecar still serves, so nothing else would say it: health carries the
        # reason in words and stays degraded until the embedder comes up.
        set_embed_failure(f"the embedder (provider={embed_provider}, model={embed_model or 'unset'}) "
                          f"did not initialise: {type(exc).__name__}: {exc}")

    # --- 6b. Reranker pipeline ---
    reranker_provider_name = os.environ.get("PROTAGINE_RERANKER_PROVIDER", "")
    if reranker_model and reranker_model.lower() not in ("none", "", "null"):
        try:
            from protagine.vector.reranker import (
                OpenAIAPIRerankerProvider,
                NativeMLXRerankerProvider,
                MLXRerankerProvider,
                CPURerankerProvider,
                CUDARerankerProvider,
            )
            reranker_base_url = os.environ.get("PROTAGINE_RERANKER_BASE_URL", "")
            reranker_api_key = os.environ.get("PROTAGINE_RERANKER_API_KEY", "")
            if reranker_provider_name == "openai_api" or reranker_base_url:
                # Remote reranker over an OpenAI/Jina-compatible /v1/rerank
                # endpoint, mirroring the embedder's openai_api path so the
                # model stays off-box instead of loading in-process.
                # PROTAGINE_RERANKER_PROMPT_STYLE=qwen3 applies the Qwen3-Reranker
                # instruction template, without which its scores are noise.
                reranker_provider = OpenAIAPIRerankerProvider(reranker_model)
                reranker_provider.configure(
                    reranker_base_url,
                    reranker_api_key,
                    os.environ.get("PROTAGINE_RERANKER_PROMPT_STYLE", ""),
                )
            else:
                from protagine.vector.scanner import scan
                hw = scan()
                if hw.gpu_type == "mlx":
                    # Prefer native MLX when the package is available
                    try:
                        import mlx_lm  # noqa: F401
                        reranker_provider = NativeMLXRerankerProvider(reranker_model)
                    except ImportError:
                        reranker_provider = MLXRerankerProvider(reranker_model)
                elif hw.gpu_type == "cuda":
                    reranker_provider = CUDARerankerProvider(reranker_model)
                else:
                    reranker_provider = CPURerankerProvider(reranker_model)
            await reranker_provider.warmup()
            set_reranker(reranker_provider)
            logger.info(
                "Reranker initialized (provider=%s model=%s)",
                reranker_provider_name or "local", reranker_model,
            )
        except Exception as exc:
            logger.warning("Reranker init failed: %s", exc)
    else:
        logger.info("No reranker configured for this tier")

    # --- 7. Retained goal records ---
    goals_store = None
    try:
        from protagine.goals.store import GoalStore
        goals_db = os.path.join(state_dir, "protagine-goals.db")
        goals_store = GoalStore(db_path=goals_db)
        set_goals_store(goals_store)
        logger.info("Goal records opened (db=%s)", goals_db)
    except Exception as exc:
        logger.warning("Goal records unavailable: %s", exc)

    # --- 7b. Commitment Store ---
    try:
        from protagine.commitments.store import CommitmentStore

        commitments_db = state_dir / "protagine-commitments.db"
        commitment_store = CommitmentStore(db_path=commitments_db)
        commitment_readiness = (
            commitment_store.resolution_recovery_readiness()
        )
        if commitment_readiness.get("ready") is not True:
            raise RuntimeError(
                "CommitmentStore resolution recovery is unavailable"
            )

        # Resolving a workspace concern raised from a commitment settles the
        # commitment itself — without this, the ingest loop re-raises the
        # concern from the still-open commitment and the resolve is cosmetic.
        from protagine.self_model.settlement import register_settler

        def _settle_commitment(source_id, *, outcome="done", note="",
                               resolved_by="owner", operation_id=None,
                               _cs=commitment_store):
            row = _cs.resolve(source_id, outcome=outcome, note=note,
                              resolved_by=resolved_by,
                              operation_id=operation_id)
            if not row:
                return None
            operation = (
                _cs.get_resolution_operation(source_id)
                if operation_id is not None else None
            )
            resolution = operation or (
                (row.get("metadata") or {}).get("resolution") or {}
            )
            return {
                "kind": "commitment",
                "status": row["status"],
                "operation_id": resolution.get("operation_id"),
                "outcome": resolution.get("outcome"),
                "note_digest": resolution.get("note_digest"),
                "resolved_by": (
                    resolution.get("resolved_by")
                    if operation is not None else resolution.get("by")
                ),
            }

        register_settler("commitment", _settle_commitment, retry_safe=True)
        set_commitment_store(commitment_store)
        logger.info(
            "CommitmentStore initialized (db=%s, capability=%s)",
            commitments_db, commitment_readiness["capability"],
        )
    except Exception as exc:
        set_commitment_store(None)
        logger.error("CommitmentStore initialization failed")
        raise RuntimeError("CommitmentStore initialization failed") from exc

    # --- 7c. Theory of Mind ---
    try:
        from protagine.tom.affect import AffectStore
        from protagine.tom.facts import SharedFactsStore

        from protagine.turns import get_turn_idempotency_ledger
        source_ledger = get_turn_idempotency_ledger(state_dir)
        affect_db = state_dir / "protagine-affect.db"
        affect_store = AffectStore(db_path=affect_db, source_ledger=source_ledger)
        affect_store.purge_erased_sources()
        set_affect_store(affect_store)
        logger.info("AffectStore initialized (db=%s)", affect_db)

        facts_db = state_dir / "protagine-facts.db"
        facts_store = SharedFactsStore(db_path=facts_db, source_ledger=source_ledger)
        facts_store.purge_erased_sources()
        set_facts_store(facts_store)
        logger.info("SharedFactsStore initialized (db=%s)", facts_db)

        from protagine.contacts.comms import CommsLog
        comms_log = CommsLog(db_path=state_dir / "protagine-comms.db", source_ledger=source_ledger)
        comms_log.purge_erased_sources()
        set_comms_log(comms_log)
        logger.info("CommsLog initialized")

        # Owner preference learner — captures the owner's *explicit* directives
        # about how to communicate ("be concise", "use bullets", "no emoji") at
        # high confidence.
        from protagine.intelligence.components.preference_learner import PreferenceLearner
        from protagine.self_model.perspective import SelfPerspective
        from protagine.turns import get_turn_idempotency_ledger
        from protagine.identity import get_owner_contact_id
        preference_learner = PreferenceLearner(db_path=str(state_dir / "protagine-preferences.db"),
            perspective=SelfPerspective(get_turn_idempotency_ledger(state_dir), owner_id=get_owner_contact_id()))
        set_preference_learner(preference_learner)
        logger.info("PreferenceLearner initialized (db=%s)", state_dir / "protagine-preferences.db")
    except Exception as exc:
        logger.warning("Theory of Mind init failed: %s", exc)

    # --- Proposal store (self-directed thinking + research -> proposals) ---
    try:
        from protagine.proposals import ProposalStore
        from protagine.api.routers.host import set_proposal_store
        _proposal_store = ProposalStore(db_path=str(state_dir / "protagine-proposals.db"))
        set_proposal_store(_proposal_store)
        logger.info("ProposalStore initialized (db=%s)", state_dir / "protagine-proposals.db")
    except Exception as exc:
        logger.warning("ProposalStore init failed: %s", exc)

    # --- Type feedback store (outcome-driven priority decay/boost) ---
    try:
        from protagine.feedback import TypeFeedbackStore
        from protagine.api.routers.host import set_feedback_store
        set_feedback_store(TypeFeedbackStore(db_path=str(state_dir / "protagine-feedback.db")))
        logger.info("TypeFeedbackStore initialized (db=%s)", state_dir / "protagine-feedback.db")
    except Exception as exc:
        logger.warning("TypeFeedbackStore init failed: %s", exc)

    # --- Self-model / trust engine + action journal (item 4, Amendment 1) ---
    # Wired before directed action so approval tiering can consult trust.
    _sm_for_directed = None
    try:
        from protagine.self_model import (
            ActionJournal, CompetenceStore, SelfModel, TrustEngine,
            self_model_enabled,
        )
        from protagine.api.routers.host import (
            set_self_model, _feedback_store as _fb_for_trust,
        )
        if self_model_enabled():
            _competence = CompetenceStore(
                db_path=str(state_dir / "protagine-self-model.db"))
            _journal = ActionJournal(
                db_path=str(state_dir / "protagine-action-journal.db"))
            _trust = TrustEngine(
                _competence, db_path=str(state_dir / "protagine-self-model.db"),
                feedback_store=_fb_for_trust, journal=_journal)
            _sm_for_directed = SelfModel(_competence, trust=_trust, journal=_journal)
            _sm_for_directed.perspective = getattr(locals().get('preference_learner'), 'perspective', None)
            set_self_model(_sm_for_directed)
            logger.info(
                "SelfModel/TrustEngine initialized (db=%s, journal=%s, "
                "autograduate=%s)",
                state_dir / "protagine-self-model.db",
                state_dir / "protagine-action-journal.db",
                os.environ.get("PROTAGINE_TRUST_AUTOGRADUATE", "true"))
        else:
            logger.info("SelfModel disabled (PROTAGINE_SELF_MODEL_ENABLED=false)")
    except Exception as exc:
        logger.warning("SelfModel init failed: %s", exc)

    # --- The owner's corrections ledger and the selfhood benchmark ---
    try:
        _learning = _initialize_learning_feedback(state_dir)
        if _learning["benchmark"] is not None:
            logger.info(
                "Selfhood benchmark ready (db=%s, corrections=%s)",
                state_dir / "protagine-benchmark.db",
                state_dir / "protagine-learning-feedback.db",
            )
        else:
            logger.info(
                "Selfhood benchmark disabled (PROTAGINE_BENCHMARK_ENABLED=false)")
    except Exception as exc:
        logger.error("Learning feedback init failed closed: %s", exc)

    # --- Expectation engine (Mind M3a): predictions + surprise + calibration ---
    try:
        from protagine.self_model.expectations import (
            ExpectationEngine, ExpectationStore, expectations_enabled,
            expectations_mode,
        )
        from protagine.api.routers.host import set_expectations
        if expectations_enabled():
            _exp_store = ExpectationStore(
                db_path=str(state_dir / "protagine-expectations.db"))
            _exp_journal = None
            try:
                from protagine.api.routers.host import _self_model as _sm_e
                _exp_journal = getattr(_sm_e, "journal", None)
            except Exception:
                _exp_journal = None
            # workspace wired later; set_expectations stores the engine and the
            # autonomy phase links the workspace ref at runtime.
            _expectations = ExpectationEngine(_exp_store, journal=_exp_journal)
            set_expectations(_expectations)
            logger.info("Expectation engine ready (mode=%s, db=%s)",
                        expectations_mode(),
                        state_dir / "protagine-expectations.db")
        else:
            logger.info("Expectation engine disabled (PROTAGINE_EXPECTATIONS=off)")
    except Exception as exc:
        logger.warning("Expectation engine init failed: %s", exc)

    # --- Skills memory (procedure memory, item 3) ---
    _skills_mem_store = None
    try:
        from protagine.skills_memory import SkillStore, skills_distill_mode
        from protagine.api.routers.host import set_skill_store
        _skills_mem_store = SkillStore(
            db_path=str(state_dir / "protagine-skills.db"))
        set_skill_store(_skills_mem_store)
        logger.info("SkillStore initialized (db=%s, %d skill(s), distill=%s)",
                    state_dir / "protagine-skills.db",
                    _skills_mem_store.count(), skills_distill_mode())
    except Exception as exc:
        logger.warning("SkillStore init failed: %s", exc)

    # --- Mining: escalation miner + verbatim turn capture (corpus source) ---
    try:
        from protagine.mining import EscalationMiner, MiningStore, mining_mode
        from protagine.api.routers.mining import set_mining

        if mining_mode() != "off":
            _mining_store_obj = MiningStore(
                db_path=str(state_dir / "protagine-mining.db"))

            def _mining_router_getter():
                try:
                    from protagine.api.routers.host import _llm_router
                    return _llm_router
                except Exception:
                    return None

            _mining_engine_obj = EscalationMiner(
                _mining_store_obj,
                skill_store=_skills_mem_store,
                router_getter=_mining_router_getter,
            )
            set_mining(_mining_store_obj, _mining_engine_obj, state_dir)
            logger.info("EscalationMiner initialized (db=%s, mode=%s)",
                        state_dir / "protagine-mining.db", mining_mode())
        else:
            logger.info("Mining disabled (PROTAGINE_ESCALATION_MINING=off)")
    except Exception as exc:
        logger.warning("Mining init failed: %s", exc)

    # --- Read-only repo mirrors ---
    try:
        from protagine.repos import RepoMirrorManager
        from protagine.api.routers.host import set_repo_mirrors
        _mirrors_mgr = RepoMirrorManager(mirror_dir=str(state_dir / "repo-mirrors"))
        set_repo_mirrors(_mirrors_mgr)
        _n_repos = len(_mirrors_mgr.configured())
        if _n_repos:
            # Clone/pull in the background so boot is not blocked by network.
            async def _sync_mirrors():
                try:
                    loop_ = asyncio.get_event_loop()
                    results = await loop_.run_in_executor(None, _mirrors_mgr.refresh_all)
                    logger.info(
                        "Repo mirrors synced: %s",
                        {k: v.get("action") or v.get("reason") for k, v in results.items()},
                    )
                except Exception:
                    logger.debug("mirror sync failed", exc_info=True)
            asyncio.create_task(_sync_mirrors())
        logger.info("RepoMirrorManager initialized (%d repo(s) configured)", _n_repos)
    except Exception as exc:
        logger.warning("Repo mirror init failed: %s", exc)

    # --- Pattern Extraction ---
    try:
        from protagine.patterns.store import PatternStore

        patterns_db = state_dir / "protagine-patterns.db"
        pattern_store = PatternStore(db_path=patterns_db)
        set_pattern_store(pattern_store)
        logger.info("PatternStore initialized (db=%s)", patterns_db)
    except Exception as exc:
        logger.warning("Pattern init failed: %s", exc)

    # --- 7e. Channel Registration Store ---
    channel_store = None
    try:
        from protagine.channels.store import ChannelStore
        from protagine.channels.router import set_channel_store

        channels_db = os.path.join(state_dir, "protagine-channels.db")
        channel_store = ChannelStore(db_path=channels_db)
        channel_store.connect()
        set_channel_store(channel_store)
        from protagine.api.routers.host import set_channel_store as _host_set_channel_store
        _host_set_channel_store(channel_store)   # turn traffic auto-registers + touches channels
        logger.info("ChannelStore initialized (db=%s)", channels_db)
    except Exception as exc:
        logger.warning("ChannelStore init failed: %s", exc)

    # --- 8. Contacts ---
    contacts_store = None
    try:
        contacts_store = await _initialize_contacts_store()
    except Exception as exc:
        logger.warning("ContactsStore init failed: %s", exc)

    # --- 9. Briefings ---
    try:
        from protagine.briefings.engine import BriefingEngine
        briefings = BriefingEngine()
        set_briefings_engine(briefings)
        logger.info("BriefingEngine initialized")
    except Exception as exc:
        logger.warning("BriefingEngine init failed: %s", exc)

    # --- 12. Research pipeline ---
    try:
        from protagine.research.search.orchestrator import SearchOrchestrator

        # Wire search orchestrator
        search_orchestrator = SearchOrchestrator()
        search_provider = os.environ.get("PROTAGINE_SEARCH_PROVIDER", "")
        if search_provider == "tavily" and os.environ.get("TAVILY_API_KEY"):
            from protagine.research.search.tavily import TavilyProvider
            search_orchestrator.add_provider(TavilyProvider(os.environ["TAVILY_API_KEY"]))
            logger.info("Search provider: Tavily")
        elif search_provider == "serpapi" and os.environ.get("SERPAPI_KEY"):
            from protagine.research.search.serpapi import SerpAPIProvider
            search_orchestrator.add_provider(SerpAPIProvider(os.environ["SERPAPI_KEY"]))
            logger.info("Search provider: SerpAPI")
        elif search_provider == "brave" and os.environ.get("BRAVE_API_KEY"):
            from protagine.research.search.brave import BraveSearchProvider
            search_orchestrator.add_provider(BraveSearchProvider(os.environ["BRAVE_API_KEY"]))
            logger.info("Search provider: Brave")
        else:
            # Zero-config fallback so web_search works out of the box.
            from protagine.research.search.duckduckgo import DuckDuckGoProvider
            search_orchestrator.add_provider(DuckDuckGoProvider())
            logger.info("Search provider: DuckDuckGo (default fallback)")

        set_search_orchestrator(search_orchestrator)

        from protagine.research.pipeline import ResearchPipeline
        research = ResearchPipeline()
        set_research_pipeline(research)
        logger.info("ResearchPipeline initialized")
    except Exception as exc:
        logger.warning("ResearchPipeline init failed: %s", exc, exc_info=True)

    # --- 13. Briefings: persistence and schedule (no proactive gateway) ---
    # The delivery bridge and its /internal/deliver path are gone; owner
    # messages go through the mind's outbox. Briefings keep their store and
    # schedule and are read through their API until the package audit.
    try:
        from pathlib import Path as _P

        from protagine.briefings.config import BriefingConfig
        from protagine.briefings.engine import BriefingEngine
        from protagine.briefings.scheduler import BriefingScheduler
        from protagine.briefings.store import BriefingStore

        b_cfg = BriefingConfig()
        b_cfg.daily.time = os.environ.get("PROTAGINE_BRIEFING_DAILY_TIME", b_cfg.daily.time)
        b_cfg.daily.timezone = os.environ.get("PROTAGINE_BRIEFING_TZ", b_cfg.daily.timezone)
        b_cfg.weekly.timezone = b_cfg.daily.timezone
        b_cfg.delivery_gateway = os.environ.get("PROTAGINE_BRIEFING_GATEWAY", "whatsapp")
        b_cfg.lm_enhancement_enabled = (
            os.environ.get("PROTAGINE_BRIEFING_LM_ENHANCE", "0") not in ("0", "false", "no")
        )
        _b_state_dir = os.environ.get("PROTAGINE_STATE_DIR", ".")
        b_store = BriefingStore(db_path=str(_P(_b_state_dir) / "briefings.db"))
        # Real aggregators where the backing subsystem exists — without
        # them the composer silently falls back to stubs and every data
        # section of every briefing is empty. Calendar/anomaly/mind/
        # synthesis still lack concrete aggregators (see docs/KNOWN-GAPS.md).
        _aggs = {}
        try:
            if goals_store is not None:
                from protagine.briefings.aggregators import GoalStoreAggregator
                _aggs["goal_aggregator"] = GoalStoreAggregator(goals_store)
        except Exception:
            logger.debug("goal aggregator wiring failed", exc_info=True)
        try:
            # Both resolve their subsystems lazily off host globals at
            # call time (the anomaly detector doesn't even exist until
            # the autonomy registry builds it, well after this point).
            from protagine.briefings.aggregators import (
                AnomalyDetectorAggregator, DiscovererSynthesisAggregator)
            _aggs["anomaly_aggregator"] = AnomalyDetectorAggregator()
            _aggs["synthesis_aggregator"] = DiscovererSynthesisAggregator()
        except Exception:
            logger.debug("anomaly/synthesis aggregator wiring failed", exc_info=True)
        try:
            # resolves the enabled calendar connector instance(s) — base
            # or per-account — at call time; harmless when none enabled
            from protagine.briefings.aggregators import ConnectorCalendarAggregator
            _aggs["calendar_aggregator"] = ConnectorCalendarAggregator()
        except Exception:
            logger.debug("calendar aggregator wiring failed", exc_info=True)
        briefings = BriefingEngine(config=b_cfg, store=b_store,
                                   delivery_bridge=None, **_aggs)
        set_briefings_engine(briefings)
        if os.environ.get("PROTAGINE_BRIEFINGS_SCHEDULE", "1") not in ("0", "false", "no"):
            b_sched = BriefingScheduler(config=b_cfg, engine=briefings, store=b_store)
            briefings.attach_scheduler(b_sched)
            b_sched.start()
        logger.info(
            "BriefingEngine rewired: gateway=%s daily=%s %s scheduler=on",
            b_cfg.delivery_gateway, b_cfg.daily.time, b_cfg.daily.timezone,
        )
    except Exception as exc:
        logger.warning("Briefing delivery wiring failed: %s", exc)

    # Insight overlay store (tracks dismissed-insight IDs).
    try:
        from protagine.intelligence.synthesis.insight_store import InsightStore
        insight_store = InsightStore(state_dir / "insights.db")
        set_insight_store(insight_store)
        logger.info("InsightStore initialized")
    except Exception as exc:
        logger.warning("InsightStore init failed: %s", exc)

    # --- 18. Secrets ---
    try:
        from protagine.secrets.manager import SecretsManager
        secrets = SecretsManager()
        set_secrets_manager(secrets)
        logger.info("SecretsManager initialized")
    except Exception as exc:
        logger.warning("SecretsManager init failed: %s", exc)

    # --- 19. Session store ---
    try:
        from protagine.sessions.store import InMemorySessionStore
        session_store = InMemorySessionStore()
        set_session_store(session_store)
        logger.info("InMemorySessionStore initialized")
    except Exception as exc:
        logger.warning("SessionStore init failed: %s", exc)

    # --- 20c. Multi-Agent System (v0.7.0) ---
    try:
        from protagine.agents.store import AgentStore, InviteStore
        from protagine.initiatives.store import InitiativeStore
        from protagine.initiatives.assignment import AssignmentEngine
        from protagine.agents.websocket import WebSocketManager

        agent_store = AgentStore(state_dir=state_dir)
        invite_store = InviteStore(state_dir=state_dir)
        set_agent_store(agent_store)
        set_invite_store(invite_store)
        logger.info("AgentStore initialized (state_dir=%s)", state_dir)

        initiative_store = InitiativeStore(state_dir=state_dir)
        set_initiative_store(initiative_store)
        logger.info("InitiativeStore initialized (state_dir=%s)", state_dir)

        # Observation store (v0.16.0) — agent-reported domain snapshots
        try:
            from protagine.observations.store import ObservationStore
            from protagine.api.routers.observations import set_observation_store
            observation_store = ObservationStore(state_dir=state_dir)
            set_observation_store(observation_store)
            logger.info("ObservationStore initialized (state_dir=%s)", state_dir)
        except Exception as exc:
            logger.warning("ObservationStore init failed (non-fatal): %s", exc)

        assignment_engine = AssignmentEngine(
            agent_store=agent_store,
            initiative_store=initiative_store,
        )
        set_assignment_engine(assignment_engine)
        logger.info("AssignmentEngine initialized")

        websocket_manager = WebSocketManager(
            agent_store=agent_store,
            initiative_store=initiative_store,
        )
        set_websocket_manager(websocket_manager)
        logger.info("WebSocketManager initialized")
    except Exception as exc:
        logger.warning("Multi-Agent System init failed: %s", exc)

    # Temporal telemetry: the mind's tick beats into it, turns/sync and context/assemble
    # touch it, and /v1/host/health reads it. Built before the mind so the tick can report.
    from protagine.telemetry import TelemetryStore
    telemetry = TelemetryStore()
    telemetry.load()  # restore last_*_at across restart (v0.21.0)
    telemetry.started_at = datetime.now(timezone.utc)
    app.state.telemetry = telemetry
    set_telemetry(telemetry)
    logger.info("TelemetryStore initialized")

    # --- 21. The mind tick (architecture 3.2) ---
    # Replaces the autonomy loop and its scheduler: health_check became the
    # tick's upkeep probes, digest_flush became the outbox digest, and the
    # other scheduled jobs went with their modules.
    mind = None
    try:
        from protagine.mind.factory import build_mind
        from protagine.api.routers.mind import set_mind
        from protagine.api.routers import host as _host_for_mind
        from protagine.config import load_config, load_identity, update_config
        from protagine.identity import get_owner_contact_id
        from protagine.turns import get_turn_idempotency_ledger

        _mind_store = locals().get("initiative_store")
        if _mind_store is None:
            raise RuntimeError("the initiative store is not wired")
        _mind_cfg = load_config()
        _mind_identity = load_identity(_mind_cfg.home) if _mind_cfg.exists else {}
        _mind_interests = [str(item) for item in (_mind_identity.get("agent") or {}).get("interests") or []]

        def _persist_mind_setting(changes: dict) -> None:
            try:
                update_config(changes)
            except Exception:
                logger.warning("mind setting not written to protagine.yaml", exc_info=True)

        # One factory for this Mind and the benchmark arm's (protagine.mind.factory): every store the
        # faculties read is taken from the host router module the stores above were set on.
        mind = build_mind(
            _host_for_mind, config=_mind_cfg.get("mind") or {}, store=_mind_store, state_dir=state_dir,
            ledger=get_turn_idempotency_ledger(state_dir), owner_id=get_owner_contact_id(),
            feedback=_host_for_mind._feedback_store, expectations=_host_for_mind._expectations,
            interests=_mind_interests, persist=_persist_mind_setting,
            heartbeat=lambda: telemetry.touch("last_tick_at"))
        set_mind(mind)
        mind.start()
        logger.info("Mind tick started (autonomy=%s, enabled=%s, owner=%s)",
                    mind.level, mind.enabled, "set" if mind.owner_id else "unset")
    except Exception as exc:
        logger.warning("Mind init failed: %s", exc)

    # --- 22b. Situation spine (P6, migration-gated) ---
    _situation_wiring = None
    try:
        _situation_wiring = _attach_situation_spine(state_dir=state_dir)
        if _situation_wiring is not None:
            logger.info(
                "Situation spine attached (db=%s, mode=%s, initial=%s)",
                state_dir / "protagine-situation.db",
                _situation_wiring["mode"],
                _situation_wiring["initial_status"],
            )
    except Exception as exc:
        logger.error("Situation spine attachment failed closed: %s", exc)

    # --- 22d. Exploration sandbox (gated isolated execution, item 6) ---
    try:
        from protagine.sandbox import SandboxManager, sandbox_mode
        from protagine.api.routers.host import set_sandbox
        _sandbox_mgr = SandboxManager(self_model=_sm_for_directed)
        set_sandbox(_sandbox_mgr)
        logger.info("SandboxManager initialized (mode=%s, backend=%s)",
                    sandbox_mode(), _sandbox_mgr.backend_name())
    except Exception as exc:
        logger.warning("SandboxManager init failed: %s", exc)

    # --- 22e. Connector framework (read-only pull senses, item 2) ---
    try:
        from protagine.connectors import (
            ConnectorManager, connectors_mode,
        )
        from protagine.api.routers.host import set_connector_manager
        _conn_mgr = ConnectorManager(
            observation_store=locals().get("observation_store"),
            self_model=_sm_for_directed,
        )
        n_conn = _conn_mgr.register_default_connectors()
        set_connector_manager(_conn_mgr)
        logger.info("ConnectorManager initialized (mode=%s, %d connector(s) enabled)",
                    connectors_mode(), n_conn)
    except Exception as exc:
        logger.warning("ConnectorManager init failed: %s", exc)

    # Session report store (cross-session context bridge)
    from protagine.sessions.reports import SessionReportStore
    session_report_store = SessionReportStore()
    set_session_report_store(session_report_store)
    logger.info("SessionReportStore initialized")

    logger.info("Sidecar capabilities: %s", supported_capabilities())

    source_claim_task = None
    claims_enabled = os.environ.get("PROTAGINE_SOURCE_CLAIMS", "on").strip().lower() in {"on", "1", "true"}
    from protagine.vector import get_store as source_vector_store
    if claims_enabled or source_vector_store() is not None:
        from protagine.turns import get_turn_idempotency_ledger
        from protagine.beliefs.source_projection import run_source_claim_worker
        from protagine.api.routers import host as source_claim_host
        source_claim_task = asyncio.create_task(run_source_claim_worker(
            get_turn_idempotency_ledger(state_dir), lambda: source_claim_host._llm_router,
            claims_enabled=claims_enabled,
            commitments_provider=lambda: source_claim_host._commitment_store))
    yield

    if source_claim_task is not None:
        source_claim_task.cancel()
        try:
            await source_claim_task
        except asyncio.CancelledError:
            pass

    # Shutdown — close connections
    # Stop the mind tick before any store it uses is closed: its in-flight
    # tick must finish.
    try:
        from protagine.api.routers.mind import get_mind as _get_mind_for_shutdown, set_mind as _set_mind
        _running_mind = _get_mind_for_shutdown()
        if _running_mind is not None:
            await _running_mind.stop()
        _set_mind(None)
    except Exception:
        logger.warning("Mind shutdown failed")
    set_llm_router(None)
    set_embedder(None)
    set_goals_store(None)
    if contacts_store is not None:
        try:
            await contacts_store.close()
        except Exception:
            logger.debug("ContactStore close failed", exc_info=True)
    set_contacts_store(None)
    set_briefings_engine(None)
    try:
        from protagine.api.routers.host import (
            set_benchmark as _set_benchmark,
            set_learning_feedback_store as _set_learning_feedback_store,
        )
        _set_benchmark(None)
        _set_learning_feedback_store(None)
    except Exception:
        logger.debug("learning feedback shutdown failed", exc_info=True)
    set_research_pipeline(None)
    set_commitment_store(None)
    set_affect_store(None)
    set_facts_store(None)
    set_pattern_store(None)
    if channel_store is not None:
        try:
            channel_store.close()
        except Exception:
            logger.debug("ChannelStore close failed", exc_info=True)
    try:
        from protagine.channels.router import set_channel_store as _set_ch_store
        _set_ch_store(None)
    except Exception:
        pass
    set_secrets_manager(None)
    set_session_store(None)
    set_session_report_store(None)
    set_situation_spine(None, None)
    try:
        situation_wiring = locals().get("_situation_wiring")
        if situation_wiring is not None:
            situation_wiring["store"].close()
    except Exception:
        logger.debug("situation spine shutdown failed", exc_info=True)
    set_session_store(None)
    # Multi-Agent cleanup
    set_agent_store(None)
    set_invite_store(None)
    set_initiative_store(None)
    set_assignment_engine(None)
    set_websocket_manager(None)
    try:
        from protagine.api.routers.observations import set_observation_store
        set_observation_store(None)
    except Exception:
        pass
    logger.info("Sidecar shutdown complete")


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    from protagine.runtime_logging import RequestLogTiming, configure_runtime_logging
    if os.environ.get('PROTAGINE_RUNTIME_LOGGING') == '1':
        configure_runtime_logging(redirect_stdio=True)
    app = FastAPI(
        title="Protagine",
        version="0.1.0",
        lifespan=lifespan,
    )

    # API authentication (skips health/docs; loopback dev mode without a key).
    from protagine.api.auth import configured_api_key
    from protagine.api.middleware import ApiKeyMiddleware, BodySizeLimitMiddleware

    # Body-size cap runs before auth so oversized payloads are rejected with
    # 413 regardless of the auth state.
    try:
        max_body = int(os.environ.get("PROTAGINE_MAX_BODY_BYTES", "") or 10 * 1024 * 1024)
    except ValueError:
        max_body = 10 * 1024 * 1024
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_body)

    api_key = configured_api_key()
    app.add_middleware(ApiKeyMiddleware, api_key=api_key)
    if api_key:
        logger.info("API key authentication enabled")
    else:
        logger.warning("No API key configured; serving loopback clients only (dev mode)")

    from protagine.api.errors import install_exception_handlers
    install_exception_handlers(app)
    app.include_router(host_router)
    app.include_router(host_v2_router)
    from protagine.mind.factory import mind_routers
    for mind_route in mind_routers():   # the same list the benchmark's mind arm mounts
        app.include_router(mind_route)
    from protagine.api.routers import executions as executions_router
    app.include_router(executions_router.router)
    from protagine.api.routers import commitment_work as commitment_work_router
    app.include_router(commitment_work_router.router)
    from protagine.api.routers import social_state as social_state_router
    app.include_router(social_state_router.router)
    from protagine.api.routers import temporal_followups as temporal_followups_router
    app.include_router(temporal_followups_router.router)
    from protagine.api.routers import transport as transport_router
    app.include_router(transport_router.router)
    from protagine.api.routers import followup_plans
    app.include_router(followup_plans.router)

    # Channel registration router
    from protagine.channels.router import router as channels_router
    app.include_router(channels_router)

    # Observations router (v0.16.0) — agent-as-sensor ingestion
    from protagine.api.routers import observations as observations_router
    app.include_router(observations_router.router)
    from protagine.api.routers import mining as mining_router
    app.include_router(mining_router.router)

    # MCP streamable HTTP endpoint
    try:
        from protagine.mcp.server import create_server
        mcp_server = create_server()
        # Mount MCP ASGI app at /mcp
        mcp_asgi = mcp_server.streamable_http_app()
        app.mount("/mcp", mcp_asgi)
        logger.info("MCP endpoint mounted at /mcp (streamable HTTP)")
    except ImportError:
        logger.debug("MCP SDK not installed — /mcp endpoint not available (install protagine[mcp])")
    except Exception as exc:
        logger.warning("Could not mount MCP endpoint: %s", exc)

    app.add_middleware(RequestLogTiming)
    return app


# Uvicorn entry point
app = create_app()
