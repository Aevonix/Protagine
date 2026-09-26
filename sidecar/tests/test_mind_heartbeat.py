"""The mind's tick reports that it ran, on or off, and never depends on the report."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from protagine.initiatives.store import InitiativeStore
from protagine.mind import Mind


def _mind(tmp_path, heartbeat, autonomy="off"):
    store = InitiativeStore(state_dir=tmp_path)
    return Mind(config={"autonomy": autonomy}, store=store, state_dir=tmp_path, owner_id=None, backups=False,
                clock=lambda: datetime.now(timezone.utc), heartbeat=heartbeat), store


@pytest.mark.asyncio
async def test_every_tick_beats_even_with_the_mind_off(tmp_path):
    beats = []

    async def heartbeat():
        beats.append("beat")

    mind, store = _mind(tmp_path, heartbeat)
    try:
        mind.authority.set_enabled(False)
        summary = await mind.tick()
        assert summary["skipped"] == "off"
        assert beats == ["beat"]
        await mind.tick()
        assert beats == ["beat", "beat"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_plain_callable_or_a_failing_one_never_breaks_the_tick(tmp_path):
    beats = []
    mind, store = _mind(tmp_path, lambda: beats.append("sync"))
    try:
        await mind.tick()
        assert beats == ["sync"]
        mind.heartbeat = lambda: (_ for _ in ()).throw(RuntimeError("telemetry gone"))
        assert (await mind.tick())["tick"] == 2
        mind.heartbeat = None
        assert (await mind.tick())["tick"] == 3
    finally:
        store.close()
