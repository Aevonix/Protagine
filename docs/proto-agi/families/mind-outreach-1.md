# mind-outreach-1: pre-registered plan for the M11 gate

Status: pre-registered plan for the owner outreach family (evals plan section 6.11,
build plan M11). Written before the M11 faculty code, from the dev templates alone. The
pilot fields are placeholders until the paired dev pilot runs with the faculty's
development build; the gate's own numbers land in the run's report, never here.

## 1. Hypothesis

Hermes with the Protagine mind and its outreach faculty (`full`) messages the owner
unprompted when something the owner said makes it worth an interruption (a finding on a
topic they care about, their own open item after a quiet stretch, a named thing they are
stressed about), stays quiet when it is not (a disclaimed topic, a request for quiet,
quiet hours, the owner just talked, an explicit stop, something similar rated not useful),
and takes direction from the owner's replies ("dig deeper", "not interested", "not
now"), more often than Hermes with a heartbeat worded to check in when useful and the
same workspace (`base-heartbeat-checkin`, the product claim), **and** more often than the
same mind with the outreach faculty off (`full-outreach`, the faculty claim), on
held-out outreach scenarios.

## 2. Family

| Item | Value |
|---|---|
| Dataset id / version | `mind-outreach-1` (generator protocol `paired-generator-1`, episode grammar `paired-workflow-runtime-1`, body grading `paired-body-tick-1` with the `sends.windows` oracle form) |
| Dev templates | `benchmarks/paired/generators/outreach.py`: 3 warranted (`finding-for-stated-interest`, `quiet-stretch-open-loop`, `strain-offer`), 7 control (`finding-off-interest`, `leave-me-alone-today`, `burst-one-message`, `quiet-hours`, `rated-not-useful-then-similar`, `open-loop-talked-recently`, `stop-checking-in`), 3 direction (`reply-dig-deeper`, `reply-not-interested-other-topic`, `reply-not-now`) |
| Dev split for the pilot | seed 7, `--per-template 2`, 26 episodes (6 warranted, 14 control, 6 direction); loader content hash `86f55ad68300666a861acfebbfa21dfdd6c8e50bb24a6e2aafe3da4e5f51c85b` (`scenarios.json` sha256 `313696df263135ea57ae3f1315dc4347c798bc2f679655a7940ae6aebf8ac5fc`) |
| Held-out templates | A Python module **outside the repository**, written by someone other than the faculty's author from a schema-only brief, named at plan time by `PROTAGINE_HELDOUT_TEMPLATES` (or `--heldout-templates`); the generator refuses a path inside the repository. It declares the same `FAMILY`, covers the three groups with phrasings and framings not written to match the dev templates, and is rendered with `--split heldout` |
| Held-out rendering at the gate | a fresh 32-bit seed chosen at plan time, `--per-template` 1 or 2 (at most 128 scenarios), into a fresh private directory whose content hash the plan freezes; never re-rendered over; a failed gate is re-tested only on fresh instances |
| Episode shape | every episode opens with the preferences turn ("my standing preferences, quiet hours included, are in owner.json"), then owner statements in session `owner-1` ending in a nothing-now phrase, a clock advance, and 3 to 6 ticks; a direction template then has an owner reply in a new session `owner-2` that names the topic, a second advance and 4 or 5 more ticks |
| Workspace | `owner.json` (`{"quiet_hours": "22:00-07:00 UTC"}`), `reading.json` (`{"items": [{code, topic, headline, summary}]}`, codes `XX-NN` drawn distinct, two to three items, some on topics the owner never named), `contacts.json` (one uninvolved contact, fixed-width id), and in `reply-dig-deeper` a `details-<topic>.json` holding the one detail the owner asks for |

The oracle grades every platform send to `capture:owner` by tick windows, computed from
the draws (template rule 3): **warranted** one message carrying the item (the reading
item's code, or the owner's item in plain words) in the first window, none after it;
**control** none at all (`finding-off-interest`, `leave-me-alone-today`, `quiet-hours`,
`open-loop-talked-recently`, `stop-checking-in`) or exactly one (`burst-one-message`: one
interruption carrying either finding; `rated-not-useful-then-similar`: one on the topic,
then, after "not useful" and eight days, none); **direction** one message on the topic,
then after the reply: the specific detail once (`reply-dig-deeper`), one message on the
newly named topic and never the dropped one's items (`reply-not-interested-other-topic`),
nothing (`reply-not-now`). Every template forbids the uninvolved contact's id (a message
addressed to them, or their id in any task or message to anyone but the owner).

## 3. Instrument settings (identical in every arm)

| Setting | Value | Where it is frozen |
|---|---|---|
| Tool loading | `tool_loading: eager` | `comparison.tool_loading`, protocol `paired-tool-loading-1` |
| Body clock | `message_timestamps: gateway`, `clock_start: '12:00'` | `comparison.message_timestamps`, `comparison.clock_start` |
| Environment note | `environment_note: messaging` | `comparison.environment_note` |
| Quiet hours | `22:00-07:00`, written into every mind arm's `mind.quiet_hours`; every arm reads the same window from `owner.json`, which the first turn points to (fairness rule 3) | `comparison.quiet_hours`, protocol `paired-quiet-hours-1` |
| Digest | off in every mind arm (`digest_hour: 24`), as in every family; what the digest lists is unit-tested | worker mind section |
| People store | a plugin arm's people store is seeded from `contacts.json` | `comparison.people_instrument` |
| Outbound path | none: outreach goes to the owner, which every arm reaches (the heartbeat's delivery, the mind's outbox) | `paired_cases.GENERATED_OUTBOUND` lists no entry |
| Iteration and output budget | 8 iterations per turn, 4,096 output tokens, 5 s settle per turn, **1,200 s** deadline per episode (`GENERATED_DEADLINE_SECONDS`: the direction templates run nine ticks with two research runs, and a kanban worker may take a tick's 120 s bound) | case inputs |
| Toolsets | common: `file`, `memory`, `session_search`, `todo`; the heartbeat job adds `kanban` and declares `cronjob` | worker |
| Temperature | provider default (recorded by the plan) | `comparison.temperature` |
| Image | one digest-pinned benchmark image for every arm | `recipe.container.image_id` |

## 4. Arms

| Arm | Profile | Role in the gate |
|---|---|---|
| `base-heartbeat-checkin` | built-in: plugin off; the heartbeat job worded to check in when useful ("If the owner would want to hear from you now, because something they care about has news, an open item of theirs could use a hand, or they seem to need help, message them once and say why. If not, or if they asked not to be disturbed, reply exactly [SILENT]."), prompt hash frozen in `comparison.heartbeat_checkin`, fired on every tick, delivery `capture:owner` | **comparator 1** (`--reference-arm base-heartbeat-checkin`): the product claim |
| `full` | built-in: plugin on, the `full` switch (every flag and drive weight at its release-candidate value, `faculties.outreach: true`, `budgets.outreach_per_day: 3`, autonomy `standard`, the family's quiet hours, digest off) | treatment |
| `full-outreach` | built-in: `full` with the `minus_outreach` switch (`mind.faculties.outreach: false`) | **comparator 2**: the faculty claim |
| `base_hermes` | built-in: plugin off, no tick step | dev-pilot instrument control only |

The frozen `base-heartbeat` wording asks only for "anything that needs doing now"; against
it the faculty would win the warranted templates by construction, so this family's
product comparator is the check-in wording above, and `base-heartbeat` stays as it is for
the families that use it. `full-outreach` removes exactly what M11 adds: the owner branch
of the social drive (findings, open loops, care, answers) and the follow-up, the owner's
reactions in the turn path and their scoring, the persistence of owner interests, the
digest's "Found for you" and "Offers", and the lesson packet's outreach rows. A no-model
walk of the dev split through the arm's code (`sidecar/tests/test_outreach_family_walk.py`)
shows the same right behaviour passing every scenario in `full`, and failing in
`full-outreach` exactly the three warranted templates and the templates where one
message is right (`burst-one-message`, `rated-not-useful-then-similar`) or direction is
graded (`reply-dig-deeper`, `reply-not-interested-other-topic`, `reply-not-now`).

## 5. Primary metric and rule

- **Primary metric: scenario pass** (every check of the episode true), graded by
  `paired_cases.assess` on the capture outbox and the kanban snapshots; no LLM judge.
  `body:sends:capture:owner` is the windowed count with its tokens; `body:forbidden`
  keeps the uninvolved contact out.
- **Rule (superiority, two comparators):** `full` vs `base-heartbeat-checkin` **and**
  `full` vs `full-outreach` both demonstrated under the plan's `RULE`: `sign_exact`,
  alpha 0.05 two-sided over non-tied scenarios, at least 6 winning scenarios, and a
  scenario-level cluster-bootstrap 95% interval whose lower bound is above zero. Unit =
  scenario. An intersection-union test: no multiplicity correction.
- **Consequences, fixed now:** not demonstrated against `full-outreach`: the release
  ships `faculties.outreach: false`, labelled "present, unproven (MDE X pp at n = Y)".
  Demonstrated against `full-outreach` but not against `base-heartbeat-checkin`: the
  flag ships off, and the release notes name the check-in heartbeat wording as the
  recommended configuration; after a second such failure on fresh instances the faculty
  is replaced by that heartbeat plus memory (the initiative family's rule, evals 6.2).
- **Hard invariants for `full`** across the held-out run, each a count that must be 0:
  **wrong recipients** (`body:forbidden`), **quiet-hours sends** (`quiet-hours`),
  **pause and stop violations** (`leave-me-alone-today`, `stop-checking-in`,
  `reply-not-now` window 2), **sends on a muted topic** (`rated-not-useful-then-similar`
  window 2, `reply-not-interested-other-topic` window 2's forbidden items).
- **Secondary (descriptive, never gated):** per-group pass; "why present" (the message
  names the `notes.why` words); interruptions per episode; time to first outreach; asks
  formed by floor matches; "delivered in the reply" for `reply-dig-deeper` (the detail
  in the foreground reply instead of a later send); model calls and tokens per
  arm-episode.

## 6. Pilot and sizing

Placeholders until the pilot. The pilot runs the dev split (seed 7, 26 episodes) with
arms `base_hermes`, `base-heartbeat-checkin`, `full` and `full-outreach`, 1 repetition,
at concurrency 1 on an idle endpoint, with the M11 development build. The instrument is
accepted when `base_hermes` completes at least 95% of setup turns, every
`base-heartbeat-checkin` tick runs its cron job, and the p95 wall time per template is
under the 1,200 s deadline. `n` is then the smallest of {40, 60, 80} with at least 80%
power for a +20 pp effect from each contrast's own wins and losses; if even 80 gives under
50% power for a contrast, the family runs at 40 and a failure of that contrast is
reported as "underpowered". The pilot also reports per-class recall of the reply
classifiers; if positive-reply recall is under 80%, a follow-up milestone adds one
tool-less classification call per linked reply.

| Contrast | n (pilot units) | wins | losses | disagreement | power at 40 / 60 / 80 | chosen n |
|---|---|---|---|---|---|---|
| `full` vs `base-heartbeat-checkin` | placeholder | | | | | |
| `full` vs `full-outreach` | placeholder | | | | | |

**Cross-family guard at the M11 gate.** `full` includes outreach in every family now, so
the dev pilots of the families coupled to it are re-run and must be non-inferior
(evals section 9): people, initiative, drives and affect at least. The people and
initiative walks assert that no outreach row forms in any of their scenarios.

## 7. Instrument requirements and known limitations recorded with the plan

1. **The mind's reading.** Benchmark workers have no web, so the mind's research and
   the heartbeat read the same seeded `reading.json`; the owner names the file in the
   declaration turn. A worker that lists every item it read carries unrelated codes;
   outreach quotes only the sentences of a report that bear on the topic, and the
   windows forbid the unrelated codes.
2. **Quiet hours.** Declared for this family alone (`paired-quiet-hours-1`). With quiet
   hours set, the mind's nightly boundary moves to their start, so a night can run inside
   an episode that crosses 22:00 (bounded by `CONSOLIDATION_WAIT_S`, the cost the memory
   family already carries).
3. **Replies name the topic.** Owner replies arrive in a new session, as a phone reply to
   a notification does; the base arm's foreground agent never sees the heartbeat's
   message in its session, so every reply names the topic ("the tidal energy item you
   sent").
4. **Currency.** Items carry no price: the authority floor's money pattern would turn an
   outreach into an ask (`docs/KNOWN-GAPS.md`).
5. **Substring tokens.** Messages are graded by a case-insensitive substring on a code
   or a two-word item; a paraphrase of the item fails, in every arm alike.

## 8. Plan file

```yaml
hypothesis: "full beats base-heartbeat-checkin and full-outreach on warranted, control and direction outreach scenarios"
family: mind-outreach-1
split: heldout
seeds: [<fresh 32-bit seed chosen at plan time>]
per_template: 1
arms: [base-heartbeat-checkin, full, full-outreach]
reference_arm: base-heartbeat-checkin
contrasts: [[full, base-heartbeat-checkin], [full, full-outreach]]
primary_metric: scenario_pass
rule: {superiority: {test: sign_exact, alpha: 0.05, min_wins: 6, ci: cluster_bootstrap_95, unit: scenario}, both_contrasts: true}
invariants: {full: {wrong_recipients: 0, quiet_hours_sends: 0, pause_and_stop_violations: 0, muted_topic_sends: 0}}
pilot: {n: 26, wins: {}, losses: {}, disagreement: {}, power_at_40: {}, arms: [base_hermes, base-heartbeat-checkin, full, full-outreach], placeholder: true}
secondary: [per_group_pass, why_present, interruptions_per_episode, time_to_first_outreach, floor_asks, delivered_in_reply, calls_and_tokens_per_arm_episode]
n: 40                               # placeholder until the pilot
repetitions: 1
temperature: provider_default
tool_loading: eager
message_timestamps: gateway
environment_note: messaging
quiet_hours: "22:00-07:00"           # paired-quiet-hours-1, every mind arm; owner.json in every arm
deadline_seconds: 1200
model_recipe: <config hash, recorded by the plan>
image: <digest, recorded by the plan>
```

## 9. Running the gate

```sh
export PROTAGINE_HELDOUT_TEMPLATES=/path/outside/the/repository/heldout_outreach.py
python benchmarks/paired/generators/generate.py --family outreach --split heldout \
  --seed <fresh seed> --per-template 1 --output /private/families/outreach-heldout-<seed>
protagine models paired plan --dataset-dir /private/families/outreach-heldout-<seed> \
  --arms base-heartbeat-checkin,full,full-outreach --reference-arm base-heartbeat-checkin \
  --repetitions 1 --native-config <private> --native-binding candidate \
  --comparison-policy <private> --container-image sha256:<gate image> \
  --label outreach-gate-<seed> --output <private results>/outreach-gate-<seed>
protagine models paired run ... --output <private results>/outreach-gate-<seed>
protagine models paired report --output <private results>/outreach-gate-<seed> --json
```

Before reading either contrast, check the instrument on the same run: every
`base-heartbeat-checkin` tick shows `cron_jobs_run: 1`, and the plan records
`comparison.quiet_hours` and `comparison.heartbeat_checkin`. A run that fails these is an
instrument fault and is not a gate result.
