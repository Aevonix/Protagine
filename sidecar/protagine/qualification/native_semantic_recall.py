"""Qualify native recollection without replacing retrieval or injecting answers."""
import hashlib
from pathlib import Path

from .native_memory import consume as memory_consume, assess as memory_assess
from .records import read, write_once


async def consume(inputs, context):
    try:
        return await memory_consume(inputs, context,
            worker=Path(__file__).with_name('native_semantic_worker.py'))
    finally:
        result = context.state_dir / 'native-result.json'
        if result.exists():
            value = read(result)
            write_once(context.state_dir.parent / 'semantic-private-diagnostic.json', {
                'stage': value.get('stage'), 'turn': value.get('turn'),
                'private_error_traceback': value.get('private_error_traceback')})


def assess(observed, oracle):
    checks = memory_assess(observed, oracle)
    # Session-scoped checkpoints intentionally do not become person claims.
    # Grade their canonical presence/scope, and only demand formation for the
    # source kinds which the shipped writer is intended to promote or reject.
    checks.pop('all_sources_formed_or_rejected')
    evidence = observed.get('effects', {}).get('semantic_recall', {})
    jobs = observed.get('effects', {}).get('formation', {}).get('jobs', [])
    eligible = {identity for identity, scope in oracle['source_scopes'].items() if scope == 'person'}
    checks['eligible_sources_formed_or_rejected'] = (
        {row.get('turn_id') for row in jobs} == eligible
        and all(row.get('status') == 'complete' for row in jobs))
    checks['canonical_sources_have_declared_scope'] = evidence.get('canonical_sources') == oracle['source_scopes']
    collections = evidence.get('collections', [])
    selections = evidence.get('selections', [])
    semantic_ids = {identity for row in evidence.get('semantic_searches', []) for identity in row['source_ids']}
    lexical_ids = {identity for row in evidence.get('lexical_searches', []) for identity in row['source_ids']}
    selected_ids = {identity for row in selections for identity in row['source_ids']}
    index = evidence.get('index_before_fault', {})
    checks.update(semantic_pipeline_initialized=evidence.get('initialized') is True,
        canonical_collection_observed=bool(collections), actual_selection_observed=bool(selections),
        expected_retrieval_state=bool(collections) and all(row['semantic'] == oracle['semantic_status'] for row in collections),
        pending_index_observed=index.get('pending_turns', -1) >= oracle['pending_minimum'],
        configured_fault_observed=evidence.get('fault') == oracle['fault'])
    if oracle['projected'] is not None:
        checks['expected_projection_count'] = index.get('projected_turns') == oracle['projected']
    for source in oracle['semantic_required']:
        checks['semantic_candidate.' + source] = source in semantic_ids
    for source in oracle['lexical_absent']:
        checks['semantic_adds_missing_lexical_source.' + source] = bool(evidence.get('lexical_searches')) and source not in lexical_ids
    for source in oracle['selected_required']:
        checks['selected_source.' + source] = source in selected_ids
    for source in oracle['erase_after_index']:
        checks['canonical_erasure.' + source] = evidence.get('erased_sources', {}).get(source) is True
        checks['erased_vector_not_selected.' + source] = source not in semantic_ids and source not in selected_ids
    if oracle['stale_rows_minimum']:
        checks['stale_derived_rows_exercised'] = evidence.get('stale_rows_retained', 0) >= oracle['stale_rows_minimum']
    if oracle['reranker_required']:
        expected = 'failed' if oracle['fault'] == 'reranker_unavailable' else 'returned'
        checks['actual_reranker_observed'] = any(row['status'] == expected for row in evidence.get('reranks', []))
    if oracle['fault'] in {'embedding_unavailable', 'reranker_unavailable'}:
        checks['declared_http_failure_exercised'] = evidence.get('fault_requests', 0) > 0
    if oracle['require_conflict']:
        checks['unresolved_conflict_preserved'] = any(row['conflict_present'] for row in selections)
    return checks


def implementation_identity():
    identity = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
        for name in ('native_semantic_recall.py', 'native_semantic_worker.py', 'semantic_recall_cases.py',
                     'semantic_recall_host.py', 'native_memory.py', 'native_memory_worker.py',
                     'native.py', 'native_worker.py')}
    for relative in ('memory/search.py', 'memory/selection.py', 'memory/recall.py',
                     'turns/source_vectors.py', 'vector/indexes.py', 'vector/store.py',
                     'vector/embedder.py', 'vector/openai_provider.py', 'vector/reranker.py'):
        identity['production/' + relative] = hashlib.sha256(
            (Path(__file__).resolve().parent.parent / relative).read_bytes()).hexdigest()
    return identity


CONSUMERS = {'native_semantic_recall': consume}
EVALUATORS = {'native_semantic_recall_outcomes': assess}
