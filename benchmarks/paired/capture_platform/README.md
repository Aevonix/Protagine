# Capture platform

A benchmark-only Hermes gateway platform plugin. The paired worker copies this
directory into the disposable profile (`<HERMES_HOME>/plugins/capture`) and
enables it in both arms. It registers through the ordinary
`PluginContext.register_platform` seam, so cron deliveries (`deliver:
capture:owner`, or the bare `capture` home target), `send_message` tool calls
and gateway sends all reach its sender.

The sender transmits nothing. It appends one entry to a single JSON array,
`CAPTURE_OUTBOX` (default `<HERMES_HOME>/outbox.json`):

```json
[{"target": "capture:owner", "text": "...", "at": "2027-03-04T09:00:00-05:00", "via": "platform"}]
```

`via` is `platform` for anything sent through Hermes and `reply` for the
agent's answer to an inbound contact message, which the harness records itself.
`at` follows the Hermes clock, so `advance_clock` events are visible in it.

Targets are recorded as written: a message to the wrong recipient is evidence,
not an error. Contact ids are fixed width (`p-01`), so a `forbidden` check on
one id cannot match another.

Nothing is received from a network; inbound messages are episode events
delivered by the harness (`docs/PAIRED-AGENT-BENCHMARK.md`, "Episode events").
