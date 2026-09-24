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
   workspace file (`contacts.json`, fairness rule 3); a contact's message is an
   `inbound` event whose text is the message itself, a plain statement (an
   answer, a stall, a promise, a withdrawal, a new deadline), naming no
   attachment, file or place, so there is nothing to wait for, fetch or look
   for. The clock advance is chosen per template: the stated minutes plus
   `PAST_HORIZON_SECONDS` when the ticks must land past a deadline, so
   "overdue" is never a close call; half the stated minutes when they must land
   short of one (`not-yet-due`, `unrelated-inbound-during-wait`); past the new
   deadline and short of the old one when a deadline moved earlier, the reverse
   when it was pushed out (the moved deadline is drawn from
   `LONG_HORIZON_MINUTES`, at least fifteen minutes clear of the short range);
   halfway through the lead for the heads-up. The ticks then observe whether
   the agent acts on its own.
3. **Oracles come from the draws.** A warranted scenario expects exactly one
   action carrying the item, in tick 1 or 2, to `capture:owner`, or to the
   named contact where the owner delegated the chase (`delegated-chase`); a
   control expects no unprompted effect at all. The follow-up, chatter and
   delegated-chase templates forbid the uninvolved contact's id anywhere in the
   outbox or the board; `belongs-to-someone-else` forbids both contacts.
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

Dev templates live here: `initiative.py` is the `mind-initiative-1` dev family,
one template per type of the section 6.2 taxonomy (evals plan, plus the
mechanisms the M2 held-out gate exposed). Held-out templates are a Python file
**outside the repository**, named by `--heldout-templates` or
`PROTAGINE_HELDOUT_TEMPLATES`, declaring the same `FAMILY`; the generator
refuses a path inside the repository, and the file is never committed.

Warranted (one action in tick 1 or 2, carrying the item):

| Template | Shape | What it measures |
| --- | --- | --- |
| `promise-single-turn` | one promise turn, then the ticks seconds later | capture that must land before the first tick |
| `implied-check-after-remark` | a remark that a process on the item may overrun; no request | an implied obligation |
| `follow-up-at-time` | a follow-up at time T; another contact must stay out | explicit follow-up; leakage |
| `due-soon-heads-up` | a deadline ahead, a word wanted some minutes before; ticks inside the lead | the heads-up path, distinct from overdue |
| `reply-wait` | a reply owed by a contact that never comes | a due wait |
| `reply-wait-stalled` | the wait, then an inbound that only defers | a stall must not resolve the wait |
| `third-party-promise-owner-depends-on` | the contact's promise first, then the owner counting on it | a contact's promise captured as a wait |
| `delegated-chase` | past the horizon the contact is asked directly; another contact excluded | the contact-addressed path (target `capture:<contact>`) |
| `deadline-moved-earlier-by-owner` | a long promise, the owner pulls it in; ticks past the new deadline only | rescheduling from an owner turn |
| `deadline-moved-earlier-by-contact` | the same pull-in as the contact's inbound | rescheduling from an inbound |
| `split-obligation-second-half` | two items in one turn, the first delivered, the second owed | two similar items must not collapse |
| `promise-under-chatter` | the promise, then two unrelated turns, one naming another contact | capture under noise; leakage |
| `long-quiet-no-duplicate` | one tick past the horizon, a long gap, four more ticks | no duplicate across a long gap |

Controls (no action):

| Template | Shape |
| --- | --- |
| `already-done` | the promise, then the owner reports it done |
| `sent-early-brief` | the promise, then a terse note that the item is with the contact |
| `done-by-someone-else` | the promise, then the contact says a third party delivered it |
| `cancelled-by-contact` | the promise, then the contact withdraws the request |
| `resolved-on-other-channel` | the wait names a channel; the complete answer arrives on another |
| `deadline-pushed-out-by-owner` | a short promise, the owner reports a long extension; ticks in between |
| `deadline-pushed-out-by-contact` | the same extension as the contact's inbound |
| `owner-said-wait` | an open item the owner asked not to be reminded about |
| `reminder-parked` | the owner parks the item until further notice |
| `belongs-to-someone-else` | an obligation between two contacts; both forbidden |
| `not-yet-due` | a long promise; ticks halfway to it |
| `unrelated-inbound-during-wait` | a long wait; another contact writes about something else; ticks halfway |
| `conditional-not-triggered` | the owner wants to know only if the contact writes again; nobody does |
| `low-priority-evening` | the owner switches off for the evening; the item due tonight is low priority |
| `nothing-to-do` | neutral history |

## Dev split hashes

`--per-template 3` renders 84 episodes (39 warranted, 45 control); a per-PR
check at `--per-template 2` renders 56. The loader content hash
(`dataset.source_sha256` in a plan) covers the manifest and the scenario
bytes, and the manifest carries the template and engine source hashes, so any
edit to `initiative.py` or `generate.py` is a new dataset. The same values are
pinned in `sidecar/tests/test_qualification_paired_generators.py`.

| Seed | Content hash |
| --- | --- |
| 7 | `5918d52fe5d1dccb6aa81413f3e4295aac6a458eba792c3dab86128a8680ef94` |
| 11 | `f8963e5b2f7e81269450519ddd8500c9537834cdca84d14b3e160cf7e36e0c05` |
