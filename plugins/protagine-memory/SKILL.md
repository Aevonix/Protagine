---
name: protagine
version: 1.5.9
description: Protagine context sidecar for Hermes with exact per-turn participant binding, scoped guest projection, and read-only general-plugin coexistence.
author: Aevonix
---

# Protagine memory provider

Use Protagine as Hermes' intelligence/context sidecar without patching Hermes
core. This integration does not own or replace the host's custom voice, phone, or
intercom turn path.

## Required posture

```bash
PROTAGINE_PREFETCH_TURN_CONTACT=1
PROTAGINE_MCP_CONTACT_ID=replace-with-exact-owner-contact-id
PROTAGINE_MEMORY_DEFAULT_CONTEXT_AUTHORITY=none
```

Bind owner CLI context through the general adapter's explicit
`attested_system_platforms: [cli]` configuration. Every real channel resolves
its sender independently; a miss yields no Protagine context or write.

Guest context requires a `context:read` scoped channel principal and an exact
server-resolved contact grant. The provider requests the guest's context with
`audience: viewer`; the sidecar returns only that contact's scoped sections.
If any step fails, use no Protagine context. Never substitute owner context.

## Model tools

The provider's one tool exists only on the owner's own lane: the write
`protagine_resolve_commitment`. Its person selector is server/provider-bound,
not a model argument. Contact affect is recorded by the sidecar from each
contact's own turns, never by a tool. Commitments, facts,
affect and recent history are read from the assembled per-turn context, and
`protagine_memory_search` (the general adapter's tool) finds more. A guest
session, or a real channel with no sender binding, is offered none of them
(Hermes collects the list when it builds the session's agent); a call that
still arrives gets one final answer
`{"unavailable": true, "retry": false, "reason": ...}`. Do not advertise memory
writes, goals, patterns, search, queue mutation, or approval tools from this
provider.

## Lifecycle

In general-plugin coexistence, turn sync, built-in memory mirroring, and
pre-compression signal writes are disabled because the general plugin owns
ingestion. Standalone fallback writing is allowed only after resolving the
exact current participant. Reply-window lookup against the global timeline is
disabled pending a transport-attested scoped endpoint.

Use `catalog_attestation()` for deployment admission. A true
`provider_governance_ready` does not imply the broader general plugin is ready;
`general_plugin_governance_ready` remains false until its named follow-on slice
is complete.
