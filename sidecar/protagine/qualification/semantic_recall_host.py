"""Wire the shipped retrieval implementation into an owned native fixture host."""
import asyncio
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, replace
import hashlib
import json
import os
import time
from unittest.mock import patch


@contextmanager
def setup(app, state, inputs, config, evidence):
    import httpx
    from protagine.api.routers import host
    from protagine.memory import search
    from protagine.memory.recall import calibration_fingerprint, provider_calibration_metadata
    from protagine.turns import get_turn_idempotency_ledger
    from protagine.turns.source_vectors import SourceVectors
    from protagine.vector import get_store, get_pipeline, set_store, set_pipeline
    from protagine.vector.collections import Collection
    from protagine.vector.config import EmbeddingConfig
    from protagine.vector.embedder import EmbeddingPipeline, make_provider
    from protagine.vector.indexes import IndexCatalog
    from protagine.vector.reranker import OpenAIAPIRerankerProvider
    from protagine.vector.store import VectorStore

    recipe = inputs['retrieval']
    embedding, ranking = recipe['embedding'], recipe['reranker']
    fault = inputs['retrieval_fault']
    ledger = get_turn_idempotency_ledger(state / 'memory-state')
    provider = make_provider(EmbeddingConfig(provider='openai_api', model_id=embedding['model'],
        dimensions=embedding['dimensions'], revision=embedding.get('revision')))
    provider.configure(embedding['base_url'], '')
    pipeline = EmbeddingPipeline(provider)
    ranker = OpenAIAPIRerankerProvider(ranking['model'])
    ranker.configure(ranking['base_url'], '', prompt_style=ranking.get('prompt_style', ''))
    prior_store, prior_pipeline, prior_ranker = get_store(), get_pipeline(), host._reranker
    store = None
    evidence.update(boundary='native_host_canonical_semantic_recollection',
        fault=fault, collections=[], selections=[], semantic_searches=[], lexical_searches=[], reranks=[],
        fault_requests=0, initialized=False)

    async def initialize():
        nonlocal store
        with ledger._connect() as db:
            evidence['canonical_sources'] = {row['turn_id']: row['scope']
                for row in db.execute('SELECT turn_id,scope FROM turn_sources')}
        await pipeline.warmup()
        store = VectorStore(str(state / 'semantic-lancedb'), identity=pipeline.index_identity,
                            catalog=IndexCatalog(ledger))
        await store.connect(pipeline.dimensions)
        await store.ensure_collections(pipeline.dimensions)
        vectors = SourceVectors(ledger, store, pipeline)
        vectors.backfill()
        # The empty lagging index still has the real scope projection schema.
        await vectors._table(store.catalog.active())
        if fault != 'index_lag':
            for _ in range(32):
                if not await vectors.process_one():
                    break
            else:
                raise RuntimeError('Fixture source projection exceeded its bounded drain')
            with ledger._connect() as db:
                if db.execute("SELECT count(*) FROM source_vector_jobs WHERE status<>'complete'").fetchone()[0]:
                    raise RuntimeError('Fixture source indexing did not complete')
        evidence['index_before_fault'] = vectors.status(inputs['contact_id'])
        table = await store._table(Collection.CONVERSATIONS, generation=store.catalog.active())
        cached = await table.query().select(['id', 'metadata']).to_list()
        erased = set(inputs.get('erase_after_index', []))
        evidence['stale_rows_retained'] = sum(json.loads(row['metadata']).get('source_turn_id') in erased for row in cached)
        if erased:
            ledger.erase_sources(contact_id=inputs['contact_id'], turn_ids=sorted(erased))
        if fault == 'cached_text_corruption':
            await table.update(updates={'text': 'CORRUPTED-CACHE-NOT-CANONICAL'})
        if fault == 'identity_mismatch':
            # Declare a changed query configuration while retaining the old index.
            # The shipped compatibility check, not a fabricated search result,
            # must reject that mismatch and leave lexical retrieval usable.
            store.identity = replace(store.identity, declared_revision='declared-fixture-mismatch')
        evidence['embedding_identity'] = asdict(pipeline.index_identity)
        evidence['index_after_fault'] = vectors.status(inputs['contact_id'])
        evidence['erased_sources'] = {source: ledger.is_source_erased(source, inputs['contact_id']) for source in erased}
        evidence['initialized'] = True

    try:
        asyncio.run(initialize())
        set_store(store)
        set_pipeline(pipeline)
        host.set_reranker(ranker)
        environment = {'PROTAGINE_EMBED_MODEL': embedding['model'],
            'PROTAGINE_EMBED_DIMS': str(embedding['dimensions']),
            'PROTAGINE_RERANKER_REVISION': ranking.get('revision') or 'unverified',
            'PROTAGINE_RECALL_INDEX_GENERATION': store.catalog.active()['id'],
            'PROTAGINE_RECALL_RERANK': 'on',
            'PROTAGINE_RECALL_RERANK_MIN_SCORE': str(ranking['cutoff']),
            'PROTAGINE_RECALL_RERANK_TIMEOUT_MS': str(ranking.get('timeout_ms', 1200))}
        with ExitStack() as resources:
            resources.enter_context(patch.dict(os.environ, environment))
            stamp = provider_calibration_metadata(ranker)
            resources.enter_context(patch.dict(os.environ, {
                'PROTAGINE_RECALL_RERANK_CALIBRATION': calibration_fingerprint(stamp)}))
            evidence['reranker_configuration'] = {k: v for k, v in stamp.items() if k != 'endpoint'}
            evidence['reranker_cutoff'] = ranking['cutoff']
            original_collect, original_select = search.collect_sources, search.select_memory
            original_search, original_rank = SourceVectors.search, ranker.rerank
            original_lexical = ledger.search_sources
            original_send = httpx.AsyncClient.send

            def lexical(*args, **kwargs):
                found = original_lexical(*args, **kwargs)
                evidence['lexical_searches'].append({'source_ids': sorted({row['turn_id'] for row in found})})
                return found

            async def collect(*args, **kwargs):
                found = await original_collect(*args, **kwargs)
                evidence['collections'].append({'semantic': found.semantic,
                    'source_ids': sorted({row['turn_id'] for row in found.hits}),
                    'contact_id': found.contact_id})
                return found

            async def select(*args, **kwargs):
                packet = await original_select(*args, **kwargs)
                evidence['selections'].append({'source_ids': sorted({row['source_id'] for row in packet.source_refs}),
                    'content_sha256': hashlib.sha256(packet.content.encode()).hexdigest(),
                    'characters': len(packet.content), 'retrieval': packet.retrieval,
                    'rerank_statuses': sorted({str(row.get('rerank_status')) for row in packet.selected}),
                    'conflict_present': any(row.get('claim_status') == 'unresolved_conflict'
                        for row in packet.selected)})
                return packet

            async def semantic(instance, query, **kwargs):
                row = {'source_ids': [], 'status': 'pending'}
                evidence['semantic_searches'].append(row)
                try:
                    sources, media = await original_search(instance, query, **kwargs)
                    row.update(status='returned', source_ids=sorted({hit['turn_id'] for hit in sources}))
                    return sources, media
                except Exception as exc:
                    row.update(status='failed', error_type=type(exc).__name__)
                    raise

            async def rerank(*args, **kwargs):
                started = time.monotonic()
                row = {'status': 'pending'}
                evidence['reranks'].append(row)
                try:
                    value = await original_rank(*args, **kwargs)
                    row.update(status='returned', scores=[{'index': item.index, 'score': item.score} for item in value])
                    return value
                except BaseException as exc:
                    row.update(status='failed', error_type=type(exc).__name__)
                    raise
                finally:
                    row['elapsed_ms'] = round((time.monotonic() - started) * 1000, 3)

            async def send(client, request, *args, **kwargs):
                target = embedding['base_url'] if fault == 'embedding_unavailable' else ranking['base_url']
                suffix = '/embeddings' if fault == 'embedding_unavailable' else '/rerank'
                if (fault in {'embedding_unavailable', 'reranker_unavailable'}
                        and request.method == 'POST' and request.url.path.endswith(suffix)
                        and str(request.url).startswith(target.rstrip('/') + '/')):
                    evidence['fault_requests'] += 1
                    # Only this owned process receives a declared synthetic HTTP
                    # error. No external service, routing or traffic is changed.
                    return httpx.Response(503, request=request, json={'error': 'declared_fixture_unavailable'})
                return await original_send(client, request, *args, **kwargs)

            resources.enter_context(patch.object(search, 'collect_sources', collect))
            resources.enter_context(patch.object(search, 'select_memory', select))
            resources.enter_context(patch.object(SourceVectors, 'search', semantic))
            resources.enter_context(patch.object(ledger, 'search_sources', lexical))
            resources.enter_context(patch.object(ranker, 'rerank', rerank))
            resources.enter_context(patch.object(httpx.AsyncClient, 'send', send))
            yield
    finally:
        set_store(prior_store)
        set_pipeline(prior_pipeline)
        host.set_reranker(prior_ranker)
        if store is not None:
            asyncio.run(store.close())
        asyncio.run(pipeline.close())
