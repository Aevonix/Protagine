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

Dev templates live here; every `*.py` module beside `generate.py` is a family,
selected by its stem (`--family initiative`, `--family drives`, `--family
people`, `--family affect`). Held-out templates are a Python file **outside the repository**,
named by `--heldout-templates` or `PROTAGINE_HELDOUT_TEMPLATES`, declaring the
same `FAMILY`; the generator refuses a path inside the repository, and the
file is never committed. Every family's arms are built-in profiles of the
harness (`paired.PROFILES`, `docs/PAIRED-AGENT-BENCHMARK.md`, "Arms and
profiles"); no family ships a `--profiles` file.

- `initiative.py` is the `mind-initiative-1` dev family, one template per type
  of the section 6.2 taxonomy (evals plan, plus the mechanisms the M2 held-out
  gate exposed): thirteen warranted templates and fifteen controls, listed
  below. Its oracle is `body.action`.
- `drives.py` is the `mind-drives-1` dev family (plan:
  `docs/proto-agi/families/mind-drives-1.md`). Four `selection` templates seed
  N opportunities across the drives (an overdue promise, a due reply wait, a
  repeated failure, a red check, an idle interest) and open K < N ticks; the
  oracle `body.selection` expects the top K of the scenario's own priority
  order and nothing after `stop_after` (a settling owner turn, or the owner's
  `/mind off`); the control resolves every opportunity before the horizon.
  Two `goal` templates seed an interest or a failure cluster whose answer sits
  in a workspace file plus a distractor assigned elsewhere; the oracle
  `body.goal` wants the right goal worked on and the distractor never, and the
  success check is a JSON `artifacts` oracle over the report the goal writes.
  Its arms are `full`, `full-drives` (the flat-priority ablation),
  `full-broadcast` and the per-drive `full-<drive>` diagnostics.
- `people.py` is the `mind-people-1` dev family (plan:
  `docs/proto-agi/families/mind-people-1.md`): five identity templates graded
  on the reply to a contact's later message (a first contact, the same handle
  on a second channel, a same-name pair that must not merge, an
  owner-confirmed merge, a per-person naming preference), three warranted
  check-ins graded on sends to the contact (a due cadence, a check-in under an
  owner-only canary, backoff after two ignored check-ins) and six controls
  (not due, satisfied by a conversation, a `never` contact, an opt-out,
  permission not granted, a group of unknown members). Its `contacts.json`
  records carry `channel`, `address` and `may_contact` (`auto`, `ask`,
  `never`), plus `cadence_minutes` and a display `name` where the scenario
  needs them. Its oracles are `body.sends` and `body.replies`; its arms are
  `base-heartbeat`, `full` and `full-people`.
- `affect.py` is the `mind-affect-1` dev family (evals section 6.4, build plan
  M6; plan: `docs/proto-agi/families/mind-affect-1.md`): six treatment
  templates in which a cause should change a decision, and seven controls in
  which the same shape carries the cause absent, decayed or resolved (or, for
  duty, must not be changed by it). The history that should move the agent's
  own affect (what failed and when, what is open, what was waved off, what is
  due soon) arrives as owner statements under the same rules as above; the
  clock advances and the body ticks; then one decision is observed. Ten
  templates end in a **decision turn** in a fresh session (`owner-2`) whose
  work needs only the file tools every arm has: the agent writes a small JSON
  file, graded by the existing `json` artifact checks (`keys_equal`, `number`,
  `label_one_of`). Three satiation templates have no decision turn and are
  graded on the ticks by the `body` oracle; `aggregate-one-cause` carries
  both. The consumer each template exercises is `CONSUMERS` in the module (its
  name's prefix). Its arms are `full`, `full-affect` (built in) and the
  mechanism arm `full-affect-plus-rules`, the one arm a family still declares
  in a file (`affect_profiles.json`, `--profiles`) because `mind.affect_rules`
  does not exist yet.

Decision-turn rule (affect): the decision turn is the only turn that asks for
work, it comes last, it restates the standing default in neutral words (which
export is the usual one, which items are on the table) so the default is
computable without memory, and the history alone decides whether the default
stands. The oracle is computed from the same draws: the figure in the export
the history makes right, or the item the history makes first.

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

```sh
python benchmarks/paired/generators/generate.py --family affect \
  --split dev --seed 7 --per-template 3 --output /private/families/affect-dev-7
```

`affect.py` is the `mind-affect-1` dev family (evals section 6.4, build plan
M6): six treatment templates in which a cause should change a decision, and
seven controls in which the same shape carries the cause absent, decayed or
resolved (or, for duty, must not be changed by it). The history that should
move the agent's own affect (what failed and when, what is open, what was waved
off, what is due soon) arrives as owner statements under the same rules as
above; the clock advances and the body ticks; then one decision is observed.
Ten templates end in a **decision turn** in a fresh session (`owner-2`) whose
work needs only the file tools every arm has: the agent writes a small JSON
file, graded by the existing `json` artifact checks (`keys_equal`, `number`,
`label_one_of`). Three satiation templates have no decision turn and are graded
on the ticks by the existing `body` oracle. `aggregate-one-cause` carries both.
The consumer each template exercises is `CONSUMERS` in the module (its name's
prefix). The arm profiles the family compares are
`affect_profiles.json` (`--profiles`): `full`, `full-minus-affect` and
`full-minus-affect-plus-rules`, flag overlays on the same plugin-on, mind-on
body; see `docs/proto-agi/families/mind-affect-1.md`.

Decision-turn rule (affect): the decision turn is the only turn that asks for
work, it comes last, it restates the standing default in neutral words (which
export is the usual one, which items are on the table) so the default is
computable without memory, and the history alone decides whether the default
stands. The oracle is computed from the same draws: the figure in the export
the history makes right, or the item the history makes first.

## Dev split hashes

The loader content hash (`dataset.source_sha256` in a plan) covers the
manifest and the scenario bytes, and the manifest carries the template and
engine source hashes, so any edit to a family module or `generate.py` is a
new dataset for that family (an engine edit re-pins every family). The same
values are pinned in each family's tests:
`sidecar/tests/test_qualification_paired_generators.py` (initiative),
`sidecar/tests/test_qualification_paired_drives.py` (drives),
`sidecar/tests/test_qualification_people_family.py` (people) and
`sidecar/tests/test_qualification_paired_affect_family.py` (affect).

`initiative`, `--per-template 3`: 84 episodes (39 warranted, 45 control); a
per-PR check at `--per-template 2` renders 56. `drives`, `--per-template 3`:
18 episodes (12 selection, 6 goal). `people`, `--per-template 2`: 28 episodes
(10 identity, 6 warranted, 12 control). `affect`, `--per-template 3`: 39
episodes (18 treatment, 21 control).

| Family | Per template | Seed | Content hash |
| --- | --- | --- | --- |
| initiative | 3 | 7 | `fc5c9247c8deb1839c226890b6ad5f4f76b00f66b4351b037a6e27c58f1a78c0` |
| initiative | 3 | 11 | `af468891bd76abcf52a1e0c3dc0e4ca35c0a98ba796350c76ad82faa4da09cb9` |
| drives | 3 | 7 | `22667ffd5881f48678a1ebe9d654a43b24224d4529b8c1c93e733c1ef77da110` |
| drives | 3 | 11 | `d34d80fa26b4e6f2bc2feb3ed307b73dbac0b3259ace9a4c25ab997a9b211e65` |
| people | 2 | 7 | `e2b31d4a9f8662de3ef491793d5d564f0f0eea0d408b181effee6ba6b71f8c28` |
| people | 2 | 11 | `b9d904b7df7981db589a074db0b192b2321fcfb08196db53ea6eed97c26b0405` |
| affect | 3 | 7 | `e3f2c4ada62ac31b6f9f76b1700de3f3fd089e85a8af74b9d70dce68a3dc35ea` |
| affect | 3 | 11 | `92ffd601e0d480b5aa5e4bd53e14ddf58a257f27e3e16fddb3b2fae71b198b1e` |
