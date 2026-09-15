"""Own one optional ordinary-skill review job in the selected native cron store."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess

DEFAULT_SCHEDULE = '0 */6 * * *'
NAME = 'Protagine ordinary skill review'


def choices(args, ask, *, existing=None, prompt=True):
    """Omission retains an existing choice; a fresh interactive choice defaults off."""
    enabled = getattr(args, 'ordinary_skill_review', None)
    schedule = getattr(args, 'skill_review_schedule', None)
    evaluator = getattr(args, 'skill_review_evaluator', None)
    if enabled is False and (schedule is not None or evaluator is not None):
        raise ValueError('Disabling ordinary-skill review cannot include schedule or evaluator options')
    if enabled is None and (schedule is not None or evaluator is not None):
        if not (existing or {}).get('enabled'):
            raise ValueError('Select --ordinary-skill-review before configuring its cadence or evaluator')
        enabled = True
    if enabled is None and not existing and prompt and not getattr(args, 'non_interactive', False):
        enabled = ask('Schedule ordinary-skill review proposals using the planning role? [y/N]', 'N').lower() in {'y', 'yes'}
    if enabled is not True:
        return {'enabled': enabled}
    schedule = schedule if schedule is not None else (existing or {}).get('schedule', DEFAULT_SCHEDULE)
    evaluator = evaluator if evaluator is not None else (existing or {}).get('evaluator_path')
    if prompt and not getattr(args, 'non_interactive', False):
        schedule = ask('Ordinary-skill review schedule', schedule, True)
        evaluator = ask('Optional evaluator declaration path (blank keeps proposals pending)', evaluator or '')
    if not isinstance(schedule, str) or not schedule.strip() or any(ord(c) < 32 for c in schedule):
        raise ValueError('The ordinary-skill review schedule must fit on one line')
    if evaluator:
        path = Path(evaluator).expanduser().resolve()
        if not path.is_file():
            raise ValueError('The selected skill-review evaluator declaration does not exist')
        evaluator = str(path)
    return {'enabled': True, 'schedule': schedule.strip(), 'evaluator_path': evaluator or None}


_NATIVE = r'''
import json, sys
from cron.jobs import create_job, get_job, pause_job, remove_job, resume_job, update_job, parse_schedule
data = json.load(sys.stdin)
operation = data['operation']
expected = data.get('expected')
job = None
if expected:
    job = get_job(expected['job_id'])
    if job:
        fields = {'name': data['name'], 'prompt': '', 'script': expected['script'],
                  'no_agent': True, 'deliver': 'local', 'workdir': data['state']}
        if (any(job.get(key) != value for key, value in fields.items())
                or job['schedule'] != parse_schedule(expected['schedule'])
                or any(job.get(key) for key in ('model', 'provider', 'base_url', 'skills',
                       'context_from', 'origin', 'monitor_script', 'monitor_url'))):
            raise ValueError('The managed ordinary-skill review job was edited; reconcile it before setup')
if operation in ('validate', 'prepare'):
    parsed = parse_schedule(data['schedule'])
    if parsed['kind'] == 'once':
        raise ValueError('Ordinary-skill review requires a recurring native schedule')
if operation == 'prepare':
    if job:
        pause_job(job['id'], reason='Protagine installer is refreshing the managed binding')
        job = update_job(job['id'], {'schedule': data['schedule']})
    else:
        job = create_job(prompt='', schedule=data['schedule'], name=data['name'],
            script=data['script'], no_agent=True, deliver='local', workdir=data['state'],
            paused=True, paused_reason='Protagine installer is preparing the managed binding')
elif operation == 'pause' and job:
    job = pause_job(job['id'], reason='Protagine ordinary-skill review is being disabled')
elif operation == 'resume':
    if not job:
        raise ValueError('The prepared ordinary-skill review job is missing')
    job = resume_job(job['id'])
    try:
        from cron.scheduler_provider import resolve_cron_scheduler
        resolve_cron_scheduler().register_job(job)
    except Exception:
        pause_job(job['id'], reason='Protagine scheduler registration failed; retry setup')
        raise
elif operation == 'remove' and job:
    remove_job(job['id'])
print(json.dumps({'job_id': job['id'] if job else None}))
'''


def _native(manifest, state, operation, **values):
    result = subprocess.run([manifest['hermes_python'], '-B', '-c', _NATIVE],
        input=json.dumps({'operation': operation, 'name': NAME, 'state': str(state), **values}),
        env=dict(os.environ, HERMES_HOME=manifest['hermes_home']),
        capture_output=True, text=True, timeout=30)
    if result.returncode:
        # Runtime tracebacks can contain private configuration. Keep them out of the wizard.
        raise ValueError('Native ordinary-skill review '+operation+' failed; check the selected job and runtime')
    return json.loads(result.stdout.splitlines()[-1])


def launcher(state, manifest):
    binding = manifest['adapter_binding']
    adapter = (state/'adapter'/'protagine_hermes' if binding['mode'] == 'private-directory'
               else Path(binding['sources']['protagine_hermes']))
    module = adapter/'ordinary_skill_review.py'
    if not module.is_file():
        raise ValueError('Refresh the selected adapter before enabling ordinary-skill review')
    code = ('import runpy, sys; '
            f'sys.path.insert(0, {str(adapter.parent)!r}); '
            f'sys.argv = ["ordinary_skill_review", "--instance", {str(state)!r}]; '
            f'runpy.run_path({str(module)!r}, run_name="__main__")')
    return ('#!/bin/sh\nexec '+shlex.join([manifest['hermes_python'], '-B', '-c', code])+'\n').encode()


def configure(state, *, enabled=True, schedule=None, evaluator_path=None):
    """Prepare paused, bind ownership, then activate; remove only verified owned bytes."""
    from .setup import _atomic_hermes_config_write
    state = Path(state).resolve()
    path = state/'instance.json'
    before = path.read_bytes()
    manifest = json.loads(before)
    if manifest.get('version') != 1 or manifest.get('profile') != 'local':
        raise ValueError('Ordinary-skill review requires a supported local attachment')
    home = Path(manifest['hermes_home']).resolve()
    previous = manifest.get('ordinary_skill_review') or {}
    script = home/'scripts'/('protagine-skill-review-'+hashlib.sha256(str(state).encode()).hexdigest()[:16]+'.sh')
    if script.parent.is_symlink() or script.is_symlink():
        raise ValueError('The managed ordinary-skill review script must not be symlinked')
    original = script.read_bytes() if script.exists() else None
    expected = previous if previous.get('job_id') else None
    if expected and previous.get('script') != str(script):
        raise ValueError('The recorded ordinary-skill review script belongs to another binding')
    if original is not None and (not expected or hashlib.sha256(original).hexdigest() != previous.get('script_sha256')):
        raise ValueError('The ordinary-skill review script was edited; reconcile it before setup')
    if not enabled:
        if not expected:
            return previous
        _native(manifest, state, 'pause', expected=expected)
        disabled = {**previous, 'enabled': False}
        after = json.dumps({**manifest, 'ordinary_skill_review': disabled}, indent=2)+'\n'
        _atomic_hermes_config_write(path, before, after.encode())
        _native(manifest, state, 'remove', expected=expected)
        if original is not None:
            if script.is_symlink() or script.read_bytes() != original:
                raise ValueError('The review script changed during removal; retain the concurrent edit')
            script.unlink()
        # Keep the removed job identity and last configuration as its ownership receipt.
        return disabled
    raw = launcher(state, manifest)
    from .router.native_policy import planning
    import asyncio
    configuration = Path(manifest.get('model_configuration_path') or state/'.protagine-llm-config.json')
    asyncio.run(planning(json.loads(configuration.read_text())))
    schedule = schedule or previous.get('schedule') or DEFAULT_SCHEDULE
    if evaluator_path and not Path(evaluator_path).is_file():
        raise ValueError('The selected skill-review evaluator declaration does not exist')
    prepared = _native(manifest, state, 'prepare', expected=expected, schedule=schedule, script=str(script))
    binding = {'enabled': True, 'job_id': prepared['job_id'], 'schedule': schedule,
        'script': str(script), 'script_sha256': hashlib.sha256(raw).hexdigest(),
        'evaluator_path': evaluator_path, 'role': 'planning'}
    try:
        script.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _atomic_hermes_config_write(script, original, raw)
        os.chmod(script, 0o700)
        after = json.dumps({**manifest, 'ordinary_skill_review': binding}, indent=2)+'\n'
        _atomic_hermes_config_write(path, before, after.encode())
    except Exception:
        if script.is_file() and not script.is_symlink() and script.read_bytes() == raw:
            if original is None:
                script.unlink()
            else:
                _atomic_hermes_config_write(script, raw, original)
        if not expected or prepared['job_id'] != expected['job_id']:
            _native(manifest, state, 'remove', expected=binding)
        elif schedule != previous['schedule']:
            _native(manifest, state, 'prepare', expected=binding, schedule=previous['schedule'], script=str(script))
        raise
    _native(manifest, state, 'resume', expected=binding)
    return binding
