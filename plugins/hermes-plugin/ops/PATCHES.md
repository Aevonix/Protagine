# Hermes patch tools

Use `protagine hermes prepare` for supported, versioned patches against official
Hermes source. See [runtime compatibility](../../../docs/HERMES-CAPABILITIES.md).

`protagine hermes check RUNTIME` verifies a prepared runtime and its required
capabilities. Update qualification checks exact patch inputs and runs native
regressions before a new Hermes revision becomes an install target.
