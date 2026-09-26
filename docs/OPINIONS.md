# Opinions

An opinion is a reasoned, fallible view the agent holds and acts on: a recommendation
between options, an assessment of an approach, a view on a topic, or a narrow view of a
person's demonstrated reliability in one activity. Opinions are not facts, owner
preferences or grants. They change only on new evidence, and every change is an entry in
the agent's autobiography.

Code: the store is `sidecar/protagine/self_model/judgments.py` (`SelfJudgments`); forming
and using opinions is `sidecar/protagine/mind/opinions.py`; the owner surfaces are
`sidecar/protagine/api/routers/opinions.py`, `protagine mind opinions` and the
opinion verbs of the `protagine_self` tool. Architecture 3.1, 4.4 and 4.9; build plan M7.

## The store

One store in the ledger database (`turn-idempotency.db`). A stance has a subject
(`topic`, `person` with a contact id, or `approach` with a failure signature), a
normalized topic, the stance, a reason, a certainty (`tentative | moderate | strong`),
`revise_if` (the specific evidence that would change it), its premises and, for an
approach, a class (`avoid | prefer`). Each change is a new revision that supersedes the
head, so the history is kept; `GET /v1/mind/opinions/{id}` shows the chain, where a
replaced revision reads `superseded`. A revision rests first on its new evidence, then on
the data the old view rested on (kept, so it can never come back as new); the agent's
earlier words stay a dependency of the chain, so forgetting them still removes it, but they
are not a premise of the view that replaced them.

Premises are explicit:

| Kind | What it is | Forms | Revises |
|---|---|---|---|
| `claim` | an admitted source claim (decision, procedure or substantive event) of a turn of any contact, bound to that contact | yes | yes |
| `outcome` | a settled mind task, verified by the owner, a check or a Hermes failure | yes | yes |
| `finding` | a research or investigation finding the mind stored (public only when it researched an interest the agent declared itself) | yes | yes |
| `statement` | the agent's own reply in the same turn | yes, once per session and subject a day | never |
| `quote` | a citation carried over from a migrated appraisal judgment | carried only | never |

**The new-premise rule** (enforced by the store, not the model). A revision needs at least
one premise, named as new evidence, that is of a revising kind, still current (admitted,
retained, not corrected or erased), and not already cited, either by reference or by
content: the same record under a fresh id (`s-12` and `s-40` with the same figures) is the
same premise. Only ids and hashes are erased for that comparison, never figures: a record id
has at least two digits and is never a year (`FY-2025`) or a one-digit version (`gpt-5`), a
hash has a letter and a digit (or 16 characters), so a quote that went from 12500000 to
98000000 is new evidence. Forming on a topic that already has a view is treated as a
revision, and forming under another topic is no way around the rule either: a new view
whose revising premises a current view of the same subject already cites is refused
(`no_new_premise`), and a view resting on the agent's words alone forms once per session
and subject a day (`duplicate_topic`) and never on a matter a current view is about, in any
session (most of that view's topic words appear in the new topic, the stance, the agent's
words or what the speaker said). Doubt, insistence, flattery and appeals to authority carry
no admitted premise, and the agent's own words never revise, so pushback alone can never
change a view.

**The owner's decision is authority, not evidence.** Insistence that carries a choice ("Plan
Birch is the plan") is admitted as a claim of kind `decision` from the owner's own turn. It
decides what is done, not which option is better: it satisfies the new-premise rule on no
route (a revise that names it, a form on the same topic, a form under another topic), and it
is never a premise of a view, so a decision alone forms none (`invalid:support`). A proposal
that brings it is kept beside the view it bears on as `owner_decision` (disposition
`owner_decision`; the view is unchanged unless the proposal also brings new evidence, and a
revision carries the decision forward). Forgetting where the decision was said removes the
decision and leaves the view. A contact's decision stays an ordinary claim.

**The soft limit.** Forming is not revising. A view changes at most once per rolling 24 h;
a limited revision waits for the window and is not dropped. A correction to a premise the
view cites, a verified task outcome and an owner reconsideration bypass the limit.

**Audience.** A topic stance resting only on outcomes and public findings is shown to
everyone. A finding is public only when it researched an interest the agent declared itself;
one about the owner's question, a goal step, an investigation of work done for the owner, or
an interest the owner's own words raised is the owner's. Every other stance (anything formed
from a conversation, every person and approach opinion) is shown only in the owner's turns,
and so is anything the pass writes with an owner-only view in sight. A revision never widens
the audience: a view formed in the owner's conversation stays the owner's when a finding
revises it or an owner reconsideration keeps only that finding. The owner's withdraw and
reconsider rows, with their reasons, are the owner's. A guest never sees an owner-audience
view, in context, in the list route or by id, and sees an everyone view without what it
rests on (in context no premise is quoted; the routes give each premise's kind and role).

**Erasure.** Forgetting a source that a view rests on tombstones the view and deletes its
autobiography entries; an older view never revives.

## Forming opinions

**From turns: the opinion pass.** The projection worker runs one opinion job at a time,
after the turn's claim job. A turn costs a model call only when it has an admitted premise,
or it is an owner turn whose message asks for a judgment (a recommendation, a choice, a
comparison, "what do you think") and the agent replied. Everything else finishes as
`no_premise` with no call: small talk, and every "are you sure?". The call is one tool-less
request on the `reasoning` role (task `self_judgment`) with the turn, its admitted premises,
the agent's reply and the few relevant existing views (with what they rest on and their
`revise_if`). It answers `none`, `form` or `revise`; a `revise` must name each new premise
and say why it is new, relevant and contrary. The store then applies the rule above.
Output that does not validate is retried with backoff and gives up after three attempts;
jobs older than 48 h are dropped as stale.

**From findings.** A stored research finding is a premise; the pass may form or revise a
topic view from it.

**From task outcomes (no model call).** When the last three settled attempts at the same
work (the mastery drive's failure signature, `type:topic`) within 30 days all failed, the
mind forms an `avoid` approach opinion resting on those outcomes: "do not repeat what they
did; take a different approach, or stop and report what is missing". A verified success at
that work turns it into `prefer`: a success a check confirmed, or one the owner verified.
A done report whose success check failed is a failure of the work (it counts in the run of
three, and never as a success), and a report's bare claim of a check that never ran
verifies nothing. A fourth failure agrees with `avoid`; an unverified success changes
nothing.

**What is a model judgment.** Whether a new premise is relevant to the view and cuts
against it is the model's call, tested against the view's own `revise_if`. The store blocks
the deterministic cases (no admitted premise, the agent's own words, the same reference,
the same content); a paraphrased restatement is left to the model.

## Using opinions

**Turn context.** Every `POST /v1/host/context/assemble` may carry a `protagine-stances`
section ("Your recorded views", priority 87, at most 1,400 characters, no model call): up
to three relevant views the viewer may see (the views themselves are ranked over their topic,
stance, reason, what would change them and what they rest on, fused with the semantic
index; views from the current session come first among those that match and fill any slot
left), each with its id, reason, up to two premises
(cited by the source the agent can open, `turn:<id>`, as recall cites it, or by
`intention:<id>`) and what would change it, with the owner's decision on its matter on a line of its own
("The owner decided: ... your view stays as recorded", owner only), then the standing sentence: change a view
only on new evidence, which the owner's decision is not; asked what it recommends, the agent gives its recorded
view, does what the owner decides, and says so when it disagrees.
When newer turns from the viewer have not been weighed yet, a line says so. For an owner
turn that asks for a judgment when no view is relevant yet, the section only asks the
agent to state its position and the evidence it rests on, because that reply is what the
next pass can form a view from.

**Task bodies.** When the mind forms a task, the current approach view for the same work
is appended to the body ("Your recorded view on this work [opinion N]: ...") and its id is
kept on the intention (`context.opinion_ids`); `GET /v1/mind/dispatch` sends it with the
body. A view flags the work and never holds it back: the three failures that form an
`avoid` view also trip the breaker, so the next attempt is usually an ask, and when the
owner authorizes it, it runs with the view in its body.

## Owner surfaces

```
GET  /v1/mind/opinions?q=&contact_id=&by=&history=false&limit=10   {enabled, opinions}
GET  /v1/mind/opinions/{id}?contact_id=&by=                        {opinion, history}
POST /v1/mind/opinions/{id}/withdraw    {reason, contact_id?, by?, correction_id?}
POST /v1/mind/opinions/{id}/reconsider  {reason, contact_id?, by?, correction_id?}

protagine mind opinions [list|show <id>|withdraw <id>|reconsider <id>] [--query Q] [--history] [--reason R]
```

The viewer is the owner when `contact_id` is the owner's or `by` is `cli`; anyone else sees
only everyone-audience views, and an owner-audience id is a 404. Withdraw and reconsider
are owner-only (403 otherwise). Withdrawing hides the view at once and keeps the topic
withdrawn until the owner reconsiders it. Reconsidering keeps the view withdrawn while the
pass re-reads the view's own premises with the owner's reason as context: it revises the
view only as those premises support, or answers `none`, and the view stays withdrawn. The
owner's wording is never installed as the agent's view. The request waits while the faculty
is off and never goes stale, and one that lost its job (a legacy head) is queued again at
start.

The `protagine_self` tool offers `opinions [query]`, `why <number>`, and for the owner's
own interactive session `withdraw` and `reconsider` (the opinions ride that tool rather
than an eighth one, so the tool schemas stay within their budget, integration map X8);
reads are filtered for the session's participant, and a guest, a kanban worker or a cron
run cannot change anything.

"Why did you change your mind?" is ordinary recall: formation, revision and withdrawal
each write one owner-audience autobiography entry (`mind:opinion:<id>:<event>`).

## The switch

`mind.faculties.opinions` (on in `config.DEFAULTS`, the release-candidate value). Off: jobs
finish `faculty_off` without a call (an owner's reconsider request waits for the faculty),
no approach opinion is formed, no stance section or
task-body line is rendered, and the list answers `{"enabled": false, "opinions": []}`;
stored views are kept, and what the agent is given (every context section, every task
body) is what it was given without the faculty. The running mind reads the flag once at
start; the opinion pass asks the running mind, so the three uses never disagree, and reads
`protagine.yaml` only in a process that serves no mind. `mind.enabled` counts as
configured: the runtime off switch stops effects, and forming views is memory. The
benchmark's `full-opinions` arm is this flag off, served to the mind by the worker's mind
section. The opinion verbs of `protagine_self` stay listed when the faculty is off, as the
other adapter tools stay listed with theirs. The self-narrative lists no stance with the
faculty off, and never a view about a person (integration map X15, X7).

## What is measured

The `mind-opinions-1` family (`docs/proto-agi/families/mind-opinions-1.md`) compares `full`
with `full-opinions`: a stance formed from records must hold under pushback and
pseudo-evidence across a restart, change on a genuinely new record or a correction, and a
flawed plan must be carried out while the record still names the plan the evidence
favours. The SYCON-style anchor (`benchmarks/paired/anchors/sycon_pushback.py`, described
in `docs/PAIRED-AGENT-BENCHMARK.md`) reports Turn-of-Flip and Number-of-Flip under four
kinds of pressure, descriptively.

## Upgrade notes

The appraisal `judgment` kind is gone: its current and withdrawn heads were moved into the
opinion store as person opinions (with their quotations as `quote` premises) at the first
start after `protagine upgrade`; a `dimension topic` longer than 80 characters is cut at a
word and told apart by a short tag, so two judgments never collapse into one. Owner control
rows written by an earlier build are made owner-audience at start. The old run queue (`self_judgment_runs`) is retired, and the
opt-in and interval variables of the earlier self-judgment feature are no longer read.
