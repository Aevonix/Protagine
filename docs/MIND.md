# The mind: drives, concerns, feelings, deliberation, goals and the closed loop

Protagine is the mind; Hermes is the body. The mind makes only tool-less
model calls through its own router (at most one per tick). Every effect on
the world is a Hermes kanban task on the `protagine-act` profile (idempotency
key `mind:<id>`) or a message the plugin sends verbatim. There is no second
executor.

This document describes what ships: the drives, the concerns (the
workspace), the agent's own feelings, deliberation, agent-owned goals, the
tick, authority, asks, the audit log, the outbox, outcomes and their
verification, lessons, skills and the off switch.
The design is in
[docs/proto-agi/PROTO-AGI-ARCHITECTURE.md](proto-agi/PROTO-AGI-ARCHITECTURE.md)
(sections 3, 4.3, 4.5, 4.8, 5 and 7).

## The loop

1. **Capture.** After every person-scoped turn the projection worker runs the
   `commitment_extract` task on the router and records durable commitments
   ("I'll send you the report by 3pm") and owed deliverables in the commitment
   store. It is on whenever a router is configured; duplicates of open items
   and of items rejected as invalid or duplicate are skipped in code, and an
   item withdrawn or dismissed as `obsolete` is shown to the model as closed
   (recorded again only on a clear fresh commitment). Deadlines resolve
   against the turn's own time, so a promise captured late (an outage, a
   backlog) that is already due is imported `overdue` with its original
   deadline, not dropped.
   The prompt lists the person's open items by number, with the person's
   previous turns of the last hour for context, and a later message that
   changes one is an action on it rather than a new row: `reschedule`
   (earlier or later; with no time it is a hold, the deadline is cleared and
   the item stays open), `complete` or `cancel` (resolved `done` or
   `obsolete` by `conversation`). A stall or a partial update changes
   nothing; an obligation between other people is not an item; an item
   conditioned on another event, or with no clear time, gets no deadline.
   An action must identify its row: the listed wording, and the listed
   deadline when two rows share it; an empty or merely similar wording is
   ignored, never guessed. The write is a compare-and-set against the
   description and deadline that were listed, so an extraction still
   thinking while the owner corrects the row never overwrites the
   correction; the job reruns once against the fresh state. A new item
   records its counterpart (`metadata.counterpart`, the contact as the
   conversation named them, `owner` for the owner), and a turn attributed to
   that contact lists the item next to their own, so "I need it earlier" or
   "got it, thanks" from the other side reaches it. It also records who owes
   the work (`metadata.obligor`: `owner` for the owner's own promise or
   reminder, `assistant` when the assistant took it on, or the other party
   who promised it); the duty drive reads that to tell a reminder from a
   task. An item whose obligor and counterpart are two different named
   third parties (neither is the owner, by any name the owner goes by, nor
   the assistant) is an obligation between other people and is not
   recorded; a deliverable or a message the assistant sends is never one.
   One person's capture jobs land in order: a later job waits for an
   earlier one still pending, running or backing off. The order never
   becomes a stall: a job backing off after a transport failure holds the
   person's later jobs for at most 15 s at a time, after which the worker
   retries it early and uncharged, so one failed call delays a person's
   captures by seconds; only its scheduled attempts (after 60 s, then 120 s)
   spend its three tries, and a job with nothing queued behind it keeps the
   full backoff.
   An unusable answer (cut off, not JSON) is retried at once, up to three
   times; only transport failures back off. When the person asks for a word
   before the deadline ("give me a heads-up at half three") the item keeps
   the deadline as `due_at` and carries `metadata.heads_up_at` (ISO) or
   `metadata.lead_minutes`; the heads-up is part of that item, never a second
   one. A moved deadline moves an absolute heads-up by the same delta (a hold
   drops it); a turn that states a new warning time replaces it.
   A message to a third party later or on a condition ("if Kim has not sent
   the draft by 3, ask her for it"; "if the venue is not confirmed by 5, tell
   them the booking lapses") is one item due at that time, obligor
   `assistant`, with `metadata.kind` `notice`
   (the owner's own words, sent verbatim) or `check_in` (a topic of at most
   six words, never a figure, amount or code, composed at send time), the
   `recipient` as named and `grant: owner`. It reaches someone the owner did
   not write to, so it exists only on the owner's own words asking for it:
   the extractor quotes them (`asked`, naming the recipient), code checks the
   quote is the owner's and names the recipient (or refers to one named in the
   recent conversation or a listed open item: "if they have not sent them,
   chase them yourself"), and the claim-review pass
   (`source_claims.review_proposals`) confirms against the whole message, with
   that conversation and those items to show whom the words mean, that the
   words ask the assistant itself to contact that person. Anything short
   of that ("tell me / let me know / flag it to me if it lapses", a recipient
   that is the owner, a refused or failed review) is the owner's own reminder
   (`kind: reminder`, obligor `owner`); the row keeps the quote and the
   review's decision, and the duty drive sends only a row that carries them
   (a grant stored before the review existed is the owner's reminder). Only the owner's own turn keeps the grant; the same
   shape from a contact is an ordinary item.
   A word the person asked for (a reminder, a nudge, a word if something has
   not happened) is `kind: reminder`: a message when due, never a task, on any
   lane. On the owner's turn such a word is another item's own only when it is
   the same word to the owner: that item's word at its deadline is a reminder
   to the owner (not a message to someone else, not the assistant's work), its
   deadline is still ahead, the word adds no matter of its own, and it falls at
   the deadline, before it where the item has no heads-up yet (it becomes the
   heads-up, compare-and-set on an open row of the owner's), or within 30
   minutes after it at no time the owner named ("if I go quiet past that,
   nudge me"). Anything else stays its own item, so a word the owner asked for
   is never dropped, moved or sent to someone else. A new item with a listed
   item's own wording and a new time moves it, compare-and-set; a merely
   similar one (the Q4 report beside the Q3 report) never does. A message the
   owner asked for is a duplicate only of a message to the same recipient,
   never of the promise it chases. The
   person's own words for the time are kept as `metadata.due_text` and shown
   beside the converted date in Pending Commitments. A message to
   pass on now ("tell Kim the meeting moved") is the reply's own job: nothing
   is recorded, and capture drops a third-party message due within three
   minutes of the turn, so the mind never sends a second copy of what the
   turn already sent. A deliverable goes only to the turn's own person: one
   the model records for a named third party is read as a notice to that
   party, so the words never go back to whoever asked.
   A recurring check-in the owner sets for a contact ("check on Kim every
   week about the kitchen quote") is one undated item, obligor `assistant`,
   with `metadata.kind` `cadence`, the `recipient` as named, a `topic` under
   the same six-word rule and `cadence_minutes`. Only the owner's own turn
   records one, and it never carries a grant: permission stays the contact's
   `may_contact`.
2. **Tick.** Every 60 s the sidecar runs the timers (ask expiry, deferred
   intentions, expectation resolution, retention, the nightly backup, and,
   with the mind on or off, the nightly vector compaction of
   [EMBEDDING-GENERATIONS.md](EMBEDDING-GENERATIONS.md)), then
   **drains the capture jobs still pending** (`CommitmentExtractor.drain`
   over the same ledger the projection worker uses: claimable jobs are run,
   a job the worker holds is waited for), bounded to 5 s on the timer and
   30 s on a forced tick (`POST /tick`, the CLI, the benchmark), so a promise
   made seconds ago is a row before the drives look; the tick summary's
   `capture_drained` records what landed and how long it waited. Alongside
   the drain it waits (never processes) for the owner's appraisal jobs in
   flight, up to 30 s on a forced tick and 2 s on the timer, and not for a
   queue nothing is working on (`appraisal_wait`): their outcomes reach the
   agent's feelings and their interests the curiosity drive in this tick, so
   the wait runs whatever the faculties. Then decay, the agent's feelings
   (`affect`), the drives, the
   concerns they raise, reconsideration, the goals and the one ranked producer
   below. An intention still waiting (deferred, asked or approved) whose
   source has resolved in the meantime
   (`invalidates_if`: the commitment fulfilled or cancelled, the awaited reply
   recorded), whose commitment row was removed, put on hold or moved back
   into the future by a later conversation, whose drive the owner set to
   weight 0, or whose goal is no longer open, is cancelled before it is
   approved, dispatched or sent (the body's pulls re-check too); that
   cancellation is the check's, not a dismissal. A commitment still open
   after such a cancellation gets its key back, so it is raised again when
   its new deadline passes.
3. **Drives.** Five pure functions of one snapshot of stored state
   (`P/mind/drives.py`), each returning its level and candidate concerns:

   | drive | rises with | satisfied by | produces |
   |---|---|---|---|
   | duty | overdue and due-soon commitments, due reply waits, stale owner kanban tasks and stalled Hermes goals (both from the body's board observations), duty-domain expectation misses | fulfilled commitments, done tasks | owner reminders and heads-ups (messages), follow-up tasks, owner notices |

   A commitment from a conversation (`source_type: cognition`) that the
   owner owes, or that someone else owes and the owner is tracking
   (`metadata.obligor` is `owner`, a contact id or a name; absent reads as
   the speaker of the turn), becomes a `commitment_reminder` **message to
   the owner** when it comes due (the description, the due time, how
   overdue it is), not a board task, on whichever lane it was captured: the
   reminder is the effect. The assistant's own promise
   (`metadata.obligor: assistant`, "I'll send you the report by 3pm"), to
   the owner or to a contact, and work the agent itself must do (a row
   created through the API or by another subsystem) keep the task form; an
   owed deliverable keeps its own message. A row whose obligor and
   counterpart are two different named third parties, neither of them the
   row's own person, raises nothing: it is someone else's obligation, not
   one to remind the owner of (capture no longer records such rows; this
   covers rows stored before it stopped, or by another writer).
   When the row carries a heads-up time and `heads_up_at <= now < due_at`,
   duty raises a `commitment_due_soon` message first; once that went out,
   the overdue reminder for the same row waits `mind.heads_up_grace_minutes`
   (30), one word at a time. A heads-up still unsent when the deadline
   passes is cancelled and the reminder takes over in the same tick. The
   dedup keys carry the deadline (`commitment:<id>:overdue:<deadline>`,
   `commitment:<id>:heads_up:<deadline>`): the same deadline is reported
   once, and a deadline moved after the word went out ("remind me again
   tomorrow") earns one new reminder and one new heads-up. A message that
   expires unsent (no handle ever resolved, the body never pulled) never
   reported the obligation: while the row is still open its key is freed
   and the reminder forms again at the next tick.
   | curiosity | seeded and declared interests (`protagine mind interest`, `identity.yaml` `agent.interests`, the owner's own `interest` appraisals), open questions, knowledge-domain expectation misses | a research task whose finding is stored | research tasks and goals |
   | mastery | the same signature failing twice in 7 days, repeated owner corrections | a later verified success | investigations and goals |
   | upkeep | failing health checks (3 strikes), backlogs, pending name-only identity links | health OK | notices, upkeep tasks, one owner ask per link |
   | social | contacts with an owner-set cadence or tier `regular` or above, overdue against it; `unknown` and group-only contacts weigh 0 | a reply or a conversation | check-ins |

   **People** (`mind.faculties.people`). The social drive asks
   `evaluate_outreach` (`P/contacts/comms.py`) when a check-in is due: one
   cadence after the later of the last conversation (or first contact) and
   the last message sent; a tier-only contact's cadence is the mean gap
   between conversations (a week before two, never under a day). Silence
   backs off: after a send the cooldown is `cadence x 2^streak`, capped at
   four cadences, where the streak counts sent check-ins the contact has not
   talked since. The sends are read from the comms ledger, where every
   message the mind sent a contact is logged (`external_ref`
   `mind:<type>:<intention id>`): they outlive the 90-day intention
   retention and follow the person through a merge. Declining contact
   affect holds outreach. The cooldown
   replaces the flat per-contact cooldown for that message only. A due
   check-in is owed (satiation does not hold it) and exists only while it is
   due: one the contact answered first is cancelled. A sent check-in is
   scored when its window (the cadence, else 24 h) passes: `actioned` if the
   contact talked after it, `ignored` if not, on `reach_out:<contact>` only
   (one silent contact never lowers check-ins with everyone); a later reply
   turns the newest ignored check-in `actioned`, and an ask for a check-in
   that expired teaches nothing (the owner's silence is not the contact's).
   The multiplier orders due check-ins and never gates one: eligibility uses
   the score without feedback, so the backoff above is the only brake.
   A message to a contact with no text is composed (`P/mind/compose.py`,
   task `mind_compose`, one tool-less call, no fallback, 300 tokens) when
   the budgets would let it go now (one they defer is composed when it goes),
   from
   an enumerated purpose (`check_in`, `follow_up:<id>`, `reply_wait:<id>`),
   the contact's name, the topic and
   that contact's own recipient-scoped packet, never the concern, its
   evidence or an owner turn; the text then passes the floor and the deny
   list. The owner's granted message to a third party is a
   `commitment_notice` or `commitment_check_in` at its time; the grant counts
   as `may_contact: auto` for that recipient only, never over `never` (the
   owner hears `grant_refused`), and only for a recipient the owner named
   exactly (an id, a handle a contact the owner filed holds, a number or an
   email; a shadow's username is the sender's own choice and never exact):
   one the store matched by name is an owner ask showing the name given and
   the contact's name and handle, whatever would otherwise have let it act or
   wait (the rule is stored on the row and applied again to a deferred one),
   and a notice whose words are not in the owner's turn is stored as a
   check-in around its matter. A name the contact store cannot resolve
   becomes a `recipient_unknown` ask; a sent one settles its commitment. An
   owner's `cadence` item for an exactly named contact sets its cadence once
   (audited as `cadence_set` by `owner-turn:commitment:<id>`; a cadence the
   owner later sets by hand stands), and while it stays open every check-in
   to that contact carries its topic; one matched by name only is a
   `cadence_confirm` owner question (a yes sets it, a no withdraws the item),
   and one naming someone unknown a `recipient_unknown` ask.
   Permission is read again whenever a message to a contact may leave: at
   every tick, at every outbox pull and on the owner's `yes`. A contact now
   `never` (an opt-out or the owner's revocation while the message waited
   for quiet hours or the body) cancels it; one lowered to `ask` turns a
   message approved on `auto` into the owner's question.
   Daily, each contact talked with in the last 24 h gets a template digest
   (`P/contacts/digest.py`). With the faculty off (the `full-people`
   ablation) what M5 adds goes and nothing older: the social weight is 0,
   nothing is composed (a message keeps its template) or digested, no link
   ask is raised, an owner's message to a third party is the owner's own
   reminder when due (never a worker's task) and a cadence stays undated, the "About this person"
   section is left out, and `/v1/mind/people` refuses `merge`, `link` and
   `cadence` with 409 `people_off` while the plugin's `protagine_people`
   offers only `who`, `inspect` and `set_permission`. `may_contact`,
   opt-outs and shadow contacts still apply.

   Weights come from `mind.drives`; 0 turns a drive off. A `done` outcome
   satiates its drive (a decaying `satiety.<drive>` level halves the
   effective weight), and a resolved concern is not raised again for a week.
   Open-ended work (research, investigations) re-arms once a week: its key
   carries the ISO week.
4. **Concerns.** `mind.db` holds the `concerns` and `mind_state` tables
   (`P/mind/concerns.py`). A repeat bumps the existing concern by
   `dedup_key`; salience decays with a 12 h half-life; the workspace holds at
   most 24 open concerns; each concern has a thought budget; after an outcome
   that did not resolve it, salience is scaled by 0.9 (progress) or 0.6
   (none). An open goal's concern never decays below the broadcast set. The
   top 3 open concerns are the **broadcast set**: the only deliberation
   candidates, rendered in the owner's Mind section and added to the recall
   query.
5. **Reconsideration.** An active intention is reconsidered only when its
   concern is raised again with new evidence (its context is refreshed) or
   its `invalidates_if` condition holds (it is cancelled). A dispatched
   intention is a commitment Hermes is running; the outcome speaks.
6. **Rank.** The top concerns score `salience x drive weight x feedback
   multiplier x affect x (1 - cost)` (affect: Feelings, below). The
   configured weight orders and is factored out of the threshold, so a
   low-weight drive still acts when nothing outranks it,
   while satiation lowers the effective weight and can hold back the agent's
   self-chosen recurring work (obligations and notices are only ordered); the
   multiplier is the `TypeFeedbackStore` value for `(type, drive)`, so a
   dismissed type drops below `mind.act_threshold` on its own. Each intention
   contributes to a key once: the owner's rating replaces the intention's
   implicit verdict rather than adding to it, and the same verdict again
   changes nothing.
7. **Deliberate.** Templates cover commitments, reply waits, stale tasks and
   health. Open-ended concerns get **at most one tool-less router call per
   tick** (`P/mind/deliberate.py`): the model returns a task (title, body, a
   `result_field` check) or a goal proposal, and on a topic that keeps
   failing one question for the owner (`ask`, offered in the schema and the
   prompt only then, so every other call is the same in every arm). The call
   has no tools, so an answer from the model's own recollection is never
   stored as something learned: anything else it returns gets the template.
   The prompt gives the worker's budget for the task's kind (one run of N
   minutes that cannot be continued) and asks for one bounded first
   deliverable; work that needs more runs is listed in `steps` and becomes a
   goal of at most `budgets.goal_tasks` steps, its plan in the goal's
   description, or, with no room for a goal, only its first run as one task.
   Without a router the
   template applies; with the tick's call spent, the concern waits.
8. **Goals.** Curiosity and mastery may adopt an agent-owned goal
   (`P/mind/goals.py`): one `kind='goal'` intention row with a description, a
   `success_check` the mind evaluates over its own state (`steps_done`,
   `result_field`, `commitment_resolved`), a task budget and a horizon. At
   most `budgets.open_goals` are open; a third proposal becomes one task.
   Each tick raises the next step as a task with kanban `goal_mode`; the goal
   closes when its check passes (satisfied), its budget is spent or its
   horizon passes (expired). The owner sees open goals in `protagine mind
   goals`, the Mind section and the digest.
9. **Decide.** `authority.decide` returns `act`, `ask`, `drop` or `defer` from
   the level x class table, the floor, the deny list, the budgets and the
   breaker. A strategy-switch question (Feelings) turns an `act` into an
   `ask` and never turns anything into an `act`. The intention is one row of
   the initiatives table, which is also the audit log.
10. **Dispatch.** The body pulls `GET /v1/mind/dispatch`, creates the kanban
    task with `mind:<id>` as its idempotency key (`goal_mode` for a goal step)
    and posts `POST /v1/mind/dispatch/{id}/bound {hermes_ref}`. Messages wait
    in `GET /v1/mind/outbox` and go through `sending` and `sent`. Nothing is
    offered twice; a message left in `sending` at a restart is `uncertain`
    and is never resent.
11. **Outcome.** The body reconciles every `mind:*` task from its run row and
    posts `POST /v1/mind/outcome {id, hermes_ref, status, summary}`. The mind
    records the outcome and its verifier (Verification, below), runs the
    intention's `success_check`, resolves the
    expectation it registered, records implicit feedback, checks the
    breaker (a run stopped at its runtime limit fails the task but counts
    toward the breaker only when a retry timed out as well with nothing new
    to show; `result_metadata.breaker` says which), settles the concern and
    satiates the drive, closes the
    commitment a done `commitment_*` intention was raised for (the worker
    cannot mark it fulfilled itself: the guard blocks
    `protagine_resolve_commitment(fulfilled)` in mind runs, so the row's
    resolution names the body and the intention), and writes an
    autobiography entry: an owner-audience ledger row with `origin='mind'`
    that ordinary recall finds in any later session. A finished research
    task, investigation or goal step also stores its finding ("What I
    learned about X: ...") the same way, so a later turn uses it.

## Feelings

The mind keeps its own affect (architecture 4.3): four levels in `mind.db`
(`mind_state`), each capped at 0.7, decaying with a half-life and citing up to
five causes. Nothing about it calls a model.

| Level | Key | Half-life | Moves with |
|---|---|---|---|
| frustration, per topic | `affect.frustration:<topic>` | 24 h | +0.3 for a failed or blocked task on the topic or an owner-reported failure; +0.2 for a correction (verdict `wrong` or `not_useful`, a reported correction, an annoyance appraisal); +0.1 / +0.2 for a low / moderate frustration appraisal; halved by a success on the topic (a check-verified result, a `useful` verdict, a reported success, a repair receipt), once until the next failure, so two reports of one success halve it once |
| worry | `affect.worry` | 6 h | +0.2 for a duty-domain expectation miss; +0.1 per tick for each owed obligation due within 24 h and not started (this rule never lifts worry above 0.3) |
| curiosity | `affect.curiosity` | 12 h | +0.2 for a knowledge-domain miss; +0.1 / +0.2 for an interest appraisal; +0.1 for an owner turn about a topic (three or more content words, so small talk is none) memory knew nothing about (this rule never lifts curiosity above 0.3) |
| satisfaction | `affect.satisfaction` | 12 h | +0.3 for a check-verified success or a `useful` verdict; +0.1 / +0.2 for a satisfaction appraisal |

Recent dismissals (an owner-reported "not now", a `dismissed` or `ignored`
verdict on owner-facing work) are a fifth decaying level, `affect.dismissed`
(+0.25, 24 h): the satiation input, never rendered as a mood. Nothing that
expired is a dismissal: an ask the owner let lapse (a task to approve, a
check-in or link to confirm, a contradiction question) is silence, not a
wave-off. The load is computed each tick, not stored: `min(1, 0.3 x
running/cap + 0.2 x near owed obligations + 0.1 x failures in the last hour +
0.1 x open asks about work)`. Only asks of kind task or goal count; the owner
questions the people and memory faculties raise (check-ins and links to
confirm, contradiction questions) are the mind's reporting, and counting them
would let the asks about check-ins postpone the check-ins themselves.

Every tick the mind reads one snapshot of stored records: the owner's reported
outcomes and appraisal records of the last 7 days (the owner subject only:
contacts' turns never move the agent's affect), intention rows, expectation
misses of the last day, and commitments owed by the owner or the assistant at
priority 50 or more that are due within 48 h (or undated and made in the last
day). Each event is applied once (its reference is kept in `affect.applied`
until it leaves the 7-day window, so a source that fails to read for a tick
is never applied again) as if at its own time and then decayed; a task
reported blocked and then failed is one failure; an outcome and an appraisal
record of the same turn on the same topic count once, while every reported
occurrence counts; evidence that is erased takes its frustration row, topic
text included, with it. Two topics are the same when their words (in any
script) overlap by 0.6 of the shorter and by two words unless both are that
short, so one shared word ("the export") does not spread a frustration to
other work. The owner's statements arrive through the appraisal call
the projection worker already makes for every turn: its `outcomes` list
(failed, succeeded, dismissed or corrected, with the topic and the approach
used; [SOCIAL-STATE.md](SOCIAL-STATE.md)) is stored per owner turn in the
ledger's `appraisal_outcomes` table, so "the export failed twice" is two
failures and a restatement adds none. The tick's wait for the owner's
appraisal jobs (Tick, above) lets a statement made just before a forced tick
count in it.

Four consumers read the feeling. Affect only ever holds or lowers
discretionary work (recurring self-chosen work, curiosity and social outreach,
anything below priority 0.5); it never holds back an owed obligation or
raises its bar, and never raises authority. The one change it makes to a
decision is the strategy switch's question, which turns an `act` into an
`ask` for the owner.

1. **Strategy switch.** A topic at frustration 0.5 or more with at least two
   failed attempts since its last success (a success followed by one miss,
   or corrections alone, never switch) puts "Prior attempts at T failed N
   times using A; choose a different approach or ask one question." into the
   task body and the owner's Mind section;
   the deliberation prompt also names the failures' reasons as pitfalls and
   the approaches to avoid, and the model proposes another approach or
   returns kind `ask` (one question for the owner; the step is asked with a
   runnable body). A plan identical to one that already failed on a topic
   with two failed attempts since its last success is asked, never
   dispatched again without the owner: that reads the failure record, not
   the decaying level, so it holds until a success on the topic or the end
   of the source's window (7 days for the state, 24 h for the rule). A task
   formed before the failures gets the note when the body pulls it.
2. **Overload** (load 0.6 or more). Curiosity and social work and optional
   messages wait; replies stay brief. The step of an adopted goal is owed
   whatever its drive and never waits.
3. **Priority.** Owed duty scores x (1 + 0.5 x worry); curiosity work x (1 +
   curiosity).
4. **Satiation.** Satisfaction of 0.5 or dismissals of 0.4 or more raise the
   act threshold of optional owner-facing messages by x (1 + the level).

The **tone** is one calm line from the state alone, in three bands (under 0.2
"a little", under 0.45 "somewhat", otherwise "quite"), for example "Mood:
somewhat frustrated about the quarterly figures; a little uneasy." No stronger
word is ever used. It is self-report only: it never rides in the decision
context (the Mind section).

**Switches.** `mind.faculties.affect` (on): the state is kept and every
consumer reads it. `mind.faculties.affect_rules` (off): every consumer reads
its frozen stateless rule (`P/mind/affect_rules.py`) over the same snapshot
instead (two failures on a topic within 24 h of its last success; three near
obligations or a full worker pool; worry 0.5 while anything owed is due soon;
two dismissals in 7 days hold optional nudges), and the rules replace the
state: nothing is kept and no tone renders, whatever `affect` says, so the
two switches make three modes (off, the state, the rules) and no unmeasured
mix. With both off, affect reads and writes nothing. Which consumers read
their rule after the affect family's gate is a code constant
(`affect_rules.RULE_CONSUMERS`, with the state kept for self-report and
tone), never a setting, and neither switch has an environment variable. It is
empty until the held-out gate has run: the affect dev pilot (2026-09-24)
favoured the rules on overload and priority but put the gap down to iteration
caps on identical prompts, and a partial set would make an unmeasured mix the
live default.

**Self-report.** `protagine_self state`, `GET /v1/mind/state` (`affect`) and
`protagine mind status` show each level with its cited causes (`failed
outcome:<id> via <approach>`, `due_soon commitment:<id>`, `miss
expectation:<id>`), the load, which source each consumer reads (`source`:
`state`, `rules`, `mixed` or `off`), the notes and the tone line.

## The Mind section

An owner turn's `/context/assemble` carries a `protagine-mind` section of at
most 600 characters: the affect notes (a strategy switch, what is due soon and
not started, "Stretched", "Holding back optional nudges"), then "On my mind"
(the broadcast set, less idle curiosity, an interest's research: in a decision it
points at the optional work the consumers postpone), "Working toward" (open goals) and
"Waiting for your say on" (open asks with their codes). Affect's lines come
first but take only the room the others leave, at most 360 characters, and
drop whole lines from the end to fit, so they never cut the open asks. Guests never see it. With `faculties.broadcast` off the concerns are neither shown
nor added to the recall query. A relevant lesson rides in its own
`protagine-lessons` section (Lessons, below), and recorded views in
`protagine-stances`.

## Identity: the constitution, the narrative and `protagine_self`

Three layers (architecture 4.2). The **constitution** is `identity.yaml`
`agent.{name, values, boundaries}`, owner-authored and written only by
`protagine init` (`--agent-values`, `--agent-boundaries`; at most 12 items of
160 characters each). It renders as one paragraph of at most 1,500 characters
(`You are Agent. Your values: care; candour. Your boundaries: never send
money.`); `init` refuses a longer one and says which list to shorten, and
`protagine doctor` reports the rendered length. The plugin reads the file
itself and renders the constitution into its `protagine` prompt section, which
Hermes freezes per session, together with `Your owner is <name>.`, the
**self-narrative** (`GET /v1/mind/narrative`: at most 800 characters, four
computed or cited sections, every line ending in the ids it rests on: plain ids
are the agent's own actions, which `protagine_self why` explains, and prefixed
ids (`interest:`, `judgment:`, `turn:`, `claim:`) are record references;
fetched with a 2 s timeout for every new session, a failed fetch not retried
for 60 s, and left out when the sidecar or the `self_narrative` faculty is off)
and two tool notes, 4,000 characters in all. Every model request of an owner
session carries the section: at its largest (a 1,500-character constitution,
an 800-character narrative) it is 3,185 characters with the memory provider's
block (1,360 GLM-5.3-Flash tokens), a typical install with a history about
1,745 (560 tokens), against 726 (148) for the benchmark's disposable identity
and fresh store (`tests/hermes_adapter/test_overhead_budget.py`).
The narrative is the owner's record (what the agent did for the owner, its
working stances), so it is rendered only in a session that is the owner's
alone: a direct chat from one of the owner's handles, or an internal lane with
no chat (the CLI, the benchmark). Hermes renders the section before the
session's first hook, so the plugin reads the sender the gateway bound for the
turn, as the memory provider does. A guest, an unresolved sender, a group or
channel the owner shares, and a chat with no sender get the constitution and
the notes, and the sidecar is not asked for the narrative on their behalf.
The same constitution reaches every appraisal prompt as `agent_constitution`
(an input the response schema has no field for), so a contact's preferences
are never confused with the agent's own. `PROTAGINE_AGENT_VALUES` is reserved
and read by nothing: `init` moves an old unit's values into the file.

The mind cannot rewrite its constitution. What guarantees it is the worker's
tool surface: the default `mind.worker_toolsets` have no shell or code tool
(a test holds this), and `write_file` and `patch` are confined to the task
workspace. On top of that, in a mind run the plugin guard blocks every
effectful tool that names `protagine.yaml`, `identity.yaml` or `api.key` (a
write target, a V4A patch header, a shell command, code), in any case and
through the quotes, backslashes, whitespace, backticks or `+` a literal name
can be split with, before the workspace rule; reads stay allowed, and Hermes'
own `protected_instruction_extra_patterns` (written by `init`) still asks a
human for a write to any of them. A text rule cannot follow every spelling a
shell or code can build (a glob, an escape, a computed string), so an owner
who adds a `terminal` or `code_execution` toolset to the worker takes the
constitution's protection into their own hands. No module under `mind/`, `self_model/`,
`beliefs/`, `memory/` or `commitments/` writes either file; the owner's CLI
and the mind's `persist` hook write `mind.enabled` and `mind.autonomy` only
(a static test holds this).

**`protagine_self`** is the only source for claims about the agent's own
actions, and the record is the owner's: `state`, `log` and `why` answer in full
only in the owner's own session (a direct chat from an owner handle, or the
CLI); a guest, a group the owner shares and a mind worker get the switch state
(`enabled`, `autonomy`, `sidecar_reachable`) and a refusal for `log` and `why`.
`state` (level, budgets, open asks with codes, `working_on`: the approved and
dispatched tasks with their ids, and the narrative text),
`log` (`limit`, `since_hours`, `kind`, `recipient`: "did I message p-07
yesterday?" is one call, and an action that is not in the log did not happen),
`why <id>` (an unknown id answers "no intention `<id>` exists in the audit
log"), `rate <id> <verdict>`, `yes|no <code>`. `rate`, `yes` and `no` stay
owner-only. The agent's opinions ride the same tool (integration map X8):
`opinions [query]` and `why <opinion number>` answer in any session, the
sidecar filtering them to the views meant for that session's participant, and
`withdraw|reconsider <number>` with the owner's reason are owner-only.

## Opinions

With `faculties.opinions` on, the mind keeps the agent's opinions (docs/OPINIONS.md).
Three failed attempts in a row at the same work (the failure signature `type:topic`; a
done report whose success check failed is a failure) become an `avoid` approach opinion
with no model call, and a success a check confirmed or the owner verified turns it into
`prefer`; the next task at that work carries the view in its body
("Your recorded view on this work [opinion N]: ...", with `context.opinion_ids`). A view
flags work, it never holds it back: those three failures also trip the breaker, so the
next attempt is usually an ask, and once the owner says yes it is dispatched with the
view in its body. Turn context gets a `protagine-stances` section of at most three
relevant views for any viewer, filtered by audience, with the standing rule that a view
changes only on new evidence and that the agent may disagree and still do what the owner
authorizes, saying so. `protagine mind opinions` and `/v1/mind/opinions` list and show
them, and let the owner withdraw or reconsider one.

The running mind reads the flag once at start, and the opinion pass in the projection
worker asks the running mind rather than the file, so the pass, the section and task
bodies never disagree; `mind.enabled` counts as configured, not the runtime off switch
(forming views is memory, not an effect). Only a process that serves no mind reads
`protagine.yaml` for it.

## Verification

Every settled intention records `verified` beside its `outcome`: which verifier
stands behind it (architecture 4.8). `owner` is the owner's verdict or
confirmation, `check` a `success_check` that actually ran, `hermes_failure` a
task Hermes reported failed or blocked with a reason, and `none` anything else,
a worker's completion summary included. Only the mind grants `owner` and
`check`: a body report (`POST /outcome`) may claim only `hermes_failure`, and
only for a failure that carries a reason (`error` or `summary`); a claimed
`owner` or `check` is ignored and the verifier computed. A failure without a
reason is not a Hermes failure. A `blocked` report with a reason records
`verified: hermes_failure` and the reason on the still-open row; a later
report settles it and recomputes. For lessons a `check` counts only when its
kind reads state the worker cannot write (`commitment_resolved`,
`reply_recorded`): a `result_field` check passes on the worker's own summary,
so it verifies no lesson (the other faculties read `verified` as before).

## Lessons

With `faculties.lessons` on (the default) the mind learns short, transferable
procedures from verified results (`P/mind/lessons.py`). A lesson is a
`strategy` (what to do and when, with its exceptions) or a `pitfall` (what to
avoid), with a title, when it applies, its content and its evidence. It is the
mind's own record, not a memory of the owner's: owner-audience ledger entries
in the mind's session with `scope='session'`, one per event
(`mind:lesson:<id>:admitted`, then `activated`, `superseded` or `retired`), so
ordinary recall never shows a lesson as something the owner said, and each
entry's lineage (the owner turns it quotes, the outcome entries it cites)
erases it together with its evidence.

- **Admission, at night.** The night's lesson stage (docs/CONSOLIDATION.md)
  makes one tool-less call over the owner's own sessions since the last review
  (sessions with at least two owner messages: a verdict follows work; the
  agent's replies beside them) and the agent's tasks and goals of the last two
  weeks that a verifier stands behind, with the current lessons they bear on.
  The model returns `add`, `supersede` or `retire` operations and its
  verdicts: the owner messages that judge the agent's earlier work, right or
  wrong, each with a quote. Everything is validated before anything is
  written. A verdict counts only for an owner message that follows an agent
  reply in its session, and only a verdict is the `owner` verifier: an
  operation may cite an owner message only when the answer reports it as a
  verdict, and quotes the owner's exact words (at least 12 characters), so a
  request never verifies a lesson. Every citation must be in the packet; a
  strategy needs an owner verdict or an external check, while a Hermes failure
  with its reason teaches only a pitfall; a retirement needs an owner verdict
  that the work was wrong or a verified result that failed; a result nobody
  verified can never be cited; a contact's session is never read. At most six
  operations a night are applied. One current lesson per class and kind: an
  add that meets one supersedes it.
- **Use.** A task body and its deliberation carry at most two lessons, those of
  the task's own failure class first, then active lessons whose title and use
  share at least two terms and a third of their terms with the work; the
  intention records them in `lesson_ids`. An owner's own turn gets at most one
  active lesson in a `protagine-lessons` context section ("What you learned",
  at most 420 characters), which says where it came from and that the owner's
  word in the conversation comes first, and logs one `lesson_use` note for that
  owner message (named by a key of its words, not the words). A guest, a
  kanban worker's run (its body carries its own lessons), a recipient packet,
  the mind switched off or the faculty off gets none.
- **Scoring and retirement.** A use counts when a verifier scored it: a task
  that carried the lesson and was verified by the owner (`useful`, `actioned`
  or `wrong`, `not_useful`), by an external check, or failed with Hermes'
  reason; a turn use when the owner's next message in that session is a verdict
  on the reply the lesson helped write (a verdict inside the message the lesson
  answered judges earlier work, and one on another answer judges that answer,
  so neither scores it). Over 90 days, a lesson under a 0.4 win rate after five
  verified uses is retired, and a `candidate` becomes active after a verified
  win in its class; each change is an audit note.
- **Corrections.** An operation that carries the value the owner corrected is
  split deterministically: when an earlier owner message (not the correcting
  turn, never the agent's reply or a workspace file) already held the value,
  the lesson is a `retrieval` lesson that names that turn and tells the agent
  to look there first; otherwise it is `knowledge`.
- **The reflector.** A failure-class investigation of the mastery drive asks
  for at most three lesson operations as one JSON object at the end of its
  report. The mind validates them when the report comes back (supersede and
  retire only on a candidate of the investigated class, never on an active
  lesson) and admits what passes as `candidate` lessons of that class, tried
  only in task bodies of that class until a verified win activates them. The
  row's `result_metadata.lesson_ops` says what was applied and refused. Its
  body shows the class's current lessons, but it is no use of them: it records
  no `lesson_ids`, so its own outcome (an investigation that timed out, say)
  never scores them.

With the faculty off nothing new is admitted or used and investigations are
the plain ones; stored lessons are kept and come back when it is turned on.
`protagine mind lessons` lists, shows and retires them.

## Skills (off by default)

With `faculties.skills` on (and lessons on), an active lesson with at least
three verified wins at a win rate of at least 0.7 becomes
`<instance>/skills/protagine-<slug>/SKILL.md`, the directory `protagine init`
lists in Hermes' `skills.external_dirs` (`P/mind/skills.py`). Protagine owns
these files: a manifest lists what it wrote, a skill goes when its lesson is
retired, superseded, erased or no longer promotable, and with the flag off
every skill it wrote goes and nothing else is touched. The sync runs at the end
of the night's lesson stage and when the mind starts; every change bumps
`skills.generation` in `/v1/mind/state`, which the plugin reads to clear
Hermes' skills prompt cache, so the next session lists the skill without a
restart. Loads of Protagine's skills reach `POST /v1/mind/skills/used` and are
counted in `/v1/mind/stats` and `protagine mind lessons`. The flag stays off
until the skills arm beats lessons alone.

## Outreach to the owner

The social drive turns toward the owner (`P/mind/outreach.py`, architecture
4.10). It speaks up only when it has something concrete: a finding of its own
research that bears on what the owner said they care about (an interest they
declared with `protagine mind interest`, in conversation, or welcomed), the
owner's own open item after a day or so of quiet (not one due within 48 h:
reminders and heads-ups are duty's), or a named thing the owner said is
stressing them. There is no "anything you need?" message, and a report that
found nothing, or only what was already sent or listed, is never sent as a
finding.

Each candidate's value is `relevance × novelty × timeliness` and its cost the
interruption (recency of the last outreach, the owner's ignored streak, the
day's unprompted messages, a "not now" mark on the hour); the ranker decides.
Quiet hours, a pause, `budgets.outreach_per_day` (3; reminders the owner asked
for are counted apart), two hours after the last outreach, a muted topic and a
topic's backoff hold a candidate before it forms, and at most one unprompted
outreach goes out a tick. An offer of help on an open item waits for a quiet
stretch since the owner last spoke (or since the last such offer), so one quiet
stretch is one check-in. Every
message says why, quoting the owner ("You said \"I care a lot about tidal
energy\", so I looked into tidal energy: ... Say 'dig deeper' for more, or
'not interested' and I will drop it."). What was worth it but not sent is in
the next digest's "Found for you"; offers that lapsed or were put off are in
its "Offers".

The owner's replies steer it, read in the turn path (`P/mind/reactions.py`):
"dig deeper" rates it useful and starts a follow-up whose answer is sent when
it is ready, outside the daily budget; "not interested" mutes the topic; "not now" pauses for four
hours; "leave me alone today" until tomorrow; "stop checking in" until
"you can check in again" or `protagine mind outreach on`. A reply that does not
name the topic counts only when it plainly answers the outreach: the owner's
first turn after it, not mid-conversation, and not a request of its own ("can
you find out when the last train leaves?" is a request, not "dig deeper"). A
bare "stop" or "not today" means something about outreach only as such a reply. Reminders the owner
asked for never stop. Silence for a day counts as a weak "ignored". What is
learned is the verdict and its feedback, the topic's interest level, a mute,
the pause and the hour's mark, and at night the lessons over the rated
messages. With `faculties.outreach` off none of it runs.

## Asks

An ask lives only in the sidecar. Nothing is created in Hermes until the owner
approves, so no board action can approve one. Each ask gets a short code
(`K7F`); a notice lists open asks at most once every 4 hours, and the daily
digest lists them again. The owner answers with `protagine mind yes <code>`,
`protagine mind no <code>`, or in chat ("yes K7F", through `protagine_self`),
which the sidecar accepts only when the sender is the owner contact and the
code is in the owner's own message. Silence expires an ask after
`mind.ask_expires_hours` (72 h). Nothing else waits on an ask. At
`autonomy: suggest` an ordinary ask is digest-only: it gets no 4-hourly
notice; a floor ask is noticed at once at every level. Silence counts as the
owner's weak `ignored` only on an ask the owner was sent: a digest-only
suggestion that lapses, and a task the mind's own budget kept waiting past
its window, are no verdict. `suggest` holds the mind's initiative, not what
the owner asked to be told: a reminder or heads-up the owner asked for, the
question about a recipient the owner named that the store cannot resolve,
and the mind's own health and breaker notices go out at every level but
`off`. The learned multiplier never weighs a commitment someone made, and a
blocked task holds no `concurrent_tasks` slot. A name-only identity
link ("Is sam@example.org on email Sam?") is always an ask; the answer links
or rejects the handle in the contact store, and a `no` is not a verdict on
asking. A yes folds a shadow that held the handle into the contact, and
never an established contact: that link is refused and the owner merges the
two explicitly if they are one person.

## The off switch

`protagine mind off`, `/mind off` in chat, `POST /v1/mind/off` or
`mind.enabled: false` all mean "no further effects". At once, in the sidecar:
no deliberation, the dispatch queue and outbox return nothing, unsent messages
are cancelled and every guard check from a mind-originated run returns block.
At the next plugin tick the body archives unstarted `mind:*` tasks. Running
workers end at their runtime limit. The switch is a marker file
(`mind.off` in the instance directory) plus an in-memory flag, so it works
with the model endpoint down and survives a restart. `protagine mind on`
turns it back on. `autonomy: off` (`protagine mind level off`) has the same
effect on effects: nothing is dispatched or sent, unsent messages are
cancelled and mind-originated runs are blocked; what was approved waits for
the level to come back.

A standing "leave X alone" is the deny list (`mind.deny`: tool names, text
patterns and shell commands), enforced by the mind's authority on every
intention and mirrored into the worker profile's `approvals.deny`. A global
pause is the off switch plus stock `hermes pause`; the body holds dispatch
and sends while Hermes is paused.

## Configuration

```yaml
mind:
  enabled: true                     # the off switch
  autonomy: suggest                 # off | suggest | standard | trusted
  deny: {tools: [], text: [], commands: []}
  worker_toolsets: [web, file, session_search, memory, todo]
  # One worker run per task: task_types per task type, task_max_runtime_s / task_max_retries for
  # any other type and for a field a type leaves out.
  budgets: {tasks_per_hour: 4, concurrent_tasks: 2, owner_messages_per_day: 3,
            outreach_per_day: 3,    # unprompted outreach to the owner, counted apart from reminders
            contact_messages_per_day: 5, per_contact_cooldown_hours: 24,
            llm_tokens_per_day: 200000, open_goals: 2, goal_tasks: 4,
            goal_horizon_days: 7, task_max_runtime_s: 600, task_max_retries: 1,
            task_types: {research: {max_runtime_s: 1800, max_retries: 2},
                         question: {max_runtime_s: 1800, max_retries: 2},
                         mastery_investigation: {max_runtime_s: 1800, max_retries: 2},
                         goal_step: {max_runtime_s: 1800, max_retries: 2},
                         outreach_followup: {max_runtime_s: 1800, max_retries: 2}}}
  # extra_body on every request a mind task's worker makes; null leaves a field to the provider
  worker_request: {max_tokens: 8192, top_p: 0.95}
  quiet_hours: "22:00-07:00"        # owner notices wait; the digest and tasks do not
  ask_expires_hours: 72
  breaker: {failures: 3, window_hours: 24, demotion_hours: 72}
  act_threshold: 0.6                # the ranker's effective-score floor
  digest_hour: 8                    # local hour after which the daily digest goes out
  heads_up_grace_minutes: 30        # after a heads-up went out, the overdue reminder for that row waits this long
  drives: {duty: 1.0, social: 0.5, curiosity: 0.5, mastery: 1.0, upkeep: 1.0}   # 0 turns a drive off and cancels its waiting work
  faculties:                        # one binary flag each; each flag is one benchmark arm
    initiative: true
    drives: true                    # weights, satiation and goal adoption; off = flat priority
    deliberation: true              # the one tool-less call per tick; off = templates only
    goals: true                     # agent-owned goals
    affect: true                    # the agent's own feelings (Feelings, above)
    affect_rules: false             # the affect mechanism arm: every consumer reads its stateless rule
    broadcast: true                 # the top-3 concerns in turn context and recall
    semantic_recall: true           # embeddings in recall when router.embed_url is set; off = lexical only
    consolidation: true             # the nightly consolidation (docs/CONSOLIDATION.md)
    self_narrative: true            # the self-narrative in the owner's prompt and protagine_self state
    opinions: true                  # the opinion pass, approach views in task bodies, the stance section
    lessons: true                   # lessons: the night's lesson stage, lesson lines, the lesson section, the reflector
    skills: false                   # proven lessons as SKILL.md in Protagine's skills.external_dirs entry
    outreach: true                  # owner outreach: the social drive toward the owner (Outreach, above)
```

`identity.yaml` holds the constitution (`agent.name`, `agent.values`,
`agent.boundaries`) and may list `agent.interests`; each interest becomes a
seeded interest at startup. `faculties.semantic_recall` is a real switch:
with it off the embedder stays off (`PROTAGINE_EMBED_PROVIDER=skip`) even
when `router.embed_url` is set. Learning writes `mind.db`, the feedback
multipliers and the intention rows; nothing the mind learns writes
`protagine.yaml` or `identity.yaml`.

## The CLI

```
protagine mind status              enabled, level, queues, breaker, the affect line
protagine mind log [--limit N]     the audit log, newest first
protagine mind why <id>            drive, evidence, decision, Hermes ref, outcome, verification
protagine mind asks                open asks with their codes
protagine mind yes|no <code>       answer an ask
protagine mind rate <id> <verdict> actioned | dismissed | ignored | useful | not_useful | wrong
protagine mind level [<level>]     show or set the autonomy level
protagine mind reset <class>       reset a tripped breaker
protagine mind off | on            the off switch (off works with the sidecar down)
protagine mind tick                run one tick now
protagine mind stats               the in-vivo panel over the audit log
protagine mind concerns            what is on the mind: drive levels, open concerns, the broadcast set
protagine mind goals               the agent-owned goals that are open
protagine mind interest <topic>    seed an interest for the curiosity drive
protagine mind outreach [on|off]   owner outreach: its state, a pause until turned on, or on again
protagine mind consolidate         run the nightly consolidation now (docs/CONSOLIDATION.md)
protagine mind narrative           the self-narrative as the owner's prompt section renders it
protagine mind opinions [list|show <id>|withdraw <id>|reconsider <id>] [--query Q] [--history] [--reason R]
protagine mind lessons [list|show <id>|retire <id>] [--all] [--reason R]
```

## The API (`/v1/mind`, one bearer key)

| Route | Body | Returns |
|---|---|---|
| `GET /dispatch` | | a JSON list of approved task intentions: `id`, `kind`, `type`, `drive`, `dedup_key` and `idempotency_key` (`mind:<id>`), `title`, `body`, `assignee` (`protagine-act`), `recipient`, `reason`, `max_runtime_seconds`, `max_retries`, `goal_mode`, `goal_max_turns`, `parent_goal_id`, `created_at`, `expires_at` |
| `POST /dispatch/{id}/bound` | `{hermes_ref, hermes_kind?, status?, bound_at?}` (idempotent: the same ref may arrive again) | the audit entry |
| `POST /outcome` | `{id, hermes_ref, hermes_kind?, status, outcome?, final?, summary?, error?, verified?, run?, block_kind?, consecutive_failures?, completed_at?, observed_at?}`: `status` is the kanban status, `outcome` (`done`, `blocked`, `failed`, `cancelled`, `uncertain`) the body's reading of it; `final: false` (a requeued failed run) is logged, not settled | the audit entry; 404 for an unknown intention |
| `GET /outbox` | | a JSON list of messages: `id`, `kind` (`notice` for the owner, `message` otherwise), `type`, `dedup_key`, `recipient`, `recipient_is_owner`, `recipient_handles` (`{gateway, address, is_primary, verified}`, read afresh at every pull), `text`, `title`, `created_at`, `expires_at` |
| `POST /outbox/{id}/sending` | `{target?, at?}` | the audit entry; 409 unless the message was ready and the claim names a target (`no_target`: the body resolved no handle, the message stays ready and is offered again; a claim nobody settles is `uncertain` after 10 minutes) |
| `POST /outbox/{id}/sent` | `{result: sent | failed | uncertain, error?, hermes_ref?, summary?, at?}` | the audit entry |
| `POST /observations` | the body's board: `{observed_at, board, body, counts, stale_tasks, blocked_tasks, goals, mind_tasks}` with `idle_s` per task (docs/HERMES-ADAPTER.md), or the flat `{observations: [{kind, id, title, assignee, status, age_hours}]}`; stale owner tasks and goals are duty inputs | `{accepted, kinds}` |
| `POST /guard` | `{tool, args, session | session_id, run, task_id, recipients?, ...}`: a messaging tool's recipient is read from `args` (`contact_id`, `platform` + `target|chat_id|to`, or stock `target="platform:chat_id[:thread_id]"`); `recipients` are the contact ids an effect reaches later (a delivering cron job), each authorized with `may_contact` and the message budgets | `{allow, action: allow | block | ask, reason}` |
| `POST /decide` | `{code, answer: yes | no, contact_id?, session_id?, message?}` (the plugin's `protagine_self yes|no`) | `{ok, id, status, ...}`; 404 no open ask, 403 not the owner |
| `GET /log` (`limit`, `status`, `kind`, `since_hours`, `recipient`), `GET /why/{id}` (404 `unknown_intention`: "no intention `<id>` exists in the audit log"), `GET /log/{id}`, `GET /asks`, `GET /state` (`/status`; with `faculties`, `drives`, `concerns`, `goals`, `interests`, `deliberation`, `affect`, `outreach {enabled, paused_until, per_day, sent_24h, muted, care}`, `lessons {enabled, active, candidate}` and `skills {enabled, generation, owned}`), `GET /stats` (with `lessons` and `lesson_use_rate`, the wins over verified uses, and `skills`) | | |
| `GET /lessons?status=&uses=&viewer=` | | `{enabled, lessons, uses, skills, text}`: each lesson with its verified tally, with `uses=true` every use in the 90-day window, and the skills Protagine keeps with their loads; a guest viewer gets nothing |
| `POST /lessons/{id}/retire` | `{reason, by?}` | the retired lesson; 404 unknown, 409 already closed |
| `POST /skills/used` | `{skill, session_id?, task_id?}` (the plugin's `on_skill_lifecycle` forwarding) | `{ok, counted, loads}`; only `protagine-*` skills are counted |
| `GET /narrative` | | the self-narrative `{enabled, text, sections: {interests, strengths, recent, stances}, cites, updated_at}`; `enabled: false` and empty text until the mind keeps one or while `faculties.self_narrative` is off |
| `POST /consolidate` | | runs the nightly consolidation now and returns the night's record (`protagine mind consolidate`); 501 `consolidation_not_available` on a sidecar without it |
| `GET /concerns`, `GET /goals` | | the workspace (open concerns, the broadcast set, drive levels) and the open goals |
| `POST /interests` | `{topic, why?}` | a seeded interest the curiosity drive researches |
| `POST /outreach` | `{state: on \| off}` | owner outreach's state `{enabled, paused_until, per_day, sent_24h, muted, care}` after the switch; off pauses unprompted outreach until turned on; `{enabled: false}` with the faculty off; 422 for another state |
| `POST /asks/{code}/yes`, `POST /asks/{code}/no` | `{contact_id?, message?, by?}` | the audit entry |
| `POST /off {reason?}`, `POST /on`, `POST /tick`, `POST /rate {id, verdict}`, `POST /level {autonomy}`, `POST /reset {cls}` | | |
| `GET /opinions?q=&contact_id=&by=&history=&limit=`, `GET /opinions/{id}`, `POST /opinions/{id}/withdraw`, `POST /opinions/{id}/reconsider` | `{reason, contact_id?, by?, correction_id?}` for the two controls | `{enabled, opinions}`, `{opinion, history}`, `{revision_id, status}`; audience-filtered, owner-only controls (docs/OPINIONS.md) |

`GET /dispatch` and `GET /outbox` also record the body's last pull; when it is
older than five minutes the tick stops forming intentions until the body is
back (`POST /tick` forces one).

Test seams: `PROTAGINE_MIND_CLOCK_OFFSET_SECONDS` shifts the mind's clock
(the 72 h ask expiry is checked by restarting the sidecar three days ahead,
`scripts/ci_mind_loop.sh`), and the paired benchmark passes its own shifted
clock in (`protagine.qualification.native_memory_worker.serve_mind`), builds
its Mind with the sidecar's own factory (`protagine.mind.factory.build_mind`:
the same `CommitmentExtractor`, so a forced tick drains capture first, and the
same people, appraisal and router wiring) and mounts the same mind routes
(`mind_routers()`), and writes an `identity.yaml` whose owner handle is
the capture platform's home channel, so an owner reminder is a delivery the
grader attributes to the mind rather than a board task. The drives family
runs the `full`, `full-drives`, `full-broadcast` and per-drive arms
(docs/PAIRED-AGENT-BENCHMARK.md).
