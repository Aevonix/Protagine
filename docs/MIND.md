# The mind: drives, concerns, deliberation, goals and the closed loop

Protagine is the mind; Hermes is the body. The mind makes only tool-less
model calls through its own router (at most one per tick). Every effect on
the world is a Hermes kanban task on the `protagine-act` profile (idempotency
key `mind:<id>`) or a message the plugin sends verbatim. There is no second
executor.

This document describes what ships: the drives, the concerns (the
workspace), deliberation, agent-owned goals, the tick, authority, asks, the
audit log, the outbox, outcomes and the off switch. The design is in
[docs/proto-agi/PROTO-AGI-ARCHITECTURE.md](proto-agi/PROTO-AGI-ARCHITECTURE.md)
(sections 3, 4.5, 5 and 7).

## The loop

1. **Capture.** After every person-scoped turn the projection worker runs the
   `commitment_extract` task on the router and records durable commitments
   ("I'll send you the report by 3pm") and owed deliverables in the commitment
   store. It is on whenever a router is configured; duplicates of open or
   rejected items are skipped in code. Deadlines resolve against the turn's
   own time, so a promise captured late (an outage, a backlog) that is
   already due is imported `overdue` with its original deadline, not dropped.
2. **Tick.** Every 60 s the sidecar runs the timers (ask expiry, deferred
   intentions, expectation resolution, retention, the nightly backup), then
   decay, the drives, the concerns they raise, reconsideration, the goals and
   the one ranked producer below. An intention still waiting (deferred,
   asked or approved) whose source has resolved in the meantime
   (`invalidates_if`: the commitment fulfilled or cancelled, the awaited reply
   recorded), whose drive the owner set to weight 0, or whose goal is no
   longer open, is cancelled before it is approved, dispatched or sent; that
   cancellation is the check's, not a dismissal.
3. **Drives.** Five pure functions of one snapshot of stored state
   (`P/mind/drives.py`), each returning its level and candidate concerns:

   | drive | rises with | satisfied by | produces |
   |---|---|---|---|
   | duty | overdue commitments, due reply waits, stale owner kanban tasks and stalled Hermes goals (both from the body's board observations), duty-domain expectation misses | fulfilled commitments, done tasks | follow-up tasks, owner notices |
   | curiosity | seeded and declared interests (`protagine mind interest`, `identity.yaml` `agent.interests`, the owner's own `interest` appraisals), open questions, knowledge-domain expectation misses | a research task whose finding is stored | research tasks and goals |
   | mastery | the same signature failing twice in 7 days, repeated owner corrections | a later verified success | investigations and goals |
   | upkeep | failing health checks (3 strikes), backlogs | health OK | notices and upkeep tasks |
   | social | (the people milestone) | a reply or a conversation | nothing yet |

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
   multiplier x (1 - cost)`. The configured weight orders and is factored out
   of the threshold, so a low-weight drive still acts when nothing outranks it,
   while satiation lowers the effective weight and can hold back the agent's
   self-chosen recurring work (obligations and notices are only ordered); the
   multiplier is the `TypeFeedbackStore` value for `(type, drive)`, so a
   dismissed type drops below `mind.act_threshold` on its own.
7. **Deliberate.** Templates cover commitments, reply waits, stale tasks and
   health. Open-ended concerns get **at most one tool-less router call per
   tick** (`P/mind/deliberate.py`): the model returns a task (title, body, a
   `result_field` check), a goal proposal, or a note. Without a router the
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
   breaker. The intention is one row of the initiatives table, which is also
   the audit log.
10. **Dispatch.** The body pulls `GET /v1/mind/dispatch`, creates the kanban
    task with `mind:<id>` as its idempotency key (`goal_mode` for a goal step)
    and posts `POST /v1/mind/dispatch/{id}/bound {hermes_ref}`. Messages wait
    in `GET /v1/mind/outbox` and go through `sending` and `sent`. Nothing is
    offered twice; a message left in `sending` at a restart is `uncertain`
    and is never resent.
11. **Outcome.** The body reconciles every `mind:*` task from its run row and
    posts `POST /v1/mind/outcome {id, hermes_ref, status, summary}`. The mind
    records the outcome, runs the intention's `success_check`, resolves the
    expectation it registered, records implicit feedback, checks the
    breaker, settles the concern and satiates the drive, and writes an
    autobiography entry: an owner-audience ledger row with `origin='mind'`
    that ordinary recall finds in any later session. A finished research
    task, investigation or goal step also stores its finding ("What I
    learned about X: ...") the same way, so a later turn uses it.

## The Mind section

An owner turn's `/context/assemble` carries a `protagine-mind` section of at
most 600 characters: "On my mind" (the broadcast set), "Working toward" (open
goals) and "Waiting for your say on" (open asks with their codes). Guests
never see it. With `faculties.broadcast` off the concerns are neither shown
nor added to the recall query.

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
notice; a floor ask is noticed at once at every level.

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
  budgets: {tasks_per_hour: 4, concurrent_tasks: 2, owner_messages_per_day: 3,
            contact_messages_per_day: 5, per_contact_cooldown_hours: 24,
            llm_tokens_per_day: 200000, open_goals: 2, goal_tasks: 4,
            goal_horizon_days: 7, task_max_runtime_s: 600, task_max_retries: 1}
  quiet_hours: "22:00-07:00"        # owner notices wait; the digest and tasks do not
  ask_expires_hours: 72
  breaker: {failures: 3, window_hours: 24, demotion_hours: 72}
  act_threshold: 0.6                # the ranker's effective-score floor
  digest_hour: 8                    # local hour after which the daily digest goes out
  drives: {duty: 1.0, social: 0.5, curiosity: 0.5, mastery: 1.0, upkeep: 1.0}   # 0 turns a drive off and cancels its waiting work
  faculties:                        # one binary flag each; each flag is one benchmark arm
    initiative: true
    drives: true                    # weights, satiation and goal adoption; off = flat priority
    deliberation: true              # the one tool-less call per tick; off = templates only
    goals: true                     # agent-owned goals
    broadcast: true                 # the top-3 concerns in turn context and recall
```

`identity.yaml` may list `agent.interests`; each becomes a seeded interest at
startup. Learning writes `mind.db`, the feedback multipliers and the
intention rows; nothing the mind learns writes `protagine.yaml` or
`identity.yaml`.

## The CLI

```
protagine mind status              enabled, level, queues, breaker, budgets
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
```

## The API (`/v1/mind`, one bearer key)

| Route | Body | Returns |
|---|---|---|
| `GET /dispatch` | | a JSON list of approved task intentions: `id`, `kind`, `type`, `drive`, `dedup_key` and `idempotency_key` (`mind:<id>`), `title`, `body`, `assignee` (`protagine-act`), `recipient`, `reason`, `max_runtime_seconds`, `max_retries`, `goal_mode`, `goal_max_turns`, `parent_goal_id`, `created_at`, `expires_at` |
| `POST /dispatch/{id}/bound` | `{hermes_ref, hermes_kind?, status?, bound_at?}` (idempotent: the same ref may arrive again) | the audit entry |
| `POST /outcome` | `{id, hermes_ref, hermes_kind?, status, outcome?, final?, summary?, error?, verified?, run?, block_kind?, consecutive_failures?, completed_at?, observed_at?}`: `status` is the kanban status, `outcome` (`done`, `blocked`, `failed`, `cancelled`, `uncertain`) the body's reading of it; `final: false` (a requeued failed run) is logged, not settled | the audit entry; 404 for an unknown intention |
| `GET /outbox` | | a JSON list of messages: `id`, `kind` (`notice` for the owner, `message` otherwise), `type`, `dedup_key`, `recipient`, `recipient_is_owner`, `recipient_handles` (`{gateway, address, is_primary, verified}`), `text`, `title`, `created_at`, `expires_at` |
| `POST /outbox/{id}/sending` | `{target?, at?}` | the audit entry; 409 unless the message was ready (a claim nobody settles is `uncertain` after 10 minutes) |
| `POST /outbox/{id}/sent` | `{result: sent | failed | uncertain, error?, hermes_ref?, summary?, at?}` | the audit entry |
| `POST /observations` | the body's board: `{observed_at, board, body, counts, stale_tasks, blocked_tasks, goals, mind_tasks}` with `idle_s` per task (docs/HERMES-ADAPTER.md), or the flat `{observations: [{kind, id, title, assignee, status, age_hours}]}`; stale owner tasks and goals are duty inputs | `{accepted, kinds}` |
| `POST /guard` | `{tool, args, session | session_id, run, task_id, recipients?, ...}`: a messaging tool's recipient is read from `args` (`contact_id`, `platform` + `target|chat_id|to`, or stock `target="platform:chat_id[:thread_id]"`); `recipients` are the contact ids an effect reaches later (a delivering cron job), each authorized with `may_contact` and the message budgets | `{allow, action: allow | block | ask, reason}` |
| `POST /decide` | `{code, answer: yes | no, contact_id?, session_id?, message?}` (the plugin's `protagine_self yes|no`) | `{ok, id, status, ...}`; 404 no open ask, 403 not the owner |
| `GET /log`, `GET /why/{id}`, `GET /log/{id}`, `GET /asks`, `GET /state` (`/status`; with `faculties`, `drives`, `concerns`, `goals`, `interests` and `deliberation`), `GET /stats` | | |
| `GET /concerns`, `GET /goals` | | the workspace (open concerns, the broadcast set, drive levels) and the open goals |
| `POST /interests` | `{topic, why?}` | a seeded interest the curiosity drive researches |
| `POST /asks/{code}/yes`, `POST /asks/{code}/no` | `{contact_id?, message?, by?}` | the audit entry |
| `POST /off {reason?}`, `POST /on`, `POST /tick`, `POST /rate {id, verdict}`, `POST /level {autonomy}`, `POST /reset {cls}` | | |

`GET /dispatch` and `GET /outbox` also record the body's last pull; when it is
older than five minutes the tick stops forming intentions until the body is
back (`POST /tick` forces one).

Test seams: `PROTAGINE_MIND_CLOCK_OFFSET_SECONDS` shifts the mind's clock
(the 72 h ask expiry is checked by restarting the sidecar three days ahead,
`scripts/ci_mind_loop.sh`), and the paired benchmark passes its own shifted
clock in (`protagine.qualification.native_memory_worker.serve_mind`). The
drives family runs the `full`, `full-drives`, `full-broadcast` and per-drive
arms (docs/PAIRED-AGENT-BENCHMARK.md).
