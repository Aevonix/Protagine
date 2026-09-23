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
| `body.py` | The delivery thread; mind dispatch, outbox, reconciliation and observations once `/v1/mind/*` exists |
| `guard.py` | `pre_tool_call` rules for mind-originated and non-owner runs (architecture 7.5) |
| `commands.py` | `/mind status|log|why <id>|asks|off` |
| `tools.py` | `protagine_self`, `protagine_people`, `protagine_memory_search`, `protagine_memory_forget` |
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
```

The key file's directory is the Protagine instance directory. The plugin reads
`protagine.yaml` (the `mind:` section: `enabled`, `autonomy`, `deny`) and
`identity.yaml` (`owner.contact_id`, `owner.handles.<platform>`) from it when
they are readable; both are optional for capture and recall.

Internal Hermes imports: `hermes_cli.kanban_db` (off-switch cleanup and, later,
dispatch), `cron.jobs` and `cron.scheduler` (reminders). Everything else goes
through the public `PluginContext` API and the config keys above.

See [docs/HERMES-ADAPTER.md](../../docs/HERMES-ADAPTER.md).
