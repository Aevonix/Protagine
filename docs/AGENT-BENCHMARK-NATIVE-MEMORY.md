# Native memory development pack

This pack measures source formation and later recollection through the installed
Protagine memory provider, Hermes CLI loop, authenticated host routes and canonical
SQLite ledger. Source text stays in the isolated ledger. The evaluator supplies
only the final question to Hermes; it does not select or inject recalled answers.

The twelve scenarios cover a fresh-session fact, correction, independent facts,
fiction and quotation attribution, a conditional preference, unknown information,
contact isolation, forgetting, selective forgetting, distractors and intervening
chatter. Correction cases require the old claim to be retired, not only a correct
final answer. Forgetting checks canonical erasure and claim removal, followed by
absence from the actual outbound model request. Controlled tests run every scenario
through the same native process with a local model substitute.

## Boundaries

Historical inputs enter the real ledger's source API and use the ordinary
extractor/reviewer. The reader runs in a fresh actual Hermes session. These are
recorded prior-session fixtures, not physical WhatsApp/SMS deliveries or a test of
the native chat turn writer. Forgetting is an explicit fixture operation on the
real ledger, not a natural-language tool-dispatch test. Embeddings, reranking,
external stores, compression and erasure of existing session histories are not
covered. Fiction can remain source history even when it must not become an owner
fact. All committed scenarios are development fixtures, not sealed holdouts.

Extraction and review use the supplied supporting configuration unchanged. Hold
that configuration constant when comparing reader models; changing the writer and
reader together cannot attribute improvement to the reader. Supporting calls have
separate observations. A failed extraction can fail the end-to-end case even when
the reader answers correctly. Inspect individual checks before attributing fault.

Request visibility is observed from serialized HTTP requests after native Relay
and ordinary middleware filtering. The observer does not alter request or response
bytes. It records actual returned model IDs from JSON/SSE responses. Primary
binding credit requires that evidence; configured labels alone earn no credit.
A returned model ID does not establish checkpoint or weight identity.

## Running

Use a private directory and an isolated interpreter containing the qualified
Hermes version, `protagine-hermes`, `protagine`, FastAPI and Uvicorn. A new native
home, source ledger and API are created for each case and closed before the runner
removes the owned state directory. The standard Hermes `state.db` path is required
for native source ownership.

Planning reads configuration and installed code only. It makes no model request
and does not deploy or rebind anything. Both commands must use the same arguments
and installed implementation.

```bash
python -m protagine.qualification.native_memory_batch plan \
  --support-config /private/benchmark/support.json \
  --native-config /private/benchmark/hermes.yaml \
  --native-binding candidate \
  --hermes-python /private/benchmark/hermes-venv/bin/python \
  --output /private/benchmark/memory-candidate \
  --label candidate-memory

python -m protagine.qualification.native_memory_batch run \
  --support-config /private/benchmark/support.json \
  --native-config /private/benchmark/hermes.yaml \
  --native-binding candidate \
  --hermes-python /private/benchmark/hermes-venv/bin/python \
  --output /private/benchmark/memory-candidate \
  --label candidate-memory
```

`--case-ids` selects installed IDs, `--native-seconds` bounds the native reading
phase, and `--case-seconds` bounds the whole case, including formation. Defaults
are 120 and 600 seconds; these are maximum budgets, not expected durations.
`--resume` retains completed attempts. `--evidence-mode controlled` labels runs
using controlled model substitutes so they cannot be presented as inference results.

The immutable `benchmark.json` uses the existing qualification-batch contract.
Child runs under `runs/native-memory-NNN` use the existing `run.json`, per-attempt
`result.json` and report contracts. Native and controller package bytes, dependency
versions, Hermes identity, fixture/oracle hashes and supporting configuration are
frozen. Store private raw request evidence privately; public reporting must use the
existing allowlisted exporter. Do not publish raw prompts, memory contents,
endpoint addresses, local paths or credentials.

The reusable surface is `native_memory_cases.CASES`,
`native_memory.CONSUMERS/EVALUATORS`, and `native_memory_batch.prepare/execute`.
There is no new benchmark service or separate memory implementation.
