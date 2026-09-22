"""Canonical facts remain useful while projection or reranking is unavailable.

SQLite and Lance are real; controlled embeddings isolate candidate selection
from model quality. These fixtures reproduce a fallback failure mechanism, not
the unavailable intermediate trace of any particular production request.
"""
import logging

import pytest

from protagine.memory.search import collect_sources, select_memory
from protagine.memory.selection import RecallSelector
from test_source_vectors import setup, drain


QUERY = 'Where is the copperbell hydrofoil parcel now?'
ORIGINAL = 'The copperbell hydrofoil parcel is on the amber shelf.'
CORRECTION = 'Correction: the copperbell hydrofoil parcel is now on the violet shelf, replacing the amber shelf.'


async def corpus(tmp_path):
    ledger, store, pipeline, projection = await setup(tmp_path)
    for i in range(5):
        ledger.record_source(f'archive-{i}', contact_id='person', session_id='archive',
            messages=[{'role': 'user', 'content': f'Archive {i}. ' + (
                'The hydrofoil parcel archive describes past deliveries and paperwork at the receiving office. ' * 18)}],
            derive_claims=False)
    await drain(projection)
    # Ordinary capture commits the source and FTS together. New projections
    # have not been processed yet; cross-session recall must still be useful.
    for name, text in [('original', ORIGINAL), ('correction', CORRECTION)]:
        ledger.record_source(name, contact_id='person', session_id='capture',
            messages=[{'role': 'user', 'content': text}], derive_claims=False)
    return ledger, store, pipeline


async def packet(ledger, store, pipeline, selector, *, query=QUERY, limit=5):
    collected = await collect_sources(ledger, query=query, contact_id='person',
        session_id='fresh-session', vector_store=store, embedding_pipeline=pipeline)
    result = await select_memory(collected, query=query, selector=selector,
        timezone_name='UTC', limit=limit)
    return collected, result


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['timeout', 'missing', 'incomplete', 'off'])
async def test_pending_projection_correction_survives_unavailable_reranking(tmp_path, monkeypatch, caplog, failure):
    monkeypatch.setenv('PROTAGINE_RECALL_RERANK', 'off' if failure == 'off' else 'on')
    monkeypatch.delenv('PROTAGINE_RECALL_RERANK_MIN_SCORE', raising=False)
    ledger, store, pipeline = await corpus(tmp_path)
    lexical = ledger.search_sources(QUERY, contact_id='person', session_id='fresh-session', limit=10)
    assert [row['turn_id'] for row in lexical[:2]] == ['correction', 'original']

    async def unavailable(*args, **kwargs):
        if failure == 'timeout':
            raise TimeoutError()
        return []  # Missing scores must not be treated as valid abstention.

    with caplog.at_level(logging.WARNING):
        collected, result = await packet(ledger, store, pipeline,
            RecallSelector(None if failure == 'missing' else unavailable))
    assert all(row['turn_id'].startswith('archive-') for row in collected.hits[:5])
    assert CORRECTION in result.content
    assert ORIGINAL in result.content
    assert {'correction', 'original'} <= {r['source_id'] for r in result.source_refs}
    assert len(result.content) <= 6000
    if failure != 'off':
        assert all(row['rerank_status'] == 'unavailable' for row in result.selected)
    if failure == 'timeout':
        assert any('TimeoutError' in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_fallback_retains_exact_annotation_and_source_versions(tmp_path, monkeypatch):
    monkeypatch.setenv('PROTAGINE_RECALL_RERANK', 'on')
    ledger, store, pipeline = await corpus(tmp_path)
    original_ref = ledger.source_references(['original'], contact_id='person', session_id='fresh-session')[0]
    note = ledger.append_source_annotation(contact_id='person', session_id='fresh-session',
        annotation_id='owner-correction', **original_ref, excerpt='amber shelf',
        correction='The parcel is on the violet shelf now.', author_principal='person')
    _, result = await packet(ledger, store, pipeline, RecallSelector())
    assert 'The parcel is on the violet shelf now.' in result.content
    assert original_ref in result.source_refs
    assert note['source_id'] in {r['source_id'] for r in result.source_refs}
    assert any(note['source_id'] in row['_annotation_ids'] for row in result.selected)
    current = ledger.source_references([ref['source_id'] for ref in result.source_refs],
        contact_id='person', session_id='fresh-session')
    assert {tuple(sorted(ref.items())) for ref in result.source_refs} == {
        tuple(sorted(ref.items())) for ref in current}


@pytest.mark.asyncio
async def test_successful_reranker_can_prefer_semantic_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv('PROTAGINE_RECALL_RERANK', 'on')
    monkeypatch.delenv('PROTAGINE_RECALL_RERANK_MIN_SCORE', raising=False)
    ledger, store, pipeline = await corpus(tmp_path)

    async def scores(query, documents, top_k):
        return [{'index': i, 'score': 1.0 if doc.startswith('Archive 0.') else 0.0}
                for i, doc in enumerate(documents)]

    _, result = await packet(ledger, store, pipeline, RecallSelector(scores), limit=1)
    assert {r['source_id'] for r in result.source_refs} == {'archive-0'}
    assert result.selected[0]['rerank_status'] == 'scored'


@pytest.mark.asyncio
async def test_shadow_mode_retains_original_order_for_comparison(tmp_path, monkeypatch):
    monkeypatch.setenv('PROTAGINE_RECALL_RERANK', 'shadow')
    monkeypatch.delenv('PROTAGINE_RECALL_RERANK_MIN_SCORE', raising=False)
    ledger, store, pipeline = await corpus(tmp_path)

    async def scores(query, documents, top_k):
        return [{'index': i, 'score': 1.0 if 'violet shelf' in doc else 0.0}
                for i, doc in enumerate(documents)]

    collected, result = await packet(ledger, store, pipeline, RecallSelector(scores), limit=1)
    assert result.source_refs[0]['source_id'] == collected.hits[0]['turn_id']
    assert 'violet shelf' not in result.content


@pytest.mark.asyncio
async def test_unavailable_reranker_keeps_semantic_only_recall(tmp_path, monkeypatch):
    monkeypatch.setenv('PROTAGINE_RECALL_RERANK', 'on')
    ledger, store, pipeline = await corpus(tmp_path)
    query = 'vessel departure identifier'
    assert ledger.search_sources(query, contact_id='person', session_id='fresh-session') == []
    _, result = await packet(ledger, store, pipeline, RecallSelector(), query=query)
    assert result.source_refs and result.retrieval['semantic'] == 'ready'
    assert all(ref['source_id'].startswith('archive-') for ref in result.source_refs)


@pytest.mark.asyncio
async def test_weak_lexical_matches_do_not_starve_useful_paraphrase(tmp_path, monkeypatch):
    monkeypatch.setenv('PROTAGINE_RECALL_RERANK', 'on')
    ledger, store, pipeline, projection = await setup(tmp_path)
    answer = 'The hydrofoil arrives Monday at eleven.'
    ledger.record_source('arrival', contact_id='person', session_id='earlier',
        messages=[{'role': 'user', 'content': answer}], derive_claims=False)
    await drain(projection)
    # Generic keyword matches are not necessarily the answer. Their length
    # makes putting all lexical results first a measurable budget regression.
    for i in range(5):
        ledger.record_source(f'paperwork-{i}', contact_id='person', session_id='archive',
            messages=[{'role': 'user', 'content': f'Paperwork {i}. ' + (
                'A parcel archive records the receiving forms and labeling instructions for past deliveries. ' * 18)}],
            derive_claims=False)
    query = 'parcel vessel arrival schedule'
    lexical = ledger.search_sources(query, contact_id='person', session_id='fresh-session', limit=10)
    assert lexical and all(row['turn_id'].startswith('paperwork-') for row in lexical)
    _, result = await packet(ledger, store, pipeline, RecallSelector(), query=query)
    assert answer in result.content
    assert 'arrival' in {ref['source_id'] for ref in result.source_refs}
    assert len(result.content) <= 6000
