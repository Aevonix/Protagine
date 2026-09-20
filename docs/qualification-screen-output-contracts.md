# Screening with an explicit output contract

`protagine models benchmark plan --output-contract json_schema` selects
`agent-screen-2-structured-output`. Use `--boundaries role_completion,cognition_consumer`
to run the complete 18-case standard screen without replaying native Hermes cases.

The 16 direct tasks send a strict JSON schema through the existing function router.
Their messages, independent semantic oracles, configured output allowances and
deadlines stay the same. Schemas declare only field names and allowed types or
choices already present in the task. They never encode the expected answer,
null-only unknowns, expected array lengths or correct numerical results. The two
memory cases continue through the actual source formation and recollection
consumer. This option does not force a schema into native Hermes tool turns.

Each direct result requires a witness of the actual serialized `response_format`,
valid JSON matching the declared schema, and the original semantic checks. There
is no markdown stripping, substring extraction, repair model, fallback or quality
retry. Bindings without declared JSON-schema support report `unsupported`.

Plan a new directory for every model and run the whole selected set, including
previous passes. Preserve the prompt-only records. This is a new protocol and
must be presented as a separate cohort; do not combine its scores with the old
prompt-only leaderboard or replace historical results. Both protocols measure
their stated output contracts. A higher structured score demonstrates useful
performance with schema-capable serving, not improved model weights.

The existing `prompt_only` default retains `agent-screen-1`. Plans freeze protocol,
case, oracle, implementation and configuration hashes before inference. Execution
refuses a modified plan or implementation, and resume preserves completed
attempts without rerunning them.
