# Shared inference capacity

The inference pool gives native Hermes, Protagine and other OpenAI-compatible
clients one admission boundary. It holds excess requests outside model servers,
balances eligible replicas by estimated work, and reserves request and token
capacity for configured traffic classes. It does not schedule agent tasks or
replace Hermes delegation.

The existing `modelPool` and `functionRoles` continue to select qualified models.
Their concurrency metadata is eligibility information. This optional boundary
enforces capacity across processes, provided all competing callers use it.

## Deployment configuration

Each physical serving replica appears once in `endpoints`. Routes reference it
by name, so chat, reasoning and workers share its counters. A route may reference
one or many equivalent replicas. Equivalence must be qualified by the deployment,
including model revision, tokenizer, tool format and inference settings. A model
name alone does not establish equivalence. Do not put different DNS aliases or
tunnels to the same replica into separate endpoint entries.

Addresses, models, capacities and reservations belong to deployment configuration,
not Protagine code. This example uses illustrative capacities, not recommendations
for a particular model or machine:

```json
{
  "version": 1,
  "endpoints": {
    "replica-a": {
      "base_url": "http://127.0.0.1:9001/v1",
      "model": "qualified-model",
      "max_requests": 4,
      "max_tokens": 131072,
      "context_tokens": 32768,
      "reservations": {
        "interactive": {"requests": 1, "tokens": 32768},
        "essential": {"requests": 1, "tokens": 16384}
      },
      "max_input_tokens": {"background": 4096}
    },
    "replica-b": {
      "base_url": "http://127.0.0.1:9002/v1",
      "model": "qualified-model",
      "max_requests": 4,
      "max_tokens": 131072,
      "context_tokens": 32768,
      "reservations": {"essential": {"requests": 1, "tokens": 16384}}
    }
  },
  "routes": {
    "chat": {
      "model": "agent-model", "endpoints": ["replica-a", "replica-b"],
      "traffic_class": "interactive", "priority": 0
    },
    "control": {
      "model": "agent-model", "endpoints": ["replica-a", "replica-b"],
      "traffic_class": "essential", "priority": 1
    },
    "workers": {
      "model": "agent-model", "endpoints": ["replica-a", "replica-b"],
      "traffic_class": "background", "priority": 2,
      "queue_timeout_seconds": 60
    }
  },
  "max_queue": 128,
  "aging_seconds": 10,
  "cooldown_seconds": 10,
  "request_timeout_seconds": 1800,
  "cancellation_grace_seconds": 0.25
}
```

Run with the sidecar's Python environment:

```sh
python -m protagine.inference_pool.server --config /path/to/private-pool.json --port 5020
```

Point a native provider at `http://127.0.0.1:5020/chat/v1` with model
`agent-model`; use the other route names for their corresponding workloads.
Function-role bindings may point to these same routes. Existing role qualification
and exceptions remain in effect. A memory extraction model need not join the
conversation replica pool.

Use one process and one worker. Multiple independent proxy workers would maintain
independent counters and invalidate reservations. The default listener is
loopback; this is a trusted local inference boundary, not a public API. Route
priority comes from configuration. Clients with access to all routes are trusted
to use the route assigned to their function.

Changing endpoints or limits requires validating the new configuration and
draining the proxy before restart. Existing function-role configuration reload
still works, but this initial pool does not hot-swap admission counters.

## What the limits mean

- `max_requests` and `max_tokens` are admission limits, not advertised maximums.
  Set them from measured mixed-load behavior. Tokens cover input plus the output
  allowance for active calls, not an engine's entire cache.
- Reservations protect unused capacity for each class. A waiting parent task
  holds no inference lease while its children run. Leases cover model calls only.
- `max_input_tokens` limits each class's estimated prefill size on each replica.
  Set a class to zero to exclude it there. Keeping huge background prefills off
  one replica protects interaction better than a spare request slot alone.
- Lower priority numbers run first. Aging improves fairness among queued calls
  that can fit. Queue deadlines and bounds prevent unbounded accumulation.
- A backend can declare `tokenize_path`, an origin-relative endpoint accepting
  the chat request and returning an integer `count`. For example, a qualified
  server implementing that protocol may use `/v1/tokenize`. The counter must
  apply the same template and tools as generation. Counts are reused only across
  the qualified equivalent replicas in that route. Otherwise input estimates
  use UTF-8 bytes and formatting overhead, which may reject text that the model
  could accept. Output limits reserve generation space. Media requires a separate
  per-item allowance even with a counter, because template token counts may omit
  image/audio expansion. Unbudgeted media is rejected explicitly.
- Each route defaults to 2,048 output tokens, with a maximum of 8,192. Override
  `default_output_tokens` and `max_output_tokens` for workloads requiring more.
  This proxy initially supports one generated sequence per request.
- Set an endpoint's `api_key_env` to the name of an environment variable when
  upstream authentication is required. Do not put credentials in URLs or JSON.

Raw streamed responses, tool calls and provider-specific request fields pass
through. Failed endpoints enter a cooldown. Requests are not transparently
replayed after streaming has begun. `/status` exposes admission counts, queue and
health information without prompts or credentials.

Slot and token reservations do not reserve GPU execution time. Long prefills,
external bypass traffic, endpoint failure and engine scheduling can still cause
delays. Closing a cancelled upstream stream plus a short grace period is best
effort cancellation, not proof that every backend has stopped computing. Strict
isolation needs a protected replica or a separately qualified engine scheduler.
An unrestricted, fully saturated fleet cannot also promise zero waiting.

## Qualification

First run the configuration, scheduler and HTTP boundary tests with mock
backends. They exercise reservations, oversized requests, queue cancellation,
failover and streamed responses without occupying inference hardware. Then run
one representative mixed workload and record time to first token, errors and
completion time by class. Use ordinary traffic for further tuning. Do not infer
usable capacity from a server's request-slot setting alone.
