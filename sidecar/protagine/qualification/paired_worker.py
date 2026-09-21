"""Run one chronological episode inside a disposable benchmark container.

No expected answer or grader enters this process. Both arms use native Hermes
file, planning, memory and session-search tools. Protagine's ordinary adapter,
source writer and projections are enabled only in the treatment arm.
"""
import asyncio
from contextlib import ExitStack, asynccontextmanager, contextmanager
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
import traceback

RESULT_MARKER = 'PROTAGINE_PAIRED_RESULT:'
COMMON_TOOLS = ['file', 'memory', 'session_search', 'todo']
# This key belongs only to the disposable, single-owner fixture API. Provider
# context tools use the existing api:access contract; live grants are untouched.
PAIRED_FIXTURE_SCOPES = ['context:read', 'turns:write', 'memory:read',
                         'memory:search', 'memory:write', 'api:access']
MEMORY_TOOLS = ['protagine_memory_search', 'protagine_memory_read_source',
                'protagine_memory_retain_observation', 'protagine_memory_annotate',
                'protagine_memory_forget']
SYSTEM = ('Complete the requested work using available evidence and tools. '
          'Workspace files are in /state/workspace. Preserve useful facts for later sessions. '
          'Distinguish confirmed facts, proposals and uncertainty. Do not claim an action '
          'succeeded unless you observed its result.')


def inspect_payload():
    from .native_identity import inspect_runtime
    from .native_memory_identity import inspect_runtime as inspect_adapters
    from .paired_trace import PROTOCOL as trace_protocol
    return {'native_runtime': inspect_runtime(), 'adapters': inspect_adapters(),
            'worker_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'profile': 'paired-text-native-memory-1', 'common_toolsets': COMMON_TOOLS,
            'treatment_tools': MEMORY_TOOLS, 'private_trace_protocol': trace_protocol}


def seed_workspace(root, files):
    if not isinstance(files, dict) or len(files) > 64:
        raise ValueError('Invalid fixture file count')
    for name, value in files.items():
        path = Path(name)
        if (not isinstance(name, str) or path.is_absolute() or '..' in path.parts
                or not path.parts or not isinstance(value, str)):
            raise ValueError('Invalid fixture path or contents')
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value)


def snapshot_workspace(root):
    files, size = {}, 0
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('Workspace artifact must not be a symlink')
        if path.is_file():
            size += path.stat().st_size
            if size > 262144 or len(files) >= 64:
                raise ValueError('Workspace artifact size bound exceeded')
            files[str(path.relative_to(root))] = path.read_text()
    return files


@contextmanager
def workspace_tools(root):
    """Constrain the same native file executor in both arms to fixture files."""
    from tools.registry import registry
    import tools.file_tools  # native registration
    from unittest.mock import patch
    with ExitStack() as stack:
        for name in ('read_file', 'write_file', 'patch', 'search_files'):
            entry = registry.get_entry(name)
            if entry is None:
                raise RuntimeError('Missing native file tool: ' + name)
            original = entry.handler

            def guarded(args, *rest, _original=original, **kwargs):
                args = dict(args)
                raw = args.get('path', '.')
                if not isinstance(raw, str):
                    return json.dumps({'error': 'Workspace path must be a string'})
                target = (root / raw).resolve()
                if not target.is_relative_to(root.resolve()):
                    return json.dumps({'error': 'Path is outside this task workspace'})
                args['path'] = str(target)
                return _original(args, *rest, **kwargs)

            stack.enter_context(patch.object(entry, 'handler', guarded))
        yield


@contextmanager
def provider_read_services(state):
    """Own empty provider stores on the API thread; never seed scenario answers."""
    from protagine.api.routers import host
    from protagine.commitments.store import CommitmentStore
    from protagine.tom.affect import AffectStore
    from protagine.tom.facts import SharedFactsStore
    from protagine.turns import get_turn_idempotency_ledger

    directory = state / 'memory-state'
    directory.mkdir(parents=True, exist_ok=True)
    ledger = get_turn_idempotency_ledger(directory)
    with ExitStack() as resources:
        for name, factory, filename in (
            ('commitment', CommitmentStore, 'protagine-commitments.db'),
            ('affect', AffectStore, 'protagine-affect.db'),
            ('facts', SharedFactsStore, 'protagine-facts.db'),
        ):
            store = factory(directory / filename, **(
                {'source_ledger': ledger} if name != 'commitment' else {}))
            if hasattr(store, 'close'):
                resources.callback(store.close)
            setter = getattr(host, 'set_' + name + '_store')
            resources.callback(setter, getattr(host, '_' + name + '_store'))
            setter(store)
        yield


def provider_read_lifespan(state):
    @asynccontextmanager
    async def lifespan(app):
        # SQLite-backed facts/affect stores require construction and shutdown
        # on the same thread that serves their HTTP handlers.
        with provider_read_services(state):
            yield
    return lifespan


@contextmanager
def source_worker(app, state, inputs, config):
    from protagine.router import LLMRouter
    from protagine.api.routers import host
    from protagine.turns import get_turn_idempotency_ledger
    from protagine.beliefs.source_projection import run_source_claim_worker
    from hermes_cli.runtime_provider import resolve_runtime_provider
    runtime = resolve_runtime_provider(requested=config['model']['provider'],
                                       target_model=config['model']['default'])
    router = LLMRouter(tiers={})
    roles = ('chat', 'reasoning', 'planning', 'extraction', 'judging', 'coding')
    router.configure({'provider': 'custom', 'protocol': 'openai-chat',
        'modelPool': {'candidate': {'model': config['model']['default'],
            'baseUrl': runtime['base_url'], 'apiKey': runtime.get('api_key', ''),
            'maxTokens': inputs['max_output_tokens'], 'supportsTools': True,
            'supportsJsonSchema': True}},
        'functionRoles': {role: {'candidates': ['candidate'], 'timeoutSeconds': 60,
                                'deadlineSeconds': 60} for role in roles}})
    from unittest.mock import patch
    resources = ExitStack()
    resources.enter_context(patch.object(host, '_llm_router', router))
    resources.enter_context(patch.object(app.router, 'lifespan_context', provider_read_lifespan(state)))
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    holder = {}

    def run():
        asyncio.set_event_loop(loop)
        holder['task'] = loop.create_task(run_source_claim_worker(
            get_turn_idempotency_ledger(state / 'memory-state'), lambda: router))
        ready.set()
        try:
            loop.run_until_complete(holder['task'])
        except asyncio.CancelledError:
            pass
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    thread = threading.Thread(target=run, name='paired-source-worker', daemon=True)
    try:
        thread.start()
        if not ready.wait(10):
            raise RuntimeError('Source worker did not start')
        yield
    finally:
        try:
            task = holder.get('task')
            if task is not None and not loop.is_closed():
                loop.call_soon_threadsafe(task.cancel)
            if thread.ident is not None:
                thread.join(10)
            if thread.is_alive():
                raise RuntimeError('Source worker did not stop')
            if task is not None and task.done() and not task.cancelled() and task.exception() is not None:
                raise RuntimeError('Source worker failed') from task.exception()
        finally:
            resources.close()


def main():
    if sys.argv[1:] == ['--inspect']:
        print(json.dumps(inspect_payload()))
        return 0
    request = json.load(sys.stdin)
    inputs, config = request['inputs'], request['config']
    arm = inputs['arm']
    if arm not in {'base_hermes', 'protagine'}:
        raise ValueError('Unknown experiment arm')
    home, workspace = Path('/state/home'), Path('/state/workspace')
    home.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    os.environ.update(request.get('provider_env', {}))
    os.environ.update(HOME=str(home), HERMES_HOME=str(home), HERMES_SKIP_DOTENV='1',
        PYTHON_DOTENV_DISABLED='1', HERMES_DISABLE_TELEMETRY='1',
        LITELLM_LOCAL_MODEL_COST_MAP='True',
        HERMES_DISABLE_LAZY_INSTALLS='1', HERMES_ENABLE_PROJECT_PLUGINS='0',
        HERMES_BUNDLED_PLUGINS=str(home / 'empty-bundled'), TERMINAL_CWD=str(workspace))
    (home / 'empty-bundled').mkdir()
    seed_workspace(workspace, inputs['initial_files'])
    config.update(plugins={'enabled': [], 'disabled': ['protagine']},
                  terminal={'backend': 'local', 'cwd': str(workspace)})
    (home / 'config.yaml').write_text(json.dumps(config))
    os.chdir(workspace)
    agents, histories, rows = {}, {}, []
    result = {'stage': 'preparing', 'agent_close_returned': False,
              'tool_evidence': {'declared_turns': len(inputs['episodes']), 'turns_completed': 0}}
    from .paired_trace import DiagnosticTrace
    trace = DiagnosticTrace(secrets=request.get('provider_env', {}).values())
    request['_diagnostic_recorder'] = trace
    trace.record('episode', {'arm': arm, 'case_id': inputs.get('case_id'),
                             'session_ids': [t['session_id'] for t in inputs['episodes']]})
    stop = threading.Event()

    def interrupt(_signal, _frame):
        stop.set()
        for agent in list(agents.values()):
            agent.hard_interrupt('Benchmark deadline')

    signal.signal(signal.SIGTERM, interrupt)
    try:
        from hermes_cli.config import load_config
        from hermes_cli.runtime_provider import resolve_runtime_provider
        from hermes_constants import resolve_reasoning_config
        from hermes_state import SessionDB
        from run_agent import AIAgent
        from .paired_transport import observe_requests, usage_summary
        config = load_config()
        model = config['model']['default']
        runtime = resolve_runtime_provider(requested=request['binding'], target_model=model)
        trace.add_secret(runtime.get('api_key'))
        arguments = dict(model=model,
            **{k: runtime[k] for k in ('base_url', 'api_key', 'provider', 'api_mode',
                'requested_provider', 'request_overrides', 'capabilities') if k in runtime},
            platform='cli', max_iterations=inputs['max_iterations'],
            max_tokens=inputs['max_output_tokens'], enabled_toolsets=list(COMMON_TOOLS),
            quiet_mode=True, skip_context_files=True, skip_memory=False,
            skip_background_review=False, fallback_model=None, save_trajectories=False,
            reasoning_config=resolve_reasoning_config(config, model))
        with ExitStack() as resources:
            observer = None
            toolsets = list(COMMON_TOOLS)
            if arm == 'protagine':
                from .native_memory_worker import prepare
                # Reuse actual adapter/provider/API setup, not its seeded consumer.
                # Empty turns: all history enters through native conversations.
                config['plugins'] = {'enabled': ['protagine']}
                request['inputs']['turns'] = []
                observer = resources.enter_context(prepare(request, home, arguments, config,
                    setup_host=source_worker, scopes=PAIRED_FIXTURE_SCOPES))
                from toolsets import create_custom_toolset
                create_custom_toolset('paired_protagine_memory', 'Protagine native memory tools',
                                      tools=MEMORY_TOOLS)
                toolsets.append('paired_protagine_memory')
            arguments.update(enabled_toolsets=toolsets, skip_background_review=False,
                             skip_memory=False, session_db=SessionDB(home / 'state.db'))
            resources.enter_context(workspace_tools(workspace))
            requests = resources.enter_context(observe_requests(runtime['base_url'], diagnostic=trace))
            result['stage'] = 'running'
            for index, turn in enumerate(inputs['episodes']):
                if stop.is_set():
                    raise InterruptedError('Benchmark interrupted')
                session_id = turn['session_id']
                if session_id not in agents:
                    agents[session_id] = AIAgent(**arguments, session_id=session_id)
                agent = agents[session_id]
                response = agent.run_conversation(turn['user'], system_message=SYSTEM,
                    conversation_history=histories.get(session_id))
                histories[session_id] = response.get('messages', [])
                complete = response.get('completed') is True and not any(
                    response.get(k) for k in ('failed', 'partial', 'interrupted'))
                trace.record('native_turn', {'index': index, 'session_id': session_id,
                    'completed': response.get('completed'), 'failed': response.get('failed'),
                    'partial': response.get('partial'), 'interrupted': response.get('interrupted'),
                    'messages': response.get('messages'), 'final_response': response.get('final_response')})
                rows.append({'session_id': session_id, 'completed': complete,
                             'final_response': response.get('final_response')})
                result['tool_evidence']['turns_completed'] += int(complete)
                # Fixed, declared settling window in both arms, included in wall
                # time. No manually inserted facts, forced review or hidden oracle.
                stop.wait(inputs.get('settle_seconds', 5))
                if not complete:
                    break
            treatment = observer(agent, response) if observer and agents else {}
            if observer:
                # Physical prompt copies are not outcome artifacts. Retaining
                # them would consume only the treatment arm's output allowance.
                trace.record('context_routes', treatment.get('context_routes', []))
                result['tool_evidence']['treatment'] = {
                    'memory_provider_loaded': treatment.get('memory_provider_loaded'),
                    'context_route_successes': sum(row.get('path') == '/v1/host/context/assemble'
                        and row.get('status') == 200 for row in treatment.get('context_routes', [])),
                    'recorded_routes': len(treatment.get('context_routes', [])),
                    'request_count': len(treatment.get('request_observations', []))}
            for agent in agents.values():
                review = getattr(agent, '_background_review_run', None)
                if review is not None:
                    review.request_done.wait(inputs.get('settle_seconds', 5))
                agent.close()
            result['agent_close_returned'] = True
            result['tool_evidence'].update(model_requests=requests, resource_usage=usage_summary(requests),
                artifacts=snapshot_workspace(workspace), native_memory_enabled=True,
                session_search_enabled=True,
                treatment_loaded=treatment.get('memory_provider_loaded', False), turns=rows,
                treatment_profile='text-native-memory-and-source-projections',
                limitations=['no embedding/reranking', 'no channel transport',
                    'no executed coding tests', 'no attested multi-user boundary',
                    'fixed settling window; background completion not guaranteed'])
            result['output'] = rows[-1]['final_response'] if rows else None
            result['stage'] = 'returned'
    except BaseException as exc:
        result.update(error_origin_stage=result['stage'], stage='error', error_type=type(exc).__name__,
                      private_error_traceback=''.join(traceback.format_exception(exc))[-8192:])
    finally:
        for agent in agents.values():
            try:
                agent.close()
            except Exception:
                result['agent_close_returned'] = False
        result['worker_stopped'] = True
        result['private_diagnostics'] = trace.summary()
        print(RESULT_MARKER + json.dumps(result, allow_nan=False), flush=True)
    return 0 if result['stage'] == 'returned' else 1


if __name__ == '__main__':
    raise SystemExit(main())
