"""Observe physical model requests without editing their content or responses."""
from contextlib import contextmanager, ExitStack
import json
import time
from unittest.mock import patch


def usage_summary(rows):
    complete = bool(rows) and all(isinstance(row.get('usage'), dict)
        and all(type(row['usage'].get(k)) is int for k in ('prompt_tokens', 'completion_tokens'))
        for row in rows)
    return {'coverage': 'partial', 'basis': 'httpx sync and async model calls only; '
            'other transports and provider-side work are not independently observed',
        'total_model_calls': None, 'input_tokens': None, 'output_tokens': None,
        'background_model_calls': None, 'observed_model_calls': len(rows),
        'observed_input_tokens': sum(r['usage']['prompt_tokens'] for r in rows) if complete else None,
        'observed_output_tokens': sum(r['usage']['completion_tokens'] for r in rows) if complete else None,
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
               'started_monotonic': time.monotonic(), 'requested_max_tokens': body.get('max_tokens')}
        rows.append(row)
        return row

    class Parser:
        def __init__(self, row, sse):
            self.row, self.sse, self.buffer = row, sse, b''

        def consume(self, raw):
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                return
            if not isinstance(value, dict):
                return
            model = value.get('model')
            if isinstance(model, str) and model not in self.row['returned_models']:
                self.row['returned_models'].append(model)
            usage = value.get('usage')
            if isinstance(usage, dict):
                self.row['usage'] = usage
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

        def finish(self):
            if not self.sse:
                self.consume(self.buffer)
            self.row['elapsed_ms'] = round((time.monotonic()-self.row['started_monotonic'])*1000, 3)

    class SyncStream(httpx.SyncByteStream):
        def __init__(self, stream, parser):
            self.stream, self.parser = stream, parser
        def __iter__(self):
            for chunk in self.stream:
                self.parser.feed(chunk)
                yield chunk
            self.parser.finish()
        def close(self):
            self.stream.close()

    class AsyncStream(httpx.AsyncByteStream):
        def __init__(self, stream, parser):
            self.stream, self.parser = stream, parser
        async def __aiter__(self):
            async for chunk in self.stream:
                self.parser.feed(chunk)
                yield chunk
            self.parser.finish()
        async def aclose(self):
            await self.stream.aclose()

    def record(response, row, wrapper):
        if row is not None:
            row['status'] = response.status_code
            parser = Parser(row, response.headers.get('content-type', '').startswith('text/event-stream'))
            if response.is_stream_consumed:
                parser.feed(response.content)
                parser.finish()
            else:
                response.stream = wrapper(response.stream, parser)
        return response

    original_sync, original_async = httpx.Client.send, httpx.AsyncClient.send
    def send(client, request, *args, **kwargs):
        row = begin(request)
        return record(original_sync(client, request, *args, **kwargs), row, SyncStream)
    async def asend(client, request, *args, **kwargs):
        row = begin(request)
        return record(await original_async(client, request, *args, **kwargs), row, AsyncStream)

    with ExitStack() as stack:
        stack.enter_context(patch.object(httpx.Client, 'send', send))
        stack.enter_context(patch.object(httpx.AsyncClient, 'send', asend))
        yield rows
