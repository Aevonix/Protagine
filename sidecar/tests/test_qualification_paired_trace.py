"""Private diagnostics preserve observed model traffic and bound retained data."""

import asyncio
import json

import httpx
import pytest

from protagine.qualification.paired_trace import DiagnosticTrace, MARKER, PROTOCOL
from protagine.qualification.paired_transport import observe_requests


_BASE_URL = "http://model.invalid/v1"


def _frame(value):
    return b"data: " + json.dumps(value).encode() + b"\r\n\r\n"


def _exchange(asynchronous, trace, body, chunks):
    sent, closed = [], []

    class SyncChunks(httpx.SyncByteStream):
        def __iter__(self):
            yield from chunks

        def close(self):
            closed.append("closed")

    class AsyncChunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in chunks:
                yield chunk

        async def aclose(self):
            closed.append("closed")

    def respond(request):
        sent.append(request.content)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
            stream=AsyncChunks() if asynchronous else SyncChunks())

    url = _BASE_URL + "/chat/completions?wire_token=private-url-canary"
    headers = {"Authorization": "Bearer fixture-provider-secret",
               "X-Private": "private-header-canary"}
    with observe_requests(_BASE_URL, diagnostic=trace) as rows:
        if asynchronous:
            async def query():
                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                    request = client.build_request("POST", url, content=body, headers=headers)
                    response = await client.send(request, stream=True)
                    actual = [chunk async for chunk in response.aiter_raw()]
                    await response.aclose()
                    await response.aclose()
                    return actual
            actual = asyncio.run(query())
        else:
            with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                request = client.build_request("POST", url, content=body, headers=headers)
                response = client.send(request, stream=True)
                actual = list(response.iter_raw())
                response.close()
                response.close()
    assert sent == [body]
    assert actual == chunks
    assert closed == ["closed"]
    return rows


@pytest.mark.parametrize("asynchronous", [False, True])
def test_trace_captures_private_context_tools_and_stream_without_wire_metadata(asynchronous):
    lines = []
    trace = DiagnosticTrace(sink=lines.append, secrets=["fixture-provider-secret"])
    payload = {"model": "candidate", "messages": [
        {"role": "system", "content": "Injected context: synthetic-owner owns two lamps."},
        {"role": "user", "content": "Check fixture-provider-secret."},
        {"role": "assistant", "tool_calls": [{"id": "call-1", "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"facts.txt"}'}}]},
        {"role": "tool", "tool_call_id": "call-1", "content": "Native tool return: two lamps."},
    ], "tools": [{"type": "function", "function": {"name": "read_file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}],
        "max_tokens": 128, "stream": True,
        "headers": {"authorization": "excluded-body-auth"}, "url": "excluded-body-url"}
    events = [
        {"model": "served", "choices": [{"delta": {"tool_calls": [{"index": 0,
            "function": {"name": "read_file", "arguments": '{"path":"facts.txt"}'}}]}}]},
        {"model": "served", "choices": [{"delta": {"content": "fixture-provider-secret result"},
            "finish_reason": "stop"}]},
    ]
    raw = b"".join(_frame(event) for event in events) + b"data: [DONE]\n\n"
    rows = _exchange(asynchronous, trace, json.dumps(payload).encode(), [raw[:13], raw[13:57], raw[57:]])
    request, response = [json.loads(line) for line in lines]
    expected = {key: value for key, value in payload.items() if key not in {"headers", "url"}}
    expected = json.loads(json.dumps(expected).replace("fixture-provider-secret", "[REDACTED]"))
    assert request["kind"] == "model_request" and request["data"]["payload"] == expected
    assert response["kind"] == "model_response"
    assert response["data"]["events"] == json.loads(
        json.dumps(events).replace("fixture-provider-secret", "[REDACTED]"))
    assert response["data"]["complete"] is True
    assert response["data"]["request_id"] == request["data"]["request_id"]
    assert rows[0]["response_complete"] is True and rows[0]["returned_models"] == ["served"]
    recorded = "\n".join(lines)
    for excluded in ("fixture-provider-secret", "private-header-canary", "private-url-canary",
                     "excluded-body-auth", "excluded-body-url", _BASE_URL, "Authorization"):
        assert excluded not in recorded


def test_concurrent_requests_keep_response_ids_when_completion_order_reverses():
    lines = []
    trace = DiagnosticTrace(sink=lines.append)

    async def query():
        first_started, second_finished = asyncio.Event(), asyncio.Event()

        async def respond(request):
            model = json.loads(request.content)["model"]
            if model == "first":
                first_started.set()
                await second_finished.wait()
            return httpx.Response(200, json={"model": model, "output": model + " result"})

        with observe_requests(_BASE_URL, diagnostic=trace) as rows:
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                first = asyncio.create_task(client.post(_BASE_URL + "/responses",
                    json={"model": "first", "input": "first request"}))
                await first_started.wait()
                second = await client.post(_BASE_URL + "/responses",
                    json={"model": "second", "input": "second request"})
                assert second.json()["model"] == "second"
                second_finished.set()
                assert (await first).json()["model"] == "first"
        return rows

    rows = asyncio.run(query())
    events = [json.loads(line) for line in lines]
    requests = {event["data"]["payload"]["model"]: event["data"]["request_id"]
                for event in events if event["kind"] == "model_request"}
    responses = [event["data"] for event in events if event["kind"] == "model_response"]
    assert len(set(requests.values())) == 2
    assert [event["events"][0]["model"] for event in responses] == ["second", "first"]
    for event in responses:
        assert event["request_id"] == requests[event["events"][0]["model"]]
    assert [row["trace_request_id"] for row in rows] == [requests["first"], requests["second"]]


def test_diagnostic_bounds_and_secret_redaction_report_retention_limits():
    lines = []
    trace = DiagnosticTrace(sink=lines.append, secrets=["configured-secret"],
        max_bytes=1800, event_bytes=256)
    trace.add_secret("added-secret")
    trace.record("native_turn", {"api_key": "unregistered-key", "headers": {"X-Key": "hidden"},
        "messages": ["configured-secret and added-secret"]})
    trace.record("oversized", {"messages": ["configured-secret " + "x" * 4000]})
    for index in range(30):
        trace.record("later", {"index": index, "content": "bounded"})
    events = [json.loads(line) for line in lines]
    assert events[0]["data"] == {"api_key": "[REDACTED]", "headers": "[REDACTED]",
        "messages": ["[REDACTED] and [REDACTED]"]}
    assert events[1]["data"]["truncated"] is True
    assert events[1]["data"]["original_bytes"] > trace.event_bytes
    assert all(event["protocol"] == PROTOCOL for event in events)
    summary = trace.summary()
    assert summary["truncated"] == 1 and summary["dropped"] > 0 and summary["errors"] == 0
    assert summary["events"] == len(lines)
    assert summary["bytes"] == sum(len(line.encode()) + 1 for line in lines) <= trace.max_bytes
    assert "configured-secret" not in "".join(lines) and "added-secret" not in "".join(lines)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_broken_diagnostic_sink_preserves_response_bytes_and_close(asynchronous):
    def unavailable(_line):
        raise OSError("private diagnostic storage unavailable")

    trace = DiagnosticTrace(sink=unavailable)
    chunks = [_frame({"choices": [{"delta": {"content": "answer"}}]}), b"data: [DONE]\n\n"]
    rows = _exchange(asynchronous, trace, b'{"messages":[],"stream":true}', chunks)
    assert rows[0]["response_complete"] is True
    assert trace.summary()["errors"] == 2
    assert trace.summary()["events"] == 0


def test_oversized_response_capture_is_marked_without_truncating_delivered_bytes():
    lines = []
    trace = DiagnosticTrace(sink=lines.append)
    event = {"choices": [{"delta": {"content": "x" * (400 * 1024)}}]}
    chunks = [_frame(event), b"data: [DONE]\n\n"]
    rows = _exchange(False, trace, b'{"messages":[],"stream":true}', chunks)
    response = json.loads(lines[-1])["data"]
    assert response["truncated"] is True and response["events"] == []
    assert response["complete"] is True and rows[0]["response_complete"] is True


def test_private_trace_extraction_keeps_only_marked_events_with_private_mode(tmp_path):
    from protagine.qualification.paired_container import _extract_diagnostics

    lines = []
    trace = DiagnosticTrace(sink=lines.append)
    trace.record("native_turn", {"messages": [{"role": "tool", "content": "synthetic fact"}]})
    trace.record("context_route", {"route": "/v1/host/context/assemble", "status": 200})
    log = tmp_path / "container.log"
    log.write_text("ordinary progress\n" + MARKER + lines[0] + "\n"
        + 'PROTAGINE_PAIRED_RESULT:{"output":"scored answer"}\n'
        + "embedded " + MARKER + "not a diagnostic event\n"
        + MARKER + lines[1] + "\n")
    extracted = _extract_diagnostics(log)
    assert extracted == tmp_path / "private-trace.jsonl"
    assert extracted.read_text() == "\n".join(lines) + "\n"
    assert extracted.stat().st_mode & 0o777 == 0o600
