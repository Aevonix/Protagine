# Protagine host worker

The stdlib-only action contract, capability catalog, execution ledger and
conformance checks used by Protagine host adapters. This package has no sidecar or
web-server dependency.

Install `protagine-hostworker` in a fresh environment and import
`protagine_hostworker`. Its canonical action names and serialized contract
identifiers must match the configured Protagine endpoint.

Run `python -m protagine_hostworker.conformance` to check a host implementation.
