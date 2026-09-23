"""Copyable canonical media references survive real context packing and reading."""
import base64
from copy import deepcopy
import importlib
import json
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from protagine.memory.recall import pack_memory_context, render_memory_context
from protagine.turns import TurnIdempotencyLedger
from protagine.turns.media import SourceMedia
from protagine.turns.source_annotations import expand, current_candidates
from protagine.turns.source_read import read
from test_native_request_erasure import freshness_response
from test_recall_source_presentation import rendered_rows
from test_source_media import Vision, image_bytes, message
from test_turn_source_evidence import source_app


@pytest_asyncio.fixture
async def media_source(source_app, tmp_path, monkeypatch):
    monkeypatch.setenv('PROTAGINE_RECALL_RERANK', 'off')
    ledger = TurnIdempotencyLedger(tmp_path/'turn-idempotency.db')
    ledger.record_source('drawing-source', contact_id='person', session_id='earlier-text',
                         messages=[message()], derive_claims=False)
    media = SourceMedia(ledger)
    assert await media.process_one(Vision())
    ref, = ledger.source_references(['drawing-source'], contact_id='person', session_id='later')
    return source_app, ledger, media, ref


def annotate(ledger, ref):
    return ledger.append_source_annotation(contact_id='person', session_id='later', annotation_id='drawing-note',
        **ref, excerpt='Please retain this reference image.',
        correction='The drawing illustrates shapes only; it is not a camera observation.', author_principal='operator')


@pytest.mark.asyncio
async def test_media_pair_is_charged_to_budget_and_corrections_remain_indivisible(media_source):
    _, ledger, media, ref = media_source
    candidates = media.search('blue circle', contact_id='person', session_id='later')
    rows = expand(ledger, candidates, contact_id='person', session_id='later')
    before = deepcopy(rows)
    selected, complete = pack_memory_context(rows)
    assert rows == before and len(selected) == 1
    assert json.dumps(ref['source_version']) in complete
    assert pack_memory_context(rows, max_chars=len(complete))[1] == complete
    selected, shortened = pack_memory_context(rows, max_chars=len(complete)-1)
    # For this short caption, adding the existing truncation notice would leave
    # fewer than the minimum useful80 characters. Omit it, never drop the pair
    # from budget accounting to make the old metadata-only size appear to fit.
    assert (selected, shortened) == ([], '')
    note = annotate(ledger, ref)
    rows = expand(ledger, candidates, contact_id='person', session_id='later')
    _, complete = pack_memory_context(rows)
    assert 'not a camera observation' in complete
    assert pack_memory_context(rows, max_chars=len(complete)-1) == ([], '')
    ledger.erase_sources(contact_id='person', turn_ids=[note['source_id']])
    assert current_candidates(ledger, rows, contact_id='person', session_id='later') == []


def test_unchecked_or_unrelated_media_metadata_does_not_invent_canonical_pair():
    row = {'id': 'media:example', 'kind': 'media_description', 'source_turn_id': 'drawing',
           'content': 'A neutral drawing.'}
    for refs in ([], [{'source_id': 'different-source', 'source_version': '1'*64}]):
        rendered, = rendered_rows(render_memory_context([dict(row, _annotation_source_refs=refs)]))
        assert 'source_id' not in rendered and 'source_version' not in rendered


