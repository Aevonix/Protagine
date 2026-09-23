# Seeded scenario generators

`generate.py` renders a family's templates into a dataset directory in the
frozen fixture shape (`manifest.json` and `scenarios.json`), byte-hashed the
way the loader hashes it. The same seed always gives the same bytes.

```sh
python benchmarks/paired/generators/generate.py --family initiative \
  --split dev --seed 7 --per-template 3 --output /private/families/initiative-dev-7
```

Then plan against it with `protagine models paired plan --dataset-dir
/private/families/initiative-dev-7 --arms base-heartbeat,protagine ...`. The
directory's content hash is frozen into the plan.

A family module declares `FAMILY` (the dataset id), `TEMPLATES = {name:
(group, render)}` and optionally `ROLE`. `render(draw)` returns
`initial_files`, `episodes` and a `body` oracle (or `artifacts`), computed from
the same draws: `draw.pick`, `draw.picks`, `draw.integer` and `draw.contact()`,
which yields fixed-width `p-01`..`p-99` ids, distinct within one instance.
Contacts, items and phrasings are synthetic; there are no names, hosts or
channels beyond the capture platform.

Dev templates live here: `initiative.py` is the `mind-initiative-1` dev family
with warranted and control templates, including nothing-to-do controls.
Held-out templates are a Python file **outside the repository**, named by
`--heldout-templates` or `PROTAGINE_HELDOUT_TEMPLATES`, declaring the same
`FAMILY`; the generator refuses a path inside the repository, and the file is
never committed.
