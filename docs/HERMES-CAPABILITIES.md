# Hermes capability contract

Protagine attachment requires native interfaces and behavior, not a Hermes version
allowlist. Stock Hermes **0.21.3**, release **v2026.9.14**, commit
[`345cd2b057a452236de401d3534b8502a7465e8d`](https://github.com/NousResearch/hermes-agent/commit/345cd2b057a452236de401d3534b8502a7465e8d)
does **not** meet the required core contract. The public transitional runtime at
[`0a9fa9747c3bb9721404c81e3f8c65e84b0389f0`](https://github.com/Kurcide/hermes-agent/commit/0a9fa9747c3bb9721404c81e3f8c65e84b0389f0)
has the same version string and passes the bounded capability checks. This is the
existing CI pin; the installer never downloads it or replaces an existing runtime.

## Inspect one interpreter

From an installed Protagine environment:

```bash
python -m protagine.hermes_capabilities \
  --python /path/to/hermes/venv/bin/python \
  --output hermes-capabilities.json --require core
```

The output path must be new. The check launches the selected interpreter with
isolated imports, a temporary home/profile, empty bundled plugins, no inherited
credentials, and blocked outbound socket connections. It uses synthetic SQLite
records; it does not inspect personal conversations, run a model, start a worker,
or activate an attachment. Failure names the missing interfaces and leaves the
existing runtime/profile in place. `--require` may be repeated for optional groups.

The receipt records the runtime version, available source revision, metadata hash,
individual check evidence, available feature groups, and `not_activated` probe
state. Setup retains it as `hermes_capabilities` in the private `instance.json`,
alongside the exact adapter resource digest and loading binding. The attachment
remains `configured_not_behaviorally_verified`; an interface pass is not a release
qualification or proof of live behavior.

## Required core and optional features

| Group | Required native checks | Stock 0.21.3 result |
| --- | --- | --- |
| Core | Plugin callback registration/caller context; provider discovery and pre-compression checkpoint v2; request/tool authority middleware | Available in bounded checks |
| Core | Exact persisted current-user row delivered to request middleware | Missing |
| Core | Selective payload erasure with preimage/watermark checks and idempotent replay; post-persistence native settlement observer | Missing |
| Core | Durable task creation, duplicate admission/claim rejection, reopen, stale-claim recovery | Available in bounded checks |
| Concurrent work | Overlapping callbacks preserve each caller; gateway-settled observer | Missing |
| Detached review | Observer-only detached completion | Missing |
| Source reminders | Exact-job output snapshot, fingerprint and erasure | Missing |
| Terminal handoff | Typed `FinishTurn` after a completed tool batch | Missing |

Core product qualification additionally requires **installed artifact loading,
profile/participant isolation, source capture, scoped recall, correction/forget
over every supported retained copy, authority preservation, and durable task
recovery**. Those end-to-end claims remain in `tests/hermes_adapter`; declaration
checks of hooks do not prove hook ordering, cache refresh, writer settlement, or
model-visible recall. They must pass before declaring stock core supported.

New setup and adapter refresh require core. Enabling or retaining native local
work, native task dispatch or operational review also requires concurrent work;
operational review requires detached completion. Missing optional interfaces do
not fail a core-only attachment. The adapter already omits source reminders when
`cron.owned_output` is unavailable, omits task tools when tasks are not enabled,
and omits the `handoff` operation without `FinishTurn`. Setup does not silently
disable requested features to make a runtime pass.

Preferences-only and skills-only operations retain their narrow scope. Repeating
setup for an unchanged attachment does not force a runtime replacement. Enabling
features and refreshing the adapter recheck the relevant contract before writes.

## Qualification evidence and gaps

The retained latest-stable run on 2026-09-21 produced **40 failed, 309 passed,
28 skipped**. It is still a failed qualification. No test was removed, skipped or
marked expected-failure by this capability work. The full suite remains blocking
in `hermes-upstream.yml`, with a separate mandatory core gate and JSON/JUnit
artifacts. The workflow now prints skip reasons as well as preserving every case.

[The failure inventory](hermes-upstream-failures.json) records all 40 exact case
IDs, observed symptoms and disposition. Its classifications distinguish confirmed
missing interfaces from fixture assumptions; fixing a probe alone does not resolve
every failure in a family.

| Cases | Finding and required follow-up |
| --- | --- |
| 3 overlapping turns + 1 delegated current-work case | Existing witnesses inspect a fork-private condition; three time out and one raises `AttributeError`. The separate public callback probe reproduces dropped overlap on stock. Preserve the behavioral witnesses; remove private fixture coupling when porting the callback change. |
| 3 named-provider timeout cases | Actual native resolver rejects `requested_provider`; port the resolver correction and retain explicit/default timeout witnesses. |
| 5 post-turn review cases | Expected terminal review outcomes are absent; detached observer is separately missing. Keep these visible as optional review qualification, including guest isolation. |
| 2 source-reminder cases | `cron.owned_output` is absent; the tool remains unadvertised until exact-job erasure works. |
| 2 task submit + 5 task handoff cases | Submit fails supplied-input/session identity; handoff times out without the terminal interface. Task state/recovery stays core; terminal handoff remains optional. |
| 2 source-update cases | Erasure remains pending and a steering carrier survives. This is a core blocker. |
| 14 source search/read/annotation/history/media cases | Exact source or annotation identity is withheld; the fixture then lacks a confirmation. Core source identity and retained-copy erasure remain required for supported source types. |
| 3 supplied-input rotate/compact cases | Missing supplied source or unexpected result shape breaks the controlled transport; retries amplify the failure. Preserve the full compaction/rotation witnesses. |

The historical log did not retain names/reasons for its **28 skipped cases**.
They remain **unresolved evidence**, not passes or asserted optional omissions.
The next stock qualification must inventory those cases from JUnit before any
stock-support claim. These are software-interface witnesses with controlled
responses, not model-quality scores.

## Upstream contributions and fork retirement

[The exact 32-commit inventory](hermes-runtime-inventory.json) includes each
commit's purpose, changed interface files, native regression tests, current
equivalence evidence and removal condition against the official base. It also
separates merge/test maintenance and unrelated channel fixes from core blockers.
For fixes not directly exercised by this audit, equivalence is explicitly
unqualified; absence from the fork commit history alone is not proof of an
upstream behavioral defect.

Prepare small generic runtime contributions in this order:

1. **Persisted input provenance.** Port `738593f869e`, `3454fd7b5e5`,
   `218dad99356`, and `a012d62efab`: exact current persisted row plus compaction,
   durable-row repair and image-anchor preservation. Keep original input separate
   from the current row and fail closed on a mismatch. Add actual request
   middleware-path tests; a direct helper test alone is insufficient. Require
   rotate/compact/delegated supplied-source witnesses and a red-on-base test.
2. **Owned transcript erasure and settlement.** Port the coherent surface from
   `66372a6bf4b`, `e2776186612`, `75f6ea6e508`, `84c7d76a247`: selected row
   snapshots, preimage/watermark rejection, payload/FTS removal, writer leases,
   native/gateway settled observers, profile ownership and cache refresh. Keep
   unrelated rows and metadata. Include restart/replay, concurrent-writer and
   changed-selection tests. A SQLite erasure helper alone does not satisfy core.
3. **Callback overlap.** Reuse the existing open upstream
   [PR #104763](https://github.com/NousResearch/hermes-agent/pull/104763), head
   `b9c112c83b60b91e918341cbb587a6a913d9d9eb` (checked 2026-09-21), preserving
   authorship rather than duplicating the PR. The local adaptation is
   `2275129fd5f`. Require context isolation, recursion/timeout behavior and the
   installed overlapping-turn witnesses.
4. **Provider policy and optional surfaces.** Keep `7102874d823` (named-provider
   timeout preservation) separate. Offer detached completion (`09fbad8e4e4`),
   exact-job cron output provenance (`792f9e89e04`) and typed post-tool handoff
   (`13dc6c542bb`) as independent interfaces, each with its native regression
   tests and opt-in behavior. No Protagine policy, storage or sidecar scheduler
   belongs in these upstream patches.

Retire the fork only when an exact public upstream revision passes the required
core installed-artifact suite **and all Phase 1 features enabled in the intended
profile**, including task outcomes/recovery. Resolve all skips and unexplained
failures, record the upstream equivalents/removal decisions for all 32 commits,
then change the qualification pin and documentation together. Preserve the
transitional runtime until that evidence exists; there is no deployment action in
this contract.
