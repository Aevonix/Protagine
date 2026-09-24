"""Run one chronological episode inside a disposable benchmark container.

No expected answer or grader enters this process. Both arms use native Hermes
file, planning, memory and session-search tools. Protagine's ordinary adapter,
source writer and projections are enabled only in the treatment arm. Body
events (ticks, clock advances) and the capture platform are identical in both.
"""
import asyncio
from copy import deepcopy
from contextlib import ExitStack, asynccontextmanager, closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import threading
import time
import traceback

from . import paired_arms, paired_body, paired_history

RESULT_MARKER = 'PROTAGINE_PAIRED_RESULT:'
# Version 2 added the binary comparator switches heartbeat and curator to a profile; version 3
# added the mind switches of the drives family (full and the minus_* ablations); version 4 adds
# one ablation per later faculty (the people, affect, opinions, memory, self and improve
# families), so an image built before it cannot apply those arms; version 5 adds the affect
# mechanism arm (plus_affect_rules).
ARM_PROFILE_PROTOCOL = 'paired-arm-profiles-5'
# The mind switches: the plugin arm with the mind on, served in-process next to the host
# routes; the body tick calls the plugin's tick() (POST /v1/mind/tick, then dispatch, outbox,
# reconciliation and observations) before cron and kanban dispatch. ``initiative`` turns on
# only the initiative faculty (mind-initiative-1); ``full`` sets every faculty flag and drive
# weight to its release-candidate value (native_memory_worker.mind_section), each
# ``minus_<faculty>`` switch turns that faculty's flag off and each ``minus_<drive>`` switch
# sets that drive's weight to 0 (evals section 3, the full-X arms); a ``plus_<faculty>`` switch
# turns on a faculty that ships off (skills, the full-plus-skills arm; affect_rules, the stateless
# affect rules of the full-affect-plus-rules mechanism arm). A faculty whose code has
# not landed yet still has its flag written, so its ablation is a no-op contrast until then.
MIND_FACULTY_ABLATIONS = ('minus_drives', 'minus_broadcast', 'minus_people', 'minus_affect', 'minus_opinions',
                          'minus_semantic_recall', 'minus_consolidation', 'minus_self_narrative', 'minus_lessons')
MIND_DRIVE_ABLATIONS = ('minus_duty', 'minus_curiosity', 'minus_mastery', 'minus_upkeep', 'minus_social')
MIND_ABLATIONS = (*MIND_FACULTY_ABLATIONS, *MIND_DRIVE_ABLATIONS)
MIND_ADDITIONS = ('plus_skills', 'plus_affect_rules')
MIND_SWITCHES = ('initiative', 'full', *MIND_ABLATIONS, *MIND_ADDITIONS)
PROFILE_SWITCHES = ('heartbeat', 'curator', *MIND_SWITCHES)
MIND_TICK_PROTOCOL = 'paired-mind-tick-1'


def mind_switches(profile):
    """The mind switches a profile turns on, or None when its mind is off."""
    switches = {name: True for name in MIND_SWITCHES if profile.get(name)}
    return switches or None
# Plans written before arm profiles carried only the arm label.
LEGACY_PROFILES = {'base_hermes': {'name': 'base_hermes', 'plugin': False, 'overlay': {}},
                   'protagine': {'name': 'protagine', 'plugin': True, 'overlay': {}}}
COMMON_TOOLS = ['file', 'memory', 'session_search', 'todo']
KANBAN_WORKER_TOOLS = ['kanban']
# Hermes 0.21.3 defers session_search, todo_list and cronjob_manage behind the
# tool_search bridge by default (tools/tool_search.py: _DEFAULT_DEFERRED_TOOLS is
# consulted before the core-tool exemption in is_deferrable_tool_name), so each
# use costs a search, a describe and a call out of the frozen iteration budget.
# A generated family declares eager loading through the stock key
# tools.tool_search.enabled: off, under which assemble_tool_defs passes every
# enabled tool through untouched. It is applied to every arm alike and recorded
# in the plan; the frozen datasets keep the stock deferral.
TOOL_LOADING_PROTOCOL = 'paired-tool-loading-1'
TOOL_LOADING_MODES = ('eager',)
EAGER_TOOLS_CONFIG = {'tool_search': {'enabled': 'off'}}
# Nothing in a benchmark turn tells the model what time it is: the stock system
# prompt carries only the date the conversation started and sends the model to a
# terminal for the time (agent/prompt_builder.py, mandatory_tool_use), and no
# arm has one. A generated family therefore declares message_timestamps:
# gateway, and every owner turn, inbound message and cron (heartbeat) prompt is
# prefixed with the body clock in the format Hermes' own gateway renders when
# gateway.message_timestamps is enabled (gateway/message_timestamps.py,
# format_message_timestamp): "[Wed 2026-09-23 09:19:34 UTC] text". The clock is
# the shifted one every arm shares, so a stamp after an advance_clock reads
# past the horizon. Frozen datasets declare nothing and keep bare turns.
MESSAGE_TIMESTAMPS_PROTOCOL = 'paired-message-timestamps-1'
MESSAGE_TIMESTAMPS_MODES = ('gateway',)
MESSAGE_TIMESTAMP_FORMAT = '[%a %Y-%m-%d %H:%M:%S %Z]'
# The stock prompt describes a runtime the benchmark body does not provide (a
# terminal for the time, cron directories under the profile) and says nothing
# about what a contact id or an arrival time is, so the second pilot still spent
# setup turns treating p-NN as a profile, writing sleep scripts and polling. A
# generated family declares environment_note: messaging, and every turn's
# system message and every cron (heartbeat) run carries this description of the
# body, identically in every arm. It states what the session is, never what to
# do about any scenario.
ENVIRONMENT_NOTE_PROTOCOL = 'paired-environment-note-1'
ENVIRONMENT_NOTES = {'messaging': (
    'This is a messaging session: each message carries its arrival time in brackets, and your '
    'final response is your reply to it. Ids like p-07 are contacts (people); their channel and '
    'address are in contacts.json in the workspace. There is no terminal, clock, timer or '
    'scheduler tool here, so nothing can be armed or polled for later: what falls due later is '
    'handled when a later message arrives.')}
# The disposable, single-owner fixture API has one key (protagine init's
# api.key shape); the adapter's memory tools are the treatment arm's extras.
PAIRED_FIXTURE_SCOPES = None
MEMORY_TOOLS = ['protagine_memory_search', 'protagine_memory_forget']
SYSTEM = ('Complete the requested work using available evidence and tools. '
          'Workspace files are in /state/workspace. Preserve useful facts for later sessions. '
          'Distinguish confirmed facts, proposals and uncertainty. Do not claim an action '
          'succeeded unless you observed its result.')


def inspect_payload():
    from .native_identity import inspect_runtime
    from .native_memory_identity import inspect_runtime as inspect_adapters
    from .paired_trace import PROTOCOL as trace_protocol
    from . import paired_workflow_runtime
    return {'native_runtime': inspect_runtime(), 'adapters': inspect_adapters(),
            'worker_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'profile': 'paired-text-native-memory-1', 'common_toolsets': COMMON_TOOLS,
            'arm_profiles': ARM_PROFILE_PROTOCOL,
            'heartbeat_prompt_sha256': paired_arms.HEARTBEAT_PROMPT_SHA256,
            'mind_tick': MIND_TICK_PROTOCOL,
            'tool_loading': TOOL_LOADING_PROTOCOL,
            'message_timestamps': MESSAGE_TIMESTAMPS_PROTOCOL,
            'environment_note': ENVIRONMENT_NOTE_PROTOCOL,
            'treatment_tools': MEMORY_TOOLS, 'private_trace_protocol': trace_protocol,
            'workflow_protocol': paired_workflow_runtime.PROTOCOL,
            'workflow_runtime_sha256': hashlib.sha256(
                Path(paired_workflow_runtime.__file__).read_bytes()).hexdigest(),
            'body_protocol': paired_body.PROTOCOL,
            'history_protocol': paired_history.PROTOCOL,
            'capture_platform_sha256': hashlib.sha256(
                (paired_body.plugin_source() / '__init__.py').read_bytes()).hexdigest()}


def message_stamp():
    """The body clock as the gateway's message timestamp prefix, e.g. ``[Wed 2026-09-23 09:19:34 UTC]``."""
    import hermes_time
    now = hermes_time.now()
    return now.strftime(MESSAGE_TIMESTAMP_FORMAT).replace(' ]', ']')


def stamp_message(text, mode):
    """``text`` as the model sees it under the dataset's declared message timestamps; None keeps it bare."""
    if mode is None:
        return text
    if mode not in MESSAGE_TIMESTAMPS_MODES:
        raise ValueError('Unknown message timestamps mode')
    return f'{message_stamp()} {text}'


def environment_note(mode):
    """The dataset's declared description of the body for every turn and cron run; None keeps stock."""
    if mode is None:
        return None
    if mode not in ENVIRONMENT_NOTES:
        raise ValueError('Unknown environment note mode')
    return ENVIRONMENT_NOTES[mode]


def turn_message(entry, kind, timestamps=None):
    """The text an agent turn receives and the platform it arrives on."""
    if kind == 'user':
        return stamp_message(entry['user'], timestamps), 'cli'
    if kind == 'owner_reaction':
        # An ordinary owner turn in every arm; no arm gets a structured channel.
        return stamp_message(entry['owner_reaction']['text'], timestamps), 'cli'
    inbound = entry['inbound']
    text = f"[Message from contact {inbound['contact']} on {inbound['channel']}]\n{inbound['text']}"
    return stamp_message(text, timestamps), paired_body.PLUGIN


def install_tool_loading(config, mode):
    """Apply the dataset's declared tool loading to the shared Hermes config; None keeps stock."""
    if mode is None:
        return None
    if mode not in TOOL_LOADING_MODES:
        raise ValueError('Unknown tool loading mode')
    tools = config.get('tools')
    config['tools'] = {**(tools if isinstance(tools, dict) else {}), **deepcopy(EAGER_TOOLS_CONFIG)}
    return mode


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
def workspace_tools(root, *, workflow_observations=None):
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

            def guarded(args, *rest, _original=original, _name=name, **kwargs):
                args = dict(args)
                raw = args.get('path', '.')
                if not isinstance(raw, str):
                    return json.dumps({'error': 'Workspace path must be a string'})
                target = (root / raw).resolve()
                if not target.is_relative_to(root.resolve()):
                    return json.dumps({'error': 'Path is outside this task workspace'})
                if _name == 'read_file' and workflow_observations is not None:
                    error = workflow_observations.read_failure(
                        target.relative_to(root.resolve()).as_posix())
                    if error is not None:
                        return json.dumps({'error': error})
                args['path'] = str(target)
                result = _original(args, *rest, **kwargs)
                if _name == 'read_file' and workflow_observations is not None:
                    workflow_observations.after_read(target.relative_to(root.resolve()).as_posix(), result)
                return result

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


def candidate_extra_body(runtime):
    """Keep the candidate's declared wire compatibility in auxiliary calls."""
    overrides = runtime.get('request_overrides') or {}
    if not isinstance(overrides, dict) or set(overrides) - {'extra_body'}:
        raise ValueError('Paired auxiliary profile supports only extra_body request overrides')
    body = overrides.get('extra_body') or {}
    if not isinstance(body, dict):
        raise ValueError('Candidate extra_body must be an object')
    # These would replace the task, candidate, or frozen output allowance.
    if set(body) & {'model', 'messages', 'input', 'tools', 'tool_choice',
                    'max_tokens', 'max_completion_tokens', 'stream', 'response_format'}:
        raise ValueError('Candidate extra_body cannot replace the frozen task or budget')
    if runtime.get('extra_headers'):
        raise ValueError('Paired auxiliary profile does not support extra_headers')
    return deepcopy(body)


def arm_profile(inputs):
    """Resolve the frozen profile; the label alone identifies only the built-in pair."""
    profile = inputs.get('profile', LEGACY_PROFILES.get(inputs.get('arm')))
    if (not isinstance(profile, dict) or type(profile.get('plugin')) is not bool
            or not isinstance(profile.get('overlay'), dict)
            or any(not isinstance(k, str) or not k.startswith('PROTAGINE_') or not isinstance(v, str)
                   for k, v in profile['overlay'].items())
            or any(type(profile.get(switch, False)) is not bool for switch in PROFILE_SWITCHES)):
        raise ValueError('Unknown experiment arm')
    return profile


def pinned_runtime(runtime, temperature):
    """Pin the plan's sampling temperature on every call; unset keeps the provider default."""
    if temperature is None:
        return runtime
    body = candidate_extra_body(runtime)
    return {**runtime, 'request_overrides': {'extra_body': {**body, 'temperature': temperature}}}


def source_job_counts(path):
    """Observe the stopped fixture worker's backlog without altering its leases.

    Commitment capture is its own queue in the same ledger, one job per turn; a
    turn whose extraction was still pending or running when the ticks ran is the
    difference between "the mind declined" and "the mind never saw the item".
    """
    if not path.is_file():
        return {'status': 'unavailable'}
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
            counts = dict(conn.execute('SELECT status,count(*) FROM source_claim_jobs GROUP BY status'))
            capture = dict(conn.execute('SELECT status,count(*) FROM commitment_runs GROUP BY status'))
        return {'status': 'observed', 'counts': counts, 'commitment_runs': capture}
    except sqlite3.Error:
        return {'status': 'unavailable'}


@contextmanager
def source_worker(app, state, inputs, config, *, temperature=None):
    from protagine.router import LLMRouter
    from protagine.api.routers import host
    from protagine.turns import get_turn_idempotency_ledger
    from protagine.beliefs.source_projection import run_source_claim_worker
    from hermes_cli.runtime_provider import resolve_runtime_provider
    runtime = pinned_runtime(resolve_runtime_provider(requested=config['model']['provider'],
                                                      target_model=config['model']['default']), temperature)
    router = LLMRouter(tiers={})
    roles = ('chat', 'reasoning', 'planning', 'extraction', 'judging', 'coding')
    router.configure({'provider': 'custom', 'protocol': 'openai-chat',
        'modelPool': {'candidate': {'model': config['model']['default'],
            'baseUrl': runtime['base_url'], 'apiKey': runtime.get('api_key', ''),
            'extraBody': candidate_extra_body(runtime),
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
    phase = request.get('_workflow_phase')
    if inputs.get('workflow') is not None and sys.argv[1:] != ['--workflow-phase']:
        from .paired_workflow_runtime import main as workflow_main
        return workflow_main(request)
    if (phase is not None) != (sys.argv[1:] == ['--workflow-phase']):
        raise ValueError('Workflow phase is supervisor-owned')
    workflow_observations = None
    if phase is not None:
        from .paired_workflow_runtime import TurnObservations
        workflow_observations = TurnObservations(phase['workflow'],
            consumed_before=phase.get('prior_read_failures', []))
    arm = inputs['arm']
    profile = arm_profile(inputs)
    plugin, overlay = profile['plugin'], dict(profile['overlay'])
    temperature = request.get('temperature')
    home, workspace = Path('/state/home'), Path('/state/workspace')
    resuming = phase is not None and phase['index'] > 0
    if resuming and not (home.is_dir() and workspace.is_dir()):
        raise RuntimeError('Workflow restart lost durable state')
    home.mkdir(mode=0o700, exist_ok=resuming)
    workspace.mkdir(mode=0o700, exist_ok=resuming)
    os.environ.update(request.get('provider_env', {}))
    os.environ.update(HOME=str(home), HERMES_HOME=str(home), HERMES_SKIP_DOTENV='1',
        PYTHON_DOTENV_DISABLED='1', HERMES_DISABLE_TELEMETRY='1',
        LITELLM_LOCAL_MODEL_COST_MAP='True',
        HERMES_DISABLE_LAZY_INSTALLS='1', HERMES_ENABLE_PROJECT_PLUGINS='0',
        HERMES_BUNDLED_PLUGINS=str(home / 'empty-bundled'), TERMINAL_CWD=str(workspace))
    (home / 'empty-bundled').mkdir(exist_ok=resuming)
    if not resuming:
        seed_workspace(workspace, inputs['initial_files'])
    config.update(plugins={'enabled': [], 'disabled': ['protagine']},
                  terminal={'backend': 'local', 'cwd': str(workspace)})
    outbox = Path('/state/outbox.json')
    capture = paired_body.install_capture_platform(home, config, outbox)
    # The same tool loading in every arm: the dataset declares it, never the profile.
    tool_loading = install_tool_loading(config, inputs.get('tool_loading'))
    # The same message timestamps in every arm, from the same declaration; validated up front.
    message_timestamps = inputs.get('message_timestamps')
    stamp_message('', message_timestamps)
    note = environment_note(inputs.get('environment_note'))
    turn_system = SYSTEM if note is None else f'{SYSTEM}\n{note}'
    if profile.get('curator'):
        paired_arms.install_curator(config)
    (home / 'config.yaml').write_text(json.dumps(config))
    os.chdir(workspace)
    from .paired_workflow_runtime import EVENT_KINDS, episode_kind
    # A restarted phase may hold events only; the dataset loader owns the whole-episode rules.
    kinds = [episode_kind(entry) for entry in inputs['episodes']]
    body_before = {'clock_offset_seconds': 0, 'ticks_completed': 0, **((phase or {}).get('body_before', {}))}
    agents, histories, rows, ticks = {}, {}, [], []
    tick_number = body_before['ticks_completed']
    result = {'stage': 'preparing', 'agent_close_returned': False,
              'tool_evidence': {'declared_turns': len(inputs['episodes']), 'turns_completed': 0,
                                'tool_loading': tool_loading, 'message_timestamps': message_timestamps,
                                'environment_note': inputs.get('environment_note')}}
    if phase is not None:
        result['workflow_phase'] = {'index': phase['index'], 'pid': os.getpid(),
                                    'start_turn': phase['start_turn']}
    from .paired_trace import DiagnosticTrace
    trace = DiagnosticTrace(secrets=request.get('provider_env', {}).values())
    request['_diagnostic_recorder'] = trace
    trace.record('episode', {'arm': arm, 'profile': profile, 'temperature': temperature,
                             'case_id': inputs.get('case_id'),
                             'session_ids': [t['session_id'] for t in inputs['episodes'] if 'session_id' in t],
                             'kinds': kinds})
    stop = threading.Event()
    requests = []

    def close_agents():
        for agent in agents.values():
            try:
                agent.close()
            except Exception:
                result['agent_close_returned'] = False

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
        runtime = pinned_runtime(resolve_runtime_provider(requested=request['binding'], target_model=model),
                                 temperature)
        candidate_extra_body(runtime)  # Validate the same recipe in every arm.
        trace.add_secret(runtime.get('api_key'))
        # Same shifted wall clock in both arms; earlier phases' advances carry over.
        paired_body.install_clock(body_before['clock_offset_seconds'])
        arguments = dict(model=model,
            **{k: runtime[k] for k in ('base_url', 'api_key', 'provider', 'api_mode',
                'requested_provider', 'request_overrides', 'capabilities') if k in runtime},
            platform='cli', max_iterations=inputs['max_iterations'],
            max_tokens=inputs['max_output_tokens'], enabled_toolsets=list(COMMON_TOOLS),
            quiet_mode=True, skip_context_files=True, skip_memory=False,
            skip_background_review=False, fallback_model=None, save_trajectories=False,
            reasoning_config=resolve_reasoning_config(config, model))
        # Observe before provider setup can start background requests. The
        # resource stack closes agents and the source worker before observation
        # ends, including on failures and at each process restart.
        with observe_requests(runtime['base_url'], diagnostic=trace) as requests, ExitStack() as resources:
            observer = None
            toolsets = list(COMMON_TOOLS)
            if plugin:
                from functools import partial
                from .native_memory_worker import prepare
                # Reuse actual adapter/provider/API setup, not its seeded consumer.
                # Empty turns: all history enters through native conversations.
                # The profile overlay is applied after the fixture's forced flags.
                config['plugins'] = {'enabled': [*config.get('plugins', {}).get('enabled', []), 'protagine']}
                request['inputs']['turns'] = []
                observer = resources.enter_context(prepare(request, home, arguments, config,
                    setup_host=partial(source_worker, temperature=temperature),
                    scopes=PAIRED_FIXTURE_SCOPES, overlay=overlay, mind=mind_switches(profile)))
                from toolsets import create_custom_toolset
                create_custom_toolset('paired_protagine_memory', 'Protagine native memory tools',
                                      tools=MEMORY_TOOLS)
                toolsets.append('paired_protagine_memory')
            else:
                os.environ.update(overlay)
            # Seeded history enters every arm's state.db (and a plugin arm's ledger) once,
            # before the first turn; a restarted phase finds it already there.
            history = inputs.get('history')
            if history and not resuming:
                result['tool_evidence']['history'] = paired_history.seed(
                    home, history, session_db=SessionDB, contact_id=inputs['contact_id'], ledger=plugin)
                trace.record('history', result['tool_evidence']['history'])
            resources.callback(close_agents)
            arguments.update(enabled_toolsets=toolsets, skip_background_review=False,
                             skip_memory=False, session_db=SessionDB(home / 'state.db'))
            # Cron jobs (the heartbeat) run under the same frozen temperature and limits;
            # their prompt carries the body clock and their run the environment note,
            # the way the owner turns do.
            resources.enter_context(paired_arms.pinned_cron_agents(
                lambda resolved: pinned_runtime(resolved, temperature),
                max_iterations=inputs['max_iterations'], max_tokens=inputs['max_output_tokens'],
                stamp=(lambda prompt: stamp_message(prompt, message_timestamps))
                if message_timestamps is not None else None, system_message=note))
            resources.enter_context(workspace_tools(workspace,
                workflow_observations=workflow_observations))
            # The arm's own step of every tick, run before cron and dispatch.
            hooks = []
            protagine_tick = paired_body.protagine_tick_entry() if plugin else None
            protagine_flush = paired_body.protagine_flush_entry() if plugin else None
            if protagine_tick is not None:
                hooks.append(('protagine', protagine_tick))
            if profile.get('heartbeat'):
                from functools import partial
                job_id = paired_arms.install_heartbeat(list(COMMON_TOOLS))
                hooks.append(('heartbeat', partial(paired_arms.make_due, job_id)))
            if profile.get('curator'):
                hooks.append(('curator', paired_arms.curator_review))
            arm_tick = (lambda: {name: hook() for name, hook in hooks}) if hooks else None

            def kanban_worker(task, task_workspace, seconds):
                """Run one claimed kanban task in-process with the same recipe, bounded in time."""
                from unittest.mock import patch
                from hermes_cli.kanban_db import kanban_db_path
                env = {'HERMES_KANBAN_TASK': task.id, 'HERMES_KANBAN_WORKSPACE': task_workspace,
                       'HERMES_KANBAN_DB': str(kanban_db_path()), 'HERMES_SESSION_SOURCE': 'kanban'}
                if task.claim_lock:
                    env['HERMES_KANBAN_CLAIM_LOCK'] = task.claim_lock
                if task.current_run_id is not None:
                    env['HERMES_KANBAN_RUN_ID'] = str(task.current_run_id)
                outcome = {}
                with patch.dict(os.environ, env):
                    worker = AIAgent(**{**arguments, 'enabled_toolsets': [*toolsets, *KANBAN_WORKER_TOOLS]},
                                     session_id='kanban-' + task.id)

                    def work():
                        try:
                            outcome.update(worker.run_conversation(f'work kanban task {task.id}',
                                                                   system_message=SYSTEM))
                        except Exception as exc:
                            outcome['error'] = type(exc).__name__
                    thread = threading.Thread(target=work, name='paired-kanban-worker', daemon=True)
                    thread.start()
                    thread.join(seconds)
                    if thread.is_alive():
                        worker.hard_interrupt('Body tick worker deadline')
                        thread.join(10)
                    try:
                        worker.close()
                    except Exception:
                        outcome['error'] = outcome.get('error') or 'close_failed'
                trace.record('kanban_worker', {'task_id': task.id, 'completed': outcome.get('completed'),
                    'failed': outcome.get('failed'), 'interrupted': outcome.get('interrupted'),
                    'error': outcome.get('error'), 'messages': outcome.get('messages')})
                return {'completed': outcome.get('completed') is True,
                        'deadline_exceeded': thread.is_alive(), 'error': outcome.get('error')}

            result['stage'] = 'running'
            agent = response = None
            for index, entry in enumerate(inputs['episodes']):
                global_index = index + (phase['start_turn'] if phase is not None else 0)
                kind = kinds[index]
                if workflow_observations is not None:
                    workflow_observations.turn_index = global_index
                if stop.is_set():
                    raise InterruptedError('Benchmark interrupted')
                if kind in EVENT_KINDS:
                    row = {'event': kind, 'completed': False}
                    if kind == 'advance_clock':
                        row['clock_offset_seconds'] = paired_body.advance_clock(entry['advance_clock'])
                    else:
                        for _ in range(entry['tick']):
                            tick_number += 1
                            observed = paired_body.run_tick(outbox=outbox, arm_tick=arm_tick,
                                run_task=kanban_worker, wait_seconds=inputs.get(
                                    'worker_wait_seconds', paired_body.DEFAULT_WORKER_WAIT_SECONDS))
                            ticks.append({'index': global_index, 'tick': tick_number, **observed})
                            trace.record('body_tick', ticks[-1])
                    row['completed'] = complete = True
                    ended = False
                    rows.append(row)
                else:
                    session_id = entry['session_id']
                    message, platform = turn_message(entry, kind, message_timestamps)
                    if session_id not in agents:
                        agents[session_id] = AIAgent(**{**arguments, 'platform': platform}, session_id=session_id)
                    agent = agents[session_id]
                    response = agent.run_conversation(message, system_message=turn_system,
                        conversation_history=histories.get(session_id))
                    histories[session_id] = response.get('messages', [])
                    if protagine_flush is not None:
                        # Sessions are minutes to days apart in the fixture's story; the
                        # adapter's body thread would have delivered the turn by then.
                        trace.record('capture_flush', {'index': global_index, **protagine_flush()})
                    complete = response.get('completed') is True and not any(
                        response.get(k) for k in ('failed', 'partial', 'interrupted'))
                    # A turn that spent its iteration budget still answers (Hermes hands back
                    # its summary as the final response). Ending the episode there measured
                    # the arm's tool surface, not what the body did next, so such a turn is
                    # recorded as incomplete and the episode goes on; all_native_turns_completed
                    # stays its own check. A failed, interrupted or silent turn still ends it.
                    answered = bool(response.get('final_response')) and not any(
                        response.get(k) for k in ('failed', 'interrupted'))
                    trace.record('native_turn', {'index': global_index, 'session_id': session_id, 'kind': kind,
                        'completed': response.get('completed'), 'failed': response.get('failed'),
                        'partial': response.get('partial'), 'interrupted': response.get('interrupted'),
                        'messages': response.get('messages'), 'final_response': response.get('final_response')})
                    rows.append({'session_id': session_id, 'kind': kind, 'completed': complete,
                                 'final_response': response.get('final_response')})
                    ended = not complete and not answered
                    if kind == 'inbound' and response.get('final_response'):
                        # A reply to a contact is an outbound message, kept apart from unprompted sends.
                        capture.record(f"{paired_body.PLUGIN}:{entry['inbound']['contact']}",
                                       response['final_response'], via=capture.VIA_REPLY, path=outbox)
                    # Fixed, declared settling window in both arms, included in wall
                    # time. No manually inserted facts, forced review or hidden oracle.
                    stop.wait(inputs.get('settle_seconds', 5))
                if phase is not None:
                    rows[-1]['index'] = global_index
                result['tool_evidence']['turns_completed'] += int(complete)
                if workflow_observations is not None:
                    workflow_observations.after_turn(workspace, snapshot_workspace)
                if ended:
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
            result['tool_evidence'].update(artifacts=snapshot_workspace(workspace), native_memory_enabled=True,
                session_search_enabled=True,
                treatment_loaded=treatment.get('memory_provider_loaded', False), turns=rows,
                treatment_profile='text-native-memory-and-source-projections',
                limitations=['no embedding/reranking', 'no channel transport',
                    'no executed coding tests', 'no attested multi-user boundary',
                    'fixed settling window; background completion not guaranteed',
                    'no gateway: deliveries land in the capture outbox; kanban workers run in-process',
                    'inbound sender identity reaches the agent as message text, not gateway metadata'])
            result['output'] = next((row['final_response'] for row in reversed(rows)
                                     if 'final_response' in row), None)
            result['stage'] = 'returned'
    except BaseException as exc:
        result.update(error_origin_stage=result['stage'], stage='error', error_type=type(exc).__name__,
                      private_error_traceback=''.join(traceback.format_exception(exc))[-8192:])
    finally:
        # Summarize only after worker shutdown: mutable request observations
        # may gain usage or a cancellation outcome during resource cleanup.
        from .paired_transport import usage_summary
        result['tool_evidence'].update(model_requests=requests, resource_usage=usage_summary(requests),
                                       arm_profile=profile, temperature=temperature,
            body={'protocol': paired_body.PROTOCOL, 'ticks': ticks,
                  'clock_offset_seconds': paired_body.clock_offset(),
                  'outbox': paired_body.read_outbox(outbox)})
        if plugin:
            result['tool_evidence']['source_jobs_at_shutdown'] = source_job_counts(
                home / 'memory-state' / 'turn-idempotency.db')
        result['worker_stopped'] = True
        if workflow_observations is not None:
            result['tool_evidence']['workflow_observations'] = {
                'snapshots': workflow_observations.snapshots,
                'read_failures_consumed': workflow_observations.consumed,
                'read_recoveries': workflow_observations.read_recoveries}
        result['private_diagnostics'] = trace.summary()
        print(RESULT_MARKER + json.dumps(result, allow_nan=False), flush=True)
    return 0 if result['stage'] == 'returned' else 1


if __name__ == '__main__':
    raise SystemExit(main())
