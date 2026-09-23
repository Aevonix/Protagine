"""Subsystem health executor skill.

Monitors Protagine's own components (embed pipeline, delivery bridge, event
bus, etc.) and reports the ones that are degraded. It performs no repair of
its own: a degraded subsystem is escalated so the initiative reaches the owner
as a proposal instead of being counted as an executed fix.
"""

import logging
from typing import Any, Dict

from protagine.skills.base import (
    ExecutionResult,
    InitiativeExecutionContext,
    InitiativeExecutorSkill,
)

logger = logging.getLogger(__name__)


class SubsystemHealthSkill(InitiativeExecutorSkill):
    """Skill for diagnosing Protagine subsystem health."""

    skill_name = "subsystem_health"
    skill_version = "1.0.0"

    # Known subsystems and the telemetry metric that marks them degraded.
    _SUBSYSTEMS = {
        "embed_pipeline": {
            "check": "embedding_latency",
            "threshold_ms": 1000,
        },
        "delivery_bridge": {
            "check": "delivery_failures",
            "threshold": 5,
        },
        "event_bus": {
            "check": "event_queue_depth",
            "threshold": 1000,
        },
        "graph_client": {
            "check": "query_failures",
            "threshold": 10,
        },
    }

    async def can_execute(
        self, category: Dict[str, Any], context: Dict[str, Any]
    ) -> bool:
        """Can handle any category with executor_skill='subsystem_health'."""
        return category.get("executor_skill") == self.skill_name

    async def execute(
        self, initiative: InitiativeExecutionContext
    ) -> ExecutionResult:
        """Diagnose a subsystem and escalate it when degraded."""
        entity_id = initiative.entity_id or "unknown"
        self._log("info", "Diagnosing subsystem: %s", entity_id)

        # Get subsystem config
        config = self._SUBSYSTEMS.get(entity_id, {})
        if not config:
            self._log("warning", "Unknown subsystem: %s", entity_id)
            return ExecutionResult.NO_ACTION

        # Run diagnosis
        health = await self._diagnose(entity_id, config)
        self._log("info", "Subsystem %s health: %s", entity_id, health)

        if health.get("status") != "degraded":
            return ExecutionResult.NO_ACTION

        # No subsystem has an automatic repair; report it rather than claim one.
        self._log(
            "warning", "Subsystem %s is degraded; escalating: %s", entity_id, health,
        )
        return ExecutionResult.ESCALATED

    async def _diagnose(self, entity_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
        """Diagnose a subsystem's health."""
        health = {"status": "healthy", "entity_id": entity_id}

        # Query telemetry if available
        if self.telemetry:
            try:
                recent = await self.telemetry.get_recent(entity_id, minutes=30)
                if recent:
                    check_key = config.get("check", "latency")
                    values = [r.get("value", 0) for r in recent if r.get("metric") == check_key]
                    if values:
                        avg_value = sum(values) / len(values)
                        threshold = config.get("threshold_ms", config.get("threshold", 0))
                        health["avg_value"] = avg_value
                        health["threshold"] = threshold
                        if avg_value > threshold:
                            health["status"] = "degraded"
            except Exception as e:
                self._log("warning", "Telemetry query failed: %s", e)

        # Query graph for subsystem node
        if self.graph:
            try:
                async with self.graph.driver.session(database=self.graph.database) as session:
                    result = await session.run(
                        "MATCH (s:Subsystem {name: $name}) RETURN s.status as status, "
                        "s.latency_ms as latency, s.error_rate as error_rate",
                        name=entity_id,
                    )
                    record = await result.single()
                    if record:
                        health["graph_status"] = record.get("status")
                        latency = record.get("latency")
                        error_rate = record.get("error_rate")
                        if latency is not None:
                            health["latency_ms"] = float(latency)
                        if error_rate is not None:
                            health["error_rate"] = float(error_rate)
                        if record.get("status") != "active":
                            health["status"] = "degraded"
            except Exception as e:
                self._log("warning", "Graph diagnosis failed: %s", e)

        return health
