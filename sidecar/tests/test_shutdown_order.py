"""Shutdown must stop background work before the stores it uses are closed.

Two layers:

* ``AutonomyLoop.stop(join_timeout=...)`` waits for the loop task, so an
  in-flight tick finishes (or is cancelled) before the caller continues.
  Plain ``stop()`` stays a prompt signal.
* The server lifespan stops the autonomy loop and the queue workers before
  it closes the graph, world, skills, presence and channel stores.
"""

from __future__ import annotations

import asyncio

import pytest

from protagine.autonomy.config import AutonomyConfig, AutonomyMode
from protagine.autonomy.loop import AutonomyLoop
from protagine.autonomy.registry import SubsystemRegistry
from protagine.identity.resolver import reset_identity_resolver


@pytest.fixture(autouse=True)
def _fresh_identity_resolver():
    # start() resolves the owner through the process-wide resolver; do not
    # leave a store-less cached instance behind for later tests.
    reset_identity_resolver()
    yield
    reset_identity_resolver()


def _proactive_loop() -> AutonomyLoop:
    config = AutonomyConfig(mode=AutonomyMode.PROACTIVE, tick_interval_secs=60)
    return AutonomyLoop(registry=SubsystemRegistry(), config=config)


async def _start_and_wait_for_tick(loop, tick):
    loop._tick = tick
    task = asyncio.create_task(loop.start())
    await asyncio.wait_for(loop._tick_started.wait(), timeout=2)
    return task


async def test_stop_join_waits_for_in_flight_tick():
    loop = _proactive_loop()
    loop._tick_started = asyncio.Event()
    finished = asyncio.Event()

    async def slow_tick():
        loop._tick_started.set()
        await asyncio.sleep(0.2)
        finished.set()

    task = await _start_and_wait_for_tick(loop, slow_tick)

    await loop.stop(join_timeout=5.0)

    assert finished.is_set(), "stop returned while a tick was still running"
    assert task.done()
    assert not loop.is_running


async def test_stop_join_cancels_a_tick_that_overruns_the_timeout():
    loop = _proactive_loop()
    loop._tick_started = asyncio.Event()

    async def stuck_tick():
        loop._tick_started.set()
        await asyncio.Event().wait()

    task = await _start_and_wait_for_tick(loop, stuck_tick)

    await asyncio.wait_for(loop.stop(join_timeout=0.1), timeout=2)

    assert task.done()
    assert not loop.is_running


async def test_plain_stop_is_still_a_prompt_signal():
    loop = _proactive_loop()
    loop._tick_started = asyncio.Event()
    release = asyncio.Event()

    async def held_tick():
        loop._tick_started.set()
        await release.wait()

    task = await _start_and_wait_for_tick(loop, held_tick)

    await asyncio.wait_for(loop.stop(), timeout=0.5)
    assert not task.done()

    release.set()
    await asyncio.wait_for(task, timeout=2)
    assert not loop.is_running


async def test_lifespan_stops_background_work_before_closing_stores(
    monkeypatch, tmp_path,
):
    from protagine import server
    from protagine.api.routers import host
    from protagine.channels.store import ChannelStore
    from protagine.world_model.store import WorldModelStore

    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PROTAGINE_EVENT_JOURNAL_DIR", str(tmp_path / "events"))

    order: list[str] = []

    def record(cls, method, label):
        original = getattr(cls, method)
        if asyncio.iscoroutinefunction(original):
            async def wrapper(self, *args, **kwargs):
                order.append(label)
                return await original(self, *args, **kwargs)
        else:
            def wrapper(self, *args, **kwargs):
                order.append(label)
                return original(self, *args, **kwargs)
        monkeypatch.setattr(cls, method, wrapper)

    record(AutonomyLoop, "stop", "autonomy_stop")
    record(WorldModelStore, "close", "world_close")
    record(ChannelStore, "close", "channel_close")

    app = server.create_app()
    async with server.lifespan(app):
        for _ in range(20):
            await asyncio.sleep(0)
        assert host._autonomy_loop is not None
        assert host._autonomy_loop.is_running

    closes = [i for i, step in enumerate(order) if step.endswith("_close")]
    assert closes, order
    assert "autonomy_stop" in order, order
    assert order.index("autonomy_stop") < min(closes), order
