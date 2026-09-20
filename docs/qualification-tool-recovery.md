# Native tool recovery qualification

Six public development tasks and four separately stored held-out tasks cover argument correction, lost acknowledgments, stale revisions, read-only requests, queued work, insufficient stock, malformed replies, transient failures, bounded retries and pending cancellation.

Each task runs a real native Hermes conversation. Its registered synthetic inventory tool executes a fixed helper inside the existing offline Docker sandbox. The helper's directory and event ledger are owned by container root and inaccessible to the agent's unprivileged file and terminal tools. The controller runs only that fixed helper with structured JSON arguments. No generated code runs as container root, and the container receives no Docker socket, host mount, credentials, network or GPU access.

This is a benchmark fixture for an external stateful tool. It adds no product service, task engine or learning system. A toy inventory event ledger provides independently inspectable effects; it is not a production inventory implementation. The fixture's deterministic failures do not measure real network or provider recovery.

Grading requires the completed native turn, accurate final fields, correct inventory and effect count, ordered error/receipt evidence, stable operation identity and bounded calls. A plausible final answer cannot substitute for effects. Lost acknowledgments must be resolved through status without another reservation. Pending acceptance does not count as completion. The final answer permits a single JSON code fence; arbitrary prose is not searched for a passing substring.

Use the existing runner, not another scheduler:

```python
from protagine.qualification.native_recovery import CONSUMERS, EVALUATORS, implementation_identity
from protagine.qualification.recovery_cases import cases
from protagine.qualification.native import configuration, native_context

selected, recipe = configuration(config_path, binding, hermes_python=python)
recipe['recovery_implementation'] = implementation_identity()
suite = cases(sandbox)  # Six public development cases.
# Existing runner.evaluate(..., lambda _: native_context(selected, recipe))
# Held-out fixtures require an explicit private pack path; do not open before freeze.
```

Each case allows 300 seconds, 2048 initial output tokens and ten native iterations. Tool calls retain the real native executor and Hermes guardrails. Original outcomes and retries remain in immutable attempt records. Primary-model attribution remains unverified for this adapter, as for the current coding adapter. Controlled HTTP transcripts validate the harness only and must use `evidence_mode=controlled`.

Container cleanup is confirmed through the existing coding sandbox receipt. Incomplete native turns preserve tool effects but fail completion. Private error diagnostics never enter public exports. The public allowlisted exporter can publish scalar case outcomes; it must not publish raw fixture state, requests, logs or held-out bodies.
