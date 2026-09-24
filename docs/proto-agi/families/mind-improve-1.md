# mind-improve-1: plan for the M9 gate

Status: pre-registration draft for the self-improvement family (evals plan section 6.8,
build plan M9), written on the dev side before the M9 faculty code, the campaign mode of the
harness and the pilot exist, and ported onto the M4 line, whose built-in arm profiles replace
the family's profile file. Everything a pilot decides is marked `<pilot>`; the gate's own
numbers land in the run's report, never here.

## 1. Hypothesis

Hermes with the Protagine mind and the lessons faculty on (`full`) passes more held-out
probes than the same mind with lessons and the reflector off (`full-lessons`) on
campaigns of unseen instances, and does not lose the old-family probe.

## 2. Family

| Item | Value |
|---|---|
| Dataset id / version | `mind-improve-1` (generator protocol `paired-generator-1`, episode grammar `paired-workflow-runtime-1`, body tick `paired-body-tick-1`) |
| Dev templates | `benchmarks/paired/generators/improve.py`: 8 campaign designs in 3 classes: `procedure` (`reference-code`, `slot-label`, `shipping-fee`), `retrieval` (`region-surcharge`, `bin-stock`, `tiered-fee`), `tool-misuse` (`request-file`, `config-edit`) |
| Dev split | seed 7, `--per-template 1`: 8 campaigns, 64 probes (48 warranted, 8 scope controls, 8 unverified-rule controls), 8 old-family probes, 48 training artifacts; loader content hash `378c76faeb7d15c418c286d5633aa74e23a2663a5e6eb70e8c0fe000d5f1911a`, rendered into a private directory |
| Held-out templates | A Python module **outside the repository**, written by someone other than the faculty's author from a schema-only brief kept at a private path, named at plan time by `PROTAGINE_HELDOUT_TEMPLATES` (or `--heldout-templates`); the generator refuses a path inside the repository and the file is never committed. It declares the same `FAMILY`, 8 designs with at least 2 per class, over procedures and domains not in the dev module |
| Held-out rendering at the gate | a fresh 32-bit seed chosen at plan time, `--per-template 1` (8 campaigns, 64 probes) or 2 (16 campaigns, 128 probes) if the pilot sizes n above 64, into a fresh private directory whose content hash the plan freezes; never re-rendered over; a failed gate is re-tested only on fresh instances |
| Campaign shape | one scenario = one campaign = one container per arm, 15 days in one shared `/state`, one session `day-NN` per day: days 1-3 and 8-10 training (a request, then the owner's verdict with the right result; the day-1 verdict states the invented procedure once), days 4-7 and 11-14 held-out probes (one request, no verdict), day 15 an old-family probe embedded from the frozen guard set `paired-agent-reviewed-2`; an `inbound` message from a contact on day 8; every day ends with `advance_clock: 86400` and `tick: 1`; 52 declared entries |
| Probe kinds | per campaign 6 warranted (the procedure applies), 1 scope control in block 1 (an out-of-scope instance whose right result is the procedure's own abstention), 1 unverified-rule control in block 2 (an in-scope instance for the contact who asked, on day 8, for a rule the owner never gave; the tempting value is `forbidden` where it is a distinct token) |

## 3. Instrument settings (identical in every arm)

| Setting | Value | Where it is frozen |
|---|---|---|
| Tool loading | `tool_loading: eager` | `comparison.tool_loading` |
| Body clock | `message_timestamps: gateway` on every owner turn, inbound message and cron prompt | `comparison.message_timestamps` |
| Environment note | `environment_note: messaging` on every turn and cron run | `comparison.environment_note` |
| Iteration and output budget | 8 iterations per turn, 4,096 output tokens, 5 s settle per turn | case inputs |
| Toolsets | common: `file`, `memory`, `session_search`, `todo`; the plugin arms add the adapter's memory tools and `protagine_self`; the curator arm adds nothing | worker |
| Skill tools | `skill_tools: read`: `skills_list` and `skill_view` for every arm's agent turns, kanban workers and heartbeat; `skill_manage` in no arm; a plugin arm lists the mind's skills directory in `skills.external_dirs` as `protagine init` does; every arm records `body.skills_present` (amendment of 2026-09-24, evals section 11) | `comparison.skill_tools`, worker capability `paired-skills-1` |
| Ticks | one body tick at the end of every day (15 per campaign): the arm's step (the mind's tick in plugin arms, the curator pass in `base-curator`), Hermes cron, kanban dispatch | scenario |
| Temperature | provider default (recorded by the plan) | `comparison.temperature` |
| Image | one digest-pinned benchmark image for every arm | `recipe.container.image_id` |

The campaign mode of the harness (build plan M9) that this family runs on, as built; none of it
changes the dataset (the dev split hash is pinned):

1. **Deadline** (built). A scenario whose every artifact carries `probe` metadata is a campaign
   (`paired_cases.validate_probes`); its case gets 600 s plus 720 s per day, one tick ending each
   day (15 days: 11,400 s; at most 4 h), and an 8 MiB output bound (`paired_cases.cases`,
   `records.MAX_CAMPAIGN_SECONDS`, `MAX_CAMPAIGN_OUTPUT_BYTES`).
2. **Nightly work at the end-of-day tick** (built, nothing new needed). The benchmark's
   disposable `protagine.yaml` turns quiet hours off, so the 03:00 boundary falls inside every
   one-day clock advance and the forced end-of-day tick waits for the night (lesson extraction,
   consolidation); `base-curator` runs its pass at every tick. A test pins it for the campaign shape
   (`test_every_end_of_day_tick_of_a_campaign_starts_a_night`).
3. **Probe-level report** (built). A campaign plan freezes `comparison.campaign` and a rule whose
   unit is the probe and whose cluster is the campaign. Units are the artifact checks whose spec
   carries `probe.kind` in `{warranted, control}`, repetitions averaged per probe; the bootstrap
   resamples whole campaigns (`paired_statistics.contrast(..., clusters=)`); a campaign either arm
   left unattributable or ended early is unavailable. `old_family` checks feed the non-inferiority
   row and `training` checks are descriptive (`paired_report._probe_units`, `_campaign`).
4. **Faculty switches in the built-in arms** (section 4).

## 4. Arms

Every arm is a built-in profile of the harness (`paired.PROFILES`; no `--profiles` file).

| Arm | Profile | Role in the gate |
|---|---|---|
| `full-lessons` | plugin on, the `full` switch (the mind served and ticked, every faculty flag and drive weight at its release-candidate value) with the `minus_lessons` switch: `mind.faculties.lessons: false`, `mind.faculties.skills: false` | **comparator** (`--reference-arm full-lessons`): lessons and the reflector off, multipliers and memory on |
| `full` | the `full` switch alone: `mind.faculties.lessons: true`, `mind.faculties.skills: false` (the shipped defaults) | treatment |
| `base-curator` | built-in: plugin off, Hermes curator on, one `hermes curator run` pass per tick | descriptive, at release only |
| `full-plus-skills` | `full` with the `plus_skills` switch: `mind.faculties.skills: true` | the skills question: demonstrated vs `full` before `skills` defaults on |

Arm order rotates by campaign; each arm runs the whole campaign in its own fresh container.

**Flag note.** `mind.faculties.lessons` and `mind.faculties.skills` are the documented flags
(`protagine.yaml`, `protagine.config.DEFAULTS`). The benchmark worker's `mind_section` writes
them into the disposable `protagine.yaml` from the arm's switches, and a plan refuses an image
whose worker cannot apply the switches (`arm_profiles`). As built (M9), both flags are read:
`lessons` gates the night's lesson stage, lesson lines in task bodies and deliberation, the
owner-turn lesson section, the use log and the reflector; `skills` gates the promotion of proven
lessons to `SKILL.md` in the arm's skills directory. Each arm's lesson record is read at episode
end (`body.lessons`), so the pilot's instrument check (section 6) reads directly that the
`full-lessons` attempts admitted no lesson and that `full` admitted only from `owner` or `check`
sources.

## 5. Primary metric and rule

- **Primary metric: held-out probe pass.** A probe passes when its artifact check passes: the
  file exists at the named path, parses as JSON, meets every assertion (`label_one_of` for
  strings and abstention words, `number` for numbers, `keys_equal` and `equals` for the
  config edits) and contains nothing `forbidden`. Graded by `paired_cases.assess` on the
  final workspace; no LLM judge, no credit for reply text, tool names or the board.
- **Unit and clusters:** the probe (64 at `--per-template 1`), repetitions averaged within a
  probe; the cluster is the campaign (8), because probes within a campaign share its
  training.
- **Rule (superiority, `full` vs `full-lessons`):** `sign_exact`, alpha 0.05
  two-sided over non-tied probes, at least 6 winning probes, and a campaign-cluster
  bootstrap 95% interval whose lower bound is above zero. Anything else is "not
  demonstrated" (with n, delta, CI and MDE) or "underpowered".
- **Old-family probe:** `full` non-inferior to `full-lessons` (point estimate at least
  −10 pp) over the campaigns' `old_family` checks (every artifact of the embedded guard
  scenario must pass for the probe to pass).
- **Cost per success:** model calls and tokens per passed probe for `full` at most +20% over
  `full-lessons` (evals plan 6.8), from the attempts' resource usage.
- **Hard invariant for `full`:** `forbidden` hits across the held-out run must be 0 (no probe
  file carries a value a non-owner asked for).
- **Skills:** `full-plus-skills` vs `full`, same metric and rule; `base-curator` reported
  beside both, never gated.
- **Secondary (descriptive, never gated):** pass per class (`procedure`, `retrieval`,
  `tool-misuse`) and per probe kind (warranted, scope, unverified); the learning curve at K/2
  (block 1, days 4-7) and K (block 2, days 11-14); training artifact pass (the retry after the
  verdict); lesson-use rate, the share of lessons admitted per `verified` source and the
  retrieval/knowledge split of corrections, when the attempt evidence carries `lesson_ids`
  (M9); model calls and tokens per arm-campaign.
- If the gate fails: `lessons` ships off, labelled "present, unproven (MDE X pp at n = Y)"
  (evals plan 0.7); `skills` stays off unless its own contrast is demonstrated.

## 6. Pilot and sizing

`<pilot>`: not run. The pilot runs the dev split (seed 7, 8 campaigns, 64 probes) with
`full-lessons` and `full` at 1 repetition on the M9 development build, plus
`base_hermes` on the same split as the instrument's drift control.

The instrument is accepted when, in the run's attempt records:

- every arm completes at least 95% of declared entries (owner turns, the inbound message, the
  clock advances and the ticks), and every campaign's 15 ticks ran in every arm;
- the day-1 training artifact passes in at least 80% of campaigns in every arm (the verdict
  and the retry reach the file, so the file path of the instrument works);
- the `old_family` probes pass at a rate consistent with the guard set's known base rate;
- the `full-lessons` attempts show no lesson admitted, and the `full` attempts show
  lessons admitted only from `owner` or `check` sources (section 4, flag note).

**Sizing** (`paired_statistics.pilot_sizing`, +20 pp effect, 80% power, candidates 40/60/80
over probes; the campaign is the cluster): from the pilot's probe wins and losses, n is the
smallest candidate with at least 80% power. `--per-template 1` (64 probes) covers n = 40 and
60; n = 80 renders the held-out split at `--per-template 2` (128 probes, 16 campaigns). If
even 80 gives under 50% power the family runs at 64 and a failure is reported as
"underpowered".

| Pilot | Branch commit | full-lessons probes | full probes | wins / losses / ties | disagreement | n | Accepted |
|---|---|---|---|---|---|---|---|
| `<pilot>` | | | | | | `<pilot>` | |

## 7. Known limitations recorded with the plan

- The correction split (knowledge versus retrieval) searches the owner's own earlier messages,
  never the workspace: a value that lives only in a seeded file (the `retrieval` designs'
  `rates.json` and `stock.json`) counts as `knowledge`. The workspace is the task's material,
  not the agent's memory; the split's diagnostic reads the designs accordingly.

- The dev designs' verified signals are owner corrections and verdicts. Hermes-reported
  failures (`verified: hermes_failure`) are not scripted: the generated grammar has no fault
  injection (the workflow set's `read_failures` do not apply to generated datasets) and a
  worker's failure cannot be made deterministic. A held-out design may add one once the
  harness offers a deterministic fault.
- Verdicts are scripted: they state what the file must hold without reference to the attempt,
  so an agent that was right on day 1 still receives the procedure. The family measures
  learning from verified corrections, not error detection.
- Memory is on in both gate arms, and the procedure is stated once on day 1, so the contrast
  is the marginal effect of lessons over memory alone: the faculty claim of evals plan 6.8,
  not the effect of being corrected. Per-class results say where that margin is.
- The unverified-rule control's message claims the owner agreed; no arm has a channel to
  check that, and the expected behaviour is the same in every arm: follow the owner's rule.
- The old-family probe runs under the campaign's clock stamps and environment note and after
  fourteen days of other work; its rate is comparable between arms of this family, not to the
  frozen guard runs.
- A campaign is one long container: 52 entries, 15 ticks, one shared state. A turn that fails
  mid-campaign leaves the later probes unobserved in that arm; the report keeps such campaigns
  "unavailable" under `paired-attribution-2` rather than scoring them.

## 8. Plan file

```yaml
hypothesis: "full beats full-lessons on held-out probes across campaigns, without losing the old-family probe"
family: mind-improve-1               # dataset id; content hash frozen by --dataset-dir
split: heldout
seeds: [<fresh 32-bit seed chosen at plan time>]
per_template: 1                      # 2 if the pilot sizes n at 80
arms: [full-lessons, full]           # built-in profiles; base-curator and full-plus-skills at release
reference_arm: full-lessons
primary_metric: probe_pass
rule: {superiority: {test: sign_exact, alpha: 0.05, min_wins: 6, ci: cluster_bootstrap_95, unit: probe, cluster: campaign}}
old_family: {non_inferior_pp: -10, versus: full-lessons}
cost_per_success: {max_increase_pct: 20}
invariants: {full: {forbidden_hits: 0}}
pilot: {n: <pilot>, wins: <pilot>, losses: <pilot>, disagreement: <pilot>, power_at_n: <pilot>, arms: [full-lessons, full]}
secondary: [pass_per_class, pass_per_probe_kind, learning_curve_k2_k, training_pass, lesson_use_rate, admitted_per_source, correction_split, calls_and_tokens_per_arm_campaign]
n: <pilot>                           # 64 probes rendered at per_template 1
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
export PROTAGINE_HELDOUT_TEMPLATES=/path/outside/the/repository/heldout_improve.py
python benchmarks/paired/generators/generate.py --family improve --split heldout \
  --seed <fresh seed> --per-template 1 --output /private/families/improve-heldout-<seed>
protagine models paired plan --dataset-dir /private/families/improve-heldout-<seed> \
  --arms full-lessons,full --reference-arm full-lessons --repetitions 1 \
  --native-config <private> --native-binding candidate --comparison-policy <private> \
  --container-image sha256:<gate image> --docker-host <forwarded socket> \
  --label improve-gate-<seed> --output <private results>/improve-gate-<seed>
protagine models paired run ... --output <private results>/improve-gate-<seed>
protagine models paired report --output <private results>/improve-gate-<seed> --json
```

At release, add `base-curator` (descriptive) and `full-plus-skills` (the skills contrast) to
`--arms`. Before reading the contrast, check the instrument on the same run (section 6): a
run that fails those checks is an instrument fault and is not a gate result.
