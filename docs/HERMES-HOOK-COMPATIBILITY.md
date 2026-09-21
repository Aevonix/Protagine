# Hermes runtime compatibility

Protagine **1.9.0rc1** prepares official Hermes source with a versioned interface
patchset packaged in its wheel. The installer stages a separate runtime and
validates it before attachment. A maintained fork and upstream acceptance are
not prerequisites. This is release-candidate functionality; the 1.8 packages do
not contain the new preparation commands.

## Qualified source and patchset

| Item | Selection |
| --- | --- |
| Official source | [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) |
| Base | 0.21.3, tag `v2026.9.14`, commit `345cd2b057a452236de401d3534b8502a7465e8d` |
| Patchset | `hermes-0.21.3-protagine-1` |
| License | [MIT](../sidecar/protagine/hermes_patchsets/hermes-0.21.3-protagine-1/LICENSE) |
| Runtime delta | 39 files; separate 28-file native regression patch |

The [manifest](../sidecar/protagine/hermes_patchsets/hermes-0.21.3-protagine-1/manifest.json)
contains the official revision, patch digests and every changed file's exact
before/after hash. [Provenance](../sidecar/protagine/hermes_patchsets/hermes-0.21.3-protagine-1/provenance.json)
preserves original contribution authors and commits. That history is attribution
and reproduction evidence, not a repository the installer needs to fetch.

This first bundle retains the qualified generic interfaces and correctness
repairs used by Protagine. It is not presented as the smallest possible core-only
delta. Unrelated WhatsApp recovery, website documentation and agent-specific
notice routing are excluded. No deployment settings or private data are included.

## Interfaces provided

| Area | Behavior |
| --- | --- |
| Current-input identity | Request middleware receives the exact persisted current-user row and the separate original admission. Compaction preserves surviving row identity; ambiguous text matches do not invent a source. Durable user rows retain their storage coordinates during history repair. |
| Owned payload erasure | Selected rows use exact preimages and a message watermark. Native writer leases, FTS updates, replay markers and gateway cache refresh keep cleanup consistent while preserving unrelated rows. |
| Turn settlement | Native and gateway observers run after their owning persistence and leases settle. They let the existing source/outbox machinery finish pending cleanup and record bounded execution outcomes. |
| Concurrent callbacks | Overlapping calls retain their own context and results instead of dropping the second call. Unload and timeout checks remain in Hermes's dispatcher. |
| Detached review | `on_detached_turn_end` reports execution identity and completion/failure/interruption metadata without ingesting the detached conversation as a new user turn. |
| Task completion | Typed failures and explicit continuation remain attributable. Delegated children can return to their parent; the actual Kanban worker still completes or blocks its own card. A status is not independent proof of success. |
| Source reminders | `cron.owned_output` binds retained output to an exact job and execution. Cleanup waits for active senders, removes owned copies and preserves unrelated jobs. |
| Terminal handoff | After a single successful tool result is persisted, a trusted plugin can return typed `FinishTurn` with its call ID and receipt. Hermes persists and delivers the response without another model call. Mixed batches, errors and pending steering keep their existing paths. |
| Provider policy | Named custom-provider timeout settings survive transport resolution, including model overrides. These are transport timeouts, not overall task deadlines. |
| Summary requests | Iteration-limit summaries and their retries pass through the existing request middleware with the active scope. Runtime summary instructions do not become participant evidence. |
| Runtime correctness | Length-continuation fragments stop at accepted tool boundaries; nonzero kernel exits report failure; detailed health reads the runner's active work; home-channel onboarding respects async-delivery capability. |

These changes do not add a scheduler, memory store or Protagine policy engine to
Hermes. Native erasure does not retract remote messages or erase backups. The
[owned-copy contract](NATIVE-REQUEST-ERASURE.md#native-owned-copy-reconciliation)
describes the supported retained copies and remaining limits.

Healthy callback overlap may still queue; the change does not promise FIFO
ordering or a bounded backlog. Hung callbacks remain subject to the existing
timeout rules. Plugin globals do not become session-local automatically.

## Optional configuration

`tools.tool_search.eager` keeps exact, already-admitted tool schemas visible.
It neither enables missing tools nor bypasses middleware. Protagine setup adds
`protagine_task` while preserving existing choices. Other tools retain discovery.
A deployment can select additional frequent tools, measuring request size and
complete-task latency before expanding the list:

```yaml
tools:
  tool_search:
    eager: [protagine_memory_read_source, protagine_task]
```

`agent.image_input_mode: native_if_supported` sends images directly when the
selected provider/model supports vision. Otherwise it retains the configured
auxiliary route. Provider identity remains part of capability selection.
Installation does not select this mode or change existing defaults.

`memory.refresh_on_turn: true` refreshes native MEMORY.md and USER.md snapshots
when they change, and once after session reconstruction. Unchanged turns retain
the prompt cache; tool iterations retain their prepared prompt. Changed prompt
bytes can cost a prefix-cache miss. The default is false. Protagine's per-turn
source recollection is separate from this native curated-memory setting.

## Installation and updates

```bash
protagine init --prepare-hermes
```

For separate preparation, use `protagine hermes prepare`; supply `--source` for a
clean official checkout or omit it to fetch the qualified official revision.
`--destination` selects a new candidate directory. Use `protagine hermes check`
to inspect its patches and native capabilities. [Local setup](LOCAL-HERMES-SETUP.md)
explains attachment, existing services and the selected-runtime launcher.

Preparation checks all preimages before publishing the candidate, verifies all
postimages and excludes untracked source files. Modified or unsupported source
is rejected without changing the original installation. A candidate is not a
live deployment until its interpreter is selected through the deployment's
normal lifecycle. Protagine does not restart existing gateways during setup.

[Release CI](../.github/workflows/ci.yml) uses the official base and patches from
the built wheel, then runs the complete adapter suite and the native patch
regressions. Native files run in separate processes as upstream's test contract
requires. The initial bundle passed all five capability groups, 358 native tests
plus 10 subtests and 18 installed-adapter memory, reminder, task and review checks.
The complete release run remains required; these counts are not model benchmarks.

The [daily latest-stable check](../.github/workflows/hermes-upstream.yml) can test
an unlisted revision when its exact patch preimages still match. It reports
conflicting files or runs the native and installed-adapter suites. Installers
still require a newly qualified bundle. The job never selects an older revision
silently, applies a patch approximately or updates production.
Keep the active runtime until the new official source plus its selected patches
passes qualification and the deployment's observable checks.

Upstream equivalents let us remove individual patches while retaining their
regression tests and attribution. Acceptance of our submitted changes is not a
Phase 1 gate. The [capability contract](HERMES-CAPABILITIES.md) defines the required
behavior regardless of whether an interface comes from upstream or this bundle.
