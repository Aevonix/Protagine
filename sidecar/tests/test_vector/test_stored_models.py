"""Health inspects every model label, without loading stored vector payloads."""

import pytest

from protagine.vector.collections import Collection
from protagine.vector.query import VectorItem
from protagine.vector.store import VectorStore


@pytest.mark.asyncio
async def test_stored_models_projects_metadata_across_real_collections(tmp_path, monkeypatch):
    store = VectorStore(str(tmp_path / "vectors"))
    await store.connect(2)
    await store.ensure_collections(2)
    await store._db.drop_table(Collection.SKILLS.value)
    for collection, identifier, model in [
        (Collection.MEMORIES, "a", "model-z"),
        (Collection.MEMORIES, "b", "model-a"),
        (Collection.CONVERSATIONS, "c", "model-z"),
        (Collection.CONVERSATIONS, "d", "model-b"),
    ]:
        await store.add(collection, identifier, "Stored text", [1.0, 0.0], {"model_id": model})
    table = await store._table(Collection.MEMORIES)
    await table.update(where="id = 'a'", updates={"metadata": "invalid-json"})
    await store.add(Collection.MEMORIES, "empty", "No model", [1.0, 0.0])

    # Observe the real Lance query boundary, not a fake store result. A full
    # scan would decode the large vector/text columns before this check.
    query_type = type(table.query())
    original_to_arrow = query_type.to_arrow
    schemas = []

    async def inspect_projection(query, *args, **kwargs):
        result = await original_to_arrow(query, *args, **kwargs)
        schemas.append(result.column_names)
        return result

    monkeypatch.setattr(query_type, "to_arrow", inspect_projection)

    async def reject_full_scan(*args, **kwargs):
        pytest.fail("Health must not materialize whole vector records")

    monkeypatch.setattr(store, "scan_all", reject_full_scan)
    assert await store.get_stored_models() == ["model-a", "model-b", "model-z"]
    assert schemas == [["metadata"]] * (len(Collection) - 1)
    await store.close()


@pytest.mark.asyncio
async def test_stored_models_includes_model_after_first_256_rows(tmp_path):
    store = VectorStore(str(tmp_path / "vectors"))
    await store.connect(2)
    await store.ensure_collections(2)
    try:
        await store.add_batch(Collection.MEMORIES, [
            VectorItem(
                id=f"row-{index:03d}",
                text="Stored text",
                vector=[1.0, 0.0],
                metadata={"model_id": "model-early"},
            )
            for index in range(256)
        ])
        # A separately appended row catches accidental search limits or a scan
        # that stops after its first batch instead of inspecting every model.
        await store.add(
            Collection.MEMORIES, "row-256", "Last stored text", [1.0, 0.0],
            {"model_id": "model-late"},
        )
        assert await store.count(Collection.MEMORIES) == 257
        assert await store.get_stored_models() == ["model-early", "model-late"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_stored_model_read_failure_reaches_health_check(tmp_path, monkeypatch):
    store = VectorStore(str(tmp_path / "vectors"))
    await store.connect(2)
    await store.ensure_collections(2)
    table = await store._table(Collection.MEMORIES)

    async def unavailable(*args, **kwargs):
        raise OSError("stored metadata unavailable")

    monkeypatch.setattr(type(table.query()), "to_arrow", unavailable)
    with pytest.raises(OSError, match="stored metadata unavailable"):
        await store.get_stored_models()
    await store.close()
