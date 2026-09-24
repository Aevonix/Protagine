"""Canonical collection and selection shared by recall and explicit search."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import os
from typing import Any

from .recall import source_candidates, contact_fact_candidates, pack_memory_context

logger = logging.getLogger(__name__)

# The packet rides in the user turn and Hermes replays it as history on every later turn, so the
# default budget is what a few excerpts need, not the 6,000 characters the first cut allowed.
DEFAULT_RECALL_CHARS = 4000


@dataclass
class CollectedSources:
    ledger: Any
    contact_id: str
    session_id: str
    watermark: int | None
    hits: list[dict[str, Any]] = field(default_factory=list)
    media: list[dict[str, Any]] = field(default_factory=list)
    semantic: str = 'unavailable'
    lexical_hits: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class MemoryPacket:
    selected: list[dict[str, Any]]
    content: str
    source_refs: list[dict[str, str]]
    watermark: int | None
    retrieval: dict[str, str]
    annotation_checks: list[dict[str, Any]]

    def public(self) -> dict[str, Any]:
        return {'content': self.content, 'count': len(self.selected),
                'source_refs': self.source_refs, 'watermark': self.watermark,
                'retrieval': self.retrieval, 'annotation_checks': self.annotation_checks}


async def collect_sources(ledger, *, query: str, contact_id: str, session_id: str,
                          vector_store=None, embedding_pipeline=None) -> CollectedSources:
    """Read exact scoped sources; optional semantic failure keeps lexical evidence."""
    from protagine.turns.source_vectors import SourceVectors, merge_source_hits
    collected = CollectedSources(ledger, contact_id, session_id,
                                ledger.erasure_watermark(contact_id) if ledger is not None else None)
    if ledger is None or not query.strip():
        return collected
    # The ledger opens its own connection; large lexical scans must not hold
    # the HTTP event loop while transport intake waits for its response.
    collected.hits = await asyncio.to_thread(
        ledger.search_sources, query, contact_id=contact_id, session_id=session_id, limit=10)
    collected.lexical_hits = list(collected.hits)
    try:
        semantic_hits, collected.media = await SourceVectors(
            ledger, vector_store, embedding_pipeline).search(
                query, contact_id=contact_id, session_id=session_id, limit=15)
        collected.hits = merge_source_hits(collected.hits, semantic_hits)
        if vector_store is not None and embedding_pipeline is not None and getattr(vector_store, 'catalog', None) is not None:
            collected.semantic = 'ready'
    except Exception as exc:
        collected.semantic = 'failed'
        logger.debug('Source semantic recall unavailable (%s); retaining lexical evidence', type(exc).__name__)
    return collected


async def select_memory(collected: CollectedSources, *, query: str, selector,
                        contact_facts=None, contact_facts_allowed=False,
                        timezone_name=None, current_work_available=False, limit=5,
                        session_history=None, now=None) -> MemoryPacket:
    """Apply the existing projections, corrections, ranking and shared budget once.

    The caller supplies an authenticated audience and an optional projected
    contact fact view. All source candidates come from the canonical ledger.
    ``session_history="intact"`` says the host still shows this session's own
    turns verbatim: quoting them back is the one recall that can add nothing,
    so those quotations and conversation pairs are left out (derived claims,
    media and other sessions' evidence stay). ``now`` pins the time query; the
    recall benchmark replays fixtures at their recorded date.
    """
    from protagine.beliefs.source_time import interpret_time_query, filter_unstructured
    from protagine.util import temporal
    ledger = collected.ledger
    scope = {'contact_id': collected.contact_id, 'session_id': collected.session_id}
    beliefs = []
    quotations = source_candidates(collected.hits)
    time_query = interpret_time_query(query, now=now or temporal.now_utc(), timezone_name=timezone_name)
    if ledger is not None:
        from protagine.beliefs.source_projection import SourceClaimProjection
        from protagine.memory.selection import current_work_query
        from protagine.turns.media import SourceMedia
        beliefs, quotations = SourceClaimProjection(ledger).prepare_context(
            beliefs, collected.hits, **scope, time_query=time_query,
            classify_work_replies=current_work_available and current_work_query(query),
            include_conversation_inputs=True)
        media_store = SourceMedia(ledger)
        media_hits = media_store.search(query, **scope)
        quotations.extend(filter_unstructured(
            media_store.named_locators(query, collected.hits, **scope), time_query))
        media_by_id = {row['id']: row for row in media_hits + collected.media}
        quotations.extend(filter_unstructured(list(media_by_id.values()), time_query))
    else:
        beliefs = filter_unstructured(beliefs, time_query)
    if session_history == 'intact' and collected.session_id:
        quotations = [row for row in quotations
                      if not (row.get('session_id') == collected.session_id
                              and row.get('kind') in ('source_quote', 'conversation_pair'))]
    facts_status = 'not_in_scope' if not contact_facts_allowed else 'unavailable'
    if contact_facts_allowed and contact_facts is not None:
        try:
            fact_result = contact_facts.list_facts(contact_id=collected.contact_id, limit=512)
            facts = fact_result if isinstance(fact_result, list) else fact_result.get('facts', [])
            quotations.extend(filter_unstructured(contact_fact_candidates(query, facts), time_query))
            facts_status = 'ready'
        except Exception as exc:
            logger.warning('Contact fact candidates unavailable (%s)', type(exc).__name__)
    try:
        max_chars = int(os.environ.get('PROTAGINE_RECALL_CONTEXT_MAX_CHARS', str(DEFAULT_RECALL_CHARS)))
    except (TypeError, ValueError):
        max_chars = DEFAULT_RECALL_CHARS
    max_chars = max(0, min(max_chars, 24000))
    if ledger is not None:
        from protagine.turns.source_annotations import expand, current_candidates
        beliefs = expand(ledger, beliefs, **scope)
        quotations = expand(ledger, quotations, covered=beliefs, **scope)
        # Lexical hits predate the optional embedding await. A removed source
        # or message has no current annotation membership; do not emit its old
        # text with empty references after expansion skips the missing source.
        quotations = [row for row in quotations if not row.get('source_turn_id')
                      or row['source_turn_id'] in {
                          ref['source_id'] for ref in row.get('_annotation_source_refs', [])}]
        quotations = [row for row in quotations if row.get('kind') != 'media_locator'
                      or {key: row['source_read'][key] for key in ('source_id', 'source_version')}
                      in row.get('_annotation_source_refs', [])]
        # Preserve the independent lexical order through claim/correction
        # expansion. Only exact, canonically checked message membership can
        # carry it; another message in the same source does not inherit rank.
        lexical_ranks = {}
        for rank, hit in enumerate(collected.lexical_hits):
            lexical_ranks.setdefault((hit['turn_id'], hit.get('source_message_hash')), rank)
        for row in beliefs + quotations:
            ranks = [lexical_ranks[(source_id, message_hash)]
                     for source_id, hashes in row.get('_annotation_message_hashes', {}).items()
                     for message_hash in hashes if (source_id, message_hash) in lexical_ranks]
            if ranks:
                row['_lexical_rank'] = min(ranks)
    selected, content = await selector.select_context(query, beliefs, quotations, limit=limit,
        current_work_available=current_work_available, max_chars=max_chars)
    if ledger is not None:
        retained = current_candidates(ledger, selected, **scope)
        if len(retained) != len(selected):
            selected, content = pack_memory_context(retained, limit=limit, max_chars=max_chars)
    source_ids = []
    for memory in selected:
        source_ids.extend(memory.get('source_turn_ids') or [])
        if memory.get('source_turn_id'):
            source_ids.append(memory['source_turn_id'])
        uri = str(memory.get('source_uri') or '')
        if uri.startswith('turn:'):
            source_ids.append(uri[5:])
    refs = ledger.source_references(source_ids, **scope) if ledger is not None else []
    refs_by_id = {ref['source_id']: ref for ref in refs}
    for memory in selected:
        # Keep the checked exact correction revision, never rebind old text to
        # a surviving version after an erasure.
        for ref in memory.get('_annotation_source_refs', []):
            refs_by_id[ref['source_id']] = ref
    checks = [{'source_refs': row['_annotation_source_refs'],
               'message_hashes': row['_annotation_message_hashes'],
               'annotation_ids': sorted(row['_annotation_ids'])}
              for row in selected if row.get('_annotation_source_refs')]
    return MemoryPacket(selected, content, list(refs_by_id.values()), collected.watermark,
                        {'semantic': collected.semantic, 'contact_facts': facts_status}, checks)
