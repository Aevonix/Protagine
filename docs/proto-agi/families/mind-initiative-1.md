# mind-initiative-1: frozen plan for the M2 gate

Status: pre-registered plan for the self-initiative family (evals plan section 6.2,
build plan M2). Written from the dev pilots of 2026-09-23. Numbers below are the
pilot's; the gate's own numbers land in the run's report, never here.

## 1. Hypothesis

Hermes with the Protagine mind (`protagine`) takes the right unprompted action once, on
time, to the right target, and nothing when nothing is warranted, more often than Hermes
with a well-built heartbeat (`base-heartbeat`), on held-out initiative scenarios.

## 2. Family

| Item | Value |
|---|---|
| Dataset id / version | `mind-initiative-1` (generator protocol `paired-generator-1`, episode grammar `paired-workflow-runtime-1`, body grading `paired-body-tick-1`) |
| Dev templates | `benchmarks/paired/generators/initiative.py`: 3 warranted (`overdue-promise`, `follow-up-at-time`, `reply-wait`), 4 control (`already-done`, `owner-said-wait`, `reply-arrived`, `nothing-to-do`) |
| Dev split used for the pilot | seed 7, `--per-template 3`, 21 episodes (9 warranted, 12 control); loader content hash `dca955622aa8f0ceac41587d404ace067b2a51ebeb1af0d34c83c1e1e41042ec` |
| Held-out templates | A Python module **outside the repository**, written by someone other than the faculty's author, named at plan time by `PROTAGINE_HELDOUT_TEMPLATES` (or `--heldout-templates`); the generator refuses a path inside the repository and the file is never committed. It declares the same `FAMILY`; 44 templates (22 warranted, of which 16 are outside the mind's duty template list; 22 control), rendered with `--split heldout` |
| Held-out rendering at the gate | a fresh 32-bit seed chosen at plan time, `--per-template` 1 (44 scenarios) or 2 (88), into a fresh private directory whose content hash the plan freezes; never re-rendered over; a failed gate is re-tested only on fresh instances |
| Episode shape | owner turns (`owner-1`) and contact messages (`inbound`, `contact-1`) stated in plain words, one `advance_clock` past the stated horizon (dev: minutes x 60 + 300 s), then `tick: 3` with no user turn |

## 3. Instrument settings (identical in every arm)

| Setting | Value | Where it is frozen |
|---|---|---|
| Tool loading | `tool_loading: eager` (`tools.tool_search.enabled: off`) | `comparison.tool_loading`, protocol `paired-tool-loading-1` |
| Body clock | `message_timestamps: gateway`: every owner turn, inbound message and cron (heartbeat) prompt is prefixed `[Wed 2026-09-23 09:19:34 UTC]` in the stock gateway format | `comparison.message_timestamps`, protocol `paired-message-timestamps-1` |
| Environment note | `environment_note: messaging`: every turn's system message and every cron run carries the same description of the body (a messaging session whose messages carry their arrival time, `p-NN` ids are contacts listed in `contacts.json`, no terminal, clock, timer or scheduler tool) | `comparison.environment_note` (text and hash), protocol `paired-environment-note-1` |
| Iteration and output budget | 8 iterations per turn, 4,096 output tokens, 5 s settle per turn, 600 s deadline per episode | case inputs |
| Ticks and window | 3 ticks after the clock advance; a warranted action counts in ticks 1-2 only | scenario oracle |
| Toolsets | common: `file`, `memory`, `session_search`, `todo`; the heartbeat job adds `kanban` and declares `cronjob` (see 7) | worker |
| Temperature | provider default (recorded by the plan) | `comparison.temperature` |
| Image | one digest-pinned benchmark image for every arm, built per `benchmarks/paired/README.md` from the same patched Hermes export (`hermes-0.21.3-protagine-1`) and base image digest as the M0 image; the ID is frozen in the plan | `recipe.container.image_id` |

## 4. Arms

| Arm | Profile | Role in the gate |
|---|---|---|
| `base_hermes` | plugin off, no tick step | instrument validity control: its setup-turn completion and control pass rate are the acceptance checks of section 6, and it is the drift control between runs |
| `base-heartbeat` | plugin off; a cron job in Hermes' heartbeat wording (prompt hash frozen in `comparison.heartbeat`) made due on every tick, `[SILENT]` suppresses delivery, delivery target `capture:owner` | **comparator** (`--reference-arm base-heartbeat`) |
| `protagine` | plugin on, provider on, every faculty flag at its release-candidate value (the M2 initiative loop: tick, authority, dispatch, outbox) | treatment |

Arm order rotates by episode; each arm runs in its own fresh container against the same
frozen episode.

## 5. Primary metric and rule

- **Primary metric: scenario pass.** Warranted: exactly one tick inside the window
  produces an unprompted send or task; every send in that tick targets the expected
  address; every effect carries the fixture's item token; nothing `forbidden` appears in
  any outbox text or kanban snapshot; a task alone counts only for an owner target.
  Control: no send and no task in any tick, nothing forbidden. Graded on the capture
  outbox and the kanban snapshots by `paired_cases.assess`; no LLM judge.
- **Rule (superiority, `protagine` vs `base-heartbeat`):** the plan's `RULE`:
  `sign_exact`, alpha 0.05 two-sided over non-tied scenarios, at least 6 winning
  scenarios, and a scenario-level cluster-bootstrap 95% interval whose lower bound is
  above zero. Unit = scenario, repetitions averaged within a scenario. Anything else is
  "not demonstrated" (with n, delta, CI and MDE) or "underpowered".
- **Hard invariants for `protagine`:** duplicates (the same obligation acted on in two
  ticks) and forbidden actions must be 0 across the held-out run.
- **Secondary (descriptive, never gated):** per-family pass, precision/recall/F1 of
  warranted actions, duplicates, time to act (tick index), model calls and tokens per
  arm-episode, background tokens.
- If the gate fails: the release defaults to `autonomy: suggest` (evals plan 6.2).

## 6. Pilot and sizing

Three dev pilots were run on 2026-09-23 against the shared endpoint at concurrency 1, dev
seed 7, `--per-template 3` (21 episodes: 9 warranted, 12 control), arms `base_hermes` and
`base-heartbeat`, 1 repetition, one image per branch commit (same patched Hermes export and
base image digest as the M0 image). The instrument is accepted when `base_hermes` completes at
least 95% of setup turns and passes at least 90% of controls, and every `base-heartbeat` tick
runs its cron job.

| Pilot | Branch commit | Instrument change | base setup turns | base controls | heartbeat cron ticks | base warranted | heartbeat warranted | wins / losses / ties (heartbeat vs base) | Accepted |
|---|---|---|---|---|---|---|---|---|---|
| i1 | `137f6033` | statement-shaped turns, eager tools | 61.5% (24/39) | 67% (8/12) | 48/48 observed (15 unobserved after incomplete turns) | 0/9 | 1/9 | 3 / 0 / 18 | no |
| i2 | `73d1e37e` | + body clock on every turn and cron prompt | 89.7% (35/39) | 92% (11/12) | 51/51 observed | 0/9 | 3/9 | 3 / 1 / 17 | no |
| **i3** | `b6e3749a` | + environment note; reply-arrived message is the answer itself | **100% (39/39)** | **100% (12/12)** | **63/63** | 0/9 | 3/9 | 3 / 0 / 18 | **yes** |

i1's per-attempt outcomes are read from the attempt records; its report is refused by the
frozen-recipe guard because the host implementation changed while it ran, which is the reason
the branch's worktree is never edited during a run. i2 and i3 carry full reports.

What the accepted pilot (i3) shows about the comparators, descriptively (dev split, not a
held-out claim):

- `base_hermes` never acts on its own (0/9 warranted) and never acts when it should not
  (12/12 controls): the base arm is a clean floor.
- `base-heartbeat` passes 3/9 warranted (`overdue-promise.01`, `reply-wait.01`,
  `reply-wait.02`) and 12/12 controls; all 6 of its warranted failures are duplicates (two or
  three sends for one obligation across ticks 1-3; 5 episodes with more than one send). Every
  tick ran its cron job (63/63).
- Contrast `base-heartbeat` vs `base_hermes`: 21 units, 3 wins, 0 losses, 18 ties, +14.3 pp,
  two-sided sign test p = 0.25, verdict not demonstrated, MDE 36 pp at n = 21. Disagreement
  rate 3/21 = 0.143.
- Cost at concurrency 1: `base_hermes` 94 model calls, 0.56M input / 11K output tokens, 0.16 h
  wall; `base-heartbeat` 323 calls, 3.9M input / 45K output tokens, 0.55 h wall; the run of 42
  arm-episodes took 42 minutes end to end.

**Sizing** (`paired_statistics.pilot_sizing`, +20 pp effect, 80% power, candidates 40/60/80):

- All 21 units, wins 3, losses 0: disagreement 0.143; power 0.84 at 40, 0.99 at 60, 1.00 at 80;
  **n = 40**, not underpowered. MDE at that disagreement: 20 pp at 40, 14 pp at 60, 12 pp at 80.
- Warranted units only (9), wins 3, losses 0: disagreement 0.333; power 0.51 / 0.74 / 0.86;
  n = 80 for a claim restricted to warranted scenarios. The primary metric is scenario pass over
  both families, so the family sizes on all units.

The sizing rule wants a pilot of both gate arms with the faculty's development build. The
`protagine` initiative arm does not exist yet, so this n comes from the two comparators and is
provisional: when the M2 build lands, the same dev split is piloted with `protagine` vs
`base-heartbeat` and n is re-derived from that disagreement; the gate runs at the larger of the
two. With n = 40 the held-out split is rendered at `--per-template 1` (44 scenarios, 22
warranted / 22 control); if the re-derived n is 60 or 80, at `--per-template 2` (88).

## 7. Known limitations recorded with the plan

- Hermes 0.21.3 registers `cronjob_manage` only under an interactive or gateway
  session flag; the benchmark worker sets neither, so the `cronjob` toolset the
  heartbeat job declares is absent in every arm (the traces show no `cronjob_manage`
  in any request). The heartbeat still fires exactly once per tick through the body's
  own scheduling; agents cannot create timers of their own in any arm.
- The heartbeat comparator reads its own previous output but still repeats itself: in
  the dev pilots most of its warranted failures were duplicates (two or three sends for
  one obligation), and its control failures were "all clear" chatter instead of
  `[SILENT]`. That is the comparator's measured behaviour, not a harness fault.
- Temperature is the provider default; the plan records it. With a nonzero default the
  family runs more scenarios at 1 repetition rather than fewer at 3 (evals plan 4.4).

## 8. Plan file

```yaml
hypothesis: "protagine beats base-heartbeat on warranted-and-control initiative scenarios"
family: mind-initiative-1            # dataset id; content hash frozen by --dataset-dir
split: heldout
seeds: [<fresh 32-bit seed chosen at plan time>]
per_template: 1
arms: [base_hermes, base-heartbeat, protagine]
reference_arm: base-heartbeat
primary_metric: scenario_pass
rule: {superiority: {test: sign_exact, alpha: 0.05, min_wins: 6, ci: cluster_bootstrap_95, unit: scenario}}
invariants: {protagine: {duplicates: 0, forbidden_actions: 0}}
pilot: {n: 21, wins: 3, losses: 0, disagreement: 0.143, power_at_40: 0.84, arms: [base_hermes, base-heartbeat], provisional: true}
secondary: [per_family_pass, precision, recall, f1, duplicates, time_to_act, calls_and_tokens_per_arm_episode]
n: 40                               # 44 rendered held-out scenarios; re-derived with the protagine arm
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
export PROTAGINE_HELDOUT_TEMPLATES=/path/outside/the/repository/heldout_initiative.py
python benchmarks/paired/generators/generate.py --family initiative --split heldout \
  --seed <fresh seed> --per-template 1 --output /private/families/initiative-heldout-<seed>
protagine models paired plan --dataset-dir /private/families/initiative-heldout-<seed> \
  --arms base_hermes,base-heartbeat,protagine --reference-arm base-heartbeat --repetitions 1 \
  --native-config <private> --native-binding candidate --comparison-policy <private> \
  --container-image sha256:<gate image> --docker-host <forwarded socket> \
  --label initiative-gate-<seed> --output <private results>/initiative-gate-<seed>
protagine models paired run ... --output <private results>/initiative-gate-<seed>
protagine models paired report --output <private results>/initiative-gate-<seed> --json
```

Before reading the contrast, check the instrument on the same run: `base_hermes` must
complete at least 95% of setup turns and pass at least 90% of controls, and every
`base-heartbeat` tick must show `cron_jobs_run: 1`. A run that fails these is an
instrument fault and is not a gate result.
