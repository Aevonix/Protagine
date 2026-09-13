# Hermes runtime compatibility

PacoMind's optional [concurrent background tasks](NATIVE-TASK-CHANNELS.md) require
healthy overlapping plugin callbacks to retain each invocation's context and
result. The pinned public CI target is the explicit Hermes build below. The
installer does not patch an existing Hermes checkout or change its selection.

## Published qualification target

**SHIPPED source:** [Kurcide/hermes-agent at
`27d1366d64daa6069b74d1ba71f04212d60df7c5`](https://github.com/Kurcide/hermes-agent/commit/27d1366d64daa6069b74d1ba71f04212d60df7c5),
based on [Hermes v0.21.2,
`939e45c91d751fadd94dcd1b873ac3cb44846213`](https://github.com/NousResearch/hermes-agent/commit/939e45c91d751fadd94dcd1b873ac3cb44846213),
under the [MIT license](https://github.com/Kurcide/hermes-agent/blob/27d1366d64daa6069b74d1ba71f04212d60df7c5/LICENSE).
This is a published compatibility fork, not a claim that the change shipped in
an upstream Hermes release.

The callback change adapts [upstream PR #104763](https://github.com/NousResearch/hermes-agent/pull/104763),
specifically [source commit
`b9c112c83b60b91e918341cbb587a6a913d9d9eb`](https://github.com/NousResearch/hermes-agent/commit/b9c112c83b60b91e918341cbb587a6a913d9d9eb).
The callback correction preserves the original contributor's authorship and
retains two synchronized regression tests.

| Runtime file | Purpose |
| --- | --- |
| `hermes_cli/plugins.py` | Track the running callback and dispatch generation. |
| `hermes_cli/plugins_dispatch.py` | Serialize healthy overlap while retaining each caller's copied context; retain timeout suppression for a stuck callback. |
| `hermes_cli/plugins_ledger.py` | Invalidate waiting dispatches during unload without treating a still-running worker as finished. |

The unmodified base can skip a callback because another invocation is still
running. For concurrent PacoMind turns, that can omit source binding or lifecycle
observations. The correction operates within Hermes' existing callback
dispatcher. It adds no PacoMind service, model route, deployment setting or new
plugin registration API.

The selected build also retains named custom-provider timeout settings after
Hermes resolves a named endpoint to its generic transport. It uses the existing
`requested_provider` identity and timeout schema. Per-model settings take
precedence over the named provider, then the generic provider fallback. Cached
agents read changed timeout settings on subsequent requests. No initial output
cap is restored; native truncation recovery keeps its existing increasing
budgets. These are per-attempt transport settings, not an overall task deadline.

Hermes 0.21.2 deliberately omits ordinary persistence hooks for detached review
forks. The compatibility build adds one `on_detached_turn_end` observer in
`agent/turn_finalizer.py`, registered in `hermes_cli/plugins.py`. Its payload is
limited to exact execution/parent identifiers, completion/failure/interruption
flags, exit reason, model and platform. It carries no conversation or output
content, ignores callback returns, and preserves the native persistence skips.
The adapter binds review starts through existing synchronous request middleware.
This small interface replaces reliance on user-turn hooks for detached work;
it does not add a scheduler, review service or alternative memory store.
Unmodified upstream 0.21.2 does not provide this completion observer.

The build also limits the Kanban completion stop gate to the existing
dispatcher-owned worker context. A delegated child inherits the task
environment but owns no Kanban card and cannot call the parent completion
tools. It can now return its findings to the parent. The actual claimed
worker still must complete or block its own card before ending. The change
reuses Hermes' existing delegation ContextVar; it adds no setting or hook.

The build adds an optional `memory.refresh_on_turn` setting, default false.
When enabled, Hermes checks its curated MEMORY.md and USER.md snapshots at each
user turn. Changed rendered memory triggers the existing prompt rebuild.
Unchanged resident turns retain the cached prompt and do not rerun plugin
renderers. A reconstructed conversation refreshes once, since its saved prompt
can contain an older snapshot than the current files. Tool iterations within
the same turn retain their prepared prompt. This applies to native curated
files; PacoMind's existing per-turn source recollection remains separate.

The native rebuild also refreshes its other prompt sections. Changed bytes can
cost a prefix-cache miss. The installer does not enable this setting or alter
an existing deployment. A deployment that needs immediate curated corrections
can enable it in the selected Hermes profile after selecting this build.

The build also adds selective native payload redaction with exact preimages,
native writer leases, FTS updates, replay markers and gateway cache eviction.
The adapter verifies ownership and selects payloads in one read transaction.
The writer checks that session's message watermark in its mutation transaction,
so a late appended answer requires a fresh selection before cleanup can finish.
`on_native_turn_settled` runs after native persistence and lease release;
`on_gateway_turn_settled` runs after the outer gateway lease release in the
owning profile. These hooks let an adapter finish a forget requested during a
turn. They do not discover source ownership or create an erasure scheduler.
PacoMind supplies that lineage through its existing source and outbox machinery.
The adapter's [storage contract](NATIVE-REQUEST-ERASURE.md#native-owned-copy-reconciliation)
lists the supported copies and remaining limits.

Request middleware receives `native_user_message`, the exact persisted user row
at Hermes's validated current-turn index, and `original_user_message`, the
separate original admission. Compression can clone rows and persist an internal
task wrapper. The adapter uses the native descriptor for storage ownership and
keeps the original admission for canonical memory. Missing or changed native
rows produce no descriptor; the request middleware still runs. Post-tool
compression also updates the current-turn index through Hermes's existing
reanchor path, as other compression paths already do.
Live history repair also preserves separate durable user rows. After a crash,
merging those rows in place discarded the resumed input's storage coordinate.
Provider requests still use Hermes's existing merge of the API copy when needed.

Source-dependent cron output uses the native `cron.owned_output` interface.
Mirrored messages and channel/thread seeds retain the job and execution IDs.
The interface pauses the exact job and removes its retained files, queue payloads
and errors, then supplies exact message selections to the existing transcript
writer. A timed-out delivery can still have a live sender; cleanup remains
pending until it settles. PacoMind's [source reminders](SOURCE-REMINDERS.md) use
this interface with the existing source-ownership ledger. No second scheduler
or delivery service is added.

The detailed API health response now reads the attached runner's existing active
work count when available. Its stored lifecycle and platform diagnostics remain
unchanged. First-contact onboarding also respects the adapter's existing
`supports_async_delivery` capability: an adapter that cannot send asynchronously
does not ask the user to configure it as a home channel.

## What is qualified

The cron ownership change passed 33 focused native checks, including real mirror
and thread persistence, selective erasure, active delivery timeout and queue
retention. They establish local output ownership, not remote message retraction.

The durable-row repair passed 65 affected native checks, two unchanged private
crash-resume cases and three unchanged public transported-input cases.

The current-row interface and post-tool index correction passed ten focused
native checks. Three installed-adapter cases use the actual compression commit
with controlled summary output: session rotation, a transported task during
rotation, and in-place compaction. All preserve recalled context, delegated
results, canonical input references and the real pre-compression checkpoint.
Separate native-storage cases verify that old and new row ownership survives
compaction and that partial erasure distinguishes canonical input from its
native wrapper. These checks use controlled responses, not model-quality scores.

The two existing compatibility changes were reapplied to the 0.21.2 release
without conflicts. Their affected native suites passed 275 checks in a native-only
environment. The detached observer then passed 24 focused checks, including
successful, failed and interrupted endings, retained persistence-hook skips and
unchanged ordinary turn endings. These are separate run scopes, not a claim that
the entire upstream test suite ran.

The delegated-stop correction passed 52 focused checks using controlled
responses and the actual native Kanban store and completion tools. The
unmodified qualified base fails both delegated-return cases; the correction
passes them while retaining actual worker completion and blocking. These
checks establish the runtime stop boundary, not useful model completion.

The curated-memory change passed 153 affected native checks. Two parameterized
invariants exercise actual file writes, prompt restoration, plugin rendering,
the native turn prologue and outgoing message assembly. Corrections and deletions
reach the next turn; unchanged turns do not rebuild; history and task identity
remain intact. Four correction/restore cases fail on the previous build. These
checks use controlled requests without inference and do not establish correct
model interpretation of dates or useful recall in an ordinary conversation.

The following callback and timeout counts describe their earlier qualification;
the retained behavior is rechecked on the current native candidate above.

The two synchronized core invariants fail on the unmodified base and pass on
the selected build. They cover distinct sessions retaining their own callback
results and parallel tools in one session receiving separate policy decisions.
The existing plugin, ownership-ledger and event-bus checks also passed, for 132
affected core tests with file retries disabled.

The timeout correction passed 139 focused native tests, including the pinned
optional Anthropic SDK. Controlled SDK requests also verify distinct foreground
and background provider policies, cached-agent refresh, separate-process task
resume and native truncation recovery. These controlled responses establish
configuration propagation, not model quality or measured timeout expiration.

PacoMind's [actual native task fixture](../tests/hermes_adapter/test_native_task_channels.py)
uses the installed adapter, real gateway, canonical source/contact APIs and
controlled SDK responses. It holds two task roots while ordinary conversation
continues, steers and stops one from another owner channel, and verifies that
the other task completes with its source parents. Late stopped output is not
retained. The [source checks](../tests/hermes_adapter/test_task_sources.py)
also distinguish inherited child authority from a fresh owner instruction.

[Pinned CI](../.github/workflows/ci.yml) selects the exact fork commit and sets
the native runtime path explicitly. It runs the adapter suite once, then checks
the JUnit report to require the actual concurrent fixture to have run and
passed. Missing collection and a skipped fixture both fail qualification.

The separate [daily upstream check](../.github/workflows/hermes-upstream.yml)
continues to select the latest published stable **NousResearch** release and
run the adapter suite against it. It is a compatibility signal: an incompatible
release produces a failing run, without changing the pinned runtime or hiding
the failure behind the fork. A passing run is evidence for review, not an
automatic production upgrade.

These checks establish controlled runtime integration. They do not establish
model instruction following, ordinary-use memory quality, physical channel
delivery, deployment health or child-process cleanup.

## Remaining limits and removal

Healthy callbacks can accumulate waiting time; the change promises neither
FIFO ordering nor a bounded backlog. A callback recursively invoking itself
can still wait until timeout. Genuine hung callbacks remain suppressed, and
abandoned threads are not terminated. Plugin process globals do not become
session-scoped automatically.

Replace the fork with an unmodified upstream release when all of these hold:

1. Identify the released commit and review its callback admission, timeout and
   unload behavior. A PR or temporary merge commit alone is not a release.
2. Run the two core invariants and affected existing suites against that exact
   unmodified release, retaining the timeout and context checks.
3. Pass PacoMind's actual concurrent task fixture, including separate roots,
   ordinary conversation, source receipts, steering and targeted interruption.
4. Update the pinned CI commit and documentation, then select the runtime
   through the deployment's normal reversible upgrade path.

Also retain or verify the named-provider timeout behavior before removing that
part of the compatibility build. Replace the detached observer when upstream
provides an equivalent exact execution-end contract. Provider response hooks and
final output transforms alone do not establish failed or interrupted completion.

Also retain the delegated-return and actual-worker completion regressions.
Remove the stop-gate adjustment when an upstream release satisfies both
using its own dispatcher/delegation ownership boundary.

Remove the curated-memory extension when upstream provides equivalent opt-in
freshness for resident and restored sessions. Retain its correction, deletion
and unchanged-prompt checks; avoid restoring unconditional prompt rebuilding.

Keep the regression tests and upstream attribution after removing the fork
selection. Do not carry an old dispatcher diff over a newer implementation
without checking whether the upstream behavior already satisfies the contract.

Iteration-limit summaries use the existing `llm_request` middleware after provider
request construction, including every retry. They retain the active session, task
and turn identifiers; the runtime summary instruction is not recorded as a new
participant input. Chat, Anthropic and Responses modes keep their existing
provider controls. The existing `HERMES_DUMP_REQUESTS` diagnostic setting also
records the transformed summary request when enabled. This repairs the summary
path; it does not add middleware to unrelated auxiliary model calls.
