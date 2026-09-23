"""The body tick and the capture outbox, identical in every arm.

One tick runs the arm's own step first (the Protagine tick in plugin arms,
making the heartbeat job due or the curator pass in the comparator arms, nothing
in base), then Hermes cron ``tick()``, then kanban ``dispatch_once`` with the
ready workers run in-process and awaited up to a bound. ``advance_clock`` shifts the two wall
clocks Hermes reads, faketime-style, without touching monotonic clocks.
"""
from datetime import timedelta
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import time

PROTOCOL = 'paired-body-tick-1'
PLUGIN = 'capture'
OWNER = 'owner'
DEFAULT_WORKER_WAIT_SECONDS = 120
MAX_SPAWN_PER_TICK = 8
MAX_SNAPSHOT_TASKS = 64
TEXT_BOUND = 2048
DISPATCH_FIELDS = ('spawned', 'promoted', 'reclaimed', 'crashed', 'timed_out', 'stale', 'auto_blocked',
                   'skipped_unassigned', 'skipped_nonspawnable', 'respawn_guarded')
_ORIGINAL = {}
_OFFSET = [0.0]


def plugin_source():
    return Path(__file__).resolve().parents[3] / 'benchmarks' / 'paired' / 'capture_platform'


def capture_module(source=None):
    """The plugin's own outbox writer, loaded from its directory."""
    source = Path(source or plugin_source())
    spec = importlib.util.spec_from_file_location('paired_capture_platform', source / '__init__.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def install_capture_platform(home, config, outbox, *, source=None):
    """Copy the plugin into the isolated profile and enable it; same config in every arm."""
    source = Path(source or plugin_source())
    target = Path(home) / 'plugins' / PLUGIN
    if not target.exists():
        shutil.copytree(source, target, ignore=shutil.ignore_patterns('__pycache__', '*.md'))
    module = capture_module(source)
    # An episode that never delivers is observed as an empty outbox, not a missing
    # one; a restarted phase keeps the entries already recorded.
    if not Path(outbox).exists():
        Path(outbox).write_text('[]')
    os.environ[module.OUTBOX_ENV] = str(outbox)
    os.environ[module.HOME_CHANNEL_ENV] = OWNER
    plugins = config.setdefault('plugins', {})
    enabled = plugins.setdefault('enabled', [])
    if PLUGIN not in enabled:
        enabled.append(PLUGIN)
    config['platforms'] = {**config.get('platforms', {}), PLUGIN: {'enabled': True}}
    # Unassigned model-created tasks dispatch to the only profile that exists,
    # and delivered text is the agent's own message, not a cron wrapper.
    config['kanban'] = {**config.get('kanban', {}), 'default_assignee': 'default'}
    config['cron'] = {**config.get('cron', {}), 'wrap_response': False}
    # Cron jobs run on the episode's configured endpoint: a job's creation
    # snapshot collapses a named provider to bare "custom", which has no URL here.
    provider = config.get('model', {}).get('provider')
    if provider:
        config['cron'].update(model_provider=provider, model=config['model']['default'])
    return module


def read_outbox(path):
    """The outbox array, or None when it is missing or not an array."""
    try:
        rows = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return rows if isinstance(rows, list) else None


def _shifted_time():
    return _ORIGINAL['time']() + _OFFSET[0]


def _shifted_now():
    return _ORIGINAL['now']() + timedelta(seconds=_OFFSET[0])


def _rebind_clock_aliases():
    # Modules bind ``from hermes_time import now as _hermes_now`` at import;
    # anything imported after install already sees the shifted function.
    for module in list(sys.modules.values()):
        for name, value in list(getattr(module, '__dict__', {}).items()):
            if value is _ORIGINAL['now']:
                setattr(module, name, _shifted_now)


def install_clock(offset_seconds=0):
    """Shift ``time.time`` (kanban timestamps, claims) and ``hermes_time.now`` (cron due times)."""
    import hermes_time
    if 'time' not in _ORIGINAL:
        _ORIGINAL['time'], _ORIGINAL['now'] = time.time, hermes_time.now
        time.time = _shifted_time
        hermes_time.now = _shifted_now
    _OFFSET[0] = float(offset_seconds)
    _rebind_clock_aliases()
    return _OFFSET[0]


def advance_clock(seconds):
    if 'time' not in _ORIGINAL:
        raise RuntimeError('Clock is not installed')
    _OFFSET[0] += float(seconds)
    _rebind_clock_aliases()
    return _OFFSET[0]


def clock_offset():
    return _OFFSET[0]


def uninstall_clock():
    if 'time' in _ORIGINAL:
        import hermes_time
        time.time, hermes_time.now = _ORIGINAL.pop('time'), _ORIGINAL.pop('now')
    _OFFSET[0] = 0.0


def _protagine_entry(name):
    """A callable the loaded Protagine plugin module defines; None in every other arm."""
    try:
        from hermes_cli.plugins import get_plugin_manager
        loaded = get_plugin_manager()._plugins.get('protagine')
    except Exception:
        return None
    if loaded is None or not loaded.enabled:
        return None
    entry = getattr(loaded.module, name, None)
    return entry if callable(entry) else None


def protagine_tick_entry():
    """The loaded Protagine plugin's ``tick`` when it defines one; None in every other arm."""
    return _protagine_entry('tick')


def protagine_flush_entry():
    """The adapter's ``flush``: deliver captured turns now, as its body thread would shortly."""
    return _protagine_entry('flush')


def _bounded(text):
    text = '' if text is None else str(text)
    return text[:TEXT_BOUND]


def kanban_snapshot(conn):
    from hermes_cli import kanban_db
    rows = []
    for task in kanban_db.list_tasks(conn, include_archived=True):
        rows.append({'id': task.id, 'title': _bounded(task.title), 'body': _bounded(task.body),
                     'status': task.status, 'assignee': task.assignee, 'created_by': task.created_by,
                     'created_at': task.created_at, 'completed_at': task.completed_at,
                     'result': _bounded(task.result)})
    rows.sort(key=lambda row: (row['created_at'] or 0, row['id']))
    return rows[:MAX_SNAPSHOT_TASKS]


def run_tick(*, outbox, arm_tick=None, run_task=None, wait_seconds=DEFAULT_WORKER_WAIT_SECONDS):
    """One body tick. ``arm_tick()`` is the arm's own step; ``run_task(task, workspace, seconds)``
    runs a claimed task in-process."""
    from cron.scheduler import tick as cron_tick
    from hermes_cli import kanban_db_connect, kanban_db_dispatch
    before = read_outbox(outbox)
    row = {'outbox_before': len(before or []), 'arm_tick': None, 'workers': []}
    queued = []

    def spawn(task, workspace, board=None):
        # No worker pid: the task runs in this process, and Hermes signals a recorded
        # pid when it reclaims an expired claim. Without one, a task a worker left
        # running is released on expiry the way a vanished worker's would be.
        queued.append((task, workspace))
        return None

    with kanban_db_connect.connect_closing() as conn:
        # Tasks created by the arm's step or by a cron job count as this tick's effects
        # and are dispatched by this same tick.
        existing = {task['id'] for task in kanban_snapshot(conn)}
        if arm_tick is not None:
            row['arm_tick'] = arm_tick()
        row['cron_jobs_run'] = int(cron_tick(verbose=False, sync=True))
        result = kanban_db_dispatch.dispatch_once(conn, spawn_fn=spawn, max_spawn=MAX_SPAWN_PER_TICK,
                                                  default_assignee='default')
        row['dispatch'] = {name: len(getattr(result, name)) if isinstance(getattr(result, name), list)
                           else getattr(result, name) for name in DISPATCH_FIELDS}
        deadline = time.monotonic() + wait_seconds
        for task, workspace in queued:
            remaining = max(0.0, deadline - time.monotonic())
            outcome = run_task(task, workspace, remaining) if run_task is not None else 'no_worker'
            row['workers'].append({'task_id': task.id, 'outcome': outcome})
        row['kanban'] = kanban_snapshot(conn)
    row['created_task_ids'] = [task['id'] for task in row['kanban'] if task['id'] not in existing]
    row['outbox_after'] = len(read_outbox(outbox) or [])
    return row
