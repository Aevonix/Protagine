# Release artifact qualification

The adapter and the sidecar are published together under one version. The
changelog states which components change in each release.

`requirements/release-py312.txt` records the dependency versions used for the
Python 3.12 Linux artifact checks. It covers the core sidecar, adapter with native
memory support, and build/quality tools. It is an installation constraint,
not a replacement for package dependency ranges. The selected Hermes runtime
and optional graph/vector/video stacks have their own qualification jobs.
This snapshot does not establish compatibility with every declared lower bound,
another Python/platform combination, or a live deployment.

In a disposable Python 3.12 environment, from the repository root:

```sh
python -m pip install -c requirements/release-py312.txt '.[test,quality]'
python -m build --no-isolation --outdir dist .
python -m build --no-isolation --outdir dist ./sidecar
python -m twine check dist/*
python scripts/qualify_artifacts.py --dist dist \
  --constraints requirements/release-py312.txt --output artifact-qualification
```

Use an empty `dist` directory for a candidate. Each default build creates an
sdist and builds its wheel from that source archive. The qualifier installs
the sidecar and adapter in a new environment outside the checkout, checks
dependency consistency, installed import locations and entry points, and
exercises CLI help. It does not attach to a Hermes profile, run inference,
or mutate deployment state.

`artifact-qualification/receipt.json` contains component versions, wheel/sdist
hashes, the constraints hash, Python version and resolved package inventory.
The CI receipt also identifies the source commit. Package inventories contain
names/versions, not pip credentials or private direct-install URLs. Keep this
receipt with the artifacts. It supplements the native behavior tests rather
than replacing them.

CI passes these same distributions to the native qualification jobs through
`PROTAGINE_DISTRIBUTIONS_DIR`. Local tests build artifacts as before when that
variable is absent. The tag-triggered release calls the existing CI workflow
on that exact tag commit, waits for all jobs, and downloads its distributions
for publication. The tagged container build is likewise retained and published
without a second build. A failed or skipped qualification prevents publication.

To refresh constraints, resolve the declared packages in a fresh supported
environment, retain third-party package names/versions (exclude local project
paths), then rerun artifact and native qualification. Commit the candidate
snapshot with the observed results; do not regenerate it during a release.
The paired benchmark's separate lock and frozen images remain unchanged.

CI checks fatal Python errors throughout the sidecar, the adapter and the
artifact qualifier, plus the packaging tests it changes. This adds no mass
formatting change, global typing migration, or arbitrary coverage threshold.
