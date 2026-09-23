"""Comparator arms that run inside the episode container.

``base-heartbeat`` is stock Hermes plus one cron job created at episode start.
Its prompt follows Hermes' own heartbeat wording with the cron silence
convention, its context is its own last output, and every body tick makes it
due before Hermes cron ``tick()`` runs it, so it fires exactly once per tick.
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
HEARTBEAT_EXTRA_TOOLSETS = ['kanban', 'cronjob']
HEARTBEAT_DELIVER = f'{PLUGIN}:{OWNER}'
# Never due on its own within an episode: the arm's tick makes it due, so it fires once per tick.
HEARTBEAT_SCHEDULE = 'every 365 days'
CURATOR_CONFIG = {'enabled': True, 'consolidate': True}


def heartbeat_toolsets(worker_toolsets):
    """The worker set plus kanban and cronjob."""
    return [*worker_toolsets, *(name for name in HEARTBEAT_EXTRA_TOOLSETS if name not in worker_toolsets)]


def install_heartbeat(worker_toolsets):
    """Create the heartbeat job once per episode; a restarted phase keeps the durable job."""
    from cron.jobs import create_job, list_jobs, update_job
    existing = next((job for job in list_jobs() if job.get('name') == HEARTBEAT_JOB), None)
    if existing is not None:
        return existing['id']
    job = create_job(prompt=HEARTBEAT_PROMPT, schedule=HEARTBEAT_SCHEDULE, name=HEARTBEAT_JOB,
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
def pinned_cron_agents(pin_runtime, *, max_iterations, max_tokens):
    """Cron-run agents (the heartbeat) get the frozen runtime and limits of the foreground agents.

    Hermes cron resolves its own runtime and turn limit from configuration and
    constructs its agent without an output cap; ``pin_runtime`` is the same
    function the worker applies to its resolved runtime.
    """
    from unittest.mock import patch
    import cron.scheduler as scheduler
    original = scheduler._construct_cron_agent

    def construct(AIAgent, job, config, setup, **kwargs):
        setup.runtime = pin_runtime(setup.runtime)
        setup.max_iterations = max_iterations
        return original(partial(AIAgent, max_tokens=max_tokens), job, config, setup, **kwargs)

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
