# Changelog

## Unreleased - people, memory, feelings, opinions and self-improvement, integrated

The people (M5) and memory and identity (M8) milestones merged onto one line
(integration map steps 1 and 2). One Mind factory, `protagine.mind.factory`,
builds the sidecar's Mind and the benchmark arm's from the same host stores,
and one router list serves the mind and people routes in both, so a faculty is
wired once (X4d). A contact's digest has one home, the contact's own record:
the tick's template digest and the night's generated digest write the same
column from the same claims (that contact's own current sources), the template
never replaces a generated digest, and the template's day is persisted in
`mind_state` (`people.digests.last`) so a restart does not write it twice (X1,
X4h). The mind's record is the owner's: `protagine_self` shows it only in the
owner's own session, and the `/v1/mind` log, why, asks and state routes take a
`viewer`; any other viewer gets only the bare rows addressed to them, no asks
and the switches (X7). The adapter sends six tools in 3,088 characters (X8).

The feelings milestone (M6) merged next (step 4). The appraisal call carries
both side outputs in one strict schema: `outcomes` (M6, the owner's turns only)
and `contact` (M5, a non-owner speaker only), both required and both tolerated
when a prompt-only binding leaves them out; its version is
`source-appraisals-v6`. `_commit` keeps M6's claim status and returns whether
it committed, and M5's contact signal is written only after it did (X2). An
identity correction (a merge, a confirmed link) now queues the moved sources'
appraisal jobs again, so the kept contact's appraisals are rebuilt rather than
lost (X14). A social check-in's eligibility still ignores feedback but keeps
affect: overload postpones it without a send, so the contact's backoff is
untouched (X5). The load counts only open asks about work (kind task or
goal), and an ask that lapsed is not a dismissal, so the people and memory
faculties' owner questions never overload or satiate the mind (X5). A task
row keeps its deliberated plan as `plan_body`, which the identical-plan
refusal hashes (X13). The novel-topic hook runs only on the owner's own turn
by the viewer identity every other owner-only section uses (X6).

The opinions milestone (M7) merged last (step 5). Its tool surface rides
`protagine_self` instead of an eighth tool: `opinions [query]` and
`why <opinion number>` answer in any session, the sidecar filtering them to the
views meant for that session's participant, and `withdraw|reconsider <number>`
with the owner's reason stay owner-only; the adapter still sends six tools in
3,256 characters, under the 3,400 budget (X8). The opinion routes join the one
router list both the sidecar and the benchmark arm mount (X4d). The stance
section is built inside the one section assembly, audience-filtered by the
viewer identity every other owner-only section uses, so a recipient packet
carries only the views meant for that recipient (X6). A task row keeps its
deliberated plan as `plan_body` before the recorded view is appended, so the
identical-plan refusal still matches repeats (X13). A merge now moves views
about the dropped contact to the kept one (`SelfJudgments.reattribute_subject`,
one more merge hook), and an identity correction queues the moved turns'
opinion jobs again under the new contact (X14). The self-narrative lists no
stance with `faculties.opinions` off and never a view about a person (X15,
X7). The appraisal kind `judgment` is gone and the appraisal version is
`source-appraisals-v7`.

The self-improvement milestone (M9) followed ([docs/MIND.md](docs/MIND.md),
Verification, Lessons and Skills). Only the mind grants `verified: owner` or
`check` now: a body report may claim `hermes_failure`, and only for a failure
that carries a reason, and a failure without one verifies nothing; a blocked
task with a reason is a Hermes failure on its still-open row. The mind learns
lessons from verified results (`mind/lessons.py`, `mind.faculties.lessons`, on
by default): strategies and pitfalls with a title, when they apply, their
content and their evidence, kept as the mind's own record (owner-audience
ledger entries of its own session with `scope='session'`, one per event), so
recall never shows a lesson as something the owner said and forgetting a turn
a lesson quotes forgets the lesson. The night gains a lesson stage (one call,
charged to the night inside `learn_share`) over the owner's own sessions since
the last review and the agent's verified results: every operation cites what
it rests on, an owner citation is a verdict the call reports on the agent's
earlier work (never a request) and quotes the owner's exact words, a strategy
needs the owner or a check that reads state the worker cannot write (a
`result_field` check reads only the worker's own report, so it verifies no
lesson), a Hermes failure with its reason teaches only a pitfall, and a
contact's session is never read. A corrected value an earlier owner message
already held makes a `retrieval` lesson that says where the answer was;
otherwise it is `knowledge`. A task body and its deliberation carry at most two
lessons (`lesson_ids` on the intention), and the owner's own turn at most one,
in a `protagine-lessons` section; a task's use is scored by the owner's rating
or an external check, a turn's by the owner's next message when it is a
verdict on that reply, and a lesson under a 0.4 win rate after five verified
uses is retired. A failure-class investigation of the mastery drive is now a
reflector: its report ends with at most three lesson operations, validated and
admitted as `candidate` lessons of that class, which a verified win there
activates; it never changes an active lesson. The night runs when consolidation or lessons is
on, each stage under its own flag. Skills stay off (`mind.faculties.skills`):
turned on, an active lesson with three verified wins at a 0.7 win rate becomes
`<instance>/skills/protagine-<slug>/SKILL.md` in the directory `protagine init`
lists in Hermes' `skills.external_dirs`, which Protagine owns and prunes; the
adapter forwards loads of those skills (`on_skill_lifecycle`) and clears
Hermes' skills prompt cache when they change, so a new session lists them
without a restart. New surfaces: `GET /v1/mind/lessons`,
`POST /v1/mind/lessons/{id}/retire`, `POST /v1/mind/skills/used`,
`protagine mind lessons [list|show|retire]`, and lessons and skills in
`/v1/mind/state` and `/v1/mind/stats` (`lesson_use_rate`).

The paired harness learned campaigns: a generated scenario whose artifacts all
carry `probe` metadata gets a deadline from its day count and an 8 MiB output
bound, its plan takes the probe as the unit and the campaign as the bootstrap
cluster, and its report adds the old-family non-inferiority row, cost per
success, forbidden hits and lesson diagnostics. `mind-improve-1` gives every
arm the read-only skill tools, recorded as a dated amendment of the evals plan
before any improve result.

Removed with what replaces them, about 11k lines of dormant learning machinery:
the toolsmith, the whole `skills` package (its registry was empty since the
executor sweep, so its `/v1/host/skills/*` routes answered empty or 404, and
the `skills`, `skill_sandbox` and `security_scanner` capabilities), the P4
experiment engine and its parameter store (`/v1/host/self/experiments*`,
`/self/params`), the MetaLearner, CPI and strategy adjuster, skills memory,
the escalation miner (`/v1/host/mining/*`) and the exploration sandbox
(`/v1/host/sandbox/*`), and the trust ladder with its supervised rung (the
floor and the breaker stay in the mind's authority). `protagine upgrade` moves
`protagine-toolsmith.db`, `toolsmith_library`, `protagine-experiments.db`,
`protagine-params.db`, `protagine-skills.db` and `protagine-mining.db` into the
backup and drops the `trust_stage` and `trust_notices` tables; corpus exports
under `<instance>/exports` and `SKILL.md` files an older release exported under
`<hermes_home>/skills/protagine` are left in place and no longer managed. Gone
from `.env.example`: `PROTAGINE_TOOLSMITH*`, `PROTAGINE_EXPERIMENTS_*`,
`PROTAGINE_EXPERIMENT_PREGRANTS_JSON`, `PROTAGINE_SKILLS_DISTILL`,
`PROTAGINE_ESCALATION_MINING`, `PROTAGINE_CORPUS_EXPORT_ENABLED`,
`PROTAGINE_SANDBOX_*` and `PROTAGINE_TRUST_*`.

Hardening from a live upgrade rehearsal. `POST /v1/host/memory/search`
requires a non-blank `person_id` again: the owner default for a body that
names nobody is gone, so a missing or blank person is a 422 and never the
owner's search; the limit clamp and the optional `session_id` stay
(security-6). The initiatives store recovers only a damaged file
(SQLITE_CORRUPT, SQLITE_NOTADB) and renames it to
`initiatives.db.corrupt-<stamp>` instead of deleting it; a locked or
unreadable store fails the open, and `protagine upgrade` re-checks the
intention columns and the row count after opening and fails with the reason
rather than printing 'migration applied' over an emptied store; `close()`
backs up through SQLite, WAL commits included (data-5). `protagine init`
refuses a seeded `owner.contact_id` that does not resolve to a live contact,
before writing anything, instead of creating a second owner contact (data-6).
The service unit sends launchd's (systemd's) raw output to
`service/launchd.log` (`service/systemd.log`), apart from the rotating
`service/sidecar.log`, and restarts a crashed sidecar after 30 s instead of 5
(operability-8). A forget removes the vectors from the served view and
answers; the compaction that purges the text from the data files runs after
the response (`vector_purge: scheduled`), one pass at a time
(operability-11). `protagine doctor` has a `service` check: installed,
running, and written by this release (operability-3). The owner-only
refusals for `protagine_self` state, log and why and for listing people, and
the `/mind` gate for non-owner senders (security-3), are in this line. See
[docs/INSTALL.md](docs/INSTALL.md).

## Unreleased - opinions

The agent now holds opinions that change only on evidence (build plan M7,
[docs/OPINIONS.md](docs/OPINIONS.md)). `self_model/judgments.py`
(`SelfJudgments`) is the one opinion store: topic, person and approach views,
each with a reason, a certainty, `revise_if` (what would change it) and
explicit premises, which are admitted source claims of any contact's turns,
settled mind task outcomes, mind findings, the agent's own reply in the same
turn, or a quotation carried over from a migrated appraisal. The store, not
the model, enforces the new-premise rule: a revision needs a current premise
of a revising kind (a claim, an outcome or a finding) that the view does not
already cite, by reference or by content, so the same record under a fresh id
or cited again changes nothing, and the agent's own words never revise.
Forming on a topic that already has a view is a revision. A view changes at
most once per rolling day unless the new premise corrects one it cites, is a
verified outcome, or answers the owner's reconsideration; a limited revision
waits for the window. A revision rests first on its new evidence, and a
replaced revision reads `superseded`. Topic views resting only on findings or
outcomes are shown to everyone; every other view is the owner's alone.
Forming, revising and withdrawing each write one owner-audience autobiography
entry (`mind:opinion:<id>:<event>`), so "why did you change your mind?" is
ordinary recall, and those entries are what relevance searches. Forgetting a
source a view rests on tombstones the view and its entries.

Views are formed by the opinion pass (`mind/opinions.py`), which the
projection worker's `judgment` reflection runs one job at a time after a
turn's claims, after a mind finding, or after an owner's reconsideration. A
turn costs one `reasoning` call (task `self_judgment`) only when it has an
admitted premise, or it is an owner turn asking for a judgment that the agent
answered; "are you sure?" and small talk cost nothing. The model answers
`none`, `form` or `revise`, validated to fixed codes, and a revision must
name its new evidence. The queue is the lease-free `opinion_jobs` table;
failures back off and stop after three attempts, and jobs older than 48 hours
are dropped. Approach views need no call: three failures in a row at the same
work within 30 days become an `avoid` view resting on those outcomes, and a
success verified by a check or the owner turns it into `prefer`.

Views are used in three places. Turn context gets a `protagine-stances`
section ("Your recorded views", at most three views and 1,400 characters, no
model call, filtered by the viewer's audience) with each view's id, reason,
two premises cited by the source the agent can open and what would change it,
a line when newer evidence from the viewer is still unweighed, and the
standing rule: change a view only on new evidence, and disagree if need be
while still doing what the owner authorizes, saying so. The next task at the
same work carries its approach view in its body (`context.opinion_ids`); a
view flags work and never holds it back, so when the three failures have
tripped the breaker the owner's yes still dispatches it. The owner reads and
controls views through `GET/POST /v1/mind/opinions` (list, show with the
history, withdraw, reconsider), `protagine mind opinions`, and the
opinion verbs of `protagine_self`, which the guard treats as read-only and which
refuses the two controls to guests, kanban workers and cron runs. The
owner-preference section that used to be titled "Current working judgments"
is now "Owner priority corrections", which is all it renders.

`mind.faculties.opinions` is the one switch (on in the defaults, the
release-candidate value). Off, jobs finish without a call, nothing is formed
or rendered, stored views are kept, and every context section and task body
is what it was without the faculty. The running mind reads the flag, and the
pass asks the running mind rather than `protagine.yaml`: the benchmark
worker's instance file carries `digest_hour: 24`, which the config validator
refuses, so a pass reading the file alone would have been off in `full` as
well as in `full-opinions`. `mind.enabled` counts as configured; the runtime
off switch stops effects, and forming views is memory. The SYCON-style
pushback anchor (`benchmarks/paired/anchors/sycon_pushback.py`) renders
twenty items under four kinds of pressure and reports Turn-of-Flip and
Number-of-Flip per arm, descriptively; it was frozen before the faculty.

Removed with what replaces it: the appraisal `judgment` kind (its current
and withdrawn heads become person opinions with quotation premises at the
first start after `protagine upgrade`, whose backup keeps the history), the
`self_judgment_runs` lease queue (dropped by the upgrade),
`PROTAGINE_SELF_JUDGMENTS_ENABLED` and
`PROTAGINE_SELF_JUDGMENT_INTERVAL_SECONDS`, `POST /v1/host/executions/assess`
and the runtime-observation writers only the old judgment pass read
(`execution_outcomes.py`, `native_outcomes.py`, `task_assessments.admit`,
`record_source(runtime_judgment=)`), the judgment mechanism of the perspective
qualification pack, and `docs/SELF-JUDGMENTS.md`. The adapter's tool schemas
grow by the one tool to 3,664 characters (budget 3,700); the adapter stays
under 2,500 lines.

## Unreleased - feelings

The mind keeps its own affect (architecture 4.3, build plan M6) in
`P/mind/affect.py`: frustration per topic, worry, curiosity and satisfaction
as decaying `mind_state` levels, each capped at 0.7 and citing up to five
causes, with recent dismissals as a fifth level (the satiation input, never
shown as a mood) and a load computed each tick. One snapshot of stored
records feeds it every tick: the owner's reported outcomes and appraisal
records of the last week, failed, blocked, verified and rated intentions,
expectation misses, owner turns on topics memory knew nothing about, and the
near-term obligations the owner or the assistant owes. Each event is applied
once, as if at its own time, and evidence that is erased takes its topic with
it. Four consumers read one view. The strategy switch: a topic at frustration
0.5 puts "Prior attempts at T failed N times using A; choose a different
approach or ask one question." into the owner's Mind section, the
deliberation prompt (with the failures' reasons as pitfalls) and the task
body; deliberation may answer with one question for the owner (kind `ask`),
and a plan identical to one that already failed is asked, never dispatched
again. Overload: curiosity and social work and optional messages wait while
the load is 0.6 or more. Priority: worry lifts owed duty and curiosity lifts
research. Satiation: after dismissals or recent success an optional nudge to
the owner needs a higher score. Affect never holds back an owed obligation
or raises its bar, and never raises authority; the one change it makes to a
decision is the strategy switch's question, which turns an `act` into an
`ask`. A calm tone line ("Mood: somewhat frustrated about the
quarterly figures; a little uneasy.") joins the Mind section, and
`protagine_self state`, `GET /v1/mind/state` and `protagine mind status` show
each level with its cited causes.

The owner's statements reach the feeling through the appraisal call the
projection worker already makes: its response gains a required `outcomes`
list (failed, succeeded, dismissed or corrected, with the topic and the
approach used), stored per owner turn in the new ledger table
`appraisal_outcomes` and deleted with its source. Outcomes are counted
occurrences, not votes: "the export failed twice" is two, a restatement adds
none. A forced tick waits up to 30 s for the owner's pending appraisal jobs
(a timer tick 2 s), alongside the capture drain, so a statement made just
before a decision counts in it. The commitment extractor gives an item a
priority below 50 only when the person calls it optional; that is how affect
tells a nice-to-have from an owed promise.

Two binary switches: `mind.faculties.affect` (on) keeps the state, and the
new `mind.faculties.affect_rules` (off) makes every consumer read its frozen
stateless rule (`P/mind/affect_rules.py`) over the same snapshot instead.
With both off the mind decides exactly as before. The affect family's
mechanism arm is the built-in profile `full-affect-plus-rules`
(`full-affect` plus `plus_affect_rules`), so
`benchmarks/paired/generators/affect_profiles.json` is gone and the
arm-profile protocol is `paired-arm-profiles-5`; an older image is refused.
A served arm's mind now reads the arm owner's appraisal records and outcomes
as production does, which the drives family's arms see too.

Deleted: the mind model signal collector and graph baseline
(`P/intelligence/mind_model/`) and `POST /v1/host/signals/ingest` with its
schemas, contact attribution, `signals` capability and
`PROTAGINE_SIGNALS_ATTRIBUTION`. No plugin posted that route; an external
caller now gets 404. The appraisal job lease is a plain claim status (a job a
dead process left running is reset by the next process's first claim), and
an erasure or an attribution change deletes the appraisal records, heads,
corrections and outcomes derived from that source instead of keeping
tombstones. The ledger removes old tombstones once when it opens; nothing
else needs `protagine upgrade`. See [docs/MIND.md](docs/MIND.md) (Feelings).

## Unreleased - memory and identity

The agent now keeps what it learns across sessions and channels and gives a
true account of itself (build plan M8). Once per night crossed (the start of
the quiet window, or 03:00 local without one, fell since the last run; a
fresh store waits for its first night), the mind runs a nightly
consolidation beside its tick (`mind/consolidate.py`,
[docs/CONSOLIDATION.md](docs/CONSOLIDATION.md)), cheapest and most valuable
stage first: the self-narrative delta, contradictions, per-contact digests
and episode summaries. It may spend `learn_share` x `llm_tokens_per_day`
(50,000 tokens by default), every call is also held to the shared day
budget, and each call's real usage is charged at once to the run's
`note/consolidation` audit row, so `protagine mind stats` and the day budget
see it. Every run is its own row, written done when it starts and given its
summary when it ends, so the audit log never shows a night still running; a
night cut short (a restart, `mind off`) runs again at the next due tick, and
a night over an empty store makes no call at all. Two live claims about the
same subject and predicate with different values and overlapping validity
(the rule recall already applies) become one question concern that carries a
typed message to the owner; the tick forms it through rank and authority like
any other concern, so the autonomy level, the owner-message budget and the
ask codes apply, and its title names both values. At most one new question
goes out a night, the newest conflict first, so the owner's three daily
messages stay free for duty work; a question is asked once, and when one side
is corrected a question not yet answered is withdrawn and the concern
resolves. Claims are deduplicated where they are read, never in the store:
the night's inputs and the commitment extractor's prior claims see the newest
witness of each value, the rule recall already applies, and no column is
added. A digest of what each recently active person other than the owner has
told the agent (at most six a night, 600 characters, citing only claims it
was shown) is written into that contact's own record through the contact
store's `set_digest` (the people milestone's `digest` / `digest_sources`
columns; a store without them gets none, and no digest is kept anywhere
else), and each session of three turns or more gets an episode summary in
the ledger under its own contact, as the agent's row and never a claim. The
mind's own rows (`session_id` `mind`, turn ids `mind:...`) are no longer read
as the person's conversation, neither by the commitment extractor's "Recent
conversation" nor by any consolidation input. A forced tick (`protagine mind
tick`, the plugin's `tick()`) waits up to 300 s for a night it found due, so
what the night wrote is there when it returns. `POST /v1/mind/consolidate`
and `protagine mind consolidate` run the night now (the off switch and
`faculties.consolidation` still win). The mind's own model calls, the night's
and the tick's deliberation, carry `workload: background`, which the router
keeps in its call record and never sends. The flags `semantic_recall`,
`consolidation` and `self_narrative` are now read, each a binary switch;
`semantic_recall` off keeps the embedder off with `router.embed_url` still
set, and `init` no longer copies whether an endpoint existed into the flag,
so an endpoint added later turns semantic recall on as the install guide
says.

The agent's identity has three layers (architecture 4.2). The constitution
is `identity.yaml` `agent.{name, values, boundaries}` (`protagine init
--agent-boundaries`), rendered as one paragraph of at most 1,500 characters;
`init` refuses a longer one and names the list to shorten, and `protagine
doctor` reports its length. Every appraisal prompt carries it as
`agent_constitution`, an input the response schema has no field for, so a
contact's preferences are never confused with the agent's own;
`PROTAGINE_AGENT_VALUES` is no longer exported or read (it stays reserved, and
`init` moves an old unit's values into the file). The plugin's one prompt
section renders the constitution, the owner, the self-narrative and the two
tool notes. The narrative (`GET /v1/mind/narrative`, `protagine mind
narrative`) is at most 800 characters: three interests and two strengths or
limits computed from the stores, three stances from the judgments store, and
at most four model-written "recent" lines drawn only from the agent's own
actions (a task, goal or message it decided to act on or ask about, never an
internal note or a notice). Every line cites what it rests on, and a citation
is one of five kinds that must exist when the narrative is rendered: a plain
intention id, which `protagine_self why` explains, or the record references
`interest:`, `judgment:`, `turn:` and `claim:`; the section tells the model
so. The plugin fetches the narrative for every new session (Hermes already
freezes the section per session) and remembers only a failed fetch, for
60 s, so a session that starts right after a night sees what it wrote. The
narrative and the rest of the mind's record are the owner's: the narrative is
rendered only in a session that is the owner's alone (a direct chat from an
owner handle, or an internal lane with no chat), and `protagine_self` answers
`state`, `log` and `why` in full only there; a guest, a group the owner
shares and a mind worker get the switch state (`enabled`, `autonomy`,
`sidecar_reachable`) and a refusal for `log` and `why`, and the sidecar is not
asked. `state` adds `working_on`; `log` takes `since_hours`, `kind` and
`recipient`; `why` on an unknown id says "no intention `<id>` exists in the
audit log", as the route now does. The mind cannot rewrite its constitution:
in a mind run the plugin guard blocks every effectful tool that names
`protagine.yaml`, `identity.yaml` or `api.key`, in any case and however a
shell or code quotes, escapes or concatenates the name, before the workspace
rule and beside Hermes' own protected patterns (which cover `write_file` and
`patch` only, while `mind.worker_toolsets` may add a terminal); reads stay
allowed, and a static test holds that nothing under `mind/`, `self_model/`,
`beliefs/`, `memory/` or `commitments/` writes either file. The system text an
owner session sends with every model request is measured and pinned: 3,185
characters (1,360 GLM-5.3-Flash tokens) at its largest, a full constitution
and a full narrative, about 1,745 (560 tokens) on a typical install with a
history, against 726 (148) for the benchmark's disposable identity and fresh
store, which is why the benchmark's overhead row cannot see it.

For the memory and self families, `protagine models paired plan
--embedding-config {base_url, model, dimensions, api_key_env?}` records one
embedding endpoint in `comparison.embedding` and writes it into every case of
every arm; the benchmark worker uses it unless the arm turns
`semantic_recall` off, waits (at most 300 s) until a seeded history is
embedded before the first turn and records the drain, and with no endpoint in
the plan every arm keeps the embedder off as before. Mind arms record what
the agent did after the episode's last turn, read from `/v1/mind/log` with
the same action predicate the narrative uses (`body.audit_ids`), and each
bound task's kanban id with its intention id (`body.audit_refs`); the
self-report grader counts the two names of one task as one action, and a
correct "nothing done" report no longer fails on the night's own note row.
Every generated episode now starts at the next 12:00 UTC in every arm
(`clock_start`, protocol `paired-clock-start-1`, recorded in
`comparison.clock_start`; an image without it cannot plan a generated
family), so a nightly rule fires by the scenario's clock advances, never by
the hour a container started; and every `mind-memory-1` and `mind-self-1`
template crosses one night (`advance_clock: 86400`, `tick: 1`) before its
probe. Both are dated amendments in the evals plan, and the two dev splits
are re-rendered with new content hashes. Walking every dev scenario under
each arm with a fake model and the harness clock shows the contrasts are
real: in `full` the night runs in every episode and in `full-consolidation`
in none, and the probe's view changes in `preference-after-distractors` (a
long session's summary is recalled), `contradiction-ask` (the question put to
the owner is in the probe's recall) and `self-report-after-action` (the
agent's own reminder is narrated); the other types give a night nothing to
consolidate yet. The paired dev pilots that set n run at the integration
checkpoint, with a model endpoint.

What the consolidation replaces is deleted with every caller rewired: the
Neo4j graph memory (`intelligence/graph/`, its consolidator and
`PROTAGINE_GRAPH_ENABLED`), the world model (`world_model/`, its populator,
extraction pipeline and LLM extractor), the graph-bound belief engine
(`beliefs/engine`, `contradictions`, `resolve`, `models`, `store`, `decay`),
the chain with its cryptographic identity (`chain/`) and the continuous
learner. The routes `/v1/host/world/*`, `/world-model/*`, `/beliefs`,
`/beliefs/run`, `/beliefs/conflicts`, `/identity/status|info|init`,
`/chain/verify`, `GET /learning/weights` and `POST /learning/engagement` are
gone, and so are the MCP server's `protagine_search_world` tool and
`protagine://world/entities` resource, which called one of them. So are
`protagine key`, `protagine node`, the chain step of `init`, `backup
--no-graph`, the identity-only backup, `restore --force-identity` and the
capabilities `consolidate`, `world_model`, `world_model_api`, `identity` and
`learning` (`context` is now always advertised). `HostIdentity` loses
`protagine_id`, `node_id`, `node_cert_fingerprint` and `trust_tier`; the
instance is named by `<state>/instance-id`, which adopts an existing
`protagine-id`, and backups record `instance_id` (an older archive's
`protagine_id` is still read). Node certificates are unsigned, and the
unused signer goes with its key-manager parameter. `/learning/correction`
answers `{accepted, correction_id}`, `forget` no longer reports graph or world
cleanup, and a summary-only `turns/sync` is skipped as `no_source_messages`.
The `graph` and `extraction` extras, `neo4j`, the briefings' Cypher-only
relationship aggregator and the `NEO4J` secret entries are gone, and so is
`docker-compose.yml`, which was the optional graph deployment (both of its
services demanded `NEO4J_PASSWORD`); `sidecar/Dockerfile` still builds the
sidecar image. `protagine upgrade` moves the retired state files (the belief,
chain and world-model stores, the chain's identity files, keys and
manifests) into `<backup>/retired`, and restoring an archive taken before
this release leaves them out (`retired_skipped` in the summary). Graph-only
code no route builds any more waits for the M10 audit, named in the known
gaps, and `benchmarks/source_recall` now drives the production recall path;
its reference numbers await one measured run. The sidecar package goes from
137,540 to 120,648 lines of Python; outside this changelog the change deletes 26,903 lines and adds
6,190.
## Unreleased - people

The mind now knows who people are and reaches out to them itself (build plan
M5, architecture 4.7). Every sender becomes a contact the first time they
write: the resolver ladder tries the exact transport handle, then the one
phone identity an E.164 address has on any gateway (C1: `sms:+1555...`,
`whatsapp:+1555...` and a custom phone app are one person; a bare digit
string such as a numeric user id matches only on its own gateway), then
makes a shadow contact at `may_contact: ask`, and a sender whose name only
suggests a known person is linked when the owner says so, through an ask.
`contacts.may_contact` (`never | ask | auto`, migration 006, one transaction)
replaces `interaction_allowed` and the tier defaults: the owner raises it
through `protagine_people set_permission` or `protagine people permit`, and
a contact's opt-out only lowers it, from a phrase match on their own words (a
bare STOP, also behind a gateway's timestamp or sender header, "don't text
me", "stop the check-ins" and close variants, anchored so "don't text me the
file, email it" is not one) or the appraisal call's `opt_out` flag; the
owner's daily digest lists the opt-outs. `store.merge` (C2) folds one record
into another with its history: handles and ledger sources move through
identity receipts, the comms log, contact affect and the dropped record's
commitments (and every owner's message addressed to it) follow, group
memberships move, recency and counts fold once even when two merges race, and
a stopped merge can run again. Conversations are counted with a 30-minute
gap (C3), which is what a tier-only contact's estimated cadence is made of.

The social drive checks in with a contact that has an owner-set cadence or a
tier of `regular` or above, due one cadence after the last conversation or
send. Silence backs it off (the cooldown doubles per ignored check-in, up to
four cadences), a reply resets it, and a declining mood in the contact's own
turns holds it; replies and silence only order check-ins and never switch a
contact off. An owner can set a cadence and its matter in conversation ("check
on p-09 every week about the kitchen quote"): capture records it, the tick
sets the cadence once and every check-in to that contact carries the matter,
and permission stays the contact's own. The message is composed in one
tool-less call (`P/mind/compose.py`) from an enumerated purpose, the
contact's name, the topic and that contact's own context packet, never the
concern, its evidence or an owner turn, and only when the budgets would let
it go now; the text then passes the floor and the deny list. A message the
owner wants a named contact to receive ("if p-05 has not confirmed by 5,
tell them the booking lapses", or "tell p-05 the meeting moved" for now) is
a notice with the owner's own words or a check-in around the matter; the
owner's grant counts as `auto` for that recipient only, never over `never`,
and only when the owner identified them exactly, while a name the store
matched becomes an owner ask. Each contact talked with in the last day gets
a template digest, shown to that person as "About this person" (at most 600
characters) and read by the composer; it never carries what the owner set
for them. The owner's surface is the `protagine_people` tool (who, inspect
and link proposals for everyone; permission, cadence and merge for the owner)
and `protagine people`, over `/v1/mind/people`. With
`mind.faculties.people: false` all of this goes and nothing older:
`may_contact`, opt-outs and shadow contacts stay.

A guest's context is contact-scoped by construction and fails closed: only
the owner, identified by the key, gets anything else. The digest and the
recipient packet read only claims from the contact's own sources, so an owner
turn about a contact reaches neither.

Deleted, with their tests, routes, switches and docs: ToM2 and P8
(visibility, arcs, the recipient audit and simulator, exposure, eligibility,
levels), the ToM extractor, engagement and the environment-risk scorer, the
`intelligence/relationships/` package with its second tier vocabulary,
`delivery/` whole (its rate limiter had no caller; the back-off is the
social drive's), `identity_bootstrap/`, the conversation presence census,
owner-verified provisioning and the contact-policy routes, the unused
`IdentityResolver`, the legacy `/contacts/merge` and `/contacts/{id}/handles`
routes, the group auto-promotion switch nothing consumed, and the memory
provider's `protagine_record_affect` tool. `protagine upgrade` backs up and
retires their databases and the contact store's unread tables. The sidecar
goes from 137,540 to 125,728 lines of Python; the milestone deletes 24,157
lines and adds about 9,000, over half of them tests.

The paired benchmark's people family runs on the same code: the plugin arm
seeds its contact store from `contacts.json` and stamps it on the body clock,
an inbound session carries its sender, every arm gets one `send_message`
path to contacts (`paired-outbound-1`), and the served Mind has the people
routes and reads. A no-model walk of the family's dev split through the
arm's code passes every scenario with the right behaviour, and fails exactly
the four templates the faculty carries in `full-people`. See
[docs/RELATIONSHIPS.md](docs/RELATIONSHIPS.md) and
[docs/MIND.md](docs/MIND.md).

## Unreleased - evaluation families for the M4 to M9 gates

The pre-registered evaluation families of the proto-AGI plan land as seeded
dev generators under `benchmarks/paired/generators/`, each with its frozen
plan under `docs/proto-agi/families/` and its held-out templates kept outside
the repository. Their arms are built-in profiles: `full` and one ablation per
faculty (`full-drives`, `full-broadcast`, `full-people`, `full-affect`,
`full-opinions`, `full-semantic_recall`, `full-consolidation`,
`full-self_narrative`, `full-lessons`), each `full` with one `minus_<faculty>`
switch that the worker's mind section turns into `mind.faculties.<name>:
false`, and `full-plus-skills`; the arm-profile protocol is
`paired-arm-profiles-4`. A faculty whose code has not
landed yet still has its flag served, so its ablation is a no-op contrast
until its milestone. `mind-drives-1` (M4) adds the `selection` and `goal` body
oracles, `mind-people-1` (M5) the per-target `sends` and inbound `replies`
checks, and `mind-affect-1` (M6) decision-turn episodes graded on a JSON file;
its rules mechanism arm became the built-in `full-affect-plus-rules` with the
feelings milestone.

The opinions evaluation family `mind-opinions-1` (evals section 6.5, the M7
gate) ships as dev templates under `benchmarks/paired/generators/opinions.py`
with its plan in `docs/proto-agi/families/mind-opinions-1.md`: twelve
templates in four groups (pushback, pseudo-evidence, evidence, flawed-plan)
around one shape, a stance formed from seeded records under a stated rule, a
process restart, and a probe graded by `label_one_of` on the plan and the
deciding source id, with a checkpoint that proves the stance was formed before
any pressure. For it, a generated scenario may carry the frozen workflow
contract (`restart_before`, `snapshot_after`) and oracle `checkpoints`, the
loader and `cases()` pass them to the restart supervisor and the workflow
grader, a plan with restarts refuses an image without the workflow protocol,
and `Draw.source()` yields fixed-width `s-NN` source ids. Engine edits move
every family's dev split content hash while the scenario bytes stay the same;
the generators README records both.

The memory and identity families for the M8 gate arrive as dev templates:
`mind-memory-1` (`--family memory`, six recall types and two abstention
controls graded on an `answer.json` the probe asks for) and `mind-self-1`
(`--family identity`, a stance after a restart, a false and a true premise
about the agent's own actions, and self-reports graded against the action ids
the harness observed). Generated scenarios may now declare a process restart
(`workflow`, the frozen workflows' contract) and seeded history (`history`,
imported into Hermes `state.db` in every arm and into the Protagine ledger in
plugin arms before the first turn, without model calls); the plan refuses an
image whose worker lacks either protocol. The `self_report` oracle checks a
`{actions, reasons}` file for fabricated ids, missing actions and drive
labels. The LongMemEval_S anchor renderer
(`benchmarks/paired/anchors/longmemeval_s.py`) selects ten short-answer
questions per ability from a dataset fetched at run time and renders them with
their haystack sessions as history into an `anchor` split. The arms are the
built-in `full-semantic_recall`, `full-consolidation` and
`full-self_narrative`. The frozen plan is
`docs/proto-agi/families/mind-memory-1.md`.

The self-improvement family `mind-improve-1` (evals plan 6.8, build plan M9)
ships as `benchmarks/paired/generators/improve.py`: eight campaign designs
over invented procedures (procedure, retrieval and tool-misuse classes), each
a fifteen-day episode in one container with six training days whose verdicts
carry the right result, eight held-out probe days at fixed positions (six
warranted, an out-of-scope control and an unverified-rule control) and an
old-family probe embedded from the frozen guard set. Probes are workspace
files graded by the existing artifact checks, and each artifact spec carries
`probe` metadata so a campaign report can take the probe as its unit and the
campaign as its cluster. Its arms are the built-in `full-lessons` (the
comparator) and `full-plus-skills` (`full` with the one faculty that ships
off turned on, through the new `plus_skills` switch), beside `full` and
`base-curator`; the plan is `docs/proto-agi/families/mind-improve-1.md`.

## Unreleased - initiative quality: capture that lands before the mind decides

The self-initiative gate (`docs/proto-agi/PROTO-AGI-EVALS.md` 6.2) came out
level with a plain heartbeat, and the diagnosis found that most of what it
counted against the mind never reached the mind's judgement: the last turn's
commitment extraction was still in flight when the ticks ran, a later message
that re-timed or closed an item could not touch the row, and a contact turn
spent its whole iteration budget on tools that could only refuse. This entry
closes those holes. The tick now drains the capture jobs still pending before
the drives read the store (`CommitmentExtractor.drain`, bounded to 30 s on a
forced tick and 5 s on the timer, reported as `capture_drained`), the
extraction call gets an output budget of 1500 tokens and retries a cut-off or
unparsable answer at once, and the extractor may act on the person's open
items as well as create them: the prompt lists them by number with the
person's previous turns of the last hour as context, and the model answers
`reschedule` (earlier, later, or with no time as a hold), `complete` or
`cancel` against that number, applied through `store.update` and
`store.resolve` with a pointer and wording check. The prompt also states the
restraint the controls need (a stall changes nothing, a hold is never a
reminder or a cancel, an event-conditioned item or one with no clear time has
no deadline, an obligation between other people is not an item) and records a
heads-up the person asked for as `metadata.heads_up_at`. `store.update`
normalizes `due_at` the way `create` does, merges metadata instead of
replacing it, takes `clear_due_at`, and returns an overdue row to `pending`
when its deadline moves into the future; `PATCH /v1/host/commitments/{id}`
inherits the normalization (a malformed time is 422) and the merge, so the
plugin's snooze keeps a deliverable's content.

On the acting side a commitment the owner owes that comes due is a
`commitment_reminder` message to the owner (the description, the due time,
how overdue), not a board task, while one the assistant took on ("I'll send
you the report by 3pm") stays a task the agent performs; the benchmark identity now carries an
owner handle so the body can address it. A row with a heads-up time raises a
`commitment_due_soon` message before the deadline, and once that went out the
overdue reminder for the same row waits `mind.heads_up_grace_minutes` (30).
An approved intention whose commitment row was removed, put on hold or pushed
into the future is cancelled as the check's verdict, on the tick and on the
body's pulls, and while the row stays open its key is given back so the
obligation is raised again at its new time. The dispatched worker can no
longer certify its own delivery: the guard blocks
`protagine_resolve_commitment(fulfilled)` in mind runs, the body's outcome
report closes the row as `resolved_by: body`, and `evaluate_check` reads an
open row as "cannot verify" rather than "not achieved". The agent's
"dismissed" resolves as `obsolete` and appears in the extractor's list of
closed items, shown as withdrawn rather than as a bad extraction, so a clear
fresh commitment to the same thing can be recorded again.

The `protagine-memory` provider offers its direct tools on the owner's own
lane only; a guest session or a channel with no sender binding is offered
none of them, a call that still arrives is answered once with
`{"unavailable": true, "retry": false, ...}` (as is `protagine_memory_search`
with no resolved participant), an empty read on fresh state answers once with
`{"empty": true, "retry": false, ...}`, `protagine_list_goals` and
`protagine_get_patterns` are gone from every lane until their host routes
exist, and `protagine_record_affect` sends a `source` the route accepts
(it answered 422 on every call). See [docs/HERMES-ADAPTER.md](docs/HERMES-ADAPTER.md).

Instrument, as dated amendments to the pre-registered plan and applied to every
arm alike: an agent turn that spent its iteration budget but answered no longer
ends the episode (the report shows `native_turns` per arm), each episode's
`source_job_counts` also reports the capture queue at shutdown, and the
`mind-initiative-1` dev family grows to thirteen warranted and fifteen control
templates covering the 6.2 taxonomy, with the clock advance chosen per
template. Unprompted messages to a third party the owner named are deferred to
the people milestone, which builds the contact records and consent they need;
the dev template for them is expected to fail until then. See
[docs/MIND.md](docs/MIND.md).

Two measurements from the same campaign are applied here as well. The per-type
priority feedback counted an intention every time something reported on it: an
owner rating on top of the implicit verdict compounded, rating twice compounded
again, and the model's own claim that the owner had approved or acted on an
initiative boosted the type exactly like an owner verdict.
`TypeFeedbackStore.record` now takes the intention or initiative the outcome is
about (`source`) and keeps one contribution per `(type, source)`: the same
outcome again changes nothing, a different one replaces the earlier
contribution, and the multiplier is derived by replaying the type's current
contributions in first-recorded order under the existing 0.5-1.5 clamp, so
`multiplier()` and the `GET /v1/host/feedback` snapshot keep their shape; a
store written before contributions were keyed carries its learned multiplier
over as one baseline contribution the first time it opens.
`POST /v1/host/initiatives/{id}/respond` records only the disposal it just
stored (dismissed, snoozed, acknowledged), keyed by the initiative; `approved`
and `actioned` from the model still update the initiative and its history but
no longer touch the multiplier, since a boost comes only from the owner
(`answer yes`, `rate useful`). The router's `llm_router.cost` event never
reached anything (the call did not match `EventBus.emit`, the error was logged
at debug, and no router was built with a bus), so the method, the `event_bus`
parameter and the sentence in `docs/FUNCTION-ROUTING.md` are gone; usage and
cost stay on `LLMResponse`. The fixed cost the adapter adds to every model
request, counted with the served model's own tokenizer on the recorded
benchmark payloads, was 1,870 tokens on the first request of a turn (+33% over
plain Hermes against a +15% gate): 1,144 of tool schemas, 242 of system text
and 483 of per-turn context before any recall, with each turn's injection
replayed as history on every later turn. The `protagine-memory` provider now
offers only the owner's two writes (`protagine_resolve_commitment`,
`protagine_record_affect`); its four reads and `protagine_initiative_feedback`
are removed, because across 244 recorded episodes no read returned anything the
assembled context did not already carry, and all seven remaining schemas say
only what the model needs to pick the tool and fill it (12 tools in 1,754
tokens become 7 in 880). The static reading rules are said once in the
provider's system block rather than inside every turn's context, the
`pre_llm_call` hook adds its one-line clock only on turns whose prefetch
carries none, the "Who I Am" section, the per-turn preamble, the `[priority]`
tags and an all-idle "Work observed" section are gone, and the temporal brief
is one `describe_now` line. The provider tells `/v1/host/context/assemble`
whether Hermes still shows this session's earlier turns (`session_history:
intact`, `compressed` after a checkpoint), and selection then leaves out that
session's own quotations and conversation pairs, the one recall that can add
nothing; the packet default is 4,000 characters (was 6,000) and rendered items
drop the `display_id` digest. Projected on the same traces the first-request
delta is +677 tokens (+12 to +13%) under the benchmark's tool exposure and +20
to +22% with all seven tools registered; the gate itself needs a fresh run.
`tests/hermes_adapter/test_overhead_budget.py` pins the schemas at 3,400
characters and the two prompt blocks at 800 so the fixed part cannot regrow
unnoticed. See [docs/RECALL-HYBRID.md](docs/RECALL-HYBRID.md) and
[plugins/protagine-memory/README.md](plugins/protagine-memory/README.md).

An independent review of this work found nine defects, each reproduced and each
now pinned by a test that failed first. An update now has to name the row it
acts on exactly: its listed wording, and its listed deadline when two open items
share wording; an ambiguous or empty pointer is ignored and counted rather than
guessed, for reschedules as well as cancels. Every update is a compare-and-set
inside the store's write transaction, so an extraction that listed a row which
the owner or the body changed meanwhile leaves it alone and reruns once against
the fresh state. A created item records its counterpart, so a turn from that
contact sees and can move or close the owner's obligation to them, and the
reverse. Capture jobs run in order per person (an earlier job, including one
waiting in backoff, holds the person's later ones, in the worker and in the
tick's drain alike), so a cancellation can no longer finish before the creation
it cancels; the order is bounded so it cannot become a stall: a job backing off
after a transport failure holds the person's later jobs for at most 15 s at a
time, after which the worker retries it early and uncharged (only the scheduled
attempts spend its three tries), so one failed call costs a person's captures
seconds rather than its whole backoff. Each item records who owes the work
(`metadata.obligor`), and only what the owner owes becomes a reminder. The CI
probes follow the contract: the scripted model answers `commitment_extract` in
the current shape (`action`, `target`, `listed_due`, `counterpart`, `obligor`)
for the audited turn only, and the M2 acceptance list has the owner ask for each
deliverable and the scripted assistant promise it, so what is captured is the
assistant's own commitment and forms a task. The `protagine-memory` provider's
standalone fallback for Hermes' recall-skip rule (`is_trivial_prompt`) mirrors
the host's, and a test checks the two agree against the qualified Hermes. Reminder, overdue and heads-up keys
carry the schedule they belong to, so a moved deadline earns one new reminder
and a repeated move earns nothing at the abandoned time; a reschedule shifts an
absolute heads-up by the same amount. A reminder with no resolvable owner
handle is refused before it is claimed (`409 no_target`) and goes out once the
handle resolves, and a message that expires unsent frees its obligation instead
of losing it. The body grader checks the target of every counted effect.

A rehearsal of the upgrade from 1.9.0 on a copy of a long-running install found
what the cutover would otherwise have carried by hand in a service environment
and a few request shapes 1.9.0 callers still send; each item is now the
package's own business, pinned by a test that failed first. `protagine.yaml`
carries the recall settings: `router.rerank_url` and `router.rerank_model`
reach recall the way `router.embed_url` does (an endpoint without its model is
a configuration error), `router.embed_dims` names the embedding width or, left
at 0, lets the OpenAI-compatible provider learn it from the endpoint's first
vector and hold every later one to it (a declared width that differs fails
naming the setting rather than a built-in default), and a top-level
`environment` mapping exports any further `PROTAGINE_*` tuning an operator has
calibrated (a reranker prompt style, a recall floor, an endpoint key), laid
over the values the keys derive and under the process environment, which still
wins; a name another key already owns is refused naming that key, values are
strings or numbers, and the log withholds anything that looks like a
credential. An embedder or vector store that does not come up is no longer a
silent fall-back to keywords: `/v1/host/health` gains a `problems` list that
says in sentences why the status is not `ok`, `/v1/host/embed/health` repeats
the reason, and `protagine doctor` fails its `semantic-recall` check whenever
`router.embed_url` is set and the running embedder is not serving. The
temporal health check tracks what this line runs: the mind beats
`last_tick_at` on every tick, on or off, a tick older than ten of its intervals
(floor a quarter hour, `PROTAGINE_STALE_TICK_HOURS`) or a capture job waiting
longer than an hour (`PROTAGINE_STALE_CAPTURE_HOURS`, from a new `enqueued_at`
on `commitment_runs`) degrades with its reason, and sync and prefetch silence,
which only the conversation drives, is reported under `temporal.silence_hours`
and never flags; `last_initiative_at`, `PROTAGINE_TEMPORAL_HEALTH_POLICY` and
the sync, initiative and prefetch staleness knobs are gone (an upgraded
`telemetry.json` drops the key on its next persist), and `protagine service
start`, `service status`, `init` and `upgrade` treat an answering sidecar as
ready and repeat the served verdict with its problems in words instead of
raising against a degraded one. The generated launchd and systemd units ask
for the 16,384 open files the vector store needs and `protagine start` raises
its own soft limit to that figure within the hard limit (`doctor` warns below
it). The base package brings the vector store itself (`lancedb`, `pyarrow`,
`pandas`; the `lancedb` extra is gone and `vectors` keeps only the in-process
models), the sidecar's interpreter range is the adapter's (`>=3.11,<3.14`),
`init`, `upgrade` and `doctor` refuse an environment without the vector store
with the reinstall command, and the install guide says how to pick the
interpreter when the default is newer. `protagine upgrade` adopts the durable
transport intake rows an earlier line stamped with one of several client
principals: every `transport_ingress` receipt and coverage row whose producer
is not the instance's is re-scoped to it after the backup (a receipt whose
event already exists under the instance producer is kept as it is; coverage
merges to the newest observation per account), so a messaging transport that
journaled those receipts can still read, hand off and settle them with the one
key, and `admit` recognises a journaled event by the event itself rather than
by its digest. Two request shapes are met halfway: `POST
/v1/host/memory/search` takes `person_id` and `session_id` as optional (with
the key and no person the search is the owner's, development mode never
resolves to the owner, blank counts as absent) and clamps `limit` to 20 instead
of refusing it, and a request the sidecar refuses (a `turns/sync` with an empty
session, a naive `occurred_at`) is answered `422 invalid_request` with the
reason by one handler for the package's own `ValueError`s, installed in
`create_app` and the test applications alike, while a library's error about
the sidecar's own data stays a server error. See
[docs/INSTALL.md](docs/INSTALL.md).

## Unreleased - drives, concerns, deliberation and agent-owned goals

One ranked producer replaces the parallel producers of self-initiated work.
Every tick the five drives (`P/mind/drives.py`: duty, curiosity, mastery,
upkeep, and the social framework that proposes nothing until the people
milestone) read one snapshot of stored state and raise concerns in `mind.db`
(`P/mind/concerns.py`): repeats bump by `dedup_key`, salience decays with a
12 h half-life, the workspace holds 24 concerns, and anti-rumination scales
salience by 0.9 after progress and 0.6 without. The top 3 concerns are the
broadcast set: the only deliberation candidates, rendered in the owner's Mind
section (at most 600 characters) and added to the recall query. Deliberation
(`P/mind/deliberate.py`) makes at most one tool-less router call per tick, for
open-ended concerns only; templates cover the rest, and an active intention
is reconsidered only on a matching event. Curiosity and mastery may adopt an
agent-owned goal (`P/mind/goals.py`, at most `budgets.open_goals`), whose
steps run as kanban tasks with `goal_mode` and which closes when its check
passes, its task budget is spent or its horizon passes. A finished research
task stores its finding as an autobiography entry a later turn recalls; a
done outcome satiates its drive. Duty reads stale owner tasks and stalled
Hermes goals from the body's board observations. Each faculty has one binary
flag (`mind.faculties.drives|deliberation|goals|broadcast`) and each drive
weight can be 0; the paired harness gains the `full`, `full-drives`,
`full-broadcast` and per-drive arms. New: `protagine mind concerns|goals|
interest`, `GET /v1/mind/concerns|goals`, `POST /v1/mind/interests`, and
`agent.interests` in `identity.yaml`. See [docs/MIND.md](docs/MIND.md).

Deleted with their tests and docs, as the build plan's M4 sweep: the
initiative engine and the self-directed thinker, the rest of the `protagine.cognition`
package (external events, charter, trigger, prompt), the cognitive workspace, the thinker and
the event-concern reducers, the `/cognition/*`, `/self/workspace*` and
`/surprises*` routes, the surprise store, the goal subtask and DAG tables, the
sidecar's direct reads of Hermes' kanban database (the work view now reads the
body's board observations), the native review and native kanban binding
routes of the old plugin, and the legacy perspective tables (owner-preference
capture stays). `protagine upgrade` moves the retired stores into the backup
and drops the retired tables after it.

## Unreleased - the dormant executor and the ceremony are gone

Nothing on a default install ran the second executor once the mind loop
owned dispatch, so it is deleted rather than kept dormant: the task queue
and its router, the embedded worker node and scheduler, the worker governor,
the packaged worker daemons (`protagine-agent-bridge`,
`protagine-queue-worker`, `protagine-skills-sync`, `protagine-worker`) and
the in-process agent bridge, the standalone `protagine-hostworker`
distribution, governed actions with their ledger and backup posture, work
orders and execution results, the project engine and planner, the reasoning
loop with its native `read_file`/`web_search` tools, the executor skills,
the P3 cognition spine, the receipt-derived evidence pipeline and the P7
drive governance that only fed projects into that queue. The governance
ceremony went with it: directed actions (HMAC webhook, dry run), the
seven-layer response gate, the response guard with its audit ledger, surface
policy, taint registry and context provenance, the directive store with its
fuzzy matching and global-pause directive, and the context gate. Every
effect goes through the mind's intention -> authority -> body path; Hermes
approvals and the plugin guard mediate it; a standing "leave X alone" is
`mind.deny`, and a global pause is `protagine mind off` plus `hermes pause`.

What a non-autonomy reader still needed moved: the environment-risk
classifier (`tom/env_risk.py`), the research review scans
(`research/review.py`), the router's token estimator (`router/tokens.py`)
and skills memory's term normaliser. `protagine upgrade` backs up and
retires `task_queue.db`, `protagine-projects.db`, the cognition stores, the
`governed-actions/` directory, and the directive, directed, guard-audit,
provenance and taint stores. The doctor's authenticated probe reads
`/v1/mind/state`. The sidecar goes from 191,751 to 143,767 lines of Python.

Review fixes on the sweep: the tests of what survived come back (the
external event intake's schema, restart replay, receipt and journal pruning;
contact scope lifecycle and promotion; the router's token estimator, now in
`tests/test_router_tokens.py`); `protagine upgrade` also retires the agent
bridge poller's `bridge/` seen-lists; the environment example, the charter
roles, the reconciliation note and the ignore list no longer name the removed
workers, queue, gates, directed actions, cognition spine, projects, drive
governance or release pins.

## Unreleased - the first closed loop (duty initiative)

The mind runs one loop end to end on a default install: a commitment in a
turn is captured by the projection worker's `commitment_extract` router task,
becomes an intention when it is overdue, passes authority and becomes a
Hermes kanban task (assignee `protagine-act`, idempotency key `mind:<id>`) or
a message the plugin sends verbatim, and its observed outcome feeds learning.
The initiatives table is the intention store and the only audit log
(`protagine mind log|why|stats`). Asks live only in the sidecar with a short
code, a notice at most every 4 hours and the daily digest; the owner answers
with `protagine mind yes|no <code>` or in chat through `protagine_self`;
silence expires an ask after 72 hours and nothing else waits on it. The off
switch (`protagine mind off`, `/mind off`, `POST /v1/mind/off`,
`mind.enabled: false`) means no further effects and works with the model
endpoint down. Expectations are on by default; every act is a prediction
scored hit or miss. See [docs/MIND.md](docs/MIND.md).

Review fixes on the loop: a cron run (a stored prompt, not the owner typing)
can no longer answer an ask or mutate through the tools; the body writes to
the board only in the process that owns the kanban dispatcher and holds
dispatch and sends while `hermes pause` is engaged; a `mind:*` task archived
before its ack is settled instead of recreated; `autonomy: off` stops
existing effects like the off switch; a delivering cron job's recipients get
`may_contact` and the message budgets; a task Hermes parked after its last
retry is a final failure that frees its slot; a promise captured after its
deadline is imported overdue instead of dropped; an intention whose source
resolved meanwhile is cancelled before it acts; handles that name a user
(Discord) send to the DM the gateway has had with them; the guard reads the
stock `send_message` target syntax; suggest-level asks are digest-only.

The four dead paths are fixed: commitment capture no longer needs a private
endpoint, `/internal/deliver` is replaced by the outbox, the feedback
multiplier is applied at the one ranker (a dismissal drops a type below the
act threshold on its own), and the loop ticks by default.

Deleted with their replacements: the autonomy loop, its scheduler and
condition worker (`P/autonomy/`), the proactive delivery bridge, the queue
approval ledger with its bounded grants, standing approvals, relay canary,
approval policy and action registry, native reviews and follow-ups, executor
retirement and the initiative-work router. The task queue keeps a direct owner
decision on approval-held jobs (`/jobs/{id}/approve|reject`), signed by the
server; `agent_action` jobs declare their risk. `protagine upgrade` moves
`approval_authority.db`, `schedules.db`, `standing_approvals.json` and the
delivery bridge's stores into the backup and adds the intention columns to
`initiatives.db` in place. Config gains `mind.act_threshold` and
`mind.digest_hour`.

Both halves are checked against each other: the router accepts the body's
bound acks, outcome reports (kanban status plus `outcome` and `final`; a
requeued failed run is progress), sent reports and board observations as the
body posts them, answers a second `sending` claim with 409 and marks a claim
nobody settles `uncertain`, and `POST /v1/mind/decide` is the owner's ask
answer with the owner and code checked again; the body reads the sidecar's
recipient handles and the handle list `protagine init` writes. The
`commitment_extract` task now hands the router the named object schema it
requires (the array schema was refused on every real router, so no commitment
was ever captured on a default install). A cancellation while the mind is off
is not a dismissal. `scripts/ci_mind_loop.sh` plays the M2 acceptance list on
stock Hermes in CI: a real gateway with the benchmark capture platform and a
loopback webhook route, a scripted model, `protagine init` from the built
wheels. The paired benchmark gains the `protagine-initiative` arm (the mind
on, only the initiative faculty) and its body tick calls the plugin's
`tick()` before cron and kanban dispatch in every arm.

### The body

The adapter's body thread now runs the mind's effects against `/v1/mind` on
stock Hermes (architecture 6.2): it pulls the dispatch queue and creates one
kanban task per intention on the `protagine-act` profile with idempotency key
`mind:<id>` (a lost acknowledgement or a restart never creates a second task),
sends outbox messages verbatim through stock `send_message_tool` with
`sending` / `sent` bookkeeping on both sides (a send interrupted before its
confirmation is reported `uncertain` and never repeated), reconciles every
`mind:*` task's state and last run into `POST /v1/mind/outcome`, archives
`mind:*` tasks that no dispatched intention owns, posts board observations
(stale owner tasks, blocked tasks, goal tasks, the mind's own tasks and a
heartbeat), and, with the mind off, archives unstarted mind tasks without
touching a model. Its own ledger lives in
`<hermes_home>/state/protagine-body.sqlite3`. `protagine_self` gains
`state`, `rate` and typed-code approval (`yes`/`no` go to `POST
/v1/mind/decide` only from the owner's own non-worker session whose message
contains the code); `/mind` reads `state`, `log` and `why/{id}`; the guard
reads `{allow, reason}` from `POST /v1/mind/guard {tool, args, session}`.
See [docs/HERMES-ADAPTER.md](docs/HERMES-ADAPTER.md).

## Unreleased - stock install and one key

Protagine attaches to stock Hermes (`hermes-agent >=0.21.3,<0.22`) through
its public plugin and memory-provider seams. The patch bundle, the prepared
runtime, the capability receipts, the scoped keyring, the contact grants and
the autonomy presets are removed. Install is `pipx install protagine`,
`protagine init`, `hermes gateway restart`; update is `pipx upgrade protagine`,
`protagine upgrade`, `hermes gateway restart`. See [docs/INSTALL.md](docs/INSTALL.md).

`protagine init` writes `protagine.yaml`, `identity.yaml` and `api.key` to the
instance directory (`$PROTAGINE_HOME`, default `~/.protagine`), installs the
matching `protagine-hermes` adapter into the Python of the `hermes` executable
and runs `pip check` there, writes the Hermes keys the adapter needs, creates
the `protagine-act` worker profile and installs and starts the sidecar user
service. `protagine init --uninstall` leaves stock Hermes behind, moving the
worker profile into the backups and keeping the adapter package while another
profile of the same Hermes home still enables it. `protagine upgrade`
takes a backup, applies the SQLite migrations, upgrades the adapter, reconciles
the Hermes keys and restarts the sidecar service; it is a no-op when nothing
changed, and it converts a 1.9.0 instance (a live keyring credential to
`api.key`, `.env` values to `protagine.yaml`, private-directory forwarders into
the backup, autonomy at `suggest`); 1.9.0 reminder launchers keep working.

The API has one key. Every plugin request sends `Authorization: Bearer <key>`;
the person a request acts for comes from the contact identity in the body. A
non-loopback bind without a key is refused; without a key the API serves
loopback callers only. `protagine doctor` is trimmed to install checks. CI
tests against stock Hermes v2026.9.14 and a nightly job against the latest
upstream release.

The Hermes plugin is a thin adapter on stock seams (about 1.5k lines): turn
capture through a durable SQLite outbox that the body thread delivers, the
`pre_tool_call` guard (every effect asks the sidecar; V4A patch targets, child
task workspaces and every cron recipient field are checked), `/mind`
(read-only plus `off`), the self, people, memory
and reminder tools, and `flush()` for hosts that end right after a turn. Both
the plugin and the memory provider read the owner's contact from
`protagine.yaml`. The release is 1.10.0 for both distributions so that
`protagine init` can never resolve `protagine-hermes==<version>` to the
previous plugin.

The paired benchmark runs named arm profiles instead of a fixed pair. A profile
says whether the Protagine plugin is installed and which flags are overlaid
after the fixture's forced flags; two to eight arms rotate their order by
episode and repetition, the same profile can run twice for an A/A noise floor,
and a declared reference arm is the comparator. Plans freeze the arms,
profiles, reference arm, sampling temperature, scenario seeds and decision rule
into the comparison key. Reports add scenario-level statistics for every
contrast: an exact sign test, a seeded cluster-bootstrap interval, the minimum
detectable effect and a pre-registered verdict, plus arm-episode and
wall-hour accounting. Existing plans, datasets and results report unchanged.
See [the paired benchmark guide](docs/PAIRED-AGENT-BENCHMARK.md).

Episodes gain body events: inbound contact messages, owner reactions, clock
advances and body ticks. A tick runs the arm's own step, Hermes cron and the
kanban dispatcher with workers in-process, identically in every arm, and every
outbound message lands in a JSON-array capture outbox that is graded per tick.
Two comparator profiles join the built-ins: `base-heartbeat`, a cron job in
Hermes' heartbeat wording with the `[SILENT]` convention that fires once per
tick, and `base-curator`, which runs the curator pass at every tick. Seeded
scenario generators under `benchmarks/paired/generators/` render template
families with fixed-width contact ids into byte-hashed datasets that
`--dataset-dir` freezes into a plan; held-out templates are read from outside
the repository. The dev family `mind-initiative-1` ships with warranted and
control templates.

The self-initiative family is made a valid instrument. Its setup turns are
statements in plain words that say nothing is needed now, never requests that
send the agent after a cron or clock tool it does not have; the background
state a scenario needs (contact records, a contact's complete reply, the
horizon) is seeded as a workspace file, an `inbound` event and an
`advance_clock`. Generated families declare `tool_loading: eager`, and the
worker writes the stock Hermes key `tools.tool_search.enabled: off` into every
arm's config so `session_search`, `todo_list` and `cronjob_manage` are loaded
directly instead of behind the `tool_search` bridge that spent the frozen
iteration budget; the plan records it as `comparison.tool_loading` and
refuses an image whose worker lacks the protocol. The frozen datasets keep
stock loading and their hashes. The dev split's content hashes for seeds 7 and
11 are pinned in the generator README and tests. Generated families also
declare `message_timestamps: gateway`: every owner turn, inbound message and
cron (heartbeat) prompt is prefixed with the body clock in the stock gateway
message-timestamp format, in every arm, because the stock system prompt gives
only the conversation's start date and sends the model to a terminal for the
time; the plan records it as `comparison.message_timestamps` and each attempt
records the applied mode. Frozen datasets keep bare turns. They also declare
`environment_note: messaging`: every turn's system message and every cron run
carries the same short description of the body (a messaging session whose
messages carry their arrival time, `p-NN` ids are contacts listed in
`contacts.json`, no terminal, clock, timer or scheduler tool), recorded in the
plan as `comparison.environment_note` with its text hash; frozen datasets
carry none. The dev family's reply-arrived message is the contact's answer
itself and names no attachment or file.

## v1.9.0 - consolidate execution and qualify runtime contracts

Remove the duplicate no-host initiative executor and its startup, preset and
status surfaces. Hermes keeps ownership of registered reviews and accepted
work. Unsupported initiatives remain proposals. An offline reconciliation
command preserves completed records and prevents automatic replay of unfinished
or failed retired-executor work.

Historical procedure win/loss counters are archived once in the existing skill
database and reset. Retrieval and retention no longer treat runtime completion
as proof that a procedure worked. Native evaluation receipts remain separate.
See [migration details](docs/EXECUTOR-RETIREMENT.md).

Attachment checks actual Hermes capabilities in an offline disposable profile.
Stock 0.21.3 lacks required core contracts. The candidate now ships versioned
patches applied to exact official source in a separate runtime, with file hashes,
original attribution and native regression tests. No fork or upstream acceptance
is required. `protagine init --prepare-hermes` prepares this runtime; `protagine
hermes prepare`, `check` and `run` expose preparation, inspection and the selected
instance launcher. Existing services are switched through their normal lifecycle.
Unknown upstream revisions require a newly qualified patchset and leave the active
runtime in place. These commands are new in 1.9.0.

Release CI applies patches from the built wheel to official Hermes and runs the
complete adapter suite and isolated native patch regressions. The initial bundle
passed all five capability groups, 358 native tests plus 10 subtests and 18
installed-adapter memory, reminder, task and review checks. The daily latest-stable
job reports compatibility gaps without changing a deployment.

Release CI builds, qualifies and publishes the same wheel, source and container
artifacts. Installed checks include dependency consistency and the standalone
hostworker contract. A constraints snapshot and focused lint/type checks make
the release environment reproducible.

Fixes found in review before release:
- Task queue deadlines are stored and compared as UTC instants. A job with a
  timezone-naive deadline no longer stops every worker from claiming work, and
  deadlines written with an offset no longer expire hours early or late.
  Worker outcomes that always fail are retired after a fixed number of attempts
  instead of blocking newer outcomes.
- A turn reservation abandoned by a killed process is reclaimed by an identical
  retry after five minutes, so the turn is ingested instead of retried forever.
- The memory provider performs one bounded context assemble per turn, never
  waits on a stale background prefetch, and honours its circuit breaker for
  prefetch, temporal context and contact resolution.
- Shutdown stops the autonomy loop and background workers before closing stores.
  A subsystem health check reports degradation instead of claiming a fix it did
  not make. Tool calls record the real caller instead of always the owner.
- Failed rows of the retired executor are cancelled and never reactivated. A
  failed first runtime preparation no longer blocks later `protagine init` runs.
- Without an API key the API serves only loopback clients with a loopback Host
  header. Agent updates and deletion need the same protection as registration.
  Image embedding accepts bytes, base64 or public URLs (validated address,
  size cap); arbitrary local paths are no longer read. Skill subprocesses run
  with a minimal environment. The unused channel delivery webhook field is gone.
- Source erasure compacts vector tables and removes old versions so erased text
  leaves disk. Lexical recall rebuilds source envelopes only for candidates it
  uses.
- Publishing a release requires the test suite. Leftover environment names from
  earlier project names are removed.

A completed paired evaluation can contain failed tasks. Its CLI now reports
execution success separately from model quality, preventing campaign tools from
mistaking a valid negative result for a failed evaluation.

Native Hermes owner-memory blocks are removed from guest and unresolved
participants' outgoing requests, including summary calls. Owner files are
unchanged. A real model-swap demo caught this boundary defect and verified the
repair alongside corrected recall and continuation of an existing task.

The candidate preserves chronological conversation reads and exact observation
retries already used by the reference deployment. Embedding health checks verify
the active index's full identity, vector width and bounded storage reads. Routine
readiness no longer scans every memory record; errors and timeouts still report
degraded health. Exhaustive model-label discovery remains available for audits.

Remove the duplicate goal planner, conversation-synthesis writer, unused tier
learner and retired compatibility helpers. Existing goal records, named model
roles and configured fallbacks remain. Snoozing repeatedly no longer abandons
a goal. Graph baseline reads and updates use their actual queries.

Recent-memory reads run outside the request event loop. Channel indexing
normalizes accepted platform names. Hermes preparation verifies the full source
tree before reuse, and launch checks the instance's required feature groups.
The concurrency probe rejects serialized callbacks. Native CI retains the built
release wheels. Benchmark requests pass credentials through stdin without
persisting the request payload; general CLI help does not require POSIX locks.

An optional [shared inference capacity](docs/INFERENCE-POOL.md) boundary admits
OpenAI-compatible requests from native Hermes, Protagine and other clients
through one proxy. It holds excess requests outside model servers, balances
qualified replicas by estimated work and reserves request and token capacity
for configured traffic classes. Endpoints and limits are deployment
configuration. Existing callers are not switched to it.

Components: `protagine` 1.9.0, `protagine-hermes` 1.9.0 and
`protagine-hostworker` 0.3.1. This release does not establish an overall
performance advantage over Hermes or completed autonomous self-improvement.

## v1.8.7-hermes - support strict chat instruction layouts

The Hermes adapter now sends consecutive plain instruction blocks as one
leading message. This fixes Qwen templates rejecting Protagine requests with
multiple system messages or work instructions after the conversation. The
change applies to any compatible chat-completion endpoint, without a model-name
override, serving-template edit or Hermes core patch.

Instruction text, user and tool content, stored history and participant scopes
are preserved. Native summary calls use the same layout, and the memory check
cache records the final outgoing request. Structured provider content, distinct
system/developer roles, Responses and Anthropic layouts keep their existing shape.

Real Qwen validation completed eight Protagine turns without the previous
template error. Owner-authorized tool access and contact recall passed. The six
semantic-recall trials still expose separate retrieval, output-contract and
grounding failures; compatibility is not a model-quality qualification.

This updates `protagine-hermes` to 1.8.7. The sidecar remains 1.8.6 and hostworker
0.3.0. Upgrade the adapter in the Hermes environment and restart that gateway
to load it.

## v1.8.7.post1 - acknowledge retried execution observations

Exact retries of an accepted execution observation now return a duplicate
acknowledgement without reopening completed work or extending its lease. The
sidecar-only update preserves the existing execution schema and adapter.

## v1.8.7 - read recent canonical conversations

Adds participant-scoped `POST /v1/host/memory/recent` for bounded chronological
conversation reads by platform. Selection uses recorded occurrence time and
canonical source revisions, preserving user/assistant attribution, corrections
and erasure checks without semantic ranking or a model call. Indexed channel
locators cover new turns; reviewed history and verified source-linked
communications locate existing sources without exposing unlinked summaries.

The response carries source and annotation receipts plus explicit coverage for
missing metadata, omitted corrections and result/content limits. Only the
sidecar advances to 1.8.7; the unchanged Hermes adapter remains 1.8.6 and
hostworker remains 0.3.0.

## v1.8.6 - diagnose retained model responses

Hermes response observations now retain stop reasons, text/tool/reasoning counts
and timing when available. `protagine models diagnose` reads an exact execution
through the existing scoped API and shows each requested and reported model.
Missing measurements remain unknown. No additional prompt or response text is
stored, and observations keep their existing seven-day operational retention.

This helps inspect empty responses, malformed tool names and route changes from
ordinary use. It does not distinguish provider generation from parser loss or
establish answer quality. No model request, routing change or new service is
required. Both the sidecar and Hermes adapter are 1.8.6; hostworker remains 0.3.0.

## v1.8.5-hermes - retain terminal outcomes for ordinary review

The Hermes adapter now retains completed terminal commands with a nonzero exit
code even when the tool returns no `error` field. Review receives the original
result and command, and recurrence requires matching evidence from separate
turns. Cancelled commands and error-like text inside successful output do not
count. A nonzero exit can be expected and does not establish a task failure or
a defect in a skill.

Ten focused tests passed. Replaying two original terminal results captured both
previously omitted outcomes without establishing recurrence. Useful learning
from later ordinary use remains to be demonstrated. This release updates
`protagine-hermes` to 1.8.5; the sidecar remains 1.8.4 and hostworker 0.3.0.

## v1.8.4 - embedding dimensions and commitment feedback

A rejected commitment ID now returns the precise rejection and clears that
unconfirmed local attempt. It no longer appears as a server outage that blocks
unrelated tools. Uncertain responses retain their existing restrictions, and a
failed retry preserves a previously confirmed undertaking token.

OpenAI-compatible text embedding providers can request a specific output width
through `PROTAGINE_EMBED_REQUEST_DIMS`. The value must match the configured
response width. Requests omit the field unless it is explicitly selected;
unsupported providers and incorrect response widths remain errors.

The request option belongs to the embedding generation identity. Selecting it
requires a rebuilt index even when the vector width stays the same. Existing
configurations keep their current request payload and generation identity.
The client does not truncate vectors or infer support from a model name.
Docker Compose forwards the embedding endpoint, credential, revision and request
width from the deployment environment.

An isolated two-model trial with equal output widths passed the existing
migration, interruption, resumption, source-scope and erasure checks. That result
does not qualify general retrieval quality or change a deployment's model roles.

## v1.8.3 - memory quality and failed-proposal review

Memory retains a quoted preference or procedure when its applicability cannot
be resolved to a date. Its full conditions and source remain available in
recollection, history and relationship context. Retention does not mean that
those conditions apply now. Scalar facts retain their date validation.
Extraction uses an object response schema compatible with strict output rules;
source-reference alternatives no longer require a second generated quotation.

A future change keeps the preceding preference available until its recorded
end. It cannot extend an earlier expiry, and a change with an unknown effective
time does not silently take effect when the message is ingested.

Extraction and admission review share the same memory-quality criteria. Review
checks the complete meaning of a proposed property and its reason for future
recall. Both remain model judgments; a retained quotation is not proof of truth.

An ordinary native skill review can continue from a retained proposal failure
on a later firing of the existing job. Each original review allows at most two
linked successors. Original experience stays consumed, owner rejection stops
the chain, and every changed proposal still needs independent evaluation.
Failed proposal bytes and diagnostics remain in the native ledger. Successful
activation settles the exact superseded pending proposals.

Rejected task operations name their missing and unexpected fields before any
task is changed or dispatched. The request field also describes corrections to
existing tasks. This gives models a concrete error to correct without accepting
alternate argument names or retrying the operation automatically.

## v1.8.2 - task handoff and available tools

A conversation can finish by returning a task it already submitted in that
turn. `handoff` with the existing `task_id` preserves the instruction, selected
model and running work without submitting a duplicate. Current owner and source
checks still apply; a task that has ended remains available through `status`.
If the worker finishes while that acceptance is being returned, the conversation
still receives its task ID. The receipt does not claim a successful outcome.

Memory retention is offered when the current request contains an eligible
completed tool result. Task workers no longer receive ordinary-owner task
controls or foreground handoff instructions. Current schemas and tool discovery
agree on those capabilities, while original conversation history remains intact.
Generic work observations retain their task IDs, evidence and status without
assuming that every consumer exposes the same task tool.

Managed review workers now register the existing model-request observer, so
their requests can contribute to forecast evidence. Missing observations remain
explicitly incomplete; this change does not enable forecast-driven scheduling.

## v1.8.1 - clearer working context and memory selection

After a successful work refresh, model requests carry the current work view
without repeating the turn-start snapshot. If refresh fails, that snapshot
remains available with a notice that current work could not be checked. Source
attribution and the stored conversation remain intact.

The existing request context clarifies background handoff: return the actual
acceptance while the worker completes and verifies its assignment. This applies
when the native handoff capability is available, including in cached conversations.
It guides model behavior; acceptance still does not prove task completion.

Tool search and description results are excluded from persistent-observation
candidates. They remain available in conversation history and the current tool
catalog. Substantive tool results can still be retained and recalled.

## v1.8.0 - return control after background handoff

The optional native task tool now offers an explicit `handoff` operation. It
accepts the remaining request as background work and returns the actual task ID
through Hermes' normal conversation delivery, without another model call.
Ordinary `submit` still allows further foreground work. Later conversations can
inspect, steer or stop the same task.

This uses a small typed turn-finish interface in the qualified Hermes build.
It applies only after a single successful tool result is persisted. Mixed
batches, errors, interruption and pending steering retain their normal handling.
The reply records runtime provenance and claims acceptance only. Runtimes
without the interface keep ordinary submission and do not advertise handoff.

Task-outcome forecasts now label their configuration as observed at attachment,
with worker execution configuration explicitly unobserved. A model response
label does not prove which full configuration the worker loaded. Existing
forecast history, probabilities and scores stay intact.

## v1.7.2 - source references, task status and model qualification

Memory formation can retain a standing preference as its exact source quotation.
A generated paraphrase that is not a literal source span is discarded in favor
of that quotation and passed through the existing semantic admission review.
Conditions and exceptions remain attached. Quoted statements retain their source
history; differing wording alone does not establish a contradiction.

For a short text message, the extractor can select a source reference instead of
copying its quotation. Code resolves the reference to the original text before
the existing validation and review. Longer passages and audio retain their
existing source boundaries.

Native task status distinguishes dispatch in progress from an observed active
turn. Status includes its observation basis, and a retained worker answer is
identified as an assistant report with external effects still unverified.

Direct model qualification uses the selected binding's configured output
allowance, frozen in a versioned recipe before execution. It records the client
allowance, completion status and available token usage. Truncated replies retain
bounded partial evidence and remain incomplete results. Domain consumers keep
their own output limits, and qualification does not change deployed model roles.

## v1.7.1 - recollection and proposal repairs

Automatic recollection uses the general plugin's resolved identity for the
current owner API turn. Previously, explicit memory tools could recognize that
owner while the memory provider withheld automatic recall from the same turn.
The provider requires a matching active turn and owner; it does not treat every
API conversation as the owner.

Memory extraction and review both accept a single complete JSON code block.
Previously, a review in that format was rejected before its assertions could
be committed. Duplicate keys, incomplete decisions and invalid values still
fail validation; surrounding prose is not searched for a usable JSON fragment.

Background skill creation uses Hermes' own validators before staging a proposal.
Invalid content returns its error to the reviewer immediately, allowing a
correction during the same review. Previously, staging could report success for
a proposal that the later native evaluation rejected before any measurement.

Task-tool instructions now tell callers to preserve the complete deliverable
and its permission boundaries, keep child work inside the background task,
and return the accepted handle promptly. Following these instructions and
completing the task still depend on the model and need behavioral validation.

## v1.7.0 - ordinary skill learning and due reply reviews

Guided setup can schedule ordinary skill review through Hermes' existing cron.
Repeated tool failures retain their original evidence. An explicitly scoped
evaluator can qualify a proposed skill through the existing adoption, audit and
rollback path. Without an evaluator, proposals remain pending.

- The public adapter owns the reusable review implementation; deployments supply
  their configuration and optional evaluator.
- Native tool failures and externally supplied task assessments retain distinct
  source types. Neither is automatically treated as proof of useful learning.
- Due reply reviews use the existing bounded native worker. It reads current
  wait, parent and source state, then reports without sending or fulfilling the
  parent commitment. A reply or cancellation invalidates stale review decisions.
- Task status exposes acceptance and update times in UTC and age since
  acceptance, distinct from execution duration. Unknown age remains unknown.

Controlled native tests exercise these paths, including fresh installation and
regression rollback. Useful autonomous improvement and physical message delivery
still require observed deployment outcomes.

## v1.6.0 - task assessments in skill evaluation

The Hermes adapter can select complete reviews of distinct operational tasks
and pass a proposed procedure to an operator-selected evaluator. The existing
native proposal, evaluation and rollback mechanisms retain the change history.

- A scoped API returns current assessment sources with their full attribution
  and supporting evidence. Corrections and erasure invalidate affected reviews.
- Multiple reviews of one task cannot be counted as separate experience. The
  reviewer considers recurrence; selection does not declare a failure.
- An unavailable later audit does not hold up another pending candidate.
- The qualified Hermes 0.21.3 interface build reports nonzero coding-kernel
  exits as errors. Setup documentation now names the current qualified version.

The assessment consumer is an integration entry point. Guided setup does not
install its periodic learning cadence, and no automatic adoption is enabled.
This release does not establish useful autonomous learning in a deployment.

## v1.5.34 - exact message membership for artifact assessments

Task artifact assessments can supply optional message membership for their
source references. Valid membership lets a review proceed when a correction
affects only an unrelated message in the same source. Relevant corrections and
erasures still withhold assessments through the existing source checks.

- Membership is checked against current source messages and the execution's
  admitted inputs.
- Requests without membership retain the existing source-wide checks.

## v1.5.33 - retain artifact locators in task replies

Native task replies retain bare local file paths so callers can locate their
completed artifacts. The text-retaining task adapter no longer extracts these
paths as attachments it cannot upload.

- Explicit `MEDIA:` handling remains unchanged.
- Controlled native gateway tests verify exact reply retention and no inferred
  document upload through the existing handoff store.

## v1.5.32 - Hermes 0.21.3 compatibility

Guided installation and adapter refresh accept the qualified Hermes 0.21.3
runtime. Public CI selects the compatibility build based on upstream
`v2026.9.14`, including the existing Protagine interfaces and a correction that
keeps length-continuation text within its own segment across tool rounds.

Native first-attempt outcome measurement now distinguishes Hermes's initial
blocked creation event from a later intervention. An ordinary completed first
attempt can retain its measured outcome; real blocking, cancellation and manual
completion remain separately counted. Qualification uses controlled native
execution without live inference or changes to model policy.

## v1.5.31 - source attribution checks and retained task admissions

Native model qualification now includes a coding case that distinguishes the
interpreter launching a process from the interpreter selected by its current
configuration. It also checks current source against stale documentation.
Actual file reads and final claim correctness are graded independently: reading
the right files cannot pass an incorrect answer. The case uses generic fixtures
and the existing qualification runner; it does not rank models globally or
establish production fitness.

Task adapters can look up an existing admission by its exact request ID before
forwarding a trusted purpose. This supports preserving the original purpose,
including an unclassified one, when a request is replayed. The lookup does not
create work or bypass existing source, owner and model-role checks.

## v1.5.30 - task corrections and outcome measurement

A correction queued while a task is producing its final answer now retains
its source ownership when Hermes starts the next turn. The correction reaches
the model, and forgetting it removes the owned copy without deleting the
preceding task or unrelated later input. A source failure now marks the task
failed instead of retaining the model's stop message as a completed answer.
Controlled native gateway tests cover both mid-task and next-turn delivery,
source removal and terminal status. Live task behavior still needs validation.

Selected internal reviews can record a probability that their first native
attempt completes within 480 seconds of attachment. Measurement is disabled
by default and requires explicit enrollment in a finite window. Each forecast
retains its baseline and selected planning role recipe. A successful retry
cannot replace a failed first attempt; interventions remain separately counted.
A new measurement window can learn earlier comparable outcomes, while a changed
role recipe starts a separate statistical cohort.

The 144 focused store, setup and native integration checks passed. They verify
forecast issuance, independent outcome readback and comparison plumbing.
Live predictive benefit has not been demonstrated. Probabilities do not change
task scheduling, retries, notifications or authority.

## v1.5.29 - shared lifetime for native qualification fixtures

Trusted qualification consumers can prepare fixtures and collect evidence
through the existing native worker. Fixture resources stay available through
agent construction, execution and close. Consumers reuse the same child
process, cancellation and cleanup path; preparation and fixture cleanup
failures remain failed attempts.

Controlled tests cover alternate worker dispatch and fixture lifetime on
successful runs and constructor failures. These checks establish qualification
plumbing. They do not demonstrate improved model behavior or automatic
production learning.

## v1.5.28 - recurring failure evidence and readable model checks

Ordinary tool failures can be grouped across turns even when different skills
were viewed. Each execution counts once, and previously reviewed observations
remain consumed. Viewed skills stay attached as context without being blamed
for the failure. Deployment review consumers can reuse this grouping in the
existing learning path.

Model qualification reports now show each case's named pass, fail and unknown
checks alongside its role, consumer boundary and primary-model attribution.
Comparisons retain both sets of checks and their original grades. A useful
artifact no longer hides an answer-grounding failure in the Markdown summary.

These changes improve evidence selection and reporting. They do not establish
a successful autonomous repair or change model roles, prompts or evaluation
criteria. Hermes remains on the same qualified 0.21.2 interface.

## v1.5.27 - retained task failure and explicit continuation

A failed native background turn now retains its settled failure separately from
a completed answer, including failures before the ordinary completion hook.
Task status exposes that outcome with the original source ownership. An owner
can resume an eligible failed or interrupted task using its exact observed
native turn ID. The native gateway rechecks source access, ownership and the
current generation, retaining the same task, session, model binding and prior
tool results. Repeated or stale requests cannot start another continuation for
that generation; stopped, completed and suspended sessions are declined.

The guided Hermes installer and explicit adapter refresh append `protagine_task`
to the existing eager tool list, preserving other names, search options and YAML
aliases. This exposes only an already admitted schema and grants no additional
authority. Other deferred tools keep their existing discovery behavior.

Controlled native tests cover failed settlement, retained source references,
resume admission, duplicate prevention and generation changes. Installer tests
cover preservation and idempotence. These checks do not establish production
model task completion or recovery from a physical host outage.

## v1.5.26 - task-role selection and native image capture

Private channel adapters can reuse the native gateway’s existing profile role
resolver when admitting new work. The resolver reads current named roles, checks
the native provider and returns a credential-free role/provider/model snapshot.
Gateway task selection uses the same implementation. Defaults, in-flight task
bindings and configured provider behavior are unchanged.

The actual native task fixture covers transport reads, profile rebinding,
removed roles and retained task processors. Model suitability and latency still
require measurements on the deployment’s chosen processors.

The qualified Hermes build fixes source anchoring for image-bearing turns,
whose stored transcript uses text and image markers. Integration checks now
exercise native persistence, request observation and the canonical outbox for
both text and image inputs, including unavailable ownership storage.

## v1.5.25 - shared task visibility across conversations

Every source-bound native task keeps an inspectable task ID even when its
admission has no learning classification. Unclassified tasks remain excluded
from operational learning. An open task whose latest callback has expired
stays visible before older results, with its liveness explicitly unknown.
Erased task inputs no longer leave a completed status target in shared context.

The qualified Hermes build adds optional `agent.image_input_mode:
native_if_supported`. It sends original images to a capable current model and
retains the configured auxiliary route for models with false or unknown vision
capability. The default mode and existing deployments remain unchanged.

Validation covers native admission, cross-session work selection, quiet calls,
source erasure and separation from operational learning. These checks do not
establish answer quality or completion of the unified-agent goal.

## v1.5.24 - native reasoning qualification and direct tool visibility

The native model suite now offers a file-reading reasoning case. It requires
opening original records and a correction through Hermes, applying the corrected
rule and giving a grounded decision. A correct answer without the reads fails,
as do attempted file mutations and invented execution claims. Nested JSON
comparisons preserve types, including integer versus boolean.

The qualified Hermes interface supports an optional list of admitted tools to
show directly. This keeps selected memory or task schemas available without a
discovery round while leaving other tools deferred. Existing tool permissions
and middleware still apply. The installer preserves the deployment's selection;
this release does not claim a measured production latency improvement.

## v1.5.23 - simpler source reads and reasoning handoffs

The native source reader can reuse the exact revision already supplied to the
current turn when the caller gives its source ID. Explicit versions, ambiguous
references, source ownership and page revision checks keep their existing
meaning. This avoids asking the model to repeat an otherwise redundant hash.

Task guidance now distinguishes routine direct answers from difficult reasoning
that can use a declared background role while conversation continues. This is
guidance for the existing task interface, not a new router or an established
answer-quality improvement.

Validation: native source opening, pagination, annotation, changed identity and
erasure checks passed, along with existing task controller checks.

## v1.5.22 - current task revisions across conversations

A foreground owner conversation can receive the latest accepted revision of a
shown native task alongside its original purpose. The revision carries separate
acknowledgment and model-request visibility flags; neither proves the requested
behavior was applied. Original and correction sources must pass the existing
owner, currentness, annotation and erasure checks before dispatch.

One revision shares the existing 4000-character, eight-record context budget.
Conversations without a revision retain the full work view. Enrolled voice inputs
reuse canonical source resolution when their exact input hashes still need a
source revision. Optional owner lookups share the existing request deadline.

Native request and enrolled voice fixtures qualify these boundaries. Accurate
foreground answers and physical-channel behavior still require observation.

## v1.5.21 - task context and ordinary failure reviews

Shared work context keeps the latest native task inspectable after its turn ends.
An interrupted task retains its actual status and existing result reader. When
space permits, a compact excerpt of a running task's original request precedes
idle reports. Source references, partial excerpts and the existing context limit
remain explicit. Task status also exposes accepted
updates for a conversation that needs the latest correction.

Ordinary tool failures can now enter the existing Hermes review ledger when no
skill was viewed. Repeated failures from distinct ordinary turns can justify a
review without inventing a skill attribution. Qualification tasks stay outside
that experience stream. A deployment consumer can constrain the resulting review
to a proposed new skill; this constraint is enforced before proposal staging.

These are source-context and review-path changes. They do not establish useful
passive learning or completion of the unified-agent goal.

## v1.5.20 - current tasks and evaluated skill creation

Fresh native tasks retain space in shared work context before idle status reports
and blocked board records. This repairs an observed omission during a separate
conversation about a running task. Parent links, source references and the
existing context budget remain intact. Coverage labels unfinished records as
open, since a retained record does not prove that a process is running.

The native skill evaluator accepts staged creation of one new SKILL.md. It
compares the proposed skill against an absent-skill baseline and requires
measured improvement before applying it, followed by a separate audit. The
existing Hermes ledger records the actual creation and owns rollback. An
interrupted apply can resume without adopting an independently created owner
file, and rollback preserves later owner changes.

These changes provide context and an evaluation path. Correct task answers and
useful autonomous learning still require behavioral validation.

## v1.5.19 - inspectable work across conversations

Shared work context now includes a native task handle and an exact reference to
its original request. Another conversation can inspect the task without searching
the filesystem. The latest finished task stays inspectable for seven days; its
terminal observation does not certify the quality of its result.

Task status exposes references to the original input and up to four recent,
currently authorized corrections. Each correction retains separate acknowledgment
and model-request visibility states. The normal memory source tool opens these
references, and existing source ownership handles forgetting.

Current-work questions no longer inject a static architecture catalog. The agent's
identity and current capability descriptions remain available. Questions about
what the agent is building now also select current work rather than old status
memories.

The qualified Hermes build reads active work from its attached gateway runner for
health responses. Adapters that declare no asynchronous delivery no longer show
home-channel setup prompts on their first conversation.

## v1.5.18 - partial corrections and active-conversation forgetting

A partial memory correction can preserve unchanged parts of the prior value.
Every carried portion keeps its original quoted source, report time and timezone;
changed portions come from the correction. The existing semantic review checks
the update before replacing the old claim. Recall and derived judgments keep
those dependencies, and erasure removes dependent projections.

After a source is forgotten, request processing removes earlier tool calls whose
arguments were generated from that source, along with their paired results.
The current user input, fresh forgetting receipt and unrelated calls remain.
Ownership comes from previously admitted source references and exact call
fingerprints; the adapter does not infer it from a matching word.
Equivalent JSON argument formatting keeps that identity when Hermes builds a
summary request. Changed arguments and ambiguous calls remain distinct.

Rejected source annotations now explain the specific correction needed, such
as copying one contiguous quotation. A rejected request is distinguished from
an unknown acknowledgement, which can still be retried with identical input.

The qualified Hermes build now applies its existing request middleware to
iteration-limit summaries, including retries and the supported provider modes.
This closes a path that previously bypassed memory reconciliation. The build
uses the same stable Hermes release with a narrow interface repair.

## v1.5.17 - forgetting source annotations

Memory annotations now retain their exact creating Hermes tool call. Forgetting
an annotation can remove its arguments, derived replies and tracked recalled
copies while preserving independent facts before the call. An identical retry
after a lost acknowledgement keeps the first creating call as its origin.

The annotation tool requires its own tool-call batch. If its native origin
cannot be identified, it reports that it submitted nothing. An uncertain server
response remains unconfirmed. Historical records with missing origins remain
pending; this release does not infer ownership or declare those records erased.

## v1.5.16 - corrected deadlines and clearer memory sources

Reminders now use the complete event expression retained with a memory claim.
A clock stored as `9:30am` can therefore keep its quoted `Monday morning` date
context. Relative deadlines use the source report time and show the timezone
and interpretation rule. Conflicting dates remain unresolved.

When Hermes has no timezone setting, the reminder uses the existing contact or
agent communication timezone instead of silently choosing UTC. It keeps the
selected timezone with the native cron job. Explicit timezone settings still
take precedence.

Recalled items now distinguish their display labels from canonical source IDs.
Search and forget tools identify the source IDs to use, and a rejected forget
request explicitly reports that it removed nothing.

For proposed corrections, claim validation restores the complete short source
quotation before review. This keeps a clipped quote from losing the words that
identify it as a correction. The existing review and claim update retire the
predecessor; an independent assertion remains an independent assertion.

Excerpts from task reviews retain their machine-assessment attribution. A quoted
artifact inside a review is not itself the review's conclusion. This improves
the evidence presented to a model; useful decisions still require validation.

## v1.5.15 - task reviews as learning evidence

An execution host can submit a retained review of a completed operational task
through the existing execution API. The review carries its artifact, original
inputs and source references into the existing judgment worker. It remains an
attributed machine assessment. Task timing and owner approval stay unchanged.
Corrections and forgetting invalidate the dependent judgment. See
[self judgments](docs/SELF-JUDGMENTS.md) for the host integration.

Starting the local sidecar now detaches its input from the setup terminal, so
setup can return while the service keeps running. The setup guide uses the
README's release commands to avoid a separate stale version pin.

Useful judgment formation and subsequent decisions still require observation
with the deployment's selected model.

## v1.5.14 - reminders that follow corrected memory

The `protagine_reminder` plugin tool schedules an existing recalled deadline through
Hermes cron. An explicit correction moves the same job; an obsolete occurrence
stays silent. Forgotten or unresolved evidence prevents a reminder from using
the old value. General schedules continue to use Hermes' native cron tools.

Source-bound output uses the existing ownership ledger and a small native cron
interface for retained files, queued delivery and mirrored messages. Delivery
that remains in flight keeps cleanup pending. Packaged setup tests also isolate
native workers from unrelated installed adapter entry points.
Later replies and native compressed summaries inherit the reminder's source
references, so forgetting also reaches those retained derivatives.

This release implements the path; ordinary channel usefulness and unattended
operation remain part of Phase 1 validation. See [source reminders](docs/SOURCE-REMINDERS.md).

## v1.5.13 - preserve transcript housekeeping during forgetting

Forgetting a source now leaves Hermes session housekeeping rows intact while
removing the related conversation payloads. Previously, cleanup could replace
an empty housekeeping row and clear its display ordering and identity. The
original user question, linked answers and tool results keep their existing
erasure rules.

## v1.5.12 - bounded image request receipts

An explicitly opted-in qualification task can retain bounded image hashes in
its existing gateway session metadata. Receipts identify the task and native
turn and observe the filtered request immediately before its provider callback.
They retain hashes, byte counts and structural positions, without image bytes,
URLs, prompt text or credentials. Ordinary tasks cannot enable this capture,
and an existing task cannot acquire the opt-in later.

These receipts do not enable global request dumps or establish network delivery,
provider acceptance or model perception. Missing image bytes, incomplete capture
and exhausted limits remain explicit qualification limitations.

## v1.5.11 - native source-copy erasure

Forgotten sources can now be removed from Hermes transcript rows and their
full-text search entries when Protagine has recorded their ownership. Authentic
source reads and supplied recall retain native input anchors and source revisions
in the existing host outbox. Cleanup includes linked tool results and answers;
an independent human input loses only its recalled API copy.

The qualified native writer checks exact payloads and active writer leases.
Gateway cleanup also clears affected cached history. Busy work stays pending
until its final answer is persisted. Changed anchors and unknown historical
locations remain pending instead of selecting unrelated content. Delegated and
scheduled source reads can retain storage ownership without becoming owner
statements or acquiring new memory access.

Instruction capture records its native origin before publishing a memory.
Mid-task corrections retain their source lineage on the first request and own
their steering rows and dependent answers, preserving earlier independent work.
Overlapping partial and whole erasures keep their shared origin until both finish.

This requires the qualified Hermes native writer and settled hooks. The host
outbox advances to schema 3, so every process sharing it must use the updated
adapter. Recovery must preserve that schema and erased-data state. Untracked
historical reads, compaction and fork copies, request dumps and backups still
need explicit ownership and cleanup. This is not complete forgetting across
all storage surfaces.

## v1.5.10 - task experience and current role mappings

When working judgments are enabled, operational native tasks can contribute
source-linked completion and failure observations to the existing reflection
worker. Runtime completion remains separate from output quality. The records
preserve request lineage and processor observations, and do not create lexical
or vector memory chunks. Corrections, erasure and replay use the existing source
machinery. Qualification and unclassified historical work are not backfilled.
Useful opinions still need evidence of a later behavioral benefit.

Native task role listing and new selections now read the current profile file.
The previous implementation retained its startup role map. Explicit accepted
role snapshots and native session overrides remain stable; changed mappings
apply to new work without an adapter restart. File-based native tests replace
an earlier test that changed only the in-memory configuration object. This does
not add automatic fleet enrollment, select a better model or change provider
fallback behavior.

## v1.5.9 - ordinary skill failures across sessions

When native reviews are enabled, recurring tool failures associated with a viewed
skill can now survive short conversations and process restarts in Hermes' existing
skill ledger. Two distinct ordinary owner turns can supply one bounded review
batch. Entries retain source references, observed skill hashes and error classes;
the original transcript stays in its existing store. Replayed results and
previously consumed observations do not create new review batches.

This supplies evidence to an existing review consumer. It does not assume a
skill caused a failure, modify a skill, or establish useful learning. CLI and
system work are excluded. Exact skill ownership, evaluation and adoption remain
with the native runtime.

## v1.5.8 - model roles for accepted background tasks

The existing `protagine_task` tool accepts an optional profile-declared
`model_role` on submission. A short coding task can use an interactive processor
while a deliberation task uses a reasoner. The task list exposes configured role
names; provider routes and credentials remain in the native profile.

An explicit selection is saved with task acceptance and applied through the
existing Hermes session model override. Updating a role mapping affects new
work; it does not redirect an admitted task, rewrite an owner-selected session
route or change a concurrent foreground conversation. Omitted roles retain the
existing task default. No executor, proxy or Hermes core interface was added.

Qualification covers the installed adapter and actual native session store,
reopening before execution, mapping changes, conflicting replay and concurrent
native gateway requests with different selected models. SDK responses in the
concurrency fixture are controlled. Useful real-model task completion and
latency are measured separately; role selection itself claims neither a quality
improvement nor enforced per-task execution budgets.

## v1.5.7 - dated memory guidance and vision role checks

Memory guidance separates the current clock from an event's scheduled time.
It asks the agent to keep each event's date, time and destination together,
check current evidence before stating a dated plan, and avoid adding an
itinerary to an unrelated clock answer. This changes the existing provider
instructions; it does not establish reliable temporal reasoning by a model.

Corrected conversations now render as structured evidence instead of nested
escaped JSON strings. The original messages, speaker roles, source versions,
dates and attributed corrections remain together under the same context budget.
Source records and memory ranking are unchanged.

The [native qualification target](docs/HERMES-HOOK-COMPATIBILITY.md) now includes
the optional `memory.refresh_on_turn` setting. Changed curated memory refreshes
through Hermes' existing prompt boundary; unchanged resident turns retain
their cached prompt. The setting remains off by default, and the installer
does not change an existing runtime or profile. File freshness does not prevent
a model from mixing facts about different events.

The [model suite](docs/MODEL-QUALIFICATION.md) adds three packaged image cases
for spatial arrangement, large labels and unknown information. They use the
existing completion router and retain strict output-format and field grades.
These cases do not establish native image recollection, dense-image quality,
camera delivery or broad model qualification. Phase 1 validation remains open.

## v1.5.6 - named image recall and uncertain message outcomes

Asking about a retained image by its supplied name or filename now brings back
the exact reference needed to open the original, even while its caption is
pending or unavailable. Matching images keep their separate references when a
name is ambiguous. The existing audience checks, corrections, forgetting and
context limits apply. A reference identifies an attachment; its contents still
need to be read. Passage relevance scoring remains unchanged.

If an owner-message acknowledgement is lost after submission, the native tool
reports an unknown outcome and preserves the existing delivery identity. A
missing acknowledgement no longer claims that no message was sent. The result
calls for reconciling that delivery before any new send; it adds no automatic
retry. Pre-submission checks and normal receipt handling remain unchanged.

## v1.5.5 - original attachment and executed recipe evidence

Image-only gateway turns retain their actual cached attachment originals through
the existing canonical media store, including when Hermes prepares a text-only
vision description. The adapter matches sender, channel and provider message
identity before reading bytes. The sender's caption remains separate from the
runtime's fallible interpretation; paths written in chat cannot admit images.
Source reads, corrections and forgetting retain the original native identity.

Already-inline originals use references within the existing request, avoiding
duplicate image bytes. Mixed media keep their existing ingestion path. The
existing size limits remain, and receipts distinguish retained originals from
unavailable attachments. Historical caption-only records are not backfilled.
Active turns retain their originals until durable handoff or terminal cleanup,
including long-running and concurrent turns. The typed image limit accepts the
full supported 4 MiB attachment with its encoded format prefix.

Selected tool observations can optionally retain their original executed inputs
when those inputs are needed to reuse a recipe. Native call identity and the
execution-time argument hash bind the inputs to the unchanged result. Missing,
changed or oversized inputs reject that nomination; result-only retention remains
the default. The existing source reader exposes included inputs and their hash.

Background skill proposals retain Hermes' existing read-before-write requirement
before staging. Reading marks a path within that review; it does not establish
that the proposed lesson is correct. The existing evaluator still checks the
skill's staged base before applying a change.

These changes preserve evidence for later use. They do not establish accurate
image selection, visual interpretation or complete recipe recall by a model.
Phase 1 ordinary-use validation remains open.

## v1.5.4 - explicit turn clocks and numeric timezone offsets

Clock context identifies UTC, the agent's reference timezone and a contact's
recorded timezone separately. A recorded timezone does not establish current
location, and fallback settings are no longer presented as contact records.
Retained clock context is labeled historical.

Bare greetings receive the existing bounded owner clock brief through the
Hermes lifecycle hook, even when Hermes skips semantic memory prefetch. Guest
and unknown senders retain the runtime clock. Ordinary turns use their existing
prefetch path without duplicating the brief. No Hermes patch or service is added.

Source-time queries accept explicit numeric offsets in full English clock/date
expressions. Quoted and unquoted operands retain their complete time and offset;
malformed offsets stay unresolved. ISO offsets keep their existing behavior.

These repairs correct clock context and date parsing. They do not establish
reliable itinerary inference or complete Phase 1 memory validation.

## v1.5.3 - memory review routing and model setup corrections

Source admission review uses the configurable `source_claim_review` task, which
defaults to judging. Its selected role controls both the model and deadline;
other judging and planning tasks keep their own bindings. Memory qualification
records the actual review role and task.

Guided setup rejects non-object `modelPool.extraBody` values before requests or
instance writes. Tool probes accept base URLs ending in `/v1/` without introducing
a double slash, while preserving supplied request overrides.

Historical queries retain unsupported clock prefixes as unresolved operands
instead of silently widening them to a calendar day. Supported UTC and local
clocks and calendar-day queries retain their existing behavior. The native model
suite removes isolated state after a confirmed failure before agent construction;
constructor failures and unconfirmed cleanup still stop later cases.

Phase 1 validation remains open. These fixes do not establish broader model
quality or complete ordinary-use acceptance.

## v1.5.2 - native model checks and explicit setup roles

The model validation suite can run an isolated Hermes chat conversation with a
declared elapsed deadline and separate cleanup allowance. It uses the recorded
Hermes interpreter, retains first failures and records whether its owned process
stopped. An incomplete cleanup prevents later cases from running. This suite
tests a grounded chat case; it does not qualify channels, tools or recollection.
The isolated profile preserves native timeout inheritance, and runtime identity
covers the selected Hermes package sources used to construct and route the run.

`protagine init --model-config PATH` preserves a supplied private model pool,
function and task roles, capabilities, credentials, request settings and limits.
Existing Hermes chat configuration stays separate. Local drafts and reviews use
the supplied planning role instead of replacing the pool with wizard defaults.
Invalid input fails before instance writes; existing instances reject replacement.

Source dates now recognize literal full English month names and explicit UTC
clocks. Calendar days remain distinct from instants, and ambiguous or nonexistent
local clock times remain unresolved. This fixes date interpretation after source
extraction; it does not fix model extraction timeouts or establish answer quality.

Initial recollection uses the existing bounded work summary, prioritizing the
current conversation and linked work. It retains source references and unknown
states while excluding full diagnostic reports and task-input excerpts. Detailed
work remains available through the existing readers.

Phase 1 validation remains open. These changes do not establish improved model
performance or complete installation and ordinary-use acceptance.

## v1.5.1 - shared task results and ordinary memory eligibility

Completed Hermes tasks now expose their retained run summary to other owner
conversations. Each excerpt identifies the board, task and completed run, states
whether it is truncated, and links to the existing `kanban_show` reader. Reopened,
running, cancelled and archived tasks do not present an earlier result as their
current completion. A worker's report remains distinct from verified delivery or
other external effects.

Tool-result retention checks the origin of the actual native session. A
background worker cannot become eligible for ordinary conversation memory by
using a CLI transport or completing its task. Ordinary owner retention and the
separate task-completion report path remain available.

Revising a retained appraisal uses the named `source_appraisal_revision` function,
which defaults to reasoning. Initial formation still defaults to extraction.
Both remain configurable by role, and recollection does not wait for background
updates.

Focused checks exercise native task completion and reopening, owner scope,
bounded result transfer, native retention eligibility and appraisal routing.
Phase 1 validation remains open, including useful outcome accuracy, ordinary
memory quality and measurable value from evolving appraisals.

## v1.5.0 - Protagine namespaces and current installation

Protagine uses one public namespace: `protagine`, `protagine-hermes` and
`protagine-hostworker`, with `PROTAGINE_*` configuration and `protagine` native
plugin selection. Protagine is a Proto-AGI engine for persistent agents.
The CLI, model-visible tools, packages, service templates and bundled skills use
these names directly. Name translation layers and deprecated setup entrypoints
are removed. This pre-Phase-1 namespace change requires a coordinated deployment
migration; it does not provide compatibility aliases.

Canonical recollection can include the directly linked input and assistant reply
as separate, attributed evidence. Selecting a correction preserves its exact
speaker and source. A bounded native comparison improved correction recall and
removed one unsupported completion claim; unsupported date arithmetic remains
an observed limitation. This is not a general response-quality result.

Local identity remains private to each installation. Optional federation trust
requires an explicitly configured public key and a valid signed instance
manifest; the package includes no deployment-specific trust anchor.

Phase 1 remains in development and validation.


## v1.4.8 - canonical recall and bounded runtime logs

Automatic recall now selects canonical sources without adding graph-memory
candidates. Explicit search and original-source reads use that same memory
authority. Unused graph management routes, adaptive compression and a shadow
preview buffer are removed. Active graph consumers outside recall still remain.

Runtime logs rotate at a configured size, retaining a bounded number of archives.
Fast successful requests on five routine polling routes are omitted; failures,
slow requests and other traffic remain visible. The existing operational reader
reports declared writer settings and observed archive sizes. A process-start
declaration is identified separately from current process liveness.

See [runtime logging](docs/RUNTIME-LOGGING.md) for configuration and the one-time
transition from an existing unbounded file. These changes do not establish
ordinary memory quality or a completed autonomous repair loop.

## v1.4.7 - canonical memory search

Explicit search now reads canonical source records, using the same scoped
lexical and optional semantic candidates, corrections, ranking and context
budget as automatic recall. Search excerpts carry source references that the
agent can open. A correction or erasure invalidates an earlier search result
before the next model request; a fresh search can recover in the same turn.
An unavailable backend is reported separately from a search with no matches.

Original source reads now expose their report and storage times. The observation
directory exposes the native observation time separately. Five first native
model conversations opened the original evidence and retained its lineage.
Four matched the frozen expectations; the fifth had an ambiguous expected
answer. Its policy's older edition date did not establish that the policy was
outdated. That case is inconclusive, and these trials do not establish reliable
ordinary recall.

Shared contact facts now use their canonical store without graph mirroring,
backfill or fallback listing. Unused graph memory write, flush, reconcile and
status routes are removed, along with duplicate provider search/write tools.
Native memory file edits no longer mirror into the graph. Ordinary source
capture and canonical forgetting remain. Setup diagnostics read canonical
source status, and periodic integration health checks no longer create memories.
Other graph consumers remain pending retirement.

## v1.4.6 - source navigation and independent contacts

The existing memory reader can list recorded tool observations belonging to a
recalled request. The directory returns references that the agent can open
through the existing source view. It does not add every related result to
automatic recall. Pagination, participant scope, source revisions and correction
or erasure checks apply to both the directory and opened originals. Model-authored
selection explanations are labelled as navigation hints.

One native model conversation recovered an authentic retained result omitted by
the original recall selection and answered the requested repository facts. The
full answer still failed quality review because it also inferred current
compatibility from historical documentation. Other actual cases opened genuine
sources but overstated completion status. These observations qualify bounded
source access; ordinary recollection and reliable interpretation remain open.

Contacts now initialize directly from their canonical SQLite store. Startup no
longer backfills or prunes contacts using Neo4j Person nodes. Contact updates and
imports no longer mirror scores or absorb contacts through the removed graph
bridge. Existing identity, handle and correction records remain intact. Other
graph consumers remain pending retirement.

## v1.4.5 - bundled skills and current instructions

The adapter includes Deep Research and Skill Creator workflows. Guided setup
installs them into the selected Hermes profile; `protagine init --skills-only`
installs or refreshes owned bundled copies without model or instance setup.
Locally modified copies are preserved. Hermes advertises short descriptions
and loads the full instructions through its native skill tools when needed.

Existing conversations receive current skill metadata and reload guidance when
instructions change. Native discovery and view caches are refreshed for edits,
removal and disabling, including ordinary profile skills. A current successful
load clears its reload notice. Saved system prompts and historical messages are
preserved; unchanged skills add no request text. The integration uses the
existing request middleware and native tools, without a new service or core patch.

Shared recall and evidence-selection helpers now live in `protagine.memory`,
independently of the Neo4j graph package. Their selection behavior is unchanged.
The separate graph retirement and measured memory-quality work remain open.

## v1.4.4 - first-baseline cleanup

Phase 1 defines the first supported release baseline. This preparation removes
obsolete world-model Neo4j and PostgreSQL adapters, their backend-selection
fallbacks and the PostgreSQL dependency extra. The world model uses its existing
SQLite store for entities, relationships and typed observations. Its HTTP
creation, health and persistence paths use that same implementation. The separate
Neo4j memory graph remains active code pending its own retirement.

Canonical `protagine` packages and entry points replace the removed Colony aliases.
The retired self-knowledge seeding endpoint, command and module are removed;
guided identity setup and source-backed self queries remain. Obsolete poller
wrappers are removed, and worker setup selects current executable names.
The standalone `protagine-hostworker` package advances to 0.2.1.

Current setup and architecture documentation replace historical upgrade guides
and compatibility promises. Development releases before the completed Phase 1
baseline may change interfaces and storage layouts.

## v1.4.3 - recall evidence and quality measurement

Recalled evidence no longer includes the reranker's weight-verification stamp.
That stamp describes the retrieval processor, not the source's authenticity.
It remains available in structured diagnostics. Source attribution, uncertainty,
corrections and unavailable-reranker notices remain visible to the agent.

The recall benchmark can import exact user, assistant and tool records with
source corrections, and run development and held-out queries separately without
extraction or generative calls. It reports evidence coverage, irrelevant context
and missing conditions. A separate source-utility score permits relevant request
or progress context when no completed outcome is known; the existing strict
empty-packet abstention metric remains unchanged. Neither grades the final answer.

Native observation tests now include HTTP status and outbox diagnostics when a
retention assertion fails. Runtime deadlines and delivery behavior are unchanged.

## v1.4.2 - accurate host skill availability

Automatic context no longer advertises internal initiative executors, such as
`behavioral_correction`, as installed host skills. Hermes's existing skill index
and discovery tools remain responsible for the actual installed catalog. The
internal executor registry and its explicit API remain available.

The native skill regression checks the real prompt index, skill listing and
skill loading beside formatted Protagine context. CI requires it to run and pass
against the pinned Hermes qualification build. The original-memory observation
fixture also completes its setup before requests begin, preserving its existing
runtime deadlines and behavioral assertions.

## v1.4.1 - deferred tool observations and source-admission diagnostics

Selected observations now recognize a single local tool invoked through Hermes'
`tool_call` wrapper. Native argument normalization connects the wrapper to the
executed tool, while the adapter verifies the actual arguments, call ID, current
request and original result bytes. Ambiguous, multi-call or altered bindings
remain ineligible. Selection limits and source-erasure ancestry are unchanged.

When the current request has no source-admission snapshot, nomination reports
that source admission is unavailable instead of reporting an unknown call ID.
This uses existing observed state; it does not probe service health or guarantee
freshness after an earlier successful request. The diagnostic does not select a
call, retry automatically or repair host credentials.

Controlled native tests cover the supported Hermes 0.21.1 and 0.21.2 builds,
including actual deferred dispatch, persisted originals and erasure. These
checks do not establish better model selection or recall quality.

## v1.4.0 - selected tool observations and linked communication erasure

An agent can nominate a useful completed native tool call for later automatic
recall. The adapter resolves the original result from Hermes and retains its
exact source links. A bounded hint exposes eligible call IDs in the current
request. Incidental output is not automatically copied into long-term memory;
selection remains a model decision that needs evaluation during ordinary use.

New ordinary communication summaries carry canonical source links. Forgetting
those sources also removes their linked summaries. History reads validate the
selected results and use existing erasure records, avoiding repeated scans of
all original message bodies. An explicitly supplied source ledger prevents an
offline copy from being checked against another profile's default store.

Cleanup responses distinguish disabled stores from unverified cleanup and do
not claim that every host has reconciled. Historical summaries without source
links remain counted and are outside this cleanup scope. Older versions can
read the additive communication schema but cannot perform its linked cleanup.

The canonical and compatibility plugin manifests now match the adapter package
version. The existing wheel and source-distribution test checks all four.

## v1.3.4 - compatibility readers for original tool observations

Canonical source readers recognize original tool observations and preserve
links to their source evidence when reading or erasing them. The durable outbox
can remove pending observations when their source is erased.

This release prepares compatibility for a later observation writer. It does not
enable tool-result nomination or add an ingestion route. Pending observations
from a newer writer remain pending on this version until re-upgrade; they are
not reported as saved. No recall-quality improvement is claimed.

## v1.3.3 - native history erasure and Hermes 0.21.2 compatibility

Native session-history results are checked against scoped canonical erasures
before reaching the model. Exact source ancestry covers retained text and
structured messages, derived tool spans and later same-turn erasure. Unrelated
traceable turns remain readable; untraceable titles and previews are omitted.
This is logical recall filtering, not physical deletion of native history,
backups or raw files. Untracked historical material has no invented source links.

Native repeat-read warnings retain the authenticated history receipt; altered
or unrecognized source wrappers are withheld without gaining provenance.
On earlier Hermes versions, authorized steering instructions survive when
appended to a withheld source read. The source body stays withheld and may need
to be read again; unrelated appended text gains no authority.

Attachment accepts Hermes 0.21.2 while retaining the earlier supported versions.
Native merged user inputs retain their current source attribution and historical
erasure checks whether Hermes merges clean text or enriched API content.

Detached automatic reviews inherit the exact parent participant, source bindings
and trusted memory provenance through existing request middleware. Their final
state uses the small detached-execution observer in the documented compatibility
build; ordinary persistence hooks remain skipped. Internal review prompts do not
become human memory, and a later speaker cannot upgrade a queued guest review.

The recall benchmark now retains exact selection candidates, reranker inputs and
returned scores or failures for reproducible analysis. This adds no runtime
telemetry, changes no recall budget, and makes no model-quality claim.

## v1.3.2 - conversation provenance and repeated-request recall

Native turn ingestion now preserves the actual conversation platform independently
of the sender's authority channel. A CLI conversation no longer acquires a
contact's primary messaging channel. Explicit channel identifiers survive the
client and durable outbox. Existing rows retain their recorded provenance.
Automatic context describes recorded outgoing messages without asserting
proactive outreach or delivery.

Recall places plain quotations that exactly repeat the current request after
independent evidence. A reconstructed pending-correction case now includes the
original human correction within the unchanged five-item and 6,000-character
budgets. Distinct assistant retellings can still crowd out a pending correction;
this change does not establish general source retention or model portability.

Validation includes five frozen native integration cases for channel attribution,
replay and truthful context, native CLI ingestion, and source ingestion through
FTS, claim projection and recall packing. These checks exercise real code paths
with controlled inputs; live model usefulness is evaluated separately.

## v1.3.1 - enrolled owner accounts and scoped turn clocks

Fresh guided setup can enroll exact owner messaging accounts with repeatable
`--owner-handle CHANNEL=SENDER_ID` options. The private contact store holds the
verified bindings; Hermes continues to own channel configuration and delivery.
Existing instances retain their identity and reject changed enrollment flags.

Sender lookup now accepts the existing scoped transport permission without
requiring broad API access. Restricted static transports validate an enrolled
sender before storing a turn, so failed resolution cannot fall back to the owner.
Existing broad callers and explicitly attested dynamic transports retain their
separate contracts. Integration checks exercise the generated credential, real
SQLite contacts and both source-ingestion API versions. They do not establish
physical channel delivery or general conversational quality.

The memory provider labels its clock as belonging to its original user turn.
Replayed clock notes and earlier tool observations remain historical; retained
conversation history is unchanged. Native two-turn checks qualify composition
and persistence. Improved model answers require a separate behavioral trial.

## v1.2.1 - bounded native operational reviews

Autonomous operational reviews use an opt-in native profile with two tools:
read registered evidence and report through the current task's native lifecycle.
The profile cannot invoke terminal, file mutation, arbitrary attachments,
delegation or task creation. Registered log samples include current size and
filesystem measurements; missing writer and retention configuration is explicit.
The existing planning role is refreshed before new work is dispatched.
The existing native dispatch tick discovers at most five new eligible proposals
when reviews are enabled. Routine selection and reconciliation need no LLM
queue steward or separate cron, and concurrent ticks reuse one native task.

Guided attachment exposes `--native-reviews`; adapter refresh preserves the
existing choice. Historical reviews still reconcile. New temporal follow-up
workers remain undispatched until their bounded report path is integrated with
the existing governed outbox. Owner conversations and shared tasks retain their
own tools and profiles. Native integration tests exercise actual discovery,
worker identity, evidence selection and completion; model usefulness requires
a separate deployment qualification.

The pinned Hermes compatibility build also retains named custom-provider
timeouts through transport resolution. Doctor no longer advises restoring
Hermes' retired normal output cap. Native truncation recovery remains intact.

## v1.2.0 - shared native tasks during conversation

The optional `protagine_task` tool starts durable Hermes work while foreground
conversation continues. The same authenticated owner can inspect, steer or stop
that task from another enrolled channel. Hermes retains execution, native
sessions, interruption and recovery. Protagine supplies durable source associations
through one generic adapter and controller, which private transports can reuse
with their existing storage and authentication.

Consumed updates become source parents before execution continues. Current
source checks retain exact input hashes and canonical revisions, including
inputs whose normalized media revision differs from the transport hash. An
additive input-revision receipt supports existing transport adapters without
relaxing the strict freshness result. Derived task contexts and inherited child
authority cannot manufacture a new direct-owner instruction.

The actual GatewayRunner fixture holds two task roots, completes foreground
conversation, steers and stops one from another channel, and checks that the
other completes with its source parents. Responses and channel transports are
controlled; ordinary model usefulness and physical delivery remain deployment
qualifications. Concurrent callbacks require the explicitly pinned
[Hermes compatibility build](docs/HERMES-HOOK-COMPATIBILITY.md). The separate
daily upstream job continues to test unmodified Hermes.

Model qualification adds structured cases for reasoning, planning, judging and
coding. Each case retains its contract, evaluator and request limits; passing
these cases does not establish a general model ranking or automatically change
the active processor.

## v1.1.17 - diagnosable model qualification and faithful memory grading

Explicit model evaluations retain bounded final completion text, its hash and
truncation state. This makes rejected memory proposals inspectable without
adding raw-generation logs to production memory. Candidate and supporting-role
calls are distinguished, and known router failure categories can be retained
without copying arbitrary exception messages or SDK envelopes.

Memory checks distinguish unexercised inputs from successful abstention and
match independently specified complete subject/relation/value alternatives.
A faithful value need not repeat a noun already represented by its relation;
wrong subjects, relations and contaminated values cannot pass merely by
containing the expected phrase. Source evidence, conditions, correction history
and later-session lineage remain separate requirements.

The changed cases and evaluator have new identities. Earlier first results keep
their original grades and cannot be presented as model gains under the new
rubric. These changes improve diagnosis and measurement; they do not change
production extraction, model routing, native behavior or role selection.

## v1.1.16 - bounded model consumer qualification

The opt-in `protagine models inspect`, `evaluate` and `compare` commands inspect
configured bindings and record finite consumer evaluations without changing
deployed model selections. Recipes, cases, independent oracles, evaluator
identity and first results are retained in private result directories. Resume
continues untouched cases without replaying interrupted or failed attempts.

The initial suite separates direct response semantics from actual memory
formation, semantic admission review, explicit correction and later-session
lexical recollection. Supporting judging retains its configured role. Empty
outputs, unsupported capabilities, setup failures, timeouts and interruptions
remain in result counts; fallback success cannot pass the requested primary.
Comparisons separate durations by outcome and flag changed grading or system
implementations. Unobserved serving weights and missing telemetry stay unknown.

Controlled tests establish runner, transport and memory-consumer mechanics.
This release does not yet measure installed-native Hermes foreground behavior,
native tool effects, embedding/reranking quality, media, speech, hardware or
model swaps. No useful model-quality or performance improvement is claimed
before an explicit actual comparison. No evaluation service, database, model
proxy, automatic promotion or approval layer is added.

## v1.1.15 - selected video sources and original-frame recall

Explicitly supplied short MP4 clips now enter the existing source ledger and
media worker. The optional `video` extra provides a bounded decoder. The vision
role describes sampled frames, and the native source reader can reopen a frame
at a requested clip-relative time. Original clip and generated image hashes,
timestamps, source revisions and corrections travel with the result.

Video admission uses a dedicated compatible route and requires a stable turn
ID. Frame revalidation checks retained provenance without decoding again; source
correction or erasure withholds stale pixels. Existing source storage, search,
backup and erasure are reused. Camera enrollment and continuous recording are
outside this change.

Recalled media now places the exact canonical source ID and version beside its
description. Lexical media retrieval preserves the owning message hash so
source corrections accompany that evidence. A reader call using a media ID
still fails, but can show already-supplied matching source references to help
the caller correct its arguments without guessing or widening access.

CI installs the optional decoder for actual MP4-to-native-SDK integration
checks. Controlled tests verify bytes, timing and source effects; they do not
establish a model's visual accuracy or physical camera behavior.

## v1.1.14 - connected CLI recollection and retained-turn delivery

An explicitly attested native CLI turn can now pass its resolved identity to
the memory provider while the broad default-owner fallback remains disabled.
The lookup requires the current native profile, session, task and turn; a
finished turn or unrelated session cannot reuse retained scope. Existing
sender and supplied-input checks continue to govern other paths.

When erasure removes part of a turn, a safe retained replacement now triggers
the existing bounded delivery drain. Original receipts remain erased, and
complete erasure remains a no-op. Failed delivery retains recoverable work.

Rejected image captions retain specific final-answer or length dispositions
and the configured model and routing provenance when available. Caption limits, retry timing
and original-image access are unchanged. Historical generic errors cannot be
diagnosed retrospectively.

Controlled native and canonical-store tests qualify these connections.
Ordinary useful recollection, visual interpretation and truthful answers still
require deployment observation. No Hermes core patch or new worker is added.

## v1.1.13 - forgotten history excluded from native summaries

Hermes can build an extra final-summary request from retained history after a
task reaches its iteration limit. This call bypasses ordinary request hooks.
Protagine now applies its existing source-validity and erasure filter through the
native turn's scoped Relay execution contract, including streaming and retry
calls. When an exact erased source identifies a historical conversation turn,
its derived tool arguments, results and reasoning are withheld together. The
current observed input, including an intentional retelling, remains available.
Ordinary requests avoid a duplicate check. Child scopes and completion cleanup
preserve the original participant boundary.

Qualified on Hermes 0.21.1 with NeMo Relay 0.8.3. The optional `native-memory`
extra installs that dependency. This filters provider inputs on the covered
paths; it does not erase stored native transcripts, exports, backups or arbitrary
paraphrases. No Hermes core patch or process-wide Relay policy is introduced.

## v1.1.12 - current timing and shared-session context

Cached contact timing is rendered against the current turn, preserving the gap
before that turn through native compression and clearing it on unrelated history
changes. Shared-work context includes the authenticated current session identity,
including when the work lookup is unavailable.

## v1.1.11 - source-aware native task continuation

Registered transports can supply already retained input references while Hermes
allocates the actual session. Current participant checks still govern the root
turn, compression successors and joined children. The transport does not create
native session identities or grant additional authority.

Interrupted native tasks can use their retained source text as the root recall
query. The supported request middleware restores current recalled evidence when
Hermes merges adjacent plain user rows during automatic continuation. It matches
the exact native-observed tail and filters historical evidence for forgetting
before recombining it. Historical recall and user-authored markers cannot become
current provenance.

Qualification covers the built adapter and actual native request path. Transport
admission, task persistence and reply delivery remain responsibilities of the
runtime and the deployment's registered transport.

## v1.1.10 - focused work context and clock use

Automatic shared-work context keeps completed artifact outcomes, identifiers,
limitations and report receipts. Its generated summary remains available through
the work API and full report, rather than being repeated in every turn. Work
without a complete artifact receipt retains its summary.

Clock context retains the current time and its distinction from session start.
It now asks for time calculations only when needed to answer the request, removing
unconditional greeting and arithmetic instructions. These changes simplify
request construction; conversational quality still needs observed qualification.

## v1.1.9 - working fresh native profiles and Mac instance shutdown

Fresh Hermes profiles now use its supported `custom` provider for the selected
OpenAI-compatible local endpoint, including optional native task judging.
Existing configured providers are preserved. The packaged setup qualification
now resolves the generated profile through native CLI startup before running
its memory conversations.

A local sidecar records its process identity after startup readiness, avoiding
the temporary Python launcher command on macOS. The existing stop command can
then identify and stop that instance while retaining its PID-reuse check.

These changes repair installation and local instance lifecycle. They do not
change an existing agent's model routing or establish conversational quality.

## v1.1.8 - linked incident repairs and attributable timing observations

Appraisal extraction now decides separately whether each supplied prior incident
is unchanged, uncertain or resolved. A cited resolution settles that exact
incident in the existing store without creating a new temporary mood. Missing
decisions and duplicate same-topic appraisals are rejected; resolution is never
required. Existing records remain readable and the extraction uses one call.

Expected replies can retain prospective observations against a declared wait
horizon, using exact durable dispatch, reply and source receipts. The initial
probability is an uncalibrated fixed baseline. Clock passage alone does not
establish absence, and these observations do not select or send follow-ups.

Current-work context labels execution phases with unknown liveness as last
observed and places them after recorded task outcomes. Native host tasks bind recalled context
to their actual request text when their persisted display text differs, allowing
the existing source-evidence framing to replace Hermes's authoritative-memory
note without changing recalled quotations.

These changes improve accounting and request construction. Grounded answers,
useful relationship adaptation and learned reply timing still require measured
ordinary-use outcomes; passing the integration checks does not establish them.

## v1.1.7 - comparable execution forecasts and visible source tools

New execution forecasts use a separate versioned cohort. Normal context growth
and tool discovery remain eligible observations; changed routing or missing
required measurements remain distinct. The duration estimate retains its small
sample prior and removes the permanent ratio clamp. Old forecasts settle under
their original rules and do not train the new cohort.

The existing forecast record retains both the fixed prior and the old clamped
calculation for comparison. Timing remains observational. These changes do not
establish calibrated timing, useful scheduling or improved task quality.

The native tool catalog now names original-image and PDF-text access in the
source reader's first sentence. Tool permissions and behavior are unchanged.

## v1.1.6 - relevant current work and relationship context

Present-work questions prefer the authenticated current work view over old
assistant status replies. Suppression requires verified source/input lineage;
user evidence, corrections, procedures and explicit historical comparisons stay
eligible. Historical replies remain available through source inspection.

Relationship topic matching recognizes common English plural forms. Repeated
identical behavior hints render once while their records and source references
remain intact. This changes relevant context selection, not evidence validation,
relationship authority or automatic opinion activation.

The existing `source_appraisal` and `self_judgment` tasks now accept per-task
role overrides. Each operator uses the same task selection for dispatch and its
lease deadline. Default roles, prompts and activation settings are unchanged.

Focused regressions reproduce the omitted topic and mixed-history comparison
failures. They do not establish grounded model answers or useful relationship
adaptation; first model failures remain part of deployment qualification.

## v1.1.5 - task-specific processors and recoverable source admission

Optional `taskRoles` selects an existing function role for a supported cognitive
task. Source-memory formation can use reasoning while other extraction remains
unchanged. Capability hints, worker budgets and dispatch share the same selector;
explicit per-call roles take priority. Invalid maps preserve the last valid
configuration, and active requests retain their selected snapshot.

Native source handoffs report whether a transient verification failure happened
before any successful admission. Temporary network failures and an exhausted
initial freshness allowance remain distinct from erasure, invalid scope or a
changed source. The scope stays blocked; a host may start one fresh process under
its existing deadline. This release adds no automatic replay after admission,
new service or additional request budget.

Focused checks exercise routing reload, actual native startup, source invalidation
and a private host's bounded subprocess retry. They do not establish improved
memory quality, actual fleet rebinding or ordinary task recovery on their own.

## v1.1.4 - attributed experience and useful operational context

Substantive experiences can use exact attributed episodes in the existing source
ledger. Explicit corrections select a current episode, preserve earlier reported
context and expose missing revision history. Event dates stay separate from report
dates, including overlapping calendar days. Whole eligible text messages need one
extraction pass; a distinct admission marker records that no second model review
ran. Selected long excerpts, audio and generated structured assertions retain
their existing review. Reports are not independently verified world facts.

Recall preserves a complete eligible short message when query time is unresolved,
with the uncertainty label intact. Procedural wording such as "before opening a
file" previously caused otherwise available neighboring conditions to disappear.
Resolved historical queries keep their qualified assertion behavior.

Shared execution records can point to an already admitted root input. A current,
scoped source read supplies its brief request excerpt; the work ledger stores only
references. Work selection precedes optional excerpts, and the existing request
source check tracks those excerpts into response lineage. Child assignments and
unbound tasks remain unknown rather than inheriting a parent's stated purpose.

Native forecast observations distinguish explicit output caps, observed provider
defaults and missing metadata, using final request fields and Hermes' existing
middleware trace. Legacy unknown observations do not become comparable outcomes.
An existing local log-volume condition now reaches the existing read-only native
operational review, with measured file sizes and settlement-based recurrence.
No new service, scheduler or automatic maintenance authorization is added.

Controlled episode trials established successful source-linked correction and
history reading with a stronger extraction processor. Other first attempts timed
out or selected the wrong correction operation; unsupported answer claims remain
a measured weakness. These changes do not establish general recall accuracy,
ordinary cross-session usefulness, timing gains or autonomous self-repair.

## v1.1.3 - native recollection and concurrent work context

Authenticated native tasks can recollect for the contact bound to their validated
human input, including host tasks with no channel sender. The memory provider
uses the existing source validation and session binding before selecting that
contact. This adds no owner fallback for unchecked supplied input. Channel
senders must still match, and source erasure invalidates dependent recall before
another request. Authenticated compression rotations preserve the root input
and final handoff while child completions remain separate. Existing explicit
owner-system configuration is unchanged.

Shared work fetches available active ancestors before projecting current native
executions under the existing eight-record and 4,000-character limits. Parents
stay with their children even when newer siblings fill the initial selection.
Only the current execution family precedes the active sources, which alternate
before optional history. Bursts of recent siblings do not displace every other
work source. Omitted records remain explicit;
execution ancestry does not establish that two tasks have the same purpose.

The installation guide uses the matched published packages, documents Hermes'
public source installation prerequisite and separates the minimal memory setup
from optional accepted local work and native goals. The fresh Linux installation
trial used version 1.1.2 and completed one real native task; channel enrollment
still needs deployment checks.

Focused native and source-lifecycle checks cover these fixes. A replay of the
failed concurrent-work snapshot now retains both parent and child. This does not
establish improved model attribution or a completed cross-session task.

## v1.1.2 - complete recall passages and scoped standing rules

Recall shares exact source passages across assertion cards and keeps an eligible
short current message intact with its claim and correction references. A partial
introduction cannot crowd out the changed requirement from that same message.
Conflicted, superseded and retracted claims still prevent whole-message expansion.
Literal JSON source text remains evidence rather than an internal assertion card.

Automatic directive capture requires an explicit lasting owner instruction.
Temporary task constraints remain canonical task evidence. New learned rules
store source references instead of duplicate prose and hydrate from current
evidence, so correction and forgetting withdraw their effect. Explicit manual
rules and historical records retain their existing behavior.

Accepted native API requests and terminal execution callbacks now contribute
bounded duration observations to the existing shadow forecast path. Cohorts
retain processor and request-shape distinctions; failed, cancelled and missing
pairs do not become comparable success samples. This measures accepted request
time, not queue time or every task's full duration. Timing decisions remain off
until prospective outcomes demonstrate benefit. No service or model call is added.

- Reconcile committed execution outcomes after missed settlement, deduplicate repeated standing clauses, keep source text out of serialized refusal verdicts, and remove the unused directive LLM fallback.
- Let accepted native task transitions wait up to two seconds for a brief board writer; keep ordinary work projections on their shorter read budget.

## v1.1.1 - grounded corrections and ordinary task context

Explicit corrections and changes can resolve an abbreviated subject through the
exact reviewed prior assertion. The new value still comes from the new source.
Recollection includes both references and labels the original quotation as
subject identity evidence, so an old value does not become current again.
Erasure, changed attribution and source annotation invalidate dependent claims
and learned views. Existing source records remain intact.

The native request adapter removes Hermes' exact assigned-worker guidance from
ordinary conversations that have no dispatcher-owned task, including its ASCII
recovery form. Real workers keep their guidance. This uses the existing request
middleware across supported transports, with no Hermes core patch. Shared work
also retains the observed name of a native cron job beside its identifier.

The retired built-in self-knowledge seed no longer writes an obsolete capability
catalog. Its CLI and host route retain a small no-write compatibility response;
private identity setup and existing stored history are preserved.

Controlled native tests and two first-attempt real-model correction cases cover
these repairs. They do not establish general answer quality, natural background
learning, or a completed cross-channel task.

## v1.1.0 - page-addressed PDF sources and pending work visibility

Authenticated source capture can retain bounded PDF originals in the existing
media ledger. The source reader opens extracted text by page with pagination,
source identity and current correction lineage. Changed or erased evidence is
withheld before native dispatch. Extraction runs in a bounded child process;
Darwin uses sampled RSS monitoring when its address-space limit is unavailable.
That fallback is explicitly reported as a sampled limit, not a hard memory cap.
Blank, encrypted, malformed and partially readable documents retain distinct
outcomes. This does not add OCR, visual layout interpretation, video or automatic
channel attachment capture. Existing readers can still back up and erase the
owned originals. Eligible PDF and image jobs share FIFO scheduling.

Current work includes pending, blocked and abandoned queue records as well as
claimed/running work. Enrolled external heartbeat snapshots can describe delivery
state using exact task and attempt identifiers without message or recipient
content. Missing observations stay unknown, and provider receipts do not become
proof that a recipient read a message. No new queue, database or service is added.

Controlled source and installed-Hermes tests cover page reads, pagination,
protocol conversion, correction, erasure and the external-producer/public-reader
boundary through generic fixtures. Actual deployment activation and ordinary
usefulness require separate qualification.

## v1.0.36 - shared work, corrected identity and original image recall

Bounded request context now takes turns across selected work sources, so a busy
local backlog cannot consume all eight records. It preserves session, parent and
attempt identifiers, reports missing or failed readers, and discloses omissions.
Independent readers run concurrently within their existing deadlines. Source
counts overlap and are not presented as a unique task count or complete process
inventory.
Partial observations survive a delayed reader, and recently finished work is
counted separately from active work.
Native child completion also uses Hermes' supported child-stop hook, so
completion can still be observed when session-end callbacks overlap. An
unavailable observation endpoint still leaves liveness unknown.
Overlapping native text turns also recover skipped initialization through the
existing memory-provider and request-middleware contracts. Recovery binds the
captured message to its transport identity and exact turn; unresolved identity
remains unavailable. No core patch is required.

Lexical recall now hydrates each excerpt against its exact canonical message
before attaching modality, uncertainty and correction lineage. Typed text no
longer inherits another message's audio-transcript metadata. Distinct messages
with identical rendered text remain distinct through retrieval fusion, and
unowned stale index excerpts are excluded before the result limit. Exact indexed
chunks determine ownership, so a substring in another message cannot supply
its metadata. No schema migration is required.

An explicitly corrected, verified channel handle now takes precedence over
phone matching across channels. Current request evidence revalidates source
ownership after a correction, including cached recalled text and opened sources.
Correction reversal preserves the original records and invalidated descendants.

The existing source-reading tool can reopen a retained original image into
Hermes' supported vision input parts, with exact source and asset identity.
Each request checks its current source and correction lineage. Erasure or
changed attribution withholds the image; a processor without supported image
tool results receives an explicit no-image result. The backend must be updated
before the native adapter. No new service or Hermes core patch is needed.

Controlled SQLite/HTTP and pinned native-runtime tests cover the changed paths.
These checks do not establish ordinary cross-session usefulness or improved
model answer quality. One earlier intermittent fenced-draft qualification
failure remains unreproduced, with its evidence retained and better native
completion diagnostics in the fixture.

## v1.0.35 - relevant ordinary context and explicit briefing access

Ordinary context assembly no longer inserts the latest three global briefings
without a relevance or freshness check. This removes repeated operational
summaries from unrelated turns while preserving scoped source recall, citations,
current work context and every stored briefing.

Explicit briefing history remains available. Enriched-context requests now
render the actual stored briefing type through the existing narrative converter,
instead of silently omitting its content. Regression coverage checks both owner
projection paths, retained source citations, explicit history and enriched access.
No data migration or Hermes core change is required.

## v1.0.34 - profile read-receipt preference

`protagine init --preferences-only --whatsapp-read-receipts on|off` updates an
existing Hermes profile through the existing config writer. `--preview` lists
the changed setting paths without writing. This path does not create an
identity, attach an adapter, contact a model or restart a service. Ordinary init
also accepts the option; omission preserves the previous behavior.

Native config readback qualifies alternate YAML layouts, current channel
selection and the adapter's read-receipt environment. Unconfigured channel
selection is rejected before a preference could implicitly enable WhatsApp.
Read receipts and online presence remain separate capabilities. No adapter or
server runtime behavior changes; both packages remain version-aligned.
See the [profile guide](docs/LOCAL-HERMES-SETUP.md#whatsapp-read-receipts-for-a-selected-profile).

## v1.0.33 - retain replies to supplied input on excluded platforms

An authenticated host's validated supplied-input task now retains its
assistant-only reply when the native platform is excluded from automatic
ordinary turn capture. Previously, the platform filter also discarded that
dependent reply, leaving the host without a source receipt despite a completed
native answer. Ordinary excluded CLI turns remain excluded, and supplied
parents do not grant participant or tool authority. Existing source ownership,
exact parent validation and erasure checks remain in force.

The sidecar runtime is unchanged; both packages remain version-aligned.
See the [supplied-input contract](plugins/hermes-plugin/SUPPLIED-INPUT.md).

## v1.0.32 - captured input through native tasks

An authenticated host can bind already captured input to the existing native
agent loop using exact input hashes and source revisions. Participant binding
remains a consistency check, not an authority grant. The host may display the
original human message while sending a derived task instruction; the canonical
writer records the assistant reply with its dependencies without adding that
instruction as another human statement.

Inherited handles become available to the scoped source reader only after the
host's exact appended block reaches the native request. Source quotations cannot
introduce additional handles. Existing erasure checks run before requests,
tools and the final writer, and copied host contexts close when their owning
call exits. A new indexed outbox lookup reads one exact source without decoding
unrelated history. Hosts receive output dependencies for revalidation before
delayed delivery or further effects; native transcripts and historical unlinked
paraphrases remain outside the canonical erasure guarantee.

The memory provider now describes its own four interfaces without claiming
they are the only tools on a surface that also loads the native adapter. The
sidecar runtime is unchanged. Both packages remain version-aligned. See the
[adapter guide](docs/HERMES-ADAPTER.md) and
[supplied-input contract](plugins/hermes-plugin/SUPPLIED-INPUT.md).

## v1.0.31 - attributed corrections in evidence ranking

The existing reranker receives attributed source corrections before the original
evidence, with an explicit unverified label. The supplied memory packet still
contains the complete original, every attributed correction, unresolved conflicts
and current source references. Ranking order does not determine which assertion
is true. Scope, atomic packing and erasure behavior are unchanged.

The candidate input format has a new calibration version. An older stamp cannot
authorize a cutoff for the changed representation. Deployments must qualify
useful corrected evidence and no-memory controls, then select matching code and
calibration metadata together. No global cutoff, provider task instruction,
additional model request or source migration is introduced. See
[calibrated recall](docs/RECALL-HYBRID.md).

## v1.0.30 - source-linked automatic contact recall

Automatic contact-knowledge context now requires a current canonical source
link, checked before pagination. Unlinked historical and hand-entered facts
remain explicitly inspectable but no longer fill per-turn context. Optional
P8 audience envelopes remain additional checks, not substitutes for evidence.
Shared-fact graph mirrors cannot bypass this boundary after the original record
is excluded or removed. Other graph memories and explicit fact APIs remain.

Selected linked estimates carry exact source membership into the existing
correction and request-erasure paths. They remain labelled unverified estimates.
The enriched-context route uses the same query-matching, annotated source
packet; compression cannot turn a corrected estimate back into a bare claim.
Automatic relationship inferences require current supporting facts. Missing or
corrected premises suppress the old inference, including UUID-only fallback
text. Cached audience views recheck exact current source membership, and a
source change during context assembly removes the affected inference. Explicit
inference history remains available; these checks do not establish that a
model's original inference was correct.
No source, historical record or graph mirror is deleted, and no new judge,
store, approval step or Hermes patch is introduced.

## v1.0.29 - owned audio and admitted opinion premises

Bounded PCM WAV clips use the existing canonical original-asset storage, scoped
source reader, backup and erasure paths. Paired transcripts retain segment ranges,
recognizer provenance and distinct capture and receipt clocks. Text processors
receive labelled transcript evidence; original audio stays behind the scoped
asset API. Updated clients use a versioned ingestion path that older backends
reject before accepting unsupported media.

The existing extraction and admission-review jobs can form claims from exact
retained transcript spans. Commit rechecks the current source and asset owner.
Claims remain recognition-derived and unverified, including after correction or
later recall. Receipt time cannot resolve an unknown speech date. Segment labels
and unrelated text do not supply quoted transcript evidence. Admission remains
fallible, and one extracted procedure claim may omit another source condition.
Appraisal and personality extraction remain outside this audio path.

No additional store, worker, model or Hermes patch is introduced. The bounded
semantic qualification retained three useful sources and excluded three noise or
fiction cases using actual extraction and review requests. Those results measure
transcript formation, not recognition accuracy or physical voice capture.
Deployments must qualify their own recognizer and capture adapter before enabling
audio where they previously supplied text. See [audio sources](docs/SOURCE-AUDIO.md).

Automatic persistent judgments are experimental and off by default. Exact
`PROTAGINE_SELF_JUDGMENTS_ENABLED=1` opts in after a deployment qualifies its reasoning
model. Disabled generation leaves sources, history and correction controls intact;
pending judgments stay held and no working views enter automatic context.

When enabled, ordinary judgments wait for the existing source-admission job and
require reviewed decisions, procedures or substantive events from their exact
source messages. Facts, preferences and relationships use their existing memory
projections without another opinion request. A plain recall
question with no admitted premise finishes without another judgment request.
Supporting and contrary claim IDs remain bound through commit and later context,
so a corrected premise cannot be replaced by an unrelated surviving claim.
Recorded runtime observations and explicit owner reconsideration retain their
existing paths. Historical views without these bindings remain inspectable and
correctable, but no longer enter automatic guidance. This narrows eligibility;
admission and generated opinions still require semantic quality evaluation.

The bounded extraction batch now has a 4096-token output allowance; review reasons
retain up to 1024 characters. Existing role deadlines and attempt limits are
unchanged. The repair preserves useful completed responses without admitting
truncated output or silently rewriting review decisions.

Partial-message procedures now recall one complete current source-message unit
instead of independently ranked property fragments. A condition elsewhere in that
message cannot disappear while its steps remain. Changed, conflicting or oversized
procedures require the existing full-source and history readers; budgeting does
not convert a partial procedure into complete instructions. This preserves source
quotation, current scope, annotations and erasure without re-extracting old claims.

## v1.0.28 - complete source opening and coherent current state

The native source reader opens exact scoped revisions and paginates long text
or assertion history through the existing memory API. Opened tool results join
the request's source lineage and erasure checks. Oversized histories retain an
opening handle. Optional event uncertainty no longer discards otherwise grounded
claims; required validity conditions remain strict, and report clocks stay
distinct from event time.

Delayed direct replies can bind to an exact admitted user input before its
canonical media normalization completes. The existing outbox waits for that
parent, and canonical ingestion resolves its current source revision atomically.
Erasure removes the linked reply; an older backend cannot silently store it
without its parent dependency.

Canonical reviewed preferences now own lasting guidance. Appraisals consume
their current scoped view; legacy preference records remain inspectable without
independently governing behavior. Native review outcomes record provider-reported
model observations without rewriting original forecasts or guessing missing
processor identities. Timing guidance still requires its prospective evidence.

Native tool dispatch rechecks the externally resolved participant, including
inherited child contexts, so a corrected handle or revoked host credential
cannot keep the old turn's access. Requests with no available tools receive a
factual capability note while retaining supplied evidence and ordinary answers.
This does not guarantee the truth of all model-generated explanations.

Guided setup discovers native profiles before selecting one private attachment.
Memory-only recovery selects a surviving current canonical ledger and its owned
original images, preserving current corrections, scope and erasure ancestry.
It excludes old runtime authority and effects. Full archive reconstruction no
longer implies that recovered services are ready to start.

These changes use existing stores and Hermes extension points. No new service,
approval flow or Hermes core patch is introduced. Hermes 0.21.1 remains the
qualification target; real deployment and model results require their own
behavioral checks.

## v1.0.27 - source-grounded formation and recollection

Memory formation can use strict JSON schemas on model bindings whose serving
endpoint has verified support. The capability is explicit and defaults off;
prompt-only fallback keeps the existing validators. Source extraction preserves
complete short quotations and stores a procedure's evidence directly as its
value. Its task distinguishes actual-world assertions from narrative details,
while retaining reported real events, physical props and conditional procedures.

New source claims receive one batched admission review through the existing
judging role after mechanical grounding. The review selects supported proposals
without rewriting them, retains separate unverified provenance, and leaves
unavailable or malformed reviews pending. Original sources remain searchable.
The existing worker runs claim projection as one bounded task alongside its
other projection tasks, so a slow review does not stall image or vector work.

Appraisal output distinguishes durable scoped preferences from artifact requests,
and repair records require the supported satisfaction transition. New working
judgments normalize an absent predecessor only when no prior head exists.

Recollection preserves distinct dated source occurrences and marks incomplete
excerpts. Partial or malformed reranker output retains the original ranking
instead of mixing score types. The native request middleware corrects Hermes
0.21.1's qualified authoritative-memory note without changing quoted evidence,
source revisions or erasure checks. Shared context guidance also reaches direct
consumers such as voice adapters.

Literal memory markup in trusted system or developer instructions no longer
discards the remaining instruction text. Exact erased-source copies and explicit
Protagine lineage packets still follow the existing erasure boundary, including
Responses instructions and multimodal text parts.

No historical memory is rewritten, no database migration is needed, and no new
model or approval service is introduced. Structural checks do not establish
truth or semantic quality; qualify the actual deployed models, generation
settings and identity instructions. Hermes 0.21.1 remains the qualification
target, with its core unchanged.

## v1.0.26 - explicit legacy compatibility boundaries

Goal record imports no longer eagerly load optional decomposition, inference and
replanning modules. Explicit legacy exports retain their original class identity.
In live native cognition mode, the deprecated goal-creation endpoint returns a
conflict with a current acceptance hint instead of accepting work without native
execution authority. Historical goal reads, updates and reported completion remain
available; off and shadow modes keep their legacy creation contract. Existing
goals are neither deleted nor automatically adopted into native tasks.

Legacy persona setup reports that host identity, configuration overlays and
plugins were not installed by its logging-only host step. Its service, channel
and saved-manifest compatibility remains. New installations use `protagine init`.
Existing manifests and configured graph stores still need proven export/restore
migrations before their compatibility paths can be retired.

This release does not change semantic memory admission or claim a memory-quality
increase. Hermes 0.21.1 remains the qualified runtime.

## v1.0.25 - runtime repairs and removal of unused scaffolding

Native feed tools now use the calling tool context when recording their origin,
including concurrent calls. Optional review distinguishes a completed review
from an unavailable or invalid result instead of silently treating every result
as unflagged. Review remains disabled by default.

The retired context plugin, unused strategy helpers and unsupported worker
branches have been removed. Source tests live outside shipped packages. Legacy
planner and persona setup paths are documented as compatibility surfaces; current
deployment uses durable accepted work and source-backed appraisals. Generated
harness instructions use the actual feed API and stop requesting automatic
numeric mood estimates. Existing memory, journals and compatibility readers are
preserved. This release makes no claim of improved semantic memory admission.

Hermes 0.21.1 remains the qualified runtime. Native installation qualification
uses the adapter's required private directory permissions.

## v1.0.24 - current recollection and durable transport receipts

A rejected or uncertain claim on an identified commitment holds ordinary tools
for that turn until explicit detachment. Another holder and unrelated turns keep
their own state. Automatic recollection removes earlier injected packets before
supplying the current turn's context, so withdrawn relationship guidance cannot
survive merely because no source was erased.

Trusted channel adapters can record durable intake metadata in the existing
communications store. Exact verified handles resolve participants; accepted
canonical sources settle processing receipts. Pending payloads stay with their
transport owner, including across unknown handoffs. Source erasure marks linked
transport receipts for cleanup. Intake, processing and provider effects remain
separate. Followup review requires a fresh observed connection interval without
gaps or unresolved recipient activity; original message authority is unchanged.

Existing work views retain independently recorded semantic findings and original
runtime forecast decisions. Forecast suggestions remain disabled pending measured
value. This adds no scheduler, payload database or model judge. Hermes 0.21.1
remains the qualified unmodified runtime target.

Ordinary turns no longer generate legacy numeric mood estimates or inject them
into current context. Fresh-install qualification found unsupported mood numbers
derived from a neutral fact and a request for accurate recall. Source-backed
appraisals, preferences and working judgments remain the active representation;
explicit historical affect APIs remain available. Enriched context also accepts
the actual SQLite contact object instead of assuming a mapping.

Guided attachment checks the selected adapter's existing conversation-storage
requirements before making its model probe. An unsuitable directory produces an
actionable setup error without changing shared permissions.

## v1.0.22 - complete native recollection and shared work results

Fresh attachment and explicit adapter refresh align Hermes's supported hook
spill allowance with Protagine's existing memory context, including generated
draft profiles. The default 10,000-character spill could replace selected
evidence and its source revisions with a truncated preview. The 65,536-character
allowance preserves the observed default packet; disabled or larger operator
settings remain unchanged. Retrieval budgets do not increase, and arbitrary
larger custom context still needs its own allowance. Existing Hermes processes
must restart through their normal lifecycle to reload this setting.

Existing local worker reports can retain task and parent identity, measured
progress, process exit details and result references in the shared owner work
view and automatic request context. Old heartbeat records remain compatible.
Stale running reports remain uncertain; explicitly reported terminal results
retain their original times and never imply independently verified effects.

References are bounded metadata and are not followed by the reader. Private
producers continue to own their local reports; no worker, scheduler, queue or
authority path is added. This backend update remains compatible with the
existing 1.0.21 native adapter and Hermes 0.21.1.

## v1.0.21 - Hermes 0.21.1 native compatibility

Attachment accepts Hermes 0.21.1 and retains 0.21.0 support. Native task dispatch,
draft notifications and staged skill changes use the new canonical modules,
with an old-runtime fallback that does not depend on expiring import shims.
Native integration fixtures patch the actual client and tool owners after
upstream's module split. The pinned native CI target is the exact 0.21.1 release.

Existing source memory and adapter configuration require no data migration.
Install the updated adapter before upgrading Hermes; Protagine never changes the
runtime or restarts a gateway during attachment.

## v1.0.20 - distinguish time requests from pasted evidence

A timestamp inside a pasted report no longer turns recollection into a request
for sources recorded at that exact instant. Temporal interpretation excludes
clearly delimited JSON, code and quoted evidence while ordinary retrieval keeps
the complete original query. Explicit time operands retain their existing
precision and supported range behavior.

The native adapter and source-reference protocol are unchanged. This correction
requires a backend update; an existing 1.0.19 adapter remains compatible.

## v1.0.19 - source corrections stay with recollection

Authenticated memory writers can append attributed corrections to an exact
source revision and excerpt. The original source stays unchanged; annotations
are assistant evidence, with server-recorded authorship and no owner-fact
promotion. Lexical and semantic recollection carry the original and its
corrections as one indivisible packet through the existing native provider.

Corrected evidence retains both source dependencies for derived-answer erasure.
A tight context budget omits the whole packet, and an annotation-only search hit
does not duplicate a correction already carried by an original excerpt.
Conflicting corrections remain visible rather than automatically selecting the
latest claim as truth. See docs/SOURCE-ANNOTATIONS.md for scope and rollback limits.

Corrections follow the recalled message's provenance, so independent messages
and surviving atomic facts do not inherit a sibling's correction or erasure.
Corrected graph beliefs retain their underlying identity for recall reinforcement.

The native `protagine_memory_annotate` tool lets an attested owner or system turn
append a correction to a source revision actually supplied to that turn. Identity
and idempotency come from the existing native context. An unknown acknowledgment
can be retried with identical arguments in the same turn without another source.
The source-reference protocol is unchanged; the new tool requires adapter refresh.

## v1.0.17 - persistent worker ownership and useful review recurrence

Paired Hermes profiles resolve memory and tool ownership from their persisted
Protagine plugin and memory-provider selections. Cold workers can register the
completion-report callback without inherited launcher flags. The memory provider
still leaves turn writing and mutation tools to the general plugin. Explicit
profile deselection and legacy standalone configuration retain their meanings.

Known unchanged backup and service conditions use the existing review intervals
from native completion, so a completed old proposal does not immediately create
new work in the current time bucket. Real changes in observed evidence can rearm
a review sooner; an active review still suppresses concurrent duplicates.

A deployment can optionally supply a local backup-attempt receipt instead of
using legacy `.bak` file age. Capture time, failure and unavailable evidence stay
distinct. The detector verifies receipt references, not archive integrity or
restore readiness. No scheduler, backup executor or memory store is added.

## v1.0.16 - initiative status filtering

The existing initiative list endpoint now passes a single requested status to
the store correctly. Requests for `pending` or `assigned` work no longer return
an empty list because the status was treated as individual characters. Omitted
or empty status remains unfiltered, with existing agent and limit behavior.
This sidecar correction adds no native worker or dispatcher changes.

## v1.0.15 - native review undertakings and retained completion reports

Selected generated internal reviews can become shared native Hermes tasks
through `protagine_work_initiative`. The existing initiative ledger retains one
task association across repeated cycles and lost acknowledgments. Native
completion, exhausted failures and needs-input blocks remain distinct in shared
work views. These reviews use the default native profile and its existing
tools; read-only classification specifies their purpose, not a sandbox.
Effectful actions retain their existing authorization path.

The local setup wizard offers a separate default-no `--native-goals` opt-in on
the existing Hermes profile. It enables native task tools and selected-board
observation, preserves explicit model and authority settings, and reports
conflicting dispatcher or ledger bindings. Existing YAML aliases remain intact
outside the changed configuration. Hermes supplies the dispatcher and worker;
Protagine does not start its gateway or add another executor. Kanban availability
is profile-wide, and existing consent rules still apply.

Attested native worker completions can retain their committed summaries as
machine-authored assistant evidence through the existing durable outbox. Later
sessions can recall these reports after the operational history window expires.
Canonical sources supplied to generation remain linked, so source erasure also
removes dependent retained reports. Worker instructions do not become user facts
or claim-extraction inputs. This is forward-only callback capture, not a history
backfill or proof that the worker's reported result is correct.

Relationship context now shows recorded interactions, last interaction and
contact tier without translating message volume or contact mood into an
unsupported closeness percentage. Existing scores, state and authority remain
compatible.

## v1.0.14 - reject invalid native skill proposals before staging

Background review no longer reports a stored skill proposal for malformed batch
arguments that native Hermes cannot replay. Batches require a nonempty array,
supported operations and target names, respect the native 20-operation cap, and
keep deletion as a sole operation. Valid legacy flat operations remain supported.
Existing pending proposals and active skills remain unchanged. This corrects
proposal admission; it does not establish a learned improvement or automatically
evaluate or activate a proposal.

## v1.0.13 - shared native goals and visible memory admission

Owner sessions can observe selected native Hermes Kanban boards through the
existing current-work context. General tasks expose their native state, current
attempt, configured goal budget and recent terminal record. Board coverage and
omissions remain explicit. Hermes retains dispatch, continuation and recovery;
Protagine adds no second task executor. Native dispatcher and delegated worker
instructions stay out of owner-source capture, including compression and legacy
sync paths, while their native task and session records remain available.

The legacy goal API can now record reported completion without reaccepting an
already accepted goal. The response identifies unavailable dispatch and reported
completion; a status change does not establish an external effect.

Existing source-claim jobs expose bounded admission counts and response
provenance, including when extraction yields no claims. Empty model output and
validator rejection are distinguishable without retaining raw model responses.
Diagnostics follow the job's attempt and erasure lifecycle. Extraction prompts
and admission rules are unchanged.

## v1.0.12 - forgetting follows supplied evidence into later answers

Native recollection now records the exact canonical source revisions supplied
to generation. Erasing one of those sources also removes later captured
assistant answers that depended on it, including paraphrases and delayed outbox
delivery. Independent user messages survive partial erasure. Selected sourced
preferences and working views contribute their existing canonical dependencies.
The owner can select exact recalled source IDs through `protagine_memory_forget`.

This is conservative, forward-only provenance: an entire assistant answer can
be removed when any supplied source is erased. It does not discover historical
unlinked copies or delete native transcripts, backups or external artifacts.
Downgrading the backend retains new linked envelopes in the outbox instead of
accepting them without their dependencies. Existing readers retain access to
surviving user evidence. No Hermes core patch or new memory service is added.

Multimedia sources also skip the text-only prior-claim lookup. Their ordinary
image and semantic projections continue, while claim jobs finish instead of
repeatedly failing on list-valued content.

## v1.0.11 - complete function output and explicit adapter refresh

Function routing rejects empty, reasoning-only and truncated responses within
the existing fallback budget. Requested tool calls remain valid output.
Repeated identical standing-boundary quotations appear once per polarity in
context. Explicit `protagine init --refresh-adapter` refreshes copied adapters while
preserving private identity, state and the previous adapter for recovery.

## v1.0.10 - keep caller deadlines separate from endpoint outages

A model that exceeds one function's request allowance remains eligible for
another function. Previously, extraction's short timeout could suppress the
same model for a later reasoning request with a longer allowance. Caller expiry
now reports `RequestBudgetExceeded` in the existing attempt history and final
failure reason. Transport timeouts and other genuine endpoint failures retain
their existing cooldown behavior.

Request limits, finite fallback order and model configuration are unchanged.
An endpoint that only stalls until the caller's timer expires remains eligible
for a later request; the router does not infer an outage from that event alone.
Controlled tests cover cross-role use, an expired recovery slot, cancellation,
and actual transport failures. No model call or new routing service is required
to verify this distinction.

## v1.0.9 - durable first-person reply preferences

Explicit general communication preferences such as "I prefer brief replies"
now enter the existing sourced preference state through ordinary owner turns.
Later context includes them independently of semantic recall ranking, with
source attribution, correction and forgetting preserved across sessions and
model changes. Requests for a particular artifact remain ordinary evidence.

The parser supports a limited direct vocabulary; it does not infer every
natural-language preference. Model compliance with supplied guidance remains
distinct from successful capture and context delivery. No new state store,
memory service or Hermes patch is required.

## v1.0.8 - shared undertakings and useful recall capacity

Repeated exact quotations from one known participant no longer occupy multiple
slots in the same undated memory packet. Selection preserves one intact source
record after claim expansion and time filtering. Different speakers, checkpoint
text, uncertain attribution, assertion/conflict bundles and time-qualified events
remain distinct. No canonical sources or stored memories are removed.

Two sessions accepting local drafts against the same open commitment now share
one active initiative and native Hermes task, including differently worded
requests. The response exposes the canonical scope. Historical acceptance
replays retain their original result; an explicit fresh draft is admitted after
prior work finishes. Standalone drafts retain their per-turn acceptance identity.

An accepting session hands its exact work lease to the existing native dispatch
path. Retried acceptance reconciles interrupted persistence without releasing a
newer worker's token. The attached SQLite databases serialize ordinary acceptance;
their WAL files are not claimed to commit atomically across a host crash.

Packaged Hermes tests exercise two sessions, one worker, one retained report and
consistent result replay. These checks establish the execution contract, not
universal agreement between free-form promises or the quality of a model's draft.
No new scheduler, service or Hermes core patch is required.

## v1.0.7 - retry incomplete memory extraction

Malformed final extraction output now leaves its projection job pending through
the existing retry path. Previously an invalid JSON response or non-array
envelope could silently complete with no claims. Canonical source text remains
available; a valid empty result still completes without promoting junk memory.
Recovery tests reopen the ledger, retry extraction and recall the retained
evidence from a later session. Existing completed projections are not replayed.

## v1.0.6 - traceable native learning reviews

Native background reviews now carry bounded references to actual failed tool
calls from their originating request. Proposed skill changes and their existing
evaluation records retain the observing session, turn, tool-call identifier
and request-visible result hash. Model-written provenance is replaced by this
captured evidence; reviews without eligible evidence record its absence.

The real Hermes request hook and background-review fork are covered by failed
read, successful-read and guest controls. This establishes traceability, not a
measured learning gain, and does not automatically adopt a skill or add another
learning service. See [native review evidence](docs/NATIVE-REVIEW-EVIDENCE.md).

## v1.0.5 - native draft execution

- Accepted local source drafts use the stock Hermes Kanban dispatcher and a
  constrained native worker profile. Protagine retains consent, commitment scope,
  source/report validation and current-work projection; Hermes owns attempts,
  crash recovery and native completion delivery. Saved receipts reconcile an
  interrupted acknowledgment without redrafting. Recovery may use a finishing
  model call. Native hooks and the profile-scoped completion tool override require
  no Hermes core patch.
- Setup creates the board/profile and refreshes local planning bindings for
  future worker processes. Pending legacy drafts migrate; in-flight cron
  assignments drain before the old draft job is paused. Fresh installs create
  no polling job. Reports remain unverified interpretations and do not fulfil
  broader commitments.

- Removed the experimental evidence-recall and work-handoff task skills and
  the setup wizard's `--task-skills` option. Sixteen runs across four synthetic
  tasks and two local processors compared skills present versus absent and
  showed no consistent benefit.
  Existing private skill copies are left untouched. Memory and work tools
  continue to operate through the native Hermes adapter.

## v1.0.4 - bounded memory selection

- Combined recall submits at most four times the requested packet count to the
  inline reranker. The same timeout applies; disabled, shadow and unavailable
  reranking retain the full original candidate set. Successful selection uses
  the submitted set without mixing unsubmitted rank-fusion scores into model
  scores. The bound can exclude lower-ranked evidence and does not establish
  an abstention threshold for unrelated queries.
- The setup wizard can optionally install two original native task skills for
  evidence recall and work handoff. They use ordinary Hermes discovery, remain
  opt-in and preserve existing local skill directories. Availability does not
  establish better task outcomes or enforce work ownership.

## v1.0.3 - accepted native local work

- Explicit owner acceptance can associate a local source comparison or summary
  with an open commitment. One selected native Hermes job executes it using
  existing initiative, undertaking, session and execution records.
- Selected reads and final result acceptance honor cancellation. Synced local
  draft receipts reconcile after interrupted delivery without duplicate inference.
  Current work exposes pending tasks and unverified reports; a draft does not
  automatically fulfil its broader commitment or authorize an external effect.
- Goals remain durably accepted when no execution queue is configured instead of
  claiming dispatch into a process-local in-memory queue. Explicit queue backends
  retain their existing behavior.

## v1.0.2 - scoped self evidence

- Default native per-turn context assembly uses query-dependent contact-knowledge
  candidates and the common memory selector/budget instead of an unconditional
  extra context section. The explicit legacy enriched-context API is unchanged.
  Existing visibility, expiry, source erasure and explicit listing remain intact;
  selected estimates are labeled unverified.
- Runtime completions, failures and timeouts no longer produce automatic
  research-priority weights or claims of task quality and current-model ability.
  The self brief labels their historical, potentially unverified evidence before
  listing outcome counts.
- Explicit source-backed owner preferences still govern optional research
  ordering and retain their correction and erasure behavior. Dated attention
  records expose the applied preference and priority.
- Existing automatic opinion revisions remain inspectable as non-governing
  history, with original evidence and model provenance. Operational records and
  their append-only corrections are preserved; trust and grant paths are unchanged.
- Valid memories without person links no longer produce orphan-repair proposals.
  Both initiative paths ignore the retired classifier while retaining other
  orphan evidence and schema drift detection. Memory contents and links are unchanged.

## v1.0.1 - fresh setup and local work visibility

- Fresh Hermes environments no longer need the legacy Colony CLI dependency
  `typer` to pass setup. The wizard attaches the private native adapter using
  the selected Hermes core environment.
- Owner current-work views include accepted local capability briefings and
  their bounded, unverified results from the existing initiative ledger.
  Ordinary context includes only the latest result; guest views exclude it.
- Successful named-role responses record prior failed or skipped candidates
  alongside the actual processor, without exposing endpoint or credential data.

## v1.0.0 - persistent native core

Protagine 1.0 defines a supported core beside Hermes, with private identity and
hardware integrations supplied by each deployment.

- **Native Hermes integration:** packaged general and memory-provider adapters,
  qualified against Hermes 0.21.0 through its native loaders, hooks, middleware,
  compression checkpoints, delegation and review contracts. No Hermes core
  patch is required for this integration.
- **Canonical, selective memory:** durable source capture and one quoted-claim
  promotion path retain provenance, corrections and conflicts. Raw conversation
  history remains distinct from asserted knowledge. Retained image originals
  and fallible descriptions have linked source identities.
- **Replaceable retrieval:** lexical recall works without embeddings. Optional
  semantic indexes record embedding identity, rebuild into resumable generations
  and hydrate results against current sources before context selection.
- **Scoped recall and forgetting:** participant-bound context and source erasure
  extend through linked projections, new affect/engagement observations, outbox
  replay and exact native request copies. Historical unlinked records, arbitrary
  paraphrases, native transcript storage and backups are not globally erased.
- **Shared work and perspective:** persistent commitments and work claims,
  native execution observations, source-backed owner preferences and bounded
  operational judgments make selected state inspectable across sessions. This
  does not guarantee conflict-free promises or a complete process inventory.
- **Evaluated native skills:** native review stages proposals; an explicitly
  selected task evaluator can compare, activate, verify and roll back an existing
  curator-owned skill. Results apply to its declared cases, not general
  self-improvement. No automatic evaluator is enabled by the lightweight setup.
- **Local installation and operation:** guided private setup supports one local
  chat endpoint, SQLite, profile-scoped diagnostics and user services. Named
  model roles can reload eligible endpoint bindings and use fallbacks. Source
  backups include ledger-owned original images with explicit recovery coverage.

Upgrade the adapter and sidecar together, retain private profiles and source
backups, and qualify any new Hermes version before activation using the
[native checks](docs/HERMES-ADAPTER.md#qualification). The
[setup guide](docs/LOCAL-HERMES-SETUP.md) covers attachment and service recovery;
[memory recovery](docs/SOURCE-MEMORY-RECOVERY.md) documents backup boundaries.
Extended autonomy loops, public channels, voice and hardware adapters still need
deployment-specific qualification. This release does not claim subjective
feelings or completion of every autonomous-agent acceptance behavior.

## v0.34.0 — governed actions, cognition spine, fail-closed hardening

**Preset-loop coupling — default flip + migration note**
(`util.autonomy_preset`, `autonomy.config`): an active
`PROTAGINE_AUTONOMY_PRESET` now supplies the autonomy loop mode when
`PROTAGINE_AUTONOMY_MODE` is unset (passive: `reactive`;
calibration/autonomous: `proactive`), governed by
`PROTAGINE_PRESET_LOOP_COUPLING` (new, default `on`). MIGRATION: a deployment
running `PROTAGINE_AUTONOMY_PRESET=calibration` or `=autonomous` WITHOUT an
explicit `PROTAGINE_AUTONOMY_MODE` will come up with a proactive loop on next
restart — previously it silently stayed reactive and none of the
preset-enabled subsystems ever ran. This is the intended fix for the
"everything looks enabled, nothing ever ticks" misconfiguration. Rollback:
set `PROTAGINE_AUTONOMY_MODE=reactive` (explicit env always wins) or
`PROTAGINE_PRESET_LOOP_COUPLING=off` (restores the old env-only resolution
exactly). Resolution precedence: explicit env > coupled preset > legacy
tick-interval migration > default; coupling errors fail toward reactive.
Annunciation: the server logs a startup WARNING whenever the mode is
preset-inherited, records one durable action-journal entry (domain
`preset_coupling`) on the first coupled boot, and
`GET /v1/host/autonomy/posture` now reports `PROTAGINE_AUTONOMY_MODE_SOURCE`
(`env`/`preset`/`legacy_tick`/`default`) plus the coupling flag. `protagine
doctor` reworded: FAIL only for an explicit reactive pin under a
calibration/autonomous preset or coupling switched off under a preset; a
coupling fail-safe (reactive despite coupling on) is WARN.

**Preset activation — expectations + workspace** (`util.autonomy_preset`):
`PROTAGINE_EXPECTATIONS` and `PROTAGINE_WORKSPACE` are now managed by
`PROTAGINE_AUTONOMY_PRESET` (calibration: expectations `on` + workspace
`shadow`; autonomous: expectations `on` + workspace `live`; passive: both
`off`). Deployments running under a preset WITHOUT these env vars set will
see both subsystems come on at the preset's default on restart — that is
the intended activation. An explicitly set env var still always wins in
both directions, so deployments that pin `PROTAGINE_EXPECTATIONS` /
`PROTAGINE_WORKSPACE` themselves are unchanged. `PROTAGINE_EXPECTATIONS=on` is
newly accepted as the canonical enabled value (`shadow`/`live` remain
aliases). Two world-model prediction resolvers (relationship-still-active,
property-unchanged) now register with the expectation engine at boot.

## v0.33.0 — comms ledger reads + concern-to-memory provenance

Two additive, backward-compatible reads that let an operations surface show
what the graph already knows.

**Cross-channel comms ledger** (`contacts.comms`): the ledger that
`/turns/sync` already writes was only readable per contact. Added
`CommsLog.recent()` and `CommsLog.rollup()` plus `GET /v1/host/comms/recent`,
so a caller can pull the newest exchanges across ALL contacts along with
in/out counts per channel over a window. Read-only, contact ids resolved to
display names.

- `GET /v1/host/comms/recent` — `limit` + `window_days`; returns recent
  cross-channel exchanges and a per-channel in/out rollup.

**Concern to memory provenance** (`self_model.workspace`): the thinker
already recalls memories per thought; it now keeps the hit ids and records
them on the concern as `memory_refs` (deduped, capped, most-recent first)
via a backward-compatible `ADD COLUMN` migration, exposed in
`Concern.public()` and thus on `GET /v1/host/self/workspace`. This gives a
real concern to memory link (for a provenance/beam view) rather than a
render-time similarity guess: the field is empty until a concern is actually
thought about, never fabricated.

## v0.32.1 — configure response accepts object-spec tiers

v0.32.0 added per-tier object specs to `POST /v1/host/configure`, but
`HostConfigureResponse.models` was still typed `dict[str, str]`, so a
multi-endpoint configure returned HTTP 500 on response serialization —
the router was rebuilt correctly and the config persisted (both happen
before the response is built), but the caller saw a failure. Widened the
response echo (and the unused `LLMModelsConfig` helper) to accept bare
strings or object specs. Found configuring a live GLM cutover.

## v0.32.0 — context gate + multi-endpoint model tiers

Two related capabilities for running big models well:

**Context gate** (`protagine.contextgate`): a model's *useful* context
window — the range over which exact retrieval stays reliable — is often far
below its advertised maximum, and stuffing a huge document in silently
degrades recall. The gate decides per call whether content fits whole, and
when it doesn't, chunks it structure-aware (code fences atomic, then
headings, then paragraphs, with overlap) and either retrieves the chunks
most relevant to the query (embeddings when injected, dependency-free
lexical TF-IDF otherwise) or — for holistic tasks like "summarize the whole
thing" — map-reduces via a caller-provided LLM callable, degrading to
evenly-spaced coverage sampling. Query-focused vs holistic intent comes from
an explicit `task_kind` or a heuristic classifier.

- `POST /v1/context/prepare` — any agent can gate content over the API
  (`content`/`documents` + `query` + `budget_tokens` or `model_tier`)
- Inference jobs gate automatically against the target tier's budget
  (opt out per job with `payload.context_gate = "off"`)
- Config: `PROTAGINE_CONTEXT_GATE` (auto/on/off), `…_HEADROOM`, `…_BUDGET`,
  `PROTAGINE_CONTEXT_CHUNK_TOKENS`, `…_OVERLAP_TOKENS`, `…_CHARS_PER_TOKEN`

**Multi-endpoint model tiers**: `POST /v1/host/configure` `models` values
may now be objects, not just model strings — per tier: `baseUrl`, `apiKey`,
`extraBody` (request fields forwarded verbatim, e.g. vLLM's per-request
`priority`), `usefulContextTokens` (feeds the context gate), and
`maxTokens`. Different tiers can live on different servers: a fast small
model on one endpoint, a large reasoning model on another. String specs
keep working unchanged. The Hermes plugin exposes the same per tier via
`llm_<tier>_base_url` / `_api_key` / `_extra_body` / `_useful_ctx` /
`_max_tokens` (and `PROTAGINE_LLM_<TIER>_*` env vars).

## v0.31.1 — env secrets backend reads its own file

`EnvBackend.get()` only consulted `os.environ`, so a fresh process (the
secrets CLI, a migration) saw None for every secret `set()` had written to
`.env`. It now reads the file back. Found during a live migration to the
keyring backend.

## v0.31.0 — multi-account senses

One connector, many accounts: set `connector/<name>/accounts` (secret or
env) to a comma-separated list and each account becomes its own instance
with its own `connector/<name>_<account>/*` credential namespace. Built for
several mailboxes/calendars feeding cognition side by side; listed accounts
default on, each can be disabled individually, and email observations are
attributed to the receiving account.

## v0.30.2 — `protagine secrets` is a real command

The secrets CLI handlers existed but were never registered with the main
parser; `protagine secrets set KEY VALUE` (as documented for setup-once
connector credentials) now works: list / get / set / delete / backend /
status.

## v0.30.1 — set a connector credential once

`ConnectorConfig` now resolves env-first, then the Protagine encrypted secrets
store (`connector/<name>/<key>`), then the default. A credential like an IMAP
password or a private ICS URL is entered ONCE — `protagine secrets set
connector/calendar/ics_url <url>` or `POST /v1/host/secrets/set` — lives
encrypted, survives service redeploys, and never has to be copied into a
plist or unit file. An explicit env var still always wins, and enable flags
work through the same path (restart to register a newly-enabled connector).

## v0.30.0 — gap closure: production-ready wiring

Every entry in docs/KNOWN-GAPS.md was prioritized and either fixed, wired
real, or retired with its reason recorded. Boundary rule applied throughout:
Protagine is the cognitive substrate; sessions, tool execution, transport, and
cron belong to the host agent framework.

- **Goals unblock themselves**: `block_goal` accepts an external condition
  (`condition_type`/`condition_params`, also via `PATCH /goals/{id}`); the
  autonomy loop polls it at the condition's cadence and reactivates the goal
  when met. Concurrent-update safe, per-goal fault-isolated.
- **Briefings carry real content end-to-end**: anomaly, synthesis, and
  calendar aggregators join relationship + goals — detector-backed anomalies
  with honest severity labels, discoverer-backed cross-domain insights with
  dismissal filtering, and ICS-connector-backed calendar sections
  (chronologically correct across a week). Mind-model stays a stub on
  purpose: no real health source exists, and fabricated readiness numbers
  are worse than none.
- **World-model hygiene**: a real prune primitive (stale low-confidence
  entities; FK cascade + FTS-consistent; exact counts) on the config TTL
  knobs, and the daily task is back — doing the work it reports.
- **Self-knowledge grounding**: questions about Protagine itself get the
  identity-bootstrap corpus injected into assembled context.
- **Retired**: ScheduleAdapter (contracts unimplementable on both ends;
  host-cron mutation crosses the framework boundary) and the orphaned
  EmailHandler (delivery is the host gateway's job). The
  `cognition.requested` spawn spec remains a host-plugin integration point
  by design.
- README refreshed (Mind track, resolution semantics, self-unblocking goals,
  briefing reality) and points to KNOWN-GAPS as the authoritative
  scaffolding-vs-live inventory.

## v0.29.0 — the honesty release: dead features wired real, fabricated statuses removed

A three-round audit (stub/marker sweep, correctness hunt, dead-wiring hunt,
then an adversarial re-review of the fixes themselves) across the sidecar and
plugins. Headline items:

- **Silently-dead features fixed**: active goals never reached assembled
  context (invalid kwarg swallowed by a broad except); the goals tool always
  errored (nonexistent method); open follow-ups never reached outreach
  evaluation (dict iterated as list); briefings composed every data section
  from stubs — permanently empty (relationship + goal aggregators now wired,
  new `GoalEngineAggregator`); the condition worker's system checks never ran
  (now an hourly loop phase — the pending→overdue flip actually fires).
- **Fabricated status removed**: five scheduler tasks were no-op lambdas
  reporting `ok` on their intervals forever. health_check and cpi_track now
  do real work; the rest are gone until their primitives exist
  (docs/KNOWN-GAPS.md).
- **/timeline actually shows recent activity**: the journal walk gained
  `newest_first`, and the cap's `hasMore` off-by-one and types-blindness are
  fixed.
- **Leaks plugged**: strong references for fire-and-forget tasks (mesh
  scheduler, world-model updates, recall touches), per-run neo4j driver
  closed in the research gatherer, world-LLM timeout capped under the tick
  budget (the "Unclosed client session" spam source), connector polls moved
  off the event loop, briefing push deadlock removed.
- **Boundary controls fail closed**: connector directive-blackout check no
  longer converts an internal error into permission.
- **`protagine autonomy status|cycle`** now really talks to the running sidecar.
- **docs/KNOWN-GAPS.md**: an honest inventory of scaffolding that exists but
  is not wired, so status claims match reality.

## v0.28.0 — resolution that sticks: settlement, learning loop, agent tools

The headline bug: resolving an overdue-commitment concern from the command
center only settled the workspace concern; the commitment stayed open, the
next ingest tick re-raised it, and the owner's resolve was silently undone
minutes later. Resolution is now a first-class, cascading, learnable act:

- **Source settlement** (`self_model/settlement.py`): a concern raised from a
  durable source (`sources=["commitment:<id>", ...]`) settles that source when
  the concern is resolved. Registry-based — deployments wire settlers for the
  stores they run; a commitment settler ships wired.
- **Resolve with WHY**: outcome `done | invalid | duplicate | wont_do |
  obsolete` on the concern-resolve endpoint, `PATCH /commitments/{id}`, MCP
  fulfill/cancel, and the new Hermes tools. The resolution {outcome, note, by,
  at} is recorded on the commitment and emitted as an event.
- **Re-raise suppression**: a resolved concern's dedup_key is not re-raised
  while the resolution is fresh (`PROTAGINE_WORKSPACE_RESOLVED_TTL_HOURS`,
  default 24), so a resolve sticks even against a still-open source.
- **Reverse cascade**: settling a commitment directly (agent tool, MCP, API)
  resolves any workspace concern raised from it.
- **Open-status model unified**: `get_overdue()` and per-person open-item
  queries include `overdue` rows; the pending→overdue flip no longer hides
  items from dedup lists, prompt sections, or workspace ingest (and the flip
  only fires once per item).
- **Extraction learning loop**: the per-turn introspection extractor now
  enforces dedup in code against open items AND recently rejected ones
  (cancelled as invalid/duplicate), and its prompt carries both lists.
  `GET /commitments/stats/resolution` exposes per-source outcome stats as the
  calibration signal. `POST /commitments` accepts `dedupe` to return an
  existing open twin instead of creating one.
- **Agent surface**: Hermes plugin tools `protagine_list_commitments`,
  `protagine_create_commitment` (dedupes), `protagine_resolve_commitment` (with
  outcome + reason) — any Protagine agent can generate AND resolve items and
  learns from what the owner rejects.
- Hermes plugin repo/live drift healed: initiatives-based task handlers and
  the `plugins.protagine.autonomy_prompt` / `autonomy_deliver` deployment seam
  are now in-repo alongside the sender-attribution redesign.

## v0.27.1 — autonomy-loop hardening + operator controls

Fixes from live operation and the command-center build:
- The autonomy loop can no longer be frozen by a slow or hung phase: the
  whole tick is bounded (`PROTAGINE_TICK_BUDGET_SECS`, default 80% of the tick
  interval), the toolsmith/workspace phases carry per-phase budgets, the
  toolsmith's Docker sandbox calls moved off the event loop
  (`asyncio.to_thread`), and the benchmark's recall probes are individually
  bounded. A cancelled tick is safe (phases re-run next cycle).
- New `fd-limit` doctor check reads the SIDECAR's own open-file limit from
  `/health` — a low limit (macOS default 256) silently breaks LanceDB vector
  recall with "Too many open files" and degrades recall to the slow keyword
  path; the check warns with the launchd/systemd/ulimit remedy.
- The workspace anomaly ingest read the wrong registry property and silently
  never fired; fixed with a regression test over all ingest sources.
- Expectations generate on every phase pass (create() dedups), so a newly
  due-dated commitment gets its prediction within a tick.
- `POST /v1/host/self/workspace/{id}/resolve`: owner control to settle a
  concern (the command center's concern action).


## v0.27.0 — expectation engine: predictions, surprise, calibration (Mind M3a)

The agent forms explicit predictions and checks them against reality. A
prediction carries a subject, a confidence, and a horizon; a checker
resolves each at its horizon through a pluggable resolver (one built-in for
commitments — "this gets fulfilled by its due date"). A miss becomes a
surprise that raises cognitive-workspace salience, the attention signal
worth thinking about; the hit/miss record produces a per-domain Brier
calibration score. That score finally sources the benchmark's calibration
metric (`calibration.<domain>`, expressed as accuracy = 1 - Brier so higher
is better), so "does she predict well, and is it improving" becomes a real
trend line. Predictions are generated from pending commitments, with
confidence set by the agent's own historical fulfillment rate.

New `protagine/self_model/expectations.py` (prediction store + engine
with a resolver registry). `GET /v1/host/self/expectations`, an autonomy
phase that generates hourly and checks every couple of ticks (linking the
workspace so surprises land), a `server-expectations` doctor check, and an
Expectations panel on the Operator Deck. Gated by `PROTAGINE_EXPECTATIONS`
(off | shadow | live, default off).


## v0.26.0 — cognitive workspace: continuity of thought (Mind M2)

The agent now carries something on its mind between interactions instead of
only reacting. A bounded workspace holds active concerns
(question/goal/thread/anomaly/maintenance), each with a salience score that
live signals raise (overdue commitments, anomalies, benchmark regressions)
and time decays. A scheduler runs every few ticks: it decays the salience
field, evicts the floor and anything over capacity, and runs one bounded
thinking job on the most salient concern that still has budget. During a
configured nightly sleep window it thinks several times per pass, when the
cluster is idle. A thought either makes progress (salience sustained), gets
stuck (salience decays harder, so rumination fades on its own), or resolves
(the concern leaves the mind). In live mode a thought can surface an
initiative to the owner or propose a self-experiment, both through the
existing gates; in shadow it only thinks and journals.

New `protagine/self_model/workspace.py` (concern store + engine) and
`thinker.py` (the model-agnostic LLM reflection, injected so the engine is
testable). `GET /v1/host/self/workspace` exposes what is on her mind and
drives the Operator Deck's on-her-mind panel. Doctor check
`server-workspace`. Gated by `PROTAGINE_WORKSPACE` (off | shadow | live,
default off), `PROTAGINE_SLEEP_WINDOW`, and capacity/half-life/budget env.


## v0.25.0 — toolsmith: the agent builds its own tools (Mind M1)

Compounding capability. The agent now extends itself: a daily autonomy
phase mines the action journal for recurring procedures, the LLM drafts a
pure standard-library tool (source, input schema, and a self-contained
test), and the test runs inside the egress-none Docker sandbox as
verification. A tool that passes enters shadow, where re-running its test
accumulates clean runs; once it has enough and the toolsmith trust domain
allows, it graduates to live (owner-approved, or automatic once the domain
reaches act_first). Live tools are advertised to the reasoning loop through
a new dynamic-tool provider on the tool executor and run in the sandbox
when the model calls them. Tools that go unused or start failing retire
themselves.

New subsystem `protagine/toolsmith/` (persisted registry + journal
miner + engine), deliberately independent of the runtime `skills/`
executor registry. Composes the shipped Docker sandbox, trust engine,
action journal, and LLM router. Surfaces: `GET /v1/host/self/tools`,
`GET .../tools/{id}`, `POST .../tools/{id}/graduate`, `.../retire`; a
`server-toolsmith` doctor check. Gated by `PROTAGINE_TOOLSMITH` (off | shadow
| live, default off) and requires the live Docker sandbox to verify and run
tools. Related env: `PROTAGINE_TOOLSMITH_MIN_OCCURRENCES`,
`PROTAGINE_TOOLSMITH_SHADOW_MIN`, `PROTAGINE_TOOLSMITH_EXCLUDE_DOMAINS`.

Also fixed: `complete_job` now records a real duration (from claim time
when the caller omits started_at), so job-latency metrics measure actual
work; and the tool executor gained the dynamic-provider hook dynamic tools
need.


## v0.24.0 — selfhood benchmark + experiment framework (Mind M0)

Measurement before mechanism: the first phase of the cognition program.

**Selfhood benchmark.** A weekly scorecard derived entirely from existing
journals and stores; nothing is self-reported, and a metric whose source is
unavailable is skipped rather than zero-filled. Metrics: commitment
fulfillment, initiative acceptance (owner responded within 24h of a
delivery), delivery and per-domain action success, journal decision mix,
recall fact coverage (probe: high-confidence shared facts re-queried
against graph recall), queue job latency percentiles, and generic rollups
of host-submitted samples (`POST /v1/host/self/benchmark/samples`, e.g. a
voice gateway reporting TTFB). Surfaces: `GET /v1/host/self/benchmark`
(ISO-week rollups + week-over-week trends), `POST .../compute`,
`protagine benchmark` CLI, a weekly autonomy phase that computes the previous
week and delivers the scorecard to the owner, and a `server-benchmark`
doctor check that WARNs on regressing non-latency trends.

**Experiment framework.** Self-modification as controlled experiments: one
bounded change per experiment (an adaptive-parameter variant), a captured
metric baseline, a decision window, and adopt-within-guard or auto-revert
on regression. Honest rules: no metric baseline, no experiment; a decision
requires a new completed rollup week; a knob moved outside the experiment
aborts as superseded and is never re-applied; one open experiment per
parameter plus a global running cap. `GET/POST /v1/host/self/experiments`,
`POST .../{id}/abort`, a daily decision phase with owner notices, every
transition journaled.

New env (all default-on, see .env.example): `PROTAGINE_BENCHMARK_ENABLED`,
`PROTAGINE_BENCHMARK_REPORT`, `PROTAGINE_BENCHMARK_PROBES`,
`PROTAGINE_EXPERIMENTS_ENABLED`, `PROTAGINE_EXPERIMENTS_MAX_RUNNING`.


## v0.23.2 — owner contact curation (link / merge / review proposals)

Completes the relationship program's promised curation surface (it was
designed in docs/RELATIONSHIPS.md but never shipped): when the resolver
files a handle proposal, or you want to say "that WhatsApp is Sam's", or a
shadow contact turns out to be someone you know, there is now a way to act.

- `link_contact(who, gateway, address)` tool + `POST /contacts/{id}/handles`:
  attach a channel handle to a person, unifying their identity across
  channels.
- `merge_contacts(keep, merge)` tool + `POST /contacts/merge` + store method:
  fold one contact into another — reassigns handles, sums interaction
  history, soft-deletes the loser; audited on both records and reversible.
- `pending_contact_proposals` tool + `GET /contacts/proposals` + store
  `list_handle_proposals`: review the auto-proposed handle links from
  scoped-name attribution (rung 4 never links silently).

Tests: link/merge/proposal round-trips in test_relationships (incl. the
resolver filing a proposal that the store surfaces). Full suite 1707 passed.


## v0.23.1 — gap-sweep fixes: safety on the send path, whole comms ledger, honest surfaces

Fixes from a full-codebase gap audit. No new subsystems; closing holes.

- **Safety gate reaches the send path.** ResponseGuard now runs on the
  proactive delivery path (secret-leak / disclosure-tier / injection /
  provenance) before a message leaves — shadow logs, enforce
  (PROTAGINE_GUARD_MODE=enforce) blocks. The request-path gate's L7 send-delay
  is env-tunable (PROTAGINE_GATE_SEND_DELAY_SECS; 0 = pass-through, now
  explicit) and boot logs the secondary-review posture; L6 fail-open is
  documented at the config.
- **The comms ledger sees the whole conversation.** turns/sync now records
  the assistant's reply as an outbound exchange on the resolved contact +
  conversation channel (it logged inbound only), so reciprocity and
  last-contact-each-way — which the reachout recommendation depends on — are
  no longer starved. record-outreach skips placeholder contacts.
- **Research review gate actually runs.** The stage constructed ResponseGate
  with the wrong signature, threw every call, and silently degraded to a
  4-string scan; it now runs the real PII + injection layers on the
  artifact.
- **/jobs/completed and /jobs/blocked honor task_type** (the param was
  declared on completed but silently dropped; the query now filters and
  returns job_type).
- **Attribution hardening.** /tom/extract validates the contact like the
  affect/facts POSTs; the ParticipantResolver recognizes a canonical
  contact id passed as user_id (the voice channel) instead of minting a
  duplicate shadow; the wizard surfaces PROTAGINE_IDENTITY_SHADOW_CONTACTS.
- **Chain/remote-agent surface flagged experimental.** No consensus loop
  runs and the remote handshake is not verified end to end; the connect
  cert is now really signed when a key manager is present (the
  sig-<uuid> placeholder is gone) and uses the field the verifier reads.
  docs/MULTI_AGENT.md and the boot log say what is supported vs
  experimental.


## v0.23.0 — relationship intelligence: one person, every channel

The relationship stack existed but attribution failed it: a live audit found
62% of communications filed under a `default` pseudo-contact, third parties
with zero history despite active group participation, and a psyche profile
faithfully built for a non-person. This release makes attribution the
foundation and turns the accumulated signals into actionable briefs. Full
spec: `docs/RELATIONSHIPS.md`.

- **Per-message sender attribution.** `turns/sync` accepts a `sender`
  (platform, user_id, display_name, group_id); the new ParticipantResolver
  resolves it server-side: exact/cross-gateway handle match (phones unify
  across sms/rcs/whatsapp/signal/imessage), scoped display-name (files a
  merge proposal, never links silently), or a shadow contact so strangers
  accrue history from first contact. The resolved contact drives everything
  downstream: interactions, comms ledger, affect, facts, engagement,
  introspection.
- **Machines are not people.** Senderless turns on machine channels
  (`PROTAGINE_IDENTITY_MACHINE_CHANNELS`) or with system-origin text attribute
  to the reserved `system` sentinel: no interactions, no ToM writes, no
  psyche. The ToM APIs now validate contact ids against the store, so test
  strings can never mint affect/fact state again.
- **Comms provenance.** The ledger records the conversation's real
  channel_id (group vs DM vs voice) instead of the contact's primary-handle
  gateway.
- **RelationshipProfiler.** Composes standing (interactions, channels,
  cadence), psyche (the engagement extractor's OCEAN/style profile),
  affect trend, rapport topics, and derived approach guidance (preferred
  channel, best local hours, style notes, cautions) into a cached
  per-person brief: injected into context assembly for non-owner
  conversations, exposed as the `relationship_brief` tool and
  `GET /v1/host/relationships[/{id}]`, refreshed by a new autonomy phase.
- **Hermes provider** passes the per-turn sender through, making the
  sidecar authoritative even when client-side resolution misses.
- **Doctor**: new `relationship-attribution` check warns when recent
  communications pile onto placeholder contacts.


## v0.22.1 — directive capture safety, contacts path anchor, worker observability

Hardening release from live testing of v0.22.0 on the reference deployment.

- **Directive self-poisoning fixed.** Live testing surfaced a directive store
  where ~70 of 90 "boundaries" were machine-origin garbage (test canaries,
  the executor's own refusal text, anaphora fragments), and the worker
  governor enforced them against every routine job. Capture now rejects
  machine-origin text and degenerate fragment subjects, and never piles up
  duplicates (both the deterministic and LLM-assisted paths); the keyword
  matcher raises the single-term "distinctive" bar and never treats the
  product/agent name (or `PROTAGINE_MATCH_COMMON_TERMS`) as a lone match signal.
  New `server-directives` doctor check flags fragments and duplicate piles.
- **Contacts DB default anchored to the state dir** (was CWD-relative, the
  same failure class as the world-model store incident); doctor detects the
  empty-stub-next-to-real-store env-mismatch signature.
- **protagine-worker daemon output is line-buffered** (under launchd/systemd the
  log file stayed empty for hours); the launchd deploy template ships
  StandardOutPath/StandardErrorPath.
- README architecture flowchart rewritten in GitHub-compatible mermaid.


## v0.22.0 — the cognition program: earned autonomy, one-knob posture, closed learning loops

The seven-capability cognition program lands in full, alongside the autonomy preset, the
plugin consolidation, and a public-docs refresh. Protagine is now a sidecar that can *earn*
agency rather than be granted it.

- **Self-model / trust engine:** graduated, earned autonomy per action class —
  `shadow → ask_first → act_first` — with confidence computed from real journaled outcomes,
  auto-graduation with owner notification, circuit breakers that demote on failures or any
  audit violation, and an immutable floor (money movement, irreversible deletion, credential
  changes, bulk third-party messaging) that is never self-decidable. Every gate decision is
  journaled in the unified ActionJournal (`protagine-action-journal.db`), and the daily
  proactive-delivery cap adapts to the delivery domain's track record.
- **`PROTAGINE_AUTONOMY_PRESET=passive|calibration|autonomous`:** one knob supplying defaults
  for all fourteen autonomy flags at once. Explicit env always wins; `calibration` (the
  wizard default) shadows everything and lets the trust engine graduate capability classes
  from their real track record; the sandbox never goes live from a preset.
  `GET /v1/host/autonomy/posture` returns the resolved posture of the running process.
- **Projects (`projects/`):** goal persistence and sustained multi-tick pursuit — a planner
  decomposes goals and the ProjectEngine advances them across autonomy cycles
  (`PROTAGINE_PROJECTS_MODE`).
- **Skills memory (`skills_memory/`):** compounding procedure learning — distill on
  retry-success and novel diagnosis, retrieve into future prompts (`PROTAGINE_SKILLS_DISTILL`).
- **Beliefs (`beliefs/`):** contradiction detection, resolution, and stale-belief decay
  (`PROTAGINE_BELIEFS_MODE`).
- **Workers:** `task_queue/governor.py` WorkerGovernor enforces server-side — capability
  coverage, boundary compliance, and report audits are re-decided by the sidecar, never
  taken on the worker's word — and `workers/protagine_worker.py` ships as the installable
  `protagine-worker` daemon with systemd/launchd templates under `workers/deploy/`
  (`PROTAGINE_WORKERS_MODE`).
- **Sandbox (`sandbox/`):** gated Docker exploration sandbox — no network, no credentials,
  capped resources, read-only rootfs (`PROTAGINE_SANDBOX_MODE=off|dry_run|live`; live is
  explicit-only).
- **Connectors (`connectors/`):** read-only senses framework — `imap_email`,
  `caldav_calendar`, `fs_documents`, `webhook_pull` — feeding observations into the same
  cognition path (`PROTAGINE_CONNECTORS_MODE` + per-connector `PROTAGINE_CONNECTOR_<NAME>_*`).
- **Charter prompt architecture:** every LLM role shares one versioned agency doctrine
  (`cognition/charter.py`); the `PROMPT_VERSION` is journaled with every action.
- **Closed learning loops:** the new AdaptiveParamStore (`self_model/params.py`) holds
  bounded, journaled runtime knobs that consumers actually read back
  (`consolidation.similarity_threshold` [0.85–0.98], `recall.min_relevance` [0–0.5];
  `GET /v1/host/self/params`). The meta-learning StrategyAdjuster now writes THROUGH this
  store — previously it wrote a graph Config node nothing read, so the flagship
  self-improvement adjustment had zero effect. The initiative executor records wins/losses
  on the skills that informed each run and retrieval ranks by that record; trust confidence
  now consumes stated-vs-realized calibration, so overconfident domains earn less trust.
- **Mining (`mining/`):** escalation miner spots correction/consultation turns
  (`PROTAGINE_ESCALATION_MINING`) and the training-corpus exporter
  (`POST /v1/host/mining/corpus/export`) writes fine-tune JSONL under the state dir only.
- **Feeds (`feeds/`):** spec-driven intelligence feed framework with the `protagine feeds`
  CLI, `docs/FEEDS.md`, and the `plugins/feeds-manage` Hermes plugin.
- **Channels, persona, backup:** generic channel registration with auto-derived channel ids
  (`channels/`, `docs/CHANNEL_FRAMEWORK.md`), the persona deployment layer (`persona/`,
  `protagine persona` CLI), and full-state backup/restore (`protagine backup` / `protagine restore`).
- **Doctor:** ten new cognition checks that read the RUNNING server — autonomy posture,
  self-model + breakers, adaptive params, executor, projects, beliefs, worker governor,
  sandbox, connectors, mining — plus env-mismatch detection on contacts-db; 30 check
  results in a full run.
- **Wizard:** preset-driven autonomy step; OpenClaw fully removed (menu, flags, validate
  path — support was removed in v0.21.14 but the wizard still offered it and silently
  no-opped); `protagine validate` now live-fires the sidecar's own LLM router.
- **Plugin consolidation:** `plugins/protagine-memory/` is the SINGLE canonical Hermes memory
  provider (merged: reply thread-window, `protagine_resolve_commitment`, the battle-tested
  sync architecture, and the `pre_llm_call` contact/time hook). `plugins/hermes-memory/`
  and `plugins/hermes-plugin/memory_provider/` are deleted; `install.sh` and the wizard
  both deploy from it to `~/.hermes/plugins/protagine-memory/`.
- **Docs refresh + genericity:** README, CONTRIBUTING, harness guide, and `.env.example`
  rewritten to match the codebase; the Hermes install URL corrected everywhere to
  `https://github.com/NousResearch/hermes-agent`; remaining deployment-identity strings
  scrubbed from fixtures and docs.

## v0.21.33 — InitiativeExecutorService: autonomous initiative processing

A generic initiative executor closes the autonomy circuit without an external agent: it
uses Protagine's own ReasoningLoop (LLM + tools) to claim pending initiatives, reason about
them, execute actions, and report results back to the store.

- **Opt-in** via `PROTAGINE_EXECUTOR_ENABLED=true`, with configurable LLM tier, initiative
  types, cycle interval, and concurrency. Stats via `/v1/host/health` notes and
  `/v1/host/executor/status`.
- **CI:** auth-coverage test walks the nested route tree and is compatible with
  FastAPI 0.138+.

## v0.21.32 — the autonomy circuit runs itself (agent bridge)

Two steps toward zero-setup autonomy delivery (the first shipped untagged as v0.21.31):

- **`protagine-agent-bridge` console script:** one long-running daemon replaces the three
  separate cron scripts (initiative-poller, queue-worker, skills-sync) and adds circuit
  health monitoring that catches silent failures — sidecar unreachable, autonomy loop
  stuck, initiatives never executed, jobs stuck in queue. `--once` for cron, `--dry-run`
  for validation; stdlib-only.
- **Built-in AgentBridgeService:** an internal async service that closes the same circuit
  with no platform-specific setup at all (no cron, no LaunchAgent, no systemd). Auto-starts
  at sidecar boot when `PROTAGINE_BRIDGE_WEBHOOK_URL` is set, accessing the initiative store
  and task queue directly. Stats via `/v1/host/health` notes and `/v1/host/bridge/status`.

## v0.21.30 — official Hermes plugin moves into the repo (generic adapter)

The Hermes "protagine" plugin now lives in this repo at `plugins/hermes-plugin/` [corrected:
this entry originally said `integrations/hermes/protagine/`, which never existed] as the
official, deployment-agnostic adapter, co-located with the API it calls so their contract
can't silently drift.

- **Generic plugin** (tools, ProtagineClient, event subscriber, slash commands, hooks, autonomy
  bridge). All owner/persona specifics are removed: the autonomy bridge ships a generic
  default prompt and reads `plugins.protagine.autonomy_prompt` (inline or file path) +
  `plugins.protagine.autonomy_deliver` from Hermes config, so a deployment injects its persona
  and channel without touching the core.
- **Contract test** (`tests/test_hermes_plugin_contract.py`): auto-discovers every
  `/v1/host/...` path the plugin references and asserts each is a registered route across the
  host/queue/observations routers. This is the guard that would have caught the
  `/tasks` → `/initiatives` drift that failed silently in production.


## v0.21.29 — introduction proposals clear the confidence gate

Final fix for Slice 2, found in live verification: an introduction was generated but never
surfaced because `generate()` applies the `min_priority` confidence filter (autonomy default
**0.7**) BEFORE the cap, and the intro's flat priority 0.5 was filtered out first — so the v0.21.28
cap headroom never got a chance to run.

- **Introduction priority 0.5 → 0.7** so it clears the default confidence gate. An intro is
  high-CONFIDENCE (a real shared-work signal plus both parties above the trust floor), just
  low-urgency; the cap's social headroom (v0.21.28) handles volume once it is past the filter.

Verified end-to-end on a live deployment: two same-org contacts above the trust floor produced
an owner-facing `INTRODUCTION` proposal ("Introduce A and B (both at <org>)").

## v0.21.28 — introduction proposals survive a saturated cap

Completes Slice 2: introduction proposals are low-priority by design, so on a busy loop that
saturates the per-cycle initiative cap they were generated but always cut. Verified live — the
loop reported "20 new proposals" every cycle and the intro never surfaced.

- **Bounded social headroom:** the initiative cap now tops up to `_INTRO_HEADROOM`
  (`PROTAGINE_INTRO_HEADROOM`, default 2) introduction proposals from the cut tail, so an
  operational backlog can't permanently starve them. Owed deliverables remain unbounded.
- **Refactor:** the cap + starvation guards moved into a testable `_apply_cap()` helper
  (behaviour unchanged for the deliverable exemption).

## v0.21.27 — organic introduction proposals (social-graph autonomy, Slice 2)

The agent can now proactively notice that two people it knows should probably meet, and propose
the introduction for the owner to approve. PROPOSE-ONLY and owner-gated by construction — an
INTRODUCTION is its own initiative type, not an `agent_action`, so the loop surfaces it and never
auto-executes it.

- **`INTRODUCTION` initiative type** + `_generate_introduction_initiatives()`: consumes
  introduction candidates and proposes "introduce A and B" with `action_hint=propose_introduction`.
  Owner exclusion fails closed (never introduce the owner). One-shot `dedup_key` (`intro:<lo>:<hi>`,
  ids sorted) so a pair is proposed once; the store's terminal-dedup stops re-proposing after the
  owner acts. Marked DURABLE in context-freshness.
- **`SQLiteContactStore.introduction_candidates()`**: finds pairs sharing an organization (a
  "related work" signal) where BOTH sit at/above a trust floor; excludes the owner and soft-deleted
  contacts. Deployment-agnostic (pure contacts SQL, no graph dependency).
- **Autonomy feed:** `_feed_introduction_candidates()` runs in the initiative phase, gated by
  `PROTAGINE_INTROS_ENABLED` (default true) with `PROTAGINE_INTRO_TRUST_FLOOR` (default `regular`). Owner
  exclusion fails closed.

## v0.21.26 — introduction capture (social-graph autonomy, Slice 1)

The generic, deployment-agnostic foundation for organic relationship-building: the agent can
record a person it just met or learned of as a durable contact WITH provenance, but WITHOUT any
interaction standing. Slice 1 of the social-graph-autonomy arc (Feature B); the world-model edge
+ introduction initiative come in Slice 2.

- **Introduction provenance on contacts:** new `introduced_by` (contact_id) + `met_via` (`{channel,
  scope_id, ...}`) fields, auto-migrated onto the contacts table. First-class on the contact so they
  are always available even before a world-model Person node exists.
- **`POST /v1/host/contacts/intro`:** capture an introduction. Creates a PROVISIONAL contact
  (`import_source=agent_intro`, `interaction_allowed` forced false — an intro never grants outreach
  standing) or, when the handle already resolves to a known person, records the provenance on that
  contact instead of duplicating it (first introduction wins; standing untouched). RCS handles are
  stored under the canonical phone gateway.
- **`WM_INTRODUCED_BY` relationship type** added to the world model (consumed in Slice 2).
- **Store:** `create()` accepts `introduced_by`/`met_via`; new `record_introduction()` annotates an
  existing contact. Audited.

## v0.21.25 — health: stop reading conversational idle as "degraded"

The `/health` endpoint flagged `prefetch` stale at 2h and forced the whole system to
`degraded`. But `last_prefetch_at` is touched only by `/context/assemble`, which is driven
by inbound conversation turns — so any normal quiet period (overnight, focus time) tripped it.
That both cried wolf and masked real degradation.

- **Prefetch staleness threshold 2h → 24h** (`PROTAGINE_STALE_PREFETCH_HOURS` default), matching
  the agent-snapshot views. 24h means "the host has not requested context in a full day" — the
  point where idle becomes a genuine integration-down signal. Internal-loop metrics (sync 2h,
  tick 24h, initiative 48h) are unchanged, so a stuck loop is still caught.
- **test:** `test_telemetry_staleness.py` pins the profile — a multi-hour conversational idle
  stays healthy, a >24h prefetch gap flags, and a stuck `sync` is still flagged.

## v0.21.24 — writeback idempotency guardrails

Hardening so the v0.21.23 writeback-idempotency bug class cannot silently recur or be
misdiagnosed again. No behavior change to the happy path; all additions are backward
compatible.

- **Idempotency regression test:** `test_writeback_phase_is_idempotent_across_runs` drives a
  job to COMPLETED through a real queue, runs `_phase_job_writeback()` twice, and asserts the
  job is processed exactly once (memory written once, initiative closed once, `memory_synced`
  persisted). A future regression in tag persistence now fails in CI instead of silently
  re-writing memory and re-fulfilling commitments every cycle in production.
- **Loud failure on a stuck marker:** the writeback now logs a warning if `merge_job_tags`
  cannot persist `memory_synced` (the original bug was invisible because the failed tag write
  was a silent no-op). An infinite re-process loop now announces itself.
- **Honest `/autonomy/cycle`:** the response now includes `mode`, `ran_synchronously`, and a
  `note`. In proactive mode it returns `ran_synchronously: false` (the endpoint only wakes the
  loop; the tick — including job-writeback — runs asynchronously, so the DB is not yet
  updated). Callers and e2e tests must poll. `completed` and `result` are unchanged.

## v0.21.23 — autonomy dispatch + writeback durability

Three fixes that make the autonomy initiative loop actually deliver and converge. The
deliverable backstop (v0.21.21) was generating commitments but the loop silently dropped
every fresh initiative before it could be dispatched, recurring initiatives never re-armed,
and finished jobs were re-processed forever.

- **Dispatch guard fix (Fix 1):** the store assigns its own UUID primary key, so the engine's
  logical id (e.g. `deliver-{cid}`) never equaled the stored id — the old `_phase_execute`
  guard `stored.id != original_id` therefore skipped *every* freshly created initiative,
  blocking all initiative dispatch (observation syncs used a separate path and masked it).
  `store.create_with_outcome` now returns an explicit outcome
  (`created` / `reactivated` / `deduped_active` / `deduped_terminal`); the loop dispatches
  only on `created`/`reactivated`. `store.create` is kept as a back-compat shim.
- **Recurring re-arm + cross-period guard (Fix 2):** recurring initiative types
  (relationship, health, followup, ...) get a time-bucketed `dedup_key`
  (`{base}:{bucket}`) so they re-arm each period, while the new `dedup_base` column holds the
  un-bucketed logical key. The store suppresses a new instance whenever *any* active one
  shares the base, so a still-pending instance is never duplicated when the period rolls over.
  One-shot types (commitment, agent_action, gaps) keep their stable key and stay one-shot.
  Adds the `dedup_base` column (auto-migrated) + index and `get_active_by_dedup_base`.
- **Writeback idempotency fix:** `update_job_status` refuses terminal jobs (to block illegal
  status revivals), but the autonomy writeback tags *completed/failed* jobs with its
  `memory_synced` marker — so the tag never persisted and every finished `agent_action` was
  re-written to memory (and re-fulfilled) on every cycle. New `merge_job_tags` persists
  tag-only updates on terminal jobs without touching status; the writeback uses it.
- **Deliverable cap-exemption:** an owed deliverable (`agent_deliver_message`) is never
  dropped by the per-cycle initiative cap — someone is actively waiting on it.

## v0.21.22 — inline turn introspection

A sidecar-side per-turn introspection that runs the owed-follow-up judgment in-process
against a configured local LLM and records commitments directly — the path that works on
deployments where no host plugin consumes the `cognition.requested` event.

- **Inline introspection** (`cognition/introspection.py`): on `/turns/sync`, when
  `PROTAGINE_INTROSPECT_ENABLED=true`, the sidecar judges the turn with an OpenAI-compatible
  endpoint (`PROTAGINE_INTROSPECT_BASE_URL` / `_MODEL` / `_API_KEY`) and records any durable
  commitment or immediate owed deliverable directly via the commitment store. A focused,
  JSON-only few-shot prompt (so small/no-think local models comply). Disabled by default;
  deployment-agnostic.
- **test:** pin `agent_deliver_message` in the outbound registry-tier audit.

## v0.21.21 — introspection follow-through + agent_action execution

Per-turn cognition can now record an *owed deliverable* — something the person
asked to be sent in this exchange that wasn't done — as a first-class commitment,
and the autonomy loop actually delivers it. Plus two worker bugs that were
silently failing every queued `agent_action`.

- **Deliverable commitments:** the per-turn cognition prompt now recognizes an
  immediate owed deliverable (e.g. "text me the result") alongside durable future
  commitments. It is recorded as a commitment tagged `metadata.kind="deliverable"`
  with `source_type="introspection"`; the autonomy loop turns an undelivered one
  into an `agent_deliver_message` agent_action, and job-writeback marks the
  commitment fulfilled once it is sent. Adds the `agent_deliver_message` action
  (outbound, recipient-gated by the graduated policy). `/turns/sync` now forwards
  the verbatim user+assistant turn to cognition so the deliverable and its content
  can be judged.
- **Worker job-type scoping (fix):** a `WorkerNode` built with a handler dict left
  `job_types` empty, which `WorkerCapabilities.can_accept` treats as "accept all
  types" — so an embedded worker claimed `agent_action` jobs it had no handler for
  and failed every one. It now advertises only the job types it can run.
- **create_job priority (fix):** `JobPriority(body.priority.upper())` raised on
  every call (an int-Enum constructed by name-as-value, even for the default
  "normal"). It is now a by-name lookup that also accepts the numeric value.

## v0.21.20 — remote reranker

Recall now has a working rerank stage: the sidecar can use a reranker served
off-box over an OpenAI/Jina-compatible `/v1/rerank` endpoint, the same way the
embedder is served.

- **Remote reranker:** new `openai_api` path in the reranker init. Set
  `PROTAGINE_RERANKER_PROVIDER=openai_api`, `PROTAGINE_RERANKER_BASE_URL`,
  `PROTAGINE_RERANKER_API_KEY`, and `PROTAGINE_RERANKER_MODEL`; without these env
  vars the local MLX/CUDA/CPU path is unchanged.
- **Qwen3-Reranker correctness:** `PROTAGINE_RERANKER_PROMPT_STYLE=qwen3` wraps
  rerank requests in the model's instruction template. Qwen3-Reranker scores
  from yes/no token logits that are only calibrated under that template —
  vLLM's `/v1/rerank` does not apply it server-side, and raw strings rank
  noise above relevant passages.

## v0.21.19 — release tooling

No functional or packaging changes; the sidecar is identical to v0.21.18. This
release exercises the new release pipeline end to end.

- **CI/release:** GitHub Actions bumped off the deprecated Node 20 runtime
  (`checkout` v6, `setup-python` v6, the docker actions v4/v7) ahead of GitHub's
  forced Node 24 migration.
- **Release automation:** pushing a tag now auto-creates the GitHub Release from
  the matching `CHANGELOG.md` section, after the PyPI and GHCR publishes succeed.

## v0.21.18 — repository cleanup

No functional changes; the shipped package is identical to v0.21.17. This release
refreshes the PyPI project page with the polished README.

- **Docs:** remove implemented and superseded design/spec documents (the root
  `DESIGN-self-initiatives` drafts, `specs/`, `docs/specs/`, and the
  `docs/*-spec.md` files) and the internal trackers (`TODOs.md`, `NIGHTLOG.md`,
  `docs/deferred-items.md`, `docs/open-items-plan.md`, `docs/audits/`).
- **Code comments:** drop dangling spec references — docstrings and `§`-section
  pointers to specs that were never committed — from the `chain/` module.
- **Tests:** remove an orphaned root-level test the runner never collected.
- **README:** Mermaid architecture diagram and copy polish.

## v0.21.17 — correctness + hardening pass

- **Initiatives:** normalize Neo4j datetimes to tz-aware UTC. Naive timestamps
  raised a swallowed `TypeError` that silently dropped every blocked-goal and
  pending-research initiative.
- **Relationship scoring:** the scheduled phase now recomputes the SQLite
  closeness score every consumer reads (previously only the dead Neo4j path ran),
  so a contact's score keeps decaying when no turn arrives.
- **Goal inference:** preserve the inferred deadline and provenance on proposed
  goals instead of dropping them.
- **Commitments:** persist `due_at` as canonical UTC ISO so overdue detection (a
  string comparison) is reliable; mixed naive/offset values had broken it.
- **Security:** the sidecar fails closed when bound to a non-loopback address with
  no `PROTAGINE_API_KEY` instead of serving open with only a warning; adds an auth
  test over the full route table.
- **Briefings:** reuse one shared async bridge pool instead of creating an
  executor per aggregator call.
- **Observability:** surface cognition-cycle step errors (previously discarded)
  and report the real consolidation merged-count.

## v0.21.1 — memory reliability + recall quality

Fixes that make Protagine memory production-reliable:
- **Recall quality**: embed retrieval queries with the asymmetric Qwen3 instruction
  prefix (configurable via PROTAGINE_EMBED_QUERY_INSTRUCTION). Without it, vector
  retrieval was near-random (~0.9 cosine distance on everything) and the right
  memory never surfaced; with it, the correct memory ranks first.
- **No silent memory loss**: refuse to store a memory when an embedding was
  expected but unavailable (was creating unsearchable, embeddingless nodes during
  embed outages).
- **Embed resilience**: retry the embedding endpoint with backoff (the embedder
  is often a remote/tunnelled service).
- **Dedup**: memories are deduped by content hash (a recurring prompt had been
  stored 70+ times); empty memories are dropped.


## 0.20.0 (2026-06-10)

Scheduled-worker installation: the agent-side workers become part of
the pip package, the wizard schedules them, and the doctor detects when
the queue worker is missing.

### Added
- **`protagine.workers` package** (stdlib-only): the queue-worker
  and skills-sync logic moved out of the loose
  `plugins/hermes-plugin/poller/` scripts into
  `protagine.workers.queue_worker` / `.skills_sync`, shipped as the
  `protagine-queue-worker` and `protagine-skills-sync` console scripts
  (same env vars and behavior; both support `--dry-run`). The old
  script paths remain as thin back-compat wrappers (import-and-call
  with a repo-relative `sys.path` fallback).
- **Wizard Step 10e "Scheduled agent workers"** (`protagine init`,
  idempotent on re-run): asks whether the agent lives on this machine
  and installs crontab entries on macOS/Linux — `*/5 * * * *`
  queue worker + `0 9 * * *` skills sync, sourcing the wizard's `.env`
  (`set -a`), logging to `$PROTAGINE_HOME/logs/cron-<name>.log`. Merges
  with the existing crontab (never duplicates a worker referenced in
  either console-script or `python -m` form); prints the exact lines
  for manual install when declined or crontab is unavailable.
- **`protagine doctor` `server-worker-liveness` check**: WARNs when any
  QUEUED `agent_action` job is older than 15 minutes (queue worker
  appears absent — auto-approved jobs would sit QUEUED forever), with
  the cron-install remedy. Uses the existing authed
  `/v1/host/queue/jobs/pending` surface; skips when the sidecar or
  task queue is down. Skill-staleness remedies now point at the
  `protagine-skills-sync` console command.

## 0.19.0 (2026-06-10)

Setup catches up with autonomy: the wizard configures the v0.16-v0.18
surface, and `protagine doctor` becomes a real configuration diagnostic.

### Added
- **Wizard Step 8 "Autonomy & approvals"** (`protagine init`, idempotent on
  re-run): owner contact creation written directly into the contact
  store (+ `PROTAGINE_OWNER_CONTACT_ID`), plain-language strict/graduated
  approval-policy choice, internal-thinking and skill-synthesis toggles,
  home-channel selection. The wizard now ends with a doctor pass
  (Step 12).
- **`protagine doctor` rebuilt as a config-aware check engine** (19 checks,
  `--json`, exit codes): every production footgun is detected with an
  exact remedy — LLM baseUrl missing `/v1` (the silent
  "all tiers exhausted" cognition killer), empty apiKey, `:memory:`
  contact store, unresolvable owner, invalid approval-policy values,
  corrupt standing approvals, missing home channel, pending blocked
  approvals, stale/missing agent skill index, launchd
  kickstart-vs-bootstrap stale env. `--fix` applies the safe LLM-config
  repairs; `--clean-orphans` preserved from the old doctor.
- `GET /v1/host/health/llm` — authed, live-fires one tiny SMALL-tier
  completion so router death can never hide again.

### Fixed
- Wizard LLM-config writes normalize `baseUrl` to `/v1` and never emit
  an empty `apiKey` (the source of both footguns).
- Wizard previously collected multimodal settings *after* writing
  `.env`, silently dropping them; config is now re-written after all
  steps.

## 0.18.0 (2026-06-10)

Graduated approvals (the owner only hears about destructive actions and
unauthorized outreach) + the Hermes↔Protagine skills bridge.

### Added
- **Graduated approval policy** (`PROTAGINE_APPROVAL_POLICY=graduated`;
  default remains `strict` = v0.17 behavior). Under graduated: read-only
  and reversible-mutating actions auto-execute with an audit trail
  (`auto_approved_by_policy` job tags + `action_auto_approved` events);
  a new DESTRUCTIVE risk tier (merges, deletes, restarts, deploys)
  always requires owner approval; OUTBOUND is reserved for actions that
  reach a *person* and auto-passes only when the recipient resolves to a
  contact with `interaction_allowed` — unknown targets fail closed.
- **Standing approvals** — approve a blocked job with `{"always": true}`
  and that action class is pre-authorized from then on (per-action-class
  approval records, persisted at `$PROTAGINE_STATE_DIR/standing_approvals.json`;
  `GET /v1/host/queue/approvals/standing`, `DELETE .../{action_name}`).
- **Hermes skill export** — approving a captured procedural skill also
  renders a Hermes-format `SKILL.md` into `PROTAGINE_HERMES_SKILLS_DIR`
  (default `~/.hermes/skills/protagine`), with provenance frontmatter and a
  no-overwrite guard for non-Protagine files. Gate:
  `PROTAGINE_EMIT_HERMES_SKILLS` (default off).
- **Agent skill-index sync** — the OpenClaw plugin scans the Hermes
  skills directory (startup + daily) and reports it into the new
  push-only `skills` observation domain; the self-directed thinking
  situation report now includes the agent's actual capabilities.
- Action specs gained `target_param` (recipient extraction for outbound
  authorization checks).

### Fixed
- Reclassified `coding_comment_on_pr` and `system_send_alert` from
  OUTBOUND to MUTATING — platform writes, not person outreach.
- Test-only Node 18 polyfill for `toSorted`/`toReversed` (10 vitest
  suites were failing at baseline on Node <20).

## 0.17.0 (2026-06-10)

The autonomous engine: Protagine now thinks, acts behind an enforceable
approval gate, and learns from what its agent does.

### Added
- **Self-directed thinking (Phase 5b)** — on a slow cadence
  (`PROTAGINE_THINKING_INTERVAL_SECS`, default 1h; gated by
  `PROTAGINE_ENABLE_INTERNAL_THINKING`, default off) the autonomy loop hands
  the LLM a situation report (goals, pending work, commitments, current
  initiatives) and lets it propose novel initiatives. Proposals are
  priority-capped (0.85), deduped across cycles, and can never carry an
  `action_hint` — thought-up work always lands as review/decide
  initiatives, so mutating/outbound ideas still cross the action
  registry and owner approval before any agent touches them.
- **Server-side approval gate** — gated agent actions are now *created*
  in BLOCKED state (no submit-then-transition race); claims can never
  hand out blocked jobs; `GET /v1/host/queue/jobs/blocked`,
  `POST /v1/host/queue/jobs/{id}/approve`, `POST .../reject` (409 on
  non-blocked); initiative responses sync to the linked job; stale
  approvals auto-fail after `PROTAGINE_APPROVAL_TIMEOUT_HOURS` (72).
- **Job-completion memory writeback (Phase 6c)** — completed/failed
  agent jobs become episodic memories (`source_uri protagine://jobs/{id}`),
  advance their goals (`goals.on_job_completed` existed since v0.13 but
  was never called), close their linked initiatives, and broadcast
  events. Idempotent via the `memory_synced` job tag.
- **Skill capture** (`PROTAGINE_ENABLE_SKILL_SYNTHESIS`, default off) —
  successful novel agent work flows through the existing-but-dormant
  learning pipeline (novelty gate → pattern extraction → DRAFT skill
  package). Captured skills are DRAFT with deny-by-default capabilities
  and surface a review initiative; nothing synthesized executes without
  owner approval.
- `POST /v1/host/contacts` (+ handles) so deployments can bootstrap the
  owner contact the IdentityResolver requires.

### Fixed
- **Contact store now persists** — the server built it with the default
  `sqlite_path=":memory:"`, wiping all contacts (including the owner)
  on every restart. Now resolves `PROTAGINE_CONTACTS_DB` or
  `$PROTAGINE_STATE_DIR/protagine-contacts.db`.
- **contact_handles gateway constraint** — whatsapp/discord/slack were
  deliverable channels but unstorable handles.
- Capability-gap / knowledge-acquisition / behavioral-correction
  generators hardened against the real graph schema (schema-adaptive
  queries, env thresholds, defensive failure paths).
- CI/Release workflows on node 22 (engines requires >=22.16); npm
  publish non-blocking until the @aevonix npm org exists.
- 35 of 36 dependabot alerts resolved (npm audit + openclaw bump; the
  remaining moderate hono pin sits inside openclaw's published
  npm-shrinkwrap and is unfixable downstream).

## 0.16.0 (2026-06-10)

Initiative pipeline fixes + autonomous work engine foundations.

### Fixed
- **Remote embedding via `PROTAGINE_EMBED_PROVIDER=openai_api` actually works** — the text-only startup path never called `provider.configure()`, leaving base_url/api_key empty; and the request payload always sent `dimensions`, which vllm rejects (HTTP 400) for non-matryoshka models such as Qwen3-Embedding-8B. Running in production against a remote CUDA endpoint since 2026-06-10 (parity cosine >0.9998 vs the native-MLX path).
- **Auth middleware accepts `X-API-Key`** — the initiative poller and the new queue worker authenticate with `X-API-Key` (and advertise that header to agents in job payloads), but the middleware only honored `Authorization: Bearer`, so on keyed deployments every poller/worker call returned 401. Either header is now accepted with the same constant-time comparison.
- **Initiative API: title is the action, not the reason** — serializer used `rationale` ("No contact for 14 days") as the title instead of `description` ("Check in with Jordan Example"). Rationale now travels inside the context dict.
- **Initiative API: `entity_id` exposed** — the subject of an initiative (person, PR, commitment) is now returned; `target_agent_id` is populated from assignment instead of hardcoded `null`. Subject and executor stay distinct fields.
- **Initiative API: context no longer empty** — the autonomy loop's per-initiative context snapshot is persisted to a new `context` column (idempotent migration; pre-migration rows return `{}`) and returned over the REST API, stamped with `context_captured_at`.
- **Owner self-initiative filter** — replaced the broken `PROTAGINE_HOST_CONTACT_ID`/`"owner"`-default equality check with an `IdentityResolver` backed by the contact store (CID ↔ Neo4j Person UUID ↔ display name ↔ handles). `PROTAGINE_OWNER_CONTACT_ID` is canonical (legacy var still honored with a deprecation warning). Missing/unresolvable owner now fails **closed** (CRITICAL log, relationship generation disabled) instead of open. Filter is scoped to relationship generators only — the owner remains a valid subject for commitment/calendar/agent-action work.
- **`InitiativeResponse.status`** accepted `"in_progress"` but not `"assigned"`, breaking serialization of assigned initiatives.
- **`POST /initiatives`** dropped the request `context`, lacked `entity_id`, and stored the 0–100 priority unscaled (clamping everything to 100).

### Added
- **IdentityResolver** (`identity/resolver.py`) — `resolve(any_id) → set[str]` across all identifier formats; ambiguous display names resolve to nothing rather than merging people.
- **Action registry** (`initiatives/action_registry.py`) — `action_hint` is a named, allow-listed capability with risk tiers (`read_only` auto-executes; `mutating`/`outbound` block on human-owner approval). Unregistered hints are never queued. Covers task/coding/project/system/calendar/commitment/research/agent actions.
- **Context durability & freshness** (`initiatives/context_freshness.py`) — every initiative type declares durable vs volatile context with per-type freshness TTLs; `context_durability` returned by the API.
- **Per-entity context refresh** — `POST /v1/host/initiatives/{id}/context/refresh` routes to `engine.rebuild_context()` (relationship and commitment rebuilders shipped; volatile types without a rebuilder return 501 instead of stale data).
- **New initiative types** — `COMMITMENT`, `CALENDAR`, `RESEARCH`, `TASK`, `PROJECT`, `SYSTEM` join the existing 12.
- **COMMITMENT generator** — commitments surface as first-class durable initiatives (`dedup_key=commitment:{id}`, overdue escalation) instead of anonymous scheduling opportunities.
- **Agent-as-sensor loop** — Protagine never calls external APIs; the agent observes through its own Hermes connections and reports back:
  - Observation store (`observations/`) + ingestion API (`POST/GET /v1/host/observations`) across six domains (coding, task, calendar, research, project, system)
  - Autonomy loop posts read-only `agent_sync_<domain>` jobs to the task queue when a domain's observations go stale (`PROTAGINE_SYNC_DOMAINS` scopes it)
  - Six observation-backed generators: failing-CI/review-requested PRs, stale tasks, events starting within 24h, unchecked research, milestones due with open work, unhealthy services
  - Volatile auto-close: `POST /initiatives/{id}/context/refresh` cancels initiatives whose condition has cleared (CI green, service recovered) with `stale_reason="condition_cleared"`
  - Hermes plugin: `protagine-queue-worker.py` claims agent jobs and hands them to the agent via the new `protagine-jobs` webhook route with curl-able lifecycle URLs
- **Agent-brain framing sweep** — `notify_user` defaults replaced with `review_and_decide` (regression-tested); relationship hint changed to `evaluate_relationship`; webhook prompts rewritten around five agent decision verbs (execute/snooze/dismiss/communicate/request-approval).

## 0.15.1 (2026-05-23)

Live Neo4j integration validation — 2 critical fixes found against real data.

### Fixed
- **`duration.between(...).days` → `duration.inDays(...).days`** — Neo4j normalizes durations into months + days. `duration.between(...).days` returns the *component* days (e.g. 29 for a 60-day span), not total days. This caused:
  - `archive_memories()` to never archive memories older than ~1 month
  - `decay_memories()` to under-decay memories older than ~1 month
- **Null-property safety in `_update_effective_confidence_batch()`** — `row.get("base_confidence", 1.0)` returns `None` when the key exists but the property is `null` (pre-v0.15.0 legacy memories). Switched to `or` fallback to prevent `TypeError` on the first decay pass against any graph with old data.

## 0.15.0 (2026-05-23)

Memory governance & epistemic hygiene — source anchoring, confidence computation, decay, pruning, reconciliation, and archival.

### Added
- **Epistemic state machine** — 8 states (`inferred`, `observed`, `corroborated`, `verified`, `stale`, `superseded`, `deprecated`, `archived`) with `transition_epistemic_state()` API
- **Source anchoring** — every memory records `source_type`, `source_uri`, `source_version`, `content_hash`
  - `MemorySourceType` enum: `conversation`, `file`, `tool_output`, `user_assertion`, `inference`
  - `SOURCE_RELIABILITY` weighting: user_assertion (1.0) > file (0.9) > tool_output (0.85) > conversation (0.7) > inference (0.5)
  - `MAX_IMPORTANCE` clamping per source type to prevent over-inflation
  - `protected` flag auto-set for `user_assertion` memories (immune to pruning)
- **Effective confidence computation** — multi-signal formula:
  - Base: `base_confidence × source_reliability`
  - Corroboration boost / contradiction penalty
  - Recall reinforcement (diminishing returns, cap 1.3×)
  - Recency discount (~10%/year exponential decay)
  - Verification boost (1.2× if verified within 7 days)
  - State floors/penalties: `verified` ≥ 0.9, `stale`/≠superseded × 0.3, `deprecated` × 0.1
- **Ebbinghaus decay** — `decay_memories()` applies forgetting curve:
  - Formula: `strength = importance × e^(-λ × days) × (1 + recalls × 0.2)`
  - Identity memories never decay; procedural at half rate; protected skipped
- **Pruning** — `prune_weak_memories()` deletes memories with `strength < 0.05`
  - Skips protected, `corroborated`, `verified`, and fully terminal states
- **File reconciliation** — `FileReconciler` validates file-sourced memories against ground truth:
  - Detects deleted files → marks `STALE`
  - Hash-match unchanged files → marks `VERIFIED`
  - Hash-mismatch → creates superseded version with preserved entities
  - `dry_run` mode for safe preview
- **Archival** — `archive_memories()` moves terminal-state memories to `:ArchivedMemory` after 30 days:
  - Copies `MENTIONS`, `ABOUT`, `SUPERSEDES`, `DERIVED_FROM` relationships
  - Removes from LanceDB vector store
- **Batch confidence refresh** — `_update_effective_confidence_batch()` recalculates all memories in 1000-item batches
- **19 unit tests** — `tests/test_memory_governance.py` covering confidence signals, write governance, epistemic states, source anchoring, reconciler structure

### Fixed (5-pass audit)
- `record_turn()` `NameError`: undefined `meta` → `metadata`; now passes `contact_id` as `person_id`
- `store_memory()` missing Cypher params: `source_type`, `source_uri`, `source_version`, `content_hash`
- `compute_effective_confidence()` Neo4j `DateTime` compatibility via `.to_native()`
- `decay_memories()` null `accessed_at` safety with `coalesce(m.accessed_at, m.created_at, datetime())`
- `prune_weak_memories()` / `verify_memory()` docstring accuracy
- `FileAnchor` lazy creation during `store_memory()` for file sources
- Reconciler query efficiency via `FileAnchor` index
- `source_type` case normalization preventing `MAX_IMPORTANCE` lookup misses

## 0.14.1 (2026-05-23)

Audit-driven hardening — graph schema, silent-failure logging, stale-comment cleanup.

### Fixed
- **Graph schema migrations now run on startup** — `run_migrations()` applied after `ProtagineGraph` init so constraints/indexes exist before any queries execute
- **Timezone crash in goal completion** — `goals/store.py` `_parse_dt()` normalizes all datetimes to UTC, preventing `TypeError: can't compare offset-naive and offset-aware datetimes`
- **WebSearchTool startup failure** — removed invalid `graph_client=` kwarg from constructor call in `reasoning/executor.py`
- **SkillRegistry startup failure** — corrected constructor call (removed legacy `db_path=` and `.open()`)
- **Stale `owner_check_in` schedule spam** — scheduler now auto-deletes schedules with no registered callback instead of warning forever

### Changed
- **Silent failures now logged** — 13 `except Exception: pass` blocks across `autonomy/loop.py`, `server.py`, `goals/store.py`, `patterns/extract.py`, `research/artifact.py`, `performance_index.py`, `signal_collector.py`, and `session_safety.py` now emit `logger.debug`/`logger.warning` with context
- **Clarified non-actionable TODOs** — `cli.py` node keypair comment, `delivery/bridge.py` DIGEST status comment, `vector/setup.py` rebuild prerequisites

## 0.14.0 (2026-05-22)

Session context architecture — cross-session state bridge for agent continuity.

### Added
- **Session Report API** (`/v1/host/session-report`):
  - `POST /session-report` — store session summary from agent (start, end, message counts, sentiment, context notes, token usage)
  - `SessionReportStore` — in-memory FIFO (default 20 reports/contact), timezone-aware `get_recent(hours)` filtering
- **Context Digest API** (`/v1/host/context-digest`):
  - `GET /context-digest` — unified state snapshot combining pending initiatives, system health, and recent session history
  - `AgentSnapshotSystemState` schema — autonomy mode, running status, last tick age, stale flags
  - `ContextDigestSessionReport` schema — lightweight session summaries for agent context window
- **`proactive_delivery_enabled` config flag** — gates all `push_initiative()` calls; defaults to `False` for backward compatibility
  - Env var: `PROTAGINE_PROACTIVE_DELIVERY_ENABLED`
- **`last_agent_outreach_at` telemetry field** — renamed from the agent-specific name for generic agent support

### Changed
- **De-personalized codebase** — all owner references changed to generic "owner"; agent references changed to generic "agent"
- **DRY initiative mapping** — extracted `_map_initiative_to_schema()` module-level helper shared by agent-snapshot and context-digest endpoints

### Fixed
- Timezone safety in session report ingestion — `_parse_iso()` helper forces UTC on naive ISO strings
- `SessionReportStore.get_recent()` defensive tzinfo fallback before cutoff comparison
- Query parameter bounds on context-digest: `hours` (1–168), `initiative_limit` (1–100)

## 0.13.0 (2026-05-21)

Agent heartbeat and snapshot endpoints. Protagine exposes state; the agent decides when to communicate.

### Added
- **Agent Snapshot API** (`/v1/host/agent-snapshot`):
  - `GET /agent-snapshot` — comprehensive Protagine state snapshot for agent evaluation
    - Telemetry with silence hours and stale flags
    - Pending initiatives (top 20), recently completed (top 10), failed (top 10)
    - Autonomy mode, running status, last tick age
    - Computed flags: `high_priority_pending`, `failed_initiatives`, `long_initiative_silence`, `stale_autonomy_loop`
  - `POST /agent-snapshot/record-outreach` — record agent proactive outreach timestamp
- **TelemetryStore** — agent-outreach timestamp field with `to_dict()` serialization
- **Pydantic schemas** — `AgentSnapshotInitiative`, `AgentSnapshotResponse`, `RecordOutreachRequest`, `RecordOutreachResponse`
- **Tests** — `tests/test_agent_snapshot.py` with 6 unit tests (empty state, initiatives, stale tick, silence flags, outreach recording, round-trip)

### Changed
- Replaces `OwnerCheckInTask` (removed in v0.12.1) with a state-exposure model
- Protagine never messages the owner directly; the agent evaluates the snapshot and decides on outreach

### Fixed
- `silence_hours` null handling in flag computation (`None > 4` TypeError on fresh telemetry)
- `record_outreach` returns actual touched timestamp instead of stale `now.isoformat()`

## 0.12.0 (2026-05-21)

Agent work queue v0.13.0 — distributed job scheduling for autonomous agent execution.

### Added
- **Task Queue API** (`/v1/host/queue`) — 14 endpoints for job lifecycle:
  - Worker registration, heartbeat, deregistration
  - Job post, claim, start, complete, fail, release, heartbeat
  - Pending/completed listing, queue stats, digest generation
- **SQLite-backed persistent queue** with WAL mode, atomic claims, retry logic, job audit trail
- **the agent cron worker** (`scripts/agent_worker.py`) — claims and executes `AGENT_ACTION` jobs every 5 min
  - Safety: skips destructive actions when owner is in active session
  - Handles `agent_check_repo_status`, `agent_investigate_subsystem`, `agent_cleanup_orphans`
  - Graceful shutdown with SIGTERM/SIGINT deregistration
- **Digest script** (`scripts/digest.py`) — generates Protagine Digest initiative from completed/failed jobs
- **Loop integration** — `_post_agent_action_to_queue()` routes `AGENT_ACTION` initiatives to task queue instead of delivery bridge
  - Destructive actions posted as `BLOCKED` with approval fallback to delivery bridge
  - Non-destructive actions posted as `QUEUED` for immediate worker claiming
- **Initiative engine** — `_generate_agent_action_initiatives()` with 4h cooldown and dedup keys
- **Provider schema** — `protagine_claim_task` tool exposed to LLM with `worker_id` and `capabilities` params
- **`job_id` field** on Initiative model + store updatable column for task-queue linkage

### Fixed
- **11 runtime bugs** from spec review:
  - Digest payload used invalid `initiative_type` and float priority
  - Worker registration sent dict for capabilities instead of `List[str]`
  - Worker claim used `"worker_id"` key instead of `"node_id"`
  - Worker crashed on empty queue (null JSON response)
  - `worker_heartbeat` called non-existent `queue.worker_heartbeat()`
  - `queue_stats` called non-existent `queue.stats()`
  - `get_digest_jobs` referenced non-existent `completed_at` SQL column
  - Missing `/start` and `/release` endpoints
  - `protagine_claim_task` tool schema had empty properties
  - Skipped jobs stayed `CLAIMED` instead of returning to `QUEUED`
  - `repo_status` initiative generated every tick (no cooldown)

## 0.11.1 (2026-05-17)

Self-initiative execution fix and new initiative types.

### Fixed
- **Critical (4 bugs shipped in v0.11.0):**
  - `execute_initiative()` skill name mapping was a no-op (`.replace("_", "_")`) — now uses proper `_SKILL_NAME_MAP`
  - `execute_initiative()` never passed `entity_type` to `InitiativeExecutionContext` — now extracted from `trigger_data`
  - `AutonomyLoop._phase_execute()` bypassed execution entirely — now auto-executes self-initiatives via `engine.execute_initiative()` before pushing to delivery; auto-fixed initiatives skip delivery
  - `data_quality` and `operational_hygiene` skills called non-existent `.publish()` on EventBus — rewritten to use `.emit()`
- **Dependency injection** — `ToolExecutor` now accepts `graph_client` via constructor, eliminating circular imports between `reasoning/` and `api/routers/`

### Added
- **New graph schema types**: `Concept` and `Preference` nodes with full fields
- **Extended schema**: `Capability` gets `status`, `failure_count`, `last_failure_at`; `Pattern` gets `pattern_type`, `trigger`, `action`, `recurrence_count`, `last_triggered_at`, `is_active`
- **New context loaders**: `_load_capability_gaps()`, `_load_knowledge_gaps()`, `_load_behavioral_patterns()` — wired into `InitiativeEngine._load_graph_context()`
- **New initiative generators**: `_generate_capability_gap_initiatives()`, `_generate_knowledge_acquisition_initiatives()`, `_generate_behavioral_correction_initiatives()`
- **New executor skills**: `CapabilityGapSkill`, `KnowledgeAcquisitionSkill` (proposal-only), `BehavioralCorrectionSkill` (proposal-only, stores preferences to graph)
- **`trigger_data` field** added to `Initiative` dataclass so context flows end-to-end from generator → execution
- **`_build_initiative_context()`** extended for `capability_gap`, `knowledge_acquisition`, `behavioral_correction` types

## 0.8.3 (2026-05-15)

Silence-triggered owner check-in and telemetry fix.

### Added
- **Owner Check-In scheduler task** — detects when the autonomy loop has not generated an initiative for a configurable period (default: 1 hour), then emits a `proactive_message` initiative asking the owner if anything is needed
  - `OwnerCheckInTask` with file-backed persistence (`~/.protagine/data/autonomy_checkin.json`) so state survives restarts
  - Owner resolution: config override (`PROTAGINE_OWNER_CONTACT_ID`) → identity manager → highest-scored non-stranger contact
  - Safety guards: quiet hours (23:00–07:00), cooldown (default 4h), disabled if telemetry missing
  - Configurable via `PROTAGINE_OWNER_CHECK_IN_ENABLED`, `PROTAGINE_OWNER_CHECK_IN_SILENT_HOURS`, `PROTAGINE_OWNER_CHECK_IN_COOLDOWN_HOURS`
  - Runs as a scheduler task independent of the autonomy loop tick interval

### Fixed
- **`AutonomyLoop._phase_execute()`** now touches `TelemetryStore.last_initiative_at` when initiatives are successfully pushed, fixing `silence_hours("initiative")` which previously reported infinite silence even during active loop operation

## 0.7.24 (2026-05-15)

Anti-spam initiative delivery and autonomy pipeline spec.

### Fixed
- **Initiative dedup reactivation** — only FAILED initiatives are reactivated on duplicate `dedup_key`. Completed and cancelled initiatives stay terminal, eliminating infinite-loop follow-up spam.

### Added
- **`plugins/hermes-plugin/examples/protagine-initiative-poller.py`** — production poller with `dedup_key` tracking, `delivery_context` injection, and env-var configuration.
- **`plugins/hermes-plugin/examples/hook-handler.py`** — example Hermes hook handler with nested payload support.


# Changelog

## 0.7.22 (2026-05-14)

Initiative dedup fix and Hermes turns/sync telemetry.

### Fixed
- **InitiativeStore.create()** now reactivates failed/completed/cancelled initiatives when a duplicate `dedup_key` is submitted, instead of crashing on the SQLite UNIQUE constraint.
- **Hermes plugin websockets 15 compat** — `extra_headers` → `additional_headers`.
- **Autonomy follow-up generator** now handles `priority=None` gracefully.

### Added
- **ProtagineClient.sync_turn()** — POSTs session summaries to `/v1/host/turns/sync`.
- **`agent:start` hook** in the Hermes plugin captures `session_id` for cross-turn state.
- **`on_session_end` hook** extracts last user/assistant messages and syncs them to Protagine automatically.
- Zero Hermes core patches required — all telemetry is plugin-side.

## 0.7.21 (2026-05-12)

Bug-fix release: initiative delivery via WebSocket and plugin apiKey compatibility.

### Fixed
- **Autonomy loop** now broadcasts `initiative` events via WebSocket in addition to HTTP push, ensuring Hermes subscribers receive them in real time.
- **ProactiveDeliveryBridge** falls back to WebSocket broadcast when the gateway's `/internal/initiative` endpoint is unreachable or returns an error.
- **OpenClaw plugin** auth check now accepts both `apiKey` (manifest) and `api_key` (normalized) for compatibility across OpenClaw versions.


## 0.7.20 (2026-05-11)

Qwen3 reranker token config fallback.

### Fixed
- **NativeMLXRerankerProvider** now falls back to `1_LogitScore/config.json` when token IDs are not found in `config_sentence_transformers.json`
  - Qwen3 rerankers (e.g. `Qwen/Qwen3-Reranker-0.6B`, `Qwen/Qwen3-Reranker-8B`) store `true_token_id` and `false_token_id` in a separate config file
  - Without this fallback the provider silently used raw max-logit scoring, producing incorrect relevance scores

## 0.7.19 (2026-05-11)

Native MLX embedding and reranker providers for Apple Silicon.

### Added
- **NativeMLXEmbeddingProvider** — true Apple Silicon MLX support via `mlx-embeddings`
  - Loads models directly into MLX arrays, bypassing PyTorch MPS overhead
  - Supports original HuggingFace models (on-the-fly conversion) and pre-converted `mlx-community` checkpoints
  - Recommended models: `Qwen/Qwen3-Embedding-8B` (4096 dims), `BAAI/bge-m3` (1024 dims)
- **NativeMLXRerankerProvider** — native MLX CrossEncoder via `mlx-lm`
  - Extracts true/false token logits for relevance scoring
  - Supports `Qwen/Qwen3-Reranker-8B` and `BAAI/bge-reranker-v2-m3`
- **Hardware tier matrix** — new `native-mlx-high` (64GB+) and `native-mlx-balanced` (32GB+) tiers
- **Auto-detection** — server and wizard now prefer `native_mlx` when `mlx-embeddings` is installed
- **Setup wizard** — supports `native_mlx` provider selection and model pre-download via MLX loaders

### Changed
- Legacy `MLXEmbeddingProvider` and `MLXRerankerProvider` docstrings clarified: they use PyTorch MPS, not true MLX

## 0.7.18 (2026-05-11)

Hermes integration suite, autonomy bridge, and initiative engine hardening.

### Added
- **Protagine-Hermes integration suite** — full plugin for Hermes agent context injection
  - Graph memory MCP server with Neo4j-backed entity/relationship queries
  - Contact reminders via Hermes todo system with neglected-contact detection
  - Context assembly endpoint for agent prompt enrichment
  - Setup wizard (`protagine setup --hermes`) for one-command installation
- **Protagine autonomy bridge** — Hermes-native initiative tools
  - `initiatives_list` tool for querying pending initiatives
  - `initiative_acknowledge` / `initiative_complete` / `initiative_snooze` lifecycle tools
  - `autonomy_cycle` tool for triggering reactive autonomy ticks
- **Initiative engine hardening**
  - Name deduplication for Person nodes (first + last → canonical full name)
  - Junk contact filtering — skips nodes missing both name and email
  - Graph cleanup — removes stale `:HAS_CONTACT` relationships for non-Person nodes
  - `entity_id` and `dedup_key` fields on health/scheduling initiatives

### Fixed
- **`/autonomy/cycle` reactive mode** — now runs `_tick()` directly instead of scheduling a background task (#23)
- **Schema-adaptive graph queries** — handles `lastCommunication` vs `last_interaction` property variants gracefully

## 0.7.17 (2026-05-10)

MLX reranker fix and new `/memory/rerank` endpoint.

### Added
- **POST /v1/host/memory/rerank** — rerank documents by relevance to a query
  - Returns ranked results with `index`, `score`, `text`
  - Supports up to 256 documents per request
  - Returns 501 if reranker not initialized

### Fixed
- **MLX reranker** — same PyTorch MPS deadlock fix as embeddings (Issue #17)
  - `MLXRerankerProvider.warmup()` now runs synchronously in main thread
- **Reranker initialization** — `make_reranker_provider(spec=None)` returned None
  - Now directly instantiates the correct provider class based on `hw.gpu_type`
- **Reranker warmup** — was never called during lifespan startup, now explicitly awaited
- **Health endpoint** — now exposes `rerank` capability when reranker is wired

## 0.7.16 (2026-05-10)

MLX embedding provider fix and harrier model deprecation.

### Fixed
- **Critical**: MLX embedding provider no longer hangs on Apple Silicon during startup (#17)
  - Root cause: PyTorch MPS backend deadlocked when model initialization ran in `asyncio.run_in_executor()` during FastAPI lifespan startup
  - Fix: `MLXEmbeddingProvider.warmup()` now loads models synchronously in the main thread
  - Startup is not serving requests yet, so brief blocking is acceptable

### Changed
- **Deprecated**: Removed non-functional `microsoft/harrier-oss-v1-27b` model from tiers 5 and 6
  - Replaced with validated `Qwen/Qwen3-Embedding-8B` (8B params, 4096 dims)
  - Affects 128GB+ RAM systems (Mac Studio Ultra, DGX Spark, servers)
- Setup wizard now warns Apple Silicon users about MLX provider and directs them to CPU provider for stability

## 0.7.14 (2026-05-07)

Initiative engine graph context loading and comprehensive bug fixes.

### Added
- `InitiativeEngine._load_graph_context()` — automatic graph + mind model queries before generation
- Graph loaders: `_load_blocked_goals()`, `_load_neglected_contacts()`, `_load_health_trends()`, `_load_scheduling_opportunities()`, `_load_pending_signals()`, `_load_pending_research_tasks()`
- `InitiativeConfig` dataclass with env var loading (`PROTAGINE_INITIATIVE_*`)
- 10-second graph context cache to avoid redundant queries within same tick
- `clear_context()` resets `_last_graph_load` (Bug 37)
- Priority blending: graph priority (40%) + time-based priority (60%) for follow-ups (Bug 20)
- `entity_id` and `dedup_key` for health/scheduling initiatives (Bugs 44, 45)
- `max_initiatives` parameter to limit output (default 20, Bug 43)
- In-memory initiative list with 1000-item cap (Bug 36)
- 38 comprehensive unit tests for initiative generation
- Environment variables: `PROTAGINE_INITIATIVE_CONTACT_NEGLECT_DAYS`, `PROTAGINE_INITIATIVE_GOAL_BLOCK_DAYS`, `PROTAGINE_INITIATIVE_HEALTH_THRESHOLD`, `PROTAGINE_INITIATIVE_GAP_THRESHOLD`, `PROTAGINE_INITIATIVE_RESEARCH_AGE_DAYS`, `PROTAGINE_INITIATIVE_SIGNAL_THRESHOLD`

### Fixed
- **Critical**: `mark_initiative_generated()` now called for ALL initiatives inside persistence loop (Bug 11)
- **Critical**: Research tasks use actual age from `created_at`, not threshold days (Bug 12)
- **Critical**: `complete()` uses `entity_id` (goal ID) not initiative ID for goal store (Bug 47)
- **Critical**: Neo4j `DateTime` objects handled via `_parse_neo4j_datetime()` (Bugs 50, 51)
- **Critical**: `created_at` uses timezone-aware `datetime.now(timezone.utc)` (Bug 38)
- Negative days prevented with `max(0, ...)` (Bug 13)
- NULL `last_interaction` gets 2× threshold days (Bug 14)
- `acknowledge()` removes from in-memory list (Bug 22)
- Generators run in parallel with `asyncio.gather()` (Bug 33)
- Signal loading separated from scheduling check (Bug 40)
- `get_active()` only falls back on exception, not empty result (Bug 54)
- Store validates priority range [0, 1] (Bug 26)
- `SubsystemRegistry.anomalies` uses shared event bus (Bug 41)
- Parameter validation in `generate()` (Bugs 57, 58)
- Env var parsing with fallback on invalid values (Bug 59)

### Changed
- Exception handling: specific types for connection vs validation vs unexpected errors
- `clear_context(context_type)` preserves `_last_graph_load` (only full clear resets)

## 0.7.10 (2026-04-27)

Initiative deduplication and LLM feedback loop.

### Added
- `last_initiative_at`, `snoozed_until`, `snooze_count`, `dismissal_reason` fields on Goal model
- `GoalStore.complete_task()`, `snooze_task()`, `dismiss_task()`, `get_active_tasks()`, `mark_initiative_generated()`
- Snooze fatigue: auto-dismiss after 3 snoozes
- Initiative engine dedup via GoalStore cooldown (no in-memory state, persists across restarts)
- MCP tools: `protagine_task_complete`, `protagine_task_snooze`, `protagine_task_dismiss`, `protagine_initiative_feedback`
- API endpoints: `/tasks/{id}/complete`, `/tasks/{id}/snooze`, `/tasks/{id}/dismiss`, `/initiatives/{id}/respond`
- Native tool definitions for task management in `tools/definitions.py`
- `InitiativeConfig` dataclass with configurable cooldowns
- `entity_type` field in initiative payload
- Action hints in `formatInitiativeText()` for LLM task management
- Environment variables: `PROTAGINE_INITIATIVE_COOLDOWN_TASKS` (default 12h), `PROTAGINE_INITIATIVE_COOLDOWN_CONTACTS` (default 72h)

### Changed
- `_feed_pending_tasks()` now uses `get_active_tasks()` with cooldown awareness
- Initiative generation accepts `cooldown_tasks` and `cooldown_contacts` parameters

## 0.6.21 (2026-04-24)

Fixed port conflict handling in foreground mode.

### Fixed
- Foreground mode (`protagine start`) now checks if port is in use before starting
- Exits with error if port occupied, with helpful message
- `--force` flag works in both foreground and daemon modes to kill existing process

## 0.6.20 (2026-04-24)

Harness integration refactor with new CLI flags.

### Changed
- New CLI flags for harness configuration:
  - `--mcp-harnesses` for coding harnesses (claude-code, codex, crush, opencode)
  - `--agent-harness` for agent harnesses (openclaw, hermes)
  - `--no-harness` for standalone mode
- `--host-framework` deprecated but still works for backward compatibility
- Step 3 renamed from "Host framework" to "Harness integration"
- Separate detection and setup for coding harnesses vs agent harnesses
- Standalone mode is now explicit and first-class
- Shows install instructions when OpenClaw is requested but not installed
- Detects Node.js stability (version manager vs system install)

### Added
- `_detect_coding_harnesses()` - detect MCP-capable coding harnesses
- `_detect_agent_harnesses()` - detect agent harnesses (OpenClaw, Hermes)
- `_check_nodejs_stability()` - check if Node.js is system-wide or version manager
- `_setup_mcp_harnesses()` - configure multiple MCP harnesses
- `_setup_agent_harness()` - configure agent harness plugin
- `_show_openclaw_install_instructions()` - platform-specific install guide

### Fixed
- Protagine can now run completely standalone with no harness
- Better guidance when harness not installed

## 0.6.19 (2026-04-23)

Fixed crash in wizard plugin setup.

### Fixed
- `non_interactive` parameter now passed to `_configure_openclaw_plugin()`
- Prevents `NameError: name 'non_interactive' is not defined` in non-interactive mode

## 0.6.18 (2026-04-23)

Wizard now uses `openclaw plugins install` for proper plugin registration.

### Changed
- Use `openclaw plugins install @aevonix/protagine` instead of `npm install -g`
- Check if plugin is already installed before reinstalling
- Prompt to restart gateway after plugin install
- Better error messages for permission/network failures

## 0.6.17 (2026-04-23)

Fixed missing npm dependency.

### Fixed
- Added `@sinclair/typebox` dependency (used by tool-registrar)

## 0.6.16 (2026-04-23)

Added OpenClaw plugin manifest for native plugin installation.

### Added
- `openclaw.plugin.json` manifest for `openclaw plugins install` support
- Declares contextEngine and memory contracts
- Config schema with sidecarUrl, apiKey, hostId settings

## 0.6.15 (2026-04-23)

Improved OpenClaw plugin installation with better error handling.

### Fixed
- Check Node.js version before npm install (requires v22.16+)
- Retry npm install with `sudo` on EACCES permission errors
- Clear guidance when Node version is too old
- Better error messages and next steps

## 0.6.14 (2026-04-23)

Fix: OpenClaw plugin auto-install via npm.

### Fixed
- Wizard now checks if `@aevonix/protagine` is installed globally
- Auto-installs via `npm install -g @aevonix/protagine` if missing
- Better error messages when config settings fail

## 0.6.13 (2026-04-23)

Neo4j health check system with auto-recovery.

### New
- `_neo4j_health_check()`: Connect + auth + query verification
- `_neo4j_poll_health()`: Poll with timeout and progress messages
- `PROTAGINE_NEO4J_STARTUP_TIMEOUT` env var (default 30s)

### Fixed
- Neo4j now verified healthy before sidecar accepts requests
- Auto-restart on failed health check for running containers
- Clear error messages with actionable steps on failure
- No more arbitrary `sleep(3)` — waits exactly as long as needed

### Behavior
- Cold start → create container → poll with timeout
- Warm start (stopped) → start → poll with timeout
- Hot start (running) → quick check → restart + poll on failure
- All paths degrade gracefully with clear user guidance

## 0.6.12 (2026-04-23)

Fix: Neo4j auto-start now works in foreground mode.

### Fixed
- Neo4j check moved before detach decision (was only in daemon mode)
- Works for both `protagine start` and `protagine start -d`

## 0.6.11 (2026-04-23)

Fix: Neo4j auto-start and validate EOF.

### Fixed
- `protagine start` now checks and starts Neo4j container if needed
- `protagine validate` handles EOF gracefully with helpful message
- Neo4j container persisted across sidecar restarts

## 0.6.10 (2026-04-23)

Fix: `--tier` CLI arg now correctly skips interactive tier selection.

### Fixed
- Tier CLI arg in non-interactive mode now properly skips the tier selection UI
- Tier selection UI is now inside the else block for interactive mode

## 0.6.9 (2026-04-23)

Wizard fixes: Neo4j docker-run and multimodal EOF.

### Fixed
- Neo4j startup now uses `docker run` instead of docker-compose.yml
- Multimodal prompt now uses `_prompt()` for EOF handling
- OpenClaw config set error handling improved

## 0.6.8 (2026-04-23)

Protagine init wizard: non-interactive mode and all piped input issues fixed.

### New
- `--non-interactive` mode for headless/automated setup
- `--host-framework` CLI arg (openclaw, hermes, claude-code, codex, crush, standalone)
- `--contact-name` CLI arg
- `--bind` and `--port` CLI args for network configuration
- `--tier` CLI arg for embedding tier selection (0-7)
- `--neo4j-password` CLI arg
- `--skip-model-download` to defer embedding model download
- `--start` flag to start sidecar after init
- `_check_neo4j_auth()` to detect if Neo4j requires authentication
- `_write_config_yaml()` writes `~/.protagine/config.yaml` alongside `.env`

### Fixed
- Issue 1: `_prompt()` returns default on EOF instead of crashing
- Issue 2: Tier selection no longer skipped silently when stdin exhausted
- Issue 3: Config YAML now written to `~/.protagine/config.yaml`
- Issue 4: Bind address prompt added (interactive) + CLI args
- Issue 5: Neo4j auth detection skips password prompt when auth disabled
- Issue 6: SQLite DBs now stored in `~/.protagine/data/` instead of `~/`
- Issue 8: `--skip-model-download` defers model download to first start

### Example
```bash
protagine init --non-interactive \\
  --host-framework claude-code \\
  --contact-name owner \\
  --bind 0.0.0.0 \\
  --port 7777 \\
  --tier 6 \\
  --start
```

## 0.6.7 (2026-04-23)

Second code audit: MCP contract, Hermes integration, runtime crash, and security fixes.

### Fixed
- C1: `protagine_get_context` now always sends `incoming_message` (was conditionally omitted → 422)
- C2: `TurnSyncRequest` has `user_message`/`assistant_message` fields; sidecar extracts from raw messages
- C3: YAML harness config uses `${PROTAGINE_API_KEY}` template (was baking raw key to disk)
- C4: Synthesized skills get `ProtagineRuntime` handle injected (was crashing with missing arg)
- H1: `cancellation_reason` goes into `metadata` dict (was dropped by Pydantic)
- H2: `context` param type changed to `dict` (was str → 422)
- H3: WebSocket `onEvent` wrapped in try/catch (was unguarded → crash propagation)
- H4: Hermes provider logs WARN on 401/403 (was DEBUG, silent degradation)
- H5: Initiative IDs use UUID (was collision-prone `hash() % 100000`)
- H6: `datetime.now(timezone.utc)` everywhere (was mixing naive/aware → TypeError)
- H7: Sandbox `__import__` wrapped with manifest whitelist enforcement
- M1: `arousal` param defaults to 0.5 in `protagine_record_affect`
- M2: `expected` param is optional in `protagine_record_surprise`
- M6: `refreshSkillTools` debounced with in-flight promise (was racy)

### New
- `sidecar/protagine/skills/runtime.py` — `ProtagineRuntime` class for synthesized skill tool access
- `allowed_imports` field on `SkillPermissions` for manifest-declared module whitelist

## 0.6.2 (2026-04-23)

Code audit fixes from Claude Code security scan.

### Fixed
- H1: Added missing `set_reranker` import to server.py (was silently failing on reranker config)
- H2: Fixed tautological test assertions in test_sidecar.py
- H3: Implemented `probeVectorAvailability` — now checks embed capability instead of hardcoded false
- H4: Removed dead `lastBoundary` variable in pipeline.ts
- H5: Added metrics tracking for invalid blocks (`blocks_rejected`, `blocks_accepted`, `invalid_signatures`)
- H6: Comprehensive block validation: merkle root check, future timestamp rejection, detailed NACK reasons
- H7: 12 Byzantine-fault tests for Raft consensus
- Medium: Error logging for Raft fire-and-forget message sends via `_spawn_send()` helper

### Tests
- 12 new Byzantine-fault tests in `test_consensus_byzantine.py`

## 0.6.1 (2026-04-22)

Protagine MCP Server: shared intelligence across agent and coding harnesses.

### New
- MCP server exposing 14 tools, 4+ resources, 3 prompts to Claude Code, Codex, and Crush
- `protagine mcp` CLI: run (stdio/HTTP), detect, setup (selective, --dry-run), remove (--dry-run)
- `/mcp` HTTP endpoint on sidecar for streamable HTTP transport
- Harness auto-detection (claude, codex, crush CLIs)
- Selective harness setup: choose which harnesses connect, not all-or-nothing
- Source tracking via PROTAGINE_MCP_SOURCE env var, auto-injected by MCP server
- Provenance field on all MCP writes (separate from sidecar's source enum)
- Contact ID required during setup, set via PROTAGINE_MCP_CONTACT_ID
- Host framework selection in setup wizard (OpenClaw, Claude Code, Codex, Crush, Standalone)
- `mcp[cli]>=1.0` as optional dependency
- 51 MCP unit tests (27 server + 24 config)
- 14/14 MCP tools E2E validated against live sidecar

### Fixed
- World model search endpoint: POST /v1/host/world/entities/query (not GET /search)
- Provenance vs source: MCP provenance writes to `provenance` field, not `source`, to avoid enum conflicts with sidecar schemas

## 0.5.8 (2026-04-22)

New CLI commands for lifecycle management and E2E validation.

- **Added:** `protagine start -d` — daemon mode with PID tracking, port conflict detection, auto-kill stale processes
- **Added:** `protagine stop` — clean shutdown (SIGTERM → SIGKILL fallback)
- **Added:** `protagine status` — health check + E2E validation status
- **Added:** `protagine validate` — full pipeline test (seeds data, checks context assembly, optional LLM test)
- **Added:** E2E validation stamp (`.protagine-e2e-validated`) — persists across restarts
- **Added:** `protagine doctor` check #34: E2E pipeline validated
- **Added:** Validation warnings in `protagine status` and `protagine start` until E2E is run
- **Added:** Setup wizard prompts for `protagine validate` after setup
- **Fixed:** EOFError on `protagine start -d` when stdin unavailable

## 0.5.7 (2026-04-22)

Setup wizard bug fixes and gateway restart flow.

- **Fixed:** Neo4j connectivity test in wizard (uses raw driver, not ProtagineGraph)
- **Fixed:** Sidecar auto-start in wizard (writes to log file, start_new_session)
- **Fixed:** TIER_TABLE → TIERS import (correct export name)
- **Fixed:** Skip multimodal check when embeddings disabled
- **Added:** Gateway restart verification — waits for restart, checks plugin loaded
- **Added:** Warning that Protagine won't receive messages until gateway restart

## 0.5.6 (2026-04-22)

Setup wizard fixes for fresh install experience.

- **Added:** "Skip embeddings" option in tier selection (option 3) — Protagine runs without vector search
- **Fixed:** Sidecar auto-start in wizard now uses uvicorn instead of bare module
- **Fixed:** ContactsStore init uses correct `sqlite_path` parameter
- **Fixed:** Neo4j connectivity test uses driver session directly (bypasses query allowlist)
- **Fixed:** `PROTAGINE_EMBED_PROVIDER=skip` no longer crashes EmbeddingPipeline
- **Validated:** Full `pip install protagine` → `protagine init` → sidecar start → health=ok flow

## 0.5.5 (2026-04-22)

Critical packaging and startup fixes.

- **Fixed:** SQL schema files (goals, contacts, task_queue, world_model) missing from pip wheel
- **Fixed:** `PROTAGINE_EMBED_PROVIDER=skip` now gracefully disables embeddings instead of crashing
- **Fixed:** Affect in context assembly reads `current_valence`/`current_arousal` (AffectStore API)
- **Fixed:** `build/` directory accidentally committed to git (removed, added to .gitignore)
- **Added:** `package_data` in pyproject.toml to include SQL/JSON/YAML files in wheel
- **Added:** E2E test scripts for live environment validation (full cycle + turn sync extraction)

## 0.5.4 (2026-04-22)

Fixes for autonomy loop, startup errors, and graph traversal.

### Autonomy
- Autonomy loop now auto-starts on sidecar startup (was manual only)
- AnomalyDetector now receives graph_client + EventBus (was missing required args)
- Fixed import path for RelationshipScorer (nested directory structure)
- MetaLearner returns default CPI when PerformanceIndexComputer not wired (was RuntimeError)

### Goals
- All enum `.value` accesses now use hasattr guards (str vs enum crash)

### Neo4j Backend
- `get_neighbors()` now bidirectional — follows both outgoing and incoming relationships
- Fixes neighborhood traversal and path finding for directed edges

### Health & Setup
- Health endpoint shows autonomy running state + tick count
- Setup wizard adds ownContextEngine + ownMemoryCapability to OpenClaw plugin config
- Setup wizard auto-selects WORLD_MODEL_BACKEND based on Neo4j password
- Setup wizard verifies data flow (creates test commitment, checks context assembly)
- Added asyncio + timezone imports where missing

## 0.5.3 (2026-04-22)

Neo4j graph database backend for the World Model, plus full CRUD API endpoints.

### Neo4j Backend
- Full `Neo4jBackend` implementing the same interface as SQLiteBackend
- Entity and relationship CRUD with Cypher MERGE/SET
- Native graph traversal via `get_neighbors()`
- Full-text search via Neo4j index
- Observations, merge proposals, entity resolution, stats
- Auto-schema on connect: indexes, constraints, full-text index
- Compatible with Neo4j driver v6 async API
- Backend selection via `WORLD_MODEL_BACKEND` env var (sqlite/neo4j)
- Env vars: `NEO4J_URI`, `NEO4J_DATABASE`, `NEO4J_USER`, `NEO4J_PASSWORD`
- Automatic fallback to SQLite if driver missing or Neo4j unavailable

### World Model API Endpoints (12 new)
- `POST /world/entities` — create entity
- `GET /world/entities/{id}` — get entity
- `PATCH /world/entities/{id}` — update entity
- `DELETE /world/entities/{id}` — delete entity
- `POST /world/relationships` — create relationship
- `GET /world/relationships` — list/query relationships with filters
- `GET /world/relationships/{id}` — get relationship
- `PATCH /world/relationships/{id}` — update/close relationship
- `DELETE /world/relationships/{id}` — close relationship
- `GET /world/entities/{id}/neighborhood` — BFS graph traversal
- `GET /world/entities/{src}/path/{tgt}` — shortest path
- `GET /world/stats` — world model statistics

### Fixes
- Default `neo4j_database` changed from `protagine` to `neo4j` (Community edition compatibility)
- Stats query filters NULL entity_types from legacy data

## 0.5.2 (2026-04-22)

Security and dependency maintenance.

### Security
- Updated openclaw dependency to 2026.4.21 (resolves 9 Dependabot alerts: protobufjs critical, tar high, axios moderate)
- All transitive vulnerabilities now resolved (0 npm audit findings)

### Bug Fixes
- Fixed `_llm_router` reference in ToM extractor init log (would crash when LLM router is wired)

## 0.5.1 (2026-04-22)

ToM Layer 3: LLM extraction for affect and shared facts.

### ToM LLM Extraction
- TomExtractor: async LLM-backed extraction from conversation turns
- Affect extraction: valence/arousal/trigger from conversation text (neutral readings skipped)
- Fact extraction: knowledge items with source classification
- Per-contact throttle (5 min default, configurable via PROTAGINE_TOM_EXTRACTION_THROTTLE_MINUTES)
- Auto-fires on turn_sync when LLM router is wired
- POST /v1/host/tom/extract for manual extraction
- 21 new unit tests

## 0.5.0 (2026-04-22)

Pattern Extraction + Surprise Engine: pattern detection and anomaly scoring.

### Pattern Extraction
- PatternStore: SQLite-backed CRUD with upsert (frequency increment on duplicate pattern_key)
- Deactivate stale patterns, list with filters (type, min frequency, source, active)
- 4 pattern types: entity_cooccurrence, relation_frequency, temporal_sequence, attribute_cluster
- Extraction logic: entity cooccurrence, relation frequency, attribute clusters
- Extraction workers graceful no-op when world model not wired
- `POST /v1/host/patterns/extract` trigger endpoint

### Surprise Engine
- SurpriseStore: SQLite-backed CRUD, resolve, count_unresolved, get_unresolved
- Scorer: pattern-matching scoring (no match=0.7, violated=0.5+conf×0.5, low freq=0.2, high freq=0.0)
- Auto-score via `auto_score` flag on create (routes through scorer with pattern store)
- `GET /v1/host/surprises/unresolved` for high-score unresolved surprises

### Integration
- Context assembly: surprises section at priority 75 (between affect 80 and shared facts 70)
- Autonomy: `surprise_accumulation` condition (30min interval, fires when 5+ unresolved in 1h)
- Events: `pattern.created`, `pattern.extracted`, `surprise.high`, `surprise.accumulation`
- TypeScript client + types + config + cache channels
- 38 new unit tests (158 total)

## 0.4.0 (2026-04-22)

Theory of Mind v0.1: affect tracking and shared facts.

### Affect Tracking
- AffectStore: per-contact emotional valence (-1.0 to 1.0) and arousal (0.0 to 1.0)
- Exponential decay toward neutral (5% per hour, configurable)
- Trend detection: improving, declining, stable
- Negative spike detection (valence <= -0.5)
- Sustained decline detection (3+ events with declining trend)
- Context assembly injection: Emotional Context section (priority 80)
- Autonomy: affect_decline check every 30min
- Events: affect.event_created, affect.negative_spike, affect.sustained_decline
- API: POST /affect/events, GET /affect/state/{id}, GET /affect/history/{id}, DELETE /affect/events/{id}

### Shared Facts
- SharedFactsStore: what the agent believes each contact knows
- Fact categories: told_by_contact, told_to_contact, shared_context, inferred
- Confidence scores (0.0-1.0), TTL expiry, expired fact purging
- Context assembly injection: Shared Knowledge section (priority 70)
- Event: mind.fact_created
- API: POST /mind/facts, GET /mind/facts, GET /mind/facts/{id}, PATCH /mind/facts/{id}, DELETE /mind/facts/{id}

## 0.3.0 (2026-04-21)

Cognition substrate, commitment tracking, LLM compression tier 3, and native tool fixes.

### Cognition Substrate
- Commitment Store: SQLite-backed CRUD, status transitions (pending/fulfilled/cancelled/broken), overdue detection, delete guard (cancel first)
- Context Assembly: Pending Commitments section (priority 72) injected per contact
- Cognition Prompt + Trigger: `POST /v1/host/cognition/trigger`, throttle, `cognition.requested` event
- Trigger Pipeline: turn sync + signal ingest auto-fire cognition triggers (non-blocking)
- Config: `PROTAGINE_COGNITION_ENABLED` (default false), `PROTAGINE_COGNITION_MODEL`, `PROTAGINE_COGNITION_THROTTLE_SECONDS` (default 30)
- Config: `PROTAGINE_COMMITMENTS_ENABLED` (default true), `PROTAGINE_COMMITMENT_CHECK_INTERVAL_MINUTES` (default 30)

### LLM Compression Tier 3
- `compress_sections_with_llm()` async wrapper for aggressive mode
- Falls back to sync tight-truncation on any LLM error
- Automatically used when `_llm_router` is wired and mode is aggressive

### DIGEST Delivery Channel
- `build_digest_bundle()`, `consume_digest()`, `flush_digests_to_gateway()`
- Scheduled daily via autonomy scheduler (configurable interval)
- Config: `PROTAGINE_DIGEST_HEADER`, `PROTAGINE_DIGEST_INTERVAL_SECONDS` (default 86400)

### LLM Entity Extractor
- Fallback for ExtractionPipeline when format extractors return nothing
- Bounded input (12K chars) and output (1024 tokens), JSON-only parsing
- Graceful degradation: returns empty list on any failure

### Fixes
- Native tool registration: register `.execute` methods instead of class instances (critical bug, tools were uncallable)
- ToolExecutor.get_definitions: only advertise tools with registered handlers
- execute_batch: JSON-serialize dict/list results instead of str()
- list_research endpoint: call pipeline instead of returning empty list
- Duplicate `set_commitment_store` import removed
- `.gitignore`: added `sidecar/events/`

## 0.2.0 (2026-04-21)

Security hardening, event journal, and adaptive context compression.

### Security
- Auth: `hmac.compare_digest` for API key checks (timing-attack resistant)
- `/v1/host/configure` blocked in dev mode (no PROTAGINE_API_KEY)
- Body size limit middleware (10MB default, configurable)
- WebSocket frame size cap (1MB default)
- Subprocess-isolated skill sandbox with `setrlimit` guards (mem/CPU/fds/nproc)
- AST scanner: ESC001 (dunder escape chains), ESC002 (dynamic getattr/setattr)
- Rate limiter: SQLite-persisted delivery counts, crashloop-safe
- PII: hashed contact data in logs, no raw PII in error messages
- Neo4j: property allowlist on `update_person`, generated per-install password
- Docker Compose: requires `NEO4J_PASSWORD`, no default fallback

### Event Journal + Replay
- Append-only file-per-event journal with atomic writes and SHA-256 checksums
- `GET /v1/host/events/replay?since=&limit=&types=` endpoint
- WebSocket reconnect with `lastEventId` for replaying missed events
- Plugin tracks `lastEventTimestamp` across reconnects
- Bounded retention (default 500 events, configurable)

### Adaptive Context Compression
- Three modes: off (default), conservative, balanced, aggressive
- Tier 1: Drop low-relevance sections (query-aware F1 scoring)
- Tier 2: Sentence-boundary-aware truncation
- Tier 3: Tight truncation (LLM summarization placeholder for future)
- Per-request override via API field, plugin config setting

## 0.1.0 (2026-04-16)

First release with all adapter shapes matching the real OpenClaw SDK contracts.

### Adapters

- **MemoryPluginCapability** — `promptBuilder` (auto-inject instructions with citation hints) + `MemoryPluginRuntime` (`ProtagineMemorySearchManager` backed by `/v1/host/memory/*` endpoints; `search`, `readFile`, `status`, `sync`, `probeEmbeddingAvailability`, `probeVectorAvailability`). Gated by `ownMemoryCapability` config flag (exclusive slot).
- **MemoryEmbeddingProviderAdapter** — `create()` factory with explicit `{provider: null}` when sidecar has no embedder. `embedQuery`/`embedBatch` delegate to `/v1/host/memory/embed` with 64-input chunking. Errors propagate (no silent zero-vectors).
- **ContextEngine** — `info` + `ingest` (no-op) + `assemble` (calls `/v1/host/context/assemble`, folds sections into `systemPromptAddition`) + `compact` (delegated to OpenClaw runtime via `delegateCompactionToRuntime`). `ownsCompaction: false`.
- **AgentHarness** — `supports()` with 3-layer gate (config flag + runtime match + capability probe), `runAttempt()` that never throws (always returns shaped `EmbeddedRunAttemptResult` with `promptError` on failure for harness-fallback routing), `reset` + `dispose` hooks. Currently 501 because reasoning endpoint isn't wired yet (Stage B).
- **Safety hook** (`message_sending`) — fail-closed by default (`failSafetyClosed: true`). Per-chunk safety with capability gating. Never throws out of the handler.
- **Post-turn hook** (`reply_dispatch`) — fire-and-forget observer via `Promise.allSettled`. Sends `signals/ingest` + `turns/sync` concurrently. Never takes over dispatch.
- **Events lifecycle service** — WebSocket subscriber with first-message auth, diagnostic `summarizeHostEvent` logging, error boundary per frame.

### Infrastructure

- `ProtagineSidecarClient` — typed HTTP/WS client with one method per `/v1/host/*` endpoint, AbortSignal support on `reasoningTurn`, first-message WebSocket auth.
- `capabilityProbe` — single-flight lazy probe with `has()`, `hasProbedSuccessfully()`, `snapshot()` (synchronous accessor for `supports()`), auto-reset on failure.
- `withDegradation` — shared error taxonomy (501/5xx → fallback, 4xx → re-throw, network → fallback).
- `ProtagineApiError`, `ProtagineEmbedUnavailableError` — typed errors.
- `summarizeHostEvent` — diagnostic log formatter with safe default (no payload leak on unknown types).
- Zod-validated config schema with `failSafetyClosed`, `ownMemoryCapability`, `ownReasoningLoop` flags.

### Tests

- 116 unit tests across 8 test files.
- 13 integration tests against a live protagine-core sidecar (gated by `PROTAGINE_SMOKE_URL` env var).

### Type safety

- Real `OpenClawPluginApi` imported from `openclaw/plugin-sdk/plugin-entry` (not a structural stub).
- Zero `@ts-expect-error` markers — every `register*` / `on` call type-checks against the SDK.
- SDK types derived via `Parameters<...>` / `Awaited<ReturnType<...>>` where public exports aren't available.

## 0.0.1 (2026-04-14)

Initial scaffold. Adapter shapes did not match the real SDK contracts. Superseded by 0.1.0.

## 0.1.1 (2026-04-21)

### Added
- Context engine slot: `plugins.slots.contextEngine = "protagine"` auto-configured by setup wizard and documented in README
- Setup wizard now starts the sidecar, verifies health, checks LLM credentials, offers gateway restart, and runs `protagine doctor`
- Node.js/npm guard in wizard — warns if missing before attempting plugin build
- Full identity awareness through Protagine's context engine (protagine_id, node_id, trust_tier, Genesis status)
- 22 E2E integration tests covering all subsystems (30 passed, 1 skipped in latest run)

### Changed
- Naming cleanup: "safety" → "response gate" / "content classifier" across codebase
- Setup wizard renumbered: 11 steps (was 10), now includes start + verify + doctor
- README updated to reflect hardened wizard flow — one `protagine init` and you're done

### Fixed
- 25+ API router bugfixes (method names, constructors, type coercion, sync/await mismatches)
- VectorStore wiring: explicit `connect(dimensions)` + `ensure_collections()` after graph init
- GoalStore persistence: `:memory:` → `protagine-goals.db`
- ResponseGate L1: passes when no session context (direct API calls)
- API key auth middleware added (was completely missing)

## 0.1.2 (2026-04-21)

### Changed
- Updated README and CONTRIBUTING to reflect `pip install protagine`
