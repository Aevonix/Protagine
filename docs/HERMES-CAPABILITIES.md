# Hermes capability contract

The **1.9.0rc1 release candidate** installs the required Hermes interfaces from a
versioned patchset shipped with Protagine. Installation uses official Hermes
source. It does not require a fork or acceptance of an upstream pull request.
These commands are new in this candidate, not the published 1.8 series.

The first patchset, `hermes-0.21.3-protagine-1`, targets official Hermes **0.21.3**,
tag `v2026.9.14`, commit
[`345cd2b057a452236de401d3534b8502a7465e8d`](https://github.com/NousResearch/hermes-agent/commit/345cd2b057a452236de401d3534b8502a7465e8d).
Unmodified stock 0.21.3 lacks required core interfaces. A matching version number
alone does not establish compatibility.

## Prepare and select a runtime

```bash
protagine init --prepare-hermes
```

This prepares official source plus the packaged patches in a separate environment,
then attaches the selected profile. Passing an existing official interpreter with
`--hermes-python` also prepares that candidate when required core interfaces are
missing. The source checkout must match the qualified official revision and have
no tracked modifications. An unsupported revision needs a new qualified patchset.

To prepare and inspect a candidate separately:

```bash
protagine hermes prepare --source /path/to/official/hermes --destination /path/to/new/runtime
protagine hermes check /path/to/new/runtime
protagine init --hermes-python /path/to/new/runtime/.venv/bin/python
protagine --instance /path/to/private/state hermes run gateway run
```

Omit `--source` to fetch the qualified revision from NousResearch. Preparation
checks exact patch bytes and file preimages, applies into a new directory, verifies
postimages, installs native dependencies and probes the result. It excludes
untracked files and private profiles. Existing source and running processes stay
in place. Reuse verifies the candidate's source, interpreter and patchset binding.

For a managed deployment, select the prepared interpreter through its existing
service lifecycle after validation. Preparation does not restart a gateway or
silently replace a service command. See [local setup](LOCAL-HERMES-SETUP.md).

## Required core and optional features

| Group | Native contract |
| --- | --- |
| Core | Plugin callback registration and caller context; memory-provider discovery and pre-compression checkpoint v2; request/tool authority middleware |
| Core | Exact persisted current-user row supplied to request middleware |
| Core | Selected payload erasure with preimage/watermark checks and replay; post-persistence native settlement observer |
| Core | Durable task creation, duplicate admission/claim rejection, reopen and stale-claim recovery |
| Concurrent work | Overlapping callbacks retain each caller; gateway-settled observer |
| Detached review | Observer-only detached completion |
| Source reminders | Exact-job output snapshot, fingerprint and erasure |
| Terminal handoff | Typed `FinishTurn` after a completed tool batch |

New setup and adapter refresh require core. Native local work, task dispatch and
operational review also require concurrent work; operational review requires
detached completion. Setup does not silently disable a requested feature to make
its runtime pass. The first patchset supplies all five groups. Features still need
their normal profile configuration; installing an interface does not enable them.

## Inspect without installation

```bash
python -m protagine.hermes_capabilities \
  --python /path/to/hermes/.venv/bin/python \
  --output hermes-capabilities.json --require core
```

The output path must be new. Repeat `--require` for optional groups. Probes use a
disposable profile, synthetic SQLite records, no inherited credentials and blocked
outbound socket connections. They do not run a model or inspect conversations.

The receipt records available interfaces and bounded behavior. Setup retains it
in the private `instance.json` with the selected interpreter and adapter binding.
Attachment remains `configured_not_behaviorally_verified` until its actual loops
are observed. A declared hook does not prove correct delivery or useful recall.

## Qualification and updates

[Release CI](../.github/workflows/ci.yml) checks out the exact official base,
applies patch bytes from the built Protagine wheel and runs the installed-adapter
suite. It also runs the packaged native regression files in separate processes,
as Hermes requires. Tests cover source capture, participant isolation, correction
and forgetting, task outcomes, overlapping work and the optional interfaces.

The initial patchset passed all five capability groups, 358 native tests plus
10 subtests, and 18 installed-adapter memory, reminder, task and review checks.
The complete release CI result remains the release gate. These controlled tests
do not establish model quality, physical channel delivery or production health.

The [daily compatibility job](../.github/workflows/hermes-upstream.yml) fetches the
latest official stable revision and checks it against the packaged contract. An
unknown revision fails with a requirement to qualify a new bundle. There is no
fuzzy application, automatic downgrade or live update. For each supported update,
review upstream changes, regenerate only needed patches and pass both native and
installed-adapter tests before switching a deployment.

Upstream contributions can reduce future patch maintenance. Their acceptance is
not a release dependency. Remove a patch when the new official source passes its
behavioral witnesses without it.

The [patch manifest](../sidecar/protagine/hermes_patchsets/hermes-0.21.3-protagine-1/manifest.json)
records exact source and file hashes. The [compatibility guide](HERMES-HOOK-COMPATIBILITY.md)
explains the interfaces. The older [stock failure inventory](hermes-upstream-failures.json)
and [commit inventory](hermes-runtime-inventory.json) retain the evidence behind
this bundle; their historical fork pins are not installation requirements.
