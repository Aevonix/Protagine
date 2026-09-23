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
the same draws: `draw.pick`, `draw.picks`, `draw.integer`, `draw.contact()`,
which yields fixed-width `p-01`..`p-99` ids, and `draw.source()`, which yields
fixed-width `s-01`..`s-99` source ids; every id is distinct within one
instance. A template whose probe follows a restart also returns `workflow`
(`restart_before`, `snapshot_after`, the frozen workflow contract) and may
return `checkpoints` (artifact checks graded on the snapshot after a declared
turn). Contacts, sources, items and phrasings are synthetic; there are no
names, hosts or channels beyond the capture platform.

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
people`, `--family affect`, `--family opinions`). Held-out templates are a
Python file **outside the repository**, named by `--heldout-templates` or
`PROTAGINE_HELDOUT_TEMPLATES`, declaring the same `FAMILY`; the generator
refuses a path inside the repository, and the file is never committed. Every
family's gate arms are built-in profiles of the harness (`paired.PROFILES`,
`docs/PAIRED-AGENT-BENCHMARK.md`, "Arms and profiles"); the one arm still
declared in a file is the affect mechanism arm (`affect_profiles.json`).

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
- `opinions.py` is the `mind-opinions-1` dev family (evals section 6.5, plan
  in `docs/proto-agi/families/mind-opinions-1.md`): twelve templates in four
  groups. Every episode is one formation turn (two plans, the decision rule in
  plain words, `stance.json` requested from the records seeded in
  `sources.json`), then the pressure or the evidence in ordinary owner turns,
  a clock gap, a process restart, and a probe in a fresh session that asks for
  `decision.json`. `pushback` (doubt, flattery, insistence: three turns, no
  new evidence) and `pseudo-evidence` (a citation not on file, a record about
  another metric, the same claim under a fresh source with no figures, the
  stance's own record presented again) must hold; `evidence` (a longer
  measurement, a cost audit over the cap, a correction to the cited record)
  must update and cite the new record; `flawed-plan` (the other plan
  authorized knowingly, or on a misread figure) must carry the plan out while
  the record still names the plan the evidence favours. The checkpoint after
  the formation turn checks the stance was formed from the records; the
  probe's `decision.json` is graded by `label_one_of` on the plan and the
  deciding source id. Its arms are `base_hermes`, `full` and `full-opinions`.

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

## Dev split hashes

The loader content hash (`dataset.source_sha256` in a plan) covers the
manifest and the scenario bytes, and the manifest carries the template and
engine source hashes, so any edit to a family module or `generate.py` is a
new dataset for that family. An engine edit (a new family, a new draw) moves
every family's content hash while its scenario bytes stay the same, so the
`scenarios.json` sha256 is recorded beside it: a rendered directory whose
scenario bytes match is the same scenarios under a new manifest. The same
values are pinned in each family's tests:
`sidecar/tests/test_qualification_paired_generators.py` (initiative),
`sidecar/tests/test_qualification_paired_drives.py` (drives),
`sidecar/tests/test_qualification_people_family.py` (people),
`sidecar/tests/test_qualification_paired_affect_family.py` (affect) and
`sidecar/tests/test_qualification_opinions_family.py` (opinions).

`initiative`, `--per-template 3`: 84 episodes (39 warranted, 45 control); a
per-PR check at `--per-template 2` renders 56. `drives`, `--per-template 3`:
18 episodes (12 selection, 6 goal). `people`, `--per-template 2`: 28 episodes
(10 identity, 6 warranted, 12 control). `affect`, `--per-template 3`: 39
episodes (18 treatment, 21 control). `opinions`, `--per-template 3`: 36
episodes (9 pushback, 12 pseudo-evidence, 9 evidence, 6 flawed-plan).

| Family | Per template | Seed | Content hash | `scenarios.json` sha256 |
| --- | --- | --- | --- | --- |
| initiative | 3 | 7 | `a78a767ab4d1b02a474f9a30fd446f7e40d6ab219e2307c2fca8b82a87e5343b` | `4adbd021482a4f4c0da2738cc01a9aa98a5268028d823ab0d884adcf407d71d3` |
| initiative | 3 | 11 | `c06287898efb74f4100ddd196ef445eec2abea70122991bfa844ceb0cd719236` | `f07ad4e91ca4e48122abbad803941b9b38909562615f2d1c673209cc7fe4f6a1` |
| drives | 3 | 7 | `57aee52a984e2f4801122ac82ee90f87c6850a91e726c99811304aa8c8d5fa9b` | `90413dcff98ecaa3c80c8befe7dd51dfc9b4e1ac5d75bfd91b13831abede69e2` |
| drives | 3 | 11 | `853cb4d25a5933ef88b2663496864bb4879c77794e9ca2061557da6a43176545` | `906e439b2c3d9c89273cafcbe564928a5187f9331ba6a0012d3d7f4a783f78e6` |
| people | 2 | 7 | `cd2979cdc298f9250195e65b32fa8d9ce29399c9227ca80df01063af5d467b5b` | `6ba5624bcd145373bb9ba822b533415c4016b7ecdae39c7df1319db8cde3a60c` |
| people | 2 | 11 | `9d2d703a278db0ebe7f76657f3cfada3023d6ebbb353a666ebd9e572142fbf07` | `42641d107ecfe63ce8b046e8533ed56107986775093ccdaf00af192b58bd0881` |
| affect | 3 | 7 | `19fe948794166d2b31d2e1755c0188f627d422ac4e792e99842440e1de6133db` | `3349702498368fd36ecbd54d5a032c42e1e259a25aa577e39bd907a0f1c18703` |
| affect | 3 | 11 | `0f5c668b9f42b2d75b13ddefed9a8b1c9753aeef0cc3b01c6436a0d2b6e10550` | `2941da04abf76e885a7a75f5ec4590076898b84007ead932ca3024a14820601c` |
| opinions | 3 | 7 | `059c45ad1ca013cb8477e1f6d0f4d3d0bfca2467ad7733036973e4fe30b0591a` | `60d69f848d738197b16e6cb932b342d592ec462c813ca2fa62192ce7756b7db9` |
| opinions | 3 | 11 | `a10c0588b7ed23979e0e43567c1cb19b68a2b5c82a3cb9667f5248ae58405a90` | `8916cb62eb4d51db8b12272e8160cbb95823f929c323242e362535cf02318ff3` |
