"""Isolated native memory observation over real plugin/provider and host routes."""
from contextlib import ExitStack, contextmanager
import json
import hashlib
import os
from pathlib import Path
import secrets
import socket
import threading
import time
from unittest.mock import patch


@contextmanager
def prepare(request, state, arguments, config, *, setup_host=None, scopes=None):
    """Enable only this fixture's private profile and ledger, before agent construction."""
    inputs = request['inputs']
    person = inputs['contact_id']
    os.environ.update(PROTAGINE_STATE_DIR=str(state / 'memory-state'),
        PROTAGINE_EVENT_JOURNAL_DIR=str(state / 'memory-state' / 'events'),
        PROTAGINE_SKIP_DOTENV='1', PYTHON_DOTENV_DISABLED='1',
        HERMES_BUNDLED_PLUGINS=str(state / 'empty-bundled'), HERMES_ENABLE_PROJECT_PLUGINS='0',
        HERMES_DISABLE_TELEMETRY='1', HERMES_DISABLE_LAZY_INSTALLS='1',
        PROTAGINE_GENERAL_PLUGIN_ACTIVE='1', PROTAGINE_MEMORY_TURN_WRITER='disabled',
        PROTAGINE_MEMORY_WORKER_TOOLS='0', PROTAGINE_MEMORY_DEFAULT_CONTEXT_AUTHORITY='none',
        PROTAGINE_OWNER_CONTACT_ID=person, PROTAGINE_GUARD_CHAT_MODE='off',
        PROTAGINE_EMBED_PROVIDER='skip', PROTAGINE_GRAPH_ENABLED='false')
    (state / 'empty-bundled').mkdir(exist_ok=True)
    from fastapi import FastAPI
    import uvicorn
    from protagine.api.middleware import ApiKeyMiddleware
    from protagine.api.routers import host
    from protagine.turns import get_turn_idempotency_ledger
    from hermes_cli.plugins import get_plugin_manager
    from hermes_state import SessionDB
    import httpx

    secret = secrets.token_urlsafe(32)
    diagnostic = request.get('_diagnostic_recorder')
    if diagnostic is not None:
        diagnostic.add_secret(secret)
    keyring = state / 'fixture-keyring.json'
    keyring.write_text(json.dumps({'version': 1, 'principals': [{
        'principal': 'benchmark-owner', 'status': 'active', 'viewer_person_id': person,
        'person_ids': [person], 'scopes': list(scopes) if scopes is not None else
            ['context:read', 'turns:write', 'memory:read'],
        'audiences': ['viewer'], 'credentials': [{'id': 'fixture', 'secret': secret, 'status': 'active'}]}]}))
    keyring.chmod(0o600)
    app = FastAPI()
    app.add_middleware(ApiKeyMiddleware, keyring_path=str(keyring))
    routes = []

    @app.middleware('http')
    async def record_route(req, next_call):
        response = await next_call(req)
        routes.append({'path': req.url.path, 'status': response.status_code})
        if diagnostic is not None:
            diagnostic.record('context_route', {'method': req.method,
                'path': req.url.path, 'status': response.status_code})
        return response

    app.include_router(host.router)
    app.include_router(host.v2_router)
    host_resources = ExitStack()
    listener = server = thread = None
    transport_observer = None
    try:
        if setup_host is not None:
            host_resources.enter_context(setup_host(app, state, inputs, config))
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(64)
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
            lifespan='on', access_log=False, log_level='error'))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
        thread.start()
        started = time.monotonic()
        while not server.started:
            if not thread.is_alive() or time.monotonic() - started > 5:
                raise RuntimeError('Isolated memory API did not start')
            time.sleep(.01)
        url = f'http://127.0.0.1:{port}'
        config.update(plugins={'enabled': ['protagine'], 'protagine': {
            **config.get('plugins', {}).get('protagine', {}),
            'url': url, 'api_key': secret, 'owner_contact_id': person,
            'attested_system_platforms': ['cli'], 'turn_writer_platforms': ['cli']}},
            memory={'provider': 'protagine-memory', 'config': {
                'url': url, 'api_key': secret, 'contact_id': person}})
        (state / 'config.yaml').write_text(json.dumps(config))
        (state / 'SOUL.md').write_text('Use available evidence. Preserve uncertainty. Answer the current question.')
        manager = get_plugin_manager()
        # A native import may have discovered the initial provider-only profile.
        # Reload this isolated profile after installing its explicit fixture config.
        manager.discover_and_load(force=True)
        loaded = manager._plugins.get('protagine')
        if loaded is None or not loaded.enabled:
            raise RuntimeError('Protagine adapter unavailable: ' + str(loaded.error if loaded else 'not discovered'))
        requests = []
        freshness = []
        original_send = httpx.Client.send
        provider_prefix = arguments['base_url'].rstrip('/') + '/'

        class ObservedStream(httpx.SyncByteStream):
            """Tee bytes unchanged; retain only model IDs, never generated text."""
            def __init__(self, stream, observation, sse):
                self.stream, self.observation, self.sse = stream, observation, sse
                self.buffer = b''

            def consume(self, raw):
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    return
                model = value.get('model') if isinstance(value, dict) else None
                if isinstance(model, str) and 0 < len(model) <= 512 and model not in self.observation['returned_models']:
                    self.observation['returned_models'].append(model)

            def __iter__(self):
                for chunk in self.stream:
                    self.buffer += chunk
                    if self.sse:
                        while b'\n' in self.buffer:
                            line, self.buffer = self.buffer.split(b'\n', 1)
                            if line.startswith(b'data:'):
                                self.consume(line[5:].strip())
                    if len(self.buffer) > 2097152:
                        self.observation['response_identity_truncated'] = True
                        self.buffer = b''
                    yield chunk
                if not self.sse:
                    self.consume(self.buffer)

            def close(self):
                self.stream.close()

        def observe_send(client, outgoing, *args, **kwargs):
            # Observe the serialized physical request after both the ordinary
            # middleware and native Relay filters. The earlier llm_request hook
            # sees an unfiltered input and is not evidence of model visibility.
            # This instrumentation is confined to the owned subprocess and does
            # not alter request content, responses, policy or endpoint selection.
            observation = None
            body = None
            if outgoing.method == 'POST' and str(outgoing.url).startswith(provider_prefix):
                body = json.loads(outgoing.content)
            if isinstance(body, dict) and any(name in body for name in ('messages', 'input', 'system', 'instructions')):
                content = {name: body[name] for name in ('messages', 'input', 'system', 'instructions')
                           if name in body}
                text = json.dumps(content, ensure_ascii=False)
                observation = {'boundary': 'httpx_serialized_provider_request',
                    'selected_binding': request['binding'] if body.get('model') == arguments['model'] else None,
                    'endpoint_sha256': hashlib.sha256(provider_prefix.encode()).hexdigest(),
                    'model': body.get('model'), 'text': text[:131072], 'truncated': len(text) > 131072,
                    'returned_models': [], 'response_identity_truncated': False}
                requests.append(observation)
            response = original_send(client, outgoing, *args, **kwargs)
            if observation is not None:
                observation['status'] = response.status_code
                sse = response.headers.get('content-type', '').startswith('text/event-stream')
                observer = ObservedStream(response.stream, observation, sse)
                if response.is_stream_consumed:
                    # OpenAI's nonstreaming transport may already have read it.
                    observer.consume(response.content)
                else:
                    response.stream = observer
            if outgoing.url.port == port and outgoing.url.path.endswith('/sources/erasures'):
                body = json.loads(response.read())
                freshness.append({name: body.get(name) for name in
                    ('complete', 'head', 'through', 'sources_current', 'annotation_checks_current')})
            return response

        transport_observer = patch.object(httpx.Client, 'send', observe_send)
        transport_observer.start()
        arguments.update(skip_memory=False, skip_background_review=True, enabled_toolsets=[],
            session_db=SessionDB(state / 'state.db'))

        def evidence(agent, response):
            manager = agent._memory_manager
            provider = manager.get_provider('protagine') if manager is not None else None
            ledger = get_turn_idempotency_ledger(state / 'memory-state')
            return {'consumer': 'native_protagine_automatic_recollection',
                'memory_provider_loaded': provider is not None,
                'source_sessions': sorted({row['session_id'] for row in inputs['turns']}),
                'native_session_id': agent.session_id,
                'fresh_session': agent.session_id not in {row['session_id'] for row in inputs['turns']},
                'context_routes': list(routes), 'request_observations': list(requests),
                'freshness_responses': list(freshness),
                'erased_sources': {source: ledger.is_source_erased(source, person)
                                  for source in inputs.get('forget_source_ids', [])},
                'embedding_and_reranking': 'not_exercised',
                'transport_delivery': 'not_exercised'}

        yield evidence
    except Exception as exc:
        # Native logs stay inside the private attempt and are never public evidence.
        print('Native memory setup:', type(exc).__name__, str(exc)[:1000], flush=True)
        raise
    finally:
        if transport_observer is not None:
            transport_observer.stop()
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(5)
        if listener is not None:
            listener.close()
        host_resources.close()
        if thread is not None and thread.is_alive():
            raise RuntimeError('Isolated memory API did not stop')


if __name__ == '__main__':
    from protagine.qualification.native_worker import main
    main(prepare)
