# Expanded native learning inventory

The inventory contains twelve independent scenarios, each with Protagine,
base-Hermes and recall-disabled arms. Thirty-six arm records are twelve
scenarios. Eight are public development scenarios; four private fixture bodies
and their oracles remain outside the repository until the round is finalized.

| Scenario | What it measures |
| --- | --- |
| Intake procedure | Apply a retained rule to a new example |
| Exception precedence | Apply an exception rather than the default |
| Computed transfer | Calculate an answer absent from the retained lesson |
| Negative transfer | Avoid applying a rule outside its declared domain |
| Approved versus quoted advice | Keep an approved procedure despite quoted bad advice |
| Irrelevant feedback | Avoid injecting an unrelated procedure into recall |
| Missing prerequisite | Preserve uncertainty when a required observation is missing |
| Delayed fresh session | Recall after closing the training host and a measured short delay |
| Private corrected procedure | Use the current rule after two ordinary feedback turns |
| Private unresolved conflict | Avoid choosing between conflicting unranked instructions |
| Private dependency transfer | Apply retained dependencies to a partially completed task |
| Private processor swap | Carry a procedure between distinct configured native processors |

All scenarios use the same ordinary native writer, canonical ledger and source
projection worker. There is no second learning engine, special answer injection
or direct lesson insertion. The training agent and its authenticated host close
before a fresh host and reader agent start. Sources persist in the same owned
SQLite state. The process remains alive; this is not a crash-recovery experiment.
The delay scenario waits a measured second. It demonstrates the declared short
delay only, not days of retention or performance under background load.

The approved-versus-quoted scenario retains useful policy and treats contrary
quoted advice as untrusted. It does not establish resistance to all malicious
feedback. Irrelevant feedback can be worth retaining for its own domain while
still being inappropriate to inject for the transfer question. That distinction
is separately checked.

## Execution and attribution

The common qualification runner remains the only scheduler and artifact store.
Use `native_learning_extended.cases()` for the eight development scenarios. Add
`cases(private_pack_path, include_initial=False)` for the four private ones.
Register `native_learning_extended.CONSUMERS` and `EVALUATORS`; these preserve the
initial cases and evaluator. Freeze `recipe_metadata(suite, training_recipe=...)`
alongside the ordinary native/supporting recipes before model calls. Preserve
private fixture classification when exporting. Shard by the runner's existing
one-hour envelope; do not launch all thirty-six maximum deadlines as one run.

Processor-swap cases require `recipe['declared']['distinct_learning_processors']`
to be true and an explicitly configured training runtime:

```python
LearningRouter(
    supporting_router,
    native_context(reader_config, reader_recipe),
    supporting_config,
    training_native=native_context(training_config, training_recipe),
)
```

The two declared model identities must differ. The worker physically routes
baseline/feedback to the training endpoint, closes its agent and host, then
routes fresh recall to the reader endpoint. Physical request bodies and returned
JSON/SSE model identities are recorded separately for each phase. The check
requires distinct returned model identities, not merely different labels in the
prompt or two aliases for the same configured model. Missing swap capability is
`unsupported` before any worker or model call. The operator must still freeze
and verify the actual served weight artifacts; provider-reported model names
are not cryptographic proof of weights.

The reader is the primary candidate. Training is a fixed supporting processor
in swap cases, and extraction/review retain their separately observed supporting
roles. Primary attribution uses actual transfer requests. Training requests stay
visible as a separate observation role. Keep the training/supporting recipes
fixed across candidate comparisons to avoid crediting their changes to the reader.

## Strict results and semantic diagnostics

Strict requested JSON remains an independent requirement. Raw grades are never
rewritten when a plain sentence contains the right answer. The versioned
`metrics(result, case.oracle)` diagnostic accepts exact JSON, fenced exact JSON,
literal scalar answers and a small frozen collection of ordinary answer forms.
It rejects explicit wrong JSON and leaves unrecognized prose ungraded. It does
not accept contradictory alternatives, negated answers or added effect claims
just because the correct word appears somewhere.

The diagnostic works on the initial four cases as well, so earlier immutable
results can receive a new derived view without rerunning inference. Preserve its
version and report ungraded answers separately from correct or incorrect ones.
Do not count an abstaining control as task success when the correct goal answer
requires the retained procedure. Report matched-arm goal completion, formation,
source visibility, scope errors and formatting separately.

Controlled tests cover the four new development scenarios and a genuine endpoint
handoff between two local HTTP fixtures with different returned model identities.
Only inference is substituted. Native agents, writer receipts, extraction/review
consumer calls, persistent sources, host teardown and automatic recall remain
real. These controlled checks validate the harness and are not model results.
