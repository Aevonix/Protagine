# Agent screening benchmark

The first executable pack is a 24-scenario **development screen**, not the planned broad agent benchmark. It reuses the model qualification runner and the actual consumers it already observes. A passing screen does not establish persistent-agent quality, privacy enforcement, learning, or performance under load.

The screen contains 16 router completion cases (including three image cases), two real memory formation/recollection cases, and six isolated native Hermes cases. Five native cases require actual complete file-tool reads and check that files remain unchanged. The sixth checks a grounded conversation answer. JSON validity and factual/effect checks remain separate; unsupported modalities and missing runtime capabilities stay visible.

New scenarios cover bounded corrections, unresolved source conflicts, accepted versus delivered work, fiction attribution, conditional promises, claimed authority, resource dependencies, inventory reconciliation, malicious document instructions, and source-level contract defects. They do not pretend to execute a scheduler, enforce live authority, run code, or exercise cross-channel memory. The batch manifest explicitly lists missing coverage.

## Run a screen

Freeze private configuration and budgets before model calls. Plan reads local configurations and inspects the selected installed Hermes payload; it does not query inference endpoints or alter production settings.

```sh
protagine models benchmark plan \
  --config /private/host-models.json --binding candidate \
  --native-config /private/hermes.yaml --native-binding candidate \
  --hermes-python /path/to/hermes/.venv/bin/python \
  --native-deadline-seconds 120 --label candidate-recipe-01 \
  --output /private/results/candidate-screen-01

protagine models benchmark run \
  --config /private/host-models.json --native-config /private/hermes.yaml \
  --hermes-python /path/to/hermes/.venv/bin/python \
  --output /private/results/candidate-screen-01

protagine models benchmark inspect --output /private/results/candidate-screen-01
```

The host configuration declares the candidate and supporting role bindings. Candidate extraction is redirected only for the case's named task. The semantic admission reviewer retains its configured binding. Supporting calls and actual selected bindings remain in ordinary attempt records. Disable unrelated automatic model promotion outside this suite when running a controlled comparison.

Use `--roles chat,reasoning` and/or `--boundaries role_completion,cognition_consumer` to declare a smaller subset. A subset is reported with its actual denominator and never called a completed 24-case screen. Native cases require an explicit provider configuration. A missing Hermes installation is recorded as a setup error when evaluated. Text-only candidates retain unsupported image results; do not infer that they are fit for vision from an HTTP success.

Direct router cases use the predeclared configured role allowances. Memory cases retain their own 240/180-second budgets. Native cases share the explicit elapsed deadline and cleanup allowance. The batch partitions runs by boundary and accumulated deadlines, retaining the existing 128-case, 3,600-second summed run and 600-second case ceilings. Long-context capacity/load sweeps are separate work, not an excuse to increase these deadlines after observing a failure.

## Explicit structured output

The default `prompt_only` protocol asks for JSON in the prompt. To test the same
tasks using the serving endpoint's JSON-schema contract, plan a separate run:

```sh
protagine models benchmark plan \
  --config /private/host-models.json --binding candidate \
  --boundaries role_completion,cognition_consumer \
  --output-contract json_schema --label candidate-structured-01 \
  --output /private/results/candidate-structured-01

protagine models benchmark run \
  --config /private/host-models.json \
  --output /private/results/candidate-structured-01
```

This selects all 18 standard cases: 16 direct tasks with explicit, model-neutral
schemas and the same two real memory cases. Messages, semantic oracles and
configured budgets remain unchanged. Direct passes require observed schema
transport in the actual HTTP request, valid schema output, and correct facts.
There is no model-specific tuning or JSON repair. Native Hermes cases are
unaffected. Publish `agent-screen-2-structured-output` as its own cohort, retain
the original prompt-only grades, and rerun the complete selection rather than
only previous failures. See [output-contract details](qualification-screen-output-contracts.md).

## Artifacts and repeatability

`benchmark.json` has `schema: 1`, `kind: qualification_batch`, the selected `suite_version` and output contract, cases and hashes, frozen shard recipes, implementation identity and honest coverage. Its SHA-256 covers every other field. It contains no connection credentials; it remains a private artifact because selected runtime information can include private paths.

Each `runs/standard-NNN` or `runs/native-NNN` directory is an ordinary model qualification run, containing `run.json`, immutable per-case attempts and numbered JSON/Markdown reports. Existing report consumers can read these unchanged. Batch `report-NNN.json` files have `kind: qualification_batch_report` and derive counts from those child records, including untouched cases. Public publication must explicitly select safe fields; do not publish the batch directory wholesale.

An execution refuses a changed recipe, fixture, scorer or implementation. `--resume` preserves all completed attempts and uses existing interruption rules for started work. It never retries a failed case automatically. Repetitions use new batch directories with the same frozen configuration. A native result without actual observed primary-provider attribution remains unverified even if its answer and file reads pass.

Additional implemented consumers are available through the [portable behavioral packs](QUALIFICATION-PACKS.md), with explicit native, supporting, retrieval and sandbox resources. The screen's own coverage remains unchanged. Physical speech and load coverage must still be measured separately. Keep held-out cases separate from this public development screen and freeze candidate settings before opening them.
