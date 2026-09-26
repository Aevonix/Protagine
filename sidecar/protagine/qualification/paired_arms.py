"""Comparator arms that run inside the episode container.

``base-heartbeat`` is stock Hermes plus one cron job created at episode start.
Its prompt follows Hermes' own heartbeat wording with the cron silence
convention, its context is its own last output, and every body tick makes it
due before Hermes cron ``tick()`` runs it, so it fires exactly once per tick.
``base-heartbeat-checkin`` is the same job worded to check in with the owner
when that is useful (the owner outreach family's product comparator); its
wording is frozen by its own hash, and ``base-heartbeat`` keeps the original.
``base-curator`` is stock Hermes with the curator on; one synchronous
``hermes curator run`` pass executes at every body tick.
"""
from contextlib import contextmanager
from functools import partial
import hashlib

from .paired_body import OWNER, PLUGIN

HEARTBEAT_JOB = 'paired-heartbeat'
HEARTBEAT_PROMPT = (
    '[Heartbeat: recurring instruction, fires on every body tick]\n'
    'Check your memory, sessions and board for anything that needs doing now. '
    'If something does, do it with your tools or tell the owner. '
    'If nothing does, reply exactly [SILENT].')
HEARTBEAT_PROMPT_SHA256 = hashlib.sha256(HEARTBEAT_PROMPT.encode()).hexdigest()
# The check-in wording (mind-outreach-1): generic, naming no scenario, the silence convention kept.
HEARTBEAT_CHECKIN_PROMPT = (
    '[Heartbeat: recurring instruction, fires on every body tick]\n'
    'Check your memory, sessions, board and workspace. If the owner would want to hear from you now, '
    'because something they care about has news, an open item of theirs could use a hand, or they seem '
    'to need help, message them once and say why. If not, or if they asked not to be disturbed, reply '
    'exactly [SILENT].')
HEARTBEAT_CHECKIN_PROMPT_SHA256 = hashlib.sha256(HEARTBEAT_CHECKIN_PROMPT.encode()).hexdigest()
HEARTBEAT_EXTRA_TOOLSETS = ['kanban', 'cronjob']
HEARTBEAT_DELIVER = f'{PLUGIN}:{OWNER}'
# Never due on its own within an episode: the arm's tick makes it due, so it fires once per tick.
HEARTBEAT_SCHEDULE = 'every 365 days'
CURATOR_CONFIG = {'enabled': True, 'consolidate': True}


def heartbeat_toolsets(worker_toolsets):
    """The worker set plus kanban and cronjob."""
    return [*worker_toolsets, *(name for name in HEARTBEAT_EXTRA_TOOLSETS if name not in worker_toolsets)]


def heartbeat_prompt(profile):
    """The heartbeat wording a profile installs: the check-in wording, the plain one, or None."""
    if profile.get('heartbeat_checkin'):
        return HEARTBEAT_CHECKIN_PROMPT
    return HEARTBEAT_PROMPT if profile.get('heartbeat') else None


def install_heartbeat(worker_toolsets, prompt=HEARTBEAT_PROMPT):
    """Create the heartbeat job once per episode; a restarted phase keeps the durable job."""
    from cron.jobs import create_job, list_jobs, update_job
    existing = next((job for job in list_jobs() if job.get('name') == HEARTBEAT_JOB), None)
    if existing is not None:
        return existing['id']
    job = create_job(prompt=prompt, schedule=HEARTBEAT_SCHEDULE, name=HEARTBEAT_JOB,
                     deliver=HEARTBEAT_DELIVER, enabled_toolsets=heartbeat_toolsets(worker_toolsets))
    # Each run sees its previous output, as Hermes' own heartbeat would.
    update_job(job['id'], {'context_from': [job['id']]})
    return job['id']


def make_due(job_id):
    """Fire the heartbeat on this tick: due now, so the coming cron tick() runs it."""
    from cron.jobs import update_job
    import hermes_time
    updated = update_job(job_id, {'next_run_at': hermes_time.now().isoformat()})
    if updated is None:
        raise RuntimeError('Heartbeat job disappeared')
    return {'job_id': job_id, 'due_at': updated['next_run_at']}


@contextmanager
def pinned_cron_agents(pin_runtime, *, max_iterations, max_tokens, stamp=None, system_message=None):
    """Cron-run agents (the heartbeat) get the frozen runtime and limits of the foreground agents.

    Hermes cron resolves its own runtime and turn limit from configuration and
    constructs its agent without an output cap; ``pin_runtime`` is the same
    function the worker applies to its resolved runtime. ``stamp``, when given,
    prefixes the run's prompt the way the worker prefixes an owner turn, so a
    cron run sees the same body clock (the scheduler puts no time in its prompt);
    ``system_message`` is handed to the run the way the worker hands a turn its
    environment note.
    """
    from unittest.mock import patch
    import cron.scheduler as scheduler
    original = scheduler._construct_cron_agent

    def construct(AIAgent, job, config, setup, **kwargs):
        setup.runtime = pin_runtime(setup.runtime)
        setup.max_iterations = max_iterations
        agent = original(partial(AIAgent, max_tokens=max_tokens), job, config, setup, **kwargs)
        if stamp is not None or system_message is not None:
            run_conversation = agent.run_conversation
            extra = {} if system_message is None else {'system_message': system_message}

            def framed(prompt, *args, **kw):
                return run_conversation(prompt if stamp is None else stamp(prompt), *args, **{**extra, **kw})
            agent.run_conversation = framed
        return agent

    with patch.object(scheduler, '_construct_cron_agent', construct):
        yield


def install_curator(config):
    """The curator arm turns Hermes' curator and its consolidation pass on in config."""
    config['curator'] = {**config.get('curator', {}), **CURATOR_CONFIG}


def curator_review():
    """One synchronous ``hermes curator run`` pass, reading the arm's curator config."""
    from agent.curator import run_curator_review
    result = run_curator_review(synchronous=True)
    return {'auto_transitions': result.get('auto_transitions'), 'summary': result.get('summary_so_far')}
