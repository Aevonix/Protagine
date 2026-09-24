"""VectorStore.list_ids: the projected id listing the generation catalog reads."""

from __future__ import annotations

import tempfile

import pytest

from protagine.vector.collections import Collection


@pytest.mark.asyncio
async def test_list_ids_projected_query():
    from protagine.vector.store import VectorStore
    with tempfile.TemporaryDirectory() as d:
        vs = VectorStore(data_dir=d)
        await vs.connect(dimensions=4)
        await vs.ensure_collections(dimensions=4)
        for i in range(3):
            await vs.add(Collection.MEMORIES, id=f"v{i}", text=f"t{i}",
                         vector=[0.1, 0.2, 0.3, float(i)])
        ids = await vs.list_ids(Collection.MEMORIES)
        assert sorted(ids) == ["v0", "v1", "v2"]
        assert await vs.list_ids(Collection.DOCUMENTS) == []
