# Reminders from recalled deadlines

`protagine_reminder` schedules a reminder from a specific recalled claim. The
agent first recalls the deadline, then passes the supplied `source_id`,
`source_version` and `claim_id`; `lead_seconds` places the reminder before the
deadline. Only the owner's own conversation can schedule, inspect or cancel.

The plugin resolves the deadline once through
`POST /v1/host/memory/sources/deadline` (which applies the configured timezone
frame and refuses unresolved or forgotten claims) and creates an ordinary
Hermes one-shot cron job named `Remembered deadline` whose prompt carries the
reminder text. Hermes owns the timer, the run history and delivery to the
originating conversation. The plugin keeps nothing but the cron job id.

`operation: inspect` returns the job's state and next run; `operation: cancel`
pauses it. Later memory corrections do not move an already scheduled job;
cancel it and schedule again. Recurring schedules unrelated to memory use
`cronjob_manage`.
