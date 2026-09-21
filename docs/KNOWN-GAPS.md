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
- **Gate Layer 6 secondary review**: remains disabled by default. If explicitly
  enabled, a missing/failed client reports `unavailable`; malformed JSON or an
  unknown verdict reports `invalid`. Neither is a completed review. Valid
  `appropriate` and `flag_for_review` verdicts alone report `reviewed`. This
  repairs the optional contract without adding a reviewer model or consent step.
  The separate ResponseGuard shadow mode still does not establish enforcement.

## Retained code outside the baseline

- The incompatible manual `plugins/hermes-context/` compressor has been removed.
  Use the native context engine with the current general and memory-provider
  adapters.
- Desktop/browser task queue workers never shipped. Non-null `desktop_config`
  or `browser_config` now raises an explicit migration error; use the native
  runtime's tools. Persisted `desktop` and `browser` job types remain readable.
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
- SQLite is the supported typed world-observation store. The separate optional
  Neo4j memory graph is a different subsystem; its records are not a substitute
  for canonical source memory. Changing databases does not fix memory admission
  quality or recover missing provenance.
- **ResponseGuard applied-output receipts**: guarded candidates now carry an
  exact candidate digest, and the proactive send path honors enforce verdicts,
  but the audit store records evaluations rather than durable proof of the
  bytes a transport actually withheld or emitted. The general Hermes adapter
  uses `transform_llm_output`; loading that hook is still not proof of a
  transport's delivered output. Do not infer enforcement from verdict row
  counts. Qualify the actual transport and mode before claiming that behavior.

## Deliberate no-builds (division of responsibility with the host agent)

Protagine is the cognitive substrate; the host agent framework (e.g. Hermes)
owns sessions, tool execution, message transport, and cron. These stay
unbuilt HERE by design:

- **`cognition.requested` consumer**: the event carries a full spawn spec
  (system_prompt, model, tools_allow with real tool names), but spawning a
  restricted agent session is the host framework's job. A deployment that
  wants it should implement a thin host-plugin subscriber; the sidecar's
  working per-turn path is the inline introspection
  (`cognition/introspection.py`).
- **Email/desktop/browser job handlers**: outbound messaging goes through
  the host gateway (delivery bridge); Protagine never sends email itself. The
  desktop/browser packages were scaffolding for host-side capabilities and
  the dead EmailHandler was removed in v0.30.0. `JobType.DESKTOP`/`BROWSER`
  remain enum values with no handler.
- **ScheduleAdapter**: removed in v0.30.0. Its contracts were
  unimplementable (the real MetaLearner has no pattern API; the
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
  sentinel and administrative CLIs. Existing chain identity, storage and
  validation consumers remain intact.
- The federation skill marketplace and its unused protocol, plus the unused
  skill schema-version helper. Hermes owns the supported native skill review
  path; executable work uses the supported native task bindings.
- The unused structured-world importer and email-header contact importer.
  Existing connector/populator and supported contact import paths remain.

The unused `gate/pending_dispatch.py` re-export, default cloud subtask handler,
inert operations scripts, webhook examples and old patch inventory runner are
removed. Explicitly registered custom workers remain supported; Hermes runtime
qualification uses the packaged patch set.

## Known mechanisms (documented so the log noise is interpretable)

- **"Unclosed client session" (aiohttp) after tick-budget cancellations**:
  when a tick exceeds `PROTAGINE_TICK_BUDGET_SECS` the whole-tick `wait_for`
  cancels whatever await is in flight; a cancellation landing inside an
  aiohttp request can interrupt the session unwind and the GC later logs the
  unclosed session. Mitigated (v0.29.0): the world-LLM extraction timeout is
  capped under the budget, per-recall touch tasks are strongly referenced,
  and the research gatherer closes its per-call graph driver. Residual noise
  right after a budget-exceeded tick is expected and harmless.
- **ResponseGuard failure behavior is surface/mode specific**: exact
  text/artifact surfaces fail open while observing in `shadow` and fail closed
  on a configured-check outage in `enforce`; exact real-time speech surfaces
  are excluded. The static contract is documented in
  `docs/response-guard-surface-policy-v1.md`. The L6 review layer inside the
  separate gate pipeline fails closed.

## Settlement semantics (by design, documented here so nobody "fixes" it)

- Workspace concerns raised from **commitments** settle durably on resolve
  (the source closes). Concerns raised from **anomalies / benchmark
  regressions** have no settler: resolving them suppresses the dedup key for
  `PROTAGINE_WORKSPACE_RESOLVED_TTL_HOURS` (default 24h), after which a source
  that is STILL firing legitimately returns. That re-raise is intentional:
  a day-old still-live anomaly deserves attention again.
