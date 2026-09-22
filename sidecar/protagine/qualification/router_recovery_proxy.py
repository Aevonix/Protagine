"""Owned fault proxy; successful traffic uses the router's existing LAN client."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import socket
import time


@asynccontextmanager
async def fault_proxy(router, snapshot, binding, mode):
    from fastapi import FastAPI, Request
    from fastapi.responses import Response
    from protagine.router.router import _local_http_client
    import uvicorn
    app, rows, stopping = FastAPI(), [], asyncio.Event()

    @app.post('/v1/chat/completions')
    async def complete(request: Request):
        raw = await request.body()
        if len(raw) > 65536:
            return Response(status_code=413)
        payload = json.loads(raw)
        row = {'sequence': len(rows)+1, 'model': payload.get('model'),
            'body_sha256': hashlib.sha256(raw).hexdigest(), 'started': time.monotonic(),
            'forwarded': False, 'injected': None, 'status': None}
        rows.append(row)
        if mode == 'unavailable' or mode == 'recover' and len(rows) == 1:
            row.update(injected='http_503', status=503)
            return Response(json.dumps({'error': {'message': 'Owned qualification fault',
                'type': 'server_error'}}), status_code=503, media_type='application/json')
        if mode == 'stall':
            row['injected'] = 'withheld_response'
            try:
                await stopping.wait()
            finally:
                row['released'] = True
            return Response(status_code=503)

        async def forward(pinned):
            headers = {'Content-Type': 'application/json',
                'Authorization': 'Bearer '+(binding.config.api_key or 'local-no-key')}
            async with _local_http_client(binding.config, pinned) as client:
                async with client.stream('POST', binding.config.base_url.rstrip('/')+'/chat/completions',
                        content=raw, headers=headers, timeout=180) as response:
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 8*1024*1024:
                            raise ValueError('Upstream qualification response exceeds bound')
                    row.update(forwarded=True, status=response.status_code)
                    return Response(bytes(data), status_code=response.status_code, media_type='application/json')
        # Do not let replacing an endpoint with localhost bypass original LAN
        # eligibility, DNS pinning, redirect or environment-proxy restrictions.
        return await router._at_endpoint(snapshot, binding, forward)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(('127.0.0.1', 0))
    sock.listen(16)
    server = uvicorn.Server(uvicorn.Config(app, log_level='critical', access_log=False,
        lifespan='off', timeout_graceful_shutdown=1))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    task.result()
                    raise RuntimeError('Owned fault proxy did not start')
                await asyncio.sleep(.01)
        yield 'http://127.0.0.1:'+str(sock.getsockname()[1])+'/v1', rows
    finally:
        stopping.set()
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 3)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            sock.close()

