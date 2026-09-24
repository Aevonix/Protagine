"""Vector tables are compacted routinely, off every request, one table at a time.

An upgraded store's conversations table held 74.7 GB of version manifests around
1.67 GB of data: every append adds a fragment and a manifest listing all of them,
and nothing but a forget compacted it. These tests pin the routine: the mind's
nightly upkeep and a version threshold schedule a background pass, the pass prunes
the old versions and logs each table's start and finish with sizes and duration,
a large table waits for the night, and no request (a forget, a write) compacts.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import pyarrow as pa
import pytest

import protagine.vector as vector_module
from protagine.initiatives.store import InitiativeStore
from protagine.mind import Mind
from protagine.turns import TurnIdempotencyLedger
from protagine.vector import Collection
from protagine.vector.compaction import Compaction, TableFootprint
from protagine.vector.indexes import EmbeddingIdentity, IndexCatalog
from protagine.vector.store import VectorStore

LOGGER = 'protagine.vector.compaction'


async def managed_store(tmp_path, **compaction):
    ledger = TurnIdempotencyLedger(tmp_path / 'turn-idempotency.db')
    store = VectorStore(str(tmp_path / 'lancedb'), identity=EmbeddingIdentity('fixture', 'fixture', 'known', 2),
                        catalog=IndexCatalog(ledger))
    await store.connect(2)
    await store.ensure_collections(2)
    store.compaction = Compaction(store, pause=0, **compaction)
    return ledger, store


async def append(store, count, collection=Collection.CONVERSATIONS):
    """``count`` commits, one row each: the source vector worker's pattern."""
    for index in range(count):
        await store.add(collection, f'row-{collection.value}-{index}', 'neutral text', [1.0, float(index + 1)],
                        {'source_uri': f'turn:t-{index}'})


def footprint(store, collection=Collection.CONVERSATIONS):
    db = store._generation_dbs[store.catalog.active()['id']]
    return TableFootprint.read(Compaction._path(db, collection.value))


async def row_ids(store, collection=Collection.CONVERSATIONS):
    table = await store._table(collection)
    return sorted(row['id'] for row in await table.query().select(['id']).to_list())


class FixedClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.mark.asyncio
async def test_the_minds_nightly_upkeep_schedules_a_pass_that_prunes_old_versions(tmp_path, monkeypatch, caplog):
    ledger, store = await managed_store(tmp_path)
    await append(store, 12)
    await append(store, 3, Collection.MEMORIES)
    assert footprint(store).versions >= 13
    monkeypatch.setattr(vector_module, '_store', store)
    clock = FixedClock(datetime(2026, 3, 10, 14, 0, tzinfo=timezone.utc))
    mind = Mind(config={'faculties': {'consolidation': False}}, store=InitiativeStore(state_dir=tmp_path),
                state_dir=tmp_path, owner_id='owner', ledger=ledger, clock=clock, backups=False)
    caplog.set_level(logging.INFO, logger=LOGGER)
    try:
        assert (await mind.tick())['vector_compaction'] is None          # a daytime start waits for its night
        assert store.compaction.task is None
        clock.now += timedelta(hours=13, minutes=30)                     # 03:30, the night crossed
        assert (await mind.tick())['vector_compaction'] == 'scheduled'
        await asyncio.wait_for(store.compaction.task, 30)
        assert footprint(store).versions == 1
        assert footprint(store, Collection.MEMORIES).versions == 1
        assert await row_ids(store) == sorted(f'row-conversations-{index}' for index in range(12))
        assert (await mind.tick())['vector_compaction'] is None          # once a night
        started = [r.getMessage() for r in caplog.records if 'vector compaction started (nightly)' in r.getMessage()]
        finished = [r.getMessage() for r in caplog.records if 'vector compaction finished (nightly)' in r.getMessage()]
        assert len(started) == len(finished) == 2
        conversations, = [line for line in finished if ': conversations (' in line]
        assert ' in ' in conversations and ' s, versions ' in conversations and ' -> 1, on disk ' in conversations
        assert all(record.levelno == logging.INFO for record in caplog.records if record.name == LOGGER)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_mind_off_still_compacts_at_night(tmp_path, monkeypatch):
    ledger, store = await managed_store(tmp_path)
    await append(store, 5)
    monkeypatch.setattr(vector_module, '_store', store)
    clock = FixedClock(datetime(2026, 3, 10, 14, 0, tzinfo=timezone.utc))
    mind = Mind(config={'faculties': {'consolidation': False}}, store=InitiativeStore(state_dir=tmp_path),
                state_dir=tmp_path, owner_id='owner', ledger=ledger, clock=clock, backups=False)
    try:
        mind.off(reason='test')
        clock.now += timedelta(days=1)
        summary = await mind.tick()
        assert summary['skipped'] == 'off' and summary['vector_compaction'] == 'scheduled'
        await asyncio.wait_for(store.compaction.task, 30)
        assert footprint(store).versions == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_the_threshold_loop_compacts_a_table_past_its_version_count(tmp_path, caplog):
    _, store = await managed_store(tmp_path, versions=10)
    await append(store, 6)
    await append(store, 12, Collection.MEMORIES)
    caplog.set_level(logging.INFO, logger=LOGGER)
    try:
        compacted = await store.compaction.run_pass('threshold')
        assert [(item['table'], item['versions_after']) for item in compacted] == [('memories', 1)]
        assert footprint(store).versions >= 6                          # under the threshold: left alone
        await append(store, 6)                                          # now past it
        loop = store.compaction.start(interval=0.01)
        for _ in range(500):
            if footprint(store).versions == 1:
                break
            await asyncio.sleep(0.01)
        assert footprint(store).versions == 1
        assert not loop.done()
        assert await row_ids(store) == sorted([f'row-conversations-{index}' for index in range(6)] * 2)
        assert any('vector compaction finished (threshold): conversations' in r.getMessage() for r in caplog.records)
    finally:
        await store.close()
    assert loop.done()


@pytest.mark.asyncio
async def test_a_table_too_large_for_the_day_waits_for_the_night_at_most_a_day(tmp_path, caplog):
    now = [0.0]
    _, store = await managed_store(tmp_path, versions=5, day_bytes=1, defer_limit=3600, clock=lambda: now[0])
    await append(store, 8)
    caplog.set_level(logging.INFO, logger=LOGGER)
    try:
        before = footprint(store).versions
        assert await store.compaction.run_pass('threshold') == []
        assert await store.compaction.run_pass('threshold') == []
        assert footprint(store).versions == before
        deferred = [r.getMessage() for r in caplog.records if 'vector compaction deferred' in r.getMessage()]
        assert len(deferred) == 1 and 'conversations' in deferred[0] and 'nightly pass' in deferred[0]
        now[0] = 3600.0                                                 # the night never came (mind off)
        assert [item['table'] for item in await store.compaction.run_pass('threshold')] == ['conversations']
        await append(store, 8)
        now[0] = 3601.0
        assert await store.compaction.run_pass('threshold') == []       # deferred afresh
        assert [item['table'] for item in await store.compaction.run_pass('nightly')] == ['conversations']
        assert footprint(store).versions == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_no_request_compacts_and_one_pass_runs_at_a_time(tmp_path, monkeypatch):
    """Writes never compact inline, whatever the version count; a forget answers first. Passes
    requested while one runs coalesce into one more pass, which runs in the compaction task."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from protagine.api.routers import host

    monkeypatch.setenv('PROTAGINE_STATE_DIR', str(tmp_path))
    ledger, store = await managed_store(tmp_path, versions=2)
    ledger.record_source('erased', contact_id='c', session_id='s',
                         messages=[{'role': 'user', 'content': 'neutral erased source'}])
    ledger.record_source('kept', contact_id='c', session_id='s',
                         messages=[{'role': 'user', 'content': 'neutral kept source'}])
    table = await store._table(Collection.MEMORIES, write=True)
    await table.add(pa.Table.from_pylist([
        {'id': f'm-{index}', 'text': 'neutral', 'vector': [1.0, 1.0],
         'metadata': json.dumps({'source_uri': 'turn:erased' if index % 2 else 'turn:kept'})}
        for index in range(4)], schema=await table.schema()))
    real, ran_in, release = store._purge_deleted, [], asyncio.Event()

    async def held(db, name):
        ran_in.append(asyncio.current_task())
        await release.wait()
        return await real(db, name)

    monkeypatch.setattr(store, '_purge_deleted', held)
    monkeypatch.setattr(vector_module, '_store', store)
    for name in ('_facts_store', '_affect_store', '_comms_log'):
        monkeypatch.setattr(host, name, None)
    try:
        await append(store, 6)                       # far past the threshold of 2: no inline compaction
        assert ran_in == [] and store.compaction.task is None
        app = FastAPI()
        app.include_router(host.router)
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await asyncio.wait_for(client.post('/v1/host/memory/sources/forget', json={
                'contact_id': 'c', 'source_ids': ['erased']}), 10)
        assert response.status_code == 200 and response.json()['vector_purge'] == 'scheduled'
        task = store.compaction.task
        await asyncio.sleep(0.05)
        assert ran_in and set(ran_in) == {task} and task.get_name() == 'protagine-vector-compaction'
        assert store.compaction.schedule('threshold') is task             # one task; the request waits in it
        assert store.compaction.schedule('nightly') is task
        release.set()
        await asyncio.wait_for(task, 30)
        assert set(ran_in) == {task}
        assert await row_ids(store, Collection.MEMORIES) == ['m-0', 'm-2']
        assert footprint(store, Collection.MEMORIES).versions == 1
    finally:
        release.set()
        await store.close()


@pytest.mark.asyncio
async def test_a_failed_table_is_logged_and_the_pass_goes_on(tmp_path, monkeypatch, caplog):
    _, store = await managed_store(tmp_path)
    await append(store, 3)
    await append(store, 3, Collection.MEMORIES)
    real = store._purge_deleted

    async def flaky(db, name):
        if name == Collection.MEMORIES.value:
            raise OSError('disk full')
        return await real(db, name)

    monkeypatch.setattr(store, '_purge_deleted', flaky)
    caplog.set_level(logging.INFO, logger=LOGGER)
    try:
        results = await store.compaction.run_pass('nightly')
        assert {item['table']: item.get('error', False) for item in results} == {'memories': True,
                                                                              'conversations': False}
        assert footprint(store).versions == 1
        failed, = [r for r in caplog.records if 'vector compaction failed (nightly): memories' in r.getMessage()]
        assert failed.levelno == logging.WARNING
    finally:
        await store.close()
