# Durable opinions and appraisals

This pack measures existing Protagine state transitions, then reads or corrects the resulting state through a fresh native Hermes turn. It contains four public development scenarios and accepts an operator-supplied four-scenario held-out pack. Held-out prompts and checks remain private until the comparison round ends.

The development scenarios cover an evidence-backed opinion surviving flattery, revision after contrary evidence, an owner withdrawing a view, and the existing update-rate limit. Other supported checks cover an incident being resolved, contact separation, appraisal withdrawal and reading a stored view with a different processor.

## What is exercised

The consumer imports attributed synthetic conversation sources into the actual canonical ledger. Judgment cases then run the existing source extraction and review consumer before `SelfJudgments.process_one`. Appraisal cases run `AppraisalStore.process_one` over those sources. The benchmark creates no judgment or appraisal record itself.

Each native turn uses the installed Protagine memory provider, authenticated host routes and native transport scope. Opinion inspection and corrections use the real `protagine_judgments` tool, including Hermes tool discovery when enabled. The native worker has no file, terminal, network or production channel tools. Owner control calls retain their exact native instruction and correction identity. The evaluator reopens the durable stores after worker shutdown and checks actual effects.

The candidate owns `self_judgment`, `source_appraisal` and `source_appraisal_revision`. Source extraction, admission review and the native reader are fixed supporting roles. Producer observations retain requested binding and returned model identity. Native answer quality is not attributed to the thinking candidate. A distinct-processor case requires both an explicit capability declaration and different observed returned identities; differing configuration labels alone do not pass it. These observations do not prove a server's weight files.

Source capture is a canonical import, not a native writer test. The learning pack separately exercises ordinary native capture. The opinion update interval is tested with the reducers' existing injected clock while leaving the production default unchanged. This measures the interval logic, not elapsed day-long behavior. Appraisals use current timestamps so expiration cannot accidentally make a recall test pass.

## Limits that stay visible

Working opinions have a native automatic-context gap in this profile. `host.py:2041` excludes canonical-only profiles from `_exact_person_allowed`; the working-opinion projection is inside that gate at `host.py:2395`. This pack binds the real perspective but does not bypass that condition. Its explicit inspections are not evidence of automatic opinion governance. Metadata therefore records `automatic_opinion_projection: unsupported_canonical_only_host`.

Appraisal context uses `_canonical_person_allowed` at `host.py:2303`. The appraisal-withdrawal check requires the active appraisal to reach the first physical native request automatically, before any inspection tool result. Repair checks require the settled record to stop entering context. Contact-isolation checks inspect physical requests, not the assistant's promise to keep a record private.

These are checks of durable mechanisms and source dependencies. They do not establish that every opinion is wise, that a frustration is proportionate or that tone feels natural. Freeform tone and stance quality remain `human_rubric_ungraded`. No inferred Big Five diagnosis, numeric emotion calibration or subjective experience claim is made. Contact trust tiers must remain unchanged throughout these cases.

## Campaign API

```python
from protagine.qualification.native import configuration, native_context
from protagine.qualification.native_perspective import (
    MemoryRouter, CONSUMERS, EVALUATORS, recipe_metadata,
)
from protagine.qualification.native_perspective_cases import cases
from protagine.qualification.runner import evaluate, router_for

suite = cases()  # Or cases(private_heldout_path).
reader_config, reader_recipe = configuration(
    reader_profile_path, reader_binding, hermes_python=native_python)
recipe = {**candidate_recipe, **recipe_metadata(suite),
          "supporting_reader": reader_recipe}

def router(case):
    return MemoryRouter(router_for(candidate_config, candidate_binding, [case]),
                        native_context(reader_config, reader_recipe))

# Pass recipe, suite, CONSUMERS, EVALUATORS and router to evaluate.
```

Keep the native reader's binding separate from the candidate recipe binding. Set `recipe["declared"]["distinct_identity_processors"] = True` only for a deliberately configured distinct reader. Otherwise that case is unsupported without making a request. Shard the eight cases into development and held-out runs to remain within the existing 3,600-second suite budget.

Use an isolated installed Hermes environment and an output path with private owned ancestry. No production state or credentials belong in fixture files. Export findings through the existing allowlisted public exporter; source bodies, raw responses, private paths and held-out checks stay private.

`tests/hermes_adapter/test_native_perspective_qualification.py` drives controlled local HTTP completions through the real reducers, provider, native tools and durable stores. It validates the harness, not model quality. No product code or policy implementation is replaced by these fixtures.
