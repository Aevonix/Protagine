# mind-memory-1 and mind-self-1: frozen plan for the M8 gate

Status: pre-registered plan for the memory family (evals plan section 6.1), the identity
family (section 6.7) and the LongMemEval_S anchor (section 6.1), build plan M8. Written from
the dev render of 2026-09-23, before any pilot: `n` below is the evals plan's default until
the paired dev pilot sets it, and ported onto the M4 line, whose built-in arm profiles replace
the families' profile file. The gate's own numbers land in the run's report, never here.

## 1. Hypotheses

- **Memory.** Hermes with the Protagine mind (`full`) uses facts, updates, preferences,
  corrections and its own earlier results across sessions and restarts, and abstains when
  nothing was said, more often than stock Hermes (`base_hermes`), on held-out memory scenarios.
  `semantic_recall` and `consolidation` each earn their flag against the arm without them.
- **Self.** `full` keeps a stable stance, refuses false premises about its own actions and
  reports what it did with ids that exist, more often than `full-self_narrative`.
- **Anchor.** Descriptive: `full` vs `base_hermes` on 50 LongMemEval_S questions, sign test over
  questions, reported per ability. Never a gate.

## 2. Families

| Item | mind-memory-1 | mind-self-1 |
|---|---|---|
| Dataset id / version | `mind-memory-1` (generator protocol `paired-generator-1`, episode grammar `paired-workflow-runtime-1`, history `paired-history-1`) | `mind-self-1` (same protocols, body grading `paired-body-tick-1`) |
| Dev templates | `benchmarks/paired/generators/memory.py`: 6 `recall` (`fact-after-restart`, `fact-across-channels`, `knowledge-update`, `scoped-correction`, `preference-after-distractors`, `own-action-recall`), 2 `abstain` (`never-said`, `contradiction-ask`) | `benchmarks/paired/generators/identity.py`: 3 `narrative` (`stance-after-restart`, `self-report-after-action`, `self-report-nothing-done`), 2 `premise` (`false-premise`, `true-premise`) |
| Dev split (this render) | seed 7, `--per-template 3`, 24 episodes (18 recall, 6 abstain); content hash `855a8d4e6ab0075cf78e8e3393e313eca3c2b8c3f4e57877cf3d8eccefc6c8b3`, at `~/protagine-bench/families/mind-memory-1-dev-7` | seed 7, `--per-template 3`, 15 episodes (9 narrative, 6 premise); content hash `7adcbd5b230f47f2c61b304bb14b9ed6f3cbf171527a704a9cc3da989d463b2a`, at `~/protagine-bench/families/mind-self-1-dev-7` |
| Held-out templates | A Python module **outside the repository**, written by someone other than the faculty's author from the schema-only brief, named at plan time by `PROTAGINE_HELDOUT_TEMPLATES` (or `--heldout-templates`); the generator refuses a path inside the repository and the file is never committed. It declares the same `FAMILY`, covers the eight scenario types of section 6.1 with phrasings not written to match the dev generators, and keeps the two abstention types as hard controls | Same mechanism; declares `mind-self-1`, covers types (a), (b) with a true and a false premise, and (c) with and without an unprompted action |
| Held-out rendering at the gate | a fresh 32-bit seed chosen at plan time, `--per-template` set so the split holds at least `n` scenarios, into a fresh private directory whose content hash the plan freezes; never re-rendered over; a failed gate is re-tested only on fresh instances | same |
| Episode shape | owner turns in plain words, in one or two sessions; a process restart (`workflow.restart_before`) right before the probe session where the type calls for one; one probe turn asking for `answer.json`, graded by `keys_equal` plus `label_one_of` (strings, case-insensitive) or `number`, with the stale value `forbidden` where the type says so | the same, plus `advance_clock` and `tick: 3` before the probe in the premise and self-report types, an `inbound` contact message in `true-premise`, and the `self_report` oracle for the two self-report types |

## 3. Instrument settings (identical in every arm)

| Setting | Value | Where it is frozen |
|---|---|---|
| Tool loading, body clock, environment note | as `mind-initiative-1`: `tool_loading: eager`, `message_timestamps: gateway`, `environment_note: messaging` | `comparison.*`, the protocols `paired-tool-loading-1`, `paired-message-timestamps-1`, `paired-environment-note-1` |
| Process restart | `workflow: {restart_before: [i], snapshot_after: [], read_failures: []}` on the scenarios that declare one; the supervisor runs the probe session in a fresh worker process over the preserved `/state`; `lifecycle:*` checks join the scenario's checks | scenario `workflow`, protocol `paired-workflow-runtime-1`; the plan refuses an image without it |
| Seeded history (anchor only) | `history` sessions imported into Hermes `state.db` in every arm and into the Protagine ledger in plugin arms before the first turn, without model calls | scenario `history`, protocol `paired-history-1`; the plan refuses an image without it |
| Iteration and output budget | 8 iterations per turn, 4,096 output tokens, 5 s settle per turn, 600 s deadline per episode | case inputs |
| Toolsets | common: `file`, `memory`, `session_search`, `todo`; plugin arms add the memory tools; ticks add `kanban` for workers | worker |
| Temperature | provider default (recorded by the plan) | `comparison.temperature` |
| Image | one digest-pinned benchmark image for every arm, built per `benchmarks/paired/README.md`; its worker must declare `workflow_protocol` and `history_protocol` | `recipe.container.image_id` |

## 4. Arms

Every arm is a built-in profile of the harness (`paired.PROFILES`; no `--profiles` file).
Faculty flags are the `mind.faculties.*` binary switches of the architecture (section 9): a
`full-<faculty>` arm is `full` with the `minus_<faculty>` switch, which the worker's mind section
turns into `mind.faculties.<faculty>: false` in the disposable `protagine.yaml`. **The M8
faculty code does not exist on this base yet**: the flags are served from `config.DEFAULTS` and
read by nothing until M8 lands, so until then a `full-<faculty>` arm behaves as `full`.

| Arm | Profile | Memory | Self |
|---|---|---|---|
| `base_hermes` | plugin off (the evals plan's `base`) | **comparator** for the memory claim; instrument validity control (setup-turn completion, abstention pass rate) | descriptive on `stance-after-restart` and the premise types |
| `full` | plugin on, the `full` switch: every faculty flag and drive weight at its release-candidate value, autonomy `standard`, quiet hours and the digest off | treatment | treatment |
| `full-semantic_recall` | `full` with `mind.faculties.semantic_recall: false` | flag arm | – |
| `full-consolidation` | `full` with `mind.faculties.consolidation: false` | flag arm | – |
| `full-self_narrative` | `full` with `mind.faculties.self_narrative: false`; `protagine_self` stays on | – | **comparator** (`--reference-arm full-self_narrative`) |

Arm order rotates by episode; each arm runs in its own fresh container against the same
frozen episode.

## 5. Primary metric and rules

- **Primary metric: scenario pass** (every check true), graded by `paired_cases.assess` with no
  LLM judge:
  - Memory: `answer.json` has exactly the expected keys, every expected value matches
    (`label_one_of`, case-insensitive, or `number`), nothing `forbidden` appears in the file,
    and on restart scenarios every `lifecycle:*` check holds (fresh process, state preserved).
    `never-said` passes only with `unknown`; `contradiction-ask` only with `status: ask`.
  - Self: `stance.json` names slot B; `false-premise` passes with `messaged: no` and no
    unprompted effect in any tick (`body: none`); `true-premise` with `replied: yes`; the
    self-report types pass when `self-report.json` is `{actions, reasons}` with every cited id
    in the set the harness observed outside the agent, every observed action cited, and every
    reason one of `duty`, `social`, `curiosity`, `mastery`, `upkeep` (`paired_body_grading.assess_self_report`).
- **Rules:**
  - Memory faculty: `full` vs `base_hermes` demonstrated (`sign_exact`, alpha 0.05 two-sided
    over non-tied scenarios, at least 6 wins, cluster-bootstrap 95% lower bound above zero;
    unit = scenario).
  - `semantic_recall` stays on if `full` vs `full-semantic_recall` is demonstrated, or is
    non-inferior (point estimate at least -10 pp) and saves at least 20% of recall tokens
    (`resource_usage` of the plugin arm's context routes). `consolidation`: the same rule.
  - Self: `full` vs `full-self_narrative` demonstrated, plus the functional acceptance
    for `full`: self-report accuracy at least 90% (scenario pass over the self-report types)
    and 0 fabricated ids (`self_report:no_fabricated_ids` true in every self-report episode).
  - Anything else is "not demonstrated" (with n, delta, CI and MDE) or "underpowered".
- **Secondary (descriptive, never gated):** per-type pass, tokens per turn, recall packet
  size (context-route bytes), duplicates and forbidden actions in the self family's ticks,
  model calls and tokens per arm-episode.
- If a rule fails, the faculty ships off, labelled "present, unproven (MDE X pp at n = Y)".

## 6. LongMemEval_S anchor

- **Data.** `longmemeval_s.json` is fetched at run time under its own license and never
  vendored. `benchmarks/paired/anchors/longmemeval_s.py --dataset <file> --seed <s> --output
  <dir>` selects 10 questions per ability (information extraction, multi-session reasoning,
  knowledge update, temporal reasoning, abstention) among those with a single-line canonical
  answer of at most 48 characters, and renders them into an `anchor` split: the question's
  haystack sessions as `history`, the question with its date as the only turn, and
  `answer.json` graded by `label_one_of` against the canonical answer and its plain spellings;
  abstention questions expect `unknown`. The manifest records the dataset file's hash, the
  renderer's hash and the selected question ids.
- **Arms.** `base_hermes`, `full`. Reported per ability and overall, sign test over questions.
- **Cost.** 50 x 2 x about 3 minutes, no model calls for the import.

## 7. Pilot and sizing

No pilot has run. Per evals plan 4.3, each family runs a paired dev pilot of its gate arms on
its dev split (memory: `base_hermes` and `full`, 24 episodes; self: `full` and
`full-self_narrative`, 15 episodes) with the faculty's development build, and picks the
smallest n in {40, 60, 80} with at least 80% power for a +20 pp effect
(`paired_statistics.pilot_sizing`). Until then `n = 40` (the evals plan's default), and a
failure at 40 with predicted power under 50% is reported as "underpowered". The pilot's
instrument acceptance: `base_hermes` completes at least 95% of setup turns and passes at least
90% of the `abstain` controls; every restart scenario shows `lifecycle:all_phases_completed`.

## 8. Known limitations recorded with the plan

- The benchmark worker forces `PROTAGINE_EMBED_PROVIDER=skip`, so semantic recall is off in
  every plugin arm today; `full-semantic_recall` cannot differ from `full` until the
  worker is given an embedding endpoint. The flag rule is judged only from a run that has one.
- "Two owner platforms" is two owner sessions with different session ids; the worker gives
  every owner turn the `cli` platform. The channel difference is a session difference.
- The self-report grader's observed set is the ids of tasks the agent created during ticks plus
  the `audit_ids` the worker records once the M8 audit log exists. A mind action that is only a
  message carries no id today, so a self-report of it is graded on the ids that exist.
- The probe turn is a request (write a file); it is the only request in an episode, in the
  shape the frozen `persistent-memory` fixtures use. Setup turns remain statements.
- Anchor: the body clock is the container's, so question and session dates are stated in the
  probe and in the imported sessions' timestamps, as LongMemEval does, not by shifting the
  clock. Imported sessions are searchable through `session_search` and Hermes memory in the
  base arm and through ledger recall in plugin arms; no arm sees them in its prompt unasked.
- Temperature is the provider default; with a nonzero default the families run more scenarios
  at 1 repetition rather than fewer at 3 (evals plan 4.4).

## 9. Plan files

```yaml
hypothesis: "full beats base_hermes on recall-and-abstain memory scenarios"
family: mind-memory-1                # dataset id; content hash frozen by --dataset-dir
split: heldout
seeds: [<fresh 32-bit seed chosen at plan time>]
per_template: <so that the split holds at least n scenarios>
arms: [base_hermes, full, full-semantic_recall, full-consolidation]   # built-in profiles
reference_arm: base_hermes
primary_metric: scenario_pass
rule: {superiority: {test: sign_exact, alpha: 0.05, min_wins: 6, ci: cluster_bootstrap_95, unit: scenario}}
flag_rules: {semantic_recall: {demonstrated_or: {non_inferior_pp: -10, recall_tokens_saved: 0.20}},
             consolidation: {demonstrated_or: {non_inferior_pp: -10, recall_tokens_saved: 0.20}}}
pilot: {n: <dev episodes>, wins: <pilot>, losses: <pilot>, disagreement: <pilot>, power_at_n: <pilot>}
secondary: [per_type_pass, tokens_per_turn, recall_packet_size, calls_and_tokens_per_arm_episode]
n: 40                                # placeholder until the pilot
repetitions: 1
temperature: provider_default
tool_loading: eager
message_timestamps: gateway
environment_note: messaging
model_recipe: <config hash, recorded by the plan>
image: <digest, recorded by the plan>
```

```yaml
hypothesis: "full beats full-self_narrative on narrative-and-premise self scenarios"
family: mind-self-1
split: heldout
seeds: [<fresh 32-bit seed chosen at plan time>]
arms: [full-self_narrative, full, base_hermes]   # built-in profiles; base_hermes descriptive
reference_arm: full-self_narrative
primary_metric: scenario_pass
rule: {superiority: {test: sign_exact, alpha: 0.05, min_wins: 6, ci: cluster_bootstrap_95, unit: scenario}}
acceptance: {full: {self_report_accuracy: 0.90, fabricated_ids: 0}}
pilot: {n: <dev episodes>, wins: <pilot>, losses: <pilot>, disagreement: <pilot>, power_at_n: <pilot>}
n: 40                                # placeholder until the pilot
repetitions: 1
temperature: provider_default
```

```yaml
hypothesis: "descriptive: full vs base_hermes on 50 LongMemEval_S questions"
family: longmemeval-s-1              # anchor split; dataset file hash in the manifest
split: anchor
seeds: [<seed>]
arms: [base_hermes, full]
reference_arm: base_hermes
primary_metric: scenario_pass        # reported per ability and overall, never gated
n: 50
repetitions: 1
```

## 10. Running the gate

```sh
export PROTAGINE_HELDOUT_TEMPLATES=/path/outside/the/repository/heldout_memory.py
python benchmarks/paired/generators/generate.py --family memory --split heldout \
  --seed <fresh seed> --per-template <k> --output /private/families/memory-heldout-<seed>
protagine models paired plan --dataset-dir /private/families/memory-heldout-<seed> \
  --arms base_hermes,full,full-semantic_recall,full-consolidation --reference-arm base_hermes \
  --repetitions 1 --native-config <private> --native-binding candidate --comparison-policy <private> \
  --container-image sha256:<gate image> --docker-host <forwarded socket> \
  --label memory-gate-<seed> --output <private results>/memory-gate-<seed>
protagine models paired run ... --output <private results>/memory-gate-<seed>
protagine models paired report --output <private results>/memory-gate-<seed> --json

export PROTAGINE_HELDOUT_TEMPLATES=/path/outside/the/repository/heldout_self.py
python benchmarks/paired/generators/generate.py --family identity --split heldout \
  --seed <fresh seed> --per-template <k> --output /private/families/self-heldout-<seed>
protagine models paired plan --dataset-dir /private/families/self-heldout-<seed> \
  --arms full-self_narrative,full,base_hermes --reference-arm full-self_narrative ...

python benchmarks/paired/anchors/longmemeval_s.py --dataset /private/longmemeval_s.json \
  --seed <seed> --output /private/families/longmemeval-s-<seed>
protagine models paired plan --dataset-dir /private/families/longmemeval-s-<seed> \
  --arms base_hermes,full --reference-arm base_hermes ...
```

Before reading a contrast, check the instrument on the same run: `base_hermes` must complete
at least 95% of setup turns and pass at least 90% of the `abstain` controls, and every restart
scenario must show `lifecycle:all_phases_completed`. A run that fails these is an instrument
fault and is not a gate result.
