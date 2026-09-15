"""Observation store (v0.16.0) — the agent is Protagine's sensor array.

Protagine does not own external API clients. The agent observes the world
through its existing Hermes connections (github, terminal, web, ...)
and reports domain snapshots here. Protagine's context loaders read
observations, never external APIs.
"""

from protagine.observations.store import (
    OBSERVATION_DOMAINS,
    Observation,
    ObservationStore,
)

__all__ = ["Observation", "ObservationStore", "OBSERVATION_DOMAINS"]
