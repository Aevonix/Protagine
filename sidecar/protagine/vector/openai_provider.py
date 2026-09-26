"""OpenAI-compatible API embedding provider.

Calls an OpenAI-compatible ``/v1/embeddings`` endpoint using the
host's API key.  This lets Protagine inherit embeddings from whichever
LLM provider the host is configured with — no separate API key needed.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from protagine.vector.config import EmbeddingConfig
from protagine.vector.embedder import EmbeddingProvider

logger = logging.getLogger(__name__)


class OpenAIAPIEmbeddingProvider(EmbeddingProvider):
    """Embedding provider that calls an OpenAI-compatible API endpoint.

    Uses ``httpx`` for async HTTP requests.  Inherits the API key and
    base URL from the host configuration (same key used for LLM calls).
    """

    def __init__(self, config: EmbeddingConfig) -> None:
        super().__init__(config)
        self._base_url: str = ""
        self._api_key: str = ""
        self._served_model: str = ""

    def configure(
        self,
        base_url: str,
        api_key: str,
    ) -> None:
        """Set the API endpoint and key (called by the host plugin)."""
        if self._served_model and (base_url.rstrip('/') != self._base_url or api_key != self._api_key):
            raise ValueError('Create a new embedding provider before changing an active endpoint')
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    @property
    def dimensions(self) -> int:
        return self._config.dimensions

    async def warmup(self) -> None:
        """Verify connectivity with a test embedding."""
        if not self._base_url:
            logger.warning("OpenAI API embedder not configured — set base_url")
            return
        result = await self.embed("warmup")
        logger.info(
            "OpenAI API embedder warmed up (dims=%d)", len(result),
        )

    async def embed(self, text: str) -> list[float]:
        """Embed a single text via the API."""
        results = await self.embed_batch([text])
        return results[0]

    async def close(self) -> None:
        """No resources to release for API provider."""
        pass

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts via the API."""
        if not texts:
            return []

        import httpx

        url = self._base_url + ('/embeddings' if self._base_url.endswith('/v1') else '/v1/embeddings')
        headers = {
            "Content-Type": "application/json",
        }
        if self._api_key:
            headers['Authorization'] = f'Bearer {self._api_key}'
        payload: dict[str, Any] = {
            "model": self._config.model_id,
            "input": texts,
        }
        # A validation width alone does not request provider-side reduction.
        if self._config.request_dimensions is not None:
            payload["dimensions"] = self._config.request_dimensions

        # Resilience (v0.21.1): the embedding endpoint is often remote (e.g. an
        # SSH-tunnelled vLLM). Transient blips were silently failing memory writes
        # AND recall (the agent then "loses" its memory). Retry with backoff.
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=30, trust_env=False, follow_redirects=False) as client:
                    response = await client.post(url, json=payload, headers=headers)
                    response.raise_for_status()
                    data = response.json()
                rows = data.get('data', [])
                if not isinstance(rows, list) or len(rows) != len(texts):
                    raise ValueError('Embedding response count does not match its inputs')
                if any('index' in row for row in rows):
                    if sorted(row.get('index', -1) for row in rows) != list(range(len(texts))):
                        raise ValueError('Embedding response indexes do not match its inputs')
                    rows = sorted(rows, key=lambda row: row['index'])
                vectors = [row['embedding'] for row in rows]
                if not any(isinstance(vector, list) and vector for vector in vectors):
                    raise ValueError('Embedding response contains an empty vector')
                if self._config.dimensions <= 0:
                    # No width was declared (router.embed_dims / PROTAGINE_EMBED_DIMS unset):
                    # the endpoint's first answer defines it, and every later vector is
                    # validated against that width exactly as against a declared one.
                    widths = {len(vector) for vector in vectors}
                    if len(widths) != 1:
                        raise ValueError('Embedding response vectors disagree on their dimension')
                    self._config.dimensions = widths.pop()
                    logger.info("Embedding dimension learned from the endpoint: %d", self._config.dimensions)
                for vector in vectors:
                    if len(vector) != self.dimensions:
                        raise ValueError(
                            f'Embedding response dimension {len(vector)} differs from the configured '
                            f'{self.dimensions} (router.embed_dims / PROTAGINE_EMBED_DIMS)')
                    if not all(math.isfinite(value) for value in vector) or not any(vector):
                        raise ValueError('Embedding response contains an invalid vector')
                served = str(data.get('model') or 'unknown')
                if self._served_model and self._served_model != served:
                    raise ValueError('Embedding endpoint changed its reported model; rebuild with a new provider snapshot')
                self._served_model = served
                return vectors
            except (httpx.HTTPError, OSError) as exc:
                last_exc = exc
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
        logger.warning("Embedding failed after 3 attempts: %s", last_exc)
        raise last_exc  # type: ignore[misc]
