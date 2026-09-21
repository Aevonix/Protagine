"""Baseline reads and writes reach the graph instead of silently defaulting."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from protagine.intelligence.graph.client import ProtagineGraph
from protagine.intelligence.graph.queries import GET_BASELINE, UPDATE_BASELINE
from protagine.intelligence.mind_model.graph_baseline import GraphBaselineStore


@pytest.mark.asyncio
async def test_message_observation_updates_existing_person_baseline():
    histogram = [0] * 24
    histogram[9] = 1
    previous = {
        "msg_count": 1, "length_mean": 10.0, "length_m2": 0.0,
        "length_std": 0.0, "hour_histogram": json.dumps(histogram),
    }
    result = SimpleNamespace(single=AsyncMock(return_value=previous))
    session = MagicMock()
    session.run = AsyncMock(return_value=result)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    graph = object.__new__(ProtagineGraph)
    graph.database = "test-baselines"
    graph.driver = SimpleNamespace(session=MagicMock(return_value=session))
    baseline = GraphBaselineStore(graph)

    assert (await baseline.get("person-a")).length_mean == 10.0
    await baseline.record_message("person-a", length=30, hour=10)

    calls = session.run.await_args_list
    assert len(calls) == 3
    assert calls[0].args == (GET_BASELINE,)
    assert calls[0].kwargs == {"person_id": "person-a"}
    assert calls[1].args == (GET_BASELINE,)
    assert calls[2].args == (UPDATE_BASELINE,)
    written = calls[2].kwargs
    assert written["person_id"] == "person-a"
    assert written["msg_count"] == 2
    assert written["length_mean"] == 20.0
    assert written["length_std"] == 10.0
    counts = json.loads(written["hour_histogram"])
    assert sum(counts) == 2
    assert counts[9:11] == [1, 1]
