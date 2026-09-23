# Hermes adapter

`protagine-hermes` is one wheel with two Hermes plugins:

- `protagine` (general): turn capture, the guard, `/mind`, and the self, people,
  memory and reminder tools
- `protagine-memory` (memory provider): per-turn recall through
  `/v1/host/context/assemble`, turn sync when the general plugin is absent, and
  the commitment, affect, fact, goal and timeline tools

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
| Heartbeat | `on_kanban_dispatch_tick` wakes the body thread |
| Owner commands | `register_command("mind")`: read-only in chat plus `off` |
| Tools | `register_tool`; handlers receive `session_id`, which the session map resolves to a sender |
| Reminders | stock cron: a one-shot job whose id is the only thing the plugin keeps |

## Owner and guest

The owner is the sender whose handle is listed in `identity.yaml`
(`owner.handles.<platform>`) or whose resolved contact is `owner.contact_id`.
Turns without a sender on the CLI, cron or API lanes are the owner's own. Any
other sender is a guest: `session_search` is blocked for guests, delivering
cron jobs need a permitted recipient, and every mutation through
`protagine_self`, `protagine_people` and `protagine_memory_forget` is refused.

## Mind-originated runs

A kanban worker spawned on the `protagine-act` profile is a mind-originated
run (the dispatcher sets `HERMES_KANBAN_TASK` and `HERMES_PROFILE`). In it:

- `kanban_create` only with `assignee: protagine-act`, no `mind:` key, no
  project override and no workspace outside `HERMES_KANBAN_WORKSPACE`
- `write_file` and `patch` only inside `HERMES_KANBAN_WORKSPACE`, every V4A
  header target included
- `mind.deny.tools` and `mind.deny.text` from `protagine.yaml` block; floor
  patterns (money, irreversible deletion, credentials, bulk messaging) ask
- messaging tools and delivering cron jobs need a permitted recipient
- with `mind.enabled: false`, every effectful tool is blocked

The guard asks `POST /v1/mind/guard` with a 2 s timeout when the sidecar serves
the mind routes; silence blocks effectful tools, a 404 falls back to the rules
above. Read-only tools never wait on the sidecar.

## Memory provider

`prefetch` assembles context for the turn's participant. A guest request sets
`audience: viewer` and `projection_policy: scoped_viewer_required`, so the
sidecar never returns owner-only sections to a guest. `sync_turn` is active
only when the general plugin is not enabled; otherwise the outbox owns
capture. `on_pre_compress` writes a checkpoint through the same outbox before
Hermes compresses a session.

## Tests

`tests/hermes_adapter` runs against stock Hermes installed in the same
environment plus a fake sidecar; see `tests/hermes_adapter/conftest.py`.
