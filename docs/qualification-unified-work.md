# Native shared work qualification

Four development scenarios exercise the installed Protagine adapter and real Hermes task gateway:

1. One conversation accepts background work. A second sees the same task and provenance-backed commitment through ordinary context assembly.
2. A foreground status question finishes while the existing background task remains in flight, without submitting duplicate work.
3. A correction from another session uses the native task tool. The update must persist, reach a worker model request, and affect the retained result.
4. A stop request reaches native task control. Cancellation must finalize, leave no retained completion, and suppress a repeated dispatch of the cancelled task.

The fixture starts only the in-process `protagine_task` platform. It uses a fresh Hermes home, native session database, authenticated loopback host API, source ledger, commitment store and native handoff database. It never connects production channels or profiles. The model must use the actual native discovery and task tools. No fake task engine or worker acknowledgment stands in for execution.

An explicit rendezvous pauses the worker at its provider request boundary until the foreground turn finishes. This creates a repeatable opportunity for status, correction or cancellation without depending on a particular model's speed. It is a controlled scheduling condition, not a measurement of hardware throughput or unconstrained latency. After release, the worker uses the configured model normally.

The observer records serialized physical provider requests after ordinary middleware. Grading reads the canonical handoff database, update records and retained result separately from the model's answer. A correct-looking answer with no submitted task or no visible correction fails. Successful control acknowledgment alone does not establish model obedience. A cancelled task's repeated native dispatch must not start another model request or retain a reply.

Version 2 retains each request hash and its observed canonical markers in the graded record. This prevents repeated tool schemas from exceeding the evidence limit. A bounded private diagnostic preserves the original native result and log tail; it is never a public artifact. A native turn that exhausts its iteration budget remains incomplete and fails a completion check while retaining its observed task effects. Original version 1 attempts are not rewritten.

Two base-Hermes controls run independent concurrent native sessions without Protagine. The foreground is expected to preserve uncertainty about the other session's work while the worker completes. These controls establish the information available at that boundary. They do not pretend that base Hermes has the Protagine task adapter, and their grounding passes are not equivalent to shared-task functionality passes. There is no meaningful base-Hermes score for a Protagine-specific steer/stop tool.

## Campaign integration

```python
from protagine.qualification.native_unified import CONSUMERS, EVALUATORS, implementation_identity
from protagine.qualification.native_unified_cases import cases
from protagine.qualification.native import configuration, native_context

selected, recipe = configuration(config_path, binding, hermes_python=python)
recipe['native_unified_implementation'] = implementation_identity()
suite = cases()  # Four development scenarios.
base_controls = cases(arm="base_hermes")  # Two separate boundary controls.
# Use the existing qualification.runner.evaluate with native_context(selected, recipe).
```

Run the four-scenario shard and two controls as distinct ordinary qualification runs. Keep inputs, traces, source bodies, local URLs, fixture credentials and hidden future cases private. Public reports should use the existing allowlisted exporter and label the arm and controlled scheduling condition. These development cases do not establish behavior for real messaging transports, voice, arbitrary concurrency or long-lived recovery.

The optional integration test requires `PROTAGINE_TEST_HERMES_PYTHON`. It drives the real native runtime with a controlled loopback HTTP transcript, including Hermes' deferred tool discovery. It makes no model calls and records `evidence_mode=controlled`. Its results validate the harness, not model quality. Actual inference runs use the selected provider through the same path.
