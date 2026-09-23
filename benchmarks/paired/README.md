# Paired agent runtime image

Build this image once and pass its digest to `protagine models paired`. Both arms
use that exact image. Protagine is installed but disabled in the Hermes baseline.
The launcher creates a disposable container for every arm of every episode; it
does not attach to an existing agent or mount its configuration or memory.

Create a build context containing these paths:

```text
Dockerfile           copied from this directory
requirements.lock    copied from this directory
hermes/              source export of one recorded Hermes Git revision
protagine/           source export of one recorded Protagine Git revision
```

Use Git source archives, not deployment directories. For example, after making
the empty directories, export each chosen revision with `git archive REVISION`
and extract it into its corresponding directory. Record both revisions with the
serving recipe. No `.hermes` directory, `.env`, credential file or production
database belongs in this build context.

```sh
docker build --tag protagine-paired:local /path/to/build-context
docker image inspect protagine-paired:local --format '{{.Id}}'
```

Use the returned `sha256:...` as `--container-image`. The runner never resolves a
moving image tag, builds an image or pulls one during a scored run. A registry
image addressed as `registry/name@sha256:...` also works once installed locally.
`BASE_IMAGE` can be supplied as an immutable Python 3.12 base image reference at
build time. The final image identity and installed runtime/dependency inventory
are recorded either way.

The build removes case fixtures, graders and test suites from the agent image.
The external controller retains them and sends only the chronological input
events and workspace files. The model cannot access the expected answers through
the agent's file tools. The only part of `benchmarks/` kept in the image is
`paired/capture_platform/`, the benchmark-only Hermes platform plugin that the
worker copies into every disposable profile.

The initial runtime profile exercises text interaction, native memory and session
search, Protagine automatic recollection, source projection and native memory
tools. It does not start gateways or connect real channels: outbound messages
land in the capture outbox, inbound contact messages are episode events, and the
body tick (cron `tick()` and kanban `dispatch_once`, with workers run in-process)
fires only when an episode declares `tick` events. It does not claim coverage of
the entire autonomy stack. The same native file tools and planning tool are
available in both arms. Embedding, reranking, vision and speech need separately
declared profiles before those capabilities can be scored.

Agent state is isolated; inference capacity is not. Running against an endpoint
also used by a live agent can slow it down and distort benchmark timings. Prefer
a period when the agent is not using that endpoint. Shared endpoints remain
allowed and are labeled in the result. No agent is stopped automatically.

See [the benchmark commands and result interpretation](../../docs/PAIRED-AGENT-BENCHMARK.md).
