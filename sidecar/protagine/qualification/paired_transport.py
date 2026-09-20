"""Observe physical model requests without editing their content or responses."""
from contextlib import contextmanager, ExitStack
import json
import time
from unittest.mock import patch

TIMING_PROTOCOL = 'paired-transport-2'


def _text(value):
    if isinstance(value, str):
        return bool(value)
    return isinstance(value, list) and any(isinstance(part, dict)
        and isinstance(part.get('text'), str) and bool(part['text']) for part in value)


def _payload_kinds(value):
    """Recognize generated payloads, never role declarations or tool-call IDs."""
    generated = content = False
    choices = value.get('choices')
    for choice in choices if isinstance(choices, list) else []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get('delta') or {}
        if not isinstance(delta, dict):
            continue
        visible = _text(delta.get('content'))
        reasoning = any(_text(delta.get(key)) for key in ('reasoning_content', 'reasoning', 'reasoning_text'))
        calls = delta.get('tool_calls')
        calls = calls if isinstance(calls, list) else []
        if isinstance(delta.get('function_call'), dict):
            calls = [*calls, {'function': delta['function_call']}]
        tool = any(isinstance(call, dict) and isinstance(call.get('function'), dict)
            and any(_text(call['function'].get(key)) for key in ('name', 'arguments')) for call in calls)
        content = content or visible
        generated = generated or visible or reasoning or tool
    event = value.get('type')
    if event == 'response.output_text.delta' and _text(value.get('delta')):
        generated = content = True
    elif event in {'response.reasoning_text.delta', 'response.reasoning_summary_text.delta',
                   'response.function_call_arguments.delta'} and _text(value.get('delta')):
        generated = True
    return generated, content


def usage_summary(rows):
    known = [row for row in rows if isinstance(row.get('usage'), dict)
        and all(type(row['usage'].get(k)) is int and row['usage'][k] >= 0
                for k in ('prompt_tokens', 'completion_tokens'))]
    return {'coverage': 'partial', 'basis': 'httpx sync and async model calls only; '
            'other transports and provider-side work are not independently observed',
        'total_model_calls': None, 'input_tokens': None, 'output_tokens': None,
        'background_model_calls': None, 'observed_model_calls': len(rows),
        'model_calls_with_usage': len(known),
        'observed_input_tokens': sum(r['usage']['prompt_tokens'] for r in known) if known else None,
        'observed_output_tokens': sum(r['usage']['completion_tokens'] for r in known) if known else None,
        'budget_enforcement': 'native per-call caps and episode deadline; total work not enforced'}


@contextmanager
def observe_requests(base_url):
    import httpx
    rows = []
    prefix = base_url.rstrip('/') + '/'

    def begin(request):
        if request.method != 'POST' or not str(request.url).startswith(prefix):
            return None
        try:
            body = json.loads(request.content)
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(body, dict) or not any(k in body for k in ('messages', 'input')):
            return None
        row = {'model': body.get('model'), 'returned_models': [], 'usage': None,
               'started_monotonic': time.monotonic(), 'requested_max_tokens': body.get('max_tokens'),
               'timing_protocol': TIMING_PROTOCOL, 'response_mode': None,
               'first_generated_ms': None, 'first_content_ms': None,
               'elapsed_ms': None, 'response_complete': False, 'termination': None}
        rows.append(row)
        return row

    class Parser:
        def __init__(self, row, sse, *, live_stream):
            self.row, self.sse, self.buffer = row, sse, b''
            self.live_stream = live_stream
            self.finished, self.terminal_seen, self.json_document_seen = False, False, False
            row['response_mode'] = ('sse' if live_stream else 'buffered_sse') if sse else 'buffered_json'

        def consume(self, raw):
            if raw.strip() == b'[DONE]':
                self.terminal_seen = True
                return
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                return
            if not isinstance(value, dict):
                return
            self.json_document_seen = True
            envelope = value.get('response') if isinstance(value.get('response'), dict) else value
            model = envelope.get('model')
            if isinstance(model, str) and model not in self.row['returned_models']:
                self.row['returned_models'].append(model)
            usage = envelope.get('usage')
            if isinstance(usage, dict):
                self.row['usage'] = usage
            if value.get('type') == 'response.completed':
                self.terminal_seen = True
            choices = value.get('choices')
            if isinstance(choices, list) and any(isinstance(choice, dict)
                    and choice.get('finish_reason') is not None for choice in choices):
                self.terminal_seen = True
            if self.sse and self.live_stream:
                generated, content = _payload_kinds(value)
                elapsed = round((time.monotonic()-self.row['started_monotonic'])*1000, 3)
                if generated and self.row['first_generated_ms'] is None:
                    self.row['first_generated_ms'] = elapsed
                if content and self.row['first_content_ms'] is None:
                    self.row['first_content_ms'] = elapsed
            # Preserve this legacy diagnostic, but never call it TTFT. It may
            # be an empty role frame or an already buffered complete response.
            if value.get('choices') and 'first_chunk_ms' not in self.row:
                self.row['first_chunk_ms'] = round((time.monotonic()-self.row['started_monotonic'])*1000, 3)

        def feed(self, chunk):
            self.buffer += chunk
            if self.sse:
                while b'\n' in self.buffer:
                    line, self.buffer = self.buffer.split(b'\n', 1)
                    if line.startswith(b'data:'):
                        self.consume(line[5:].strip())
            if len(self.buffer) > 4194304:
                self.row['truncated'] = True
                self.buffer = b''

        def finish(self, reason):
            if self.finished:
                return
            self.finished = True
            if not self.sse:
                self.consume(self.buffer)
            elif self.buffer.strip().startswith(b'data:'):
                self.consume(self.buffer.strip()[5:].strip())
            self.row.update(elapsed_ms=round((time.monotonic()-self.row['started_monotonic'])*1000, 3),
                termination=reason, response_complete=(self.terminal_seen if self.sse else self.json_document_seen))

    class SyncStream(httpx.SyncByteStream):
        def __init__(self, stream, parser):
            self.stream, self.parser = stream, parser
        def __iter__(self):
            try:
                for chunk in self.stream:
                    self.parser.feed(chunk)
                    yield chunk
            except BaseException:
                self.parser.finish('stream_error')
                raise
            else:
                self.parser.finish('exhausted')
        def close(self):
            try:
                self.stream.close()
            finally:
                self.parser.finish('closed')

    class AsyncStream(httpx.AsyncByteStream):
        def __init__(self, stream, parser):
            self.stream, self.parser = stream, parser
        async def __aiter__(self):
            try:
                async for chunk in self.stream:
                    self.parser.feed(chunk)
                    yield chunk
            except BaseException:
                self.parser.finish('stream_error')
                raise
            else:
                self.parser.finish('exhausted')
        async def aclose(self):
            try:
                await self.stream.aclose()
            finally:
                self.parser.finish('closed')

    def record(response, row, wrapper):
        if row is not None:
            row['status'] = response.status_code
            parser = Parser(row, response.headers.get('content-type', '').startswith('text/event-stream'),
                            live_stream=not response.is_stream_consumed)
            if response.is_stream_consumed:
                parser.feed(response.content)
                parser.finish('buffered_body')
            else:
                response.stream = wrapper(response.stream, parser)
        return response

    original_sync, original_async = httpx.Client.send, httpx.AsyncClient.send
    def send(client, request, *args, **kwargs):
        row = begin(request)
        try:
            response = original_sync(client, request, *args, **kwargs)
        except BaseException:
            failed(row)
            raise
        return record(response, row, SyncStream)
    async def asend(client, request, *args, **kwargs):
        row = begin(request)
        try:
            response = await original_async(client, request, *args, **kwargs)
        except BaseException:
            failed(row)
            raise
        return record(response, row, AsyncStream)

    def failed(row):
        if row is not None:
            row.update(elapsed_ms=round((time.monotonic()-row['started_monotonic'])*1000, 3),
                       response_complete=False, termination='request_error')

    with ExitStack() as stack:
        stack.enter_context(patch.object(httpx.Client, 'send', send))
        stack.enter_context(patch.object(httpx.AsyncClient, 'send', asend))
        yield rows
