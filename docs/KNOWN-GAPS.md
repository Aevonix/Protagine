# Known gaps and retired scaffolding

This is a source inventory, not a production health report. Phase 1 establishes
the first supported Protagine baseline and is still being validated. Current
behavior and limits live in the README and capability guides. Enabling a
profile still requires checking its actual effects.

Older Protagine releases, aliases and migration paths carry no public support
promise. Retained modules are listed here so cleanup can account for their
actual callers and data, not to require compatibility with every old release.

Known current limits include complete erasure of unlinked historical data and
host transcripts, cross-database recovery, empirical retrieval quality beyond
the tested cases, and deployment-specific hardware coverage. Native skill
evaluation needs an independently supplied task oracle; it does not establish
general self-improvement simply by recording a successful review.

## Partially wired (works, with a missing half)

- **Mind-model briefing section**: `HealthSnapshot` (sleep/readiness) and
  predicted-load remain a protocol + stub with NO backing data source in the
  system. Deliberately not wired: fabricating health numbers would violate
  the measurement doctrine. Wire only when a real health/wearable source
  feeds the mind model.

- **Memory consolidation in the benchmark**: the memory and self families cross
  one night before the probe (amended 2026-09-24), so `full-consolidation` and
  `full-self_narrative` can differ from `full`; no pilot has measured it yet. The
  per-contact digest stage writes only into a contact store with the people
  milestone's digest columns (`set_digest`), so on a store without them the
  consolidation arm differs by the narrative, contradictions and episode
  summaries alone. The `semantic_recall` arm differs from `full` only in a plan
  given an embedding endpoint (`paired plan --embedding-config`).
- **Recall reference numbers**: `benchmarks/source_recall/reference-results.json`
  still holds the three-arm run made before the harness moved from the graph
  shim to `collect_sources`/`select_memory`; re-freezing it needs one measured
  run against real extraction, embedding and reranker endpoints.

## Retained code outside the baseline

- The incompatible manual `plugins/hermes-context/` compressor has been removed.
  Use the native context engine with the current general and memory-provider
  adapters.
- Goal records, saved DAG history, GET/PATCH APIs, context and condition updates
  remain readable and editable. The duplicate planner, decomposition, queue
  dispatch, conversation synthesis and goal-creation endpoint are removed.
  Hermes owns executable tasks. Reported goal completion does not verify an
  outcome or stop a worker; it preserves its first completion time and commits
  the record and transition together.
- The unused tier learner and quality-outcome API have been removed. Named
  function routing and configured fallback remain; explicit tier selection uses
  deterministic thresholds. Old `router_self_learning.db` files are neither read
  nor written and can be discarded.
- SQLite holds canonical source memory and typed situation observations. The
  Neo4j memory graph, the SQLite world model, the belief engine, the chain and
  the continuous learner were removed in M8; `protagine upgrade` moves their
  local state files into the upgrade backup. Changing databases does not fix
  memory admission quality or recover missing provenance.
- Graph consumers outside the deleted packages await the M10 audit. Those that
  only optionally read the graph lost that branch (the insight validator's
  data-age check, the relationship scorer's and cognition components' type
  hints); the research pipeline's graph stage (`GraphGatherer`, its source type
  and its web-versus-graph contradiction rule) is deleted. Those that cannot
  work without a Neo4j driver (`SignalCollector`, which M6 deleted with
  `mind_model`, and `ConnectionDiscoverer` with the synthesis and insight
  routes) are no longer constructed by the server; their routes report the
  subsystem as not wired. The cognition pipeline (the MetaLearner, the CPI and
  the strategy adjuster) was deleted in M9.
  The briefing `RelationshipAggregator` (Cypher only), `GraphBaselineStore`,
  the session `SessionContextLoader`, the node-certificate signer and the
  `extraction` extra had no other use and are deleted.

## Deliberate no-builds (division of responsibility with the host agent)

Protagine is the cognitive substrate; the host agent framework (e.g. Hermes)
owns sessions, tool execution, message transport, and cron. These stay
unbuilt HERE by design:

- **Email/desktop/browser job handlers**: outbound messaging goes through
  the host gateway (delivery bridge); Protagine never sends email itself. The
  desktop/browser packages were scaffolding for host-side capabilities and
  the dead EmailHandler was removed in v0.30.0. `JobType.DESKTOP`/`BROWSER`
  remain enum values with no handler.
- **ScheduleAdapter**: removed in v0.30.0. Its contracts were
  unimplementable (the MetaLearner, deleted in M9, had no pattern API; the
  AutonomyScheduler is interval-based, not a cron store) and mutating host
  cron jobs would cross into the host framework's domain.
- **Initiative execution requires Hermes**: registered evidence reviews and
  accepted local work use existing native task bindings. Follow-ups use the
  native preparation and delivery path. Unsupported work remains visible as
  proposals; proposal text is never an execution grant. The duplicate no-host
  executor, its startup flag and status endpoint are removed. See
  [executor retirement](EXECUTOR-RETIREMENT.md) for queue reconciliation and
  the historical skill-counter migration.

## Removed during the 1.0 consolidation

The following modules had no runtime imports, registered entry points or
configured loaders in this repository. They are retained in Git history rather
than offered as unfinished features:

- Raft consensus and its isolated unit suite, plus the unregistered chain key,
  sentinel and administrative CLIs. (The rest of the chain followed in M8.)
- The federation skill marketplace and its unused protocol, plus the unused
  skill schema-version helper. Hermes owns the supported native skill review
  path; executable work uses the supported native task bindings.
- The unused structured-world importer and email-header contact importer.
  Supported contact import paths remain.

The unused `gate/pending_dispatch.py` re-export, default cloud subtask handler,
inert operations scripts, webhook examples and old patch inventory runner are
removed. Explicitly registered custom workers remain supported; Hermes runtime
qualification uses the packaged patch set.

## Known mechanisms (documented so the log noise is interpretable)

- **"Unclosed client session" (aiohttp) after tick-budget cancellations**:
  when a tick exceeds `PROTAGINE_TICK_BUDGET_SECS` the whole-tick `wait_for`
  cancels whatever await is in flight; a cancellation landing inside an
  aiohttp request can interrupt the session unwind and the GC later logs the
  unclosed session. Mitigated (v0.29.0) by strongly referenced background
  tasks; the world-LLM extraction and the per-call graph driver that also
  produced it are gone (M8). Residual noise right after a budget-exceeded tick
  is expected and harmless.

## Settlement semantics (by design, documented here so nobody "fixes" it)

- Workspace concerns raised from **commitments** settle durably on resolve
  (the source closes). Concerns raised from **anomalies / benchmark
  regressions** have no settler: resolving them suppresses the dedup key for
  `PROTAGINE_WORKSPACE_RESOLVED_TTL_HOURS` (default 24h), after which a source
  that is STILL firing legitimately returns. That re-raise is intentional:
  a day-old still-live anomaly deserves attention again.
