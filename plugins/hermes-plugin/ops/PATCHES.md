# Hermes patch tools

Use `protagine hermes prepare` for supported, versioned patches against official
Hermes source. See [runtime compatibility](../../../docs/HERMES-CAPABILITIES.md).
The older `hermes-patch-runner.py` only inventories deployment-local scripts:

```bash
python hermes-patch-runner.py status --json
```

It exits `0` when that local registry is empty and never executes a script. It
does not inspect or reject the packaged compatibility layer.
