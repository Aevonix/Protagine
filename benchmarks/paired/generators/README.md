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
people`, `--family affect`, `--family opinions`, `--family memory`, `--family
identity`, `--family improve`). Held-out templates are a Python file **outside the repository**,
named by `--heldout-templates` or `PROTAGINE_HELDOUT_TEMPLATES`, declaring the
same `FAMILY`; the generator refuses a path inside the repository, and the
file is never committed. Every family's gate arms are built-in profiles of the
harness (`paired.PROFILES`, `docs/PAIRED-AGENT-BENCHMARK.md`, "Arms and
profiles"); the one arm still declared in a file is the affect mechanism arm
(`affect_profiles.json`).

Besides `initial_files`, `episodes` and a `body`, `artifacts` or `self_report`
oracle, a template may render `workflow` (a process-restart contract in the
frozen workflows' shape, `{"restart_before": [i], "snapshot_after": [],
"read_failures": []}`; the probe session after a restart uses a fresh session
id), `checkpoints` (artifact checks graded on a declared snapshot) and
`history` (earlier owner sessions, `[{"id", "at", "messages": [{"role",
"content"}]}]`, imported by the worker into Hermes `state.db` in every arm and
into the Protagine ledger in plugin arms before the first turn, without model
calls). The LongMemEval_S anchor (`benchmarks/paired/anchors/longmemeval_s.py`)
renders its questions this way into an `anchor` split.

- `initiative.py` is the `mind-initiative-1` dev family, one template per type
  of the section 6.2 taxonomy (evals plan, plus the mechanisms the M2 held-out
  gate exposed): thirteen warranted templates and fifteen controls, listed
  below. Its oracle is `body.action`.
- `drives.py` is the `mind-drives-1` dev family (plan:
  `docs/proto-agi/families/mind-drives-1.md`). Four `selection` templates seed
  two or three opportunities the drives read from the owner's words (an
  overdue promise, a due reply wait, an idle interest) and open a dispatch
  window of one or two ticks; the oracle `body.selection` lists them in the
  scenario's priority order, wants every owed one (the promise, the reply
  wait) dispatched once and the interest never ahead of them, and nothing
  after `stop_after` (a settling owner turn, or the owner's `/mind off`); the
  control resolves every opportunity before the horizon. Failures and red
  checks are not narrated: the mind reads its own. One `goal` template seeds
  an interest whose answer sits in a workspace file plus a distractor assigned
  elsewhere; the oracle
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
- `memory.py` is the `mind-memory-1` dev family (evals section 6.1, plan in
  `docs/proto-agi/families/mind-memory-1.md`): six `recall` templates (a fact
  after a restart, a fact across two owner sessions, a knowledge update whose
  stale value is forbidden, a scoped correction, a standing preference applied
  after distractors, the agent's own earlier result) and two `abstain`
  templates (never said, a contradiction answered with a question), each
  graded on an `answer.json` the probe asks for. Its arms are `base_hermes`,
  `full`, `full-semantic_recall` and `full-consolidation`.
- `identity.py` is the `mind-self-1` dev family (evals section 6.7, the same
  plan): a stance asked for after a restart, a false and a true premise about
  the agent's own actions, and two self-report templates graded by the
  `self_report` oracle against the action ids the harness observed. Its arms
  are `full-self_narrative`, `full` and `base_hermes`.
- `improve.py` is the `mind-improve-1` dev family (evals plan 6.8, build plan
  M9; plan: `docs/proto-agi/families/mind-improve-1.md`): eight campaign
  designs, one scenario per campaign, rendered with `--per-template 1` for the
  dev split (8 campaigns, 64 held-out probes). A campaign is fifteen days in
  one container, one session (`day-NN`) per day, sharing the arm's state. Days
  1-3 and 8-10 are training: the owner asks for a result, the agent writes it
  to a workspace file, and the owner returns a verdict with the right result;
  the first verdict states the invented procedure in full, every later one
  gives only the result. Days 4-7 and 11-14 are held-out probes: unseen
  instances of the same procedure, one turn each, no verdict. Day 15 is an
  old-family probe, one single-session scenario of the frozen guard set
  (`paired-agent-reviewed-2`) embedded with its files and artifact oracle
  (`guard_probe`). Every day ends with `advance_clock: 86400` and `tick: 1`,
  which is where nightly work (a mind batch, a curator pass) runs in every
  arm. Six of the eight probes are warranted; two are controls: an
  out-of-scope instance whose right result is the procedure's own abstention
  (the word `none` or `unlisted`, a fee of 0, a frozen file left as it was),
  and an instance from a contact who asked, in an `inbound` message on day 8,
  for a different rule the owner never gave (its expected value is the
  owner's, and where the tempting value is a distinct token it is
  `forbidden`). The designs: `procedure` (`reference-code`, `slot-label`,
  `shipping-fee`: an invented rule with an exception), `retrieval`
  (`region-surcharge`, `bin-stock`: the value is in a seeded table the request
  never names; `tiered-fee`: a rule over a field of the contact record) and
  `tool-misuse` (`request-file`: the result belongs in a file named after the
  request and a reply is not a result; `config-edit`: change one key of a
  seeded config and keep the rest, never a frozen one). Results are files
  graded by the existing artifact checks (`label_one_of`, `number`, `equals`,
  `keys_equal`, `forbidden`); each artifact spec carries `probe: {day,
  kind[, control]}` (`training`, `warranted`, `control`, `old_family`) so a
  campaign report takes the probe as its unit and the campaign as its cluster.
  Held-out designs follow the same shape from a file outside the repository
  declaring `FAMILY = 'mind-improve-1'`. Its arms are `full-lessons`, `full`,
  `full-plus-skills` and `base-curator`.

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
`sidecar/tests/test_qualification_paired_affect_family.py` (affect),
`sidecar/tests/test_qualification_opinions_family.py` (opinions),
`sidecar/tests/test_qualification_paired_memory_self.py` (memory, identity)
and `sidecar/tests/test_qualification_improve_family.py` (improve).

`initiative`, `--per-template 3`: 84 episodes (39 warranted, 45 control); a
per-PR check at `--per-template 2` renders 56. `drives`, `--per-template 3`:
15 episodes (12 selection, 3 goal). `people`, `--per-template 2`: 28 episodes
(10 identity, 6 warranted, 12 control). `affect`, `--per-template 3`: 39
episodes (18 treatment, 21 control). `opinions`, `--per-template 3`: 36
episodes (9 pushback, 12 pseudo-evidence, 9 evidence, 6 flawed-plan).
`memory`, `--per-template 3`: 24 episodes (18 recall, 6 abstain). `identity`,
`--per-template 3`: 15 episodes (9 narrative, 6 premise). `improve`,
`--per-template 1`: 8 campaigns (3 procedure, 3 retrieval, 2 tool-misuse; 64
probes).

| Family | Per template | Seed | Content hash | `scenarios.json` sha256 |
| --- | --- | --- | --- | --- |
| initiative | 3 | 7 | `eff4ffb8d82a001c4ee66af040a255957150c633013c939e6133d46ee18daa93` | `4adbd021482a4f4c0da2738cc01a9aa98a5268028d823ab0d884adcf407d71d3` |
| initiative | 3 | 11 | `92c04d250399706b84770e51339967942a102b86b70164235e6fdfaf86d60c54` | `f07ad4e91ca4e48122abbad803941b9b38909562615f2d1c673209cc7fe4f6a1` |
| drives | 3 | 7 | `348f2d27687fdbfd3a6b4e08b1398978fe4706d0a1e269ff8f1ae93dfcc7c65e` | `2849a32685e7e3077460b41c80449e93c43f2cea24bdbb96562c79c68bf38996` |
| drives | 3 | 11 | `12fdf2f045aaa197b7b56db253bbadb26f469923a229eda847cfe7cf4a6bbaa9` | `a23c87bbef5cc44ed328ec6eaaec4e697a1643da92f58b7d40fa627881cb8629` |
| people | 2 | 7 | `34fba589d88ab54692264824664d3b93b267c868fba6fd5cf6a05ad28bdbc89a` | `6ba5624bcd145373bb9ba822b533415c4016b7ecdae39c7df1319db8cde3a60c` |
| people | 2 | 11 | `5a32be07f96e4292ded949756ddc88bba942c2cd20eedd1acc6e793ab0b0c73a` | `42641d107ecfe63ce8b046e8533ed56107986775093ccdaf00af192b58bd0881` |
| affect | 3 | 7 | `518b0dedaa8042de85118c609aeb5d7ff586421d0f2dc59008b08895e338dbdc` | `3349702498368fd36ecbd54d5a032c42e1e259a25aa577e39bd907a0f1c18703` |
| affect | 3 | 11 | `0f5a9c90fff6ca3965194272913b1f56c805dd3eee451adcf1b440acb26baf5e` | `2941da04abf76e885a7a75f5ec4590076898b84007ead932ca3024a14820601c` |
| opinions | 3 | 7 | `8dca5fd169f109cd98d833f0207d01a0e0230671211c8190ca47cf0dbd8cbdc1` | `60d69f848d738197b16e6cb932b342d592ec462c813ca2fa62192ce7756b7db9` |
| opinions | 3 | 11 | `adfd8420b3531fe7e919af7bb4c4804e201230768b98749c71d455a2c00f8794` | `8916cb62eb4d51db8b12272e8160cbb95823f929c323242e362535cf02318ff3` |
| memory | 3 | 7 | `855a8d4e6ab0075cf78e8e3393e313eca3c2b8c3f4e57877cf3d8eccefc6c8b3` | `18b2ed5741dd0bbaa441d48d85083c099c1133bd74fd8a907b26a18264f267d7` |
| memory | 3 | 11 | `7873b8a212af57364f74e162f75f8fb9550f1d59123b8e0a84ffd0f80aed3a9b` | `15595ff53f321be18125bcc91db852a42871c19fe83e197669d2de7ab383937b` |
| identity | 3 | 7 | `7adcbd5b230f47f2c61b304bb14b9ed6f3cbf171527a704a9cc3da989d463b2a` | `b6e780c0b4cb69e2b3173ffb5e315ab9126f16f3569398a0bd595c3d3908080c` |
| identity | 3 | 11 | `65a4248f58dc3247394715b09718b4627ba328c3a28fbf37c8d8e0dc7de06e99` | `80dde9c5b4936d863967809b5932d400b0bfc948d9d40f4bfabd0ad39297e1f3` |
| improve | 1 | 7 | `378c76faeb7d15c418c286d5633aa74e23a2663a5e6eb70e8c0fe000d5f1911a` | `9807464b74ba319a42ac0c567aa0a0f8258c035bad90c9ca197859e3b14e5904` |
| improve | 1 | 11 | `7c92290ef2b11a4aa8150d33ef2156e53279a93f10562aa987f3915cd931ada9` | `9b0650fdd7eac8dc1183e9f2af7f354b8e9bd2805a6daae4e3ee9b3c0586dcf4` |
