# Public benchmark results

Qualification evidence is private by default. The optional exporter creates a
new immutable publication snapshot containing counts, outcomes, bounded timing
measurements and explicitly authored public deployment metadata. It never copies
configuration, prompts, oracles, answers, observations, exception strings or
reasoning traces. Runs containing private cases are rejected.

Prepare a private JSON input list, with one entry per ordinary qualification
run directory. Batch runs use the child paths declared in `benchmark.json`.

```json
[
  {
    "directory": "/private/results/candidate/runs/standard-001",
    "metadata": {
      "publication_scope": "public_synthetic",
      "phase": "screen",
      "deployment": {
        "id": "example-stock-recipe-1",
        "model": "Example model",
        "source_url": "https://huggingface.co/example/model",
        "revision": null,
        "variant": "stock",
        "quantization": null,
        "hardware": null,
        "node_count": null,
        "runtime": null,
        "runtime_version": null,
        "context_limit": null,
        "concurrency": null,
        "profile": "practical",
        "weights_verified": false
      }
    }
  }
]
```

The metadata is intended for publication. Write a public description rather than
copying a live configuration file. Missing deployment observations stay `null`.
A verified-weight declaration requires a revision; the operator must establish
the weight identity independently. An API model label is insufficient evidence.

```sh
python -m protagine.qualification.public_export \
  --source /private/publication-inputs.json \
  --output /private/publication-snapshot-001
```

The output contains `manifest.json` and `runs/*.json`. It can be served as static
files under `/benchmarks/data/`. `--benchmark` accepts a separate public JSON
description with `id`, `version`, `title`, `planned_scenarios` and
`methodology_version`. No database, remote publication or production mutation
is performed. Publish only after reviewing the generated snapshot.

Each exported record represents one role and consumer boundary from one source
run. Manifest counts count these result records, not independent models or
scenario repetitions. Outcome counts retain unsupported, setup-error, timeout,
interrupted and unrun cases. A semantic pass with unverified primary attribution
is not counted as a verified model pass. Controlled fixture runs remain labeled
and must not be ranked as measured inference performance.

Comparisons are disabled by default. An optional `comparison_protocol` declares
exactly `id`, `budget_policy`, `supporting_models` and `hardware_policy`. The
exporter hashes this declaration together with the actual selected cases,
evaluators, consumer implementation, runtime, phase, role and boundary. It does
not publish the protocol body. The declaration must describe the fixed
conditions actually used; a matching hash is a grouping aid, not proof that the
operator used equal hardware or observed all serving conditions.

Keep prompt-only and explicit JSON-schema screening in separate comparison
cohorts. The latter uses `agent-screen-2-structured-output`, retains the original
semantic oracles and requires a serialized schema witness. Its new scores do
not replace historical grades or show a change in model weights. Publish the
entire 18-case standard selection, including failures and unsupported cases;
see [screening output contracts](qualification-screen-output-contracts.md).

This scalar exporter deliberately does not publish synthetic response examples
yet. Such examples need a separate explicit fixture publication path. Do not
relax the exporter into mirroring raw result logs to add examples.
