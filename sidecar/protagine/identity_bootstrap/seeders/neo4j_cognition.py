"""Neo4j cognition seeder — writes bootstrap metrics and a BootstrapEvent node to Neo4j."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


class Neo4jCognitionSeeder:
    name = "neo4j_cognition"

    def __init__(
        self,
        protagine_graph: Optional[Any] = None,
        metrics_collector: Optional[Any] = None,
    ) -> None:
        self._graph = protagine_graph
        self._metrics = metrics_collector

    async def seed(self, corpus: Any) -> None:
        protagine_id = corpus.protagine_id
        now_iso = datetime.now(timezone.utc).isoformat()

        # Record bootstrap metrics
        if self._metrics is not None:
            try:
                await self._metrics.record(
                    metric_type="bootstrap_layer_count",
                    value=float(len(corpus.layers)),
                    domain="identity",
                    context={"protagine_id": protagine_id},
                )
                await self._metrics.record(
                    metric_type="bootstrap_endpoint_count",
                    value=float(len(corpus.api_endpoints)),
                    domain="identity",
                    context={"protagine_id": protagine_id},
                )
                await self._metrics.record(
                    metric_type="bootstrap_gate_layers",
                    value=float(len(corpus.gate_layers)),
                    domain="safety",
                    context={"protagine_id": protagine_id},
                )
                logger.info("neo4j_cognition: bootstrap metrics recorded")
            except Exception as exc:
                logger.warning("neo4j_cognition: metrics recording failed: %s", exc)

        # Write BootstrapEvent node to Neo4j
        if self._graph is not None:
            cypher = """
            MERGE (b:BootstrapEvent {protagine_id: $protagine_id})
            SET b.protagine_name      = $protagine_name,
                b.protagine_version   = $protagine_version,
                b.network_id       = $network_id,
                b.corpus_version   = $corpus_version,
                b.bootstrapped_at  = $bootstrapped_at,
                b.layer_count      = $layer_count,
                b.endpoint_count   = $endpoint_count
            RETURN b.protagine_id
            """
            params = {
                "protagine_id": protagine_id,
                "protagine_name": corpus.protagine_name,
                "protagine_version": corpus.protagine_version,
                "network_id": corpus.network_id,
                "corpus_version": corpus.corpus_version,
                "bootstrapped_at": now_iso,
                "layer_count": len(corpus.layers),
                "endpoint_count": len(corpus.api_endpoints),
            }
            try:
                await self._graph.run_query(cypher, params)
                logger.info("neo4j_cognition: BootstrapEvent node upserted for protagine_id=%s", protagine_id)
            except Exception as exc:
                logger.warning("neo4j_cognition: run_query failed: %s", exc)
