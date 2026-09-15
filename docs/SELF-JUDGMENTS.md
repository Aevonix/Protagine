# Revisable working judgments

Automatic working judgments are experimental and **off by default**. Only the
exact setting `PROTAGINE_SELF_JUDGMENTS_ENABLED=1` enables them. Qualify the configured
reasoning model before opting in; the system does not infer that a new model is
qualified. A bounded evaluation of the default single-model installation still
produced an unsupported cost comparison after source-premise admission checks.
That result does not establish the quality of every other available processor.

While disabled, ordinary turns and typed runtime observations retain their
canonical sources but enqueue no judgment reflection. Existing pending work is
held without consuming attempts, and no judgments enter automatic context.
Memory formation, recall, preferences and appraisals remain active. History,
withdrawal and correction controls remain available; explicit reconsideration
may queue work, which stays held until opt-in. `/v1/host/self` exposes
`judgments_enabled` and a `held` flag on pending processing records. No retained
judgment or source is deleted by this switch.

The following describes the opt-in behavior.

An ordinary attributed owner turn can now produce a durable agent judgment:
a topic, stance, reason, supporting and contrary source references, stated
certainty, predecessor revision and actual model/configuration provenance.
The existing source worker performs this reflection using the configured
`reasoning` role. It owns one in-flight reflection while continuing source
indexing, claim extraction and image captioning; no additional service is needed.

Ordinary reflection waits for the existing source-claim job to complete without
spending a reflection attempt. Only current reviewed `decision`, `procedure` and
`substantive_event` claims supply premises. Personal facts, preferences and
relationships remain in their existing memory and relationship projections.
A question with no qualifying claim does not invoke the judgment model. This
reuses the fallible admission category; it adds no classifier or semantic judge.

Supporting and contrary references bind exact admitted claim IDs. Correction,
retraction, attribution invalidation and erasure are checked again before commit
and automatic context use. A different surviving claim in the same message
cannot replace a corrected premise. Older message-only views remain inspectable
as `unsupported_premise` history, without automatic injection. Explicit owner
reconsideration and typed native execution observations retain their paths.

The model is asked to abstain on transient logistics, copied preferences,
unsupported generalizations and views without lasting use. This is an inference
requirement, not proof that every accepted judgment is good. Output validation
requires a completed final answer and at least one retained current-source
reference. A later reflection receives bounded quotations rehydrated from prior
canonical evidence as well as the prior model's explicitly fallible view.

Each normalized topic can change once per day by default. Operators can set
`PROTAGINE_SELF_JUDGMENT_INTERVAL_SECONDS` to another nonnegative interval. Contrary
evidence received during that interval stays eligible for reconsideration when
it ends. Replaying the same source does not create another vote. Unavailable
inference receives at most three attempts; source bytes, the captured topic head
and the current processing lease are checked again inside the commit transaction.

The current `/v1/host/self` perspective exposes `judgments` and
`judgment_history`. `applies_to=owner_turn_deliberation` describes the implemented
effect: up to two relevant judgments, within 2,400 characters, enter the existing
owner-only working-perspective section during ordinary context assembly. An
unrelated or empty query adds no judgment text. These views do not change owner
preferences, memory truth, tool authority or initiative priority. Existing
automatic legacy opinion weights remain non-governing.

The existing `POST /v1/host/learning/correction` accepts an exact current
`judgment_id`, a stable `correction_id`, and `judgment_action` of `withdraw` or
`reconsider`, alongside its ordinary identity/context/correction fields. It uses
the existing `memory:write` scope and resolves the configured owner. Withdrawal
immediately removes the view from current context and appends an owner control
record; later automatic output for the same normalized topic remains withheld.
This does not promise semantic suppression under arbitrary topic renaming.

Reconsideration requires a retained, attributed owner `source_id`. It keeps the
view withdrawn while the existing worker reasons from that evidence, permits
one reconsideration assignment without the ordinary daily wait, and requires
the same topic. An abstention leaves withdrawal in place. No owner wording is
installed as an agent stance. Repeating a correction ID is idempotent; a stale
target is rejected, and changing the head fences any in-flight reflection.
Owner control records remain visible in judgment history. Source erasure clears
dependent correction text too while preserving value-free withdrawal records.
The latest ten processing records are exposed under `judgment_processing`,
including fixed local validation codes without raw provider responses.

The native `protagine_judgments` tool exposes inspection and these controls during
an ordinary owner conversation. It returns the latest ten views/control records
with truncation flags; its model arguments contain only the operation, exact
judgment ID and (for reconsideration) retained source ID. Identity and the
correction instruction come from the transport-bound owner turn. The instruction
is retained as an excerpt of up to 1,500 characters. A current turn's source can
be selected after normal source capture; the tool does not fabricate a source
to satisfy reconsideration. Scheduled, subagent and internal review turns cannot
perform owner controls. A built-wheel native fixture exercises actual owner
conversation tool dispatch over HTTP, guest/scheduled denial and subsequent
reflection using controlled inference, without adding canonical sources.
The native control turn ID is also an explicitly erasure-only dependency until
normal capture. Forgetting that turn clears its copied instruction and dependent
view prose, including during inference, without reviving a withdrawn view or
treating the event reference as quoted evidence.

The projection and processing records use the canonical source SQLite database.
New attributed owner sources eligible for ordinary claim derivation enqueue
reflection; historical imports and session-scoped transcript checkpoints do not.
Source erasure removes dependent judgment prose and topic text, including
superseded history. Opaque head tombstones prevent an older view from reviving.
Fresh retained evidence can establish a new view later.

With reflection enabled, prospectively classified operational native tasks also
contribute terminal observations through the existing execution registry. Both
completion and failure can be considered. The record retains the attempted
request's source handles, origin, lifecycle, duration and available request
metadata. A completed turn does not establish correct output, useful work,
external effects or owner approval. These facts support limited process
judgments; they do not enqueue relationship appraisals.

Runtime facts remain canonical metadata with empty recall text. The judgment
worker renders them when needed, and a source handle can open the record. They
create no lexical or vector memory chunks. Input corrections invalidate support;
erasure follows the existing source lineage. Replayed observations retain the
first record without another judgment job. The usual update rate and owner
correction controls remain in force.

Ordinary resolved owner-channel task submissions are classified operational.
Trusted in-process admissions can explicitly declare operational or qualification
purpose. Local operator permissions alone do not establish that distinction;
unclassified admissions and old records remain excluded. Purpose is not a
model-facing tool argument. A qualification deliberately admitted through an
identical ordinary owner-channel path is indistinguishable without trusted
classification. Controlled tests must retain that limitation rather than claim
ordinary-use learning. This connection does not prove a later useful opinion or
independently evaluate task output.

### Machine assessments of task artifacts

A host evaluator can explicitly admit an already performed machine review via
`POST /v1/host/executions/assess`. This uses the existing execution API: scoped
`turns:write`, an exact owner person grant, and the same authenticated principal
that recorded the execution. The execution must be terminal and prospectively
classified operational. This operation does not run a review, change task
timestamps or forecasts, or reopen its previous judgment disposition.

The host supplies the exact execution/task/session/turn IDs, original admitted
`input_refs`, current `runtime_source_ref`, and the complete `source_refs` that
were recorded with the task output. It supplies UTF-8 documents as objects with
`name`, `content` and `sha256`: one `artifact`, one `assessment`, and optionally
up to four `context_documents`. The complete rendered evidence must fit the
existing 16,000-character judgment budget; excess evidence is rejected, not
silently clipped. `assessed_at` is the reported timestamp with a timezone;
`reviewer_identity` and `reviewer_model` remain `unknown` when not recorded.

The server verifies the received document hashes and source revisions. The
artifact-to-run association remains the authenticated host's report, not an
independent server-side file check. The source identifies the review as an
unverified machine assessment; owner approval stays unobserved. It cannot grant
permission, establish a global quality grade or change a contact appraisal.

The existing host caller is `protagine_hermes.executions.ExecutionObserver.assess`.
After a real evaluator finishes, it can submit the retained first assessment
using its existing scoped client; no model tool or extra review worker is added:

```python
import json
from protagine_hermes.executions import ExecutionObserver

# Read the exact completed association through the existing task store.
handoff, _ = native_tasks.handoffs.resolve(task_id)
output = handoff["response"]["source_dependencies"]
# runtime_source is the current source-reader receipt for this execution's
# task-execution source. Its typed facts retain the originally registered
# session/turn IDs even if native compression later rotated the session.
facts = json.loads(runtime_source["content"])["messages"][0]["_task_execution_facts"]
payload = {
    "execution_id": facts["execution_id"],
    "task_id": handoff["id"],
    "contact_id": handoff["source"]["contact_id"],
    "session_id": facts["session_id"],
    "turn_id": facts["turn_id"],
    "input_refs": handoff["source"]["input_refs"],
    "runtime_source_ref": {key: runtime_source[key] for key in ("source_id", "source_version")},
    "source_refs": output["source_refs"],
    "artifact": retained_artifact_document,
    "assessment": retained_first_review_document,
    "context_documents": retained_review_context_documents,
    "assessed_at": retained_review_timestamp,
    "reviewer_identity": recorded_reviewer_identity_or_unknown,
    "reviewer_model": recorded_reviewer_model_or_unknown,
}
receipt = ExecutionObserver(scoped_client).assess(payload)
```

This same explicit call supports a finite operator import of a preserved review
and future ordinary host evaluations. It is not called merely because a task
finished: a real assessment must exist, and qualification tasks remain excluded.
Admission errors propagate to the evaluator. Exact replay returns the existing
source; altered metadata under the same execution/artifact/review identity is
rejected. The first review is never regraded or rewritten by this operation.

The existing source ledger retains the complete assessment as attributed
assistant text, so normal source reading and exact source annotations work.
This differs from metadata-only lifecycle telemetry: the assessment is actual
review evidence and receives the ordinary source index. Original input, supplied
context, runtime-source or assessment correction withholds its supported view;
erasure removes the dependent source and judgment through existing lineage.
The normal worker may abstain. Any resulting view states its machine-assessment
basis and unknown owner approval in relevant owner-turn context. Useful behavior
still requires observation in a later actual decision.

Newly attached internal native reviews can also contribute a runtime observation
when the existing review observer reads an ended crash, timeout, spawn failure
or exhausted execution from the owned native ledger. This is prospective: old
bindings and runs are not imported. Normal completion and model-written reports
are not evidence of factual correctness. The source contains lifecycle outcome,
elapsed time, configured deadline and attempt identity, with no report or error
prose. Task model overrides are labeled as configuration at observation time;
the actual serving processor remains unknown. Later observations preserve the
first source unchanged even if task configuration changes.

The same source transaction queues the existing reflection worker without
assertion extraction. It receives explicit runtime attribution and must not turn
one failed attempt into a global competence or personality claim. It may abstain;
that leaves the observation retained without inventing a working view. Any view
uses the existing rate limits, owner controls, source dependencies and erasure
rules. Its only current effect is relevant working-perspective context, for
example guidance when considering smaller bounded steps after a timeout. It does
not automatically change tools, permissions, priorities or model selection.
Controlled tests establish this wiring, not a measured improvement in subsequent
native work or a claim that every runtime failure should alter a view.

Focused tests exercise scoped HTTP ingress/context, two controlled processor
identities, changed views with contrary evidence, persistence, erasure, concurrent
heads and lease recovery. They demonstrate the storage and behavior contract;
they do not establish semantic quality or coherence across actual live models.

A bounded neutral LAN exercise ran the production projection against two
configured models. Both formed and revised a checkpointing view and abstained
on routine logistics. One model introduced an unsupported duration and count;
the original results were retained. After making quotation grounding explicit
and replacing semantic fixture IDs with opaque IDs, that model's three cases
were repeated once. Its initial explanation stayed within the reported counts
and outcomes, but the revision failed validation and logistics again produced
abstention. The failed revision retained processor metadata and its error class,
not the raw output or exact validation reason. No general quality pass follows
from this exercise, and no production owner facts or judgments were created.
