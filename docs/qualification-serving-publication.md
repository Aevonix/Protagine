# Publishing serving cells

`public_performance.export_serving_record` converts existing serving-client,
timing-observer and independent correctness receipts into the public run schema.
It does not run inference, start a server, grade model answers or publish files.
The caller supplies an explicitly approved synthetic dataset manifest and public
deployment metadata. Prompts, generated text, private model aliases, command
lines, endpoints and internal topology are never exported.

The optional `performance` object uses `serving-cell-v1`. A record belongs to
the `serving` role, `role_completion` boundary and `performance` phase. Its input
target and client concurrency limit identify a cell. Actual input-token ranges
come from server usage. Configured server slots remain in deployment metadata.

Observed overlap is nullable. When every request has
`request_started_monotonic_s` and `request_finished_monotonic_s`, the exporter
counts overlapping same-client intervals, with touching intervals excluded.
This counts open client requests, not active GPU sequences. The older pilot has
no such timestamps, so its observed overlap remains unknown. The serving
client's estimated peak is deliberately ignored.

The exported metrics distinguish:

- First generated output, which may be reasoning.
- First final-content delta, which need not be useful or correct.
- Entire request completion, including reasoning and any recorded failures.
- Aggregate completion tokens divided by the measured batch duration. Token
  accounting declares whether reasoning is included. Missing usage is unknown.
- SSE chunk intervals. Speculative bursts are not individual token timestamps.

Every declared request remains in the correctness denominator. Timing and grade
receipts are joined by their frozen prompt digest, never position or a mutable
display label. Transport completions are reported separately from correct
answers. Missing attempts, absent grades, timeouts and wrong-model responses
cannot become verified primary successes. Repeated prompts need separately
identified repetition runs.

The caller must explain the cache condition, output budget, thinking settings
and sample limitations. The current pilot reuses a small development set and
is not a capacity ceiling or heldout-quality result. Its correctness receipt
names a scorer; if its source artifact was not recorded, state that limitation
rather than presenting the available version digest as full scorer provenance.

The exporter returns a content-derived immutable run identity. It leaves the
comparison key null until a matched comparison protocol exists. The ordinary
`publish_snapshot` function can append reviewed records while retaining old
files byte for byte. Never publish the unreviewed private draft merely because
the schema validates it.
