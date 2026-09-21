# Paired Hermes and Protagine benchmark

`protagine models paired` runs the same task episodes through fresh Hermes and fresh Hermes with Protagine enabled. It records what the base agent completes, what the combined system completes, and the difference. The reviewed development dataset has 60 episodes, ten in each of six families. The original 18-episode pilot remains available under its original version. Neither dataset establishes a comprehensive model ranking.

Both arms use the same configured model, provider settings, immutable container image, initial files, source events, task instructions, output limits and artifact verifier. Hermes keeps its ordinary memory and session tools in the baseline. Protagine adds its normal integration in the other arm. Historical facts arrive through the same episode turns; answers are not preloaded only for Protagine. The verifier's oracle stays outside the agent container.

Each arm starts a new container with fresh state for each episode. State persists across turns and sessions inside that episode. No owner home, memories, live-agent state or Docker socket is mounted inside the agent. The container can call the selected model endpoint. Sharing an endpoint with a live agent is allowed, but can slow that agent and distort benchmark timings. Prefer running while the agent is not using that endpoint when practical. The benchmark does not stop the live agent or require a reserved endpoint.

The Protagine fixture starts empty facts, affect and commitment stores through its own API lifespan, using the same store classes as the normal server. Facts and affect share the fixture source ledger. No scenario answers are preloaded. These stores are created and closed on the API thread, as their SQLite connections require. Authorization checks alone do not establish backend readiness: image validation must exercise the real provider handlers, including retain/search/read/forget, before a cohort runs. Optional work-context lookups remain outside this memory fixture.

## Declare the comparison

Use one private Hermes provider configuration and one policy file for both arms. The image must already exist and be addressed by its immutable digest. Plan inspects the image in a disposable container with networking disabled; it does not call a model or download an image.

```json
{
  "version": "paired-policy-1",
  "budget_mode": "deployment_policy",
  "budget_policy": {
    "description": "Same declared episode and per-call limits; ordinary auxiliary processing remains enabled.",
    "total_work_enforcement": "not_verified"
  },
  "environment": {
    "endpoint_usage": "shared",
    "hardware_recipe": "Private serving recipe identifier",
    "supporting_models": "All generative roles use the selected candidate; text-only pilot."
  }
}
```

`endpoint_usage` accepts `idle_declared`, `shared` or `unknown`. This is an operator declaration, not measured endpoint isolation. Record hardware, serving recipe and competing traffic honestly. Policy contents, dataset version and content hash, installed implementation, container identity and selected model configuration are frozen into the run identity. Changing them requires a new plan.

`budget_mode` can be `deployment_policy` or `matched_work`. The latter is a declaration, not proof of equal cost: ordinary memory reviews, extraction, judgment, background work and retries must all be observed and bounded before claiming matched total work. Per-call token limits alone do not establish it. Resource totals remain unknown where coverage is incomplete.

## Plan, run and report

```sh
protagine models paired plan \
  --native-config /private/candidate-hermes.yaml --native-binding candidate \
  --comparison-policy /private/paired-policy.json \
  --container-image registry.example/agent-benchmark@sha256:IMAGE_DIGEST \
  --dataset-version paired-agent-reviewed-1 \
  --label candidate-pilot-01 --output /private/results/candidate-pilot-01

protagine models paired run \
  --native-config /private/candidate-hermes.yaml \
  --comparison-policy /private/paired-policy.json \
  --container-image registry.example/agent-benchmark@sha256:IMAGE_DIGEST \
  --output /private/results/candidate-pilot-01

protagine models paired report --output /private/results/candidate-pilot-01
```

Replace the example image digest with the actual pinned image. `--docker-host` selects an explicitly configured local or forwarded Docker daemon. There is no local-process fallback. Select a bounded subset with `--case-ids` at plan time; it always selects both arms together. Without `--case-ids`, every episode in the selected dataset runs. Without `--dataset-version`, the original `paired-agent-pilot-1` is selected. A run uses the version frozen in its plan.

Fixture JSON, task instructions and artifact checks are versioned in the repository. A changed task or grading contract gets a new dataset version and content hash; it does not replace an earlier result. The reviewed dataset includes stricter output contracts and checks of both successful and incorrect artifacts. It is still public development data, so improvements measured here need separate held-out validation.

The supplied model URL must be reachable from the container network. A loopback URL names the container itself, not the machine running the CLI or a remote Docker daemon. Use the endpoint's reachable address; the benchmark does not change model listeners or production routing. [Image build instructions](../benchmarks/paired/README.md) describe the pinned source exports and dependencies.

Execution is sequential. The first episode runs base Hermes first, the second runs Protagine first, and the order continues alternating. An episode has exactly one attempt per arm. There is no best-of selection or adaptive retry. `--resume` continues only untouched attempts. Interrupted attempts retain their outcome and are never replayed. Unconfirmed container cleanup stops further execution.

Diagnostic images record bounded private model requests/responses, native turn messages and completion flags, plus context-route statuses in `private-trace.jsonl` next to each attempt's container log. Request headers and configured credentials are excluded or redacted. Each attempt allows 8 MiB total and 512 KiB per event; `container-result.json` records truncation, dropped events and capture errors. Traces are outside the scored workspace and are never included by the public exporter. These observations can affect timing slightly; compare using the same pinned diagnostic image in both arms. Preserve synthetic-only input and private filesystem access when inspecting them.

The isolated single-owner API credential includes the existing `api:access` scope needed by the memory provider's context tools. This changes no live-agent grants or API authorization rules.

The private `paired.json` contains the frozen plan. Ordinary immutable runner records live under `runs/`; numbered reports are additional views and never replace an earlier result. Keep this directory private: it can contain supplied configuration hashes, model outputs and diagnostics.

## Read the result

The report shows each arm's completion counts, separate unsupported/error/timeout outcomes, paired wins/ties/losses and completion delta in percentage points. A win means Protagine completed an episode that baseline Hermes did not. A tie can mean both succeeded or both failed; those counts are also separate.

An aggregate delta is available only after every declared episode has two attributable outcomes. Missing, interrupted, unsupported, setup-failed or unattributed attempts keep it unavailable. Infrastructure, consumer and verifier errors also remain unavailable: a broken harness is not a model failure. A returned, attributable episode that fails its task checks counts as noncompletion. A timeout counts as noncompletion only when execution evidence shows dispatch to the declared candidate without fallback; otherwise attribution is unknown. Every error and timeout remains visible in its own category. A partial cohort is never promoted into an improvement score. Reports label this attribution policy `paired-attribution-2`; earlier raw attempts remain unchanged.

Accounting reports measured model calls, input/output tokens, background calls and arm wall time when available. Partial subtotals are labeled; missing observations are not zero. The first pilot does not claim complete auxiliary-call accounting, enforced equal compute, peak throughput or latency without competing traffic.

Request timing reports first generated output (including reasoning or tool payload), first nonreasoning text, complete request duration and request output tokens per second. Empty role frames are not first tokens. Output throughput uses provider-reported completion tokens divided by full request time, including queueing and prefill; it is not isolated decode speed. Each metric includes its observed and eligible sample counts. Old receipts without the current timing observer remain unmeasured. Episode wall time also includes tools, settling and container cleanup.

## Publish a result

Author a separate public metadata JSON document with `publication_scope: "public_synthetic"` and a `deployment` object containing `id`, `model` and `profile`. Optional deployment fields describe weights, hardware and the serving recipe using the ordinary qualification publication contract. Do not reuse a private endpoint configuration as metadata.

```sh
protagine models paired export \
  --output /private/results/candidate-pilot-01 \
  --metadata /private/public-deployment.json \
  --public-output /private/publication/paired-candidate-01
```

The new snapshot contains a hash-pinned `index.json` and an allowlisted public result in `runs/`. It can be staged under a website's `benchmarks/paired/` directory. Export verifies repository-owned fixture inputs and includes scalar outcomes, resource counts, timings and runtime hashes. It excludes conversation bodies, agent artifacts, endpoint URLs, private policy text and exception logs. Existing snapshots are never overwritten. Original pilot grading is flagged under review and its aggregate score is withheld; controlled fixtures also cannot become a public model score.

No model tier is assigned. These public development episodes do not establish broad generalization, hidden-test performance, concurrent-session correctness, comprehensive authorization, deletion from every store, executable code correctness, or voice/vision/embedding quality. Inspect the per-case limitations and effects alongside the aggregate. The earlier endpoint screen and its raw records remain separate protocols.
