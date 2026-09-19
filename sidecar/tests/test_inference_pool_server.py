"""Behavioral tests with local mock transports; no model/network calls."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json

import httpx
import pytest

from protagine.inference_pool.config import parse_config
from protagine.inference_pool import server


def config(**route_options):
    return parse_config(
        {
            "endpoints": {
                "one": {
                    "base_url": "http://one.test/v1",
                    "model": "processor-one",
                    "max_requests": 4,
                    "max_tokens": 100_000,
                    "context_tokens": 40_000,
                },
                "two": {
                    "base_url": "http://two.test",
                    "model": "processor-two",
                    "max_requests": 4,
                    "max_tokens": 100_000,
                    "context_tokens": 40_000,
                },
            },
            "routes": {
                "chat": {
                    "model": "agent-chat",
                    "endpoints": ["one", "two"],
                    "traffic_class": "interactive",
                    "default_output_tokens": 64,
                    "max_output_tokens": 256,
                    "queue_timeout_seconds": 0.2,
                    **route_options,
                }
            },
            "cancellation_grace_seconds": 0,
            "cooldown_seconds": 30,
        }
    )


def payload(**changes):
    return {
        "model": "agent-chat",
        "messages": [{"role": "user", "content": "Hello"}],
        **changes,
    }


def counted_config(**route_options):
    cfg = config(**route_options)
    return replace(
        cfg,
        endpoints={
            name: replace(endpoint, tokenize_path="/v1/tokenize")
            for name, endpoint in cfg.endpoints.items()
        },
    )


class Bytes(httpx.AsyncByteStream):
    def __init__(self, *chunks, error=None, gate=None):
        self.chunks = chunks
        self.error = error
        self.gate = gate
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error

    async def aclose(self):
        self.closed = True


def upstream_response(body=b'{"choices":[],"usage":{"total_tokens":5}}', **kwargs):
    return httpx.Response(
        200, stream=Bytes(body), headers={"content-type": "application/json"}, **kwargs
    )


@asynccontextmanager
async def client_for(handler, cfg=None):
    app = server.create_app(cfg or config(), transport=httpx.MockTransport(handler))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://pool.test"
        ) as client:
            yield app, client


@pytest.mark.asyncio
async def test_forwarding_preserves_tools_reasoning_and_usage_and_bounds_output():
    seen = []

    async def handler(request):
        seen.append(request)
        return upstream_response()

    request_body = payload(
        reasoning_effort="high",
        thinking={"type": "enabled"},
        tools=[
            {
                "type": "function",
                "function": {"name": "lookup", "parameters": {"type": "object"}},
            }
        ],
        temperature=0.3,
    )
    async with client_for(handler) as (app, client):
        result = await client.post("/chat/v1/chat/completions", json=request_body)
        assert result.status_code == 200
        assert result.json()["usage"] == {"total_tokens": 5}
        forwarded = json.loads(seen[0].content)
        assert forwarded == {**request_body, "model": "processor-one", "max_tokens": 64}
        assert str(seen[0].url) == "http://one.test/v1/chat/completions"
        assert app.state.scheduler.status()["active_requests"] == 0
        assert app.state.scheduler.status()["active_tokens"] == 0


@pytest.mark.asyncio
async def test_raw_sse_forwards_reasoning_usage_and_done_unchanged():
    chunks = [
        b'data: {"choices":[{"delta":{"reasoning_content":"Check"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n',
        b'data: {"choices":[],"usage":{"total_tokens":6}}\n\ndata: [DONE]\n\n',
    ]
    stream = Bytes(*chunks)

    async def handler(request):
        assert json.loads(request.content)["stream_options"] == {"include_usage": True}
        return httpx.Response(
            200, stream=stream, headers={"content-type": "text/event-stream"}
        )

    async with client_for(handler) as (app, client):
        result = await client.post(
            "/chat/v1/chat/completions",
            json=payload(stream=True, stream_options={"include_usage": True}),
        )
        assert result.content == b"".join(chunks)
        assert result.headers["content-type"] == "text/event-stream"
        assert stream.closed
        assert app.state.scheduler.status()["active_requests"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["connect", "500"])
async def test_one_failover_before_response_rewrites_only_model(failure):
    seen = []

    async def handler(request):
        seen.append(request)
        if len(seen) == 1:
            if failure == "connect":
                raise httpx.ConnectError(
                    "upstream-secret-must-not-leak", request=request
                )
            return httpx.Response(500, stream=Bytes(b"upstream-secret-must-not-leak"))
        return upstream_response()

    async with client_for(handler) as (app, client):
        result = await client.post(
            "/chat/v1/chat/completions", json=payload(max_completion_tokens=100)
        )
        assert result.status_code == 200
        assert [str(r.url) for r in seen] == [
            "http://one.test/v1/chat/completions",
            "http://two.test/v1/chat/completions",
        ]
        assert [json.loads(r.content)["model"] for r in seen] == [
            "processor-one",
            "processor-two",
        ]
        assert all(json.loads(r.content)["max_completion_tokens"] == 100 for r in seen)
        assert "max_tokens" not in json.loads(seen[-1].content)
        status = app.state.scheduler.status()
        assert status["active_requests"] == 0
        assert status["endpoints"]["one"]["failures"] == 1


@pytest.mark.asyncio
async def test_stream_failure_never_retries_or_discloses_error():
    seen = []
    stream = Bytes(
        b'data: {"partial":true}\n\n', error=httpx.ReadError("secret provider message")
    )

    async def handler(request):
        seen.append(request)
        return httpx.Response(
            200, stream=stream, headers={"content-type": "text/event-stream"}
        )

    async with client_for(handler) as (app, client):
        with pytest.raises(Exception) as error:
            await client.post("/chat/v1/chat/completions", json=payload(stream=True))
        assert "secret provider message" not in str(error.value)
        assert len(seen) == 1
        assert stream.closed
        assert app.state.scheduler.status()["active_requests"] == 0
        assert app.state.scheduler.status()["endpoints"]["one"]["failures"] == 1


@pytest.mark.asyncio
async def test_asgi_disconnect_closes_upstream_before_releasing_lease():
    first_sent = asyncio.Event()
    never = asyncio.Event()
    observations = []
    app = None

    class WatchedStream(Bytes):
        async def aclose(self):
            observations.append(app.state.scheduler.status()["active_requests"])
            await super().aclose()

    stream = WatchedStream(b"data: first\n\n", gate=never)

    async def handler(request):
        return httpx.Response(
            200, stream=stream, headers={"content-type": "text/event-stream"}
        )

    app = server.create_app(config(), transport=httpx.MockTransport(handler))
    body = json.dumps(payload(stream=True)).encode()
    initial = True

    async def receive():
        nonlocal initial
        if initial:
            initial = False
            return {"type": "http.request", "body": body, "more_body": False}
        await first_sent.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            first_sent.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/chat/v1/chat/completions",
        "raw_path": b"/chat/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1),
        "server": ("pool.test", 80),
    }
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(app(scope, receive, send), 2)
        assert observations == [1]
        assert stream.closed
        assert app.state.scheduler.status()["active_requests"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"model": "unqualified-model"},
        {"max_tokens": 257},
        {"max_tokens": True},
        {"n": 2},
        {"stream": "true"},
        {"messages": []},
    ],
)
async def test_invalid_requests_never_reach_a_model(change):
    async def handler(request):
        pytest.fail("Invalid request reached upstream")

    async with client_for(handler) as (app, client):
        result = await client.post("/chat/v1/chat/completions", json=payload(**change))
        assert result.status_code == 400
        assert app.state.scheduler.status()["active_requests"] == 0


@pytest.mark.asyncio
async def test_caller_cannot_set_engine_priority():
    async def handler(request):
        pytest.fail("Caller priority reached upstream")

    async with client_for(handler) as (app, client):
        result = await client.post(
            "/chat/v1/chat/completions", json=payload(priority=9999)
        )
        assert result.status_code == 422
        assert "assigned by the configured route" in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["queue", "headers", "nonstream"])
async def test_disconnect_cancels_queued_headers_and_nonstream_work(stage):
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    never = asyncio.Event()
    seen = []

    class BlockedBody(Bytes):
        async def __aiter__(self):
            entered.set()
            yield b'{"partial":'
            await never.wait()

    stream = BlockedBody()

    async def handler(request):
        seen.append(request)
        if stage == "headers":
            entered.set()
            try:
                await never.wait()
            finally:
                cancelled.set()
        return httpx.Response(200, stream=stream)

    cfg = config(endpoints=["one"], queue_timeout_seconds=1)
    cfg = replace(cfg, endpoints={"one": replace(cfg.endpoints["one"], max_requests=1)})
    app = server.create_app(cfg, transport=httpx.MockTransport(handler))
    body = json.dumps(payload()).encode()
    initial = True

    async def receive():
        nonlocal initial
        if initial:
            initial = False
            return {"type": "http.request", "body": body, "more_body": False}
        if stage == "queue":
            while app.state.scheduler.status()["queued_requests"] == 0:
                await asyncio.sleep(0)
        else:
            await entered.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/chat/v1/chat/completions",
        "raw_path": b"/chat/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1),
        "server": ("pool.test", 80),
    }
    async with app.router.lifespan_context(app):
        held = (
            await app.state.scheduler.acquire(cfg.routes["chat"], 1, 1)
            if stage == "queue"
            else None
        )
        await asyncio.wait_for(app(scope, receive, send), 2)
        assert app.state.scheduler.status()["queued_requests"] == 0
        if held:
            assert not seen
            await held.release()
            await asyncio.sleep(0)
            assert not seen
        elif stage == "headers":
            assert cancelled.is_set()
        else:
            assert stream.closed
        assert app.state.scheduler.status()["active_requests"] == 0
        assert app.state.scheduler.status()["active_tokens"] == 0


@pytest.mark.asyncio
async def test_upstream_bad_request_is_sanitized_without_retry():
    seen = []

    async def handler(request):
        seen.append(request)
        return httpx.Response(400, stream=Bytes(b"private upstream diagnostics"))

    async with client_for(handler) as (app, client):
        result = await client.post("/chat/v1/chat/completions", json=payload())
        assert result.status_code == 400
        assert result.json()["error"]["code"] == "upstream_rejected"
        assert "private upstream" not in result.text
        assert len(seen) == 1
        assert app.state.scheduler.status()["active_requests"] == 0


@pytest.mark.asyncio
async def test_malformed_json_body_limit_and_context_limit(monkeypatch):
    async def handler(request):
        pytest.fail("Invalid request reached upstream")

    async with client_for(handler) as (app, client):
        assert (
            await client.post("/chat/v1/chat/completions", content=b"{")
        ).status_code == 400
        assert (
            await client.post("/chat/v1/chat/completions", content=b'{"value":NaN}')
        ).status_code == 400
        result = await client.post(
            "/chat/v1/chat/completions",
            json=payload(messages=[{"role": "user", "content": "x" * 40_000}]),
        )
        assert result.status_code == 413
        monkeypatch.setattr(server, "MAX_BODY_BYTES", 128)
        result = await client.post("/chat/v1/chat/completions", content=b"x" * 129)
        assert result.status_code == 413

        # Enforce the actual streamed size even if Content-Length is omitted.
        async def chunks():
            yield b"x" * 100
            yield b"x" * 29

        result = await client.post("/chat/v1/chat/completions", content=chunks())
        assert result.status_code == 413


@pytest.mark.asyncio
async def test_media_requires_explicit_budget_then_preserves_payload():
    seen = []
    body = payload(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "https://example.test/image.png",
                            "detail": "high",
                        },
                    },
                ],
            }
        ]
    )

    async def handler(request):
        seen.append(json.loads(request.content))
        return upstream_response()

    async with client_for(handler) as (app, client):
        result = await client.post("/chat/v1/chat/completions", json=body)
        assert result.status_code == 400
        assert not seen
    cfg = config(media_tokens_per_item=4096, max_media_items=1)
    assert server.estimate_input_tokens(body, cfg.routes["chat"]) >= 4096
    async with client_for(handler, cfg) as (app, client):
        assert (
            await client.post("/chat/v1/chat/completions", json=body)
        ).status_code == 200
        assert seen[0]["messages"] == body["messages"]
        body["messages"][0]["content"].append(body["messages"][0]["content"][-1])
        assert (
            await client.post("/chat/v1/chat/completions", json=body)
        ).status_code == 413
        assert len(seen) == 1


@pytest.mark.asyncio
async def test_errors_and_status_never_expose_upstream_credentials(monkeypatch):
    monkeypatch.setenv("POOL_TEST_SECRET", "private-upstream-key")
    cfg = config()
    cfg = replace(
        cfg,
        endpoints={
            name: replace(endpoint, api_key_env="POOL_TEST_SECRET")
            for name, endpoint in cfg.endpoints.items()
        },
    )

    async def handler(request):
        assert request.headers["authorization"] == "Bearer private-upstream-key"
        return httpx.Response(500, stream=Bytes(b"private-upstream-key and prompt"))

    async with client_for(handler, cfg) as (app, client):
        result = await client.post(
            "/chat/v1/chat/completions",
            json=payload(),
            headers={"authorization": "Bearer caller-credential"},
        )
        assert result.status_code == 502
        status = await client.get("/status")
        assert "private-upstream-key" not in result.text + status.text
        assert "POOL_TEST_SECRET" not in result.text + status.text
        assert "caller-credential" not in result.text + status.text
        assert "one.test" not in status.text


@pytest.mark.asyncio
async def test_models_and_health_are_local_and_unknown_route_fails():
    async def handler(request):
        pytest.fail("Metadata request reached upstream")

    async with client_for(handler) as (app, client):
        result = await client.get("/chat/v1/models")
        assert result.json()["data"][0]["id"] == "agent-chat"
        assert (await client.get("/health")).json()["engine_health_verified"] is False
        assert (await client.get("/unknown/v1/models")).status_code == 404
        assert (
            await client.post("/unknown/v1/chat/completions", json=payload())
        ).status_code == 404


@pytest.mark.asyncio
async def test_qualified_counter_admits_long_text_and_receives_template_fields():
    seen = []
    request_body = payload(
        messages=[{"role": "user", "content": "readable long input " * 2500}],
        tools=[
            {
                "type": "function",
                "function": {"name": "lookup", "parameters": {"type": "object"}},
            }
        ],
        chat_template_kwargs={"thinking": True},
        thinking={"type": "enabled"},
    )

    async def handler(request):
        seen.append(request)
        if request.url.path == "/v1/tokenize":
            return httpx.Response(200, json={"count": 9000, "max_model_len": 40_000})
        return upstream_response()

    cfg = counted_config()
    assert server.estimate_input_tokens(request_body, cfg.routes["chat"]) > 40_000
    async with client_for(handler, cfg) as (app, client):
        response = await client.post("/chat/v1/chat/completions", json=request_body)
        assert response.status_code == 200
        assert [request.url.path for request in seen] == [
            "/v1/tokenize",
            "/v1/chat/completions",
        ]
        assert json.loads(seen[0].content) == {
            **json.loads(seen[1].content), "stream": False
        }
        assert json.loads(seen[0].content)["chat_template_kwargs"] == {"thinking": True}
        stats = (await client.get("/status")).json()["token_counting"]
        assert stats == {"qualified_counter_requests": 1, "conservative_fallbacks": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "counter_result",
    [
        {"count": True},
        {"count": -1},
        {"count": 1, "max_model_len": 0},
        {"tokens": [1, 2]},
    ],
)
async def test_bad_counter_uses_conservative_fallback(counter_result):
    seen = []

    async def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/v1/tokenize":
            return httpx.Response(200, json=counter_result)
        return upstream_response()

    async with client_for(handler, counted_config()) as (app, client):
        assert (
            await client.post("/chat/v1/chat/completions", json=payload())
        ).status_code == 200
        assert seen == ["/v1/tokenize", "/v1/tokenize", "/v1/chat/completions"]
        assert (await client.get("/status")).json()["token_counting"][
            "conservative_fallbacks"
        ] == 1


@pytest.mark.asyncio
async def test_counter_timeout_is_bounded_and_falls_back(monkeypatch):
    monkeypatch.setattr(server, "TOKEN_COUNT_TIMEOUT_SECONDS", 0.01)
    cancelled = asyncio.Event()

    async def handler(request):
        if request.url.path == "/v1/tokenize":
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return upstream_response()

    async with client_for(handler, counted_config()) as (app, client):
        result = await asyncio.wait_for(
            client.post("/chat/v1/chat/completions", json=payload()), 1
        )
        assert result.status_code == 200
        assert cancelled.is_set()
        assert (await client.get("/status")).json()["token_counting"][
            "conservative_fallbacks"
        ] == 1


@pytest.mark.asyncio
async def test_streaming_generation_uses_nonstreaming_counter_copy():
    seen = []
    wire = b'data: {"usage":{"total_tokens":7}}\n\ndata: [DONE]\n\n'

    async def handler(request):
        body = json.loads(request.content)
        seen.append((request.url.path, body))
        if request.url.path == "/v1/tokenize":
            if body.get("stream"):
                return httpx.Response(501, json={"error": "Streaming count unsupported"})
            assert body["stream"] is False
            assert "stream_options" not in body
            return httpx.Response(200, json={"count": 6, "max_model_len": 40_000})
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        return httpx.Response(
            200, stream=Bytes(wire), headers={"content-type": "text/event-stream"}
        )

    async with client_for(handler, counted_config()) as (app, client):
        result = await client.post(
            "/chat/v1/chat/completions",
            json=payload(stream=True, stream_options={"include_usage": True}),
        )
        assert result.content == wire
        assert len(seen) == 2
        stats = (await client.get("/status")).json()["token_counting"]
        assert stats == {"qualified_counter_requests": 1, "conservative_fallbacks": 0}


@pytest.mark.asyncio
async def test_counter_counts_placeholder_plus_qualified_media_budget():
    body = payload(
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                ],
            }
        ]
    )
    seen = []
    app = None

    async def handler(request):
        seen.append(request)
        if request.url.path == "/v1/tokenize":
            return httpx.Response(200, json={"count": 20, "max_model_len": 40_000})
        assert app.state.scheduler.status()["active_tokens"] == 20 + 4096 + 64
        assert json.loads(request.content)["messages"] == body["messages"]
        return upstream_response()

    async with client_for(handler, counted_config()) as (app, client):
        assert (
            await client.post("/chat/v1/chat/completions", json=body)
        ).status_code == 400
        assert not seen
    async with client_for(handler, counted_config(media_tokens_per_item=4096)) as (
        app,
        client,
    ):
        assert (
            await client.post("/chat/v1/chat/completions", json=body)
        ).status_code == 200
        assert len(seen) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["after_dispatch", "watcher_cleanup"])
async def test_external_cancel_before_stream_handoff_releases_owned_response(
    monkeypatch, stage
):
    stream = Bytes(b"data: never handed off\n\n")
    watcher_closing = asyncio.Event()
    never = asyncio.Event()

    async def handler(request):
        return httpx.Response(
            200, stream=stream, headers={"content-type": "text/event-stream"}
        )

    if stage == "after_dispatch":
        original_wait = asyncio.wait

        async def cancel_before_resume(*args, **kwargs):
            result = await original_wait(*args, **kwargs)
            # The dispatch task now owns a complete _OwnedStream, but its
            # handler has not resumed to hand that response to ASGI.
            assert any(
                isinstance(task.result(), server._OwnedStream)
                for task in result[0]
                if not task.cancelled() and task.exception() is None
            )
            asyncio.current_task().cancel()
            await asyncio.sleep(0)
            return result

        monkeypatch.setattr(server.asyncio, "wait", cancel_before_resume)

    app = server.create_app(config(), transport=httpx.MockTransport(handler))
    request_body = json.dumps(payload(stream=True)).encode()
    initial = True

    async def receive():
        nonlocal initial
        if initial:
            initial = False
            return {"type": "http.request", "body": request_body, "more_body": False}
        try:
            await never.wait()
        except asyncio.CancelledError:
            if stage == "watcher_cleanup":
                watcher_closing.set()
                await never.wait()
            raise

    async def send(message):
        pytest.fail("Cancelled request must not hand the response to ASGI")

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/chat/v1/chat/completions", "raw_path": b"/chat/v1/chat/completions",
        "query_string": b"", "root_path": "", "headers": [],
        "client": ("127.0.0.1", 1), "server": ("pool.test", 80),
    }
    async with app.router.lifespan_context(app):
        request_task = asyncio.create_task(app(scope, receive, send))
        if stage == "watcher_cleanup":
            await asyncio.wait_for(watcher_closing.wait(), 1)
            request_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request_task, 1)
        assert stream.closed
        assert app.state.scheduler.status()["active_requests"] == 0
        assert app.state.scheduler.status()["active_tokens"] == 0
