"""Lazy access to the wired sidecar subsystems for the native tool handlers.

Each property reads the host router's module-level wiring and returns None
when the subsystem is not wired, so a handler that depends on one checks for
None and answers "unavailable". This is what the tool handlers and the
self-model's load estimate read; it goes with ``P/tools`` when the second
executor is retired.
"""

from __future__ import annotations

from typing import Any


class SubsystemRegistry:
    """Provides lazy access to the wired subsystems the tool handlers use."""

    @property
    def graph(self) -> Any:
        from protagine.api.routers.host import _graph
        return _graph

    @property
    def goals(self) -> Any:
        from protagine.api.routers.host import _goals_store
        return _goals_store

    @property
    def world_model(self) -> Any:
        from protagine.api.routers.host import _world_store
        return _world_store

    @property
    def directives(self) -> Any:
        from protagine.api.routers.host import _directive_manager
        return _directive_manager

    @property
    def research(self) -> Any:
        from protagine.api.routers.host import _research_pipeline
        return _research_pipeline

    @property
    def repo_mirrors(self) -> Any:
        from protagine.api.routers.host import _repo_mirrors
        return _repo_mirrors

    @property
    def briefings(self) -> Any:
        from protagine.api.routers.host import _briefings_engine
        return _briefings_engine

    @property
    def connection_discoverer(self) -> Any:
        from protagine.api.routers.host import _connection_discoverer
        return _connection_discoverer

    synthesis = connection_discoverer

    @property
    def contacts(self) -> Any:
        from protagine.api.routers.host import _contacts_store
        return _contacts_store

    @property
    def self_model(self) -> Any:
        from protagine.api.routers.host import _self_model
        return _self_model

    @property
    def skill_store(self) -> Any:
        from protagine.api.routers.host import _skill_store
        return _skill_store

    @property
    def project_engine(self) -> Any:
        from protagine.api.routers.host import _project_engine
        return _project_engine

    @property
    def belief_engine(self) -> Any:
        from protagine.api.routers.host import _belief_engine
        return _belief_engine

    @property
    def sandbox(self) -> Any:
        from protagine.api.routers.host import _sandbox
        return _sandbox

    @property
    def initiative_store(self) -> Any:
        from protagine.api.routers.host import _initiative_store
        return _initiative_store

    @property
    def task_queue(self) -> Any:
        from protagine.api.routers.host import _task_queue
        return _task_queue

    @property
    def commitment_store(self) -> Any:
        from protagine.api.routers.host import _commitment_store
        return _commitment_store

    @property
    def mind(self) -> Any:
        from protagine.api.routers.mind import get_mind
        return get_mind()


__all__ = ["SubsystemRegistry"]
