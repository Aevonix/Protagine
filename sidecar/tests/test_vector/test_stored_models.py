"""Health model provenance stays complete without loading embedding columns."""
import json

import pytest

from protagine.vector.collections import Collection
from protagine.vector.store import VectorStore


@pytest.mark.asyncio
async def test_stored_models_streams_metadata_across_batches(tmp_path, monkeypatch):
    store = VectorStore(str(tmp_path / "vectors"))
    await store.connect(dimensions=4)
    await store.ensure_collections(dimensions=4)
    table = await store._table(Collection.MEMORIES)
    await table.add([
        {"id": str(i), "text": "unused payload", "vector": [1., 0., 0., 0.],
         "metadata": (None if i == 2 else "malformed" if i == 1 else
                      json.dumps({"model_id": "later" if i == 1025 else "first"})),
         "modality": "text", "image_hash": "", "image_ref": "",
         "thumbnail_ref": "", "caption": "", "created_at": 0., "updated_at": 0.}
        for i in range(1026)
    ])
    await store.add(Collection.DOCUMENTS, id="other", text="other model",
                    vector=[1., 0., 0., 0.], metadata={"model_id": "another"})
    await store.add(Collection.DOCUMENTS, id="missing", text="no model",
                    vector=[1., 0., 0., 0.])

    original_table = store._table
    projected = []

    class MetadataOnlyTable:
        def __init__(self, real):
            self.real = real

        def query(self):
            real_query = self.real.query()

            class Projection:
                def select(self, columns):
                    assert columns == ["metadata"]
                    projected.append(columns)
                    return real_query.select(columns)

            return Projection()

    async def projected_table(collection):
        return MetadataOnlyTable(await original_table(collection))

    monkeypatch.setattr(store, "_table", projected_table)
    try:
        assert await store.get_stored_models() == ["another", "first", "later"]
        assert len(projected) == len(Collection)
    finally:
        await store.close()
