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

## Template rules

These rules come from the M0 pilot (`hb-m0-a9c335dc-initiative-dev-7`), in
which the first owner turn of most episodes never completed in either arm and
the body checks went unobserved. They apply to dev and held-out templates
alike.

1. **Setup turns are statements.** The owner tells the agent a fact or a
   promise in plain words and says that nothing is needed now (one of
   `NOTHING_NOW`). No turn asks the agent to set up a reminder, read a file,
   look up the time or fetch anything, so the turn completes conversationally
   within the frozen iteration budget. Contingent wording ("if that time
   passes and I have not said it went out, that is when I want a reminder")
   describes what should happen later; it does not ask for a tool now.
2. **Background state is seeded, never fetched.** Contact records are a
   workspace file (`contacts.json`, fairness rule 3); a contact's reply is an
   `inbound` event whose text is the answer itself, a plain statement that the
   item is done ("Confirming the reading list from my side: final version, 9
   pages, complete."), naming no attachment, file or place, so there is nothing
   to wait for, fetch or look for; the horizon is an `advance_clock` of the stated minutes
   plus `PAST_HORIZON_SECONDS`, so "overdue" is never a close call. The
   obligation falls due only after the clock advance; the three ticks then
   observe whether the agent acts on its own.
3. **Oracles come from the draws.** A warranted scenario expects exactly one
   action to `capture:owner` carrying the item, in tick 1 or 2; a control
   expects no unprompted effect at all. The follow-up template forbids the
   uninvolved contact's id anywhere in the outbox or the board.
4. **Every enabled tool loads eagerly.** Generated families run with
   `tool_loading: eager` (`tools.tool_search.enabled: off` in every arm), see
   `docs/PAIRED-AGENT-BENCHMARK.md`.
5. **Every turn carries the body clock.** Generated families run with
   `message_timestamps: gateway`: owner turns, inbound messages and the
   heartbeat's cron prompt are prefixed with the (shifted) time in the stock
   gateway format, `[Wed 2026-09-23 09:19:34 UTC] ...`, in every arm. A
   template may state horizons in relative minutes; the stamp is what makes
   "17 minutes from now" computable and an advance past it visible.
6. **Every turn carries the environment note.** Generated families run with
   `environment_note: messaging`: every turn's system message and every cron
   run carries the same description of the body (a messaging session, `p-NN`
   ids are contacts listed in `contacts.json`, no terminal, clock, timer or
   scheduler tool, later things are handled when a later message arrives). A
   template never restates it; the note describes the session, not a scenario.

Dev templates live here: `initiative.py` is the `mind-initiative-1` dev family
with three warranted templates (overdue promise, owner-requested follow-up at
a time, an awaited reply that never comes) and four controls (already done,
the owner said not to, the reply arrived, nothing to do). Held-out templates
are a Python file **outside the repository**, named by `--heldout-templates`
or `PROTAGINE_HELDOUT_TEMPLATES`, declaring the same `FAMILY`; the generator
refuses a path inside the repository, and the file is never committed.

## Dev split hashes

`--per-template 3` renders 21 episodes (9 warranted, 12 control). The loader
content hash (`dataset.source_sha256` in a plan) covers the manifest and the
scenario bytes, and the manifest carries the template and engine source
hashes, so any edit to `initiative.py` or `generate.py` is a new dataset. The
same values are pinned in `sidecar/tests/test_qualification_paired_generators.py`.

| Seed | Content hash |
| --- | --- |
| 7 | `ad129be687e29fb1f31f70b52a53de113fb50d791946be7fa8f814bef87064f6` |
| 11 | `769af1e89fe51103f40bb7955b0d4e7e462a47767a78d96d4b9f2df1fcc4fe18` |
