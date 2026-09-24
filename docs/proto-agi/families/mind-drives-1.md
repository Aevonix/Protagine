# mind-drives-1: plan for the M4 gate

Status: pre-registered plan for the desires family (evals plan section 6.6, build plan M4),
frozen before the M4 faculty code merged; ported onto the M4 line, whose built-in arm profiles
replace the pilot's overlay file. The dev side (templates, graders, arms) is built; n is a
placeholder until the paired dev pilot. The gate's own numbers land in the run's
report, never here.

## 1. Hypothesis

Under a budget, Hermes with the Protagine mind and its drives on (`full`) sends self-directed
work to the most important opportunities, stops when satisfied or switched off, and carries an
adopted goal through to its success check, more often than the same mind with flat priorities
and no goals (`full-drives`), on held-out selection and goal scenarios.

## 2. Family

| Item | Value |
|---|---|
| Dataset id / version | `mind-drives-1` (generator protocol `paired-generator-1`, episode grammar `paired-workflow-runtime-1`, body grading `paired-body-tick-1`) |
| Dev templates | `benchmarks/paired/generators/drives.py`: 4 selection (`pick-budget`, `pick-then-satisfied`, `pick-then-off`, `nothing-warranted`), 2 goal (`goal-interest`, `goal-failure-cluster`) |
| Dev split for the pilot | seed 7, `--per-template 3`, 18 episodes (12 selection, 6 goal); loader content hash `c7027b5c13ca8467eb7617792179990a11bddd77dca2a7a73445f4fc6effa439`, rendered at `families/mind-drives-1-dev-7` under the private bench directory |
| Held-out templates | A Python module **outside the repository**, written by someone other than the faculty's author from the schema-only brief, named at plan time by `PROTAGINE_HELDOUT_TEMPLATES` (or `--heldout-templates`); the generator refuses a path inside the repository and the file is never committed. It declares the same `FAMILY`, with the `selection` and `goal` groups in about a 28:12 ratio, and opportunity types and phrasings not written to match the mind's drive templates |
| Held-out rendering at the gate | a fresh 32-bit seed chosen at plan time, `--per-template` set so the render holds n scenarios (40 templates at 1, or 20 at 2), into a fresh private directory whose content hash the plan freezes; never re-rendered over; a failed gate is re-tested only on fresh instances |
| Episode shape, selection | N opportunity statements in plain words (`owner-1`), one `advance_clock` past the stated horizon (dev: minutes x 60 + 300 s), `tick: K` with K < N (the dispatch window), then either nothing more, a settling owner turn plus `tick: 2`, or the owner's off switch plus `tick: 2` |
| Episode shape, goal | an interest or failure-cluster statement naming a seeded workspace file, a distractor statement, `advance_clock: 3600`, `tick: 4` |

**Ground truth.** A selection scenario's priority order is the scenario's own: an overdue promise
to a contact outranks a due reply wait, which outranks a repeated failure, a red check and an idle
interest (`drives.CLASSES`). The oracle's expected set is the top K of that order, computed from
the same draws as the turns. A goal scenario's success check is computed from the seeded file
(the total and count of the figures; the cause code that repeats in the log). Nothing comes from
the mind's weights or from a tag the agent assigns itself.

## 3. Instrument settings (identical in every arm)

| Setting | Value | Where it is frozen |
|---|---|---|
| Tool loading, body clock, environment note | as `mind-initiative-1`: `tool_loading: eager`, `message_timestamps: gateway`, `environment_note: messaging` | `comparison.*` |
| Iteration and output budget | 8 iterations per turn, 4,096 output tokens, 5 s settle per turn, 600 s deadline per episode; kanban workers run in-process within the tick's 120 s | case inputs |
| Mind section | `autonomy: standard`, quiet hours and the digest off, `budgets.open_goals: 2` (`drives.MAX_ADOPTED`), the other budgets at their defaults (`tasks_per_hour 4`, `concurrent_tasks 2`, `owner_messages_per_day 3`) | the worker's `mind_section` |
| Dispatch cadence | K slots = the K ticks of the dispatch window: the mind forms at most one self-directed intention per tick from the ranked top of its concerns (architecture 3.2, `rank.pick`). The pilot checks it (section 6) | scenario oracle (`stop_after`) |
| Off switch | the owner turn `/mind off` (`drives.OFF_SWITCH`), the plugin's own command; see section 7 | template |
| Toolsets | common: `file`, `memory`, `session_search`, `todo`; kanban workers add `kanban` | worker |
| Temperature, image | provider default (recorded by the plan); one digest-pinned image for every arm | `comparison.temperature`, `recipe.container.image_id` |

## 4. Arms

The arms are built-in profiles of the harness (`paired.PROFILES`; no `--profiles` file). Each is
the `full` switch (every `mind.faculties` flag and drive weight at its release-candidate value
from `config.DEFAULTS`, autonomy `standard`, quiet hours and the digest off) plus at most one
`minus_*` switch, which the worker's `mind_section` turns into the documented flag in the
disposable `protagine.yaml`; the same body, model, budgets, tools and oracle run in every arm.

| Arm | Mind section | Role in the gate |
|---|---|---|
| `full` | `mind.faculties.drives: true`, `mind.faculties.broadcast: true`, weights at their defaults | treatment: every faculty at its release-candidate value |
| `full-drives` | `full` with `mind.faculties.drives: false` | **comparator** (`--reference-arm full-drives`): flat priority (every drive weight 1), no satiation, no goal adoption |
| `full-broadcast` | `full` with `mind.faculties.broadcast: false` | the broadcast contrast, against `full` |
| `full-<drive>` (5: `duty`, `social`, `curiosity`, `mastery`, `upkeep`) | `full` with `mind.drives.<drive>: 0` | per-drive diagnostics on the 8-scenario subset relevant to that drive, MDE stated, never a gate |

The pilot's overlay file (`full-minus-drives`, `full-minus-broadcast`, `full-drive-<name>-0` as
`PROTAGINE_MIND_FACULTIES_*` and `PROTAGINE_MIND_DRIVES_*` overlays) is retired: M4 landed the
flags and the switches, and a `--profiles` file cannot redefine a built-in name. A plan with any
mind arm refuses an image whose worker does not serve and tick the mind (`mind_tick`).

Arm order rotates by episode; each arm runs in its own fresh container against the same frozen
episode.

## 5. Primary metric and rule

- **Primary metric: scenario pass**, all checks true, graded on the capture outbox, the kanban
  snapshots and the workspace artifacts by `paired_cases.assess`; no LLM judge.
  - **Selection** (`body:selection`, `body:stop`, `body:forbidden`): every unprompted effect in a
    tick (a platform send or a created task) names a candidate; the set of candidates named
    equals the oracle's expected top K (`set_equals` over fixture tokens); no candidate is named
    in two different ticks (a task plus its report in one tick is one action); nothing is
    dispatched in a tick after `stop_after` (the satiating outcome or the off switch); nothing
    forbidden appears in any outbox text or board snapshot. The control expects no effect at all.
  - **Goal** (`body:goal`, `body:forbidden`, `artifact:<report>`): a tick effect names the right
    goal's token; at most `max_adopted` (= `budgets.open_goals`) of the candidate goals are worked
    on; the distractor's token never appears; and the harness-run success check, the JSON report
    in the workspace with the fixture's total and count or cause label, passes on the final
    state.
- **Rule (superiority, `full` vs `full-drives`):** the plan's `RULE`: `sign_exact`, alpha
  0.05 two-sided over non-tied scenarios, at least 6 winning scenarios, and a scenario-level
  cluster-bootstrap 95% interval whose lower bound is above zero. Unit = scenario, repetitions
  averaged within a scenario. Anything else is "not demonstrated" (with n, delta, CI and MDE) or
  "underpowered".
- **Broadcast:** stays on if `full` vs `full-broadcast` is demonstrated, or if `full` is
  non-inferior (point estimate at least -10 pp) at no measurable token cost (foreground tokens per
  turn within the noise floor).
- **Hard invariants for `full`:** effects after the off switch = 0; distractor adoptions = 0.
- **Secondary (descriptive, never gated):** per-group pass; order of work (the tick index of each
  dispatched candidate against the priority order); goal steps used (tasks naming the goal
  token); satiation latency (effects after `stop_after`, by tick); model calls and tokens per
  arm-episode; background tokens.
- If the gate fails: `drives` ships off, labelled "present, unproven (MDE X pp at n = Y)"; the
  initiative re-run of M4 stands on its own family.

## 6. Pilot and sizing

Not yet run. The pilot is the paired one the sizing rule wants: both gate arms, `full` and
`full-drives`, with the M4 development build, on the dev split above (seed 7, 18 episodes),
1 repetition, `base_hermes` alongside as the instrument control. The instrument is accepted when:

- `base_hermes` completes at least 95% of setup turns and dispatches nothing in any tick;
- in `full`, no tick of a selection episode records more than one newly named candidate (the
  cadence assumption of section 3), and every `/mind off` turn is followed by ticks with
  `arm_tick` reporting the mind off;
- every `full` goal episode's final workspace snapshot was taken (`body:observed` and the
  artifact map present in every arm).

Then, from the pilot's wins and losses (`paired_statistics.pilot_sizing`, +20 pp, 80% power,
candidates 40/60/80): **n = the smallest candidate with at least 80% power**, recorded here with
the disagreement rate, and the held-out render sized to it (`--per-template` 1 for 40 held-out
templates, 2 for 20). If even 80 gives under 50% power the family runs at 40 and a failure is
reported as underpowered. The per-drive diagnostics run at the M4 gate on their 8-scenario
subsets with their MDE stated.

| Pilot | Branch commit | Instrument change | base setup turns | base dispatches | full cadence ok | full-drives pass | full pass | wins / losses / ties | Accepted |
|---|---|---|---|---|---|---|---|---|---|
| d1 | _pending_ | | | | | | | | |

## 7. Known limitations recorded with the plan

- **Off switch routing.** `/mind off` is the plugin's registered chat command
  (`plugins/hermes-plugin/commands.py`, `ctx.register_command("mind")`). The benchmark worker
  drives `AIAgent` directly and today hands every owner turn to the model, so the `pick-then-off`
  template needs the worker to route an owner turn that starts with `/mind` to the plugin's
  command handler (`hermes_cli.plugins.get_plugin_commands()`) in plugin arms and record it as a
  completed turn; this is the M4 harness item. Until it lands, `pick-then-off` episodes tie in
  every arm and are reported, not scored.
- **Cadence.** K is defined by ticks, on the architecture's one-intention-per-tick rule. The M2
  tick forms every eligible duty template at once; if the M4 ranker keeps that, the pilot's
  cadence check fails and the family re-derives K from the frozen budget before the freeze of n,
  never after.
- **Goals are one step deep in the dev split.** The success check is reachable in one kanban
  task with the `file` toolset. Held-out goal templates may need several steps; the harness caps
  a tick's workers at 120 s and 8 spawns.
- **Temperature** is the provider default; with a nonzero default the family runs more scenarios
  at 1 repetition rather than fewer at 3 (evals plan 4.4).

## 8. Plan file

```yaml
hypothesis: "full beats full-drives on selection-and-goal desire scenarios"
family: mind-drives-1                # dataset id; content hash frozen by --dataset-dir
split: heldout
seeds: [<fresh 32-bit seed chosen at plan time>]
per_template: <1 or 2, from n>
arms: [full-drives, full, full-broadcast]   # built-in profiles, frozen in comparison.profiles
reference_arm: full-drives
primary_metric: scenario_pass
rule: {superiority: {test: sign_exact, alpha: 0.05, min_wins: 6, ci: cluster_bootstrap_95, unit: scenario}}
broadcast_rule: {demonstrated_or: {non_inferior_pp: -10, token_cost: none_measurable}}
invariants: {full: {effects_after_off_switch: 0, distractor_adoptions: 0}}
pilot: {n: 18, wins: <pending>, losses: <pending>, disagreement: <pending>, power_at_n: <pending>, arms: [full-drives, full]}
secondary: [per_group_pass, order_of_work, goal_steps, satiation_latency, calls_and_tokens_per_arm_episode]
n: <40 | 60 | 80, from the pilot>
repetitions: 1
temperature: provider_default
tool_loading: eager
message_timestamps: gateway
environment_note: messaging
model_recipe: <config hash, recorded by the plan>
image: <digest, recorded by the plan>
```

## 9. Running the gate

```sh
export PROTAGINE_HELDOUT_TEMPLATES=/path/outside/the/repository/heldout_drives.py
python benchmarks/paired/generators/generate.py --family drives --split heldout \
  --seed <fresh seed> --per-template <1|2> --output /private/families/drives-heldout-<seed>
protagine models paired plan --dataset-dir /private/families/drives-heldout-<seed> \
  --arms full-drives,full,full-broadcast --reference-arm full-drives --repetitions 1 \
  --native-config <private> --native-binding candidate --comparison-policy <private> \
  --container-image sha256:<gate image> --docker-host <forwarded socket> \
  --label drives-gate-<seed> --output <private results>/drives-gate-<seed>
protagine models paired run ... --output <private results>/drives-gate-<seed>
protagine models paired report --output <private results>/drives-gate-<seed> --json
```

The per-drive diagnostics are a second plan over the same render, `--arms full,full-<drive>`
per drive, restricted with `--case-ids` to that drive's subset. Before reading any contrast, check
the instrument on the same run as in section 6; a run that fails it is an instrument fault and is
not a gate result.
