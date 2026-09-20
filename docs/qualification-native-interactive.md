# Native task transitions and interactive conversations

This pack measures retained work through the existing Hermes gateway and
Protagine task tool. It does not provide a second task engine. Model answers,
canonical task state and serialized provider-request observations are graded
separately. A correct final answer with a missing, duplicated or wrongly owned
task fails.

## Cases

`native_interactive_cases.cases()` returns eleven development scenarios:

| ID | Mechanism |
| --- | --- |
| U04 | Read a completed task's retained result from a fresh conversation |
| U05 | Keep two independently submitted native roots and sources distinct |
| U06 | Cancel one root while its sibling finishes |
| U07 | Reject resumption against an obsolete native generation |
| U08 | Resume a genuinely failed task in its existing native session |
| X02 | Answer a useful foreground question while two workers are pending |
| X03 | Apply two successive corrections in their actual order |
| X04 | Correct one root without changing its sibling |
| X05 | Preserve background work after a terminal foreground handoff |
| X06 | Observe completion that occurs during the next conversation |
| X07 | Steer work across verified SMS and WhatsApp participant contexts |

An explicitly supplied private pack adds nine heldout scenarios. Their bodies,
expected values and manifest remain outside public git until the evaluation
round is closed. Different model arms do not count as additional scenarios.

## Execution boundary

The worker starts an owned authenticated API, canonical source ledger,
execution/commitment/initiative stores and a real local Hermes gateway. Task
submission, inspection, steering, stop, resume and handoff go through the
installed native plugin and adapter. No task IDs, acceptance receipts or
retained results are fabricated by the worker.

Each fixture has one or two synthetic inventory tasks. Rendezvous gates hold
their provider requests while independent conversations act on them. Declared
fault scenarios affect only the owned subprocess: a provider read timeout,
reply-retention write failure, delayed native admission, canonical source
erasure or restart of this fixture's gateway. The candidate still calls the
real tools. Faults do not affect a shared inference endpoint or live agent.

SMS/WhatsApp fixtures enroll actual verified contact handles, set native session
context and use the real resolver. They exercise identity and task boundaries,
not either external messaging service. Tasks have no filesystem, shell,
network-browsing or deployment tools available.

Private receipts retain task/turn/session identities, source hashes, owner
checks, updates, terminal state, bounded final text and actual tool receipts.
Provider observations retain hashes, byte counts, returned model identity and
matched task/correction markers. Repeated full context is discarded from the
result to keep evidence bounded. The ordinary runner owns immutable attempts
and cleanup. Incomplete turns preserve evidence but cannot earn a pass.

## Calling the pack

Use the existing qualification `evaluate` runner, `native.configuration` and
`native_context` with `native_interactive.CONSUMERS` and `EVALUATORS`. Put
`native_interactive.implementation_identity()` in the immutable recipe. The
suite version is `native-interactive-transitions-v1`. The configured candidate
is used by both foreground and native worker sessions.

Controlled integration checks require an isolated Hermes interpreter:

```sh
PROTAGINE_TEST_HERMES_PYTHON=/path/to/isolated/python \
PYTHONPATH=sidecar python -m pytest -q \
  sidecar/tests/test_qualification_native_interactive.py
```

`PROTAGINE_INTERACTIVE_HELDOUT` optionally supplies a private JSON fixture to the
controlled integration checks. Their local HTTP responder supplies only model
messages. Native tools, state transitions and grading are unchanged. This is
harness validation, never candidate performance evidence.

## Limits and known integration risks

The task list deliberately reports a bounded retained inventory, with
`complete_running_inventory: false`; this suite cannot establish a complete
global inventory of every agent activity. Background task ownership is tested,
but native delegated child-agent inheritance needs its own boundary test.

The rendezvous does not measure latency under GPU contention, physical channel
delivery, audio interruptions, long-history compression or large worker pools.
A failed result-retention fixture distinguishes computation from delivery; it
does not establish automatic durable redelivery. Isolated gateway restart does
not establish recovery of a production machine.

The controlled two-correction case exposed an existing native integration
failure: both updates are retained and acknowledged, then the resumed task
reports `source_update_ownership_unavailable` before receiving the correction.
The strict benchmark result remains a failure. Its integration test recognizes
only that exact terminal reason and failed-effect combination as an expected
failure; unrelated failures still fail the test. Candidate ability cannot be
inferred where this integration prevents the task from receiving its input.

Native Protagine projection can add a system message after the user message.
An inference template that requires all system messages first may reject this
before reasoning begins. Preserve that outcome as an integration failure;
changing the template or prompt order starts a different recipe. Likewise,
retrieval cutoffs are supporting-system configuration, not measured model
ability. Neither is repaired silently during an evaluation run.
