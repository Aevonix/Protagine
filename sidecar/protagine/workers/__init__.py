"""Schedulable agent-side workers, available as installed console commands.

- ``protagine-agent-bridge`` (:mod:`protagine.workers.agent_bridge`) runs
  initiative polling, job dispatch, skills sync and circuit health checks.
- ``protagine-queue-worker`` (:mod:`protagine.workers.queue_worker`) claims
  approved ``agent_action`` jobs and hands them to the agent.
- ``protagine-skills-sync`` (:mod:`protagine.workers.skills_sync`) reports
  the agent's installed skill index to Protagine.

These modules are stdlib-only so the workers can run without the full
sidecar dependency stack. Do not import heavy dependencies here.
"""
