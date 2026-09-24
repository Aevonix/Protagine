# Nightly consolidation

Sleep-time compute for the mind (architecture 3.1, 4.1, 4.2; build plan M8). Once per night
crossed, the sidecar consolidates what the time since the last run left in its stores: the
self-narrative delta, contradictions turned into questions, claim dedupe, per-contact digests
and episode summaries. It runs inside the token budget, writes only to stores that already
exist, and its whole record is one audit row.

Code: `sidecar/protagine/mind/consolidate.py` (`Consolidation`); the schedule is in
`sidecar/protagine/mind/tick.py` (`Mind._schedule_consolidation`).

## When it runs

| Condition | Rule |
|---|---|
| The boundary | The nightly boundary is a local time of day: the start of `mind.quiet_hours` when they are set, else 03:00 local; `PROTAGINE_AGENT_TIMEZONE` decides "local" |
| Due | The boundary fell in `(last run, now]`. `mind_state["consolidation.last"]` (no half-life) holds the moment of the last run in `updated_at` and its local date in `text`; a store without it is marked at the mind's first start, so a fresh store never consolidates before its first night has passed, whatever hour it started at. So: once per night crossed, never twice for the same night, and once for a machine that slept through the boundary |
| Switches | `mind.faculties.consolidation` (the schedule), `mind.enabled` / `protagine mind off` (the tick returns before the schedule), a router that supports function routing, and `Authority.tokens_allowed()` (the shared day budget) |
| How | The 60 s timer tick starts one `asyncio` task off the tick's lock, before the body-stale check (the body, Hermes, is not needed), and goes on. A forced tick (`POST /v1/mind/tick`: `protagine mind tick`, the plugin's `tick()`, the benchmark body) awaits a night it found due, at most 300 s (`CONSOLIDATION_WAIT_S`), so what the night wrote is there when the tick returns; a night still running then keeps running in the background. `Mind.stop()` and `protagine mind off` cancel a night in flight at once |
| Forcing | `protagine mind consolidate` (`POST /v1/mind/consolidate`) runs it inline now, whether or not a night was crossed; never with the mind off or `faculties.consolidation` false (`{skipped: off}` / `{skipped: consolidation off}`). It too marks the moment of the last run |

`Mind.state()["consolidation"]` reports `{last, running, last_tokens}`. A night interrupted mid-way
(a restart, `mind off`) leaves its audit row `dispatched` and the marker unmoved, so the night is
still due; the next run that date, scheduled or forced, resumes on that row, and every stage is
idempotent, so nothing is redone or doubled. A row left `dispatched` from an earlier date is closed
as `cancelled` ("interrupted") by the next run, so the audit log never shows a night still running
that is not. A second run on a local date that already has a finished one writes a second row.

## What runs, in order

Cheapest and most valuable first; a stage that runs out of budget stops and the rest waits for the
next night.

1. **Narrative delta** (`mind.faculties.self_narrative`; one call). The computed sections are
   rebuilt from the stores and stored: `self.interests` (the seeded and declared interests, each
   citing the task and goal intentions it led to; one nothing has come of yet has no citation),
   `self.strengths` (per task type over 30 days: `research: 3 done, 1 failed, 2 verified of 4`,
   or `weak at <type>` when failures outnumber successes) and `self.stances` (the judgments
   store's current revisions until M7's opinion store replaces the reader). Only `self.recent`
   ("the last 7 days") is model-written, by delta edits of at most 8 lines. The model sees the
   last 7 days of audit rows and of the mind's own findings, each with an id (rows addressed to
   anyone but the owner, findings of such rows and contradiction questions are left out of the
   evidence; the plugin renders the narrative only in the owner's own sessions), and every line it
   returns must cite ids from that evidence that also exist in their stores; a line that cites
   anything else is dropped and counted in `rejected_lines`. A line whose cited row is later
   pruned by retention disappears at the next render: the narrative moves only when the evidence
   does. Lines end with their citations in square brackets: `... [<intention uuid>, turn:<id>]`.
   The id kinds are an intention uuid, `turn:<turn_id>`, `claim:<sha>`, `concern:<id>` and
   `expectation:<id>`.
2. **Contradictions** (no model). The rule recall already applies: two live scalar claims about the
   same `(subject, predicate)` with different values and overlapping validity (quoted preferences
   and derived claims never count). Each becomes one `question` concern
   (`dedup_key = contradiction:<contact>:<subject_key>:<predicate>`, drive `curiosity`, broadcast
   only: it carries no task template, so it is never a research task) and exactly one question to
   the owner through the ordinary message path (`Mind.request_message`, type
   `reach_out:contradiction`), so the autonomy level, the owner-message budget and the ask codes
   apply unchanged: `standard` queues it, `suggest` asks. It is asked once; when the group stops
   contradicting (a correction retracted or superseded one side) the concern resolves itself, and a
   question still waiting (deferred, asked or queued, not yet sent) is cancelled by the check
   (`verified: check`, no verdict): the owner is never asked something already settled.
3. **Dedupe** (no model). Identical live scalar claims, same subject, predicate, value and validity
   interval, fold into the earliest: the others get `source_claims.duplicate_of`. A folded claim
   keeps its evidence but leaves the assertion set that recall and the digests read; a lookup by
   claim id, turn or message still sees it, so a retrieved quote folds into its bundle rather than
   surfacing as a second witness. Erasing the canonical claim's source un-marks its witnesses
   until the next night. Preferences, corrections with `value_parts`, derived claims, retracted
   and superseded rows are never touched. The column is added idempotently when the store opens.
4. **Per-contact digests** (at most 6 calls). Contacts with a person-scoped turn in the last 7 days,
   the owner first, read through `ContactStore.list()`; a contact whose live claim set is
   unchanged (hash in `mind_state["digest.hash:<cid>"]`) is skipped. The model sees that person's
   own live claims (at most 40, with their ids) and the previous digest and returns at most 600
   characters plus the claim ids it rests on; unknown ids are dropped and a digest with no valid
   source is rejected. The digest is written through a sink callable: by default
   `mind_state["digest:<cid>"]` with the claim ids as its causes (the M5 contact model passes its
   own writer and reader through the same seam, `digest_sink` and `digest_source`; the writer may
   be async, the reader must be synchronous because the context path is).
   `Mind.person_section(contact_id)` renders it, and only it, into that person's own turn context
   as `About <contact id> (digest, <date>): <digest>`, at most 600 characters.
5. **Episode summaries** (at most 8 calls). Sessions with at least 3 person-scoped turns in the last
   24 hours, newest first, each summarised in at most 120 words and written by
   `Autobiography.record(contact_id=...)` as the ledger row
   `mind:episode:<session>:<date>:episode_summary` under **that contact** (`session_id="mind"`,
   `scope="person"`, `derive_claims=False`): the person's later sessions recall it, and it never
   becomes a claim.

Every input query excludes the mind's own rows (`SELF_TURN_SQL`: `session_id<>'mind' AND turn_id
NOT LIKE 'mind:%'`). The same exclusion applies to the commitment extractor's "Recent conversation":
the autobiography is the agent's record, not the person's conversation.

## Budget

| Stage | Calls per night | Output cap |
|---|---:|---:|
| Narrative delta | 1 | 600 |
| Contradictions, dedupe | 0 | – |
| Per-contact digests | <= 6 | 300 |
| Episode summaries | <= 8 | 250 |

The night may spend at most `mind.budgets.learn_share x mind.budgets.llm_tokens_per_day` tokens
(0.25 x 200,000 = 50,000 by default). Before every call the run checks that what is left of that
share still covers the call (the larger of its output cap and the biggest call so far) and that
`Authority.tokens_allowed()` holds; the call's real usage (`response.usage`) is written at once to
`cost_tokens` on the night's `note/consolidation` audit row, so the shared day budget and
`protagine mind stats` (`mind_tokens`) see it. Each call has the router's function deadline
(clamped to 10 minutes) and the whole run is bounded by 15 minutes; a stage that fails is recorded
in the row's `context.errors` and the next stage still runs. Calls are labelled by `context.task`
(`mind_consolidate_narrative`, `mind_consolidate_digest`, `mind_consolidate_episode`).

## What it writes

| Store | Key or row |
|---|---|
| `mind.db` `mind_state` | `consolidation.last` (text = local date); `digest:<cid>` (text, causes = claim ids), `digest.hash:<cid>`; `self.interests`, `self.strengths`, `self.recent`, `self.stances` (text = the section, causes = up to 5 cited ids) |
| `mind.db` `concerns` | kind `question`, `dedup_key contradiction:*`, no task template |
| `initiatives` (the audit log) | one `note/consolidation` row per night (`cost_tokens`, `context` = the night's record: counts, calls, tokens, rejected lines, errors); one `message` row per contradiction (`reach_out:contradiction`) |
| ledger `turn_sources` | `mind:episode:<session>:<date>:episode_summary` under the episode's contact |
| `source_claims` | `duplicate_of` (nullable column, added by `initialize()`) |

Nothing here writes `protagine.yaml` or `identity.yaml`.

## Reading it

- `GET /v1/mind/narrative` and `protagine mind narrative`: `{enabled, text, sections: {interests,
  strengths, recent, stances}, cites, updated_at}`. `text` is at most 2,000 characters, one line per
  entry prefixed `interest:` / `strength:` / `recent:` / `stance:`, each ending with its ids in
  brackets. With `self_narrative` off: `enabled: false` and an empty text. With `consolidation` off
  and `self_narrative` on the computed sections still render (the `full-consolidation` arm keeps a
  narrative; `full-self_narrative` has none).
- `POST /v1/mind/consolidate` and `protagine mind consolidate`: the night's record (`local_date`,
  `calls`, `tokens`, `budget`, `counts`, `done`, `errors`, `rejected_lines`, `id` of the audit row),
  or `{skipped: running}` while a scheduled night is in flight.
- `protagine mind why <id>` on the night's row, and `protagine mind log --kind note`.
