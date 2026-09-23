"""The subsystem health skill reports a degraded subsystem; it never claims
to have fixed one, because it performs no restart."""

from __future__ import annotations

import pytest

from protagine.skills.base import ExecutionResult, InitiativeExecutionContext
from protagine.skills.executors.subsystem_health import SubsystemHealthSkill


class _Telemetry:
    async def get_recent(self, entity_id, minutes=30):
        return [{"metric": "embedding_latency", "value": 5000}]


class _Result:
    async def single(self):
        return None


class _Session:
    def __init__(self, log):
        self._log = log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def run(self, query, **params):
        self._log.append(query)
        return _Result()


class _Driver:
    def __init__(self, log):
        self._log = log

    def session(self, database=None):
        return _Session(self._log)


class _Graph:
    database = "neo4j"

    def __init__(self):
        self.queries = []
        self.driver = _Driver(self.queries)


def _context(entity_id: str) -> InitiativeExecutionContext:
    return InitiativeExecutionContext(
        initiative_id="init-1",
        category_id="subsystem_health",
        category_name="subsystem_health",
        entity_id=entity_id,
    )


async def test_degraded_subsystem_is_reported_not_claimed_fixed():
    graph = _Graph()
    skill = SubsystemHealthSkill(graph_client=graph, telemetry=_Telemetry())

    result = await skill.execute(_context("embed_pipeline"))

    assert result == ExecutionResult.ESCALATED
    assert result != ExecutionResult.AUTO_FIXED
    assert not any("last_fixed_at" in q or "status = 'active'" in q
                   for q in graph.queries)


async def test_skill_has_no_fake_restart_path():
    assert not hasattr(SubsystemHealthSkill, "_restart")
    assert not hasattr(SubsystemHealthSkill, "_record_fix")
    assert not any("restartable" in cfg
                   for cfg in SubsystemHealthSkill._SUBSYSTEMS.values())


async def test_healthy_and_unknown_subsystems_take_no_action():
    skill = SubsystemHealthSkill()
    assert await skill.execute(_context("embed_pipeline")) == ExecutionResult.NO_ACTION
    assert await skill.execute(_context("not_a_subsystem")) == ExecutionResult.NO_ACTION
