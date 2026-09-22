"""Admission behavior against fake endpoints; no model or network calls."""

import asyncio
import json
from types import SimpleNamespace as Spec

import pytest

from protagine.inference_pool.scheduler import (
    NoHealthyEndpoint,
    PoolBusy,
    RequestTooLarge,
    Scheduler,
)
from protagine.inference_pool.config import Endpoint, PoolConfig, Route


def endpoint(
    name="one",
    *,
    requests=2,
    tokens=100,
    context=100,
    reservations=None,
    max_input=None,
):
    return Endpoint(
        name=name,
        base_url="http://127.0.0.1:1/v1",
        model="fixture",
        max_requests=requests,
        max_tokens=tokens,
        context_tokens=context,
        reservations=reservations or {},
        max_input_tokens=max_input or {},
        api_key_env=None,
    )


def route(names=("one",), *, traffic="bulk", priority=10, timeout=1):
    return Route(
        name=traffic,
        model="fixture",
        endpoints=names,
        traffic_class=traffic,
        priority=priority,
        queue_timeout_seconds=timeout,
        default_output_tokens=10,
        max_output_tokens=100,
    )


def scheduler(*endpoints, max_queue=10, aging_seconds=10, cooldown=10):
    return Scheduler(
        PoolConfig(
            endpoints={e.name: e for e in endpoints},
            routes={},
            max_queue=max_queue,
            aging_seconds=aging_seconds,
            cooldown_seconds=cooldown,
        )
    )


async def queued(pool, count):
    async def wait():
        while pool.status()["queued_requests"] != count:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), 1)


@pytest.mark.parametrize("count", [1, 2, 4])
def test_one_two_or_many_endpoints_balance_and_release(count):
    async def run():
        names = tuple(f"endpoint-{i}" for i in range(count))
        pool = scheduler(*(endpoint(n) for n in names))
        leases = [await pool.acquire(route(names), 10, 10) for _ in range(2 * count)]
        assert {s["active_requests"] for s in pool.status()["endpoints"].values()} == {
            2
        }
        assert pool.status()["active_tokens"] == 40 * count
        pending = asyncio.create_task(pool.acquire(route(names), 10, 10))
        await queued(pool, 1)
        await leases[0].release()
        next_lease = await pending
        assert next_lease.endpoint.name == leases[0].endpoint.name
        await next_lease.release()
        for lease in leases:
            await lease.release()
        assert pool.status()["active_requests"] == 0
        assert pool.status()["active_tokens"] == 0

    asyncio.run(run())


def test_reserved_request_slot_remains_available_to_interactive():
    async def run():
        pool = scheduler(
            endpoint(
                requests=4,
                tokens=1000,
                reservations={"interactive": Spec(requests=1, tokens=0)},
            )
        )
        leases = [await pool.acquire(route(), 10, 10) for _ in range(3)]
        bulk = asyncio.create_task(pool.acquire(route(), 10, 10))
        await queued(pool, 1)
        foreground = await pool.acquire(
            route(traffic="interactive", priority=0), 10, 10
        )
        assert not bulk.done()
        assert pool.status()["active_requests"] == 4
        await leases[0].release()
        await (await bulk).release()
        await foreground.release()
        for lease in leases:
            await lease.release()

    asyncio.run(run())


def test_reserved_tokens_protect_memory_even_with_free_request_slots():
    async def run():
        pool = scheduler(
            endpoint(
                requests=10, reservations={"interactive": Spec(requests=0, tokens=30)}
            )
        )
        first = await pool.acquire(route(), 30, 10)
        bulk = asyncio.create_task(pool.acquire(route(), 30, 10))
        await queued(pool, 1)
        foreground = await pool.acquire(route(traffic="interactive"), 20, 10)
        assert not bulk.done()
        assert pool.status()["active_tokens"] == 70
        await first.release()
        next_lease = await bulk
        assert pool.status()["active_tokens"] == 70
        await foreground.release()
        await next_lease.release()

    asyncio.run(run())


def test_oversized_prefill_uses_only_allowed_backend_and_rejects_impossible_budget():
    async def run():
        pool = scheduler(
            endpoint("protected", max_input={"bulk": 20}), endpoint("work")
        )
        lease = await pool.acquire(route(("protected", "work")), 60, 10)
        assert lease.endpoint.name == "work"
        with pytest.raises(RequestTooLarge):
            await pool.acquire(route(("protected",)), 60, 10)
        with pytest.raises(RequestTooLarge):
            await pool.acquire(route(("work",)), 95, 10)
        await lease.release()
        own = await pool.acquire(route(("protected",), traffic="interactive"), 60, 10)
        await own.release()

    asyncio.run(run())


def test_impossible_reserved_budget_is_rejected_without_waiting():
    async def run():
        pool = scheduler(
            endpoint(reservations={"interactive": Spec(requests=1, tokens=40)})
        )
        with pytest.raises(RequestTooLarge):
            await pool.acquire(route(), 60, 10)
        assert pool.status()["queued_requests"] == 0

    asyncio.run(run())


def test_zero_input_limit_disables_class_even_for_empty_prompt():
    async def run():
        pool = scheduler(endpoint(max_input={"bulk": 0}))
        with pytest.raises(RequestTooLarge):
            await pool.acquire(route(), 0, 1)
        own = await pool.acquire(route(traffic="interactive"), 0, 1)
        await own.release()

    asyncio.run(run())


def test_queue_bound_deadline_and_canceled_wait_do_not_consume_capacity():
    async def run():
        pool = scheduler(endpoint(requests=1), max_queue=1)
        holder = await pool.acquire(route(), 10, 10)
        waiting = asyncio.create_task(pool.acquire(route(), 10, 10))
        await queued(pool, 1)
        with pytest.raises(PoolBusy, match="full"):
            await pool.acquire(route(), 10, 10)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert pool.status()["queued_requests"] == 0
        with pytest.raises(PoolBusy, match="deadline"):
            await pool.acquire(route(timeout=0.01), 10, 10)
        assert pool.status()["active_requests"] == 1
        assert pool.status()["queued_requests"] == 0
        await holder.release()

    asyncio.run(run())


def test_cancel_after_dispatch_returns_handed_off_lease():
    async def run():
        pool = scheduler(endpoint(requests=1))
        holder = await pool.acquire(route(), 10, 10)
        pending = asyncio.create_task(pool.acquire(route(), 10, 10))
        await queued(pool, 1)
        await holder.release()
        assert pool.status()["active_requests"] == 1
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert pool.status()["active_requests"] == 0
        assert pool.status()["active_tokens"] == 0

    asyncio.run(run())


def test_priority_and_aging_select_waiters_without_starving_old_work():
    async def run():
        pool = scheduler(endpoint(requests=1), aging_seconds=1)
        holder = await pool.acquire(route(), 10, 10)
        now = pool._clock()
        pool._clock = lambda: now
        low = asyncio.create_task(pool.acquire(route(priority=10, timeout=100), 10, 10))
        await queued(pool, 1)
        high = asyncio.create_task(pool.acquire(route(priority=0, timeout=100), 10, 10))
        await queued(pool, 2)
        await holder.release()
        high_lease = await high
        assert not low.done()
        # Old low-priority work eventually outranks newly arrived foreground work.
        now += 20
        newcomer = asyncio.create_task(
            pool.acquire(route(priority=0, timeout=100), 10, 10)
        )
        await queued(pool, 2)
        await high_lease.release()
        low_lease = await low
        assert not newcomer.done()
        await low_lease.release()
        await (await newcomer).release()

    asyncio.run(run())


def test_failed_endpoint_cools_down_and_failover_uses_other_backend():
    async def run():
        pool = scheduler(endpoint("a"), endpoint("b"))
        routes = route(("a", "b"))
        first = await pool.acquire(routes, 10, 10)
        assert first.endpoint.name == "a"
        await first.fail()
        second = await pool.acquire(routes, 10, 10)
        assert second.endpoint.name == "b"
        with pytest.raises(NoHealthyEndpoint):
            await pool.acquire(routes, 10, 10, excluded=frozenset({"b"}))
        await second.fail()
        with pytest.raises(NoHealthyEndpoint):
            await pool.acquire(routes, 10, 10)
        assert pool.status()["active_requests"] == 0
        now = pool._clock() + 11
        pool._clock = lambda: now
        recovered = await pool.acquire(routes, 10, 10)
        await recovered.release()
        await recovered.fail()  # Finished leases cannot cool the endpoint again.
        assert sum(s["failures"] for s in pool.status()["endpoints"].values()) == 2

    asyncio.run(run())


def test_different_logical_routes_share_one_physical_capacity_counter():
    async def run():
        pool = scheduler(endpoint(requests=1))
        first = await pool.acquire(route(traffic="chat"), 10, 10)
        worker = asyncio.create_task(pool.acquire(route(traffic="coding"), 10, 10))
        await queued(pool, 1)
        assert pool.status()["active_requests"] == 1
        await first.release()
        await (await worker).release()
        assert pool.status()["active_requests"] == 0
        json.dumps(pool.status())
        assert "base_url" not in json.dumps(pool.status())

    asyncio.run(run())


def test_shielded_cleanup_on_worker_cancellation_releases_capacity():
    async def run():
        pool = scheduler(endpoint(requests=1))
        acquired = asyncio.Event()

        async def worker():
            lease = await pool.acquire(route(), 10, 10)
            try:
                acquired.set()
                await asyncio.Event().wait()
            finally:
                await asyncio.shield(lease.release())

        task = asyncio.create_task(worker())
        await acquired.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert pool.status()["active_requests"] == 0
        assert pool.status()["active_tokens"] == 0

    asyncio.run(run())


def test_cumulative_counts_and_duration_measure_lease_lifetime_once():
    async def run():
        pool = scheduler(endpoint())
        pool._check_loop()
        now = 100.0
        pool._clock = lambda: now
        first = await pool.acquire(route(), 10, 10)
        second = await pool.acquire(route(), 10, 10)
        now += 3
        await first.release()
        now += 4
        await second.fail()
        await second.release()
        report = pool.status()
        assert report["admitted_requests"] == report["released_requests"] == 2
        assert report["total_duration_seconds"] == 10
        assert report["endpoints"]["one"]["total_duration_seconds"] == 10
        assert report["endpoints"]["one"]["failures"] == 1

    asyncio.run(run())
