"""Forgetting real Lance projections must not stall unrelated async requests."""
import asyncio
import json
import threading
import time

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pyarrow as pa
import pytest

from protagine.turns import TurnIdempotencyLedger
from protagine.vector import Collection
from protagine.vector.indexes import EmbeddingIdentity, IndexCatalog
from protagine.vector.store import VectorStore


@pytest.mark.asyncio
async def test_large_erasure_allows_unrelated_http_and_preserves_later_batches(tmp_path, monkeypatch, record_property):
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    catalog = IndexCatalog(ledger)
    identity = EmbeddingIdentity('fixture', 'fixture', 'known', 2)
    store = VectorStore(str(tmp_path / 'vectors'), identity=identity, catalog=catalog)
    await store.connect(2)
    await store.ensure_collections(2)
    for source in ('erased', 'retained'):
        ledger.record_source(source, contact_id='owner', session_id='source-session',
                             messages=[{'role': 'user', 'content': source + ' neutral source'}])

    table = await store._table(Collection.MEMORIES)
    # Matching rows in every batch, including an independently appended tail.
    rows = [{'id': f'row-{index:05}', 'text': 'Neutral projection', 'vector': [1., 0.],
             'metadata': json.dumps({'source_uri': 'turn:erased' if index % 2 else 'turn:retained'})}
            for index in range(4097)]
    await table.add(pa.Table.from_pylist(rows, schema=await table.schema()))
    await table.add(pa.Table.from_pylist([{
        'id': "erased'last", 'text': 'Neutral tail', 'vector': [1., 0.],
        'metadata': json.dumps({'source_uri': 'turn:erased'}),
    }], schema=await table.schema()))
    # A building generation and another collection contain the same
    # logical row IDs. Deletion counts stay unique by collection and ID.
    active = catalog.active()
    building = catalog.begin(identity)
    for generation in (active, building):
        db = await store._generation_db(generation)
        if Collection.CONVERSATIONS.value not in await db.table_names():
            await db.create_table(Collection.CONVERSATIONS.value, schema=await table.schema())
        other = await db.open_table(Collection.CONVERSATIONS.value)
        await other.add(pa.Table.from_pylist(rows[:2], schema=await other.schema()))

    ledger.erase_sources(contact_id='owner', turn_ids=['erased'])
    entered, release = threading.Event(), threading.Event()
    check = catalog.source_erased
    blocked_once = False

    def held_provenance(metadata):
        nonlocal blocked_once
        if not blocked_once:
            blocked_once = True
            entered.set()
            # A bounded stand-in for a slow disk read. Real canonical checking
            # follows it; no provenance result or Lance deletion is mocked.
            assert release.wait(5), 'unrelated request could not progress during provenance read'
        return check(metadata)

    monkeypatch.setattr(catalog, 'source_erased', held_provenance)
    app = FastAPI()

    @app.get('/unrelated')
    async def unrelated():
        return {'responsive': True}

    erase = asyncio.create_task(store.erase_source_projections(['erased', 'retained']))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), timeout=3)
        assert not erase.done()
        started = time.monotonic()
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await asyncio.wait_for(client.get('/unrelated'), timeout=1)
        assert response.json() == {'responsive': True}
        elapsed = time.monotonic() - started
        record_property('unrelated_http_ms_during_erasure', round(elapsed * 1000, 3))
        assert elapsed < 1
        assert not erase.done(), 'HTTP progressed only after the erasure finished'
    except BaseException:
        release.set()
        await asyncio.gather(erase, return_exceptions=True)
        await store.close()
        raise
    finally:
        release.set()
    try:
        assert await asyncio.wait_for(erase, timeout=30) == 2050
        # The pre-erasure table handle retains its earlier Lance snapshot.
        # Read committed storage through a fresh handle, as another caller does.
        db = await store._generation_db(active)
        table = await db.open_table(Collection.MEMORIES.value)
        retained = await table.query().select(['id', 'metadata']).to_list()
        assert len(retained) == 2049
        assert {row['id'] for row in retained} == {row['id'] for row in rows[::2]}
        for generation in (active, building):
            db = await store._generation_db(generation)
            other = await db.open_table(Collection.CONVERSATIONS.value)
            assert [row['id'] for row in await other.query().select(['id']).to_list()] == ['row-00000']
        assert catalog.deleted(Collection.MEMORIES.value, "erased'last")
        assert not catalog.deleted(Collection.MEMORIES.value, 'row-04096')
        assert await store.erase_source_projections(['erased', 'retained']) == 0
    finally:
        await store.close()
