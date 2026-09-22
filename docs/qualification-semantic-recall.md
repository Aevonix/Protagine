# Native semantic recollection qualification

This pack adds eleven retrieval scenarios: six public development cases and five held-out cases loaded from a private file. It uses the shipped source ledger, claim formation, `EmbeddingPipeline`, `IndexCatalog`, `SourceVectors`, LanceDB, combined memory selector and native Hermes memory provider. It adds no retrieval engine or persistent service.

Each attempt first forms sources through the configured extraction and review roles. An isolated native worker builds its own derived index, starts the ordinary authenticated host API, and asks the reader model a question in a fresh session. Source text is not included in that question. The installed provider owns automatic recollection and injection.

The evaluator distinguishes:

- Formation: eligible person-source jobs finished or deliberately rejected their claims. Session-scoped checkpoints retain their actual scope and do not become person claims.
- Retrieval: actual semantic candidates, canonical source collection, index readiness and declared fallback behavior.
- Selection: source IDs retained by the combined selector and actual reranker responses.
- Visibility: canonical source references and forbidden material in the serialized request sent to the reader model.
- Answer: exact requested JSON fields, separately from retrieval and source visibility.

A correct answer alone cannot pass. Missing collection, selection or provider-request evidence fails the relevant checks. The original memory pack's strict JSON answer contract remains unchanged. Format failures should be reported separately from missing evidence or wrong facts.

## Cases and boundaries

The development cases cover paraphrase retrieval, a lagging index with lexical fallback, unavailable embeddings with lexical fallback, contact scope before semantic search, stale vectors after canonical erasure, and ranking among distractors. The paraphrase case also requires that the target was absent from actual lexical results, so a lexical hit cannot earn semantic-retrieval credit. The held-out file covers additional scope, contradiction, index compatibility and derived-data failure scenarios. Held-out bodies and oracles are not part of this repository.

Fault injection runs only inside the owned subprocess. Endpoint failure returns a declared HTTP 503 to that process after successful indexing; it does not stop or reconfigure any external endpoint. The index-lag case intentionally leaves projection jobs pending. The erasure case deliberately retains stale derived rows while exercising canonical deletion fences. An incompatible identity changes only the fixture store's declared identity. Cached-text corruption changes only the disposable derived table; hydration must still read the canonical source.

All other retrieval calls use the production implementations unchanged. Instrumentation observes return values and serialized requests without adding source text or answers. The native worker receives only explicitly configured reader credentials; the separate retrieval recipe currently supports unauthenticated endpoints. Endpoint URLs and declared revisions belong in the private campaign recipe, not public fixtures or exports.

## Run through the existing qualification runner

```python
from protagine.qualification.native import configuration, native_context
from protagine.qualification.native_memory import MemoryRouter
from protagine.qualification.native_semantic_recall import (
    CONSUMERS, EVALUATORS, implementation_identity,
)
from protagine.qualification.semantic_recall_cases import cases, VERSION

selected, recipe = configuration(native_config, binding, hermes_python=python)
recipe['semantic_recall_implementation'] = implementation_identity()
suite = cases(retrieval_config)  # Or an explicit private pack path.
# Existing runner.evaluate(..., suite, CONSUMERS, EVALUATORS,
#     lambda _: MemoryRouter(fixed_supporting_router,
#                            native_context(selected, recipe)),
#     evidence_mode='actual_inference', suite_version=VERSION)
```

The worker interpreter needs the ordinary native plugin/runtime dependencies and the existing LanceDB extra, including pandas and PyArrow. Freeze the full interpreter package list, installed adapter bytes, supporting-role configuration and retrieval recipe alongside the candidate recipe. Source indexing is bounded to 32 existing projection jobs; case source counts retain the memory consumer's five-source bound.

The embedding recipe declares model, endpoint, dimensions and optional weight revision. The reranker recipe declares model, endpoint, prompt style, optional revision, timeout and cutoff. Warmup observes the embedding response model and validates dimensions. Declared revisions are not cryptographic verification of remote weights. The cutoff is supplied by the operator; the configuration fingerprint does not establish that a threshold is calibrated for a new corpus. Candidate comparisons must keep these supporting roles and settings fixed.

## Limits

Controlled HTTP responses verify integration and independent oracles, not embedding quality or reader intelligence. Actual candidate runs are separate immutable attempts. This pack does not exercise multimodal ingestion, live voice/text transports, a physical embedding-model replacement and reindex migration, large-corpus retrieval scaling, or full-text deletion from every possible external store. Synthetic endpoint failures measure fallback behavior, not production outage recovery or latency. The fresh-session path does not test long-history compression.

Public export should map this consumer to the native Protagine boundary, preserve strict and diagnostic results separately, and exclude endpoints, raw source text, prompts, credentials and held-out bodies.
