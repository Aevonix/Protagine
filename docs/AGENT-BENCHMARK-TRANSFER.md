# Native procedural transfer experiment

Four public development scenarios exercise routing, exception precedence,
calculation from a learned procedure and avoiding transfer outside a procedure's
scope. Each has three matched arms: Hermes with Protagine, base Hermes without
Protagine persistence, and Protagine with automatic recollection disabled for the
transfer session. Twelve arm records are four independent scenarios, not twelve
independent learning problems. They are not sealed holdouts.

Every arm receives the same baseline question and ordinary feedback message in a
real native session. The training session closes before the transfer question is
asked in a newly constructed session. Both Protagine arms use the actual native
turn writer and existing source extraction/review worker. The evaluator does not
write a lesson into the ledger, assemble a recalled answer, or inject feedback
into the transfer prompt. The base arm receives the same conversation but has no
Protagine writer/provider. The recall-disabled arm keeps formation and storage;
its transfer agent skips the memory provider.

The lessons describe invented local procedures absent from model pretraining.
Training and transfer use different examples. The calculated-transfer answer is
not stated in the feedback, and the negative-transfer scenario concerns a device
outside the learned procedure's scope. This measures procedural feedback transfer
and retention. It does not establish autonomous discovery, recursive code
improvement, weight learning, long-term durability, or transfer between different
reader models. Native channels and embeddings are not exercised here.

## Evidence and comparisons

Checks distinguish baseline uncertainty, correct transfer, a fresh session,
native feedback receipt, completed source formation, a useful retained procedure,
source visibility in the actual outbound request and cleanup. The response model
is observed from the physical JSON/SSE stream. Extraction and review remain fixed
supporting roles with their own observations.

An arm can correctly abstain when it lacks the procedure. That is a valid control
outcome, not a successfully completed routing/calculation task. The separate
`transfer_metrics` function reports baseline and transfer goal completion so an
all-passing control does not hide Protagine's contribution. Negative transfer
expects uncertainty in every arm and measures a regression constraint. Preserve
the strict JSON-format result separately from any later semantic scorer view.

Compare the same transfer task across arms; do not infer intelligence improvement
from a baseline/transfer difficulty difference. Freeze candidate/supporting
configuration and scenario order before inference. The current four scenarios
are too small to support broad learning claims or close-model rankings. Additional
sealed variants, order counterbalancing and processor swaps belong to the larger
experiment, not to this initial coverage claim.

## Integration

The pack uses the existing qualification runner and ordinary artifacts. Register
`native_learning.CONSUMERS` and `native_learning.EVALUATORS`, select
`native_learning_cases.CASES`, and construct each router with:

```python
LearningRouter(supporting_router, native_context(native_config, recipe), support_config)
```

The support configuration is written only inside the private owned attempt and
removed during cleanup. Never include it as a case input, public artifact or log.
Freeze package bytes and dependencies as for the native memory pack and run its
output-ancestry preflight before starting. `native_learning_worker.py` owns both
agents and the temporary authenticated API; interruption targets its current
agent and cleanup must be observed before another case proceeds.

The reusable metrics call is `transfer_metrics(result, case.oracle)`. Do not infer
learning from the arm's aggregate pass rate alone. Preserve all attempted records,
including harness-invalid attempts, and use new derived scorer versions for
grading corrections rather than overwriting raw results.
