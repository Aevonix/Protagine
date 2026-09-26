# Durable appraisals

This pack measures existing Protagine state transitions, then reads or corrects the resulting state through a fresh native Hermes turn. It contains two public development scenarios and accepts an operator-supplied held-out pack of up to eight appraisal scenarios. Held-out prompts and checks remain private until the comparison round ends.

The development scenarios cover an incident being resolved and contact separation. A held-out pack may also use the appraisal-withdrawal check. The agent's opinions are no longer measured here: the `mind-opinions-1` paired family and its ablation arm measure them (see `docs/proto-agi/families/mind-opinions-1.md`).

## What is exercised

The consumer imports attributed synthetic conversation sources into the actual canonical ledger and runs `AppraisalStore.process_one` over them. The benchmark creates no appraisal record itself.

Each native turn uses the installed Protagine memory provider, authenticated host routes and native transport scope. The native worker has no file, terminal, network or production channel tools. The evaluator reopens the durable stores after worker shutdown and checks actual effects.

The candidate owns `source_appraisal` and `source_appraisal_revision`. The native reader is a fixed supporting role. Producer observations retain requested binding and returned model identity. Native answer quality is not attributed to the thinking candidate. These observations do not prove a server's weight files.

Source capture is a canonical import, not a native writer test. The learning pack separately exercises ordinary native capture. Appraisals use current timestamps so expiration cannot accidentally make a recall test pass.

## Limits that stay visible

Appraisal context uses `_canonical_person_allowed` at `host.py:2303`. The appraisal-withdrawal check requires the active appraisal to reach the first physical native request automatically, before any inspection tool result. Repair checks require the settled record to stop entering context. Contact-isolation checks inspect physical requests, not the assistant's promise to keep a record private.

These are checks of durable mechanisms and source dependencies. They do not establish that a frustration is proportionate or that tone feels natural. Freeform tone and stance quality remain `human_rubric_ungraded`. No inferred Big Five diagnosis, numeric emotion calibration or subjective experience claim is made. Contact trust tiers must remain unchanged throughout these cases.

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

Keep the native reader's binding separate from the candidate recipe binding. Shard development and held-out runs to remain within the existing 3,600-second suite budget.

Use an isolated installed Hermes environment and an output path with private owned ancestry. No production state or credentials belong in fixture files. Export findings through the existing allowlisted public exporter; source bodies, raw responses, private paths and held-out checks stay private.

The harness validates durable effects, not model quality. No product code or policy implementation is replaced by these fixtures.
