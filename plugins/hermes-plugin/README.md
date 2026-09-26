# Protagine adapter for Hermes

The `protagine` plugin is a thin adapter on stock Hermes plugin seams. It has
no patches, no middleware and no synthetic platform. `protagine init` installs
it into the Python of the `hermes` executable and writes the two config keys it
needs; nothing else is required.

| Module | Job |
|---|---|
| `__init__.py` | Registers the hooks, the `/mind` command, the tools and one prompt section: the constitution from `identity.yaml`, the owner, the self-narrative (`GET /v1/mind/narrative`) and two tool notes, at most 4,000 characters, frozen per session by Hermes |
| `client.py` | Settings (`plugins.protagine.sidecar_url`, `plugins.protagine.key_file`), bearer key, short timeouts, circuit breaker |
| `capture.py` | Session map (`session_id -> sender, platform, last message`) and the durable SQLite turn outbox; hooks only enqueue, including `on_skill_lifecycle`'s loads of Protagine's own `protagine-*` skills (a `skill_use` row the body delivers to `POST /v1/mind/skills/used`) |
| `body.py` | The body thread: turn and skill-load delivery, then the mind loop on `/v1/mind` (dispatch to kanban with `mind:<id>` keys, the outbox sent verbatim once, outcome reconciliation, orphan archiving, board observations, off-switch cleanup, clearing Hermes' skills prompt cache when the sidecar's `skills.generation` moves) with its own ledger in `<hermes_home>/state/protagine-body.sqlite3` |
| `guard.py` | `pre_tool_call` rules for mind-originated and non-owner runs (architecture 7.5): in a mind run every effectful tool that names `protagine.yaml`, `identity.yaml` or `api.key` is blocked (a tripwire over the guarantee, a default worker with no shell or code tool), writes stay inside the task workspace, a delivering cron job's recipients are checked (`may_contact: never` blocks), then `POST /v1/mind/guard` → `{allow, reason}` |
| `commands.py` | `/mind status|log|why <id>|asks|off` |
| `tools.py` | `protagine_self` (`state|log|why|rate|yes|no|opinions|withdraw|reconsider`: the only source for claims about the agent's own actions, the record shown only in the owner's own session (anyone else gets the switch state and a refusal for `log` and `why`); `state` carries `working_on` and the narrative, `log` filters by `since_hours`, `kind`, `recipient`, `why` on an unknown id answers "no intention `<id>` exists in the audit log"; the agent's recorded opinions: `opinions [query]` and `why <opinion number>` for any session, filtered by the sidecar to the views meant for that session's participant, and owner only `withdraw|reconsider <number>` with the owner's reason), `protagine_people` (`who|inspect|propose_link`; owner: `set_permission|set_cadence|merge`; with `mind.faculties.people: false` only `who|inspect|set_permission`), `protagine_memory_search`, `protagine_memory_forget` |
| `reminders.py` | `protagine_reminder` on stock cron; the plugin keeps only the job id |

Hermes config written by `protagine init`:

```yaml
plugins:
  enabled: [protagine]
  hook_callback_timeout: 0
  protagine:
    sidecar_url: http://127.0.0.1:7777
    key_file: ~/.protagine/api.key
memory:
  provider: protagine-memory
kanban:
  dispatch_in_gateway: true
skills:
  external_dirs: [~/.protagine/skills]
```

The key file's directory is the Protagine instance directory. `skills.external_dirs` names
Protagine's own skills directory: with `mind.faculties.skills` on, the sidecar writes proven
lessons there as `SKILL.md` and owns their retirement (docs/MIND.md, Skills). The plugin reads
`protagine.yaml` (the `mind:` section: `enabled`, `autonomy`, `deny`) and
`identity.yaml` (`owner.contact_id`, `owner.handles.<platform>`, and the
constitution `agent.name`, `agent.values`, `agent.boundaries`) from it when
they are readable; both are optional for capture and recall.

## Identity in the prompt

The `protagine` prompt section is rendered once per session by Hermes
(`register_system_prompt_section`) and holds, in this order: the constitution
(`You are <name>. Your values: a; b. Your boundaries: x; y.`, at most 1,500
characters, owner-authored, written only by `protagine init`), `Your owner is
<name>.`, the self-narrative the sidecar keeps (`GET /v1/mind/narrative`, at
most 800 characters, fetched with a 2 s timeout for every new session; a failed
fetch is not retried for 60 s, and a slow, absent or older sidecar leaves the
constitution alone) and the two tool notes. Plain ids in the narrative are the
agent's own actions (`protagine_self why` explains them); prefixed ids are
record references.
A nightly narrative change reaches the next session, so the prompt cache holds
within one. The narrative is rendered only in a session that is the owner's
alone (a direct chat from an owner handle, or an internal lane with no chat),
read from the sender the gateway bound for the turn; every other session gets
the constitution and the notes. The worker profile loads the same plugin, so
mind tasks carry the constitution too.

Internal Hermes imports: `hermes_cli.kanban_db` and `kanban_db_connect`
(dispatch, reconciliation, observations, off-switch cleanup),
`tools.send_message_tool` (the outbox), `cron.jobs` and `cron.scheduler`
(reminders), `agent.prompt_builder.clear_skills_system_prompt_cache` (optional:
without it a new skill shows after a restart). Everything else goes through the public `PluginContext` API and
the config keys above.

See [docs/HERMES-ADAPTER.md](../../docs/HERMES-ADAPTER.md).
