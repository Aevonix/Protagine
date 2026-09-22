# Initiative execution and outcome evidence

Hermes owns initiative execution. The built-in no-host executor is removed,
including `PROTAGINE_EXECUTOR_ENABLED`, its other `PROTAGINE_EXECUTOR_*` settings,
the automatic preset defaults, and `/v1/host/executor/status`. Old environment
values cannot launch it. Remove them from deployment configuration.

| Work | Existing execution path | What completion establishes |
| --- | --- | --- |
| Registered evidence review | `initiatives/native_work.py` and the Hermes `NativeReviews` task binding | A source-bound report was submitted; its conclusions remain unverified |
| Accepted local draft | `commitments/local_work.py` and native local-work binding | The bound draft exists; this does not establish that its contents are correct |
| Temporal follow-up | `initiatives/temporal_followup.py`, native preparation and selected outbox | Preparation and delivery have separate receipts; neither proves recipient benefit |
| Other generated initiative | Existing initiative ledger | A proposal exists; missing native execution capability is reported by reconciliation |

The shared reasoning API still serves project analysis. It is not the removed
initiative poller. No new executor, scheduler or verifier service replaces it.

## Reconcile existing work

Stop the old executor and take the deployment's normal state backup first.
Run this command using the sidecar environment, substituting the state directory
and every actual historical executor ID. IDs are explicit because installations
may have configured their own. The default is `protagine-executor`.

```sh
python -m protagine.initiatives.executor_retirement \
  --state-dir /path/to/state --executor-id protagine-executor > retirement-preview.json
python -m protagine.initiatives.executor_retirement \
  --state-dir /path/to/state --executor-id protagine-executor \
  --apply --executor-stopped > retirement-applied.json
```

`--executor-stopped` records the operator's prerequisite; the command cannot
inspect remote service managers. Repeat `--executor-id` for other historic IDs.
Preview opens the existing database read-only, accounts for every row and never
creates or repairs a database. Apply uses one transaction and appends idempotent
receipts in the existing assignment history.

Terminal results, effect receipts and task IDs are unchanged. Work currently
owned by another executor is untouched, even when old assignment history exists.
Unfinished work last owned by the retired executor becomes cancelled with
`executor_retired_effect_unverified` and an explicit reconciliation reason.
This cancels that execution, not its underlying goal. A failed status would let
the generator retry automatically, which is inappropriate when prior effects
are unknown. Inspect retained results and effects before explicitly issuing new
native work. Never convert an old description directly into an action grant.

Unclaimed proposals remain unchanged. The report identifies existing native
review, accepted local-work and delivery consumers; other proposals receive
`proposal_only_native_execution_capability_missing`. Consumer classification is
not execution authorization. Reconciliation does not claim that a consumer is
enabled or that a task ran.

## Historical skill counters

Opening `SkillStore` migrates the existing skills database once, atomically:

1. Copy each skill ID and its original wins/losses to `skill_counter_archive`.
2. Reset the active counters to zero, preserving procedure text, source
   references, usage and other existing metadata.
3. Record counts, totals and the unverified evidence basis in `skill_migrations`.

Retrieval uses relevance and domain matching. Retention uses confidence and age
heuristics. Neither uses historical wins or losses; confidence itself is not a
verified success rate. New/imported procedures cannot restore those counters,
and there is no boolean `record_outcome(win=...)` writer. Prompt blocks identify
these procedures as unverified candidates.

The existing native skill evaluation owns source-bound outcome receipts and its
independent criteria. An absent criterion, model-authored assessment, normal
runtime exit, persisted draft or owner permission earns no quality credit.
Successful tasks also cannot award blanket credit to every retrieved skill.
This change does not infer new verified scores or retrospectively certify old
outcomes. Native evaluation evidence is retained in its existing store.

## Verification and rollback

Focused tests cover exact-once counter migration, transaction failure, retained
provenance, ignored positive/negative counters, all queue dispositions, distinct
same-entity work, preservation of active native ownership, repeat application
and prevention of implicit retry. Existing native task and skill-evaluation
tests remain responsible for their execution and criterion contracts.

Rollback uses the deployment's existing backup and binary selection. Do not
restart the retired executor or restore queued work without reconciling effects.
An older binary can resume unsupported reward writes; use a release that retains
the counter retirement rather than reviving those quality claims.
