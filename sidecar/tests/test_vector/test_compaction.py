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


# --- nothing prunes or commits under a pass, and a pass prunes nothing a read still needs ------------

VANISHED = ('lance error: Not found: /store/generations/0f/conversations.lance/_versions/18446744073709550522.manifest, '
            '/registry/lance-io-12.0.0/src/object_store.rs:1615:12')


ROWS = 200      # one fragment each, more than a Lance scan reads ahead


async def ids_of(batches):
    return [row['id'] for row in batches.to_pylist()]


@pytest.mark.asyncio
async def test_a_pass_holds_every_commit_back_until_it_ends(tmp_path, monkeypatch):
    """Tables created by earlier releases carry Lance's own auto-cleanup (``lance.auto_cleanup.interval``
    20, ``older_than`` 14 days), which runs inside a commit. A writer committing beside a pass set it off,
    and it deleted the manifests the pass was pruning: "Not found: .../_versions/<n>.manifest" after
    777 s. Every optimize of a pass runs under the store's write lock, so a commit waits for the pass."""
    from lancedb.table import AsyncTable
    _, store = await managed_store(tmp_path)
    await append(store, 8)
    real, locked, entered, release = AsyncTable.optimize, [], asyncio.Event(), asyncio.Event()

    async def observed(self, **kwargs):
        locked.append(store.write_lock.locked())
        entered.set()
        await release.wait()
        return await real(self, **kwargs)

    monkeypatch.setattr(AsyncTable, 'optimize', observed)
    try:
        compaction = asyncio.create_task(store.compaction.run_pass('nightly'))
        await asyncio.wait_for(entered.wait(), 10)
        write = asyncio.create_task(store.add(Collection.CONVERSATIONS, 'late', 'neutral text', [1.0, 9.0],
                                              {'source_uri': 'turn:late'}))
        await asyncio.sleep(0.2)
        assert not write.done()                                     # the commit waits for the pass
        release.set()
        done = await asyncio.wait_for(compaction, 30)
        await asyncio.wait_for(write, 10)
        assert len(locked) >= 2 and all(locked)                    # each optimize of the pass
        assert [(item['table'], item['versions_after']) for item in done] == [('conversations', 1)]
        assert footprint(store).versions == 2                      # the pass's version, then the commit
        assert 'late' in await row_ids(store)
    finally:
        release.set()
        await store.close()


@pytest.mark.asyncio
async def test_a_read_begun_before_the_pass_keeps_its_files(tmp_path, monkeypatch):
    """A read opens the latest version and reads its files as it goes; a prune of every older version
    deleted them under it (Lance "Not found"). Here a commit lands after the read opened and replaces
    a fragment the read has yet to reach (the source vector worker re-projects a chunk: one row, one
    fragment), so the read's version is no longer the current one. Nothing is pruned until the reads
    begun before the pass have ended."""
    from lancedb.table import AsyncTable
    _, store = await managed_store(tmp_path)
    await append(store, ROWS)
    real, pruned_while_reading, reading = AsyncTable.optimize, [], [True]

    async def observed(self, **kwargs):
        pruned_while_reading.append(reading[0])
        return await real(self, **kwargs)

    monkeypatch.setattr(AsyncTable, 'optimize', observed)
    try:
        with store.reads.hold():
            table = await store._table(Collection.CONVERSATIONS)
            batches = await table.query().select(['id']).to_batches(max_batch_length=1)
            seen = await ids_of(await batches.__anext__())
            await store.update(Collection.CONVERSATIONS, f'row-conversations-{ROWS - 1}', 'neutral text',
                               [1.0, 0.5], {'source_uri': f'turn:t-{ROWS - 1}'})
            compaction = asyncio.create_task(store.compaction.run_pass('nightly'))
            await asyncio.sleep(0.5)
            assert not compaction.done() and pruned_while_reading == []
            async for batch in batches:
                seen += await ids_of(batch)
            reading[0] = False
        assert sorted(seen) == sorted(f'row-conversations-{index}' for index in range(ROWS))
        done = await asyncio.wait_for(compaction, 30)
        assert [(item['table'], item['versions_after']) for item in done] == [('conversations', 1)]
        assert pruned_while_reading and not any(pruned_while_reading)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_a_read_begun_while_the_pass_compacts_keeps_its_version_too(tmp_path, monkeypatch):
    """A read that opens the table while the pass holds it reads the version the compaction replaces.
    The pass's first prune keeps that version and prunes the history before it; the rest goes once
    the read ends."""
    from lancedb.table import AsyncTable
    import protagine.vector.store as store_module
    monkeypatch.setattr(store_module, 'KEEP_SLACK_SECONDS', 0.5)   # the compaction takes far less
    _, store = await managed_store(tmp_path)
    await append(store, ROWS)
    await asyncio.sleep(1.0)
    await store.add(Collection.CONVERSATIONS, 'current', 'neutral text', [1.0, 0.5], {'source_uri': 'turn:current'})
    history = footprint(store).versions
    real, entered, release = AsyncTable.optimize, asyncio.Event(), asyncio.Event()

    async def gated(self, **kwargs):
        if not release.is_set():
            entered.set()
            await release.wait()
        return await real(self, **kwargs)

    monkeypatch.setattr(AsyncTable, 'optimize', gated)
    try:
        compaction = asyncio.create_task(store.compaction.run_pass('nightly'))
        await asyncio.wait_for(entered.wait(), 10)                 # the pass holds the table; it compacts next
        with store.reads.hold():
            table = await store._table(Collection.CONVERSATIONS)
            batches = await table.query().select(['id']).to_batches(max_batch_length=1)
            seen = await ids_of(await batches.__anext__())
            release.set()
            for _ in range(200):                                    # compacted, the older history pruned
                if footprint(store).versions < history:
                    break
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.2)
            assert footprint(store).versions <= 3                  # this read's version and optimize's commits
            assert not compaction.done()
            async for batch in batches:
                seen += await ids_of(batch)
        assert sorted(seen) == sorted([f'row-conversations-{index}' for index in range(ROWS)] + ['current'])
        done = await asyncio.wait_for(compaction, 30)
        assert [(item['table'], item['versions_after']) for item in done] == [('conversations', 1)]
    finally:
        release.set()
        await store.close()


@pytest.mark.asyncio
async def test_a_forget_scanning_while_a_pass_runs_finishes_and_so_does_the_pass(tmp_path, monkeypatch):
    """The forget's scan reads a whole table and deletes what it matched under the write lock, which the
    pass holds while it waits for reads: the scan reads to the end first, then deletes."""
    ledger, store = await managed_store(tmp_path)
    for index in range(ROWS):
        ledger.record_source(f't-{index}', contact_id='c', session_id='s',
                             messages=[{'role': 'user', 'content': f'neutral source {index}'}])
    await append(store, ROWS)
    ledger.erase_sources(contact_id='c', turn_ids=['t-3', 't-9'])
    real_to_thread, paused, release = asyncio.to_thread, asyncio.Event(), asyncio.Event()

    async def pausing(func, *args, **kwargs):
        if getattr(func, '__name__', '') == 'erased_ids' and not paused.is_set():
            paused.set()
            await release.wait()
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, 'to_thread', pausing)
    try:
        erase = asyncio.create_task(store.erase_source_projections(['t-3', 't-9'], purge=False))
        await asyncio.wait_for(paused.wait(), 10)                  # the scan holds its first batch
        compaction = asyncio.create_task(store.compaction.run_pass('nightly'))
        await asyncio.sleep(0.3)
        release.set()
        assert await asyncio.wait_for(erase, 30) == 2
        await asyncio.wait_for(compaction, 30)
        assert await row_ids(store) == sorted(f'row-conversations-{index}' for index in range(ROWS)
                                              if index not in (3, 9))
    finally:
        release.set()
        await store.close()


@pytest.mark.asyncio
async def test_a_read_that_never_ends_holds_the_pass_back_only_so_long(tmp_path, monkeypatch, caplog):
    _, store = await managed_store(tmp_path)
    await append(store, 6)
    monkeypatch.setattr(store, 'read_drain_seconds', 0.3)
    caplog.set_level(logging.WARNING, logger='protagine.vector.store')
    try:
        with store.reads.hold():
            done = await asyncio.wait_for(store.compaction.run_pass('nightly'), 30)
        assert [(item['table'], item['versions_after']) for item in done] == [('conversations', 1)]
        assert any('still running after 0 s' in record.getMessage() for record in caplog.records)
    finally:
        await store.close()


@pytest.mark.parametrize('failures, error, retried', [
    (1, VANISHED, True),                                             # retried once: the pass completes
    (2, VANISHED, True),                                             # once only
    (1, 'lance error: Not found: /store/conversations.lance/data/0a.lance', False),   # not a manifest
])
@pytest.mark.asyncio
async def test_a_manifest_that_vanished_under_the_pass_is_retried_once(tmp_path, monkeypatch, caplog,
                                                                       failures, error, retried):
    from lancedb.table import AsyncTable
    _, store = await managed_store(tmp_path)
    await append(store, 6)
    real, calls = AsyncTable.optimize, []

    async def vanishing(self, **kwargs):
        calls.append(kwargs)
        if len(calls) <= failures:
            raise RuntimeError(error)
        return await real(self, **kwargs)

    monkeypatch.setattr(AsyncTable, 'optimize', vanishing)
    caplog.set_level(logging.INFO)
    try:
        done = await store.compaction.run_pass('nightly')
        retries = [r for r in caplog.records if 'retrying once' in r.getMessage()]
        assert len(retries) == (1 if retried else 0)
        if retried and failures == 1:
            assert done[0]['versions_after'] == 1 and 'error' not in done[0]
            first, again = calls[0]['cleanup_older_than'], calls[1]['cleanup_older_than']
            assert timedelta(0) <= again - first < timedelta(seconds=5)  # the same cutoff, measured again
        else:
            assert done[0].get('error') is True
            assert len(calls) == (2 if retried else 1)
            assert any('vector compaction failed (nightly): conversations' in r.getMessage() for r in caplog.records)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_every_read_of_the_store_is_counted(tmp_path, monkeypatch):
    """So a pass can wait for it. Writes are not reads: they hold the write lock, which the pass holds."""
    from contextlib import contextmanager
    _, store = await managed_store(tmp_path)
    await append(store, 2)
    held, real = [], store.reads.hold

    @contextmanager
    def counted():
        held.append(1)
        with real():
            yield

    monkeypatch.setattr(store.reads, 'hold', counted)
    try:
        reads = {
            'search': lambda: store.search(Collection.CONVERSATIONS, [1.0, 1.0], limit=2),
            'search_by_image_hash': lambda: store.search_by_image_hash(Collection.CONVERSATIONS, 'none'),
            'get': lambda: store.get(Collection.CONVERSATIONS, 'row-conversations-0'),
            'count': lambda: store.count(Collection.CONVERSATIONS),
            'list_ids': lambda: store.list_ids(Collection.CONVERSATIONS),
            'scan_all': lambda: store.scan_all(Collection.CONVERSATIONS),
            'get_stored_models': store.get_stored_models,
            'check_index_health': lambda: store.check_index_health(store.identity),
            'erase_source_projections': lambda: store.erase_source_projections(['t-0'], purge=False),
        }
        for name, read in reads.items():
            before = len(held)
            try:
                await read()
            except Exception:
                pass                                                # a fixture index may be refused; it was read
            assert len(held) > before, name
    finally:
        await store.close()
