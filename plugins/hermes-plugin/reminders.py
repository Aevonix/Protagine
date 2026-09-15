"""Source-bound reminders on Hermes' existing cron store and delivery path.

The model selects a remembered claim, never a replacement date or recipient.
Native cron owns scheduling, claims, execution history and delivery retries.
Only references live in the job; source text is read when the reminder fires.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re

logger = logging.getLogger(__name__)
KIND = 'protagine-source-reminder-v1'
ENDPOINT = '/v1/host/memory/sources/deadline'
SCHEMA = {
    'name': 'protagine_reminder',
    'description': 'Remind the owner about a recalled deadline. Schedule uses the exact supplied source revision and claim ID; later explicit corrections move the reminder and forgotten evidence cancels it. lead_seconds schedules before the deadline. Uses this conversation for delivery; CLI output is local. Inspect or cancel by the returned native job_id. Use native cronjob_manage for schedules unrelated to memory.',
    'parameters': {'type': 'object', 'properties': {
        'operation': {'type': 'string', 'enum': ['schedule', 'inspect', 'cancel']},
        'source_id': {'type': 'string'}, 'source_version': {'type': 'string'},
        'claim_id': {'type': 'string'}, 'job_id': {'type': 'string'},
        'lead_seconds': {'type': 'integer', 'minimum': 0, 'maximum': 2592000}},
        'required': ['operation'], 'additionalProperties': False},
}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def instant(value):
    date = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if date.tzinfo is None:
        raise ValueError('An explicit timezone is required')
    return date.astimezone(timezone.utc)


def sibling(name):
    import importlib.util
    import sys
    key = '_protagine_reminder_' + name
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, Path(__file__).with_name(name + '.py'))
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


class NativeReminders:
    @staticmethod
    def available():
        try:
            from cron.owned_output import payload_fingerprint, snapshot, erase
        except ImportError:
            return False
        return all(callable(method) for method in (payload_fingerprint, snapshot, erase))

    def __init__(self, client, owner, request_memory=None, *, home=None, outbox=None):
        self.client, self.owner, self.request_memory = client, owner, request_memory
        self.outbox = outbox if outbox is not None else getattr(request_memory, 'outbox', None)
        if home is None:
            from hermes_cli.config import get_hermes_home
            home = get_hermes_home()
        self.home = Path(home).resolve()

    def _bindings(self):
        from cron.jobs import list_jobs
        for job in list_jobs(include_disabled=True):
            if not str(job.get('script', '')).startswith('protagine-reminder-'):
                continue
            try:
                binding = json.loads(job['prompt'])
            except (ValueError, TypeError, KeyError):
                continue
            if (isinstance(binding, dict) and binding.get('kind') == KIND
                    and binding.get('contact_id') == self.owner
                    and job['script'] == self._script_name(binding['binding_id'])
                    and job.get('no_agent') is True):
                yield job, binding

    @staticmethod
    def _script_name(binding_id):
        if not re.fullmatch('[0-9a-f]{64}', binding_id):
            raise ValueError('Invalid reminder identity')
        return 'protagine-reminder-' + binding_id + '.py'

    def _launcher(self, binding_id):
        script = self.home/'scripts'/self._script_name(binding_id)
        script.parent.mkdir(parents=True, exist_ok=True)
        # The launcher carries only identity and the installed implementation.
        # Reconciliation refreshes it after a plugin upgrade.
        content = ('import runpy, sys\n'
                   f'sys.argv = [{str(Path(__file__).resolve())!r}, {binding_id!r}]\n'
                   f'runpy.run_path({str(Path(__file__).resolve())!r}, run_name="__main__")\n')
        if script.exists() and script.read_text() == content:
            return script.name
        temporary = script.with_suffix('.pending')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
        os.replace(temporary, script)
        return script.name

    def _current(self, binding, scope=None):
        response = self.client.post(ENDPOINT, timeout=3, json={key: binding[key] for key in (
            'contact_id', 'session_id', 'source_id', 'source_version', 'claim_id', 'timezone_name')})
        response.raise_for_status()
        result = response.json()
        ownership = getattr(self.request_memory, 'ownership', None)
        if scope is not None and ownership is not None and result.get('source_refs'):
            if not ownership.retain(scope, result['source_refs']):
                raise ValueError('Native source ownership could not retain this deadline read')
        return result

    @staticmethod
    def _scheduled_at(current, binding):
        return (instant(current['deadline_at']) - timedelta(seconds=binding['lead_seconds'])).isoformat()

    @staticmethod
    def _view(job, current=None):
        binding = json.loads(job['prompt'])
        return {key: job.get(key) for key in ('id', 'state', 'enabled', 'next_run_at',
            'last_status', 'last_delivery_error', 'last_delivery_unverified', 'deliver')} | {
            'job_id': job['id'], 'current_deadline': current,
            'binding_id': binding['binding_id'],
            'timezone_name': binding['timezone_name'],
            'timezone_basis': binding.get('timezone_basis', 'caller_override'),
            'delivery_confirmation': 'not_checked',
            'note': 'Scheduling or rendering does not confirm delivery. Native cron retains the delivery result.'}

    @staticmethod
    def _script_failed(job):
        # Native delivery failures/unknown outcomes belong to its delivery
        # queue. Retry only a script that never returned a successful result.
        return job.get('last_status') == 'error' and str(job.get('last_error') or '').startswith((
            'Script exited with code ', 'Script timed out after ', 'Script execution failed: '))

    def handle(self, args, scope, context=None):
        from cron import jobs
        try:
            if (scope is None or not scope.valid_participant or scope.contact_id != self.owner
                    or scope.authority_lane != 'owner' or not scope.turn_id
                    or scope.platform in {'cron', 'subagent', 'background_review'}):
                raise ValueError('A current owner conversation is required')
            with jobs.use_cron_store(self.home), jobs._jobs_lock():
                operation = args.get('operation')
                if operation in {'inspect', 'cancel'} and set(args) == {'operation', 'job_id'}:
                    pair = next(((job, binding) for job, binding in self._bindings()
                                 if job['id'] == args['job_id']), None)
                    if pair is None:
                        raise ValueError('Unknown source-bound reminder')
                    job, binding = pair
                    if operation == 'cancel':
                        job = jobs.pause_job(job['id'], reason='Cancelled by owner')
                        return encoded(self._view(job))
                    return encoded(self._view(job, self._current(binding, scope)))
                required = {'operation', 'source_id', 'source_version', 'claim_id'}
                if operation != 'schedule' or not required <= set(args) or set(args)-required-{'lead_seconds'}:
                    raise ValueError('Use schedule with exact recalled source and claim, or inspect/cancel with job_id')
                lead = args.get('lead_seconds', 0)
                if type(lead) is not int or not 0 <= lead <= 2592000:
                    raise ValueError('lead_seconds must be a nonnegative integer, at most 30 days')
                ref = {key: args[key] for key in ('source_id', 'source_version')}
                supplied = self.request_memory.supplied_snapshot(scope) if self.request_memory else []
                if ref not in (supplied or []):
                    raise ValueError('Use a source revision supplied in this turn; recall it first')
                from hermes_time import get_timezone
                from tools.cronjob_job_args import _origin_from_env
                configured_zone = get_timezone()
                zone = configured_zone.key if configured_zone is not None else None
                binding = dict(kind=KIND, contact_id=self.owner, session_id=scope.session_id,
                    source_id=args['source_id'], source_version=args['source_version'],
                    claim_id=args['claim_id'], timezone_name=zone, lead_seconds=lead)
                current = self._current(binding, scope)
                if current.get('status') != 'current':
                    return encoded({'error': 'This source does not provide a current precise deadline', 'deadline': current})
                # Freeze the resolved communication frame, including when the
                # native profile delegates to its server-local default. Missing
                # profile configuration is not an instruction to use UTC.
                from zoneinfo import ZoneInfo
                binding['timezone_name'] = ZoneInfo(current['timezone_name']).key
                binding['timezone_basis'] = current['timezone_basis']
                identity = hashlib.sha256(encoded([self.owner, current['root_claim_id'], lead]).encode()).hexdigest()
                binding['binding_id'] = identity
                previous = next((job for job, record in self._bindings()
                                 if record['binding_id'] == identity), None)
                if previous is not None:
                    return encoded(self._view(previous, current))
                schedule = self._scheduled_at(current, binding)
                if instant(schedule) <= datetime.now(timezone.utc):
                    raise ValueError('The reminder time has already passed; choose a smaller lead or clarify the deadline')
                script = self._launcher(identity)
                # Require the native ownership interface before scheduling a
                # job that can create remembered text outside a conversation.
                from cron.owned_output import payload_fingerprint
                if not callable(payload_fingerprint):
                    raise ValueError('Native cron source ownership is unavailable')
                if self.outbox is None:
                    raise ValueError('Native source ownership is unavailable')
                from cron.scheduler import create_job_with_scheduler_registration
                job = create_job_with_scheduler_registration(prompt=encoded(binding), schedule=schedule,
                    name='Remembered deadline', script=script, no_agent=True, repeat=1,
                    origin=_origin_from_env(), attach_to_session=True)
                return encoded(self._view(job, current))
        except Exception as error:
            return encoded({'error': str(error) if isinstance(error, ValueError) else type(error).__name__,
                            'scheduling_confirmed': False})

    def reconcile(self, **kwargs):
        if kwargs.get('dry_run') or kwargs.get('board') != 'default' or os.environ.get('HERMES_KANBAN_TASK'):
            return
        from cron import jobs
        from cron.scheduler_provider import resolve_cron_scheduler
        with jobs.use_cron_store(self.home):
            for job, binding in list(self._bindings()):
                # User pauses and completed delivered occurrences stay closed.
                if job.get('state') == 'paused' or (job.get('protagine_rendered_claim_id') and not self._script_failed(job)):
                    continue
                try:
                    current = self._current(binding)
                    with jobs._jobs_lock():
                        fresh = jobs.get_job(job['id'])
                        if not fresh or fresh.get('state') == 'paused' or (fresh.get('protagine_rendered_claim_id') and not self._script_failed(fresh)):
                            continue
                        if current.get('status') != 'current':
                            jobs.pause_job(job['id'], reason='Remembered deadline evidence is unavailable or unresolved')
                            continue
                        target = self._scheduled_at(current, binding)
                        self._launcher(binding['binding_id'])
                        if instant(target) < datetime.now(timezone.utc)-timedelta(seconds=120):
                            jobs.pause_job(job['id'], reason='Remembered deadline is past the native catch-up window')
                            continue
                        if fresh.get('state') in {'completed', 'failed'}:
                            # A stale fire emitted nothing. Native rearm refuses
                            # any still-live execution/dispatch claim.
                            if fresh.get('protagine_suppressed_claim_id') or self._script_failed(fresh):
                                changed = jobs.rearm_oneshot(job['id'], target)
                                if self._script_failed(fresh):
                                    changed = jobs.update_job(job['id'], {'protagine_rendered_claim_id': None})
                            else:
                                continue
                        elif instant(fresh['schedule']['run_at']) != instant(target):
                            if fresh.get('run_claim') or fresh.get('fire_claim'):
                                continue
                            changed = jobs.update_job(job['id'], {'schedule': target})
                        else:
                            continue
                        resolve_cron_scheduler().register_job(changed)
                except Exception as error:
                    logger.warning('Source reminder %s reconciliation: %s', job['id'], type(error).__name__)

    def render(self, binding_id):
        """Return current text to native cron. Does not send or confirm delivery."""
        from cron import jobs
        with jobs.use_cron_store(self.home):
            pair = next(((job, binding) for job, binding in self._bindings()
                         if binding['binding_id'] == binding_id), None)
            if pair is None:
                return ''
            job, binding = pair
            if job.get('state') == 'paused' or job.get('protagine_rendered_claim_id'):
                return ''
            current = self._current(binding)
            with jobs._jobs_lock():
                fresh = jobs.get_job(job['id'])
                if not fresh or fresh.get('state') == 'paused' or fresh.get('protagine_rendered_claim_id'):
                    return ''
                if current.get('status') != 'current':
                    jobs.pause_job(job['id'], reason='Remembered deadline evidence is unavailable or unresolved')
                    return ''
                target = instant(self._scheduled_at(current, binding))
                now = datetime.now(timezone.utc)
                if target > now or now-target > timedelta(seconds=120):
                    jobs.update_job(job['id'], {'protagine_suppressed_claim_id': current['claim_id']})
                    return ''
                text = f"Reminder: {current['subject']} {current['predicate']}: {current['value']}"
                if self.outbox is None:
                    raise ValueError('Native source ownership is unavailable')
                sibling('cron_memory').retain(self.outbox, home=self.home, owner=self.owner,
                    job=fresh, binding=binding, sources=current['source_refs'])
                jobs.update_job(job['id'], {'protagine_rendered_claim_id': current['claim_id']})
                return text


def main(binding_id):
    # The launcher uses the current Hermes interpreter/config. Import just the
    # packaged HTTP client, without registering the full plugin a second time.
    import importlib.util
    import sys
    from hermes_cli.config import load_config
    try:
        provider = importlib.import_module('protagine_memory.provider')
    except ModuleNotFoundError as error:
        if error.name not in {'protagine_memory', 'protagine_memory.provider'}:
            raise
        # Private directory installs and source checkouts have the same
        # provider beside this adapter, without a pip package on sys.path.
        adapter = Path(__file__).resolve().parent
        sibling = 'protagine-memory' if adapter.name == 'hermes-plugin' else 'protagine_memory'
        spec = importlib.util.spec_from_file_location('_protagine_reminder_provider',
            adapter.parent/sibling/'provider.py')
        provider = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = provider
        spec.loader.exec_module(provider)
    path = Path(__file__).with_name('client.py')
    spec = importlib.util.spec_from_file_location('_protagine_reminder_client', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    config = load_config() or {}
    home = provider._active_hermes_home()
    settings = dict(config.get('plugins', {}).get('protagine', {}))
    # Keep explicit general-plugin settings; native provider setup owns the
    # fallback over inline memory.config. Cron scrubs inherited credentials,
    # so resolve placeholders from this same selected profile's existing .env.
    for key, value in provider._profile_config(home).items():
        settings.setdefault(key, value)
    key = str(settings.get('api_key') or provider._profile_env('PROTAGINE_API_KEY', home) or '').strip()
    if key.startswith('${') and key.endswith('}'):
        key = provider._profile_env(key[2:-1], home)
    client = module.ProtagineClient(url=settings.get('url') or provider._profile_env('PROTAGINE_URL', home), api_key=key)
    contact = settings.get('contact_id') or provider._profile_env('PROTAGINE_MCP_CONTACT_ID', home)
    owner = str(settings.get('owner_contact_id') or (contact if contact != 'default' else '')
                or provider._profile_env('PROTAGINE_OWNER_CONTACT_ID', home) or '').strip()
    outbox_path = module.turn_outbox_path(settings)
    if not Path(outbox_path).is_file():
        raise ValueError('Native source ownership ledger is unavailable')
    outbox = module.TurnOutbox(outbox_path)
    output = NativeReminders(client, owner, home=home, outbox=outbox).render(binding_id)
    if output:
        print(output)


if __name__ == '__main__':
    import sys
    main(sys.argv[1])
