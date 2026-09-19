"""One-process admission for shared inference endpoints.

Reservations protect request and token capacity, not GPU execution time. All
competing clients must use this scheduler for its accounting to be meaningful.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Endpoint, PoolConfig, Route


class PoolBusy(RuntimeError):
    """The bounded waiting queue is full, or the request's deadline expired."""


class RequestTooLarge(ValueError):
    """No configured endpoint can admit the requested input/output budget."""


class NoHealthyEndpoint(RuntimeError):
    """All otherwise eligible endpoints are excluded or cooling down."""


@dataclass
class _Usage:
    requests: int = 0
    tokens: int = 0
    class_requests: Counter = field(default_factory=Counter)
    class_tokens: Counter = field(default_factory=Counter)
    retry_at: float = 0
    failures: int = 0
    last_assignment: int = 0
    admitted_requests: int = 0
    released_requests: int = 0
    total_duration_seconds: float = 0


@dataclass
class _Waiter:
    sequence: int
    route: Route
    input_tokens: int
    output_tokens: int
    endpoints: tuple[Endpoint, ...]
    started: float
    future: asyncio.Future
    lease: Lease | None = None


class Lease:
    """Capacity ownership until the entire response or upstream abort finishes.

    Release is idempotent and has no suspension points. Callers can shield it
    in their finally block when cancellation also needs to close the upstream.
    """

    def __init__(self, scheduler, endpoint, traffic_class, tokens):
        self.endpoint = endpoint
        self._scheduler = scheduler
        self._traffic_class = traffic_class
        self._tokens = tokens
        self._started = scheduler._clock()
        self._released = False

    async def release(self):
        self._finish(failed=False)

    async def fail(self):
        self._finish(failed=True)

    def _finish(self, *, failed):
        self._scheduler._check_loop()
        if self._released:
            return
        self._released = True
        usage = self._scheduler._usage[self.endpoint.name]
        if failed:
            usage.retry_at = (
                self._scheduler._clock() + self._scheduler.config.cooldown_seconds
            )
            usage.failures += 1
        usage.requests -= 1
        usage.tokens -= self._tokens
        usage.class_requests[self._traffic_class] -= 1
        usage.class_tokens[self._traffic_class] -= self._tokens
        usage.released_requests += 1
        usage.total_duration_seconds += max(0, self._scheduler._clock() - self._started)
        self._scheduler._dispatch()


class Scheduler:
    """Bounded, priority-aged waiting without background workers or storage."""

    def __init__(self, config: PoolConfig):
        self.config = config
        self._loop = None
        self._clock = None
        self._usage = {name: _Usage() for name in config.endpoints}
        self._waiting = []
        self._sequence = 0

    def _check_loop(self):
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
            self._clock = loop.time
        elif self._loop is not loop:
            raise RuntimeError("An inference scheduler belongs to one asyncio loop")

    @staticmethod
    def _possible(endpoint, route, input_tokens, output_tokens):
        total = input_tokens + output_tokens
        input_limit = endpoint.max_input_tokens.get(route.traffic_class)
        if input_limit is not None and (input_limit == 0 or input_tokens > input_limit):
            return False
        reserved_requests = sum(
            r.requests
            for c, r in endpoint.reservations.items()
            if c != route.traffic_class
        )
        reserved_tokens = sum(
            r.tokens
            for c, r in endpoint.reservations.items()
            if c != route.traffic_class
        )
        return (
            total <= endpoint.context_tokens
            and total <= endpoint.max_tokens - reserved_tokens
            and 1 <= endpoint.max_requests - reserved_requests
        )

    def _fits(self, endpoint, waiter):
        usage = self._usage[endpoint.name]
        protected_requests = sum(
            max(0, r.requests - usage.class_requests[c])
            for c, r in endpoint.reservations.items()
            if c != waiter.route.traffic_class
        )
        protected_tokens = sum(
            max(0, r.tokens - usage.class_tokens[c])
            for c, r in endpoint.reservations.items()
            if c != waiter.route.traffic_class
        )
        return (
            usage.requests + 1 + protected_requests <= endpoint.max_requests
            and usage.tokens
            + waiter.input_tokens
            + waiter.output_tokens
            + protected_tokens
            <= endpoint.max_tokens
        )

    def _rank(self, endpoint, waiter):
        usage = self._usage[endpoint.name]
        requests = (usage.requests + 1) / endpoint.max_requests
        tokens = (
            usage.tokens + waiter.input_tokens + waiter.output_tokens
        ) / endpoint.max_tokens
        return (
            max(requests, tokens),
            requests + tokens,
            usage.last_assignment,
            endpoint.name,
        )

    def _dispatch(self):
        now = self._clock()
        ordered = sorted(
            self._waiting,
            key=lambda w: (
                w.route.priority - int((now - w.started) / self.config.aging_seconds),
                w.sequence,
            ),
        )
        for waiter in ordered:
            if waiter.future.done():
                self._waiting.remove(waiter)
                continue
            if now - waiter.started >= waiter.route.queue_timeout_seconds:
                self._waiting.remove(waiter)
                waiter.future.set_exception(
                    PoolBusy("Inference queue deadline expired")
                )
                continue
            healthy = [
                e for e in waiter.endpoints if self._usage[e.name].retry_at <= now
            ]
            if not healthy:
                self._waiting.remove(waiter)
                waiter.future.set_exception(
                    NoHealthyEndpoint("Eligible inference endpoints are cooling down")
                )
                continue
            available = [e for e in healthy if self._fits(e, waiter)]
            if not available:
                continue
            endpoint = min(available, key=lambda e: self._rank(e, waiter))
            usage = self._usage[endpoint.name]
            self._sequence += 1
            usage.last_assignment = self._sequence
            tokens = waiter.input_tokens + waiter.output_tokens
            usage.requests += 1
            usage.admitted_requests += 1
            usage.tokens += tokens
            usage.class_requests[waiter.route.traffic_class] += 1
            usage.class_tokens[waiter.route.traffic_class] += tokens
            waiter.lease = Lease(self, endpoint, waiter.route.traffic_class, tokens)
            self._waiting.remove(waiter)
            waiter.future.set_result(waiter.lease)

    async def acquire(
        self,
        route: Route,
        input_tokens: int,
        output_tokens: int,
        excluded: frozenset[str] = frozenset(),
    ) -> Lease:
        self._check_loop()
        if (
            type(input_tokens) is not int
            or input_tokens < 0
            or type(output_tokens) is not int
            or output_tokens < 1
        ):
            raise RequestTooLarge(
                "Input and output token budgets must be nonnegative/positive integers"
            )
        if output_tokens > route.max_output_tokens:
            raise RequestTooLarge("Requested output exceeds the route limit")
        endpoints = tuple(
            self.config.endpoints[name]
            for name in route.endpoints
            if name not in excluded
        )
        if not endpoints:
            raise NoHealthyEndpoint("No untried inference endpoint remains")
        endpoints = tuple(
            e
            for e in endpoints
            if self._possible(e, route, input_tokens, output_tokens)
        )
        if not endpoints:
            raise RequestTooLarge(
                "No endpoint admits this request's context and class budget"
            )
        if not any(self._usage[e.name].retry_at <= self._clock() for e in endpoints):
            raise NoHealthyEndpoint("Eligible inference endpoints are cooling down")
        self._sequence += 1
        waiter = _Waiter(
            self._sequence,
            route,
            input_tokens,
            output_tokens,
            endpoints,
            self._clock(),
            self._loop.create_future(),
        )
        # Dispatch first: an immediately runnable request does not occupy the
        # waiting-queue allowance, even when other classes cannot currently run.
        self._waiting.append(waiter)
        self._dispatch()
        if not waiter.future.done() and len(self._waiting) > self.config.max_queue:
            self._waiting.remove(waiter)
            waiter.future.cancel()
            raise PoolBusy("Inference waiting queue is full")
        try:
            return await asyncio.wait_for(
                asyncio.shield(waiter.future), route.queue_timeout_seconds
            )
        except BaseException as error:
            if waiter in self._waiting:
                self._waiting.remove(waiter)
            if not waiter.future.done():
                waiter.future.cancel()
            elif not waiter.future.cancelled():
                # Consume an exception if admission/deadline raced cancellation.
                waiter.future.exception()
            if waiter.lease is not None:
                waiter.lease._finish(failed=False)
            else:
                self._dispatch()
            if isinstance(error, TimeoutError):
                raise PoolBusy("Inference queue deadline expired") from None
            raise

    def status(self):
        now = self._clock() if self._clock is not None else 0
        return {
            "active_requests": sum(s.requests for s in self._usage.values()),
            "active_tokens": sum(s.tokens for s in self._usage.values()),
            "admitted_requests": sum(s.admitted_requests for s in self._usage.values()),
            "released_requests": sum(s.released_requests for s in self._usage.values()),
            "total_duration_seconds": sum(
                s.total_duration_seconds for s in self._usage.values()
            ),
            "duration_basis": "admitted lease lifetime, including cancellation and failure; excludes queue wait",
            "queued_requests": len(self._waiting),
            "queued_by_class": dict(
                Counter(w.route.traffic_class for w in self._waiting)
            ),
            "endpoints": {
                name: {
                    "active_requests": s.requests,
                    "active_tokens": s.tokens,
                    "active_by_class": {c: n for c, n in s.class_requests.items() if n},
                    "tokens_by_class": {c: n for c, n in s.class_tokens.items() if n},
                    "cooldown_seconds": max(0, s.retry_at - now),
                    "failures": s.failures,
                    "admitted_requests": s.admitted_requests,
                    "released_requests": s.released_requests,
                    "total_duration_seconds": s.total_duration_seconds,
                }
                for name, s in self._usage.items()
            },
        }
