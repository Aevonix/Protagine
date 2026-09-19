"""Small OpenAI-compatible admission proxy for configured model pools.

Run one worker: admission accounting is process-local. A model engine closing a
socket is not proof that it cancelled computation. Cancelled streams retain their
lease for the configured grace period; actual cancellation must be qualified for
each backend. No prompt, response, credential, or upstream error body is logged.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import os
import time
from typing import Any
from urllib.parse import urljoin

import anyio
from fastapi import FastAPI, Request
import httpx
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, Response, StreamingResponse

from .config import PoolConfig, load_config
from .scheduler import NoHealthyEndpoint, PoolBusy, RequestTooLarge, Scheduler

MAX_BODY_BYTES = 32 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
TOKEN_COUNT_TIMEOUT_SECONDS = 10.0
_MEDIA_TYPES = {
    "image_url",
    "input_image",
    "input_audio",
    "audio",
    "video_url",
    "video",
}


class InvalidRequest(ValueError):
    def __init__(self, message: str, status: int = 400):
        self.status = status
        super().__init__(message)


def _error(message: str, status: int, code: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": "inference_pool_error", "code": code}},
        status_code=status,
    )


def estimate_input_tokens(payload: dict[str, Any], route: Any) -> int:
    """Conservative UTF-8 byte estimate, not a tokenizer measurement.

    JSON formatting, tool schemas and chat framing are charged as input. Media
    payloads are replaced in this *estimate only* by a deployment-qualified cost;
    their original bytes are forwarded intact. Unknown content types fail closed
    rather than silently treating an unbounded modality as a short URL.
    """
    media_count = 0

    def walk(value: Any) -> Any:
        nonlocal media_count
        if isinstance(value, dict):
            if value.get("type") in _MEDIA_TYPES:
                media_count += 1
                if route.media_tokens_per_item is None:
                    raise InvalidRequest(
                        "This route has no qualified media token budget."
                    )
                if media_count > route.max_media_items:
                    raise InvalidRequest("Too many media items for this route.", 413)
                return {"type": "media_budgeted_separately"}
            return {key: walk(item) for key, item in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    messages = payload["messages"]
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    raise InvalidRequest("Message content items must be objects.")
                if part.get("type") not in _MEDIA_TYPES | {
                    "text",
                    "input_text",
                    "refusal",
                }:
                    raise InvalidRequest(
                        "Unsupported message content type for token accounting."
                    )
    # Conservatively count all non-media request fields. Byte count deliberately
    # overestimates ordinary text, rather than assuming four characters per token.
    encoded = json.dumps(
        walk(payload), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return (
        len(encoded)
        + 64
        + 16 * len(messages)
        + media_count * (route.media_tokens_per_item or 0)
    )


def _media_items(value: Any) -> int:
    if isinstance(value, dict):
        if value.get("type") in _MEDIA_TYPES:
            return 1
        return sum(_media_items(item) for item in value.values())
    if isinstance(value, list):
        return sum(_media_items(item) for item in value)
    return 0


async def _read_payload(
    request: Request, route: Any
) -> tuple[dict[str, Any], int, int]:
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            length = int(raw_length)
        except ValueError:
            raise InvalidRequest("Invalid Content-Length.") from None
        if length < 0 or length > MAX_BODY_BYTES:
            raise InvalidRequest("Request body exceeds the size limit.", 413)
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_BODY_BYTES:
                raise InvalidRequest("Request body exceeds the size limit.", 413)
            body.extend(chunk)
    except ClientDisconnect:
        raise InvalidRequest("Request interrupted.") from None
    try:

        def reject_constant(value):
            raise ValueError("Nonfinite JSON number")

        payload = json.loads(body, parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise InvalidRequest("Request body must be valid JSON.") from None
    if not isinstance(payload, dict):
        raise InvalidRequest("Request body must be an object.")
    if payload.get("model") != route.model:
        raise InvalidRequest(
            "Model does not match this route's configured model alias."
        )
    if "priority" in payload:
        raise InvalidRequest(
            "Priority is assigned by the configured route, not the request.", 422
        )
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise InvalidRequest("messages must be a nonempty array.")
    if any(
        not isinstance(m, dict) or not isinstance(m.get("role"), str) for m in messages
    ):
        raise InvalidRequest("Each message must have a role.")
    if type(payload.get("stream", False)) is not bool:
        raise InvalidRequest("stream must be a boolean.")
    if type(payload.get("n", 1)) is not int or payload.get("n", 1) != 1:
        raise InvalidRequest(
            "This route supports one generated sequence per request (n=1)."
        )
    budgets = [
        payload[k] for k in ("max_tokens", "max_completion_tokens") if k in payload
    ]
    if not budgets:
        payload["max_tokens"] = route.default_output_tokens
        budgets = [route.default_output_tokens]
    if any(
        type(value) is not int or value <= 0 or value > route.max_output_tokens
        for value in budgets
    ):
        raise InvalidRequest(
            "Output token budget is outside this route's configured limits."
        )
    output_tokens = max(budgets)
    try:
        input_tokens = estimate_input_tokens(payload, route)
    except (RecursionError, UnicodeError):
        raise InvalidRequest("Request content cannot be accounted for.") from None
    return payload, input_tokens, output_tokens


class _OwnedStream(StreamingResponse):
    """Release ownership even if ASGI disconnects before iterating the body."""

    def __init__(self, upstream: httpx.Response, lease: Any, grace: float):
        self.upstream = upstream
        self.lease = lease
        self.grace = grace
        self.completed = False
        self.failed = False
        super().__init__(
            self._body(),
            headers=_response_headers(upstream),
            status_code=upstream.status_code,
        )

    async def _body(self):
        try:
            async for chunk in self.upstream.aiter_raw():
                yield chunk
            self.completed = True
        except httpx.HTTPError:
            self.failed = True
            # Once streaming starts there is never a fallback generation or a
            # fabricated continuation of the partial model answer.
            raise RuntimeError("Upstream generation stream interrupted.") from None

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await _dispose(
                self.upstream,
                self.lease,
                0 if self.completed else self.grace,
                failed=self.failed,
            )


def _response_headers(response: httpx.Response) -> dict[str, str]:
    # Preserve encoding when forwarding raw bytes, but not cookies, server
    # headers, provider authentication or arbitrary implementation details.
    headers = {
        key: response.headers[key]
        for key in ("content-type", "content-encoding", "cache-control")
        if key in response.headers
    }
    headers.setdefault("cache-control", "no-store")
    return headers


async def _dispose(
    upstream: httpx.Response | None,
    lease: Any,
    grace: float = 0,
    *,
    failed: bool = False,
) -> None:
    with anyio.CancelScope(shield=True):
        try:
            if upstream is not None:
                await upstream.aclose()
        finally:
            if grace:
                await anyio.sleep(grace)
            if failed:
                await lease.fail()
            else:
                await lease.release()


def create_app(
    config: PoolConfig, *, transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    scheduler = Scheduler(config)
    counter_stats = {"qualified_counter_requests": 0, "conservative_fallbacks": 0}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(config.request_timeout_seconds, connect=10.0),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=None, max_keepalive_connections=32),
        ) as client:
            app.state.upstream_client = client
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.scheduler = scheduler

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "kind": "inference_pool",
            "engine_health_verified": False,
        }

    @app.get("/status")
    async def status():
        result = scheduler.status()
        if hasattr(result, "__await__"):
            result = await result
        return {**result, "token_counting": dict(counter_stats)}

    @app.get("/{route_name}/v1/models")
    async def models(route_name: str):
        route = config.routes.get(route_name)
        if route is None:
            return _error("Unknown route.", 404, "unknown_route")
        return {
            "object": "list",
            "data": [
                {
                    "id": route.model,
                    "object": "model",
                    "created": 0,
                    "owned_by": "configured_pool",
                }
            ],
        }

    async def count_input_attempts(route, payload, fallback):
        candidates = [config.endpoints[name] for name in route.endpoints]
        # Count on one replica only when the deployment declares the same
        # qualified counting contract for every interchangeable candidate.
        if not all(endpoint.tokenize_path for endpoint in candidates):
            counter_stats["conservative_fallbacks"] += 1
            return fallback
        deadline = time.monotonic() + TOKEN_COUNT_TIMEOUT_SECONDS
        for endpoint in candidates:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            headers = {"accept-encoding": "identity"}
            if endpoint.api_key_env:
                key = os.environ.get(endpoint.api_key_env)
                if not key:
                    continue
                headers["authorization"] = f"Bearer {key}"
            response = None
            try:
                counter_payload = {**payload, "model": endpoint.model, "stream": False}
                counter_payload.pop("stream_options", None)
                outgoing = app.state.upstream_client.build_request(
                    "POST",
                    urljoin(endpoint.base_url, endpoint.tokenize_path),
                    json=counter_payload,
                    headers=headers,
                    timeout=httpx.Timeout(remaining, connect=min(remaining, 3.0)),
                )
                response = await app.state.upstream_client.send(outgoing, stream=True)
                if response.status_code != 200:
                    continue
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ValueError("Token counter response exceeds limit")
                    body.extend(chunk)
                result = json.loads(body)
                if (
                    not isinstance(result, dict)
                    or type(result.get("count")) is not int
                    or result["count"] < 0
                ):
                    raise ValueError("Invalid token count")
                if "max_model_len" in result and (
                    type(result["max_model_len"]) is not int
                    or result["max_model_len"] <= 0
                ):
                    raise ValueError("Invalid model context limit")
                counter_stats["qualified_counter_requests"] += 1
                # Tokenizers can report only media placeholder tokens. Expanded
                # image/audio/video work still needs a qualified separate budget.
                return result["count"] + _media_items(payload) * (
                    route.media_tokens_per_item or 0
                )
            except (httpx.HTTPError, ValueError, UnicodeError, RecursionError):
                continue
            finally:
                if response is not None:
                    with anyio.CancelScope(shield=True):
                        await response.aclose()
        counter_stats["conservative_fallbacks"] += 1
        return fallback

    async def count_input(route, payload, fallback):
        try:
            async with asyncio.timeout(TOKEN_COUNT_TIMEOUT_SECONDS):
                return await count_input_attempts(route, payload, fallback)
        except TimeoutError:
            counter_stats["conservative_fallbacks"] += 1
            return fallback

    async def dispatch(route, payload, input_tokens, output_tokens):
        input_tokens = await count_input(route, payload, input_tokens)
        # Retry at most once, on another configured endpoint, before returning
        # any upstream response bytes. The second admission shares the deadline.
        deadline = time.monotonic() + route.queue_timeout_seconds
        excluded: set[str] = set()
        for attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _error(
                    "Inference admission deadline exceeded.", 503, "pool_busy"
                )
            try:
                lease = await scheduler.acquire(
                    replace(route, queue_timeout_seconds=remaining),
                    input_tokens,
                    output_tokens,
                    excluded=frozenset(excluded),
                )
            except RequestTooLarge:
                return _error(
                    "Request exceeds the compatible endpoints' configured token or input limits.",
                    413,
                    "request_too_large",
                )
            except PoolBusy:
                return _error("Inference pool is busy; retry later.", 503, "pool_busy")
            except NoHealthyEndpoint:
                return _error(
                    "No eligible inference endpoint is available.",
                    503,
                    "no_healthy_endpoint",
                )

            endpoint = lease.endpoint
            excluded.add(endpoint.name)
            forwarded = dict(payload)
            forwarded["model"] = endpoint.model
            headers = {
                "content-type": "application/json",
                "accept-encoding": "identity",
            }
            if endpoint.api_key_env:
                key = os.environ.get(endpoint.api_key_env)
                if not key:
                    await lease.fail()
                    await lease.release()
                    return _error(
                        "Inference endpoint credential is unavailable.",
                        503,
                        "endpoint_unavailable",
                    )
                headers["authorization"] = f"Bearer {key}"
            client = app.state.upstream_client
            base = endpoint.base_url.rstrip("/")
            url = base + (
                "/chat/completions" if base.endswith("/v1") else "/v1/chat/completions"
            )
            upstream = None
            try:
                outgoing = client.build_request(
                    "POST", url, json=forwarded, headers=headers
                )
                upstream = await client.send(outgoing, stream=True)
            except httpx.HTTPError:
                await _dispose(
                    upstream, lease, config.cancellation_grace_seconds, failed=True
                )
                if attempt == 0 and len(excluded) < len(route.endpoints):
                    continue
                return _error(
                    "Inference endpoint did not respond.", 502, "upstream_unavailable"
                )
            except BaseException:
                await _dispose(upstream, lease, config.cancellation_grace_seconds)
                raise

            if upstream.status_code >= 500:
                await _dispose(upstream, lease, failed=True)
                if attempt == 0 and len(excluded) < len(route.endpoints):
                    continue
                return _error("Inference endpoint failed.", 502, "upstream_unavailable")
            if not 200 <= upstream.status_code < 300:
                status_code = (
                    upstream.status_code
                    if upstream.status_code in (400, 413, 422, 429)
                    else 502
                )
                await _dispose(
                    upstream, lease, failed=upstream.status_code in (401, 403, 429)
                )
                return _error(
                    "Inference endpoint rejected the request.",
                    status_code,
                    "upstream_rejected",
                )
            if payload.get("stream", False):
                return _OwnedStream(upstream, lease, config.cancellation_grace_seconds)

            completed = False
            failed = False
            try:
                body = bytearray()
                async for chunk in upstream.aiter_raw():
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        failed = True
                        return _error(
                            "Inference response exceeded the size limit.",
                            502,
                            "invalid_upstream_response",
                        )
                    body.extend(chunk)
                completed = True
                return Response(
                    bytes(body),
                    status_code=upstream.status_code,
                    headers=_response_headers(upstream),
                )
            except httpx.HTTPError:
                failed = True
                return _error(
                    "Inference response was interrupted.", 502, "upstream_unavailable"
                )
            finally:
                await _dispose(
                    upstream,
                    lease,
                    0 if completed else config.cancellation_grace_seconds,
                    failed=failed,
                )

        return _error(
            "No eligible inference endpoint is available.", 503, "no_healthy_endpoint"
        )

    @app.post("/{route_name}/v1/chat/completions")
    async def completions(route_name: str, request: Request):
        route = config.routes.get(route_name)
        if route is None:
            return _error("Unknown route.", 404, "unknown_route")
        try:
            payload, input_tokens, output_tokens = await _read_payload(request, route)
        except InvalidRequest as exc:
            return _error(str(exc), exc.status, "invalid_request")

        async def disconnected():
            while True:
                message = await request.receive()
                if message["type"] == "http.disconnect":
                    return

        # ASGI does not cancel a handler merely because the caller left. Watch
        # for disconnect during admission, upstream headers and nonstream reads.
        # Once a streaming response is returned it owns disconnect handling.
        work = asyncio.create_task(
            dispatch(route, payload, input_tokens, output_tokens)
        )
        watcher = asyncio.create_task(disconnected())
        transferred = False
        try:
            done, _ = await asyncio.wait(
                (work, watcher), return_when=asyncio.FIRST_COMPLETED
            )
            if watcher in done:
                return Response(status_code=499)
            response = work.result()
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            # No await may follow ownership transfer: cancellation while waiting
            # for watcher cleanup still belongs to this handler, not ASGI.
            transferred = True
            return response
        finally:
            if not transferred:
                watcher.cancel()
                if not work.done():
                    work.cancel()
                with anyio.CancelScope(shield=True):
                    await asyncio.gather(work, watcher, return_exceptions=True)
                    if work.done() and not work.cancelled() and work.exception() is None:
                        abandoned = work.result()
                        if isinstance(abandoned, _OwnedStream):
                            await _dispose(
                                abandoned.upstream,
                                abandoned.lease,
                                config.cancellation_grace_seconds,
                            )

    return app


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a configured model admission pool (one worker)."
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate configuration and exit without connecting to endpoints.",
    )
    args = parser.parse_args()
    config = load_config(args.config)
    if args.check:
        print("Inference pool configuration is valid.")
        return
    import uvicorn

    uvicorn.run(
        create_app(config), host=args.host, port=args.port, workers=1, access_log=False
    )


if __name__ == "__main__":
    main()
