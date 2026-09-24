# Hermes adapter

`protagine-hermes` is one wheel with two Hermes plugins:

- `protagine` (general): turn capture, the guard, `/mind`, and the self, people,
  opinions, memory and reminder tools
- `protagine-memory` (memory provider): per-turn recall through
  `/v1/host/context/assemble`, turn sync when the general plugin is absent, and
  the owner's commitment and affect writes

Both run on stock Hermes (`hermes-agent >= 0.21.3, < 0.22`) with no patches.

## Install

```bash
pipx install protagine && protagine init && hermes gateway restart
```

`protagine init` installs `protagine-hermes` into the Python of the `hermes`
executable, writes the config keys below and creates the `protagine-act`
worker profile. `protagine upgrade` repeats the adapter install when the
version changed.

```yaml
plugins:
  enabled: [protagine]
  hook_callback_timeout: 0          # callbacks run inline; overlapping fires are not dropped
  protagine:
    sidecar_url: http://127.0.0.1:7777
    key_file: ~/.protagine/api.key  # one line, mode 600
memory:
  provider: protagine-memory
kanban:
  dispatch_in_gateway: true         # the plugin's heartbeat is the dispatch tick
security:
  protected_instruction_extra_patterns: [protagine.yaml, identity.yaml, api.key]
```

Every request to the sidecar carries `Authorization: Bearer <key>`.

## Seams used

| Need | Stock seam |
|---|---|
| Who is speaking | `pre_llm_call` (`session_id`, `platform`, `sender_id`, `user_message`), kept in a bounded session map |
| Turn capture | `post_llm_call` enqueues one row in `<hermes_home>/state/protagine-turn-outbox.sqlite3`; the body thread delivers it to `POST /v1/host/turns/sync` |
| Recall | memory provider `prefetch` |
| Guard | `pre_tool_call`; a block directive fails closed, a floor match becomes an `approve` directive (Hermes' human-approval gate, which fails closed in a worker) |
| Heartbeat | `on_kanban_dispatch_tick` wakes the body thread; stock fires it from `dispatch_once` in the one process holding the dispatcher lock, so it also marks this process as the board's writer |
| Pause | `agent.estop.is_engaged()` (`hermes pause`): the body creates and sends nothing while it holds |
| DM lookup | the stock session rows (`hermes_state_registry`, `list_gateway_sessions`): the DM chat the gateway has had with a sender, for handles that name a user rather than a chat (Discord) |
| Durable work | `hermes_cli.kanban_db.create_task(idempotency_key="mind:<id>", assignee="protagine-act", ...)`; an existing non-archived task with the key is returned, never duplicated |
| Messages | `tools.send_message_tool.send_message_tool({"action": "send", "target", "message"})`, text verbatim |
| Task outcomes | the `mind:*` task's status plus its latest `task_runs` row (`kanban_db.latest_run`) |
| Owner commands | `register_command("mind")`: read-only in chat plus `off` |
| Tools | `register_tool`; handlers receive `session_id`, which the session map resolves to a sender |
| Reminders | stock cron: a one-shot job whose id is the only thing the plugin keeps |

## Owner and guest

The owner is the sender whose handle is listed in `identity.yaml`
(`owner.handles.<platform>`) or whose resolved contact is `owner.contact_id`.
Turns without a sender on the CLI, cron or API lanes are the owner's own for
memory; a cron run (`platform: cron`) is a stored prompt rather than the
owner typing, so it cannot answer an ask, rate, change a permission or
forget. Any other sender is a guest: `session_search` is blocked for guests,
delivering cron jobs need a recipient whose `may_contact` is not `never`, and
every mutation through `protagine_self`, `protagine_people` and
`protagine_memory_forget` is refused.

## Mind-originated runs

A kanban worker spawned on the `protagine-act` profile is a mind-originated
run (the dispatcher sets `HERMES_KANBAN_TASK` and `HERMES_PROFILE`). In it:

- `kanban_create` only with `assignee: protagine-act`, no `mind:` key, no
  project override and no workspace outside `HERMES_KANBAN_WORKSPACE`
- `write_file` and `patch` only inside `HERMES_KANBAN_WORKSPACE`, every V4A
  header target included
- `mind.deny.tools` and `mind.deny.text` from `protagine.yaml` block; floor
  patterns (money, irreversible deletion, credentials, bulk messaging) ask
- messaging tools and delivering cron jobs need a permitted recipient: the
  plugin resolves a job's effective `deliver` and `failure_deliver` contacts
  (a stored job's when an update does not resend them) and hands them to the
  sidecar as `recipients`, which applies `may_contact` and the message budgets
  to each of them exactly as it does to a `send_message` recipient
- with `mind.enabled: false` or `autonomy: off`, every effectful tool is blocked

The guard asks `POST /v1/mind/guard {tool, args, session, run, task_id, owner,
contact_id, platform, sender_id, recipients}` with a 2 s timeout when the
sidecar serves the mind routes and reads `{allow, reason}` (`ask: true` becomes
Hermes' approval gate); silence blocks effectful tools, a 404 falls back to the
rules above. Read-only tools never wait on the sidecar.

## The body: the mind's effects on Hermes

The body thread runs in every Hermes process, but only the one that owns the
kanban dispatcher writes to the board: stock fires `on_kanban_dispatch_tick`
from `dispatch_once` in the process holding the singleton dispatcher lock, and
the body runs steps 2 to 5 only after it has seen that tick. A CLI session, a
gateway that lost the lock and a kanban worker drain the turn outbox only. While
`hermes pause` holds (the stock ESTOP sentinel) steps 2 and 3 wait as well. Once
the sidecar serves `/v1/mind/*`, the body does this on every tick (about every
60 s, and at once when the dispatch tick or a captured turn wakes it):

1. `GET /v1/mind/state`. With `enabled: false` there, or `mind.enabled: false`
   in `protagine.yaml`, steps 2 and 3 are skipped and every unstarted `mind:*`
   task is archived; running workers end on their own. None of this needs a
   model endpoint.
2. `GET /v1/mind/dispatch` → for each `kind: task` intention one
   `kanban_db.create_task(title, body, assignee="protagine-act",
   idempotency_key=dedup_key or "mind:<id>", workspace_kind="scratch",
   created_by="protagine", priority, max_runtime_seconds, max_retries,
   goal_mode, goal_max_turns)`, then `POST /v1/mind/dispatch/{id}/bound
   {hermes_ref, hermes_kind: "kanban", status, bound_at}`. A lost ack or a
   restart creates no second task: the key returns the existing one, and a
   task with the key that was archived in between (stock's lookup skips
   archived tasks) is bound and settled as `cancelled` rather than recreated.
   Messages and notices never travel through dispatch. Runtime and retries
   fall back to `mind.budgets.task_max_runtime_s` / `task_max_retries`.
3. `GET /v1/mind/outbox` → for each message, in order: skip it if the body's
   own ledger has begun it before; `POST /v1/mind/outbox/{id}/sending {target,
   at}` (any non-2xx: not ours); record `sending` locally; send **verbatim**
   with `send_message_tool`; record the result; `POST
   /v1/mind/outbox/{id}/sent {result: sent|failed|uncertain, error, hermes_ref,
   at}`. `success` from the tool is `sent`; an error raised before the platform
   call (unknown target, unconfigured platform, no home channel) is `failed`;
   `Send failed: ...` or an exception is `uncertain`. A
   process that stops between `sending` and `sent` reports that message
   `uncertain` at the next start and never sends it, whatever the sidecar
   lists. The target is the message's `target` (`platform:chat_id`), else
   `recipient.platform` + `recipient.chat_id|address|handle`, else, for the
   owner (`recipient_is_owner`, the owner contact or no recipient), the first
   handle in `identity.yaml` `owner.handles` (the `[{platform, id}]` list
   `protagine init` writes, or a `{platform: [ids]}` mapping), else the first of
   the sidecar's `recipient_handles` (`{gateway, address}`, primary first). A
   handle names a sender; where that is not a chat (a Discord user id) the
   target is the DM session stock has recorded with that user, and the handle
   itself when none has been seen yet.
   The sidecar answers `sending` with 409 for a message it already handed
   out, and marks a claim nobody settled `uncertain` after ten minutes. A
   claim that names no target (`target: null`, the body resolved no handle)
   is also 409 (`no_target`) and is not a claim: the message stays ready,
   never `failed`, and the next pull offers it again with `recipient_handles`
   read afresh, so a handle added later lets it go out.
4. Reconciliation: for every `mind:*` task, `GET /v1/mind/why/{id}`. A 404, or
   a lifecycle `status` in `proposed | asked | denied | expired | cancelled |
   dropped`, means no dispatched intention owns the task and it is archived
   (the ask backstop of architecture 7.7). Otherwise, when the task is
   `done | blocked | review | archived` or its latest run has ended, one
   `POST /v1/mind/outcome {id, hermes_ref, hermes_kind, status, outcome:
   done|blocked|failed|cancelled|uncertain, final, summary, error, verified:
   "hermes_failure"|null, run: {id, outcome, status, profile, started_at,
   ended_at}, block_kind, consecutive_failures, completed_at, observed_at}` per
   state change (a failed run that Hermes requeues is one `failed`, `final:
   false` outcome; a task parked `blocked` with its failed run once
   `consecutive_failures` reached `max_retries` is `failed`, `final: true`; a
   `kanban_block` is `blocked`; a 404 archives the task). The body remembers
   what it posted in `<hermes_home>/state/protagine-body.sqlite3` so an outcome
   is posted once.
5. `POST /v1/mind/observations {observed_at, board, body: {pid, profile,
   started_at, interval_s, ticks, mind_ticks, last_tick_at, last_mind_tick_at,
   last_pull_at, stale}, counts: {status: n}, stale_tasks, blocked_tasks,
   goals, mind_tasks}` when the board changed and at least every 5 minutes.
   Task entries carry `id, title, status, assignee, created_by, created_at,
   started_at, age_s, idle_s, block_kind, goal`; a stale task is an open
   non-mind task idle for `mind.stale_task_hours` (default 72); goals are
   kanban goal-mode tasks; `mind_tasks` add `intention_id` and the latest run.

Every `GET /v1/mind/dispatch` is the body's heartbeat: a sidecar whose last
pull is older than 5 minutes stops deliberating (architecture 3.1).

`protagine_hermes.tick()` runs one full tick on the caller's thread: `POST
/v1/mind/tick` (the sidecar forms intentions now) and then the body pass above.
The paired benchmark's body tick calls it before Hermes cron and kanban
dispatch, with `PROTAGINE_BODY_THREAD=0` so the body's own thread stays
parked and nothing lands between two observed ticks; a gateway never needs
either.

## Asks

An ask lives only in the sidecar and creates nothing in Hermes until approved.
The owner answers with `protagine mind yes|no <code>`, or in chat by typing
the code ("yes K7F"): the model calls `protagine_self` `yes`/`no` and the
plugin sends `POST /v1/mind/decide {code, answer, session_id, contact_id,
message}` only when the session's sender is the owner, the turn is not a kanban
worker, and the owner's own message for that turn contains the code as a whole
word; the sidecar checks the contact and the message again and answers
`{ok, id, status, ...}` (404: no open ask with that code, 403: not the owner).
A guest, a worker whose task body quotes the code, or a page injected into an
owner session cannot approve. `protagine_self rate {id, verdict}` posts
`POST /v1/mind/rate` for the owner only. `log` and `why` are the owner's too,
and a guest's `state` says only whether the mind is on and at what level: the
log and the asks name what the owner asked about other people.

## People

`protagine_people` is the model's view of the people store over `/v1/mind/people`
(the sidecar side is `sidecar/protagine/api/routers/people.py`):

| Operation | Who | Route |
|---|---|---|
| `who {contact_id}` | everyone | `GET /v1/mind/people?q=` (a name, handle or id; empty lists the newest) |
| `inspect {contact_id}` | everyone | `GET /v1/mind/people/{who}` |
| `propose_link {contact_id, handle: gateway:address}` | everyone | `POST /v1/mind/people/link`: a candidate the owner confirms as an ask |
| `set_permission {contact_id, permission}` | owner | `POST /v1/mind/people/{who}/permission {may_contact: never\|ask\|auto}` |
| `set_cadence {contact_id, minutes}` | owner | `POST /v1/mind/people/{who}/cadence {minutes}` (0 clears) |
| `merge {contact_id, drop}` | owner | `POST /v1/mind/people/merge {keep, drop}`: `drop` folds into `contact_id`; handles, sources, comms and affect follow the person |

A guest sees who someone is (`contact_id`, `display_name`, `trust_tier`) and
nothing else: the plugin names the guest as the viewer (`contact_id`) on its
reads and the sidecar answers with only those fields. The owner also sees
`may_contact`, `cadence_minutes`, `last_interaction_at`, the per-contact
`digest` and the handles. The three mutations are refused in the plugin
outside the owner's own interactive session (a guest, a kanban worker, a cron
run) and are sent with the owner as `contact_id`, which the sidecar checks
again (403 `not_owner`). `protagine people who|inspect|permit|cadence|merge|link|proposals`
is the same interface from the CLI. `may_contact` is raised nowhere else; a
contact's opt-out ("STOP", "don't text me", ...) only lowers it to `never`.

`protagine_self` also carries the agent's recorded opinions (one tool, not a
separate one, so the tool schemas stay within their budget): `opinions` with an
optional `query` and `why <number>` read `GET /v1/mind/opinions` with the session's
contact, so a guest sees only everyone-audience views (an intention id is never all
digits, so `why` on any other id is still the owner's record); `withdraw` and
`reconsider <number>` with the owner's `reason` post
`POST /v1/mind/opinions/{id}/withdraw|reconsider` from the owner's own interactive
session only, never a guest, a kanban worker or a cron run (docs/OPINIONS.md). The
guard treats the tool as read-only.

## Memory provider

`prefetch` assembles context for the turn's participant. A guest request sets
`audience: viewer`, and the sidecar returns a guest only that contact's scoped
sections, never an owner-only one. The scoping fails closed: anyone the
sidecar cannot show to be the owner, a caller without the key included, gets
the contact-scoped set. The request also says
whether Hermes still shows this session's earlier turns (`session_history:
intact`, `compressed` after a checkpoint), so recall never quotes back what the
model is already reading. The provider's direct tools are offered on the
owner's own lane only: a guest session or a channel
with no sender binding gets none of them, and a call that still arrives, like
`protagine_memory_search` for a turn with no resolved participant, is answered
once with `{"unavailable": true, "retry": false, "reason": ...}` rather than
an error the model retries. `sync_turn` is active
only when the general plugin is not enabled; otherwise the outbox owns
capture. `on_pre_compress` writes a checkpoint through the same outbox before
Hermes compresses a session.

## Tests

`tests/hermes_adapter` runs against stock Hermes installed in the same
environment plus a fake sidecar; see `tests/hermes_adapter/conftest.py`.
