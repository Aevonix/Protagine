# mind-affect-1: frozen plan for the M6 gate

Status: pre-registered plan for the feelings family (evals plan section 6.4, build plan M6).
Written on 2026-09-23 from the dev family, before the faculty exists, and ported onto the M4
line, whose built-in arm profiles replace the pilot's overlay file for the two gate arms. The
pilot and its n are placeholders until the M6 development build lands (section 6); the gate's
own numbers land in the run's report, never here. 2026-09-24: the mechanism arm is a built-in
profile (`full-affect-plus-rules`, switch `plus_affect_rules`); no arm is declared in a file.
2026-09-24, after the dev pilot: the Mind section no longer carries the tone line or idle
curiosity (an interest); the tone stays in self-report. Every consumer of `full` still reads the
state: the dev results favoured the rules (6/6, 5/6, 2/3 against the state's 4/6, 3/6, 0/3 on
overload, priority and aggregate), but the gap was put down to iteration caps on identical prompts,
so `affect_rules.RULE_CONSUMERS` stays empty until the held-out gate decides. The dev pilot's
numbers describe `full` before those two lines left the section.

## 1. Hypothesis

Hermes with the Protagine mind and its decaying affect state (`full`) makes the fixture-defined
decision correctly more often than the same mind with the affect flag off (`full-affect`),
on held-out affect scenarios. Mechanism: for each affect consumer, a frozen stateless rule set
over the same inputs (`full-affect-plus-rules`) does not tie or beat the state; where it
does, that consumer reads the rule.

## 2. Family

| Item | Value |
|---|---|
| Dataset id / version | `mind-affect-1` (generator protocol `paired-generator-1`, episode grammar `paired-workflow-runtime-1`, body grading `paired-body-tick-1`, artifact grading `paired_cases.assess`) |
| Dev templates | `benchmarks/paired/generators/affect.py`: 3 treatment (`overload-postpone-curiosity`, `worry-commitment-first`, `aggregate-one-cause`), 2 control (`overload-light-load`, `worry-nothing-due-soon`); the consumer of each is `CONSUMERS` (the name's prefix: `overload`, `priority`, `aggregate`) |
| Dev split for the pilot | seed 7, `--per-template 3`, 15 episodes (9 treatment, 6 control); loader content hash `7810d2c0e19b3430bfbcf8032421905a2752af48136de9212f0f74d4a22f0c50`; rendered dev directories carry it in their manifest |
| Held-out templates | A Python module **outside the repository**, written by someone other than the faculty's author from the schema-only brief, named at plan time by `PROTAGINE_HELDOUT_TEMPLATES` (or `--heldout-templates`); the generator refuses a path inside the repository and the file is never committed. It declares the same `FAMILY`, covers the types of evals section 6.4 whose causes the mind can observe with paraphrase variants, and pairs every treatment shape with a hard control; a cause that is the agent's own failure, a correction of its work or a dismissal of its initiative is produced as an event, never narrated by the owner; rendered with `--split heldout` |
| Held-out rendering at the gate | a fresh 32-bit seed chosen at plan time, `--per-template` 1 or 2 within the loader's 128-scenario cap, into a fresh private directory whose content hash the plan freezes; never re-rendered over; a failed gate is re-tested only on fresh instances |
| Episode shapes | **Decision turn** (every dev template): owner statements (`owner-1`) seeding the state, one `advance_clock`, `tick: 1`, then one turn in a fresh session (`owner-2`) that asks for a small JSON file. `aggregate-one-cause` advances past a soft nudge's stated horizon (minutes x 60 + 300 s) and runs `tick: 3` first, graded on the ticks as well |
| What a decision is | Priority and overload: which item is taken first, written as `{"first"}`; the oracle's labels are the items the state makes first. Aggregate: the same pick, and the soft nudge held on the ticks under load (`body` oracle: no action). Tone has no deterministic grader and is a self-report only |

## 3. Instrument settings (identical in every arm)

| Setting | Value | Where it is frozen |
|---|---|---|
| Tool loading | `tool_loading: eager` (`tools.tool_search.enabled: off`) | `comparison.tool_loading`, protocol `paired-tool-loading-1` |
| Body clock | `message_timestamps: gateway`: every owner turn and cron prompt is prefixed `[Wed 2026-09-23 09:19:34 UTC]` in the stock gateway format; a decision turn after a 3-day advance reads three days later | `comparison.message_timestamps`, protocol `paired-message-timestamps-1` |
| Environment note | `environment_note: messaging` on every turn and cron run | `comparison.environment_note` (text and hash), protocol `paired-environment-note-1` |
| Iteration and output budget | 8 iterations per turn, 4,096 output tokens, 5 s settle per turn, 600 s deadline per episode | case inputs |
| Ticks | one tick between the history and a decision turn (the mind's decay runs on the tick); three ticks in `aggregate-one-cause`, where no action may go out | scenario oracle |
| Toolsets | common: `file`, `memory`, `session_search`, `todo`; kanban workers add `kanban`. No turn has a send tool: the decision turn's work is a file write in every arm | worker |
| Temperature | provider default (recorded by the plan) | `comparison.temperature` |
| Image | one digest-pinned benchmark image for every arm, built per `benchmarks/paired/README.md`; its ID is frozen in the plan | `recipe.container.image_id` |

## 4. Arms

All three arms are built-in profiles of the harness (`paired.PROFILES`): the plugin with the
mind on through the `full` switch (every `mind.faculties` flag and drive weight at its
release-candidate value from `config.DEFAULTS`, `autonomy: standard`, quiet hours and the digest
off), the comparator with the `minus_affect` switch, and the mechanism arm with `minus_affect`
and `plus_affect_rules`:

| Arm | Mind section | Role in the gate |
|---|---|---|
| `full` | built-in: `mind.faculties.affect: true` (the release-candidate value), `mind.faculties.affect_rules: false`; every other faculty at its release-candidate value; every consumer reads the decaying state. Since 2026-09-24 no tone line and no idle curiosity in the decision context | treatment |
| `full-affect` | built-in: `full` with `mind.faculties.affect: false` | **comparator** (`--reference-arm full-affect`): the faculty claim |
| `full-affect-plus-rules` | built-in: `full` with `mind.faculties.affect: false` and `mind.faculties.affect_rules: true` (every consumer reads the frozen stateless rule table of `P/mind/affect_rules.py` over the same snapshot the state reads; no decaying state and no tone line) | mechanism arm, read per consumer |

Arm order rotates by episode; each arm runs in its own fresh container against the same frozen
episode.

**What exists today.** The worker's mind section (`native_memory_worker.mind_section`) writes
every faculty flag from `config.DEFAULTS`, turns `mind.faculties.affect` off for the
`minus_affect` switch and `mind.faculties.affect_rules` on for the `plus_affect_rules` switch,
and a plan refuses an image whose worker cannot apply the switches (arm-profile protocol
`paired-arm-profiles-5`) or tick the mind (`mind_tick`). Both flags are binary and neither has
an environment variable. The arm's Mind reads the owner's appraisal records and reported
outcomes from the arm's own ledger (`serve_mind` passes the `AppraisalStore`), as production
does. The feeling is `P/mind/affect.py` (the decaying state) and `P/mind/affect_rules.py` (the
stateless rules); each consumer reads whichever source the two flags route it to (`affect.route`
in the mind state). A run on an image whose affect faculty reads nothing is an instrument fault,
not a result.

## 5. Primary metric and rule

- **Primary metric: scenario pass**, every check of the episode true (`paired_cases.assess`,
  no LLM judge). Decision-turn scenarios: `all_native_turns_completed` and the decision file's
  `artifact:<path>` check (valid JSON with exactly the declared keys, the label one of the
  oracle's aliases, case-insensitive). `aggregate-one-cause` adds the `body:*` checks (observed,
  action, forbidden) as in `mind-initiative-1`. The pick is the decision; there is no separate
  non-inferiority check.
- **Rule (superiority, `full` vs `full-affect`):** the plan's `RULE`: `sign_exact`,
  alpha 0.05 two-sided over non-tied scenarios, at least 6 winning scenarios, and a
  scenario-level cluster-bootstrap 95% interval whose lower bound is above zero. Unit =
  scenario, repetitions averaged within a scenario. Anything else is "not demonstrated" (with n,
  delta, CI and MDE) or "underpowered".
- **Mechanism (descriptive, decides wiring, not release):** `paired report` contrasts every
  non-reference arm against `full-affect`; the per-consumer reading comes from the same
  report's `pairs[].completion`, grouped by the template's consumer prefix (`overload`,
  `priority`, `aggregate`). For each consumer, if `full-affect-plus-rules` passes at least as
  many of that consumer's scenarios as `full`, that consumer reads the rule instead of the
  decaying state (evals 6.4). The state stays for self-report and tone.
- **Hard invariants for `full`:** duplicates in tick-graded scenarios must be 0.
- **Secondary (descriptive, never gated):** per-consumer and per-template pass in all three
  arms; paraphrase consistency (pass variance across instances of one template); model calls and
  tokens per arm-episode; background tokens.
- If the gate fails: `affect` ships off, labelled "present, unproven (MDE X pp at n = Y)". No
  automatic deletion.

## 6. Pilot and sizing

Not run: the M6 development build does not exist at the time of writing, and the sizing rule
(evals 4.3) wants a paired pilot of both gate arms with it. When it lands, the pilot is the dev
split above (seed 7, 15 episodes) at concurrency 1, arms `full-affect,full,
full-affect-plus-rules`, 1 repetition, one image per branch commit.

| Pilot | Branch commit | Instrument change | comparator setup turns | decision files parse | full wins / losses / ties vs comparator | Accepted |
|---|---|---|---|---|---|---|
| a1 | `<pending>` | dev family as frozen here | `<pending>` | `<pending>` | `<pending>` | `<pending>` |

The instrument is accepted when the comparator completes at least 95% of setup turns and
writes a parseable decision file (the artifact's key shape) in at least 90% of decision-turn
episodes; control pass rates are part of the primary metric and are reported, not gated.

**Sizing:** `paired_statistics.pilot_sizing` over all units (+20 pp effect, 80% power,
candidates 40/60/80) from the pilot's wins and losses; **n = `<pending>`** (default 40 when the
pilot predicts at least 80% power there; the family runs at 40 and reports "underpowered" if even
80 gives under 50%). The held-out module is asked for at least 40 templates (at least 20
treatment, 20 control) so `--per-template 1` yields n at 40 and `--per-template 2` at 60 or 80
within the loader's 128-scenario cap.

## 7. Known limitations recorded with the plan

- The affect faculty and the rule table landed in M6 (section 4); the three arms differ only in
  `mind.faculties.affect` and `mind.faculties.affect_rules`.
- Architecture 4.3 moves affect with the agent's own failed or blocked tasks, owner corrections
  of its work, verified outcomes and dismissals of its initiatives. The dev family states none of
  those in owner prose: an owner describing a failure or a dismissal that never happened to the
  agent can only be read, every arm shares that memory, and a faculty that won on it would be a
  prose parser. Open obligations and near deadlines do reach the mind from owner turns (captured
  commitments), so the overload, priority and aggregate consumers are measured. The
  `strategy_switch` and `satiation` consumers are measured once the harness produces their
  causes as events: the mind's own tick-dispatched task failing twice against a seeded file,
  an owner verdict on its intention through `protagine_self rate` (now in every plugin arm's
  tools), and a nudge of its own that the owner dismisses. M6 reads those events, never owner
  descriptions of them.
- The history lives in a separate session from the decision turn, so cross-session memory is
  a confound shared by every arm (all three are `full`-based). The decision turn restates the
  standing default in neutral words so the default is computable without memory; only the
  history decides whether it stands.
- Tone (consumer 5) has no deterministic grader and is not in the pass.
- Temperature is the provider default; the plan records it.

## 8. Plan file

```yaml
hypothesis: "full beats full-affect on affect decision scenarios"
family: mind-affect-1                # dataset id; content hash frozen by --dataset-dir
split: heldout
seeds: [<fresh 32-bit seed chosen at plan time>]
per_template: 1
arms: [full-affect, full, full-affect-plus-rules]   # all three built in
reference_arm: full-affect
primary_metric: scenario_pass
rule: {superiority: {test: sign_exact, alpha: 0.05, min_wins: 6, ci: cluster_bootstrap_95, unit: scenario}}
mechanism: {per_consumer: "rules arm ties or beats full -> that consumer reads the rule", consumers: [overload, priority, aggregate]}
invariants: {full: {duplicates: 0}}
pilot: {n: <pending>, wins: <pending>, losses: <pending>, disagreement: <pending>, power_at_40: <pending>, arms: [full-affect, full, full-affect-plus-rules]}
secondary: [per_consumer_pass, per_template_pass, paraphrase_consistency, calls_and_tokens_per_arm_episode]
n: <pending>                         # from the pilot; default 40
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
export PROTAGINE_HELDOUT_TEMPLATES=/path/outside/the/repository/heldout_affect.py
python benchmarks/paired/generators/generate.py --family affect --split heldout \
  --seed <fresh seed> --per-template 1 --output /private/families/affect-heldout-<seed>
protagine models paired plan --dataset-dir /private/families/affect-heldout-<seed> \
  --arms full-affect,full,full-affect-plus-rules --reference-arm full-affect \
  --repetitions 1 --native-config <private> --native-binding candidate \
  --comparison-policy <private> --container-image sha256:<gate image> \
  --docker-host <forwarded socket> --label affect-gate-<seed> --output <private results>/affect-gate-<seed>
protagine models paired run ... --output <private results>/affect-gate-<seed>
protagine models paired report --output <private results>/affect-gate-<seed> --json
```

Before reading the contrast, check the instrument on the same run: the image advertises the
switches (section 4), a `full-affect` attempt's mind state shows affect off
(`affect.source: off`), a `full-affect-plus-rules` attempt's shows `affect.source: rules`, the
comparator completes at least 95% of setup turns and writes parseable decision files in at least
90% of decision-turn episodes. A run that fails these is an instrument fault and is not a gate
result.
