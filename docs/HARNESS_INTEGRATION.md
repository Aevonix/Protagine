# Connecting Hermes and coding harnesses

Use the [guided local setup](LOCAL-HERMES-SETUP.md) for the current Protagine
baseline and its [Hermes qualification target](HERMES-HOOK-COMPATIBILITY.md).
This guide describes the integration boundaries. Older Protagine installers,
manual context plugins and migration procedures are outside that baseline.

## Hermes native integration

The matching `protagine-hermes` package provides both native registrations:

| Repository path | Registration | Responsibility |
| --- | --- | --- |
| `plugins/hermes-plugin/` | `protagine` general plugin | Capture ordinary turns once, expose configured tools, and observe native work. |
| `plugins/protagine-memory/` | `protagine-memory` provider | Prepare scoped recollection before inference and use Hermes' native compression checkpoint. |

They share Protagine's client and durable ingestion path. The general plugin owns
ordinary turn capture when both are active; the memory provider does not write
a second copy. A general tool catalog alone does not provide automatic memory.

Hermes owns channels, interactive execution, native scheduling and subagents.
Protagine retains evidence and shared work associations. The general plugin does
not start a second event subscriber, scheduler or voice service. Private
hardware and channel integrations remain with the agent's deployment.

Guided setup selects one Hermes home and the actual runtime interpreter. It
preserves that profile's unrelated identity, model and channel configuration.
See the setup guide for installation, copied-adapter refresh and recovery;
installing a package alone does not prove that the selected gateway loaded it.

For concurrent tasks and detached reviews, use the explicitly documented
qualification build. Protagine does not silently patch or replace Hermes.

## Coding harnesses through MCP

MCP provides explicit tool access to the configured Protagine instance. It does
not by itself install Hermes' per-turn memory provider or unify a coding
harness's entire conversation with the agent.

Install the matching `protagine[mcp]` extra in the Protagine environment when adding
MCP. Inspect the selected harness configuration before writing it:

```bash
protagine mcp detect
protagine mcp setup --harness codex --dry-run
protagine mcp setup --harness codex
```

The CLI also accepts `claude-code`, `crush`, `opencode` and `hermes`. Select the
intended harness explicitly; `all` configures every detected harness. Use
`protagine mcp setup --help` for a configured sidecar URL, a contact selector or a
custom launch command. Configuration support is not proof of an active client
connection or equivalent behavior across those harnesses.

The default MCP server uses stdio; `protagine mcp run --transport http` selects
its HTTP transport. Keep credentials and harness configuration outside source
repositories. The selected credential's grants determine access; a contact ID
in configuration does not establish authority. See [scoped API authentication](SCOPED-API-AUTH.md).

## Verify the intended behavior

For native Hermes, tell the agent a harmless distinctive fact, start another
session and verify the answer against the retained source. Test shared work
through the intended channels separately. HTTP health and a loaded plugin do
not prove either behavior.

For an MCP client, verify that it lists the current Protagine tools and retrieves
one authorized source. Confirm the same credential cannot retrieve another
principal's private evidence. Explicit MCP tools and automatic native
recollection have different triggers; measure the path actually being used.

The [architecture guide](ARCHITECTURE.md) describes state ownership. The
[native adapter guide](HERMES-ADAPTER.md) covers lifecycle and evaluated learning,
and the [memory provider](../plugins/protagine-memory/README.md) describes context
scope and outages.
