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
- **Lessons (M9)**: which lesson reaches a task body or an owner turn is lexical
  relevance (shared terms of the lesson's title and when-to-use with the work,
  plurals folded, a hand-set threshold), so a request worded differently from
  the lesson misses it; the campaign report's lesson diagnostics show the miss
  rate. A `result_field` check passes on the worker's own summary, so it
  verifies no lesson (the other faculties still read it as `check`). Lesson
  uses are joins over intention rows and use notes, so the tally covers the
  90-day audit retention and no more. An owner verdict in conversation counts
  only when the night's model quotes the owner's exact words; a verdict it
  does not recognise scores and teaches nothing.
- **Owner outreach (M11)**: the owner's replies are read by a lexicon
  (`P/mind/reactions.py`) with two appraisal nets (the owner's opt-out and a
  dismissal after a send); a reply worded outside it and not caught by the
  nets is linked only as engagement or not at all, so recall on held-out
  phrasings is unmeasured until the family's pilot, which reports it per class
  (a follow-up adds one tool-less classification call per linked reply if
  positive-reply recall is under 80%). A position link needs a short or
  referring reply, the owner's first turn after the outreach, not
  mid-conversation, with no request of its own, so a reply that names neither
  the topic nor the item and misses any of these links nothing and the
  outreach is scored by silence. Relevance is lexical (shared terms with the
  owner's interests and goals, and a sentence of the owner's naming the
  topic), and so are the null-report and repeat checks (a report worded as a
  finding that says nothing new in new words passes them). "Not now" teaches a per-hour mark, not a schedule:
  best-hour learning from reply latency is not built. A finding whose text
  carries a price trips the authority floor's money pattern and becomes an ask
  instead of a message; the family's items carry no currency. No family has
  measured the faculty yet: the flag carries its release-candidate value (on)
  so the family's `full` arm measures it; the release sets it by the gate
  (`docs/proto-agi/families/mind-outreach-1.md` section 5: off unless `full`
  beats `full-outreach`), and a deployment before the gate turns it on or off
  deliberately in its own config.
- **Skills (M9)**: off by default. Loads are counted only for Protagine's own
  `protagine-*` skills, and a process whose Hermes lacks
  `clear_skills_system_prompt_cache` lists a new skill only after a restart.
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
