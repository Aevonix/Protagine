# Nightly consolidation

Sleep-time compute for the mind (architecture 3.1, 4.1, 4.2; build plan M8). Once per night
crossed, the sidecar consolidates what the time since the last run left in its stores: the
self-narrative delta, contradictions turned into questions, per-contact digests and episode
summaries. It runs inside the token budget, writes only to stores that already exist, and its
whole record is one audit row.

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

`Mind.state()["consolidation"]` reports `{last, running, last_tokens}` (the tokens of the last
finished run, whose row the marker names). Every run is its own audit row, written `done` when the
run starts (result: "did not finish") and charged after every call, then given its summary at the
end; no row is ever left running. A night cut short (a restart, `mind off`) keeps what it wrote and
leaves the marker unmoved, so the night is still due and runs again at the next due tick; every
stage is idempotent, so nothing is doubled.

## What runs, in order

Cheapest and most valuable first; a stage that runs out of budget stops and the rest waits for the
next night.

1. **Narrative delta** (`mind.faculties.self_narrative`; one call). The computed sections are
   rebuilt from the stores and stored: `self.interests` (the three strongest seeded and declared
   interests, each citing its own record, `interest:<slug>`), `self.strengths` (the two task
   types with the most work over 30 days: `research: 3 done, 1 failed, 2 verified of 4`, or
   `weak at <type>` when failures outnumber successes, citing up to two intention ids) and
   `self.stances` (three of the judgments store's current revisions, each citing
   `judgment:<revision id>`, until M7's opinion store replaces the reader). Only `self.recent`
   ("the last 7 days") is model-written, by delta edits of at most 4 lines. Its evidence is the
   agent's own actions of the last 7 days (`audit.is_action`: a task, goal or message it decided
   to act on or ask about; never an internal note or a notice) and their recorded findings, all
   under the intention's own id, so every `recent` citation is an id `protagine_self why`
   explains. Actions addressed to anyone but the owner and contradiction questions (they quote
   what people said) are left out; the plugin renders the narrative only in the owner's own
   sessions. Every line must cite ids from that evidence that also exist; a line that cites
   anything else is dropped and counted in `rejected_lines`. A line whose cited row is later
   pruned by retention disappears at the next render: the narrative moves only when the evidence
   does. Lines end with their citations in square brackets. A citation is one of five kinds: a
   plain intention id (the agent's own action), or a record reference `interest:<slug>`,
   `judgment:<revision id>`, `turn:<turn_id>` or `claim:<id>`; nothing else resolves.
2. **Contradictions** (no model). The rule recall already applies: two live scalar claims about the
   same `(subject, predicate)` with different values and overlapping validity (quoted preferences
   and derived claims never count). A contradiction becomes one `question` concern that carries a
   typed message candidate to the owner (type `contradiction`, drive `curiosity`, salience 0.75,
   cost 0, `dedup_key = reach_out:contradiction:<id>`); the tick's `_act` forms it through rank and
   authority like every other concern, so the autonomy level, the owner-message budget and the ask
   codes apply unchanged: `standard` sends it, `suggest` asks. The question's title carries both
   values ("Which is right for your office location: 'room 4' or 'room 7'?"), so an ask line is
   answerable on its own. At most one new question a night, the newest conflicting pair first:
   the owner's three daily messages are left to duty work, and the rest are raised on later
   nights. A question is asked once (the shared dedup key); a concern still waiting for the ranker
   is refreshed. When the claims agree again (a correction retracted or superseded one side) a
   question not yet answered (deferred, asked or queued) is withdrawn by the check
   (`verified: check`, no verdict) and the concern resolves: the owner is never asked something
   already settled.
3. **Per-contact digests** (`mind.faculties.people`; at most 6 calls). A digest has one home, the
   contact's own record (`digest`, `digest_sources`, the people milestone's columns), and one
   writer path, the contact store's `set_digest(contact_id, text, sources)` (awaited when it is a
   coroutine). A contact store without `set_digest` (the one before the people milestone) gets no
   digest, and nothing is stored anywhere else. Candidates come from the store's public
   `list()`: the contacts whose `last_interaction_at` (the host bumps it on every turn) is in the
   last 7 days, newest first, never the owner (a digest serves the other people the agent talks
   with). A contact whose stored `digest_sources` are exactly its live claim ids is skipped; a
   template digest (`digest_sources: ["template"]`) is replaced. The model sees that person's own
   live claims (at most 40, with their ids) and the previous digest and returns at most 600
   characters of what the person has told the agent, plus the claim ids it rests on; unknown ids
   are dropped and a digest with no valid source is rejected (and tried again the next night).
   The mind renders no section of its own: the people milestone's `protagine-person` section
   renders the contact's digest in that contact's turns.
4. **Episode summaries** (at most 8 calls). Sessions with at least 3 person-scoped turns since the
   last run (at least the last 24 hours, at most 7 days), newest first, each summarised in at most 120 words and written by
   `Autobiography.record(contact_id=...)` as the ledger row
   `mind:episode:<session>:<date>:episode_summary` under **that contact** (`session_id="mind"`,
   `scope="person"`, `derive_claims=False`): the person's later sessions recall it, and it never
   becomes a claim.

Every input query excludes the mind's own rows (`SELF_TURN_SQL`: `session_id<>'mind' AND turn_id
NOT LIKE 'mind:%'`). The same exclusion applies to the commitment extractor's "Recent conversation":
the autobiography is the agent's record, not the person's conversation.

Claim dedupe happens where the night reads claims, never in the store: of the scalar claims
repeating one `(subject, predicate, value)`, the contradiction and digest stages see only the
newest witness, the rule recall's `current_group` already applies (`distinct_values`); quoted
preferences are never folded. The commitment extractor's prior claims use the same rule
(`source_projection.one_witness_per_value`).

## Budget

| Stage | Calls per night | Output cap |
|---|---:|---:|
| Narrative delta | 1 | 600 |
| Contradictions | 0 | – |
| Per-contact digests | <= 6 | 300 |
| Episode summaries | <= 8 | 250 |

The night may spend at most `mind.budgets.learn_share x mind.budgets.llm_tokens_per_day` tokens
(0.25 x 200,000 = 50,000 by default). The share is the learning work's, not the night's alone: a
later consumer of it (M9's nightly lesson batch) reads what the day's consolidation rows already
charged and spends what is left. Before every call the run checks that what is left of that
share still covers the call (the larger of its output cap and the biggest call so far) and that
`Authority.tokens_allowed()` holds; the call's real usage (`response.usage`) is written at once to
`cost_tokens` on the night's `note/consolidation` audit row, so the shared day budget and
`protagine mind stats` (`mind_tokens`) see it. Each call has the router's function deadline
(clamped to 10 minutes) and the whole run is bounded by 15 minutes; a stage that fails is recorded
in the row's `context.errors` and the next stage still runs. Calls are labelled by `context.task`
(`mind_consolidate_narrative`, `mind_consolidate_digest`, `mind_consolidate_episode`) and carry
`workload: background`, which the router keeps in its call record (`routing_status`
`recent_calls`) and never sends. The overhead row's background tokens come from the audit rows'
`cost_tokens` (`protagine mind stats`: `mind_tokens`): the paired harness observes requests on the
wire, where the label is not.

## What it writes

| Store | Key or row |
|---|---|
| `mind.db` `mind_state` | `consolidation.last` (`updated_at` = the moment of the last run, text = its local date); `self.interests`, `self.strengths`, `self.recent`, `self.stances` (text = the section, causes = up to 5 cited ids) |
| contact store | `digest`, `digest_sources` of each digested contact, through `set_digest` (only a store that has it) |
| `mind.db` `concerns` | kind `question`, `dedup_key reach_out:contradiction:*`, a typed message candidate to the owner |
| `initiatives` (the audit log) | one `note/consolidation` row per run (`cost_tokens`, `context` = the run's record: counts, calls, tokens, rejected lines, errors); the owner question is the `message` row (type `contradiction`) the tick forms from the concern |
| ledger `turn_sources` | `mind:episode:<session>:<date>:episode_summary` under the episode's contact |

Nothing here writes `protagine.yaml` or `identity.yaml`.

## Reading it

- `GET /v1/mind/narrative` and `protagine mind narrative`: `{enabled, text, sections: {interests,
  strengths, recent, stances}, cites, updated_at}`. `text` is at most 800 characters (3 interests,
  2 strengths, 4 recent lines, 3 stances), one line per entry prefixed `interest:` / `strength:` /
  `recent:` / `stance:`, each ending with its ids in brackets. It rides in every model request of
  an owner session, so it is sized by the overhead budget
  (`tests/hermes_adapter/test_overhead_budget.py`). With `self_narrative` off: `enabled: false` and an empty text. With `consolidation` off
  and `self_narrative` on the computed sections still render (the `full-consolidation` arm keeps a
  narrative; `full-self_narrative` has none).
- `POST /v1/mind/consolidate` and `protagine mind consolidate`: the night's record (`local_date`,
  `calls`, `tokens`, `budget`, `counts`, `done`, `errors`, `rejected_lines`, `id` of the audit row),
  or `{skipped: running}` while a scheduled night is in flight.
- `protagine mind why <id>` on the night's row, and `protagine mind log --kind note`.
