# Protagine adapter for Hermes

The `protagine` plugin is a thin adapter on stock Hermes plugin seams. It has
no patches, no middleware and no synthetic platform. `protagine init` installs
it into the Python of the `hermes` executable and writes the two config keys it
needs; nothing else is required.

| Module | Job |
|---|---|
| `__init__.py` | Registers the hooks, the `/mind` command, the tools and one prompt section |
| `client.py` | Settings (`plugins.protagine.sidecar_url`, `plugins.protagine.key_file`), bearer key, short timeouts, circuit breaker |
| `capture.py` | Session map (`session_id -> sender, platform, last message`) and the durable SQLite turn outbox; hooks only enqueue |
| `body.py` | The body thread: turn delivery, then the mind loop on `/v1/mind` (dispatch to kanban with `mind:<id>` keys, the outbox sent verbatim once, outcome reconciliation, orphan archiving, board observations, off-switch cleanup) with its own ledger in `<hermes_home>/state/protagine-body.sqlite3` |
| `guard.py` | `pre_tool_call` rules for mind-originated and non-owner runs (architecture 7.5); `POST /v1/mind/guard` → `{allow, reason}` |
| `commands.py` | `/mind status|log|why <id>|asks|off` |
| `tools.py` | `protagine_self` (`state|log|why|rate|yes|no`), `protagine_people`, `protagine_memory_search`, `protagine_memory_forget` |
| `reminders.py` | `protagine_reminder` on stock cron; the plugin keeps only the job id |
| `opinions.py` | `protagine_opinions` (`list|why <id>`, owner only `withdraw|reconsider <id>`); reads carry the session's contact |

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
```

The key file's directory is the Protagine instance directory. The plugin reads
`protagine.yaml` (the `mind:` section: `enabled`, `autonomy`, `deny`) and
`identity.yaml` (`owner.contact_id`, `owner.handles.<platform>`) from it when
they are readable; both are optional for capture and recall.

Internal Hermes imports: `hermes_cli.kanban_db` and `kanban_db_connect`
(dispatch, reconciliation, observations, off-switch cleanup),
`tools.send_message_tool` (the outbox), `cron.jobs` and `cron.scheduler`
(reminders). Everything else goes through the public `PluginContext` API and
the config keys above.

See [docs/HERMES-ADAPTER.md](../../docs/HERMES-ADAPTER.md).
