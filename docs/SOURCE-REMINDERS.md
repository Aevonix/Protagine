# Reminders that follow memory corrections

`pacomind_reminder` schedules a reminder from a specific memory claim. The agent first recalls the deadline, then selects the supplied source ID, source version and claim ID. `lead_seconds` can place the reminder before the deadline. This does not automatically schedule every date mentioned in a conversation.

The reminder is an ordinary Hermes one-shot job. Hermes owns its timer, execution history and delivery. Its prompt stores source references, not a copy of the original statement. A small generated script reads the current value when the job fires, with no model call. Delivery goes to the originating conversation; a terminal session without a delivery origin produces local output.

PacoMind's existing native dispatch tick follows explicit correction links and updates the job's schedule. Independently asserted conflicting dates require clarification. An annotation, forgotten source, invalid attribution or unresolved time prevents the reminder from using the old value. Owner cancellation pauses the job and remains effective across subsequent ticks.

Use `pacomind_reminder(operation="inspect", job_id="...")` to inspect the job or `operation="cancel"` to cancel it. Hermes' normal cron listing and output history remain available. Scheduling and preparing reminder text are distinct from confirmed delivery.

The reminder reserves its source lineage in the existing native ownership ledger
before rendering. Forgetting a source pauses its job and cleans up that job's
local output, queued payloads, errors and exact mirrored messages. Active
execution or delivery leaves cleanup pending until the native writer settles.
When a later reply consumes a persisted reminder, its answer inherits the source
references. Native compressed summaries and later replies that consume those
summaries retain that dependency through the same ownership ledger.
This requires the [qualified Hermes interface](HERMES-HOOK-COMPATIBILITY.md).
The plugin exposes the reminder tool only when that interface is available.
It does not retract a message already delivered to another device.

If a correction reaches the source store after the old occurrence was claimed, the script checks the current deadline again. A later deadline produces no output at the old time; after that occurrence ends, reconciliation rearms the same job for the corrected time. This check observes a source snapshot before output preparation. It does not provide a transaction spanning a simultaneous memory correction and a remote messaging service.

The first version requires a precise timestamp supported by the existing source date parser. A calendar day alone does not become midnight, and a date stored as a value is not rewritten into the memory claim's validity interval. General recurring jobs continue to use Hermes' `cronjob_manage`; waiting for a person's reply continues to use the separate expected-reply workflow.

Earlier development releases cannot reconcile this reminder ownership. After
admitting a reminder, retain the current adapter and native ownership interface
until its cleanup is complete, including when pausing reminder use during recovery.
