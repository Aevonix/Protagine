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


MIND_FACULTIES = ('initiative', 'drives', 'deliberation', 'goals', 'people', 'affect', 'opinions', 'broadcast',
                  'semantic_recall', 'consolidation', 'self_narrative', 'lessons', 'skills')
WORKER_PROFILE = 'protagine-act'


def mind_section(switches):
    """The ``mind`` section of the disposable ``protagine.yaml`` for a profile's mind switches.

    Off (``None`` or ``False``) in the plain plugin arm. ``{'initiative': True}``
    (or ``True``) turns the mind on at autonomy ``standard`` with only the
    initiative faculty (mind-initiative-1). ``{'full': True}`` sets every
    faculty flag and drive weight to its release-candidate value from the
    shipped defaults; a ``minus_<faculty>`` switch turns that faculty's flag
    off, a ``plus_<faculty>`` switch turns on one that ships off, and a
    ``minus_<drive>`` switch sets that drive's weight to 0 (evals section 3,
    the ``full-X`` arms). The flag is written whether or not the
    faculty's code has landed, so an ablation of a faculty nothing reads yet
    is a no-op contrast until its milestone. Quiet hours and the daily digest
    are off in every mind arm because an episode's clock advance would
    otherwise hold or add owner notices that have nothing to do with the
    scenario.
    """
    if not switches:
        return {'enabled': False}
    if switches is True:
        switches = {'initiative': True}
    section = {'enabled': True, 'autonomy': 'standard', 'quiet_hours': '', 'digest_hour': 24}
    if not switches.get('full'):
        section['faculties'] = {name: name == 'initiative' for name in MIND_FACULTIES}
        return section
    from copy import deepcopy
    from protagine.config import DEFAULTS
    defaults = DEFAULTS['mind']
    faculties = {name: bool(defaults['faculties'].get(name, False)) for name in MIND_FACULTIES}
    drives = deepcopy(defaults['drives'])
    for name in MIND_FACULTIES:
        if switches.get(f'minus_{name}'):
            faculties[name] = False
        if switches.get(f'plus_{name}'):
            faculties[name] = True
    for name in drives:
        if switches.get(f'minus_{name}'):
            drives[name] = 0.0
    section.update(faculties=faculties, drives=drives, budgets=deepcopy(defaults['budgets']))
    return section


def mind_clock():
    """The body clock: ``time.time`` as the paired body shifts it, as an aware UTC datetime."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(time.time(), timezone.utc)


def benchmark_identity(person):
    """The disposable ``identity.yaml``: the owner, and the one handle a mind message can reach.

    The body resolves an owner message through ``owner.handles`` first, so
    without one no mind message goes out: the body finds no target and the
    sidecar leaves the message waiting unclaimed. The handle is the capture
    platform's home channel, where the heartbeat arm already delivers.
    """
    from protagine.qualification.paired_body import OWNER, PLUGIN
    return {'owner': {'name': 'Owner', 'contact_id': person, 'handles': {PLUGIN: [OWNER]}},
            'agent': {'name': 'Agent'}}


def mount_routes(app, *, mind):
    """The routes the arm serves, as the sidecar mounts them: the host routes, and with the mind
    its own routes and the people routes (an owner's ``protagine_people`` reaches them)."""
    from protagine.api.routers import host, people
    from protagine.api.routers import mind as mind_router
    app.include_router(host.router)
    app.include_router(host.v2_router)
    if mind:
        app.include_router(mind_router.router)
        app.include_router(people.router)


@contextmanager
def serve_mind(app, state, person, section):
    """A real Mind over this arm's stores, on ``/v1/mind`` next to the host routes.

    The stores the host routes already own (the commitment store, contacts and
    the ledger) are shared; the intention, feedback and expectation stores live
    in the same ``memory-state`` directory. The Mind's own timer is never
    started: the body tick calls ``POST /v1/mind/tick`` through the plugin's
    ``tick()``, so intentions form in lockstep with the episode's clock.
    """
    from protagine.api.routers import host
    from protagine.api.routers import mind as mind_router
    from protagine.commitments.extract import CommitmentExtractor, contact_aliases
    from protagine.feedback import TypeFeedbackStore
    from protagine.initiatives.store import InitiativeStore
    from protagine.mind import Mind
    from protagine.self_model.expectations import ExpectationEngine, ExpectationStore
    from protagine.turns import get_turn_idempotency_ledger
    directory = state / 'memory-state'
    directory.mkdir(parents=True, exist_ok=True)
    store = InitiativeStore(state_dir=directory)
    try:
        # The arm's own router (the endpoint the plan pinned) serves the one deliberation call per tick
        # and the capture jobs a tick drains first; the extractor shares the source worker's ledger,
        # so a job the worker holds is waited for, never run twice.
        mind = Mind(config=section, store=store, state_dir=directory, owner_id=person,
                    commitments=host._commitment_store,
                    feedback=TypeFeedbackStore(db_path=str(directory / 'protagine-feedback.db')),
                    expectations=ExpectationEngine(ExpectationStore(str(directory / 'protagine-expectations.db'))),
                    contacts=getattr(host, '_contacts_store', None),
                    ledger=get_turn_idempotency_ledger(directory), clock=mind_clock, backups=False,
                    router=getattr(host, '_llm_router', None),
                    # The people faculty's reads, as the sidecar wires them (served where the host has them).
                    comms=getattr(host, '_comms_log', None), affect=getattr(host, '_affect_store', None),
                    packet_for=getattr(host, 'assemble_packet', None), claims_for=getattr(host, 'claims_for', None),
                    capture=CommitmentExtractor(get_turn_idempotency_ledger(directory),
                                                lambda: host._commitment_store,
                                                aliases=contact_aliases(lambda: getattr(host, '_contacts_store', None))))
        mind_router.set_mind(mind)
        yield mind
    finally:
        mind_router.set_mind(None)
        store.close()


def install_worker_profile(home):
    """The ``protagine-act`` profile directory the kanban dispatcher spawns mind tasks on.

    The in-process worker runs the task with the episode's own recipe; the
    profile only has to exist for ``dispatch_once`` to consider the task
    spawnable, as ``protagine init`` makes it exist on a real install.
    """
    directory = Path(home) / 'profiles' / WORKER_PROFILE
    for name in ('memories', 'sessions', 'skills', 'logs', 'workspace'):
        (directory / name).mkdir(parents=True, exist_ok=True)
    config_path = directory / 'config.yaml'
    if not config_path.exists():
        config_path.write_text(json.dumps({'plugins': {'enabled': ['protagine'], 'hook_callback_timeout': 0}}))
    return directory


@contextmanager
def prepare(request, state, arguments, config, *, setup_host=None, scopes=None, overlay=None, mind=None):
    """Enable only this fixture's private profile and ledger, before agent construction.

    An arm profile overlay is applied after the forced flags below and before
    the plugin loads, so a frozen profile can flip any fixture default. With
    ``mind`` (the profile's mind switches, see ``mind_section``) the arm also
    serves ``/v1/mind`` over a real Mind.
    """
    inputs = request['inputs']
    person = inputs['contact_id']
    os.environ.update(PROTAGINE_STATE_DIR=str(state / 'memory-state'),
        PROTAGINE_EVENT_JOURNAL_DIR=str(state / 'memory-state' / 'events'),
        PROTAGINE_SKIP_DOTENV='1', PYTHON_DOTENV_DISABLED='1',
        HERMES_BUNDLED_PLUGINS=str(state / 'empty-bundled'), HERMES_ENABLE_PROJECT_PLUGINS='0',
        HERMES_DISABLE_TELEMETRY='1', HERMES_DISABLE_LAZY_INSTALLS='1',
        PROTAGINE_GENERAL_PLUGIN_ACTIVE='1', PROTAGINE_MEMORY_TURN_WRITER='disabled',
        PROTAGINE_MEMORY_WORKER_TOOLS='0', PROTAGINE_MEMORY_DEFAULT_CONTEXT_AUTHORITY='none',
        PROTAGINE_OWNER_CONTACT_ID=person,
        PROTAGINE_EMBED_PROVIDER='skip', PROTAGINE_GRAPH_ENABLED='false',
        # The body tick drives the adapter (tick() and flush()); its own thread stays parked
        # so no dispatch or send lands between two observed ticks.
        PROTAGINE_BODY_THREAD='0')
    os.environ.update(overlay or {})
    (state / 'empty-bundled').mkdir(exist_ok=True)
    from fastapi import FastAPI
    import uvicorn
    from protagine.api.middleware import ApiKeyMiddleware
    from protagine.api.routers import host
    from protagine.api.routers import mind as mind_router
    from protagine.turns import get_turn_idempotency_ledger
    from hermes_cli.plugins import get_plugin_manager
    from hermes_state import SessionDB
    import httpx

    secret = secrets.token_urlsafe(32)
    diagnostic = request.get('_diagnostic_recorder')
    if diagnostic is not None:
        diagnostic.add_secret(secret)
    # The disposable instance directory the adapter reads: the one key, the
    # owner identity and a stock protagine.yaml (mind defaults, nothing enabled
    # beyond memory). The same secret guards the fixture API below.
    instance = state / 'protagine-instance'
    instance.mkdir(mode=0o700, exist_ok=True)
    key_file = instance / 'api.key'
    key_file.write_text(secret + '\n')
    key_file.chmod(0o600)
    (instance / 'identity.yaml').write_text(json.dumps(benchmark_identity(person)))
    section = mind_section(mind)
    (instance / 'protagine.yaml').write_text(json.dumps({'owner': {'contact_id': person}, 'mind': section}))
    os.environ.update(PROTAGINE_HOME=str(instance), PROTAGINE_API_KEY=secret)
    if mind:
        install_worker_profile(state)
    app = FastAPI()
    app.add_middleware(ApiKeyMiddleware, api_key=secret)
    routes = []

    @app.middleware('http')
    async def record_route(req, next_call):
        response = await next_call(req)
        routes.append({'path': req.url.path, 'status': response.status_code})
        if diagnostic is not None:
            diagnostic.record('context_route', {'method': req.method,
                'path': req.url.path, 'status': response.status_code})
        return response

    mount_routes(app, mind=bool(mind))
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
        if mind:
            # The host stores exist once the server's lifespan ran; the Mind shares them.
            host_resources.enter_context(serve_mind(app, state, person, section))
        already = [name for name in config.get('plugins', {}).get('enabled', []) if name != 'protagine']
        # The keys `protagine init` writes: the adapter finds the sidecar and
        # the key file here; callbacks run inline so no capture fire is dropped.
        config.update(plugins={'enabled': [*already, 'protagine'], 'hook_callback_timeout': 0,
                               'protagine': {**config.get('plugins', {}).get('protagine', {}),
                                             'sidecar_url': url, 'key_file': str(key_file),
                                             'turn_outbox_path': str(state / 'protagine-turn-outbox.sqlite3')}},
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
