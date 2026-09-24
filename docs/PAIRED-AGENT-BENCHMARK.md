# Paired Hermes and Protagine benchmark

`protagine models paired` runs the same task episodes through fresh Hermes and fresh Hermes with Protagine enabled. It records what the base agent completes, what the combined system completes, and the difference. The current development baseline, `paired-agent-reviewed-2`, has 60 episodes, ten in each of six families. The original 18-episode pilot and previous reviewed dataset remain available under their original versions. These datasets do not establish a comprehensive model ranking.

Reviewed-2 changes only two ambiguous output instructions from reviewed-1: case
008 explicitly requests the top-level `people` property; case 026 explicitly
names `tasks` and each task's `id`, `start` and `finish` properties. Source data,
expected answers, graders and budgets are unchanged. Earlier fixture bytes and
results remain unchanged. Publish fresh results under the new version rather
than replacing scores or combining versions into a ranking.

The [frozen workflow pack](FROZEN-WORKFLOWS.md) adds twelve eight-turn workflows,
each with two process restarts and two graded intermediate checkpoints. Select
`--dataset-version paired-agent-workflows-1 --repetitions 3` at plan time for a
complete release comparison: 36 pairs and 72 isolated arm executions. This is a
separate public evaluation dataset, not a private holdout or an extension of the
60-case score. Repeated attempts alternate which arm runs first and never share
state. Existing plans default to one repetition.

Both arms use the same configured model, provider settings, immutable container image, initial files, source events, task instructions, output limits and artifact verifier. Hermes keeps its ordinary memory and session tools in the baseline. Protagine adds its normal integration in the other arm. Historical facts arrive through the same episode turns; answers are not preloaded only for Protagine. The verifier's oracle stays outside the agent container.

Each arm starts a new container with fresh state for each episode. State persists across turns and sessions inside that episode. No owner home, memories, live-agent state or Docker socket is mounted inside the agent. The container can call the selected model endpoint. Sharing an endpoint with a live agent is allowed, but can slow that agent and distort benchmark timings. Prefer running while the agent is not using that endpoint when practical. The benchmark does not stop the live agent or require a reserved endpoint.

The Protagine fixture starts empty facts, affect and commitment stores through its own API lifespan, using the same store classes as the normal server. Facts and affect share the fixture source ledger. No scenario answers are preloaded. These stores are created and closed on the API thread, as their SQLite connections require. Authorization checks alone do not establish backend readiness: image validation must exercise the real provider handlers, including retain/search/read/forget, before a cohort runs. Optional work-context lookups remain outside this memory fixture.

## Declare the comparison

Use one private Hermes provider configuration and one policy file for both arms. The image must already exist and be addressed by its immutable digest. Plan inspects the image in a disposable container with networking disabled; it does not call a model or download an image.

```json
{
  "version": "paired-policy-1",
  "budget_mode": "deployment_policy",
  "budget_policy": {
    "description": "Same declared episode and per-call limits; ordinary auxiliary processing remains enabled.",
    "total_work_enforcement": "not_verified"
  },
  "environment": {
    "endpoint_usage": "shared",
    "hardware_recipe": "Private serving recipe identifier",
    "supporting_models": "All generative roles use the selected candidate; text-only pilot."
  }
}
```

`endpoint_usage` accepts `idle_declared`, `shared` or `unknown`. This is an operator declaration, not measured endpoint isolation. Record hardware, serving recipe and competing traffic honestly. Policy contents, dataset version and content hash, installed implementation, container identity and selected model configuration are frozen into the run identity. Changing them requires a new plan.

`budget_mode` can be `deployment_policy` or `matched_work`. The latter is a declaration, not proof of equal cost: ordinary memory reviews, extraction, judgment, background work and retries must all be observed and bounded before claiming matched total work. Per-call token limits alone do not establish it. Resource totals remain unknown where coverage is incomplete.

## Plan, run and report

```sh
protagine models paired plan \
  --native-config /private/candidate-hermes.yaml --native-binding candidate \
  --comparison-policy /private/paired-policy.json \
  --container-image registry.example/agent-benchmark@sha256:IMAGE_DIGEST \
  --dataset-version paired-agent-reviewed-2 \
  --label candidate-pilot-01 --output /private/results/candidate-pilot-01

protagine models paired run \
  --native-config /private/candidate-hermes.yaml \
  --comparison-policy /private/paired-policy.json \
  --container-image registry.example/agent-benchmark@sha256:IMAGE_DIGEST \
  --output /private/results/candidate-pilot-01

protagine models paired report --output /private/results/candidate-pilot-01
```

Replace the example image digest with the actual pinned image. `--docker-host` selects an explicitly configured local or forwarded Docker daemon. There is no local-process fallback. Select a bounded subset with `--case-ids` at plan time; it always selects both arms together. Without `--case-ids`, every episode in the selected dataset runs. Without `--dataset-version`, the original `paired-agent-pilot-1` is selected. A run uses the version frozen in its plan.

Fixture JSON, task instructions and artifact checks are versioned in the repository. A changed task or grading contract gets a new dataset version and content hash; it does not replace an earlier result. The reviewed dataset includes stricter output contracts and checks of both successful and incorrect artifacts. It is still public development data, so improvements measured here need separate held-out validation.

The supplied model URL must be reachable from the container network. A loopback URL names the container itself, not the machine running the CLI or a remote Docker daemon. Use the endpoint's reachable address; the benchmark does not change model listeners or production routing. [Image build instructions](../benchmarks/paired/README.md) describe the pinned source exports and dependencies.

Execution is sequential. With the default two arms the first episode runs base Hermes first, the second runs Protagine first, and the order continues alternating. With N arms the first arm rotates by `(episode index + repetition) mod N`, so every arm leads equally often. An episode has exactly one attempt per arm. There is no best-of selection or adaptive retry. `--resume` continues only untouched attempts. Interrupted attempts retain their outcome and are never replayed. Unconfirmed container cleanup stops further execution.

### Arms and profiles

An arm is a named profile: whether the Protagine plugin is installed, an
optional overlay of `PROTAGINE_*` flags applied after the fixture's own forced
flags, before the plugin loads, and the binary switches that are on
(`heartbeat`, `curator`, and the mind switches `initiative`, `full`, the
`minus_*` ablations and the additions `plus_skills` and `plus_affect_rules`; a switch is listed
only when it is on). The built-in
profiles are `base_hermes` (plugin off) and `protagine` (plugin on, the mind
off), the default arm set, the two comparators `base-heartbeat` and
`base-curator` described below, `protagine-initiative`, the treatment arm of
`mind-initiative-1` (the plugin with the mind on at `autonomy: standard`, only
the `initiative` faculty, quiet hours and the daily digest off), and the
drives-family arms: `full` (every `mind.faculties` flag and drive weight at its
release-candidate value from the shipped defaults), `full-drives` (the flat
priority ablation: `faculties.drives` off, so every weight is 1, nothing
satiates and no goal is adopted), `full-broadcast` (`faculties.broadcast` off)
and one diagnostic per drive, `full-duty`, `full-curiosity`, `full-mastery`,
`full-upkeep` and `full-social` (that drive's weight set to 0), and one
ablation per later faculty, `full` with that faculty's `mind.faculties` flag
off: `full-people` (the people family, `mind-people-1`), `full-affect` (the
feelings family, `mind-affect-1`), `full-opinions` (the opinions family,
`mind-opinions-1`), `full-semantic_recall` and `full-consolidation` (the
memory family, `mind-memory-1`), `full-self_narrative` (the identity family,
`mind-self-1`) and `full-lessons` (the self-improvement family,
`mind-improve-1`), plus two arms that turn on a faculty that ships off:
`full-plus-skills` (`faculties.skills` on) and `full-affect-plus-rules`, the
feelings family's mechanism arm (`full-affect` with `faculties.affect_rules`
on, so every affect consumer reads its frozen stateless rule instead of the
decaying state). The flag is served whether or not
the faculty's code has landed, so such an arm is a no-op contrast until its
milestone. Every mind arm is served in the worker next to the host routes and
ticked by the body tick.
`--arms` selects two to eight profiles by name; repeating a name runs the
same profile twice (an A/A run, labelled `base_hermes` and `base_hermes.2`),
which measures the noise floor. `--reference-arm` names the comparator; it
defaults to the first arm and every other arm is contrasted against it.
`--profiles` adds profiles from a JSON object:

```json
{"full-x": {"plugin": true, "overlay": {"PROTAGINE_SOME_FACULTY": "off"}},
 "full-heartbeat": {"plugin": true, "heartbeat": true},
 "base-plain": {"plugin": false}}
```

A profile cannot redefine a built-in one, and an overlay cannot name a model,
endpoint, credential, contact, path or database setting: those are shared by
every arm, never arm differences. The same body runs in every arm, so the image,
model, budgets, tools and oracle are identical; only the profile differs. Arm
profiles beyond the built-in pair require an image whose worker declares the
current arm-profile protocol (`arm_profiles`, `paired-arm-profiles-5` since the
affect mechanism arm); older images run only the default pair.

### Comparator arms

`base-heartbeat` is stock Hermes plus one cron job, created at episode start
(`paired_arms.install_heartbeat`). Its prompt follows Hermes' own heartbeat
wording with the cron silence convention:

> Check your memory, sessions and board for anything that needs doing now. If
> something does, do it with your tools or tell the owner. If nothing does,
> reply exactly [SILENT].

`[SILENT]` suppresses delivery; anything else is delivered to `capture:owner`
and lands in the outbox. The job's `context_from` is its own id, so every run
sees its previous output. Its toolsets are the worker set plus `kanban` and
`cronjob`, so it can create board tasks, which the same tick dispatches. The
job's schedule never makes it due on its own: the arm's step of every body tick
sets `next_run_at` to now, so Hermes cron `tick()` runs it exactly once per
tick and the heartbeat gets at least as many model calls as the mind's tick.
The prompt's SHA-256 is frozen in the plan (`comparison.heartbeat`) and the
image must carry the same prompt. A restarted phase keeps the durable job.

`base-curator` is stock Hermes with `curator.enabled` and `curator.consolidate`
on; the arm's step of every body tick runs one synchronous `hermes curator run`
pass (`agent.curator.run_curator_review`). Campaign mode, where the pass runs
between episodes, comes later.

In the plugin arms the arm's step is the plugin's `tick()`: `POST
/v1/mind/tick` (the mind forms intentions from the episode's shifted clock)
and then the body pass (dispatch, outbox, reconciliation, observations),
before Hermes cron and kanban dispatch; where the mind is off or absent only
the body pass runs. The adapter's own body thread stays parked
(`PROTAGINE_BODY_THREAD=0`) so nothing lands between two observed ticks. Each
tick row records the step's result under `arm_tick`. Base arms have no step,
at the same position. An image whose worker serves the mind advertises
`mind_tick`; the plan refuses the initiative arm on an older image.

`--temperature` pins the sampling temperature on every model call in every arm,
foreground and auxiliary, through the same request body the candidate
compatibility settings use. Unset, the provider default applies and the plan
records `null`. `--seeds` freezes the scenario seeds a seeded family draws its
instances from. Arms, profiles, the reference arm, temperature, seeds and the
decision rule are all part of the comparison key and the plan hash: changing
any of them is a new plan.

Diagnostic images record bounded private model requests/responses, native turn messages and completion flags, plus context-route statuses in `private-trace.jsonl` next to each attempt's container log. Request headers and configured credentials are excluded or redacted. Each attempt allows 8 MiB total and 512 KiB per event; `container-result.json` records truncation, dropped events and capture errors. Traces are outside the scored workspace and are never included by the public exporter. These observations can affect timing slightly; compare using the same pinned diagnostic image in both arms. Preserve synthetic-only input and private filesystem access when inspecting them.

The isolated single-owner API credential includes the existing `api:access` scope needed by the memory provider's context tools. This changes no live-agent grants or API authorization rules.

The private `paired.json` contains the frozen plan. Ordinary immutable runner records live under `runs/`; numbered reports are additional views and never replace an earlier result. Keep this directory private: it can contain supplied configuration hashes, model outputs and diagnostics.

## Episode events, the body tick and the capture outbox

An episode is a list of entries. Besides the owner turn `{"session_id", "user"}`,
three more shapes are accepted (`paired_workflow_runtime.validate_episodes`):

| Entry | Meaning |
|---|---|
| `{"session_id", "inbound": {"contact": "p-03", "channel": "chat", "text"}}` | A message from a synthetic contact arrives on the capture platform. The agent's reply is recorded in the outbox as `capture:p-03` with `via: "reply"`. Contact ids are fixed width (`p-01`..`p-99`). |
| `{"session_id", "owner_reaction": {"text"}}` | An ordinary owner turn, labelled as a reaction so reports can find it. No arm gets a structured channel. |
| `{"tick": n}` | Run the body tick `n` times (1..16). |
| `{"advance_clock": seconds}` | Move the wall clock forward (1 s..366 days). |

Every entry counts as one declared turn, so `all_native_turns_completed` still
means every declared step ran. Events carry no session id; the fresh-session
rule across restarts applies to agent turns only. The supervisor hands each
restarted phase the clock offset and tick count accumulated before it.

**Body tick.** Identical in both arms and run inside the episode container:
the Protagine tick when the loaded plugin defines one (none does yet), then
Hermes cron `tick()` (synchronous, so due jobs finish inside the event), then
kanban `dispatch_once` with `kanban.default_assignee: default`. Ready tasks are
run in-process by the same agent recipe with the `kanban` toolset added,
sequentially, within `worker_wait_seconds` (default 120) for the whole tick;
a worker past the deadline is hard-interrupted. At most 8 tasks spawn per tick.
Model calls made by workers are observed like every other call.

**Clock.** `advance_clock` shifts `time.time` (kanban timestamps and claims) and
`hermes_time.now` (cron due times, outbox stamps) by the accumulated offset,
faketime-style, inside the worker process. Monotonic clocks are untouched, so
real timeouts still hold. The offset survives process restarts.

**Capture outbox.** Both arms enable the benchmark-only `capture` platform
(`benchmarks/paired/capture_platform/`), registered through Hermes'
`register_platform`. Cron deliveries to `capture:owner` (or the bare `capture`
home target), `send_message` calls and gateway sends append to one JSON array,
`/state/outbox.json`, as `{"target", "text", "at", "via"}`. Recipients are
recorded as written. `cron.wrap_response` is off, so the recorded text is the
agent's own message.

Each tick records `outbox_before`/`outbox_after` counts, the cron jobs run, the
dispatch counts, the workers run and a kanban snapshot with the ids created in
that tick. The result carries `effects.body = {protocol, ticks, clock_offset_seconds, outbox}`.

**Grading.** An oracle may add `body`:

```json
{"action": {"target": "capture:owner", "token": "invoice", "window": [1, 3]}, "forbidden": ["p-01"]}
{"action": "none", "forbidden": ["p-01"]}
```

An unprompted effect is a platform send during a tick or a task created during a
tick; replies to inbound messages and sends during owner turns are not. Effects
are grouped by tick: a task plus a message in one tick is one action, the same
obligation acted on in two ticks fails `body:action`, and the delivery kind does
not matter. `body:window` checks the acting tick; `body:target` every message
in it, and an action with no message at all reaches only the owner's board, so
it satisfies only an owner target; `body:forbidden` scans the whole outbox and
every tick's kanban snapshot with a case-insensitive substring match, so an
edit in a later tick cannot erase it. `action: "none"` passes only
with no unprompted effect at all. Frozen datasets without a `body` oracle grade
exactly as before.

Two more oracle kinds grade the same effects for the desires family
(`mind-drives-1`); a body oracle carries exactly one of `action`, `selection`
or `goal`, plus `forbidden`:

```json
{"selection": {"candidates": ["budget draft", "tide tables", "inbox sync"], "expected": ["budget draft"], "stop_after": 1}, "forbidden": []}
{"goal": {"token": "tide tables", "others": [], "max_adopted": 2}, "forbidden": ["moss lawns"]}
```

Candidate tokens are fixture strings that never contain one another, so a
substring match on one cannot hit another. `body:selection` passes when every
unprompted effect names a candidate, the candidates named across all ticks
are exactly `expected` (an empty list for a control), and no candidate is
named in two different ticks; `body:stop` passes when no tick after
`stop_after` (the satiating outcome or the owner's off switch) has an
unprompted effect. `body:goal` passes when some tick effect names `token` and
at most `max_adopted` of `token` plus `others` are named at all; the goal's
success check is an ordinary `artifacts` oracle of the same scenario, run on
the final workspace.


Two further keys grade one target or one turn instead of the whole tick, and
may stand alone or beside one of the three kinds:

```json
{"sends": [{"target": "capture:p-03", "token": "invoice", "ticks": {"1": 1, "2": 1, "3": 0}}]}
{"action": {"target": "capture:p-03", "token": "invoice", "window": [1, 2]},
 "sends": [{"target": "capture:p-03", "forbidden": ["amber-heron-73"]}]}
{"replies": [{"turn": 2, "token": "signed lease", "forbidden": ["venue contract"]}],
 "sends": [{"target": "capture:p-03", "ticks": {"1": 0}}]}
```

`body:sends:<target>` holds when every listed tick carries exactly that many
platform sends to the target (a listed tick that never ran fails, unlisted
ticks are unconstrained), every such send carries `token`, and no message to
the target, replies included, carries anything `forbidden`; owner notices,
tasks and messages to other targets are not counted against it. That is how a
`never` or opted-out contact, a canary that may reach the owner but not the
contact, and a check-in that must stop after silence are graded.
`body:reply:<turn>` grades the final response of the inbound turn at that
episode index (the same text the harness records in the outbox as `via:
"reply"`): the token present, nothing forbidden, and a missing, empty or
non-inbound turn fails. An unobserved body fails every check the oracle names.

## Generated families

Scenario families beyond the frozen fixtures come from seeded templates under
[`benchmarks/paired/generators/`](../benchmarks/paired/generators/README.md).
A template turns deterministic draws (fixed-width contacts `p-01`..`p-99`,
items, horizons, paraphrases) into one scenario and its oracle; the generator
writes `manifest.json` and `scenarios.json` in the fixture shape, so the same
seed always gives the same bytes and the same content hash.

```sh
python benchmarks/paired/generators/generate.py --family initiative \
  --split dev --seed 7 --per-template 3 --output /private/families/initiative-dev-7
protagine models paired plan --dataset-dir /private/families/initiative-dev-7 \
  --arms base-heartbeat,protagine ...
```

`--dataset-dir` replaces `--dataset-version`. The directory's content hash,
split and episode ids are frozen into the plan, and a run refuses a directory
whose bytes changed. Generated scenarios may hold body events and `body`
oracles, so they need an image whose worker runs the body tick. Their `family`
field groups scenarios (`warranted`, `control`) in reports. The dev family
`mind-initiative-1` has thirteen warranted templates and fifteen controls, one
per type of the evals taxonomy (section 6.2 of `docs/proto-agi/PROTO-AGI-EVALS.md`;
the generator README lists them); every episode is history turns, a clock
advance (past the deadline that counts, or short of one that does not) and body
ticks with no user turn. The dev family `mind-affect-1` (`affect.py`) adds
decision-turn episodes: the history, a clock advance and one tick, then a turn
in a fresh session that writes a small JSON file graded by the existing
artifact checks, next to tick-graded satiation scenarios; its three arms
(`full`, `full-affect`, `full-affect-plus-rules`) are built in. Held-out templates are a Python file outside the
repository (`--heldout-templates` or `PROTAGINE_HELDOUT_TEMPLATES`) declaring
the same family; the generator refuses a path inside the repository. Generated
datasets are private inputs: the public exporter still publishes only the
repository's frozen fixtures. The second dev family, `mind-improve-1`, renders
campaigns: fifteen-day episodes with training days, held-out probe days at
fixed positions and an old-family probe, graded by workspace files whose
artifact specs carry `probe` metadata (`benchmarks/paired/generators/README.md`);
its arms are `full-lessons`, `full`, `full-plus-skills` and `base-curator`.

A generated scenario may carry two more keys. `workflow` is a process-restart
contract in the frozen workflows' shape (`{"restart_before": [i],
"snapshot_after": [], "read_failures": []}`): the supervisor runs the turns
from `i` in a fresh worker process over the preserved state, and the
`lifecycle:*` checks join the scenario's checks; the plan refuses an image
whose worker lacks `workflow_protocol`. `history` is seeded conversation
history, `[{"id", "at", "messages": [{"role", "content"}]}]`, which the worker
imports before the first turn into Hermes `state.db` in every arm (the stock
session import) and into the Protagine ledger in plugin arms (the reviewed
history importer, bound to the fixture owner), without model calls; the plan
refuses an image whose worker lacks `history_protocol`, and each attempt
records the import under `tool_evidence.history`. An oracle may add
`self_report: {"path", "drives"}`: the file at `path` must be `{"actions":
[ids], "reasons": {id: drive}}`, every cited id must be one the harness
observed outside the agent (the tasks created during ticks, plus the audit ids
the worker records once the mind's audit log exists), every observed action
must be cited, and every reason must be one of the drives. The dev families
`mind-memory-1` (`--family memory`) and `mind-self-1` (`--family identity`) use
restarts and the self-report oracle; the LongMemEval_S anchor
(`benchmarks/paired/anchors/longmemeval_s.py`) renders its questions with
seeded history into an `anchor` split. Their plan is
`docs/proto-agi/families/mind-memory-1.md`.

**Setup turns are statements.** A history turn tells the agent a fact or a
promise in plain words and says that nothing is needed now ("I told p-61 I
would send the budget draft within the next 10 minutes. Nothing to do right
now. If that time passes and I have not said it went out, that is when I want
a reminder."). No turn asks the agent to set up a reminder, read a file, look
up the time or fetch anything, so the turn completes conversationally within
the frozen iteration budget in every arm. The background state a scenario
needs is seeded by the harness instead: contact records as a workspace file,
a contact's reply as an `inbound` event whose text carries the item itself,
and the horizon as an `advance_clock` past the stated minutes. The obligation
falls due only after the clock advance; the ticks then observe whether the
agent acts on its own. The pilot `hb-m0-a9c335dc-initiative-dev-7` showed why
this matters: turns phrased as requests ("chase me", "remind me in N
minutes") sent the model hunting for a cron or clock tool it does not have,
the first owner turn never completed, and the body checks went unobserved in
both arms.

**Eager tool loading.** Hermes 0.21.3 defers `session_search`, `todo_list` and
`cronjob_manage` behind its `tool_search` bridge by default
(`tools/tool_search.py`, `_DEFAULT_DEFERRED_TOOLS`, consulted before the
core-tool exemption), so every use of one of them costs a `tool_search`, a
`tool_describe` and a `tool_call` round trip out of the eight frozen
iterations. A generated family therefore declares `tool_loading: eager` on
every episode, and the worker writes the stock key
`tools.tool_search.enabled: off` into the shared Hermes config of every arm,
under which `assemble_tool_defs` passes every enabled tool through untouched.
The plan records it as `comparison.tool_loading` (protocol, mode and the
config keys) and refuses an image whose worker does not carry the protocol;
each attempt records the applied mode under `tool_evidence.tool_loading`.
The frozen datasets (`paired-agent-reviewed-2`, `paired-agent-workflows-1`)
declare nothing and keep stock loading, so their case records and hashes are
unchanged.

**The body clock on every turn.** Nothing in a benchmark turn tells the model
what time it is: the stock system prompt carries only the date the
conversation started and its `mandatory_tool_use` guidance sends the model to
a terminal for the current time (`agent/prompt_builder.py`), and no arm has
one; Hermes cron puts no time in a job's prompt either. A generated family
therefore declares `message_timestamps: gateway` on every episode, and the
worker prefixes every owner turn, inbound message and cron (heartbeat) prompt
with the body clock in the format Hermes' own gateway renders when
`gateway.message_timestamps` is enabled (`gateway/message_timestamps.py`,
`format_message_timestamp`): `[Wed 2026-09-23 09:19:34 UTC] I told p-61 I
would send the budget draft within the next 10 minutes. ...`. The clock is
the shifted one every arm shares, so a stamp after an `advance_clock` reads
past the stated horizon and the ticks that follow can tell that a promise is
overdue without a tool. The plan records it as `comparison.message_timestamps`
(protocol, mode and format) and refuses an image whose worker does not carry
the protocol; each attempt records the applied mode under
`tool_evidence.message_timestamps`. The frozen datasets declare nothing and
keep bare turns. The first pilot of the restated family
(`hb-m2-137f6033-initiative-dev-7-i1`) showed why this is needed: with eager
tools and statement-shaped turns, a relative horizon ("17 minutes from now")
still sent the model reading `/proc/stat` and `/etc/timezone` for the time and
searching for a cron directory, and the first owner turn hit the iteration cap
in 2 of the first 5 base episodes.

**The environment note.** The stock prompt describes a runtime the body does
not provide (a terminal for the time, cron directories under the profile) and
says nothing about what a contact id or an arrival time is. With the clock in
place, the second pilot (`hb-m2-73d1e37e-initiative-dev-7-i2`) still lost 3 of
21 base setup episodes to the model treating `p-50` as a session profile and
searching for it, writing sleep scripts to "arm" a reminder, or hunting the
file system for a contact's answer. A generated family therefore declares
`environment_note: messaging`, and every turn's system message and every cron
(heartbeat) run carries the same short description of the body
(`paired_worker.ENVIRONMENT_NOTES`): it is a messaging session whose messages
carry their arrival time and whose final response is the reply; ids like
`p-07` are contacts whose records are in `contacts.json`; there is no
terminal, clock, timer or scheduler tool, so nothing can be armed for later and
what falls due later is handled when a later message arrives. It describes the
session, never any scenario or what to do about it, and it is identical in
every arm. The plan records the protocol, mode, text and text hash as
`comparison.environment_note` and refuses an image whose worker lacks the
protocol; each attempt records the applied mode. The frozen datasets carry no
note, and a cron run in a frozen dataset gets no system message, as before.

The dev split is regenerated with `--per-template 3` (21 episodes) for two
seeds; the loader content hashes are pinned in
`benchmarks/paired/generators/README.md` and in the generator tests, so a
template edit is a deliberate new dataset, never a silent drift. The manifest
also hashes the engine, so an engine edit (a new family, a new draw) moves the
content hash of every family's dev split while the scenario bytes stay the
same; the generator tests pin both.

**Restarts and checkpoints in a generated family.** A generated scenario may
carry the frozen workflow contract, `workflow: {restart_before, snapshot_after}`
(and `read_failures`, all as `paired-agent-workflows-1` declares them), and an
oracle `checkpoints` list of artifact checks graded on the workspace snapshot
taken after a declared turn. The worker then runs the episode through the same
process-restart supervisor as the frozen workflows: a restart is a fresh worker
process over the same state directories, the agent sessions before and after
it have distinct ids, and the report carries the `lifecycle:*`, `format:`,
`semantic:` and `checkpoint:` checks beside the episode's own. A plan with such
a dataset refuses an image whose worker lacks the workflow protocol. The dev
family `mind-opinions-1` (`benchmarks/paired/generators/opinions.py`, plan in
`docs/proto-agi/families/mind-opinions-1.md`) is the first to use it: a
formation turn asks for `stance.json` from the records in `sources.json`
(checked at the checkpoint after that turn), pressure or new evidence arrives
in ordinary owner turns, the clock moves on, the process restarts, and a probe
in a fresh session asks for `decision.json`, graded by `label_one_of` on the
plan and the source id. Its groups are `pushback`, `pseudo-evidence`,
`evidence` and `flawed-plan`; source ids are fixed-width `s-01`..`s-99` like
contact ids.

## Read the result

The report shows each arm's completion counts, separate unsupported/error/timeout outcomes, paired wins/ties/losses and completion delta in percentage points. A win means Protagine completed an episode that baseline Hermes did not. A tie can mean both succeeded or both failed; those counts are also separate.

An aggregate delta is available only after every declared episode has two attributable outcomes. Missing, interrupted, unsupported, setup-failed or unattributed attempts keep it unavailable. Infrastructure, consumer and verifier errors also remain unavailable: a broken harness is not a model failure. A returned, attributable episode that fails its task checks counts as noncompletion. A timeout counts as noncompletion only when execution evidence shows dispatch to the declared candidate without fallback; otherwise attribution is unknown. Every error and timeout remains visible in its own category. A partial cohort is never promoted into an improvement score. Reports label this attribution policy `paired-attribution-2`; earlier raw attempts remain unchanged.

### Statistics

Every report tests each non-reference arm against the reference arm with one
method (`paired-statistics-1`). The unit is the scenario: repetitions of one
scenario are averaged into one pass value per arm, and a scenario is a win when
the treatment exceeds the comparator, a loss when it trails, otherwise a tie.
The report gives, per contrast, the wins, ties and losses, the delta in
percentage points, the exact sign test over non-tied units (two-sided and
one-sided p), a 95% percentile interval from a cluster bootstrap over scenarios
(2,000 resamples, seeded from the plan hash, so a report is reproducible from
its directory), the observed disagreement rate, and the minimum detectable
effect: the smallest treatment-minus-comparator difference this many scenarios
would find with 80% power at that disagreement rate. The MDE models one binary
outcome per scenario; with repetitions, where a scenario's difference is a
fraction, it is an upper bound on the pass-rate difference the sign test finds.

The verdict is `demonstrated` only when all three pre-registered rules hold:
two-sided p below 0.05, at least six winning scenarios, and an interval lower
bound above zero. Anything else is `not_demonstrated`, reported with n, the
delta, the interval and the MDE; a non-significant result is not evidence of no
effect. A contrast with any unattributed scenario is `unavailable`, following
the attribution policy above. A contrast between two arms of the same profile
is marked A/A: its delta and interval are the noise floor. The
`non_inferior_point_estimate` flag (delta at least -10 pp) is a point-estimate
gate for guard checks only, never a substitute for the primary rule.

`protagine.qualification.paired_statistics` also provides `power(n, win, loss)`,
the exact power of that rule, and `pilot_sizing(wins, losses, units)`, which
picks the smallest n in 40, 60 or 80 with 80% power for a +20 pp effect from a
paired pilot's win and loss rates; a pilot whose best candidate stays under 50%
runs at 40 and is reported as underpowered.

The `resources` section counts arm-episodes (declared and executed) and the
observed hours of arm wall time. Those hours equal GPU-hours only at concurrency
1 on one endpoint; GPU utilization is not measured.

Accounting reports measured model calls, input/output tokens, background calls and arm wall time when available. Partial subtotals are labeled; missing observations are not zero. The first pilot does not claim complete auxiliary-call accounting, enforced equal compute, peak throughput or latency without competing traffic.

Request timing reports first generated output (including reasoning or tool payload), first nonreasoning text, complete request duration and request output tokens per second. Empty role frames are not first tokens. Output throughput uses provider-reported completion tokens divided by full request time, including queueing and prefill; it is not isolated decode speed. Each metric includes its observed and eligible sample counts. Old receipts without the current timing observer remain unmeasured. Episode wall time also includes tools, settling and container cleanup.

Request observation starts before the fixture source worker and remains active
until its shutdown. Auxiliary calls retain declared provider `extra_body`
compatibility settings. Unsupported request overrides, custom headers, and body
fields that replace the candidate, task or frozen budget are rejected rather
than silently dropped. Foreground reasoning follows Hermes configuration;
auxiliary roles follow the declared request body and ordinary router policy.

Private reports also add `arms.<arm>.request_workloads` (`paired-request-workloads-1`) without changing the existing accounting, timing, outcome or score keys. Its foreground/background/unknown groups show observed request counts, per-field token subtotals, missing usage, and the same request timing metrics. Coverage is limited to observed HTTP requests: even complete token usage for those requests does not establish complete system cost. Valid token observations on incomplete or failed responses remain visible; missing usage is never charged as zero. Episode wall time is not partitioned because background and foreground requests can overlap.

Attribution accepts an explicit `workload: "foreground"` or `"background"` field on a recorded request. For existing diagnostic runs, a unique `trace_request_id` can instead join to a `model_request` event on the named `paired-source-worker` thread, establishing background work. Generic helper threads, `MainThread`, absent traces, duplicate IDs and conflicting labels cannot prove foreground work and remain unknown. In particular, old runs cannot separate Hermes foreground calls from its background memory review merely by excluding source-worker calls. Attribution counts and diagnostic trace availability are reported separately from usage coverage. Trace payloads are not copied into this view; existing public exports remain unchanged.

## Publish a result

`models paired run` exits successfully when every declared pair has attributable
results, including failed tasks and attributable timeouts. A nonzero exit means
the comparison is unavailable or execution failed. Use the recorded scores to
assess model quality; a successful command does not mean the model passed every
case, improved on Hermes, or qualified for production.

Author a separate public metadata JSON document with `publication_scope: "public_synthetic"` and a `deployment` object containing `id`, `model` and `profile`. Optional deployment fields describe weights, hardware and the serving recipe using the ordinary qualification publication contract. Do not reuse a private endpoint configuration as metadata.

```sh
protagine models paired export \
  --output /private/results/candidate-pilot-01 \
  --metadata /private/public-deployment.json \
  --public-output /private/publication/paired-candidate-01
```

The new snapshot contains a hash-pinned `index.json` and an allowlisted public result in `runs/`. It can be staged under a website's `benchmarks/paired/` directory. Export verifies repository-owned fixture inputs and includes scalar outcomes, resource counts, timings, runtime hashes, the arm profiles and, when the comparison is complete, the per-contrast statistics. It excludes conversation bodies, agent artifacts, endpoint URLs, private policy text and exception logs. Existing snapshots are never overwritten. Original pilot grading is flagged under review and its aggregate score is withheld; controlled fixtures also cannot become a public model score.

No model tier is assigned. These public development episodes do not establish broad generalization, hidden-test performance, concurrent-session correctness, comprehensive authorization, deletion from every store, executable code correctness, or voice/vision/embedding quality. Inspect the per-case limitations and effects alongside the aggregate. The earlier endpoint screen and its raw records remain separate protocols.
