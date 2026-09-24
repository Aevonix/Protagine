# Install and update

Protagine runs beside stock Hermes: the sidecar and its CLI live in their own
Python environment, one adapter package goes into the Python that runs
`hermes`, and one directory holds everything the instance owns. No patched
Hermes, no prepared runtime, no keyring.

Supported Hermes releases: `hermes-agent >=0.21.3,<0.22`. You need Python 3.12,
a Hermes install whose `hermes` executable is on `PATH` (pipx, uv or a venv all
work) and one OpenAI-compatible chat endpoint, which Hermes already has.

## Install

```bash
pipx install protagine
protagine init
hermes gateway restart
```

`protagine init` asks for your name and messaging handles, the agent's name and
values, and the autonomy level (`off`, `suggest`, `standard` or `trusted`;
default `standard`). Then it:

1. writes `protagine.yaml`, `identity.yaml` and `api.key` (mode 600) to the
   instance directory (`$PROTAGINE_HOME`, default `~/.protagine`);
2. creates the owner contact;
3. points the router at the model endpoint Hermes uses, and records an
   embedding endpoint when you pass `--embed-url` (semantic recall is on only
   then);
4. installs `protagine-hermes==<same version>` into the Python of the `hermes`
   executable and runs `pip check` there;
5. writes these Hermes keys: `plugins.enabled += protagine`,
   `memory.provider: protagine-memory`, `plugins.hook_callback_timeout: 0`,
   `kanban.dispatch_in_gateway: true`, `skills.external_dirs += <instance>/skills`,
   `security.protected_instruction_extra_patterns += protagine.yaml, identity.yaml, api.key`,
   and `plugins.protagine.{sidecar_url, key_file}`;
6. creates the `protagine-act` worker profile with the mind's toolsets and
   `approvals.deny` from `mind.deny.commands`;
7. installs and starts the sidecar user service (systemd `--user` on Linux,
   launchd on macOS; skipped with a message elsewhere, then start it with
   `protagine start`).

It writes no Hermes admin lists and never restarts a running gateway. Every
step is idempotent: run it again to change an answer, or pass the flags
(`protagine init --help`). `--non-interactive` uses the flags and defaults.

Two keys deserve a word. `plugins.hook_callback_timeout: 0` makes Hermes run
plugin callbacks inline, for every plugin, so overlapping capture and guard
calls are never skipped. The protected patterns match by basename in any
directory: writes to any `protagine.yaml`, `identity.yaml` or `api.key` need a
human, even under yolo, and fail closed inside a worker.

Check the result:

```bash
protagine doctor
```

It checks the Hermes version, the instance files, the adapter version, `pip
check`, the Hermes keys, the worker profile, the plugin registration and the
sidecar.

## Update

```bash
pipx upgrade protagine
protagine upgrade
hermes gateway restart
```

`protagine upgrade` takes a backup (`<instance>/backups/<stamp>/`), applies
the SQLite migrations, upgrades the adapter in Hermes' environment, reconciles
the Hermes keys and the worker profile, and restarts the sidecar service. When
nothing changed it says so and stops. Run it as often as you like.

To roll back, install the previous version and copy the files in
`<instance>/backups/<stamp>/` back into the instance directory.

## Upgrading from 1.9.0

Run `protagine upgrade` with `PROTAGINE_HOME` pointing at the 1.9.0 instance
directory (the one holding `instance.json`). It converts the keyring to
`api.key` (a credential the 1.9.0 sidecar still accepted, else a fresh key),
the `.env` values to `protagine.yaml` and `identity.yaml`, keeps the old files
as `.env.1.9.0` and `api-keyring.json.1.9.0`, moves the private-directory
forwarders out of `<hermes_home>/plugins/` into the backup (stock Hermes would
load them ahead of the installed adapter), and starts the mind at
`autonomy: suggest`; edit `mind.autonomy` to choose `standard` or `trusted`.
Reminders scheduled by 1.9.0 keep firing: their launchers run the new adapter.

If the instance used a prepared (patched) Hermes runtime, the upgrade prints
the one command that binds it to stock Hermes instead:

```bash
protagine upgrade --hermes-python <python of your stock hermes>
```

If the `protagine` sidecar package was installed inside Hermes' environment,
the upgrade prints how to move it out. Nothing restarts the running gateway;
do that yourself once you are ready.

## Configure

`<instance>/protagine.yaml`:

```yaml
sidecar: {host: 127.0.0.1, port: 7777}
hermes: {home: ~/.hermes, python: /path/to/hermes/python}
router: {base_url: http://127.0.0.1:8000/v1, model: my-model,
         embed_url: "", embed_model: "", embed_dims: 0, rerank_url: "", rerank_model: ""}
owner: {contact_id: "<created by init>"}
environment: {}                      # PROTAGINE_* settings no key above covers (see below)
mind:
  enabled: true                      # the off switch
  autonomy: suggest                  # off | suggest | standard | trusted
  deny:
    commands: []                     # globs written to the worker's approvals.deny
    tools: []                        # exact tool names blocked in mind-originated runs
    text: []                         # regexes over intention, message and tool-argument text
  worker_toolsets: [web, file, session_search, memory, todo]
  budgets: {tasks_per_hour: 4, concurrent_tasks: 2, owner_messages_per_day: 3,
            contact_messages_per_day: 5, per_contact_cooldown_hours: 24,
            llm_tokens_per_day: 200000, task_max_runtime_s: 600, task_max_retries: 1}
  quiet_hours: "22:00-07:00"         # owner notices wait; tasks and the digest do not
  ask_expires_hours: 72              # silence = no
  breaker: {failures: 3, window_hours: 24, demotion_hours: 72}
  act_threshold: 0.6                 # the ranker's effective-score floor
  digest_hour: 8                     # local hour after which the daily digest goes out
  stale_task_hours: 72               # open board tasks idle longer are reported to the mind
  faculties: {initiative: true, people: true, affect: true, opinions: true, broadcast: true,
              semantic_recall: false, consolidation: true, self_narrative: true, lessons: true,
              skills: false}
```

`router.embed_url` (an OpenAI-compatible embeddings endpoint, with
`embed_model`) turns semantic recall on. `router.embed_dims` is the model's
vector width; left at 0, the sidecar learns it from the endpoint's first
embedding, and a declared width is validated against every vector (a mismatch
is a startup failure named after the setting, never a silent switch to
keyword recall). `router.rerank_url` (an OpenAI/Jina style `/v1/rerank`
endpoint) with `router.rerank_model` (the model it serves; required with the
endpoint) makes recall rerank its candidates there. All are exported to the
sidecar process when it starts; a value already in that process environment
wins, so `PROTAGINE_RECALL_RERANK=shadow` can still be pinned to measure a
reranker before it changes what recall returns.

When an embedding endpoint is configured and the embedder does not come up
(the endpoint is down, the model name is wrong, the width differs), the
sidecar still serves, but `/v1/host/health` reports `degraded` with the reason
in `problems` ("semantic recall is off: ..."), and `protagine doctor` fails its
`semantic-recall` check with that reason. Recall does not quietly fall back to
keywords.

The mind itself (the tick, authority, asks, the audit log, the outbox and the
off switch) and the `protagine mind` command are described in
[docs/MIND.md](MIND.md).

The sidecar reads a number of tuning settings from its process environment
that have no key of their own: a reranker prompt style, recall thresholds and
oversampling, an endpoint credential. `environment` carries any of them in the
one configuration file, so a calibration survives an upgrade without a
hand-maintained service unit:

```yaml
environment:
  PROTAGINE_RERANKER_PROMPT_STYLE: qwen3
  PROTAGINE_RECALL_RERANK_MIN_SCORE: "0.74"
  PROTAGINE_RECALL_OVERSAMPLE: 5
  PROTAGINE_EMBED_API_KEY: "<the embedding endpoint's key, if it needs one>"
```

Names must be `PROTAGINE_` followed by capitals, digits and underscores. A
name that another key already defines (the instance directory, `sidecar.*`,
`api.key`, `mind.enabled`, `mind.autonomy`, `owner.contact_id`, the identity
fields, `router.embed_*` and `router.rerank_*`) is refused with the key to use
instead. Values are strings or numbers; quote words YAML would read as
booleans (`"on"`, `"off"`, `"yes"`). Entries are exported when the sidecar
starts, after the values the keys above derive (so `PROTAGINE_RECALL_RERANK:
shadow` here measures a configured reranker before it changes what recall
returns), and a value already in the process environment still wins. The
sidecar log names the entries it exported; a credential's value never reaches
a log, and neither does its name.

A few environment variables override the file for one process:
`PROTAGINE_HOME` (the instance directory), `PROTAGINE_SIDECAR_HOST`,
`PROTAGINE_SIDECAR_PORT`, `HERMES_HOME`, `PROTAGINE_MIND_ENABLED`,
`PROTAGINE_AUTONOMY` and `PROTAGINE_API_KEY` (in place of `api.key`).

## Security

- The sidecar binds `127.0.0.1` and refuses a non-loopback bind without a key.
- One key. Every request from the plugin sends `Authorization: Bearer <key>`;
  the person a request acts for comes from the contact identity in the body,
  so owner versus guest is a property of the contact, never of a credential.
- Without a key the API serves loopback callers only (a development
  convenience), and the credential-handling routes stay closed.

## Uninstall

```bash
protagine init --uninstall
hermes gateway restart
```

This removes the adapter and its keys from Hermes' config, moves the
`protagine-act` profile (its sessions, memories and logs) into
`<instance>/backups/<stamp>/profiles/`, removes the sidecar service and
uninstalls the adapter from Hermes' environment, unless another profile of
that Hermes home still enables it. What is left is stock Hermes. The instance
directory and its data are kept; delete it yourself when you no longer want
them.
