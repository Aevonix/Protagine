"""Routine health verifies managed identity and bounded physical index reads."""
import asyncio
from dataclasses import asdict, replace
import json
from types import SimpleNamespace

import pytest

from protagine.turns import TurnIdempotencyLedger
from protagine.vector.collections import Collection
from protagine.vector.indexes import EmbeddingIdentity, IndexCatalog, IncompatibleIndex
from protagine.vector.store import VectorStore


async def managed(tmp_path):
    identity = EmbeddingIdentity("model-a", "served-a", "revision-a", 2)
    catalog = IndexCatalog(TurnIdempotencyLedger(tmp_path / "sources.db"))
    store = VectorStore(str(tmp_path / "vectors"), identity=identity, catalog=catalog)
    await store.connect(2)
    await store.ensure_collections(2)
    return store, identity


@pytest.mark.asyncio
async def test_health_reads_ids_only_and_bounds_each_collection(tmp_path, monkeypatch):
    store, identity = await managed(tmp_path)
    for index in range(3):
        await store.add(Collection.MEMORIES, str(index), "Source text", [1.0, 0.0],
                        {"model_id": "legacy annotation is not generation provenance"})
    table = await store._table(Collection.MEMORIES)
    query_type = type(table.query())
    original = query_type.to_arrow
    observations = []

    async def inspect(query, *args, **kwargs):
        rows = await original(query, *args, **kwargs)
        observations.append((rows.column_names, rows.num_rows))
        return rows

    async def forbidden_audit():
        pytest.fail("Routine health must not discover every row's legacy model annotation")

    monkeypatch.setattr(query_type, "to_arrow", inspect)
    monkeypatch.setattr(store, "get_stored_models", forbidden_audit)
    await store.check_index_health(identity)
    assert len(observations) == len(Collection)
    assert all(columns == ["id"] and count <= 1 for columns, count in observations)
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"requested_model": "model-b"}, {"served_model": "served-b"},
    {"declared_revision": "revision-b"}, {"dimensions": 3},
    {"query_format": "another-query-format"},
])
async def test_health_rejects_pipeline_and_catalog_identity_mismatches(tmp_path, change):
    store, identity = await managed(tmp_path)
    different = replace(identity, **change)
    with pytest.raises(IncompatibleIndex):
        await store.check_index_health(different)
    # A matching pointer fingerprint must not conceal inconsistent stored identity.
    with store.catalog.ledger._connect() as conn:
        conn.execute("UPDATE vector_generations SET identity_json=?", (json.dumps(asdict(different)),))
    with pytest.raises(IncompatibleIndex):
        await store.check_index_health(identity)
    await store.close()


@pytest.mark.asyncio
async def test_health_rejects_legacy_and_nonready_generation(tmp_path):
    legacy = VectorStore(str(tmp_path / "legacy"))
    await legacy.connect(2)
    with pytest.raises(IncompatibleIndex):
        await legacy.check_index_health(EmbeddingIdentity("m", "m", "r", 2))
    store, identity = await managed(tmp_path)
    with store.catalog.ledger._connect() as conn:
        conn.execute("UPDATE vector_generations SET status='building'")
    with pytest.raises(IncompatibleIndex):
        await store.check_index_health(identity)
    await store.close()
    await legacy.close()


@pytest.mark.asyncio
async def test_health_checks_actual_vector_width(tmp_path):
    store, identity = await managed(tmp_path)
    db = await store._generation_db(store.catalog.active())
    from protagine.vector.store import _base_schema
    await db.create_table(Collection.MEMORIES.value, schema=_base_schema(3), mode="overwrite")
    with pytest.raises(IncompatibleIndex, match="width"):
        await store.check_index_health(identity)
    await store.close()


@pytest.mark.asyncio
async def test_health_accepts_empty_tables_but_rejects_missing_index(tmp_path):
    store, identity = await managed(tmp_path)
    db = await store._generation_db(store.catalog.active())
    # Empty indexes are valid; missing optional collections are not a read error.
    for collection in Collection:
        if collection != Collection.MEMORIES:
            await db.drop_table(collection.value)
    await store.check_index_health(identity)
    await db.drop_table(Collection.MEMORIES.value)
    with pytest.raises(IncompatibleIndex, match="no readable collections"):
        await store.check_index_health(identity)
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["catalog", "read", "timeout"])
async def test_index_failure_degrades_host_health(tmp_path, monkeypatch, failure):
    from protagine.api.routers import host
    from protagine import vector

    store, identity = await managed(tmp_path)

    class Embedder:
        index_identity = identity
        _provider = SimpleNamespace(_config=SimpleNamespace(model_id=identity.requested_model))

        async def health_check(self):
            return {"status": "ok"}

    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path / "host"))
    monkeypatch.setattr(host, "_embedder", Embedder())
    monkeypatch.setattr(host, "_telemetry", None)
    monkeypatch.setattr(host, "_commitment_store", None)
    monkeypatch.setattr(vector, "_store", store)
    assert (await host.health()).status == "ok"

    if failure == "catalog":
        def unavailable(*args):
            raise OSError("catalog unavailable")
        monkeypatch.setattr(store.catalog, "read_generation", unavailable)
    else:
        table = await store._table(Collection.MEMORIES)

        async def unreadable(*args, **kwargs):
            if failure == "timeout":
                await asyncio.Event().wait()
            raise OSError("index unavailable")

        monkeypatch.setattr(type(table.query()), "to_list", unreadable)
        monkeypatch.setattr(host, "_INDEX_HEALTH_TIMEOUT_SECONDS", 0.02)
    result = await host.health()
    assert result.status == "degraded"
    assert "index-check failed" in result.notes["embed"]
    if failure == "timeout":
        assert "TimeoutError" in result.notes["embed"]
    await store.close()
